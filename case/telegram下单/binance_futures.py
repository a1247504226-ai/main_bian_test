"""
币安 U 本位合约 API 客户端（自实现签名，无第三方 SDK 依赖）

设计要点：
1. 所有下单必须带 newClientOrderId —— 幂等，防网络超时重试导致重复开仓
2. 自动读取 exchangeInfo 的 stepSize / tickSize，数量价格按精度取整（否则报 -1111）
3. 监控 X-MBX-USED-WEIGHT-1M 响应头，接近上限自动降速
4. 支持 testnet，默认不连主网
"""
import hashlib
import hmac
import math
import time
import logging
from collections import deque
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"

# 合约 API 权重上限（每分钟）
WEIGHT_LIMIT_1M = 2400
# 本地软上限：留 600 余量给突发，到线就先等，绝不硬撞 429/418
WEIGHT_SOFT_LIMIT = 1800

# 各接口权重（币安文档值）。用于本地预算和诊断统计
_ENDPOINT_WEIGHT = {
    ("GET", "/fapi/v2/balance"): 5,
    ("GET", "/fapi/v2/positionRisk"): 5,
    ("GET", "/fapi/v1/order"): 1,
    ("POST", "/fapi/v1/order"): 1,
    ("DELETE", "/fapi/v1/order"): 1,
    ("DELETE", "/fapi/v1/allOpenOrders"): 1,
    ("GET", "/fapi/v1/openOrders"): 1,
    ("GET", "/fapi/v1/allOrders"): 5,
    ("GET", "/fapi/v1/premiumIndex"): 1,
    ("GET", "/fapi/v1/exchangeInfo"): 1,
    ("GET", "/fapi/v1/time"): 1,
    ("GET", "/fapi/v1/ping"): 1,
    ("POST", "/fapi/v1/leverage"): 1,
    ("POST", "/fapi/v1/marginType"): 1,
    ("GET", "/fapi/v1/userTrades"): 5,
    ("GET", "/fapi/v1/positionSide/dual"): 30,
}


class BinanceError(Exception):
    """币安业务错误（有 code / msg）"""

    def __init__(self, code: int, msg: str, http_status: int = 0):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(f"[{code}] {msg} (http {http_status})")


class BinanceFutures:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        recv_window: int = 5000,
        timeout: int = 10,
        offline: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = TESTNET if testnet else MAINNET
        self.recv_window = recv_window
        self.timeout = timeout
        self.offline = offline
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

        # 精度缓存 {symbol: {"step": Decimal, "tick": Decimal, "minQty": Decimal, "minNotional": Decimal, "qtyPrecision": int, "pricePrecision": int}}
        self._filters: Dict[str, Dict[str, Any]] = {}
        # 时间偏移（本机与服务器时差）。懒同步：第一次签名请求前才校准
        self._time_offset = 0
        self._time_synced = False
        # markPrice 短缓存 {symbol: (价格, 时间戳)}，避免同一币反复请求
        self._price_cache: Dict[str, tuple] = {}
        self._price_ttl = 3.0
        # exchangeInfo 是否已全量加载（只拉一次）
        self._info_loaded = False
        # 离线模式的虚拟账户
        self._off_equity = 500.0
        self._off_positions: Dict[str, Dict[str, Any]] = {}

        # ---------------- 日志限流 ----------------
        # 连接类错误（网络异常 / 5xx / 429 / 时间偏差）同类 60 秒最多一条，
        # 否则币安不可达时每笔单重试 3 次会把日志刷满
        self._warn_at: Dict[str, float] = {}

        # ---------------- 封禁 / 冷却 ----------------
        # 418 是 IP 封禁，重试只会让封禁时间翻倍（2 分钟 → 5 小时）。
        # 记下解封时间，期间所有请求直接本地失败，一个包都不发
        self._banned_until = 0.0
        self._cool_until = 0.0
        self._ban_logged = False

        # ---------------- 本地权重预算（滑动 60 秒）----------------
        # 服务端按 IP 每分钟 2400 计。发请求前先看自己的账，
        # 到软上限就等窗口释放，绝不硬撞 429/418
        self._w_win: deque = deque()          # [(时间戳, 权重)]
        self._w_used_header = 0               # 最近一次服务端报的权重
        self._last_warn = 0.0                 # 权重告警限流（10 秒一次）

        # ---------------- 接口调用统计（诊断「谁在刷接口」）----------------
        self._stat: Dict[str, int] = {}
        self._stat_since = time.time()

        # ---------------- 读接口短缓存 ----------------
        # 巡检 + 开仓 + 平仓可能在同一秒内重复查余额/持仓，
        # 缓存 1.5 秒能砍掉大量重复权重；下单后立即失效
        self._cache_ttl = 1.5
        self._bal_cache = None                # (时间戳, dict)
        self._pos_cache = None                # (时间戳, list)
        # 杠杆/保证金模式只需按币种设一次，避免每笔开仓都发两个请求
        self._lev_set = set()                 # {(symbol, leverage)}
        self._mt_set = set()                  # {symbol}
        # 持仓模式：None=未探测，False=单向(One-way)，True=双向(Hedge)
        # 单向模式下报单不能带 positionSide（会 -4061），双向模式下必须带且不能用 reduceOnly
        self._dual_side: Optional[bool] = None

        if offline:
            log.info("离线模式：不连接币安（回放/测试用，零 API 权重）")
            return

    # ------------------------------------------------------------------
    # 离线模拟（回放、单元测试用）
    # ------------------------------------------------------------------
    def _offline_response(self, method: str, path: str, params: Dict[str, Any]):
        m = method.upper()
        if path == "/fapi/v1/time":
            return {"serverTime": int(time.time() * 1000)}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": s, "quantityPrecision": 4,
                                 "pricePrecision": 6,
                                 "filters": [
                                     {"filterType": "LOT_SIZE", "stepSize": "0.0001",
                                      "minQty": "0.0001"},
                                     {"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
                                     {"filterType": "MIN_NOTIONAL", "notional": "5"},
                                 ]} for s in ["BTCUSDT", "ETHUSDT"]]}
        if path == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": str(self._off_equity),
                     "availableBalance": str(self._off_equity),
                     "crossUnPnl": "0"}]
        if path == "/fapi/v2/positionRisk":
            want = (params or {}).get("symbol")
            out = []
            for sym, p in self._off_positions.items():
                if want and sym != want.upper():
                    continue
                out.append({"symbol": sym, "positionAmt": str(p["amount"]),
                            "entryPrice": str(p["entry"]), "markPrice": str(p["mark"]),
                            "unRealizedProfit": "0", "leverage": str(p.get("leverage", 3))})
            return out
        if path == "/fapi/v1/premiumIndex":
            sym = (params or {}).get("symbol", "")
            px = self._off_positions.get(sym, {}).get("mark", 1.0)
            return {"symbol": sym, "markPrice": str(px)}
        if path == "/fapi/v1/order":
            qty = float((params or {}).get("quantity", 0) or 0)
            sym = (params or {}).get("symbol", "")
            is_reduce = str((params or {}).get("reduceOnly", "")).lower() == "true"
            otype = (params or {}).get("type", "")
            if otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                return {"orderId": int(time.time() * 1000) % 1000000, "status": "NEW"}
            if is_reduce:
                px = self._off_positions.get(sym, {}).get("mark", 1.0)
                self._off_positions.pop(sym, None)
            else:
                px = self._off_positions.get(sym, {}).get("mark", 1.0)
                side = (params or {}).get("side", "BUY")
                self._off_positions[sym] = {
                    "symbol": sym, "amount": qty if side == "BUY" else -qty,
                    "entry": px, "mark": px, "leverage": 3}
            return {"orderId": int(time.time() * 1000) % 1000000,
                    "avgPrice": str(px),
                    "executedQty": str(qty), "status": "FILLED"}
        if path == "/fapi/v1/allOpenOrders":
            return {"code": 200, "msg": "ok"}
        if path in ("/fapi/v1/leverage", "/fapi/v1/marginType"):
            return {"code": 200, "msg": "ok"}
        return {}

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _sync_time(self, force: bool = False):
        """同步服务器时间，避免 -1021 timestamp outside recvWindow

        懒同步：只在第一次签名请求前调一次；force=True 用于 -1021 后强制重新校准。
        用短超时：失败也不影响下单（签名请求遇到 -1021 会自动重试）
        """
        if self.offline:
            return
        if self._time_synced and not force:
            return
        if self._banned_until > time.time():
            return          # 封禁期间一个包都不发
        try:
            r = self.session.get(f"{self.base}/fapi/v1/time", timeout=5)
            r.raise_for_status()
            server_ts = r.json()["serverTime"]
            self._time_offset = server_ts - int(time.time() * 1000)
            self._time_synced = True
            log.info("时间同步完成，偏移 %d ms", self._time_offset)
        except Exception as e:
            log.debug("时间同步失败（签名请求遇到 -1021 会自动重试）: %s", e)

    def _sign(self, params: Dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        sig = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={sig}"

    # ------------------------------------------------------------------
    # 权重预算 / 统计
    # ------------------------------------------------------------------
    @staticmethod
    def _weight_of(method: str, path: str) -> int:
        return _ENDPOINT_WEIGHT.get((method.upper(), path), 1)

    def _warn_limited(self, key: str, msg: str, *args):
        """同类连接类警告 60 秒最多一条"""
        now = time.time()
        if now - self._warn_at.get(key, 0.0) < 60:
            return
        self._warn_at[key] = now
        log.warning(msg, *args)

    def _w_trim(self, now: float) -> int:
        """清掉 60 秒前的记录，返回「本进程」当前窗口已用权重

        注意：只算自己发的请求。服务端权重是整个 IP 共享的，
        如果同一出口 IP 上还有别的程序/机器在打币安，那个数字与本进程无关，
        拿它来限速只会把自己卡死（等 60 秒也没用，别人还在刷）。
        """
        while self._w_win and now - self._w_win[0][0] > 60:
            self._w_win.popleft()
        return sum(w for _, w in self._w_win)

    def _w_wait(self, path: str, weight: int):
        """发请求前先卡「本进程」预算。到软上限就等窗口释放，宁可慢也不撞限频

        服务端每分钟清零，所以最多等 60 秒；超过 65 秒还降不下来
        （说明是别的进程/别的机器在同一 IP 上刷），就不再等，硬着头皮发，
        避免机器人被卡死。
        """
        deadline = time.time() + 65
        warned = False
        while True:
            now = time.time()
            used = self._w_trim(now)
            if used + weight <= WEIGHT_SOFT_LIMIT:
                return
            if not self._w_win:
                return
            wait = 60 - (now - self._w_win[0][0]) + 0.2
            wait = min(wait, deadline - now)
            if wait <= 0.2:
                log.warning("本进程权重预算 %.0f 秒内降不下来（%d/%d），继续发送 %s",
                            65, used, WEIGHT_SOFT_LIMIT, path.rsplit("/", 1)[-1])
                return
            if not warned:
                warned = True
                log.warning("本进程权重预算已满 %d/%d，等 %.1fs 再发 %s（避免 429/418）",
                            used, WEIGHT_SOFT_LIMIT, wait, path.rsplit("/", 1)[-1])
            time.sleep(wait)

    def _w_record(self, path: str, weight: int, used_header: Optional[str]):
        """记录本次请求的权重消耗，并累计接口统计"""
        now = time.time()
        self._stat[path] = self._stat.get(path, 0) + 1
        self._w_win.append((now, weight))
        if used_header:
            try:
                self._w_used_header = int(used_header)
            except ValueError:
                pass

    def _w_diagnose(self, used: str):
        """服务端权重高时，判断是自己刷的还是别人刷的"""
        now = time.time()
        if now - self._last_warn < 60:
            return
        self._last_warn = now
        try:
            n = int(used)
        except (TypeError, ValueError):
            return
        own = self._w_trim(now)
        ratio = n / WEIGHT_LIMIT_1M * 100
        # 80% 以下正常波动，不刷日志
        if n < WEIGHT_LIMIT_1M * 0.8:
            return
        if own * 4 < n:
            # 自己几乎没用量，服务端却很高 → 同一 IP 上有别的程序/机器
            log.warning(
                "API 权重 %d/%d（%.0f%%），但本进程近 60 秒只用了 %d —— "
                "消耗不是本进程产生的。请确认没有重复启动（start.bat + 开机自启各一个），"
                "或同一个出口 IP 上还有别的机器在跑",
                n, WEIGHT_LIMIT_1M, ratio, own)
        else:
            log.warning("API 权重 %d/%d（%.0f%%）已超 90%%，本进程近 60 秒用了 %d。"
                        "近 60s 请求: %s",
                        n, WEIGHT_LIMIT_1M, ratio, own, self._stat_desc())

    def _stat_desc(self, top: int = 5) -> str:
        if not self._stat:
            return "无"
        items = sorted(self._stat.items(), key=lambda kv: -kv[1])[:top]
        return " ".join(f"{p.rsplit('/', 1)[-1]}×{c}" for p, c in items)

    def _stat_maybe_flush(self):
        """每 60 秒打一行接口统计（只在有量或权重偏高时打，避免刷屏）"""
        now = time.time()
        if now - self._stat_since < 60:
            return
        total = sum(self._stat.values())
        if total >= 10 or self._w_used_header > WEIGHT_LIMIT_1M * 0.8:
            log.info("近 %d 秒共 %d 次请求（本进程权重 %d，服务端权重 %d/%d）: %s",
                     int(now - self._stat_since), total,
                     self._w_trim(now), self._w_used_header, WEIGHT_LIMIT_1M,
                     self._stat_desc())
        self._stat.clear()
        self._stat_since = now

    def _invalidate(self):
        """下单/撤单后账户状态变了，缓存必须立刻失效"""
        self._bal_cache = None
        self._pos_cache = None

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = True,
        max_retries: int = 3,
    ) -> Any:
        params = dict(params or {})
        if self.offline:
            return self._offline_response(method, path, params)

        # 第一次签名请求前才校准时间：启动时不做任何网络请求
        if signed and not self._time_synced:
            self._sync_time()

        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            params["recvWindow"] = self.recv_window

        # 封禁/冷却期间：直接本地失败，不发请求。
        # 418 期间每多发一个包，封禁时间都会再加长
        now0 = time.time()
        if self._banned_until > now0:
            left = int(self._banned_until - now0)
            if not self._ban_logged:
                self._ban_logged = True
                log.error("IP 处于币安封禁中，还需 %d 分 %d 秒。"
                          "期间不发任何请求（避免封禁被延长）", left // 60, left % 60)
            raise BinanceError(-1003, f"IP 封禁中，还需 {left} 秒", 418)
        if self._cool_until > now0:
            time.sleep(min(self._cool_until - now0, 30))
        self._ban_logged = False

        url = f"{self.base}{path}"
        last_err: Optional[Exception] = None

        weight = self._weight_of(method, path)

        for attempt in range(1, max_retries + 1):
            try:
                # 发之前先看本地预算：到软上限就等窗口释放，
                # 从源头避免 429/418（事后 sleep 没用，权重已经花掉了）
                self._w_wait(path, weight)

                # 必须按真实 HTTP 方法发：撤单接口是 DELETE，
                # 当成 POST 发会 404，导致旧保护单永远撤不掉
                m = method.upper()
                q = self._sign(params) if signed else urlencode(params)
                full = f"{url}?{q}"
                if m == "GET":
                    resp = self.session.get(full, timeout=self.timeout)
                elif m == "DELETE":
                    resp = self.session.delete(full, timeout=self.timeout)
                elif m == "PUT":
                    resp = self.session.put(full, timeout=self.timeout)
                else:
                    resp = self.session.post(full, timeout=self.timeout)

                # 记账 + 接口统计（谁在刷接口一眼能看到）
                used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                self._w_record(path, weight, used)
                self._stat_maybe_flush()
                if used:
                    self._w_diagnose(used)

                if resp.status_code == 200:
                    return resp.json()

                # 4xx 业务错误：不重试（重试也没用，且可能重复下单）
                try:
                    body = resp.json()
                    code = body.get("code", -1)
                    msg = body.get("msg", resp.text)
                except Exception:
                    code, msg = -1, resp.text

                # -1021 时间不同步：重同步后重试
                if code == -1021:
                    self._warn_limited("time_drift", "时间戳偏差，重新同步时间")
                    self._sync_time(force=True)
                    params["timestamp"] = int(time.time() * 1000) + self._time_offset
                    continue

                # 418 = IP 封禁（权重超限），429 = 限频
                # 关键：418 绝不能重试！盲目重试只会延长封禁时间（可能从 2 分钟变成几小时）
                if resp.status_code in (418, 429):
                    ra = resp.headers.get("Retry-After", "")
                    try:
                        wait = int(ra)
                    except (ValueError, TypeError):
                        wait = 120 if resp.status_code == 418 else 30
                    if resp.status_code == 418:
                        # 记下解封时间：期间不再发包，否则封禁时间会越来越长
                        self._banned_until = time.time() + wait
                        self._ban_logged = False
                        raise BinanceError(
                            -1003,
                            f"IP 被币安封禁（418），需等 {wait} 秒（{wait/3600:.1f} 小时）。"
                            f"期间本程序不再发任何请求，避免封禁被延长",
                            418)
                    self._cool_until = time.time() + wait
                    self._warn_limited("429", "触发限频 429，按 Retry-After 等待 %d 秒", wait)
                    time.sleep(wait)
                    last_err = BinanceError(code, msg, resp.status_code)
                    continue

                # 5xx 服务端错误：退避重试
                if resp.status_code >= 500:
                    wait = 2 ** attempt
                    self._warn_limited("5xx", "HTTP %d，%d 秒后重试 (%d/%d)",
                                    resp.status_code, wait, attempt, max_retries)
                    time.sleep(wait)
                    last_err = BinanceError(code, msg, resp.status_code)
                    continue

                raise BinanceError(code, msg, resp.status_code)

            except (requests.Timeout, requests.ConnectionError) as e:
                wait = 2 ** attempt
                self._warn_limited("network", "网络异常 %s，%d 秒后重试 (%d/%d)",
                                   type(e).__name__, wait, attempt, max_retries)
                time.sleep(wait)
                last_err = e

        raise last_err if last_err else RuntimeError("请求失败")

    # ------------------------------------------------------------------
    # 交易规则 / 精度
    # ------------------------------------------------------------------
    def load_exchange_info(self, symbols: Optional[List[str]] = None):
        """加载交易对精度规则，缓存到 self._filters

        全量只拉一次（响应约 1MB，权重 1）。旧版每遇到一个新币种就重新拉一遍
        全量接口，频道里几百个币种等于打几百次 —— 这是 418 的重要来源之一
        """
        if self._info_loaded:
            return
        data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in data["symbols"]:
            sym = s["symbol"]
            f = {"step": None, "tick": None, "minQty": None,
                 "minNotional": None,
                 "qtyPrecision": s.get("quantityPrecision", 8),
                 "pricePrecision": s.get("pricePrecision", 8)}
            for flt in s["filters"]:
                t = flt["filterType"]
                if t == "LOT_SIZE":
                    f["step"] = Decimal(flt["stepSize"])
                    f["minQty"] = Decimal(flt["minQty"])
                elif t == "PRICE_FILTER":
                    f["tick"] = Decimal(flt["tickSize"])
                elif t == "MIN_NOTIONAL":
                    f["minNotional"] = Decimal(flt.get("notional", "5"))
            self._filters[sym] = f
        self._info_loaded = True
        log.info("已加载 %d 个交易对的精度规则", len(self._filters))

    def get_filters(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        # 离线模式：任何币种都给一套通用规则，不查 exchangeInfo
        # （回放时数据里几百个币种，不可能逐个预置）
        if self.offline and symbol not in self._filters:
            self._filters[symbol] = {
                "step": Decimal("0.0001"), "tick": Decimal("0.000001"),
                "minQty": Decimal("0.0001"), "minNotional": Decimal("5"),
                "qtyPrecision": 4, "pricePrecision": 6,
            }
        if symbol not in self._filters:
            self.load_exchange_info([symbol])
        if symbol not in self._filters:
            raise BinanceError(-1, f"交易对 {symbol} 不存在或未在合约市场上市")
        return self._filters[symbol]

    def round_qty(self, symbol: str, qty: float) -> float:
        """数量向下取整到 stepSize（必须向下，向上会超出余额）

        低于 minQty 时返回 0.0，让调用方明确拒绝，而不是下单后收到 -1013
        """
        f = self.get_filters(symbol)
        step = f["step"]
        if not step:
            result = round(qty, f["qtyPrecision"])
        else:
            d = Decimal(str(qty))
            n = (d / step).to_integral_value(rounding=ROUND_DOWN)
            result = float(n * step)
        if f.get("minQty") is not None and result < float(f["minQty"]):
            return 0.0
        return result

    def round_price(self, symbol: str, price: float, mode: str = "down") -> float:
        """价格取整。

        mode="down" 向下（数量/止盈用它，成交概率更高）
        mode="up"   向上（空单止损用它：止损在入场价上方，向上取整 = 离入场更远，
                    不会被一个 tick 的误差提前扫掉）
        """
        f = self.get_filters(symbol)
        tick = f["tick"]
        if not tick:
            return round(price, f["pricePrecision"])
        d = Decimal(str(price))
        rounding = ROUND_UP if mode == "up" else ROUND_DOWN
        n = (d / tick).to_integral_value(rounding=rounding)
        return float(n * tick)

    # ------------------------------------------------------------------
    # 账户 / 持仓
    # ------------------------------------------------------------------
    def get_balance(self, asset: str = "USDT", fresh: bool = False) -> Dict[str, float]:
        """账户权益。带 1.5 秒缓存：巡检/开仓/平仓常在同一秒重复查，白花 5 权重"""
        now = time.time()
        if not fresh and self._bal_cache and now - self._bal_cache[0] < self._cache_ttl:
            return dict(self._bal_cache[1])
        data = self._request("GET", "/fapi/v2/balance")
        out = {"total": 0.0, "available": 0.0, "unrealized": 0.0}
        for b in data:
            if b["asset"] == asset:
                out = {
                    "total": float(b["balance"]),
                    "available": float(b["availableBalance"]),
                    "unrealized": float(b["crossUnPnl"]),
                }
                break
        self._bal_cache = (now, out)
        return dict(out)

    def get_positions(self, symbol: Optional[str] = None,
                      fresh: bool = False) -> List[Dict[str, Any]]:
        """持仓。带 1.5 秒缓存；下单/撤单后由 _invalidate() 立即失效"""
        now = time.time()
        cache = self._pos_cache
        if not fresh and cache and now - cache[0] < self._cache_ttl:
            rows = cache[1]
        else:
            params = {"symbol": symbol.upper()} if symbol else {}
            data = self._request("GET", "/fapi/v2/positionRisk", params)
            rows = []
            for p in data:
                amt = float(p["positionAmt"])
                if abs(amt) < 1e-12:
                    continue
                rows.append({
                    "symbol": p["symbol"],
                    "amount": amt,
                    "side": "LONG" if amt > 0 else "SHORT",
                    "entry": float(p["entryPrice"]),
                    "mark": float(p["markPrice"]),
                    "unrealized": float(p["unRealizedProfit"]),
                    "leverage": int(p["leverage"]),
                })
            # 只有全量查询才写缓存（带 symbol 的只是子集）
            if not symbol:
                self._pos_cache = (now, rows)
        if symbol:
            up = symbol.upper()
            return [r for r in rows if r["symbol"] == up]
        return [dict(r) for r in rows]

    def get_position_mode(self) -> bool:
        """账户是双向持仓(hedge)还是单向(one-way)。结果缓存，只查一次

        这个区别很关键：
          单向：报单**不能**带 positionSide（带了报 -4061），平仓用 reduceOnly
          双向：报单**必须**带 positionSide，且不能同时用 reduceOnly
        搞错了 → 开仓成功但止盈止损全部挂不上（裸仓）
        """
        if self._dual_side is None:
            try:
                d = self._request("GET", "/fapi/v1/positionSide/dual")
                self._dual_side = bool(d.get("dualSidePosition"))
                log.info("持仓模式：%s", "双向(hedge)" if self._dual_side else "单向(one-way)")
            except Exception as e:
                # 查不到就按单向处理（绝大多数账户是单向）
                log.warning("查询持仓模式失败（%s），按单向处理", e)
                self._dual_side = False
        return self._dual_side

    def set_leverage(self, symbol: str, leverage: int):
        """同一币种同一杠杆只需设一次，之后直接跳过（省 1 权重/笔）"""
        key = (symbol.upper(), int(leverage))
        if key in self._lev_set:
            return {"msg": "cached"}
        r = self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol.upper(), "leverage": leverage})
        self._lev_set.add(key)
        return r

    def set_margin_type(self, symbol: str, isolated: bool = True):
        """设置逐仓/全仓。已是该模式时币安返回 -4046，忽略即可"""
        key = symbol.upper()
        if key in self._mt_set:
            return {"msg": "cached"}
        try:
            r = self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol.upper(),
                "marginType": "ISOLATED" if isolated else "CROSSED",
            })
        except BinanceError as e:
            if e.code == -4046:  # No need to change margin type
                self._mt_set.add(key)
                return {"msg": "margin type unchanged"}
            raise
        self._mt_set.add(key)
        return r

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
        reduce_only: bool = False,
        position_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        市价单。side: BUY / SELL
        client_order_id 必须唯一 —— 币安会对相同 ID 拒绝重复提交（-4116 Duplicate order sent）
        这正是我们要的幂等保护：网络超时重试时，第二次会拿到 -4116，
        此时说明第一次其实成功了，直接查订单状态返回即可。
        """
        q = self.round_qty(symbol, quantity)
        if q <= 0:
            raise BinanceError(
                -1, f"{symbol} 下单数量 {quantity} 取整后为 0（低于该交易对最小下单量）")

        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": q,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if position_side:
            params["positionSide"] = position_side.upper()

        # 按账户持仓模式调整参数：
        #   双向(hedge)：必须带 positionSide，且**不能**用 reduceOnly
        #   单向(one-way)：不能带 positionSide，平仓用 reduceOnly
        # 搞反了会 -4061 / -1106，单子直接下不出去
        hedge = self.get_position_mode()
        if hedge:
            params.pop("reduceOnly", None)
            if position_side:
                ps = position_side
            elif reduce_only:
                ps = "LONG" if side.upper() == "SELL" else "SHORT"
            else:
                ps = "LONG" if side.upper() == "BUY" else "SHORT"
            params["positionSide"] = ps.upper()
        else:
            params.pop("positionSide", None)

        try:
            r = self._request("POST", "/fapi/v1/order", params)
            self._invalidate()          # 持仓变了，余额/持仓缓存立即失效
            return r
        except BinanceError as e:
            # 账户在运行中被切换了持仓模式 → 参数对不上，翻一下模式重试一次
            if e.code == -4061:
                self._dual_side = not hedge
                if self._dual_side:
                    params.pop("reduceOnly", None)
                    ps = position_side or ("LONG" if side.upper() == "BUY" else "SHORT")
                    params["positionSide"] = ps.upper()
                else:
                    params.pop("positionSide", None)
                    if reduce_only:
                        params["reduceOnly"] = "true"
                log.warning("持仓模式判断有误(-4061)，按%s模式重试 %s",
                            "双向" if self._dual_side else "单向", client_order_id)
                r = self._request("POST", "/fapi/v1/order", params)
                self._invalidate()
                return r
            if e.code == -4116:
                log.warning("订单 %s 已存在（幂等命中），查询实际状态", client_order_id)
                try:
                    return self.get_order(symbol, client_order_id)
                except Exception as e2:
                    log.error("查询已有订单失败: %s", e2)
            raise

    def get_order(self, symbol: str, client_order_id: str) -> Dict[str, Any]:
        """按 clientOrderId 查订单，用于幂等重试后确认实际状态"""
        return self._request("GET", "/fapi/v1/order", {
            "symbol": symbol.upper(),
            "origClientOrderId": client_order_id,
        })

    def _place_protection(
        self,
        kind: str,
        symbol: str,
        position_side: str,
        stop_price: float,
        client_order_id: str,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """挂保护单（STOP_MARKET / TAKE_PROFIT_MARKET + closePosition）

        关键点：positionSide 只在「双向持仓」模式下传。
        单向模式下传了会被币安拒（-4061），导致止盈止损一个都挂不上 → 裸仓。
        """
        hedge = self.get_position_mode()
        close_side = "SELL" if position_side.upper() == "LONG" else "BUY"
        # 取整方向：SELL 单向下、BUY 单向上。
        # 对止损 = 远离入场价（不会被一个 tick 提前扫掉）；
        # 对止盈 = 靠近入场价（更容易成交）。两个都符合预期。
        px = self.round_price(symbol, stop_price,
                              mode="down" if close_side == "SELL" else "up")
        params = {
            "symbol": symbol.upper(),
            "side": close_side,
            "type": "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET",
            "stopPrice": px,
            "closePosition": "true",
            "workingType": working_type,
            "newClientOrderId": client_order_id,
        }
        if hedge:
            params["positionSide"] = position_side.upper()

        try:
            r = self._request("POST", "/fapi/v1/order", params)
            self._invalidate()
            return r
        except BinanceError as e:
            # 模式判断错了：币安说方向不匹配 → 翻一下模式重试一次
            if e.code == -4061:
                self._dual_side = not hedge
                if self._dual_side:
                    params["positionSide"] = position_side.upper()
                else:
                    params.pop("positionSide", None)
                log.warning("%s %s positionSide 被拒(-4061)，按%s模式重试",
                            symbol, kind, "双向" if self._dual_side else "单向")
                r = self._request("POST", "/fapi/v1/order", params)
                self._invalidate()
                return r
            if e.code == -4116:
                log.warning("%s单 %s 已存在，查询状态", kind, client_order_id)
                try:
                    return self.get_order(symbol, client_order_id)
                except Exception:
                    pass
            raise

    def stop_loss(
        self,
        symbol: str,
        position_side: str,
        stop_price: float,
        client_order_id: str,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """止损单：STOP_MARKET + closePosition=true

        position_side: 当前持仓方向 LONG / SHORT（平仓方向自动反向）
        """
        return self._place_protection("SL", symbol, position_side, stop_price,
                                      client_order_id, working_type)

    def take_profit(
        self,
        symbol: str,
        position_side: str,
        stop_price: float,
        client_order_id: str,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """止盈单：TAKE_PROFIT_MARKET + closePosition=true"""
        return self._place_protection("TP", symbol, position_side, stop_price,
                                      client_order_id, working_type)

    def cancel_all(self, symbol: str) -> Any:
        # 币安这个接口是 DELETE，不是 POST
        r = self._request("DELETE", "/fapi/v1/allOpenOrders",
                          {"symbol": symbol.upper()})
        self._invalidate()
        return r

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params)

    def get_mark_price(self, symbol: str) -> float:
        """标记价，带 3 秒缓存：同一币短时间反复查（开仓+挂单校验）不重复打 API"""
        sym = symbol.upper()
        now = time.time()
        hit = self._price_cache.get(sym)
        if hit and now - hit[1] < self._price_ttl:
            return hit[0]
        data = self._request("GET", "/fapi/v1/premiumIndex",
                             {"symbol": sym}, signed=False)
        px = float(data["markPrice"])
        self._price_cache[sym] = (px, now)
        return px

    def ping(self) -> bool:
        if self.offline:
            return True
        if self._banned_until > time.time():
            return False
        try:
            r = self.session.get(f"{self.base}/fapi/v1/ping", timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False
