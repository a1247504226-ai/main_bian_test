# -*- coding: utf-8 -*-
"""
BTC 策略 · 币安实盘下单与仓位调整
==================================

这个脚本只做一件事：**把账户里的真实仓位对齐到策略目标仓位**。
它不产生信号、不发明参数，信号和敞口全部来自 btc_monitor.py（与回测同一套）。


默认状态：关闭
--------------
配置 monitor_config.json 里 `binance.enabled` 默认是 **false**。
只要它是 false，无论加什么命令行参数，都只打印计划、绝不下单。

要真正开启，必须**同时**满足 5 个条件（缺一个都只演练）：

    ① binance.enabled          = true
    ② binance.api_key/secret   已填
    ③ 命令行显式加 --live
    ④ 没有别的进程正在下单（机器级锁）
    ⑤ 账户读取成功、余额足够


用法
----
    python btc_trade.py                演练：算出「该做什么」并打印，不下单
    python btc_trade.py --status       只读：查账户余额 / 持仓 / 目标仓位
    python btc_trade.py --live         真实下单（还需 enabled=true + 填 key）
    python btc_trade.py --live --force 忽略「本日线已执行过」的提示（对账仍会做）
    python btc_trade.py --testnet      强制连测试网（覆盖配置）
    python btc_trade.py --live --testnet  在测试网真实下单（建议先这样练手）

双击 `一键下单.bat` 等价于 `--live`；双击 `查看账户.bat` 等价于 `--status`。


为什么是「对账」而不是「按信号下单」
------------------------------------
市价单可能因为网络中断、余额不足、精度错误只成交一半。如果脚本记一个
「今天下过了」就跳过，账户就会长期偏离策略而没人发现。

所以每次运行都做一次**目标 vs 实际**的核对：

    目标 = 信号方向 × 账户权益 × 敞口
    实际 = 现货 BTC + 合约持仓
    差异超过容忍度（默认 2% 名义）才下单

这样重复运行是安全的（已对齐 → 无需操作），失败一半也能自动补齐。


下单落盘
--------
    trade_log.csv     每一笔真实成交一行（时间/日线/动作/数量/价格/订单号/结果）
    trade_state.json  最近一次运行快照（权益、目标、动作、结果）
    exchange_rules.json  交易所精度规则缓存（24 小时）


安全设计
--------
· 精度不硬编码：下单前从 exchangeInfo 读 stepSize / minQty / minNotional，
  按交易所规则向下取整，避免 -1013 / -4164 这类「精度错误」拒单。
· 现货账户与 U 本位合约账户的 USDT 是**两个独立账户**，开空前自动从现货划到合约、
  买现货前自动从合约划回现货（/sapi/v1/asset/transfer），否则会出现「有钱却下不了单」。
· 现货买入受 USDT 余额约束：账户权益里含合约浮盈，那部分钱不能拿来买现货，
  所以买入量会截断到「实际可用 USDT」并明确提示。
· 空头开仓受可用保证金约束，1× 逐仓会占满保证金 → 自动按 short_size_factor 缩量。
· 每次只执行一轮「平反向腿 → 建目标腿」，最多 3 轮收敛，不会无限下单。
· 任何异常立即停止并落盘，不会带着半个仓位继续操作。
"""

import csv
import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btc_strategy as E                      # 数据 / 指标 / 引擎常量
import btc_monitor as M                       # 信号 / 敞口 / 配置 / 日志（唯一来源）

TRADE_LOG = os.path.join(HERE, "trade_log.csv")
TRADE_STATE = os.path.join(HERE, "trade_state.json")
RULES_CACHE = os.path.join(HERE, "exchange_rules.json")

LOCK_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "BTC_Strategy")
LOCK_PATH = os.path.join(LOCK_DIR, "trade.lock")
LOCK_STALE_SEC = 900          # 锁超过 15 分钟视为陈旧（进程被强杀）

LOG_HEADER = ["时间", "日线日期", "信号", "动作", "市场", "方向",
              "数量", "参考价", "名义USDT", "订单号", "状态", "说明"]


# ==============================================================================
# 0. 工具
# ==============================================================================
def fmt_qty(qty, step):
    """按交易所 stepSize 向下取整，返回不带科学计数法的字符串。"""
    d = Decimal(str(step))
    v = (Decimal(str(qty)) / d).to_integral_value(rounding=ROUND_DOWN) * d
    s = format(v, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def qty_problem(qty, price, rule, cfg_min=20.0):
    """
    这个数量到底能不能下单？返回 (是否有问题, 原因)。

    三个必须同时满足的门槛（缺一个都会被交易所拒单）：
      ① 按 stepSize 向下取整后不能是 0   —— 否则订单里是 quantity=0，直接报错
      ② 不能小于 minQty                 —— -1013
      ③ 名义金额不能小于 minNotional    —— -4164 / -1013

    ⚠ 这里最容易踩的坑：BTCUSDT 合约 stepSize=0.001（≈83 USDT），
    而配置里的 min_notional_usdt 只有 20。如果只按 20 判断，
    「减仓 30 USDT」会被放行 → 取整后变成 0 → 下单失败。
    所以必须先把数量取整、再拿取整后的数量去算名义。
    """
    s = fmt_qty(qty, rule["step"])
    if s in ("", "0", "0.0"):
        return True, "按 stepSize %s 取整后为 0" % rule["step"]
    q = float(s)
    if q < rule["min_qty"] - 1e-12:
        return True, "低于最小下单量 %s" % rule["min_qty"]
    need = max(rule["min_notional"], cfg_min)
    if q * price < need - 1e-9:
        return True, "名义 %.2f USDT 低于最小 %.2f USDT" % (q * price, need)
    return False, ""


class TradeLockBusy(RuntimeError):
    """另一个下单进程正持有机器级锁 —— 绝不能并发下单（会变成双倍仓位）。"""

    def __init__(self, owner):
        super().__init__("另一个下单进程正在运行（%s）" % owner)
        self.owner = owner


def claim_lock():
    """原子抢锁，防止「定时任务 + 手动双击」并发下双倍单。"""
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
    except Exception:
        pass
    try:
        if os.path.exists(LOCK_PATH):
            age = time.time() - os.path.getmtime(LOCK_PATH)
            if age > LOCK_STALE_SEC:
                try:
                    os.remove(LOCK_PATH)
                except Exception:
                    pass
            else:
                with open(LOCK_PATH, encoding="utf-8") as f:
                    owner = f.read().strip()
                return False, owner or "未知进程"
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("pid=%d at=%s" % (os.getpid(), M.fdt()))
        return True, ""
    except FileExistsError:
        return False, "另一进程刚刚抢到锁"
    except Exception as e:
        return False, "抢锁失败：%s" % e


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def load_trade_state():
    if not os.path.exists(TRADE_STATE):
        return {}
    try:
        with open(TRADE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_trade_state(st):
    """原子写，避免写一半被强杀留下半个 JSON（下次读不出来会丢 last_live_bar）。"""
    try:
        tmp = TRADE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, TRADE_STATE)
    except Exception as e:
        M.log("      ⚠ 写 trade_state.json 失败：%s" % e)


def append_trade_log(rows):
    """追加成交记录。首次创建时写表头（utf-8-sig，Excel 直接打开不乱码）。"""
    if not rows:
        return
    new = not os.path.exists(TRADE_LOG)
    try:
        with open(TRADE_LOG, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_HEADER)
            for r in rows:
                w.writerow(r)
    except Exception as e:
        M.log("      ⚠ 写 trade_log.csv 失败：%s" % e)


# ==============================================================================
# 1. 币安客户端（只读 + 下单 + 精度规则）
# ==============================================================================
class Binance:
    """极简币安 REST 客户端，只用标准库（HMAC-SHA256 签名）。"""

    def __init__(self, cfg, testnet=None):
        b = cfg["binance"]
        self.testnet = (bool(b.get("use_testnet", False))
                        if testnet is None else bool(testnet))
        self.spot = b["testnet_spot_base"] if self.testnet else b["spot_base"]
        self.fut = b["testnet_futures_base"] if self.testnet else b["futures_base"]
        self.key = (b.get("api_key") or "").strip()
        self.secret = (b.get("api_secret") or "").strip()
        self.symbol = b["symbol"]
        self.base = self.symbol.replace("USDT", "")
        # 代理：留空 = 不走代理（默认，和行情下载保持一致）；
        # 需要科学上网才能连币安的机器上填 http://127.0.0.1:7890
        proxy = (b.get("proxy") or "").strip()
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._offset = None            # 本机时钟与币安服务器的毫秒偏移（懒加载）

    def has_keys(self):
        return bool(self.key and self.secret)

    def endpoint_label(self):
        return "测试网 (testnet)" if self.testnet else "实盘 (mainnet)"

    def sync_time(self):
        """校准本机时钟偏移。

        本机时钟只要快/慢超过 recvWindow（5 秒），所有签名请求都会报
        **-1021 Timestamp for this request is outside of the recvWindow**，
        表现是「明明 key 没错却全部失败」。电教室/老机器的时钟常年没校准，
        所以这里主动跟服务器对一次时间，用偏移量修正 timestamp。
        """
        # 交易域名优先（返回的就是撮合用的时间），但它在本机常年不通；
        # 补一个行情域名 data-api.binance.vision 作备选 —— 它跟 K 线同一个源，
        # 即使不能下单也能对时，避免「时钟快了 → 把还在交易的 K 线当成已收盘」。
        for base, path in ((self.fut, "/fapi/v1/time"), (self.spot, "/api/v3/time"),
                           ("https://data-api.binance.vision", "/api/v3/time")):
            try:
                t0 = time.time()
                req = urllib.request.Request(base + path)
                req.add_header("User-Agent", "btc-trader/1.0")
                with self.opener.open(req, timeout=10) as r:
                    srv = json.loads(r.read().decode()).get("serverTime")
                t1 = time.time()
                if srv:
                    # 用往返中点估计，抵消网络延迟的一半
                    self._offset = int(srv) - int((t0 + t1) / 2.0 * 1000)
                    return self._offset
            except Exception:
                continue
        self._offset = 0
        M.log("      ⚠ 连不上币安的服务器时间接口，按本机时钟处理（可能需要代理）")
        return 0

    def preflight(self):
        """开跑前探一下交易接口通不通，返回 (是否可用, 说明)。

        只探「时间接口」——它不需要 key、不需要权限，通了说明网络到得了币安，
        不通就一定下不了单，早点说清楚比让用户在查余额那一步看到裸超时好。
        """
        for base, path, name in ((self.fut, "/fapi/v1/time", "合约"),
                                 (self.spot, "/api/v3/time", "现货")):
            try:
                req = urllib.request.Request(base + path)
                req.add_header("User-Agent", "btc-trader/1.0")
                with self.opener.open(req, timeout=8) as r:
                    json.loads(r.read().decode())
                return True, "%s接口可达（%s）" % (name, self.endpoint_label())
            except Exception as e:
                last = e
        return False, ("连不上币安交易接口（%s: %s）。多半是这台机器没挂代理 —— "
                       "在 monitor_config.json 的 binance.proxy 里填本地代理地址后再试。"
                       "注意：每日行情走 data-api.binance.vision，"
                       "它能下载不代表交易接口能通。"
                       % (type(last).__name__, last))

    @staticmethod
    def _hint(msg):
        """网络类错误补一句「大概率是没挂代理」，否则用户只看到裸英文。"""
        low = msg.lower()
        keys = ("timed out", "timeout", "10054", "10060", "10061", "urlerror",
                "connection", "ssl", "网络错误")
        if any(k in low for k in keys):
            return (msg + "  ← 这台机器连不上币安**交易**接口。"
                    "① 需要代理的话，在 monitor_config.json 的 binance.proxy 填 "
                    "http://127.0.0.1:7890（Clash 默认端口）这类本地代理地址；"
                    "② 行情走的是 data-api.binance.vision，**它能通不代表交易接口能通**。")
        return msg

    @staticmethod
    def _retryable(msg):
        """这个错误重试一次有没有意义（只对幂等的 GET 用）。"""
        if "HTTP 5" in msg or "HTTP 429" in msg or "HTTP 418" in msg:
            return True
        for k in ("-1003", "-1001", "-1007", "timed out", "timeout",
                  "URLError", "Connection", "网络错误"):
            if k in msg:
                return True
        return False

    def _send_once(self, method, base, path, params, signed, timeout):
        params = dict(params or {})
        if signed:
            if self._offset is None:
                self.sync_time()
            params["timestamp"] = int(time.time() * 1000) + (self._offset or 0)
            params["recvWindow"] = 5000
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            qs = qs + "&signature=" + sig
        else:
            qs = urllib.parse.urlencode(params)
        url = base + path + ("?" + qs if qs else "")
        req = urllib.request.Request(url, method=method)
        req.add_header("X-MBX-APIKEY", self.key)
        req.add_header("User-Agent", "btc-trader/1.0")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError("HTTP %d %s -> %s" % (e.code, path, body))
        except Exception as e:
            # URLError / socket.timeout / ssl 错误统一成中文，别让用户看裸英文栈
            raise RuntimeError(self._hint(
                "网络错误（%s）：%s: %s" % (path, type(e).__name__, e)))

    def _request(self, method, base, path, params=None, signed=False, timeout=25,
                 retries=None):
        """发一个请求。

        重试策略（很重要，直接关系到会不会下重复单）：
        · **GET（查余额/查持仓/查订单）幂等 → 网络抖动、5xx、限频自动重试 3 次**
        · **POST（下单/划转/设杠杆）绝不自动重试** —— 请求可能已经进撮合了，
          再发一次就是双倍仓位。唯一例外是 -1021（时间戳被拒，请求根本没被受理），
          校准时钟后可以安全重发一次。
        """
        if retries is None:
            retries = 3 if method == "GET" else 1
        attempts = 0
        ts_retried = False
        while True:
            attempts += 1
            try:
                return self._send_once(method, base, path, params, signed, timeout)
            except RuntimeError as e:
                msg = str(e)
                if "-1021" in msg and not ts_retried:
                    # 时间戳被拒 → 请求根本没被受理，重新对时后重发是安全的
                    ts_retried = True
                    self._offset = None
                    self.sync_time()
                    M.log("      ⚠ 币安拒绝时间戳（-1021），已校准时钟偏移 %+d ms，重发一次"
                          % (self._offset or 0))
                    continue
                if attempts < retries and self._retryable(msg):
                    time.sleep(1.2 * attempts)
                    continue
                raise

    # ---------------- 只读 ----------------
    def spot_balances(self):
        acc = self._request("GET", self.spot, "/api/v3/account", signed=True)
        out = {}
        for b in acc.get("balances", []):
            free = to_float(b.get("free"))
            locked = to_float(b.get("locked"))
            if free or locked:
                out[b["asset"]] = {"free": free, "locked": locked,
                                   "total": free + locked}
        return out

    def futures_account(self):
        return self._request("GET", self.fut, "/fapi/v2/account", signed=True)

    # 余额一律走 snapshot()（现货 USDT + 现货 BTC + 合约钱包 + 浮盈 才算完整权益）。
    # 不要再单独暴露「只读现货 USDT」的方法，否则又会有人拿它当权益用。
    def futures_position(self, symbol=None):
        sym = symbol or self.symbol
        data = self._request("GET", self.fut, "/fapi/v1/positionRisk",
                             {"symbol": sym}, signed=True)
        active = [p for p in data if abs(to_float(p.get("positionAmt"))) > 0]
        # 双向持仓模式（Hedge Mode）下会同时返回多单和空单两条，本脚本按单向逻辑处理，
        # 直接拒绝比「随便取一条」安全得多。
        if len(active) > 1:
            raise RuntimeError(
                "检测到 %s 处于「双向持仓」模式（同时有多单和空单）。"
                "请到币安合约设置里改成「单向持仓」后重跑。" % sym)
        if active:
            p = active[0]
            return {"amt": to_float(p.get("positionAmt")),
                    "entry": to_float(p.get("entryPrice")),
                    "upnl": to_float(p.get("unRealizedProfit")),
                    "lev": int(to_float(p.get("leverage"), 1)),
                    "margin": to_float(p.get("isolatedMargin")),
                    "liq": to_float(p.get("liquidationPrice"))}
        return {"amt": 0.0, "entry": 0.0, "upnl": 0.0, "lev": None,
                "margin": 0.0, "liq": 0.0}

    def order_status(self, market, order_id):
        base = self.spot if market == "spot" else self.fut
        path = "/api/v3/order" if market == "spot" else "/fapi/v1/order"
        try:
            return self._request("GET", base, path,
                                 {"symbol": self.symbol, "orderId": order_id},
                                 signed=True)
        except Exception:
            return None

    # ---------------- 精度规则 ----------------
    def rules(self, market):
        """从 exchangeInfo 读 LOT_SIZE / MIN_NOTIONAL / PRICE_FILTER，带 24h 缓存。"""
        cache = {}
        if os.path.exists(RULES_CACHE):
            try:
                with open(RULES_CACHE, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}
        ck = "%s:%s" % (market, self.symbol)
        hit = cache.get(ck) or {}
        if hit and (time.time() - to_float(hit.get("_at"))) < 86400:
            return hit

        base = self.spot if market == "spot" else self.fut
        path = "/api/v3/exchangeInfo" if market == "spot" else "/fapi/v1/exchangeInfo"
        info = self._request("GET", base, path, {"symbol": self.symbol})
        sym = None
        for s in info.get("symbols", []):
            if s.get("symbol") == self.symbol:
                sym = s
                break
        if not sym:
            raise RuntimeError("exchangeInfo 里找不到 %s" % self.symbol)

        fl = {x.get("filterType"): x for x in sym.get("filters", [])}
        lot = fl.get("LOT_SIZE", {})
        mn = fl.get("MIN_NOTIONAL") or fl.get("NOTIONAL") or {}
        pf = fl.get("PRICE_FILTER", {})
        out = {
            "_at": time.time(),
            "step": to_float(lot.get("stepSize"), 0.00001),
            "min_qty": to_float(lot.get("minQty"), 0.00001),
            "min_notional": to_float(mn.get("notional") or mn.get("minNotional"), 5.0),
            "tick": to_float(pf.get("tickSize"), 0.01),
        }
        cache[ck] = out
        try:
            with open(RULES_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return out

    # ---------------- 写操作 ----------------
    def set_leverage(self, lev):
        return self._request("POST", self.fut, "/fapi/v1/leverage",
                             {"symbol": self.symbol, "leverage": int(lev)},
                             signed=True)

    def set_margin_type(self, mt):
        try:
            return self._request("POST", self.fut, "/fapi/v1/marginType",
                                 {"symbol": self.symbol, "marginType": mt},
                                 signed=True)
        except RuntimeError as e:
            if "-4046" in str(e):        # 已经是这个模式，无需修改
                return {"msg": "already " + mt}
            raise

    def spot_market_order(self, side, qty_str):
        return self._request("POST", self.spot, "/api/v3/order",
                             {"symbol": self.symbol, "side": side,
                              "type": "MARKET", "quantity": qty_str},
                             signed=True)

    # 现货账户 ⇄ U 本位合约账户 划转（两个账户的 USDT 是分开的，不划转开不出保证金）
    def transfer(self, direction, amount):
        """
        direction: "in"  = 现货 → 合约（开空前用）
                   "out" = 合约 → 现货（买现货前用）
        """
        # /sapi/v1/asset/transfer 只在实盘提供，测试网（testnet.binance.vision）没有这个端点。
        # 不提前拦住的话会得到一个 404，看起来像「代码有 bug」。
        if self.testnet:
            raise RuntimeError(
                "测试网不提供「现货 ⇄ 合约」划转接口（/sapi/v1/asset/transfer）。"
                "请在测试网手动划转后再跑，或改用实盘小额账户验证。")
        t = "MAIN_UMFUTURE" if direction == "in" else "UMFUTURE_MAIN"
        return self._request("POST", self.spot, "/sapi/v1/asset/transfer",
                             {"type": t, "asset": "USDT",
                              "amount": ("%.8f" % amount).rstrip("0").rstrip(".")},
                             signed=True)

    def futures_market_order(self, side, qty_str, reduce_only=False):
        # newOrderRespType=RESULT 必须显式指定：默认是 ACK，返回里的 executedQty /
        # avgPrice 全是 0，日志会写成「成交 0.000 @ 0.00」，看着像没成交。
        p = {"symbol": self.symbol, "side": side, "type": "MARKET",
             "quantity": qty_str, "newOrderRespType": "RESULT"}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._request("POST", self.fut, "/fapi/v1/order", p, signed=True)

    # ---------------- 止损条件单 ----------------
    def fut_open_orders(self):
        """当前挂着的合约委托（含条件单）。"""
        data = self._request("GET", self.fut, "/fapi/v1/openOrders",
                             {"symbol": self.symbol}, signed=True)
        return data if isinstance(data, list) else []

    def fut_stop_market(self, side, stop_px_str, working_type="MARK_PRICE"):
        """
        挂「触发即全平」的止损市价单。

        用 closePosition=true：触发时把当前持仓一次平光，不用算数量，
        也不会因为 stepSize 取整留下零头。币安规定 closePosition 与
        quantity / reduceOnly 互斥，所以这里都不传。
        """
        p = {"symbol": self.symbol, "side": side, "type": "STOP_MARKET",
             "stopPrice": stop_px_str, "closePosition": "true",
             "workingType": working_type, "timeInForce": "GTC"}
        return self._request("POST", self.fut, "/fapi/v1/order", p, signed=True)

    def fut_cancel(self, order_id):
        return self._request("DELETE", self.fut, "/fapi/v1/order",
                             {"symbol": self.symbol, "orderId": order_id},
                             signed=True)


# ==============================================================================
# 2. 账户快照 & 目标仓位
# ==============================================================================
def snapshot(bnc, price):
    """读账户，算出权益、可用资金、现有仓位。"""
    bal = bnc.spot_balances()
    btc = bal.get(bnc.base, {"free": 0.0, "locked": 0.0, "total": 0.0})
    usdt = bal.get("USDT", {"free": 0.0, "locked": 0.0, "total": 0.0})
    fut = bnc.futures_position()
    acc = bnc.futures_account()

    spot_qty = btc["total"]
    spot_val = spot_qty * price

    # 合约账户的钱必须计入权益：现货和合约的 USDT 是两个独立账户，
    # 空头平仓后保证金会留在合约账户里，漏掉就会把权益算成 0 → 目标仓位归零。
    fut_wallet = to_float(acc.get("totalWalletBalance"))
    available = to_float(acc.get("availableBalance"))

    equity = usdt["total"] + spot_val + fut_wallet + fut["upnl"]

    return {
        "spot_qty": spot_qty, "spot_free": btc["free"], "spot_val": spot_val,
        "usdt_free": usdt["free"], "usdt_total": usdt["total"],
        "fut_amt": fut["amt"], "fut_entry": fut["entry"], "fut_upnl": fut["upnl"],
        "fut_lev": fut["lev"], "fut_margin": fut["margin"], "fut_liq": fut["liq"],
        "fut_wallet": fut_wallet,
        "equity": equity,
        "available": available,
        "wallet": fut_wallet,
        "price": price,
    }


def target_of(cfg, bars, sig):
    """目标方向 + 目标敞口（与回测/邮件完全同一口径）。"""
    n = len(bars)
    expo, dvol = M.exposure_at(bars, cfg, n)      # 用最新已收盘那根的波动率
    return int(sig[-1]), float(expo), float(dvol)


# ==============================================================================
# 3. 生成动作计划（不执行）
# ==============================================================================
def build_actions(cfg, bnc, snap, target_dir, expo, sig_text_now, resize=None):
    """
    对比「目标」和「实际」，返回结构化动作列表。
    每条：{kind, market, side, qty(float), qty_str, notional, text}

    关键点：现货账户和 U 本位合约账户的 USDT 是两个独立账户，必须显式划转。
    所以这里用「预演余额」（proj_spot_usdt / proj_fut_avail）逐条推演，
    平仓所得、卖出所得都算进去，避免「明明有钱却说余额不足」。

    resize：持仓期间要不要按当日波动率调仓。
        None → 读 config 的 binance.resize_on_drift（默认 false）
        false（默认）= 只在进场那天定名义，持有期间**不动**（= 回测口径）
        true  = 每天把名义对齐到「当日权益 × 当日敞口」（旧行为）
        ⚠ 实测每日再平衡 9 年终值 55.97×，而回测（持仓不动）是 162.62×，差 −65.6%，
          10 年里 9 年负贡献。所以默认必须是 false，否则实盘跑出来的不是回测的那个策略。
    """
    b = cfg["binance"]
    price = snap["price"]
    tol = float(b.get("rebalance_tolerance", 0.02))
    min_notional_cfg = float(b.get("min_notional_usdt", 20.0))
    fee_buffer = float(b.get("fee_buffer", 0.002))
    lev = max(1, int(to_float(b.get("leverage"), 1)))   # 0 或空会除零，兜到 1
    if resize is None:
        resize = bool(to_float(b.get("resize_on_drift")))

    acts, notes = [], []
    equity = snap["equity"]
    if equity <= 0:
        notes.append("账户权益为 0（USDT/现货/合约都没有余额），无法计算目标仓位。")
        return acts, notes

    # 交易所精度
    try:
        r_spot = bnc.rules("spot")
        r_fut = bnc.rules("futures")
    except Exception as e:
        notes.append("读取交易所精度规则失败：%s → 不下单（避免精度错误拒单）" % e)
        return acts, notes

    notes.append("交易所最小下单：现货 %.5f %s / %.1f USDT，合约 %.3f %s / %.1f USDT"
                 % (r_spot["min_qty"], bnc.base, r_spot["min_notional"],
                    r_fut["min_qty"], bnc.symbol, r_fut["min_notional"]))

    target_notional = equity * expo
    target_spot_qty = target_notional / price if target_dir > 0 else 0.0
    sf = M.short_size_factor(lev)
    target_fut_qty = (target_notional * sf / price) if target_dir < 0 else 0.0

    # ---- 持仓期间不调仓（回测口径）----
    # 回测引擎只在「进场那根」按 权益×敞口 定名义，之后整段持有不再动。
    # 如果这里每天按当日敞口重算目标，就等于给策略偷偷加了一个「每日再平衡」，
    # 实测会把 162.62× 打成 55.97×。所以只要同方向已经有仓位，就把目标设成「现有仓位」，
    # 让下面的差额逻辑自然判定「已对齐」—— 只保留方向翻转/清仓的判断。
    if not resize and target_dir != 0:
        if target_dir > 0 and snap["spot_qty"] > 0:
            target_spot_qty = snap["spot_qty"]
            target_notional = target_spot_qty * price
        elif target_dir < 0 and snap["fut_amt"] < 0:
            target_fut_qty = abs(snap["fut_amt"])
            target_notional = target_fut_qty * price

    if resize:
        notes.append("账户权益 %.2f USDT × 敞口 %.2f× = 目标名义 %.2f USDT"
                     % (equity, expo, target_notional))
    elif target_dir != 0 and (snap["spot_qty"] > 0 or snap["fut_amt"] < 0):
        notes.append("持仓期间不调仓：沿用进场时的名义 %.2f USDT"
                     "（今天若重新进场会用 权益 %.2f × 敞口 %.2f× = %.2f USDT）"
                     % (target_notional, equity, expo, equity * expo))
    else:
        notes.append("账户权益 %.2f USDT × 敞口 %.2f× = 目标名义 %.2f USDT（今天进场，按此定仓）"
                     % (equity, expo, target_notional))

    # 预演余额：平仓/卖出所得都会改变可用资金
    proj_spot_usdt = snap["usdt_free"]
    proj_fut_avail = snap["available"]
    min_move = max(min_notional_cfg, 1.0)          # 划转低于 1 USDT 没意义

    # ---------- 1) 先平反向腿 ----------
    have_fut = snap["fut_amt"]

    if have_fut != 0 and target_dir >= 0:
        q = abs(have_fut)
        # 平空 = 买入，平多 = 卖出。以前这里恒写 BUY，遇到「合约多头」时
        # reduceOnly 买单会被币安直接拒掉（-2022 ReduceOnly Order is rejected）。
        side = "BUY" if have_fut < 0 else "SELL"
        bad, why = qty_problem(q, price, r_fut, min_notional_cfg)
        if bad:
            notes.append("合约持仓 %+.3f 想平掉，但%s → 跳过（请在币安手动平仓）。" % (have_fut, why))
        else:
            acts.append({"kind": "fut_close", "market": "futures", "side": side,
                         "qty": q, "qty_str": fmt_qty(q, r_fut["step"]),
                         "notional": q * price,
                         "text": "平合约%s头 %.3f（reduceOnly 市价%s）"
                                 % ("空" if have_fut < 0 else "多", q,
                                    "买入" if side == "BUY" else "卖出")})
        # 平仓释放的保证金：逐仓的 isolatedMargin 已经含未实现盈亏，
        # 再单独加一次 upnl 会重复计算（曾导致多划转一笔）。
        released = (snap["fut_margin"] if snap["fut_margin"] > 0
                    else q * price / max(lev, 1) + snap["fut_upnl"])
        proj_fut_avail += max(released, 0.0)
        have_fut = 0

    if snap["spot_qty"] > 0 and target_dir <= 0:
        # 卖「可用」而不是「总量」：有挂单/冻结时卖总量会被拒（余额不足）
        q = snap["spot_free"]
        if snap["spot_qty"] - q > 1e-9:
            notes.append("现货有 %.8f %s 被挂单或冻结，本次只卖可用的 %.8f。"
                         % (snap["spot_qty"] - q, bnc.base, q))
        bad, why = qty_problem(q, price, r_spot, min_notional_cfg)
        if bad:
            if q > 0 or snap["spot_qty"] > 0:
                notes.append("现货 %.8f %s 想清掉，但%s → 跳过（零头只能手动处理）。"
                             % (snap["spot_qty"], bnc.base, why))
        else:
            acts.append({"kind": "spot_sell_all", "market": "spot", "side": "SELL",
                         "qty": q, "qty_str": fmt_qty(q, r_spot["step"]),
                         "notional": q * price,
                         "text": "卖出现货 %.6f %s（全部清掉，转成 USDT）"
                                 % (q, bnc.base)})
            proj_spot_usdt += q * price * (1.0 - fee_buffer)

    # ---------- 2) 建目标腿 ----------
    if target_dir > 0:
        diff = target_spot_qty - snap["spot_qty"]
        diff_notional = abs(diff) * price
        # 容忍度：差额小于「目标名义 × tol」且小于最小下单额 → 不动
        dead = max(target_notional * tol, min_notional_cfg, r_spot["min_notional"])
        if diff_notional < dead:
            if target_notional < min_notional_cfg:
                notes.append("目标名义只有 %.2f USDT，低于最小下单额 %.0f USDT → 不值得下单。"
                             % (target_notional, min_notional_cfg))
            else:
                notes.append("现货与目标差 %.2f USDT（< 容忍 %.2f），视为已对齐，不动。"
                             % (diff_notional, dead))
        elif diff > 0:
            need_usdt = diff * price / (1.0 - fee_buffer)
            # 现货 USDT 不够 → 从合约账户划转（合约可用要扣掉已有持仓占用的保证金）
            if proj_spot_usdt < need_usdt and proj_fut_avail > 0:
                move = min(need_usdt - proj_spot_usdt, proj_fut_avail)
                if move >= min_move:
                    acts.append({"kind": "transfer_out", "market": "transfer",
                                 "side": "->", "qty": move, "qty_str": "%.2f" % move,
                                 "notional": move,
                                 "text": "划转 %.2f USDT：合约账户 → 现货账户（买现货要用）"
                                         % move})
                    proj_spot_usdt += move
                    proj_fut_avail -= move
                else:
                    notes.append("合约可用仅 %.2f USDT，不足 1 USDT 起划，跳过划转。"
                                 % proj_fut_avail)

            # 买入受现货 USDT 约束（合约浮盈不能直接买现货）
            spend_cap = proj_spot_usdt * (1.0 - fee_buffer)
            buy_qty = min(diff, spend_cap / price)
            bad, why = qty_problem(buy_qty, price, r_spot, min_notional_cfg)
            if bad:
                notes.append("现货 USDT 可用 %.2f，买不了 %.6f %s（%s）→ 跳过买入。"
                             % (proj_spot_usdt, diff, bnc.base, why))
            else:
                buy_qty = float(fmt_qty(buy_qty, r_spot["step"]))
                if buy_qty < diff * 0.999:
                    notes.append("现货 USDT 只够买 %.6f（目标差 %.6f），按可用余额截断。"
                                 % (buy_qty, diff))
                acts.append({"kind": "spot_buy", "market": "spot", "side": "BUY",
                             "qty": buy_qty, "qty_str": fmt_qty(buy_qty, r_spot["step"]),
                             "notional": buy_qty * price,
                             "text": "现货买入 %.6f %s（≈ %.2f USDT）"
                                     % (buy_qty, bnc.base, buy_qty * price)})
        else:
            q = -diff
            bad, why = qty_problem(q, price, r_spot, min_notional_cfg)
            if bad:
                notes.append("需要减仓 %.6f %s，但%s → 跳过。" % (q, bnc.base, why))
            else:
                acts.append({"kind": "spot_reduce", "market": "spot", "side": "SELL",
                             "qty": q, "qty_str": fmt_qty(q, r_spot["step"]),
                             "notional": q * price,
                             "text": "现货减仓 %.6f %s（≈ %.2f USDT）"
                                     % (q, bnc.base, q * price)})

    elif target_dir < 0:
        if have_fut > 0:
            # 账户里是合约多头，与目标空头相反 → 先平掉，下一轮再开空
            q = have_fut
            notes.append("⚠ 账户里是合约多头 %.3f，与目标空头相反 → 本轮先平掉。" % q)
            acts.append({"kind": "fut_close", "market": "futures", "side": "SELL",
                         "qty": q, "qty_str": fmt_qty(q, r_fut["step"]),
                         "notional": q * price,
                         "text": "平合约多头 %.3f（reduceOnly 市价卖出）" % q})
            notes.append("平掉后重新对账再决定是否开空（最多 3 轮）。")
            return acts, notes

        if have_fut == 0:
            # 开空需要合约账户里有保证金：不够就从现货划转。
            # 按「实际要开的量」算（target_fut_qty 已含 1% 保证金缓冲），
            # 不能按 target_notional 算 —— 1× 下那会把现货余额整锅搬走，还多搬。
            # ⚠ 先按 stepSize 取整再算，否则会把「取整后被砍掉的那一截」也划过去，
            #   白白留在合约账户里（1000 U 账户开 0.002 曾划 248 却只用到 200）。
            _want_round = float(fmt_qty(target_fut_qty, r_fut["step"])) or target_fut_qty
            need_margin = (_want_round * price) / lev / (1.0 - fee_buffer)
            if proj_fut_avail < need_margin and proj_spot_usdt > 0:
                move = min(need_margin - proj_fut_avail, proj_spot_usdt)
                if move >= min_move:
                    acts.append({"kind": "transfer_in", "market": "transfer",
                                 "side": "<-", "qty": move, "qty_str": "%.2f" % move,
                                 "notional": move,
                                 "text": "划转 %.2f USDT：现货账户 → 合约账户（开空要保证金）"
                                         % move})
                    proj_fut_avail += move
                    proj_spot_usdt -= move
                else:
                    notes.append("现货 USDT 仅 %.2f，不足 1 USDT 起划，跳过划转。"
                                 % proj_spot_usdt)

            avail = proj_fut_avail
            max_notional = avail * lev * (1.0 - fee_buffer)
            # 注意必须取 min：以前写成 want = max_notional/price，在
            # 「可用略小于目标」时反而会把仓位开到 0.998×，吃掉 1% 的保证金缓冲，
            # 1× 逐仓下会直接因为「保证金不足」被拒单。
            want = min(target_fut_qty, max_notional / price)
            if max_notional < target_notional:
                notes.append("合约可用保证金 %.2f USDT 只够开 %.2f USDT 名义（目标 %.2f），按可用截断。"
                             % (avail, max_notional, target_notional))
            bad, why = qty_problem(want, price, r_fut, min_notional_cfg)
            if bad:
                notes.append("可用保证金不足，开不出最小空头仓位（%s）→ 跳过。" % why)
            else:
                want = float(fmt_qty(want, r_fut["step"]))
                acts.append({"kind": "fut_setup", "market": "futures", "side": "-",
                             "qty": 0.0, "qty_str": "0", "notional": 0.0,
                             "text": "设置 %s 为 %s 逐仓 %d×"
                                     % (bnc.symbol, b["margin_type"], lev)})
                acts.append({"kind": "fut_open", "market": "futures", "side": "SELL",
                             "qty": want, "qty_str": fmt_qty(want, r_fut["step"]),
                             "notional": want * price,
                             "text": "开合约空头 %.3f %s（市价卖出，逐仓 %d×）"
                                     % (want, bnc.symbol, lev)})
                if sf < 1.0:
                    notes.append("已按 %.0f%% 缩量留保证金缓冲（1× 逐仓会占满保证金）。"
                                 % (sf * 100))
        else:
            # 已有空头：只在明显偏离时调整
            have = abs(have_fut)
            diff = target_fut_qty - have
            if abs(diff) * price < max(target_notional * tol, r_fut["min_notional"],
                                       min_notional_cfg):
                notes.append("合约空头 %.3f 已接近目标 %.3f，不动。" % (have, target_fut_qty))
            elif diff > 0:
                bad, why = qty_problem(diff, price, r_fut, min_notional_cfg)
                if bad:
                    notes.append("空头需要加仓 %.6f，但%s → 跳过。" % (diff, why))
                else:
                    acts.append({"kind": "fut_add", "market": "futures", "side": "SELL",
                                 "qty": diff, "qty_str": fmt_qty(diff, r_fut["step"]),
                                 "notional": diff * price,
                                 "text": "空头加仓 %.3f %s（≈ %.2f USDT）"
                                         % (diff, bnc.symbol, diff * price)})
            else:
                q = -diff
                bad, why = qty_problem(q, price, r_fut, min_notional_cfg)
                if bad:
                    notes.append("空头需要减仓 %.6f，但%s → 跳过。" % (q, why))
                else:
                    acts.append({"kind": "fut_reduce", "market": "futures", "side": "BUY",
                                 "qty": q, "qty_str": fmt_qty(q, r_fut["step"]),
                                 "notional": q * price,
                                 "text": "空头减仓 %.3f %s（reduceOnly）" % (q, bnc.symbol)})

    # ---------- 3) 目标空仓 ----------
    if target_dir == 0:
        if snap["fut_amt"] == 0 and snap["spot_qty"] <= 0:
            notes.append("已是空仓，无需操作。")
        elif snap["fut_amt"] == 0:
            notes.append("已无合约持仓。")

    return acts, notes


# ==============================================================================
# 4. 执行
# ==============================================================================
def run_action(bnc, act, cfg):
    """执行一条动作，返回 (成功, 订单号, 说明)。"""
    b = cfg["binance"]
    kind = act["kind"]
    # 兜底：数量取整后为 0 的订单一定失败，宁可跳过也不要发出去
    if kind != "fut_setup" and str(act.get("qty_str")) in ("", "0", "0.0"):
        return False, "-", "数量取整后为 0，已跳过（避免无效订单）"
    try:
        if kind == "fut_setup":
            bnc.set_margin_type(b["margin_type"])
            bnc.set_leverage(b["leverage"])
            return True, "-", "已设置 %s 逐仓 %d×" % (b["margin_type"], int(b["leverage"]))

        if kind in ("fut_close", "fut_reduce", "fut_add", "fut_open"):
            r = bnc.futures_market_order(
                act["side"], act["qty_str"],
                reduce_only=(kind in ("fut_close", "fut_reduce")))
            oid = r.get("orderId", "-")
            st = bnc.order_status("futures", oid) or {}
            status = st.get("status") or r.get("status") or "SENT"
            filled = to_float(st.get("executedQty") or r.get("executedQty"))
            avg = to_float(st.get("avgPrice") or r.get("avgPrice"))
            return True, oid, "%s · 成交 %.3f @ %.2f" % (status, filled, avg)

        if kind in ("transfer_in", "transfer_out"):
            direction = "in" if kind == "transfer_in" else "out"
            r = bnc.transfer(direction, act["qty"])
            tid = r.get("tranId", "-")
            return True, tid, "划转成功 · %.2f USDT（tranId %s）" % (act["qty"], tid)

        if kind in ("spot_buy", "spot_sell_all", "spot_reduce"):
            r = bnc.spot_market_order(act["side"], act["qty_str"])
            oid = r.get("orderId", "-")
            st = bnc.order_status("spot", oid) or {}
            status = st.get("status") or r.get("status") or "SENT"
            filled = to_float(st.get("executedQty") or r.get("executedQty"))
            spent = to_float(st.get("cummulativeQuoteQty") or r.get("cummulativeQuoteQty"))
            avg = (spent / filled) if filled else 0.0
            return True, oid, "%s · 成交 %.6f @ %.2f" % (status, filled, avg)

        return False, "-", "未知动作 %s" % kind
    except Exception as e:
        return False, "-", "失败：%s" % e


def execute(cfg, bnc, target_dir, expo, price, bar_date, sig_label,
            live, force=False, max_rounds=3, lock=True, resize=None):
    """对外入口：真实下单时先抢机器级锁，防止「定时任务 + 手动双击」并发下双倍单。

    btc_trade 自己的 main() 已经先抢了锁，那里传 lock=False；
    btc_monitor 的 --live 走这里，必须让它自己抢，否则定时任务和
    「一键下单.bat」同时跑就会各下一遍。
    """
    if not (live and lock):
        return _execute(cfg, bnc, target_dir, expo, price, bar_date, sig_label,
                        live, force=force, max_rounds=max_rounds, resize=resize)
    got, owner = claim_lock()
    if not got:
        raise TradeLockBusy(owner)
    try:
        return _execute(cfg, bnc, target_dir, expo, price, bar_date, sig_label,
                        live, force=force, max_rounds=max_rounds, resize=resize)
    finally:
        release_lock()


def sync_stop_order(cfg, bnc, snap, price):
    """
    把合约账户里的止损条件单对齐到「当前空头持仓 × short_stop」。

    幂等，重复跑安全：
      · 没有空头持仓 → 撤掉所有「触发即全平」的条件单
      · 有空头持仓   → 必须恰好一张，触发价 = 成本价 ×(1+short_stop)
        已有的那张价格差 < 0.3% 就不动，否则撤旧挂新

    为什么要按「成本价」而不是现价：策略是固定止损（亏 5% 就走），
    不是移动止损。浮盈变大时止损价不会跟着抬，这一点邮件里也写明了。

    ⚠ closePosition=true → 触发时全平，不用管数量精度。
    ⚠ workingType 默认 MARK_PRICE（标记价格），抗插针；回测是按成交价判断的，
      所以极端行情下实盘可能比回测晚一点点触发。
    """
    b = cfg["binance"]
    ss = to_float(cfg["strategy"].get("short_stop"))
    acts, notes = [], []
    if ss <= 0:
        return acts, notes

    orders = bnc.fut_open_orders()
    stops = [o for o in orders
             if o.get("type") in ("STOP_MARKET", "STOP")
             and str(o.get("closePosition")).lower() == "true"]

    amt = snap["fut_amt"]
    entry = snap["fut_entry"]

    # 没有空头持仓（或不是空头）→ 全部撤掉，别留孤儿单
    if amt >= 0 or entry <= 0:
        for o in stops:
            bnc.fut_cancel(o["orderId"])
            acts.append("撤销止损单 #%s（当前没有空头持仓）" % o["orderId"])
        return acts, notes

    want = entry * (1.0 + ss)
    rule = bnc.rules("fut")
    tick = to_float(rule.get("tick"), 0.1)
    # 向上取整到 tickSize：宁可早一个 tick 止损，也不要因为差一档挂不上。
    # 代价是实际止损幅度比设定值高不到 0.01%。
    want = math.ceil(want / tick) * tick
    want_s = ("%.8f" % want).rstrip("0").rstrip(".")

    keep = None
    for o in stops:
        sp = to_float(o.get("stopPrice"))
        if sp > 0 and abs(sp / want - 1.0) < 0.003:
            keep = o
            break
    for o in stops:
        if keep and o["orderId"] == keep["orderId"]:
            continue
        bnc.fut_cancel(o["orderId"])
        acts.append("撤销旧止损单 #%s（触发价 %s，已不匹配）"
                    % (o["orderId"], o.get("stopPrice")))
    if keep:
        notes.append("止损单已在：触发价 %s（#%s）" % (keep.get("stopPrice"), keep["orderId"]))
        return acts, notes

    wt = b.get("stop_working_type") or "MARK_PRICE"
    r = bnc.fut_stop_market("BUY", want_s, working_type=wt)
    acts.append("挂止损单：买入平空 · 触发价 %s · 市价 · %s（#%s）"
                % (want_s, wt, r.get("orderId")))
    return acts, notes


def _execute(cfg, bnc, target_dir, expo, price, bar_date, sig_label,
             live, force=False, max_rounds=3, resize=None):
    """
    对账 → 下单 → 再对账，最多 max_rounds 轮，直到没有动作。
    返回 (ok, rounds_detail, final_snapshot)
    """
    tst = load_trade_state()
    last_live_bar = tst.get("last_live_bar")
    log_rows = []
    rounds = []

    if live and not force and last_live_bar == bar_date:
        M.log("      提示：%s 这根日线之前已经执行过一次（%s），本次做对账补齐。"
              % (bar_date, tst.get("last_live_at") or "—"))

    def _sync_stop(snap):
        """
        仓位动作做完后再对齐止损单 —— 顺序不能反：先有仓位才有止损。
        只有 auto_stop_order=true 才做，默认关闭（手动挂单的人不需要程序碰条件单）。
        """
        if not live or not to_float(cfg["binance"].get("auto_stop_order")):
            return
        try:
            s_acts, s_notes = sync_stop_order(cfg, bnc, snap, price)
        except Exception as e:
            M.log("      ✘ 止损单对齐失败：%s（请手动到币安挂/撤条件单）" % e)
            rounds.append({"round": "stop", "actions": [],
                           "notes": ["止损单对齐失败：%s" % e]})
            return
        if s_acts or s_notes:
            M.log("      ── 止损单 ──")
            for x in s_acts:
                M.log("      · " + x)
            for x in s_notes:
                M.log("      " + x)
            rounds.append({"round": "stop",
                           "actions": [{"text": x} for x in s_acts],
                           "notes": s_notes})

    for rd in range(1, max_rounds + 1):
        snap = snapshot(bnc, price)
        acts, notes = build_actions(cfg, bnc, snap, target_dir, expo, sig_label,
                                    resize=resize)

        M.log("")
        M.log("      ── 第 %d 轮对账 ──" % rd)
        M.log("      现货 %.6f %s（%.2f USDT）· USDT 可用 %.2f · 合约 %+.3f（浮盈 %+.2f）"
              % (snap["spot_qty"], bnc.base, snap["spot_val"],
                 snap["usdt_free"], snap["fut_amt"], snap["fut_upnl"]))
        M.log("      账户权益 %.2f USDT" % snap["equity"])
        for nt in notes:
            M.log("      " + nt)

        if not acts:
            M.log("      ✔ 已对齐，本轮无需下单")
            rounds.append({"round": rd, "actions": [], "notes": notes})
            if rd == 1:
                _sync_stop(snap)
                return True, rounds, snap
            break

        M.log("      待执行 %d 个动作：" % len(acts))
        for a in acts:
            M.log("        · " + a["text"])

        if not live:
            M.log("      （演练模式，不执行）")
            rounds.append({"round": rd, "actions": acts, "notes": notes})
            return True, rounds, snap

        for a in acts:
            M.log("      → 执行：" + a["text"])
            ok, oid, detail = run_action(bnc, a, cfg)
            M.log(("        ✔ " if ok else "        ✘ ") + detail)
            log_rows.append([M.fdt(), bar_date, sig_label, a["kind"],
                             a["market"], a["side"], a["qty_str"],
                             "%.2f" % price, "%.2f" % a["notional"],
                             str(oid), "OK" if ok else "FAIL", detail])
            if not ok:
                append_trade_log(log_rows)
                M.log("      ✘ 有动作失败，立即停止（避免半个仓位）。请检查后重跑。")
                return False, rounds + [{"round": rd, "actions": acts, "notes": notes,
                                         "failed": detail}], snap

        rounds.append({"round": rd, "actions": acts, "notes": notes})
        if rd < max_rounds:
            M.log("      等待 2 秒后重新对账 ...")
            time.sleep(2)

    # 跑满 max_rounds 还在产生动作 = 没收敛（通常是资金/精度问题），不能当成功
    converged = not acts
    if not converged:
        M.log("      ⚠ 已跑满 %d 轮仍有未完成动作（最后一轮 %d 个），请重跑或手工检查。"
              % (max_rounds, len(acts)))

    snap = snapshot(bnc, price)
    append_trade_log(log_rows)
    _sync_stop(snap)

    if live:
        tst["last_live_bar"] = bar_date
        tst["last_live_at"] = M.fdt()
    tst["last_run"] = M.fdt()
    tst["last_bar"] = bar_date
    tst["last_signal"] = sig_label
    tst["last_target_dir"] = target_dir
    tst["last_expo"] = expo
    tst["last_equity"] = snap["equity"]
    tst["last_spot_qty"] = snap["spot_qty"]
    tst["last_fut_amt"] = snap["fut_amt"]
    tst["last_rounds"] = len(rounds)
    save_trade_state(tst)
    return converged, rounds, snap


def preview(cfg, bnc, target_dir, expo, price, sig_label, resize=None):
    """
    只读预览：读账户 → 算出「该做什么」，不下单。
    给 btc_monitor 的邮件「执行计划」板块复用，保证**邮件里写的 = 实际会做的**。
    """
    snap = snapshot(bnc, price)
    acts, notes = build_actions(cfg, bnc, snap, target_dir, expo, sig_label, resize=resize)
    return snap, acts, notes


# ==============================================================================
# 5. 主流程
# ==============================================================================
def show_status(cfg, bnc, bar_date, sig_label, target_dir, expo, price):
    snap = snapshot(bnc, price)
    M.log("")
    M.log("【账户快照】")
    M.log("  连接：%s" % bnc.endpoint_label())
    M.log("  现货 %-6s %.8f（市值 %.2f USDT，可用 %.8f）"
          % (bnc.base, snap["spot_qty"], snap["spot_val"], snap["spot_free"]))
    M.log("  USDT          可用 %.2f / 合计 %.2f" % (snap["usdt_free"], snap["usdt_total"]))
    if snap["fut_amt"]:
        M.log("  合约 %-6s %+.3f @ %.2f · 浮盈 %+.2f · %d× 逐仓 · 保证金 %.2f · 强平价 %.2f"
              % (bnc.symbol, snap["fut_amt"], snap["fut_entry"], snap["fut_upnl"],
                 snap["fut_lev"] or 0, snap["fut_margin"], snap["fut_liq"]))
    else:
        M.log("  合约 %-6s 无持仓" % bnc.symbol)
    # 空头止损价：查看账户时也直接看得到，不用翻邮件
    _ss = to_float(cfg["strategy"].get("short_stop"))
    if _ss > 0 and snap["fut_amt"] < 0 and snap["fut_entry"] > 0:
        M.log("  空头止损价 %.2f（成本价 × %.2f，逆行 %.0f%% 市价买回）"
              % (snap["fut_entry"] * (1 + _ss), 1 + _ss, _ss * 100))
    M.log("  账户权益 %.2f USDT · 合约可用保证金 %.2f" % (snap["equity"], snap["available"]))
    M.log("")
    M.log("【策略目标】")
    M.log("  最新已收盘日线 %s · 收盘 %.2f" % (bar_date, price))
    M.log("  信号：%s · 敞口 %.2f× → 今天若进场，名义 %.2f USDT"
          % (sig_label, expo, snap["equity"] * expo))
    _resize = bool(to_float(cfg["binance"].get("resize_on_drift")))
    _holding = (snap["spot_qty"] > 0) or (snap["fut_amt"] != 0)
    if _holding and not _resize:
        M.log("  持仓期间不调仓：沿用进场时的名义（回测口径）。"
              "要让每天都对齐敞口，把 binance.resize_on_drift 改成 true。")
    if target_dir > 0:
        M.log("  目标：现货持有 %.6f %s（当前 %.6f）"
              % (snap["equity"] * expo / price, bnc.base, snap["spot_qty"]))
    elif target_dir < 0:
        M.log("  目标：合约空头 %.3f %s（当前 %+.3f）"
              % (snap["equity"] * expo * M.short_size_factor(cfg["binance"]["leverage"]) / price,
                 bnc.symbol, snap["fut_amt"]))
    else:
        M.log("  目标：空仓（当前现货 %.6f / 合约 %+.3f）"
              % (snap["spot_qty"], snap["fut_amt"]))
    return snap


def main():
    argv = sys.argv[1:]
    live_flag = "--live" in argv
    force = "--force" in argv
    status_only = "--status" in argv
    testnet_override = True if "--testnet" in argv else None

    cfg = M.load_config()
    b = cfg["binance"]
    st = cfg["strategy"]

    M.log("=" * 100)
    M.log(" BTC 策略 · 币安下单与仓位调整")
    M.log("=" * 100)

    # ---- 闸门检查 ----
    enabled = bool(b.get("enabled"))
    bnc = Binance(cfg, testnet_override)

    M.log("")
    M.log("【前置检查】")
    M.log("  %s 配置 enabled = %s" % ("✔" if enabled else "✘", enabled))
    M.log("  %s API Key %s" % ("✔" if bnc.has_keys() else "✘",
                               "已配置" if bnc.has_keys() else "未配置"))
    M.log("  %s 命令行 --live %s" % ("✔" if live_flag else "✘",
                                     "已加" if live_flag else "未加"))
    M.log("  连接目标：%s" % bnc.endpoint_label())
    M.log("  空头杠杆：%d× 逐仓（下单量系数 %.2f）"
          % (int(b["leverage"]), M.short_size_factor(b["leverage"])))

    # ---- 网络预检 ----
    # 只探时间接口：不需要 key、不需要权限。通不了就一定下不了单，
    # 早点说清楚，比让用户在「查余额」那一步看到裸超时好得多。
    if bnc.has_keys():
        M.log("  正在探测币安交易接口 ...")
        reachable, why = bnc.preflight()
        M.log("  %s %s" % ("✔" if reachable else "✘", why))
        if not reachable:
            M.log("")
            M.log("  提示：行情下载（data-api.binance.vision）和交易接口是两套域名，")
            M.log("        行情能更新不代表这里能下单。")
            M.log("")
            M.log("=" * 100)
            return 3

    if status_only:
        if not bnc.has_keys():
            M.log("")
            M.log("  ✘ 未配置 API Key，无法读取账户。")
            M.log("    请编辑 monitor_config.json 的 binance.api_key / api_secret。")
            return 1
        if not enabled:
            M.log("")
            M.log("  提示：enabled 仍是 false，但 --status 只读不写，可以正常查看。")

    # ---- 行情与信号 ----
    M.log("")
    M.log("【信号】")
    raw = M.load_bars(st["symbol"], force=False,
                      max_age_h=float(st.get("cache_max_age_hours", 1)))
    bars = M.closed_bars(raw)
    fast, slow = st["primary_set"]
    sig = M.regime_long(bars, E.sig_ema_cross(bars, fast, slow),
                        st.get("regime_ma", 0))
    bar_date = M.fdate(bars[-1]["t"])
    price = bars[-1]["close"]
    target_dir, expo, dvol = target_of(cfg, bars, sig)
    sig_label = M.sig_text(target_dir)

    M.log("  最新已收盘：%s  收盘 %.2f USDT" % (bar_date, price))
    M.log("  均线 EMA %d/%d · 日波动 %.2f%% · 信号 %s · 目标敞口 %.2f×"
          % (fast, slow, dvol * 100, sig_label, expo))

    # ---- 只读模式 ----
    if status_only:
        show_status(cfg, bnc, bar_date, sig_label, target_dir, expo, price)
        M.log("")
        M.log("=" * 100)
        return 0

    # ---- 演练 ----
    if not (enabled and live_flag):
        M.log("")
        M.log("【演练模式】不会真实下单")
        if not enabled:
            M.log("  原因：monitor_config.json 的 binance.enabled 还是 false。")
            M.log("  要开启：把 enabled 改成 true、填好 api_key/api_secret，")
            M.log("          再双击「一键下单.bat」（等价于 --live）。")
        elif not live_flag:
            M.log("  原因：命令行没加 --live。")
        if not bnc.has_keys():
            M.log("")
            M.log("  ⚠ 没有 API Key，无法读取真实账户，只能给目标方向：")
            M.log("    目标：%s · 名义 %.2f USDT × 敞口 %.2f×"
                  % (sig_label, float(st.get("base_capital_usdt", 1000.0)), expo))
            M.log("    （按本金 %.0f USDT 估算，实际以账户余额为准）"
                  % float(st.get("base_capital_usdt", 1000.0)))
            M.log("")
            M.log("=" * 100)
            return 0
        # 有 key 但未开启 → 读账户做演练对账
        ok, rounds, snap = execute(cfg, bnc, target_dir, expo, price,
                                   bar_date, sig_label, live=False)
        M.log("")
        M.log("=" * 100)
        return 0

    # ---- 真实下单 ----
    got, owner = claim_lock()
    if not got:
        M.log("")
        M.log("  ✘ 另一个下单进程正在运行（%s），本次退出。" % owner)
        M.log("    等它结束，或确认没有进程在跑后删掉：%s" % LOCK_PATH)
        M.log("")
        M.log("=" * 100)
        return 2

    try:
        M.log("")
        M.log("【真实下单】")
        ok, rounds, snap = execute(cfg, bnc, target_dir, expo, price,
                                   bar_date, sig_label, live=True, force=force,
                                   lock=False)   # 本函数已持有锁
        M.log("")
        M.log("【执行后账户】")
        M.log("  现货 %.8f %s（%.2f USDT）· USDT 可用 %.2f"
              % (snap["spot_qty"], bnc.base, snap["spot_val"], snap["usdt_free"]))
        M.log("  合约 %+.3f · 浮盈 %+.2f · 权益 %.2f USDT"
              % (snap["fut_amt"], snap["fut_upnl"], snap["equity"]))
        M.log("")
        M.log("  成交记录已追加到 %s" % TRADE_LOG)
        M.log("  快照已写入 %s" % TRADE_STATE)
        M.log("")
        M.log("=" * 100)
        return 0 if ok else 1
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        err = traceback.format_exc()
        M.log("运行出错：")
        M.log(err)
        try:
            with open(os.path.join(HERE, "trade_log.txt"), "a", encoding="utf-8") as f:
                f.write("\n[ERROR] %s\n%s\n" % (M.fdt(), err))
        except Exception:
            pass
        sys.exit(1)
