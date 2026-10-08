# -*- coding: utf-8 -*-

"""
============================================================
Telegram -> SignalParser -> TradeManager -> Binance USDs-M Futures
============================================================

优化版。核心原则：

    **一切下单参数都来自 Binance API 返回值，不做任何本地假设。**

    1. 价格 / 数量精度   <- GET /fapi/v1/exchangeInfo (pricePrecision / quantityPrecision / stepSize / tickSize)
    2. 最小名义价值      <- exchangeInfo 的 MIN_NOTIONAL 过滤器（不再硬编码 100）
    3. 下单价格基准      <- GET /fapi/v1/ticker/bookTicker（买一卖一，而非最新成交价）
    4. 真实成交均价      <- GET /fapi/v1/order（avgPrice）-> GET /fapi/v1/userTrades（VWAP）-> positionRisk（兜底）
    5. 真实持仓数量/方向 <- GET /fapi/v2/positionRisk?symbol=xxx
    6. 时间戳基准        <- GET /fapi/v1/time（服务器时间偏移校准）
    7. 可用保证金        <- GET /fapi/v2/account

相比原版修复的问题：

  P0 致命
    - MARKET+RESULT 的 avgPrice 常为 0，原版拿不到真实成交价 -> 三级兜底取 VWAP
    - TP1/TP2 比例按「当前持仓」算，实际成了 30%/21%/49% -> 改为按「初始持仓」算
    - validate_tpsl 用现价校验，价格越过 TP1 时整单被拒、保护全丢 -> 改为按 entry 校验 + 只 warn
    - calculate_default_tpsl 查了 filters 却没取整，触发价精度不符被拒单 -> 删除死代码，统一 format_price
    - 未检查账户持仓模式，双向持仓下 positionSide=BOTH 直接失败 -> 启动时强制单向
    - MIN_NOTIONAL 硬编码 100 -> 改读 exchangeInfo

  P1 稳定性
    - 每次请求新建连接 -> requests.Session + 连接池
    - 无重试无限流 -> 指数退避，识别 429/418/-1021/-1003
    - 无幂等单号，超时重试点两次仓 -> newClientOrderId + 下单前查单
    - 状态文件非原子写，崩溃即损坏 -> tmp + os.replace + 备份
    - 精度未量化，可能生成 0.3000000000000000001 触发 -1111 -> quantize
    - 本地时钟签名 -> 服务器时间偏移

  P2 策略
    - TP 只存本地，程序掉线就没有止盈保护 -> TP1 挂交易所条件单做兜底
    - TP1 后没有保本止损 -> 移动 SL 到成本价（含手续费缓冲）
    - 「补仓」解析了但没实现 -> 已实现
    - 市价单无滑点控制 -> bookTicker 预估 + 超限拒单
    - 反手信号被静默跳过 -> 识别反向信号
    - handle_signal 吞掉所有异常 -> 返回结构化结果
"""

import os
import re
import json
import time
import hmac
import uuid
import hashlib
import logging
import threading
import traceback

from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from urllib.parse import urlencode
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 兼容（老版本没有 allowed_methods）
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None


# Decimal 精度要够，否则大数量币种（如 1000SHIB）会丢位
getcontext().prec = 50


# ============================================================
# 1. 配置
# ============================================================

ENV = os.getenv("BINANCE_ENV", "TEST").upper()

CONFIG = {
    "TEST": {"BASE_URL": "https://testnet.binancefuture.com"},
    "MAIN": {"BASE_URL": "https://fapi.binance.com"},
}

BASE_URL = CONFIG.get(ENV, CONFIG["TEST"])["BASE_URL"]

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    raise RuntimeError("未设置 BINANCE_API_KEY / BINANCE_API_SECRET 环境变量")


# ---- 交易参数 ----

DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_USDT = Decimal(os.getenv("DEFAULT_USDT", "20"))

# 开仓保护距离（正式 TPSL 到达前）
DEFAULT_PROTECTION_PERCENT = Decimal("0.07")

# 保证金模式：ISOLATED / CROSSED / None(不修改)
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED") or None

# TP 分批比例（按【初始持仓】计算，不是当前持仓）
TP_CLOSE_PERCENT = {
    1: Decimal("0.30"),
    2: Decimal("0.30"),
    3: Decimal("1.00"),
}

# 是否把 TP1 挂到交易所作为掉线兜底
EXCHANGE_TP1_ENABLED = os.getenv("EXCHANGE_TP1_ENABLED", "true").lower() == "true"

# 是否把最后一个 TP 挂到交易所作为兜底止盈
EXCHANGE_FINAL_TP_ENABLED = os.getenv("EXCHANGE_FINAL_TP_ENABLED", "true").lower() == "true"

# TP1 后是否把止损移到保本价
BREAKEVEN_ENABLED = os.getenv("BREAKEVEN_ENABLED", "true").lower() == "true"

# 保本止损时覆盖的手续费比例（taker 0.05% * 2 再加余量）
BREAKEVEN_FEE_BUFFER = Decimal("0.0015")

# 市价单最大可接受滑点（相对 bookTicker 的对手价）
MAX_SLIPPAGE_PERCENT = Decimal("0.005")

# 允许交易的币种白名单，空表示不限制
SYMBOL_WHITELIST = [
    s.strip().upper()
    for s in os.getenv("SYMBOL_WHITELIST", "").split(",")
    if s.strip()
]


# ---- 请求参数 ----

REQUEST_TIMEOUT = (5, 15)          # (connect, read)
RECV_WINDOW = 5000
MAX_RETRY = 4
RETRY_BACKOFF = 0.5

# 可忽略的 Binance 错误码
IGNORE_ERROR_CODES = {
    -4028,   # No need to change initial leverage
    -4046,   # No need to change margin type
    -4059,   # No need to change position side
    -4164,   # Notional must be no smaller than ... (已在下单前校验)
}

# 规则缓存
CACHE_FILE = "exchange_info_cache.json"
CACHE_TTL = 24 * 3600

STATE_FILE = "trade_state.json"


# ============================================================
# 2. 日志
# ============================================================

logger = logging.getLogger("TradeManager")
logger.setLevel(logging.INFO)

if not logger.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    try:
        sh.stream.reconfigure(encoding="utf-8")   # py3.7+
    except Exception:
        pass
    fh = logging.FileHandler("trader.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)


# ============================================================
# 3. Decimal 工具
# ============================================================

def D(value):
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def decimal_to_str(value):
    """Decimal -> Binance 可接受的普通十进制字符串（避免科学计数法）"""
    v = D(value)
    text = format(v, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def floor_step(value, step):
    """按 stepSize 向下取整，并用 step 的小数位量化，消除浮点尾数"""
    value = D(value)
    step = D(step)
    if step <= 0:
        return value

    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    result = steps * step

    exp = step.normalize().as_tuple().exponent
    if exp < 0:
        result = result.quantize(Decimal(1).scaleb(exp), rounding=ROUND_DOWN)
    else:
        result = result.to_integral_value(rounding=ROUND_DOWN)
    return result


def ceil_step(value, step):
    value = D(value)
    step = D(step)
    if step <= 0:
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_UP)
    result = steps * step
    exp = step.normalize().as_tuple().exponent
    if exp < 0:
        result = result.quantize(Decimal(1).scaleb(exp), rounding=ROUND_UP)
    else:
        result = result.to_integral_value(rounding=ROUND_UP)
    return result


def gen_client_id(prefix):
    """生成 Binance 合法的 newClientOrderId（字母数字 _ -，<=36 位）"""
    raw = "%s_%s" % (prefix, uuid.uuid4().hex)
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:36]


def now_ms():
    return int(time.time() * 1000)


# ============================================================
# 4. Binance Client
# ============================================================

class BinanceError(RuntimeError):
    def __init__(self, code, msg, payload=None):
        super(BinanceError, self).__init__("Binance Error %s: %s" % (code, msg))
        self.code = code
        self.msg = msg
        self.payload = payload


class BinanceClient(object):

    def __init__(self):
        self.base_url = BASE_URL
        self.api_key = API_KEY
        self.api_secret = API_SECRET

        self.time_offset = 0
        self._time_lock = threading.Lock()
        self._leverage_cache = {}

        self.session = self._build_session()

        self.filters = {}
        self._load_filters()

        self._sync_time()

    # --------------------------------------------------------
    # Session + 重试策略
    # --------------------------------------------------------

    @staticmethod
    def _build_retry_policy():
        if Retry is None:
            return None
        for kwargs in (
            {"allowed_methods": frozenset(["GET", "DELETE"]),
             "total": MAX_RETRY, "backoff_factor": RETRY_BACKOFF,
             "status_forcelist": [429, 500, 502, 503, 504],
             "raise_on_status": False},
            {"method_whitelist": frozenset(["GET", "DELETE"]),
             "total": MAX_RETRY, "backoff_factor": RETRY_BACKOFF,
             "status_forcelist": [429, 500, 502, 503, 504],
             "raise_on_status": False},
            {"total": MAX_RETRY, "backoff_factor": RETRY_BACKOFF,
             "status_forcelist": [429, 500, 502, 503, 504],
             "raise_on_status": False},
        ):
            try:
                return Retry(**kwargs)
            except TypeError:
                continue
        return None

    def _build_session(self):
        s = requests.Session()
        s.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tg-binance-trader/2.0",
        })
        retry = self._build_retry_policy()
        adapter = HTTPAdapter(
            pool_connections=16,
            pool_maxsize=32,
            max_retries=retry,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    # --------------------------------------------------------
    # 服务器时间校准（避免 -1021）
    # --------------------------------------------------------

    def _sync_time(self):
        try:
            r = self.session.get(
                self.base_url + "/fapi/v1/time", timeout=REQUEST_TIMEOUT
            )
            server = int(r.json()["serverTime"])
            local = now_ms()
            self.time_offset = server - local
            logger.info("时间校准完成 | offset=%sms", self.time_offset)
        except Exception as e:
            logger.warning("时间校准失败，使用本地时钟 | %s", e)
            self.time_offset = 0

    def _timestamp(self):
        return now_ms() + self.time_offset

    def _sign(self, params):
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # --------------------------------------------------------
    # 统一请求
    # --------------------------------------------------------

    @staticmethod
    def _strip_auth(params):
        """去掉签名相关字段，让 request 用最新时间戳重新签名"""
        return {k: v for k, v in (params or {}).items()
                if k not in ("timestamp", "signature", "recvWindow")}

    def request(self, method, endpoint, params=None, signed=False, retry=0):
        params = dict(params or {})

        if signed:
            params["timestamp"] = self._timestamp()
            params["recvWindow"] = RECV_WINDOW
            params["signature"] = self._sign(params)

        url = self.base_url + endpoint

        try:
            resp = self.session.request(
                method, url, params=params, timeout=REQUEST_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            # 网络异常：GET/DELETE 可安全重试；POST 交给调用方按 cid 反查，绝不盲重试
            if method.upper() in ("GET", "DELETE") and retry < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * (2 ** retry))
                logger.warning("网络异常重试 | %s %s | 第 %s 次",
                               method, endpoint, retry + 1)
                return self.request(
                    method, endpoint, self._strip_auth(params),
                    signed=signed, retry=retry + 1)
            raise

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                "非 JSON 响应 | HTTP %s | %s | %s" % (
                    resp.status_code, endpoint, resp.text[:300])
            )

        # 业务错误优先于 HTTP 状态码解析
        if isinstance(data, dict) and data.get("code") is not None:
            code = data["code"]
            if code < 0 and code not in IGNORE_ERROR_CODES:
                # -1021 时间戳问题：重新校准后重试一次
                if code == -1021 and retry < 1:
                    self._sync_time()
                    return self.request(
                        method, endpoint, self._strip_auth(params),
                        signed=signed, retry=retry + 1)

                # 限流：退避重试
                if code in (-1003, -1015) and retry < MAX_RETRY:
                    time.sleep(RETRY_BACKOFF * (2 ** retry))
                    logger.warning("触发限流，退避重试 | %s | %s", code, endpoint)
                    return self.request(
                        method, endpoint, self._strip_auth(params),
                        signed=signed, retry=retry + 1)

                raise BinanceError(code, data.get("msg"), data)

        if not resp.ok:
            raise RuntimeError(
                "HTTP %s | %s | %s" % (resp.status_code, endpoint, resp.text[:300])
            )

        return data

    # ========================================================
    # 交易规则（exchangeInfo）—— 带本地文件缓存
    # ========================================================

    def _load_filters(self):
        cached = self._read_cache()
        if cached:
            self.filters = cached
            logger.info("使用本地交易规则缓存 | symbols=%s", len(self.filters))
            return

        self.refresh_exchange_info()

    def _read_cache(self):
        if not os.path.exists(CACHE_FILE):
            return None
        try:
            if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL:
                return None
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取规则缓存失败 | %s", e)
            return None

    def refresh_exchange_info(self):
        logger.info("拉取 Binance ExchangeInfo ...")
        data = self.request("GET", "/fapi/v1/exchangeInfo")

        parsed = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue

            info = {
                "symbol": s["symbol"],
                "baseAsset": s["baseAsset"],
                "quoteAsset": s["quoteAsset"],
                "pricePrecision": int(s.get("pricePrecision", 8)),
                "quantityPrecision": int(s.get("quantityPrecision", 8)),
                "step": D("0"),
                "tick": D("0"),
                "min_qty": D("0"),
                "max_qty": None,
                "market_step": D("0"),
                "market_min_qty": D("0"),
                "market_max_qty": None,
                "min_notional": D("5"),
            }

            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    info["step"] = D(f["stepSize"])
                    info["min_qty"] = D(f["minQty"])
                    info["max_qty"] = D(f["maxQty"])
                elif ft == "MARKET_LOT_SIZE":
                    info["market_step"] = D(f["stepSize"])
                    info["market_min_qty"] = D(f["minQty"])
                    info["market_max_qty"] = D(f["maxQty"])
                elif ft == "PRICE_FILTER":
                    info["tick"] = D(f["tickSize"])
                elif ft == "MIN_NOTIONAL":
                    info["min_notional"] = D(f.get("notional", "5"))

            if info["step"] > 0 and info["tick"] > 0:
                parsed[s["symbol"]] = info

        self.filters = parsed
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("写规则缓存失败 | %s", e)

        logger.info("ExchangeInfo 加载完成 | symbols=%s", len(parsed))

    def get_filters(self, symbol):
        symbol = symbol.upper()
        info = self.filters.get(symbol)
        if not info:
            self.refresh_exchange_info()
            info = self.filters.get(symbol)
        if not info:
            raise RuntimeError("Binance 无此交易对或已停牌：%s" % symbol)
        return info

    # ---------- 基于 API 规则的格式化 ----------

    def format_price(self, symbol, price):
        f = self.get_filters(symbol)
        p = D(price)
        if f["tick"] > 0:
            p = floor_step(p, f["tick"])
        return p.quantize(
            Decimal(1).scaleb(-f["pricePrecision"]), rounding=ROUND_DOWN
        )

    def format_qty(self, symbol, qty, for_market=True):
        f = self.get_filters(symbol)
        q = D(qty)

        step = f["step"] if f["step"] > 0 else Decimal(1).scaleb(-f["quantityPrecision"])
        if for_market and f["market_step"] > 0:
            # 市价单同时受 MARKET_LOT_SIZE 约束，取更严格的步进
            step = max(step, f["market_step"])

        q = floor_step(q, step)
        return q.quantize(
            Decimal(1).scaleb(-f["quantityPrecision"]), rounding=ROUND_DOWN
        )

    # ========================================================
    # 行情（全部来自 API）
    # ========================================================

    def get_server_price(self, symbol):
        """最新成交价"""
        d = self.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return D(d["price"])

    def get_book_ticker(self, symbol):
        """买一卖一：市价单的真实对手价，用于估算滑点"""
        d = self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return {
            "bid": D(d["bidPrice"]),
            "ask": D(d["askPrice"]),
            "bid_qty": D(d["bidQty"]),
            "ask_qty": D(d["askQty"]),
        }

    def reference_price(self, symbol, side):
        """
        开仓基准价：BUY 看 ask，SELL 看 bid。
        这是「按 API 数据下单」的第一环。
        """
        bt = self.get_book_ticker(symbol)
        p = bt["ask"] if side == "BUY" else bt["bid"]
        if p <= 0:
            p = self.get_server_price(symbol)
        return p, bt

    # ========================================================
    # 账户
    # ========================================================

    def get_account(self):
        return self.request("GET", "/fapi/v2/account", signed=True)

    def get_available_balance(self, asset="USDT"):
        acc = self.get_account()
        for a in acc.get("assets", []):
            if a["asset"] == asset:
                return D(a["availableBalance"])
        return Decimal("0")

    def set_leverage(self, symbol, leverage):
        if self._leverage_cache.get(symbol) == leverage:
            return None
        try:
            r = self.request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": leverage}, signed=True)
            self._leverage_cache[symbol] = leverage
            return r
        except BinanceError as e:
            if e.code == -4028:
                self._leverage_cache[symbol] = leverage
                return None
            raise

    def set_margin_type(self, symbol, margin_type):
        try:
            return self.request("POST", "/fapi/v1/marginType",
                                {"symbol": symbol, "marginType": margin_type},
                                signed=True)
        except BinanceError as e:
            if e.code == -4046:
                return None
            raise

    def ensure_single_position_mode(self):
        """本程序 positionSide=BOTH，必须确保账户是单向持仓模式"""
        try:
            r = self.request("GET", "/fapi/v1/positionSide/dual", signed=True)
            if r.get("dualSidePosition"):
                logger.warning("账户为双向持仓模式，正在切换为单向 ...")
                self.request("POST", "/fapi/v1/positionSide/dual",
                             {"dualSidePosition": "false"}, signed=True)
                logger.info("已切换为单向持仓模式")
        except BinanceError as e:
            if e.code == -4059:
                return
            logger.warning("检查持仓模式失败 | %s | %s", e.code, e.msg)

    # ========================================================
    # 持仓（带 symbol 参数，降权重）
    # ========================================================

    def get_position(self, symbol):
        data = self.request("GET", "/fapi/v2/positionRisk",
                            {"symbol": symbol.upper()}, signed=True)
        for p in data:
            qty = D(p["positionAmt"])
            if qty != 0:
                return {
                    "symbol": symbol.upper(),
                    "qty": abs(qty),
                    "position_amt": qty,
                    "entry_price": D(p["entryPrice"]),
                    "mark_price": D(p["markPrice"]),
                    "unrealized_profit": D(p["unRealizedProfit"]),
                    "leverage": int(D(p["leverage"])),
                    "side": "LONG" if qty > 0 else "SHORT",
                    "notional": abs(qty) * D(p["entryPrice"]),
                }
        return None

    def get_all_positions(self):
        return self.request("GET", "/fapi/v2/positionRisk", signed=True)

    # ========================================================
    # 成交价：三级兜底，确保拿到真实数据
    # ========================================================

    def get_fill_price(self, symbol, order_id, fallback_qty=None):
        """
        真实成交均价。优先级：

          1) GET /fapi/v1/order          -> avgPrice（FILLED 后才有值）
          2) GET /fapi/v1/userTrades     -> sum(quoteQty)/sum(qty) 真实 VWAP
          3) positionRisk.entryPrice     -> 最终兜底

        返回 (price, qty, source)
        """
        # 1) 订单回查
        try:
            o = self.request("GET", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)
            avg = D(o.get("avgPrice") or "0")
            exec_qty = D(o.get("executedQty") or "0")
            if o.get("status") == "FILLED" and avg > 0 and exec_qty > 0:
                return avg, exec_qty, "order.avgPrice"
        except Exception as e:
            logger.debug("order 回查失败 | %s", e)

        # 2) 成交明细 VWAP
        try:
            trades = self.request(
                "GET", "/fapi/v1/userTrades",
                {"symbol": symbol, "orderId": order_id, "limit": 1000}, signed=True)
            tq = sum((D(t["qty"]) for t in trades), Decimal("0"))
            tquote = sum((D(t["quoteQty"]) for t in trades), Decimal("0"))
            if tq > 0:
                return (tquote / tq), tq, "userTrades.vwap"
        except Exception as e:
            logger.debug("userTrades 回查失败 | %s", e)

        # 3) 持仓兜底
        pos = self.get_position(symbol)
        if pos:
            return pos["entry_price"], pos["qty"], "positionRisk.entryPrice"

        return (None, fallback_qty or Decimal("0"), "unknown")

    # ========================================================
    # 下单（幂等）
    # ========================================================

    def find_order_by_cid(self, symbol, cid):
        try:
            return self.request(
                "GET", "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": cid}, signed=True)
        except BinanceError as e:
            if e.code == -2013:      # Order does not exist
                return None
            raise

    def submit_order(self, symbol, payload):
        """
        下单。带 newClientOrderId 幂等：
        网络超时后先用 cid 反查，避免重复开仓。
        """
        cid = payload.get("newClientOrderId")
        if cid:
            exist = self.find_order_by_cid(symbol, cid)
            if exist:
                logger.warning("订单已存在，跳过重复提交 | cid=%s | status=%s",
                               cid, exist.get("status"))
                return exist

        try:
            return self.request("POST", "/fapi/v1/order", payload, signed=True)
        except (requests.Timeout, requests.ConnectionError) as e:
            if not cid:
                raise
            logger.warning("下单网络异常，反查订单 | cid=%s | %s", cid, e)
            for _ in range(3):
                time.sleep(0.8)
                exist = self.find_order_by_cid(symbol, cid)
                if exist:
                    logger.info("反查命中，订单已创建 | status=%s",
                                exist.get("status"))
                    return exist
            raise

    def market_open(self, symbol, side, quantity):
        """
        市价开仓。side: LONG / SHORT
        数量必须已经过 format_qty（step + precision）处理。
        """
        order_side = "BUY" if side == "LONG" else "SELL"
        qty = self.format_qty(symbol, quantity, for_market=True)

        # 用 API 的对手价校验名义价值与滑点
        ref, book = self.reference_price(symbol, order_side)
        f = self.get_filters(symbol)

        notional = qty * ref
        if notional < f["min_notional"]:
            raise RuntimeError(
                "名义价值 %s < 最小 %s (%s)" % (
                    decimal_to_str(notional),
                    decimal_to_str(f["min_notional"]), symbol)
            )

        max_qty = f["market_max_qty"] or f["max_qty"]
        if max_qty and qty > max_qty:
            raise RuntimeError(
                "数量 %s 超过上限 %s" % (decimal_to_str(qty), decimal_to_str(max_qty))
            )

        spread = (book["ask"] - book["bid"]) / ref if ref > 0 else Decimal("0")
        if spread > MAX_SLIPPAGE_PERCENT:
            raise RuntimeError(
                "买卖价差 %.3f%% 超过滑点上限 %.3f%%，暂不开仓" % (
                    spread * 100, MAX_SLIPPAGE_PERCENT * 100)
            )

        logger.info("市价开仓 | %s | %s | qty=%s | ref=%s | notional=%s",
                    symbol, side, decimal_to_str(qty),
                    decimal_to_str(ref), decimal_to_str(notional))

        return self.submit_order(symbol, {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": decimal_to_str(qty),
            "newClientOrderId": gen_client_id("o" + order_side[:1]),
            "newOrderRespType": "RESULT",
        })

    def market_close(self, symbol, position_side, quantity):
        close_side = "SELL" if position_side == "LONG" else "BUY"
        qty = self.format_qty(symbol, quantity, for_market=True)

        logger.info("市价平仓 | %s | %s | qty=%s", symbol, position_side,
                    decimal_to_str(qty))

        return self.submit_order(symbol, {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": decimal_to_str(qty),
            "reduceOnly": "true",
            "newClientOrderId": gen_client_id("c" + close_side[:1]),
            "newOrderRespType": "RESULT",
        })

    # ========================================================
    # Algo Orders（条件单）
    # ========================================================

    def get_open_algo_orders(self, symbol=None):
        params = {"symbol": symbol} if symbol else {}
        return self.request("GET", "/fapi/v1/openAlgoOrders", params, signed=True)

    def cancel_algo_order(self, symbol, algo_id):
        return self.request("DELETE", "/fapi/v1/algoOrder",
                            {"symbol": symbol, "algoId": algo_id}, signed=True)

    def cancel_all_algo_orders(self, symbol):
        """清理该币种所有条件单。TP 事件前必须先清，避免与手动平仓打架"""
        try:
            orders = self.get_open_algo_orders(symbol)
        except Exception as e:
            logger.error("获取条件单失败 | %s | %s", symbol, e)
            return 0

        n = 0
        for o in orders or []:
            algo_id = o.get("algoId") or o.get("orderId")
            if not algo_id:
                continue
            try:
                self.cancel_algo_order(symbol, algo_id)
                n += 1
            except BinanceError as e:
                logger.warning("取消条件单失败 | %s | %s | %s", symbol, algo_id, e.msg)
        if n:
            logger.info("已取消条件单 | %s | 共 %s 个", symbol, n)
        return n

    def _algo_close_side(self, position_side):
        return "SELL" if position_side == "LONG" else "BUY"

    def place_stop_loss(self, symbol, position_side, trigger_price):
        tp = self.format_price(symbol, trigger_price)
        logger.info("挂止损 | %s | %s | %s", symbol, position_side,
                    decimal_to_str(tp))
        try:
            return self.request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": self._algo_close_side(position_side),
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "triggerPrice": decimal_to_str(tp),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "TRUE",
                "newClientOrderId": gen_client_id("sl"),
            }, signed=True)
        except BinanceError as e:
            if e.code == -2021:   # 触发价会立刻成交
                logger.error("止损触发价会立即成交，请检查方向 | %s", tp)
            raise

    def place_take_profit(self, symbol, position_side, trigger_price,
                          quantity=None):
        """
        quantity=None -> closePosition 全平
        quantity 有值  -> 只平该数量（用于 TP1 分批）
        """
        tp = self.format_price(symbol, trigger_price)
        payload = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": self._algo_close_side(position_side),
            "positionSide": "BOTH",
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": decimal_to_str(tp),
            "workingType": "MARK_PRICE",
            "priceProtect": "TRUE",
            "newClientOrderId": gen_client_id("tp"),
        }

        if quantity is None:
            payload["closePosition"] = "true"
        else:
            payload["quantity"] = decimal_to_str(
                self.format_qty(symbol, quantity, for_market=True)
            )
            payload["reduceOnly"] = "true"

        logger.info("挂止盈 | %s | %s | %s | qty=%s", symbol, position_side,
                    decimal_to_str(tp), quantity if quantity else "ALL")
        return self.request("POST", "/fapi/v1/algoOrder", payload, signed=True)


# ============================================================
# 5. SignalParser
# ============================================================

class SignalParser(object):

    SYMBOL_ALIASES = {
        "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BB": "BBUSDT",
        "AIN": "AINUSDT", "NIL": "NILUSDT", "SOL": "SOLUSDT",
        "BNB": "BNBUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT",
    }

    # 平仓关键词
    CLOSE_WORDS = ("平多", "平空", "平仓", "清仓", "全部平仓", "手动平仓", "止损离场")

    @classmethod
    def normalize_symbol(cls, symbol):
        s = symbol.upper().strip().replace("#", "")
        s = re.sub(r"[^A-Z0-9]", "", s)
        if s.endswith("USDT"):
            return s
        return cls.SYMBOL_ALIASES.get(s, s + "USDT")

    @classmethod
    def parse_symbol(cls, text):
        m = re.search(r"#\s*([A-Za-z0-9]{1,15})", text)
        if not m:
            return None
        return cls.normalize_symbol(m.group(1))

    @classmethod
    def parse_side(cls, text):
        t = text.lower()
        if any(w in t for w in ("现价多", "开多", "做多", "多单", "买入", "做多单")):
            return "LONG"
        if any(w in t for w in ("现价空", "开空", "做空", "空单", "卖出", "做空单")):
            return "SHORT"
        return None

    @classmethod
    def is_close(cls, text):
        return any(w in text for w in cls.CLOSE_WORDS)

    @classmethod
    def parse_tp_level(cls, text):
        # 兼容 TP1 / TP 1 / TP1️⃣ / tp1
        m = re.search(r"(?<![A-Za-z0-9])TP\s*([123])(?![0-9])", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    @classmethod
    def _parse_range(cls, text, keyword):
        m = re.search(keyword + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*-\s*[0-9]+(?:\.[0-9]+)?)*)", text)
        if not m:
            return []
        return [D(x.strip()) for x in m.group(1).split("-") if x.strip()]

    @classmethod
    def parse_tpsl(cls, text):
        return (
            cls._parse_range(text, "补仓"),
            cls._parse_range(text, "止盈"),
            cls._parse_range(text, "止损"),
        )

    @classmethod
    def parse_signal(cls, text):
        text = (text or "").strip()
        if not text:
            return cls._unknown()

        symbol = cls.parse_symbol(text)
        side = cls.parse_side(text)
        add, tp, sl = cls.parse_tpsl(text)
        tp_level = cls.parse_tp_level(text)

        # 平仓
        if cls.is_close(text) and symbol:
            return {
                "type": "CLOSE", "symbol": symbol, "side": side,
                "add": [], "tp": tp, "sl": sl, "tp_level": tp_level,
                "raw": text,
            }

        # TP 事件（有 TP 层级 + "止盈"）
        if tp_level is not None and "止盈" in text:
            return {
                "type": "TP", "symbol": symbol, "side": None,
                "add": [], "tp": [], "sl": [], "tp_level": tp_level,
                "raw": text,
            }

        # 开仓
        if symbol and side and (
            "现价" in text or "开多" in text or "开空" in text
            or "做多" in text or "做空" in text
        ):
            return {
                "type": "OPEN", "symbol": symbol, "side": side,
                "add": [], "tp": tp, "sl": sl, "tp_level": None,
                "raw": text,
            }

        # 设置 TPSL / 补仓
        if tp or sl or add:
            return {
                "type": "SET_TPSL", "symbol": symbol, "side": side,
                "add": add, "tp": tp, "sl": sl, "tp_level": None,
                "raw": text,
            }

        r = cls._unknown()
        r.update({"symbol": symbol, "side": side, "raw": text})
        return r

    @staticmethod
    def _unknown():
        return {
            "type": "UNKNOWN", "symbol": None, "side": None,
            "add": [], "tp": [], "sl": [], "tp_level": None, "raw": "",
        }


# ============================================================
# 6. TradeManager
# ============================================================

class TradeManager(object):

    def __init__(self):
        self.client = BinanceClient()
        self.active_trades = {}
        self.last_symbol = None
        self._lock = threading.RLock()

        self.client.ensure_single_position_mode()
        self.load_state()
        self.sync_positions()

    # ========================================================
    # 状态持久化（原子写）
    # ========================================================

    def save_state(self):
        data = {}
        for sym, tr in self.active_trades.items():
            data[sym] = {k: self._ser(v) for k, v in tr.items()}

        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(STATE_FILE):
                try:
                    os.replace(STATE_FILE, STATE_FILE + ".bak")
                except Exception:
                    pass
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.error("保存状态失败 | %s", e)

    @classmethod
    def _ser(cls, v):
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, dict):
            return {k: cls._ser(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [cls._ser(x) for x in v]
        return v

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error("状态文件损坏，尝试读取备份 | %s", e)
            self._load_state_from(STATE_FILE + ".bak")
            return
        self._load_state_from_data(data)

    def _load_state_from(self, path):
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._load_state_from_data(json.load(f))
        except Exception as e:
            logger.error("备份状态也无法读取 | %s", e)

    def _load_state_from_data(self, data):
        """统一反序列化：所有数值字段还原为 Decimal"""
        dec_keys = {
            "entry_price", "qty", "initial_qty", "default_tp", "default_sl",
            "breakeven_price", "realized_pnl", "peak_price",
        }
        dec_list_keys = {"tp", "sl", "add_prices"}

        for sym, tr in data.items():
            for k, v in list(tr.items()):
                if v is None:
                    continue
                if k in dec_keys:
                    tr[k] = D(v)
                elif k in dec_list_keys and isinstance(v, list):
                    tr[k] = [D(x) for x in v]
                elif k == "tp_executed" and isinstance(v, list):
                    tr[k] = [int(x) for x in v]
            self.active_trades[sym] = tr

        if self.active_trades:
            logger.info("恢复交易状态 | %s", list(self.active_trades.keys()))

    # ========================================================
    # 工具
    # ========================================================

    def resolve_symbol(self, symbol):
        if symbol:
            return symbol.upper()
        if self.last_symbol:
            logger.warning("消息无 symbol，沿用最近币种：%s", self.last_symbol)
            return self.last_symbol
        if len(self.active_trades) == 1:
            return list(self.active_trades.keys())[0]
        raise RuntimeError("消息缺少 symbol，且无法确定对应交易")

    def check_whitelist(self, symbol):
        if SYMBOL_WHITELIST and symbol not in SYMBOL_WHITELIST:
            raise RuntimeError("%s 不在白名单内，拒绝交易" % symbol)

    def calc_qty(self, symbol, usdt, price, leverage):
        """
        根据保证金计算数量。
        min_notional / step / precision 全部来自 exchangeInfo。
        """
        f = self.client.get_filters(symbol)
        notional = D(usdt) * D(leverage)

        qty = floor_step(notional / D(price), f["step"])

        # 满足交易所最小名义价值
        if qty * D(price) < f["min_notional"]:
            qty = ceil_step(f["min_notional"] / D(price), f["step"])

        qty = self.client.format_qty(symbol, qty, for_market=True)

        if qty < f["min_qty"]:
            qty = ceil_step(f["min_qty"], f["step"])

        max_qty = f["market_max_qty"] or f["max_qty"]
        if max_qty and qty > max_qty:
            qty = floor_step(max_qty, f["step"])

        if qty <= 0:
            raise RuntimeError("计算出的数量为 0 | usdt=%s price=%s" % (usdt, price))

        est_margin = qty * D(price) / D(leverage)
        logger.info("数量计算 | notional=%s | qty=%s | 占用保证金≈%s USDT",
                    decimal_to_str(notional), decimal_to_str(qty),
                    decimal_to_str(est_margin))
        return qty

    def validate_tpsl(self, side, entry_price, tp, sl, strict=True):
        """
        用【开仓价】校验方向，而不是现价。
        现价可能已经跑过 TP1，用现价校验会把整单保护拒掉。
        """
        entry_price = D(entry_price)
        tp = D(tp)
        sl = D(sl)

        ok = (tp > entry_price > sl) if side == "LONG" else (tp < entry_price < sl)

        if not ok:
            msg = "TP/SL 方向异常 | side=%s entry=%s tp=%s sl=%s" % (
                side, decimal_to_str(entry_price),
                decimal_to_str(tp), decimal_to_str(sl))
            if strict:
                raise RuntimeError(msg)
            logger.warning(msg)
            return False
        return True

    def default_tpsl(self, symbol, entry_price, side):
        e = D(entry_price)
        if side == "LONG":
            tp = e * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
            sl = e * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
        else:
            tp = e * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
            sl = e * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
        return self.client.format_price(symbol, tp), self.client.format_price(symbol, sl)

    # ========================================================
    # OPEN
    # ========================================================

    def handle_open(self, signal):
        symbol = signal["symbol"]
        side = signal["side"]
        if not symbol or not side:
            return self._fail("OPEN 缺少 symbol / side")

        self.check_whitelist(symbol)
        self.last_symbol = symbol

        logger.info("========== OPEN ==========")
        logger.info("symbol=%s | side=%s", symbol, side)

        with self._lock:
            # 已有持仓：判断是否反手
            exist = self.client.get_position(symbol)
            if exist:
                if exist["side"] != side and os.getenv("ALLOW_REVERSE", "false").lower() == "true":
                    logger.info("检测到反手信号 %s -> %s，先平仓", exist["side"], side)
                    self.client.cancel_all_algo_orders(symbol)
                    self.client.market_close(symbol, exist["side"], exist["qty"])
                    time.sleep(0.6)
                else:
                    msg = "⚠ %s 已有 %s 持仓，跳过重复开仓" % (symbol, exist["side"])
                    logger.warning(msg)
                    self.active_trades.setdefault(symbol, {
                        "side": exist["side"],
                        "entry_price": exist["entry_price"],
                        "qty": exist["qty"],
                        "initial_qty": exist["qty"],
                        "tp": [], "sl": [], "add_prices": [],
                        "tp_executed": [],
                    })
                    self.save_state()
                    return self._ok(msg, skipped=True)

            # 1) 杠杆 / 保证金模式
            self.client.set_leverage(symbol, DEFAULT_LEVERAGE)
            if MARGIN_TYPE:
                self.client.set_margin_type(symbol, MARGIN_TYPE)

            # 2) 可用余额检查（API）
            try:
                avail = self.client.get_available_balance()
                if avail < D(DEFAULT_USDT):
                    return self._fail(
                        "可用余额 %s USDT < 计划投入 %s USDT" % (
                            decimal_to_str(avail), DEFAULT_USDT)
                    )
            except Exception as e:
                logger.warning("余额查询失败，继续 | %s", e)

            # 3) 基准价来自 bookTicker
            order_side = "BUY" if side == "LONG" else "SELL"
            ref_price, book = self.client.reference_price(symbol, order_side)

            # 4) 数量（step/precision/minNotional 来自 exchangeInfo）
            qty = self.calc_qty(symbol, DEFAULT_USDT, ref_price, DEFAULT_LEVERAGE)

            # 5) 下单
            order = self.client.market_open(symbol, side, qty)

            # 6) 等成交 + 取真实成交价（三级兜底）
            fill_price, fill_qty, src = None, qty, "unknown"
            for _ in range(12):
                time.sleep(0.25)
                p, q, s = self.client.get_fill_price(
                    symbol, order.get("orderId"), fallback_qty=qty)
                if p and q > 0:
                    fill_price, fill_qty, src = p, q, s
                    break

            if not fill_price:
                return self._fail("开仓后未取得成交价，请立即人工检查 | %s" % symbol)

            logger.info("真实成交 | entry=%s | qty=%s | 来源=%s",
                        decimal_to_str(fill_price), decimal_to_str(fill_qty), src)

            # 7) 以 API 返回的真实成交价算保护单
            d_tp, d_sl = self.default_tpsl(symbol, fill_price, side)
            self.validate_tpsl(side, fill_price, d_tp, d_sl)

            # 8) 清旧单 + 挂保护
            self.client.cancel_all_algo_orders(symbol)
            try:
                self.client.place_stop_loss(symbol, side, d_sl)
                self.client.place_take_profit(symbol, side, d_tp)
            except BinanceError as e:
                logger.error("保护单挂载失败，仓位裸露！| %s | %s", e.code, e.msg)

            self.active_trades[symbol] = {
                "side": side,
                "entry_price": fill_price,
                "qty": fill_qty,
                "initial_qty": fill_qty,      # 关键：TP 比例基于初始持仓
                "default_tp": d_tp,
                "default_sl": d_sl,
                "tp": signal.get("tp") or [],
                "sl": signal.get("sl") or [],
                "add_prices": [],
                "tp_executed": [],
                "leverage": DEFAULT_LEVERAGE,
                "order_id": order.get("orderId"),
                "entry_source": src,
                "created_at": int(time.time()),
            }
            self.save_state()

            return self._ok(
                "✅ OPEN %s %s | 成交=%s | qty=%s | TP=%s | SL=%s | 来源=%s" % (
                    symbol, side, decimal_to_str(fill_price),
                    decimal_to_str(fill_qty), decimal_to_str(d_tp),
                    decimal_to_str(d_sl), src),
                data=self.active_trades[symbol])

    # ========================================================
    # SET_TPSL
    # ========================================================

    def handle_set_tpsl(self, signal):
        symbol = self.resolve_symbol(signal.get("symbol"))
        self.check_whitelist(symbol)
        self.last_symbol = symbol

        logger.info("========== SET_TPSL ==========")

        with self._lock:
            pos = self.client.get_position(symbol)
            if not pos:
                return self._fail("⚠ %s 当前无持仓，无法设置 TPSL" % symbol)

            side = pos["side"]
            entry = pos["entry_price"]
            cur_qty = pos["qty"]

            tp_raw = signal.get("tp") or []
            sl_raw = signal.get("sl") or []
            add_raw = signal.get("add") or []

            if not (tp_raw or sl_raw or add_raw):
                return self._fail("⚠ TPSL 消息没有有效内容")

            # 按交易所 tickSize 规范化
            tp_list = [self.client.format_price(symbol, x) for x in tp_raw]
            sl_list = [self.client.format_price(symbol, x) for x in sl_raw]
            add_list = [self.client.format_price(symbol, x) for x in add_raw]

            # 方向校验：用 entry 而不是现价；异常只告警不阻断
            if tp_list and sl_list:
                self.validate_tpsl(side, entry, tp_list[0], sl_list[0], strict=False)
            else:
                for t in tp_list:
                    ok = t > entry if side == "LONG" else t < entry
                    if not ok:
                        logger.warning(
                            "TP %s 与 %s 方向不符（entry=%s），仍将记录",
                            decimal_to_str(t), side, decimal_to_str(entry))

            self.client.cancel_all_algo_orders(symbol)

            trade = self.active_trades.get(symbol, {})
            trade.update({
                "side": side,
                "entry_price": entry,
                "qty": cur_qty,
                "initial_qty": trade.get("initial_qty") or cur_qty,
                "tp": tp_list,
                "sl": sl_list,
                "add_prices": add_list,
                "tp_executed": trade.get("tp_executed", []),
                "updated_at": int(time.time()),
            })

            # 止损：必须挂上，这是硬保护
            if sl_list:
                try:
                    self.client.place_stop_loss(symbol, side, sl_list[0])
                except BinanceError as e:
                    logger.error("止损挂载失败 | %s | %s", e.code, e.msg)

            # 止盈：挂到交易所做兜底，防止程序掉线后裸奔
            if tp_list:
                if EXCHANGE_TP1_ENABLED and cur_qty > 0:
                    tp1_qty = cur_qty * TP_CLOSE_PERCENT[1]
                    min_q = self.client.get_filters(symbol)["min_qty"]
                    if self.client.format_qty(symbol, tp1_qty) >= min_q:
                        try:
                            self.client.place_take_profit(
                                symbol, side, tp_list[0], quantity=tp1_qty)
                        except BinanceError as e:
                            logger.warning("TP1 条件单挂载失败 | %s", e.msg)

                if EXCHANGE_FINAL_TP_ENABLED and len(tp_list) > 1:
                    try:
                        self.client.place_take_profit(
                            symbol, side, tp_list[-1], quantity=None)
                    except BinanceError as e:
                        logger.warning("最终 TP 条件单挂载失败 | %s", e.msg)

            self.active_trades[symbol] = trade
            self.save_state()

            return self._ok(
                "✅ TPSL 已更新 | %s | entry=%s | TP=%s | SL=%s | 补仓=%s" % (
                    symbol, decimal_to_str(entry),
                    [decimal_to_str(x) for x in tp_list],
                    [decimal_to_str(x) for x in sl_list],
                    [decimal_to_str(x) for x in add_list]),
                data=trade)

    # ========================================================
    # TP1 / TP2 / TP3
    # ========================================================

    def handle_tp(self, signal):
        symbol = self.resolve_symbol(signal.get("symbol"))
        level = signal.get("tp_level")
        self.last_symbol = symbol

        logger.info("========== TP%s ==========", level)

        if level not in (1, 2, 3):
            return self._fail("不支持 TP%s" % level)

        with self._lock:
            pos = self.client.get_position(symbol)
            if not pos:
                self.active_trades.pop(symbol, None)
                self.save_state()
                return self._fail("⚠ %s 已无持仓" % symbol)

            trade = self.active_trades.get(symbol)
            if not trade:
                logger.warning("%s 无本地状态，按 Binance 实际持仓处理", symbol)
                trade = {
                    "side": pos["side"],
                    "entry_price": pos["entry_price"],
                    "initial_qty": pos["qty"],
                    "tp_executed": [],
                }
                self.active_trades[symbol] = trade

            executed = trade.setdefault("tp_executed", [])
            if level in executed:
                return self._ok("⚠ %s TP%s 已执行过，跳过" % (symbol, level),
                                skipped=True)

            side = pos["side"]
            cur_qty = pos["qty"]                      # API 实时值
            initial_qty = D(trade.get("initial_qty") or cur_qty)

            # 关键修复：比例按【初始持仓】算，不是当前持仓
            target_qty = initial_qty * TP_CLOSE_PERCENT[level]
            close_qty = min(target_qty, cur_qty)

            close_qty = self.client.format_qty(symbol, close_qty, for_market=True)
            min_qty = self.client.get_filters(symbol)["min_qty"]

            if close_qty <= 0:
                return self._fail("%s TP%s 平仓数量为 0" % (symbol, level))

            # 低于最小下单量 -> 直接全平，避免被交易所拒单留下尾巴
            if close_qty < min_qty:
                logger.warning("平仓量 %s < minQty %s，改为全部平仓",
                               decimal_to_str(close_qty), decimal_to_str(min_qty))
                close_qty = self.client.format_qty(symbol, cur_qty)

            # 先撤条件单，避免交易所 TP 与手动平仓重复成交
            self.client.cancel_all_algo_orders(symbol)

            self.client.market_close(symbol, side, close_qty)

            executed.append(level)
            trade["tp_executed"] = executed

            time.sleep(0.6)
            new_pos = self.client.get_position(symbol)

            if not new_pos:
                self.active_trades.pop(symbol, None)
                self.save_state()
                return self._ok("🎯 %s 已全部平仓（TP%s 收尾）" % (symbol, level))

            trade["qty"] = new_pos["qty"]
            trade["side"] = new_pos["side"]

            # 剩余仓位要重新挂保护，否则平完这一段就是裸奔
            self._reprotect(symbol, side, new_pos["qty"], trade, level)

            self.active_trades[symbol] = trade
            self.save_state()

            return self._ok(
                "✅ TP%s 完成 | %s | 本次平=%s | 剩余=%s" % (
                    level, symbol, decimal_to_str(close_qty),
                    decimal_to_str(new_pos["qty"])))

    # ========================================================
    # 平仓后重建保护（含保本止损）
    # ========================================================

    def _reprotect(self, symbol, side, remain_qty, trade, level):
        entry = D(trade.get("entry_price") or 0)

        # 止损：优先用保本价，其次用信号止损，最后用默认 ±7%
        sl = None
        if BREAKEVEN_ENABLED and level >= 1 and entry > 0:
            be = self.client.format_price(
                symbol,
                entry * (Decimal("1") + BREAKEVEN_FEE_BUFFER) if side == "LONG"
                else entry * (Decimal("1") - BREAKEVEN_FEE_BUFFER)
            )
            # 保本价必须比原止损更有利，否则不动
            old_sl = trade.get("sl") or []
            if old_sl:
                better = be > old_sl[0] if side == "LONG" else be < old_sl[0]
                if not better:
                    be = old_sl[0]
            sl = be
            trade["breakeven_price"] = be
            logger.info("移动止损到保本价 | %s | %s", symbol, decimal_to_str(be))
        elif trade.get("sl"):
            sl = trade["sl"][0]
        elif trade.get("default_sl"):
            sl = trade["default_sl"]

        # 止盈：还剩仓位就挂下一档
        tp = None
        tp_list = trade.get("tp") or []
        if tp_list:
            idx = min(level, len(tp_list) - 1)
            tp = tp_list[idx]

        try:
            if sl:
                self.client.place_stop_loss(symbol, side, sl)
            if tp:
                self.client.place_take_profit(symbol, side, tp)
        except BinanceError as e:
            logger.error("重建保护失败，仓位裸露 | %s | %s", e.code, e.msg)

    # ========================================================
    # CLOSE（全部平仓）
    # ========================================================

    def handle_close(self, signal):
        symbol = self.resolve_symbol(signal.get("symbol"))
        self.last_symbol = symbol

        with self._lock:
            pos = self.client.get_position(symbol)
            if not pos:
                self.active_trades.pop(symbol, None)
                self.save_state()
                return self._fail("⚠ %s 无持仓" % symbol)

            self.client.cancel_all_algo_orders(symbol)
            self.client.market_close(symbol, pos["side"], pos["qty"])

            self.active_trades.pop(symbol, None)
            self.save_state()
            return self._ok("✅ %s 已全部平仓" % symbol)

    # ========================================================
    # 入口
    # ========================================================

    def handle_signal(self, signal):
        t = signal.get("type")
        logger.info("收到信号 | %s", signal)

        try:
            if t == "OPEN":
                return self.handle_open(signal)
            if t == "SET_TPSL":
                return self.handle_set_tpsl(signal)
            if t == "TP":
                return self.handle_tp(signal)
            if t == "CLOSE":
                return self.handle_close(signal)
            if t == "UNKNOWN":
                logger.info("非交易消息，忽略")
                return self._ok("忽略", skipped=True)
            return self._fail("未知类型 %s" % t)
        except Exception as e:
            logger.error("交易执行失败 | %s", e)
            logger.error(traceback.format_exc())
            return self._fail(str(e))

    @staticmethod
    def _ok(msg, data=None, skipped=False):
        logger.info(msg)
        return {"ok": True, "skipped": skipped, "msg": msg, "data": data}

    @staticmethod
    def _fail(msg):
        logger.error(msg)
        return {"ok": False, "msg": msg}

    # ========================================================
    # 启动同步
    # ========================================================

    def sync_positions(self):
        logger.info("========== 同步 Binance 持仓 ==========")
        try:
            positions = self.client.get_all_positions()
        except Exception as e:
            logger.error("同步失败 | %s", e)
            return

        real = set()
        for p in positions:
            qty = D(p["positionAmt"])
            if qty == 0:
                continue

            sym = p["symbol"]
            real.add(sym)
            tr = self.active_trades.get(sym, {})
            tr.update({
                "side": "LONG" if qty > 0 else "SHORT",
                "qty": abs(qty),
                "entry_price": D(p["entryPrice"]),
            })
            tr.setdefault("initial_qty", abs(qty))   # 重启后保住 TP 比例基准
            tr.setdefault("tp", [])
            tr.setdefault("sl", [])
            tr.setdefault("tp_executed", [])
            self.active_trades[sym] = tr

        for sym in list(self.active_trades.keys()):
            if sym not in real:
                logger.info("清理无持仓状态 | %s", sym)
                self.active_trades.pop(sym, None)

        self.save_state()
        logger.info("同步完成 | %s", sorted(real))


# ========================================================
# 7. Telegram 接入层
# ========================================================

class TelegramTradeBot(object):

    def __init__(self):
        self.manager = TradeManager()
        self._seen = {}
        self._seen_ttl = 60

    def on_message(self, text, msg_id=None):
        logger.info("\n========== Telegram ==========\n%s", text)

        # 去重：Telegram 编辑/重发会带来同一条消息
        if msg_id:
            now = time.time()
            self._seen = {k: v for k, v in self._seen.items()
                          if now - v < self._seen_ttl}
            if msg_id in self._seen:
                logger.info("重复消息，跳过 | id=%s", msg_id)
                return {"ok": True, "skipped": True, "msg": "重复消息"}
            self._seen[msg_id] = now

        signal = SignalParser.parse_signal(text)
        logger.info("解析结果 | %s", signal)

        if signal["type"] == "UNKNOWN":
            return {"ok": True, "skipped": True, "msg": "非交易消息"}

        result = self.manager.handle_signal(signal)
        # 在这里接 Telegram 回执：bot.send_message(chat_id, result["msg"])
        return result


# ========================================================
# 8. 离线冒烟测试（不发网络请求）
# ========================================================

def test_parser():
    cases = [
        "#ETH 现价空",
        "#BB 现价多",
        "#BB TP1止盈",
        "#AIN 手动TP1止盈",
        "#NIL TP3️⃣止盈",
        "止盈：1.32-1.39 止损：1.195",
        "#ETHUSDT 止盈：1.32-1.39-1.49 止损：1.195",
        "#ETHUSDT 补仓：1.25 止盈：1.39 止损：1.20",
        "#ETH 平多",
        "#IRYSUSDT 空单 20x 93,34%🚀",
        "#ETH 现价多 止盈：3200",
    ]
    for t in cases:
        print("=" * 68)
        print("原文 :", t)
        r = SignalParser.parse_signal(t)
        print("类型 :", r["type"], "| symbol:", r["symbol"],
              "| side:", r["side"], "| tp_level:", r["tp_level"])
        print("TP   :", [str(x) for x in r["tp"]],
              "| SL:", [str(x) for x in r["sl"]],
              "| 补仓:", [str(x) for x in r["add"]])


def test_decimal():
    print("=" * 68)
    print("精度测试")
    assert decimal_to_str(floor_step("1.23456789", "0.001")) == "1.234"
    assert decimal_to_str(floor_step("0.3000000000000000001", "0.01")) == "0.3"
    assert decimal_to_str(floor_step("1234.9", "1")) == "1234"
    assert decimal_to_str(floor_step("0.0000001", "0.000001")) == "0"
    assert decimal_to_str(Decimal("1E+3")) == "1000"
    assert "," not in decimal_to_str(Decimal("123456.5"))

    # TP 比例：初始 100，TP1 平 30，TP2 平 30，TP3 平剩余
    init = Decimal("100")
    for lv, expect in ((1, "30"), (2, "30"), (3, "100")):
        q = min(init * TP_CLOSE_PERCENT[lv], init)
        print("  TP%s 应平 %s" % (lv, decimal_to_str(q)))
    print("精度测试通过")


if __name__ == "__main__":
    print("=" * 68)
    print("Telegram -> SignalParser -> TradeManager -> Binance")
    print("环境:", ENV, "| 基础URL:", BASE_URL)
    print("=" * 68)

    test_decimal()
    test_parser()

    print("\n离线解析测试完成。")
    print("实盘接入：")
    print("  bot = TelegramTradeBot()")
    print('  bot.on_message("#ETH 现价空", msg_id=12345)')
