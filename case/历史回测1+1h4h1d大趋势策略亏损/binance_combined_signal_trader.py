# -*- coding: utf-8 -*-
"""
================================================================================
  综合版信号实盘交易机器人  (sig_combined + SL 5ATR / TP 8ATR + 2% 单笔风险)
================================================================================

策略来源：9 年回测（2018-06 ~ 2026-08，BTC/ETH/SOL）最优配置

  信号  综合版 sig_combined
        4h / 1h / 日线 三层趋势同向（EMA 排列 + DI 方向）
        + 回踩 EMA20 后重新站回（不追高）
        + ADX 环比上升且 > 30
        + 量能 > 前 96 根均值

  出场  止损 5 × ATR(15m)   止盈 8 × ATR(15m)   超时 10 天平仓
        两者都以「交易所条件单」挂出，脚本掉线也能成交

  仓位  按风险反推数量，不是固定金额：
        risk_amount = 权益 × RISK_PER_TRADE
        qty         = risk_amount / (ATR × SL_ATR)
        即：无论 ATR 大小，每笔亏损失败时亏的都是权益的固定百分比

  回测表现（单笔风险 2.0%）
        最大回撤 58.0%  年化 54.0%  MAR 0.93  9 年累计 +3379%
        胜率 42.9%  平均持仓 23.9 小时

⚠️  风险提示：58% 回撤意味着账户一度腰斩再腰斩。最长水下时间约 2 年。
    如果无法承受，请把 RISK_PER_TRADE 降到 0.005（回撤约 19%，年化约 13%）。

用法：
    # 1) 先算下单金额，不下单
    python binance_combined_signal_trader.py --calc --balance 10000

    # 2) 只看信号，不下单
    python binance_combined_signal_trader.py --scan

    # 3) 模拟盘/测试网实跑
    set BINANCE_ENV=TEST
    set BINANCE_API_KEY=xxx
    set BINANCE_API_SECRET=xxx
    python binance_combined_signal_trader.py

    # 4) 实盘
    set BINANCE_ENV=MAIN
    ...
"""

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:                                          # pragma: no cover
    Retry = None


# ============================================================================
# 1. 配置
# ============================================================================

ENV = os.getenv("BINANCE_ENV", "TEST").upper()
BASE_URL = os.getenv("BINANCE_FAPI_BASE") or {
    "TEST": "https://testnet.binancefuture.com",
    "MAIN": "https://fapi.binance.com",
}.get(ENV, "https://testnet.binancefuture.com")
# 若本机网络访问 fapi.binance.com 受限，可用环境变量指向可达的镜像：
#   set BINANCE_FAPI_BASE=https://your-mirror.example.com

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# ---- 策略参数（与回测完全一致，改动前请重新回测）----

SYMBOLS = [s.strip().upper() for s in
           os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
# 注意：BNBUSDT 在回测中期望 R 为负（-0.014），已排除

RISK_PER_TRADE = Decimal(os.getenv("RISK_PER_TRADE", "0.02"))   # 单笔风险 2%
SL_ATR = Decimal(os.getenv("SL_ATR", "5.0"))                    # 止损 5 ATR
TP_ATR = Decimal(os.getenv("TP_ATR", "8.0"))                    # 止盈 8 ATR
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "10"))           # 超时平仓
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))             # 同标的开仓冷却

# 三层趋势门槛：ADX 必须 > 30 且环比上升（回测固定值）
ADX_MIN = float(os.getenv("ADX_MIN", "30"))
VOL_LOOKBACK = int(os.getenv("VOL_LOOKBACK", "96"))
NEAR_ATR = float(os.getenv("NEAR_ATR", "1.8"))

# ---- 资金与杠杆 ----

LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "ISOLATED") or None
MARGIN_USAGE = Decimal(os.getenv("MARGIN_USAGE", "0.90"))       # 最多用 90% 可用保证金

# 组合总风险上限：3 个标的同时满仓时，累计风险 = 3 × 2% = 6%
MAX_TOTAL_RISK = Decimal(os.getenv("MAX_TOTAL_RISK", "0.06"))

# ---- 运行参数 ----

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
REQUEST_TIMEOUT = (5, 15)
RECV_WINDOW = 5000
MAX_RETRY = 4
RETRY_BACKOFF = 0.5

IGNORE_ERROR_CODES = {-4028, -4046, -4059, -4164, -2011}

STATE_FILE = os.getenv("STATE_FILE", "combined_trader_state.json")
LOG_FILE = os.getenv("LOG_FILE", "combined_trader.log")

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# 每个周期拉多少根 K 线（够算 EMA60 / ADX / 量能96 即可）
KLINE_LIMIT = {
    "5m": 300, "15m": 500, "1h": 500, "4h": 500, "1d": 300,
}


# ============================================================================
# 2. 日志
# ============================================================================

logger = logging.getLogger("CombinedTrader")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    try:
        _sh.stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logger.addHandler(_sh)
    try:
        _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        _fh.setFormatter(_fmt)
        logger.addHandler(_fh)
    except Exception:
        pass


# ============================================================================
# 3. Decimal 工具
# ============================================================================

def D(value):
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def decimal_to_str(value):
    """Decimal -> 普通十进制字符串（避免科学计数法）"""
    v = D(value)
    text = format(v, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


def floor_step(value, step):
    """按 stepSize 向下取整并量化，消除浮点尾数（否则报 -1111）"""
    value, step = D(value), D(step)
    if step <= 0:
        return value
    result = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    exp = step.normalize().as_tuple().exponent
    if exp < 0:
        return result.quantize(Decimal(1).scaleb(exp), rounding=ROUND_DOWN)
    return result.to_integral_value(rounding=ROUND_DOWN)


def ceil_step(value, step):
    value, step = D(value), D(step)
    if step <= 0:
        return value
    result = (value / step).to_integral_value(rounding=ROUND_UP) * step
    exp = step.normalize().as_tuple().exponent
    if exp < 0:
        return result.quantize(Decimal(1).scaleb(exp), rounding=ROUND_UP)
    return result.to_integral_value(rounding=ROUND_UP)


def gen_client_id(prefix):
    raw = "%s_%s" % (prefix, uuid.uuid4().hex)
    return re.sub(r"[^A-Za-z0-9_-]", "", raw)[:36]


def now_ms():
    return int(time.time() * 1000)


def ms_to_str(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


# ============================================================================
# 4. Binance 客户端（所有下单参数都来自 API 返回值）
# ============================================================================

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
        self.session = self._build_session()
        self.filters = {}
        self._load_filters()
        self._sync_time()

    # ---------------- Session ----------------

    @staticmethod
    def _build_retry_policy():
        if Retry is None:
            return None
        for kwargs in (
            {"allowed_methods": frozenset(["GET", "DELETE"]), "total": MAX_RETRY,
             "backoff_factor": RETRY_BACKOFF, "status_forcelist": [429, 500, 502, 503, 504],
             "raise_on_status": False},
            {"method_whitelist": frozenset(["GET", "DELETE"]), "total": MAX_RETRY,
             "backoff_factor": RETRY_BACKOFF, "status_forcelist": [429, 500, 502, 503, 504],
             "raise_on_status": False},
            {"total": MAX_RETRY, "backoff_factor": RETRY_BACKOFF,
             "status_forcelist": [429, 500, 502, 503, 504], "raise_on_status": False},
        ):
            try:
                return Retry(**kwargs)
            except TypeError:
                continue
        return None

    def _build_session(self):
        s = requests.Session()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "combined-signal-trader/1.0",
        }
        # 只有拿到真实密钥才带这个头；带空/占位值会被 Binance 拒绝（-2014）
        if self.api_key and self.api_key.lower() not in ("public", "none"):
            headers["X-MBX-APIKEY"] = self.api_key
        s.headers.update(headers)
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32,
                              max_retries=self._build_retry_policy())
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    # ---------------- 时间校准 ----------------

    def _sync_time(self):
        try:
            r = self.session.get(self.base_url + "/fapi/v1/time", timeout=REQUEST_TIMEOUT)
            self.time_offset = int(r.json()["serverTime"]) - now_ms()
            logger.info("时间校准完成 | offset=%sms", self.time_offset)
        except Exception as e:
            logger.warning("时间校准失败，使用本地时钟 | %s", e)
            self.time_offset = 0

    def _timestamp(self):
        return now_ms() + self.time_offset

    def _sign(self, params):
        query = urlencode(params)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    @staticmethod
    def _strip_auth(params):
        return {k: v for k, v in (params or {}).items()
                if k not in ("timestamp", "signature", "recvWindow")}

    # ---------------- 统一请求 ----------------

    def request(self, method, endpoint, params=None, signed=False, retry=0):
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._timestamp()
            params["recvWindow"] = RECV_WINDOW
            params["signature"] = self._sign(params)

        url = self.base_url + endpoint
        try:
            resp = self.session.request(method, url, params=params, timeout=REQUEST_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            # 网络异常：GET/DELETE 可安全重试；POST 交给调用方按 cid 反查，绝不盲重试
            if method.upper() in ("GET", "DELETE") and retry < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * (2 ** retry))
                logger.warning("网络异常重试 | %s %s | 第 %s 次 | %s",
                               method, endpoint, retry + 1, e)
                return self.request(method, endpoint, self._strip_auth(params),
                                    signed=signed, retry=retry + 1)
            raise

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("非 JSON 响应 | HTTP %s | %s | %s"
                               % (resp.status_code, endpoint, resp.text[:300]))

        if isinstance(data, dict) and data.get("code") is not None:
            code = data["code"]
            if code < 0 and code not in IGNORE_ERROR_CODES:
                if code == -1021 and retry < 1:
                    self._sync_time()
                    return self.request(method, endpoint, self._strip_auth(params),
                                        signed=signed, retry=retry + 1)
                if code in (-1003, -1015) and retry < MAX_RETRY:
                    time.sleep(RETRY_BACKOFF * (2 ** retry))
                    logger.warning("触发限流，退避重试 | %s | %s", code, endpoint)
                    return self.request(method, endpoint, self._strip_auth(params),
                                        signed=signed, retry=retry + 1)
                raise BinanceError(code, data.get("msg"), data)

        if not resp.ok:
            raise RuntimeError("HTTP %s | %s | %s"
                               % (resp.status_code, endpoint, resp.text[:300]))
        return data

    # ---------------- 交易规则 ----------------

    def _load_filters(self):
        self.refresh_exchange_info()

    def refresh_exchange_info(self):
        data = self.request("GET", "/fapi/v1/exchangeInfo")
        parsed = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            info = {"symbol": s["symbol"], "pricePrecision": int(s.get("pricePrecision", 8)),
                    "quantityPrecision": int(s.get("quantityPrecision", 8)),
                    "step": D("0"), "tick": D("0"), "min_qty": D("0"),
                    "market_step": D("0"), "market_min_qty": D("0"),
                    "min_notional": D("5")}
            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    info["step"] = D(f["stepSize"])
                    info["min_qty"] = D(f["minQty"])
                elif ft == "MARKET_LOT_SIZE":
                    info["market_step"] = D(f["stepSize"])
                    info["market_min_qty"] = D(f["minQty"])
                elif ft == "PRICE_FILTER":
                    info["tick"] = D(f["tickSize"])
                elif ft == "MIN_NOTIONAL":
                    info["min_notional"] = D(f.get("notional", "5"))
            if info["step"] > 0 and info["tick"] > 0:
                parsed[s["symbol"]] = info
        self.filters = parsed
        logger.info("ExchangeInfo 加载完成 | symbols=%s", len(parsed))

    def get_filters(self, symbol):
        info = self.filters.get(symbol.upper())
        if not info:
            raise RuntimeError("Binance 无此交易对或已停牌：%s" % symbol)
        return info

    def format_price(self, symbol, price):
        f = self.get_filters(symbol)
        p = D(price)
        if f["tick"] > 0:
            p = floor_step(p, f["tick"])
        return p.quantize(Decimal(1).scaleb(-f["pricePrecision"]), rounding=ROUND_DOWN)

    def format_qty(self, symbol, qty, for_market=True):
        f = self.get_filters(symbol)
        q = D(qty)
        step = f["step"] if f["step"] > 0 else Decimal(1).scaleb(-f["quantityPrecision"])
        if for_market and f["market_step"] > 0:
            step = max(step, f["market_step"])
        q = floor_step(q, step)
        return q.quantize(Decimal(1).scaleb(-f["quantityPrecision"]), rounding=ROUND_DOWN)

    # ---------------- 行情 ----------------

    def get_klines(self, symbol, interval, limit=500):
        """只返回【已收盘】的 K 线，最后一根未收盘的会被剔除"""
        raw = self.request("GET", "/fapi/v1/klines",
                           {"symbol": symbol, "interval": interval, "limit": limit})
        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
        df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")

        # ★ 关键：剔除尚未收盘的 K 线，否则会用未来数据
        cutoff = self._timestamp()
        df = df[df["close_time"] <= cutoff].reset_index(drop=True)
        return df

    def get_book_ticker(self, symbol):
        d = self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return {"bid": D(d["bidPrice"]), "ask": D(d["askPrice"])}

    def reference_price(self, symbol, side):
        """开仓基准价：BUY 看卖一，SELL 看买一"""
        bt = self.get_book_ticker(symbol)
        p = bt["ask"] if side == "BUY" else bt["bid"]
        return p, bt

    # ---------------- 账户 ----------------

    def get_balance(self, asset="USDT"):
        """账户权益（walletBalance，含未实现盈亏前）"""
        d = self.request("GET", "/fapi/v2/balance", signed=True)
        for item in d:
            if item.get("asset") == asset:
                return D(item.get("balance", "0"))
        return Decimal("0")

    def get_available_balance(self, asset="USDT"):
        d = self.request("GET", "/fapi/v2/balance", signed=True)
        for item in d:
            if item.get("asset") == asset:
                return D(item.get("availableBalance", "0"))
        return Decimal("0")

    def set_leverage(self, symbol, leverage):
        return self.request("POST", "/fapi/v1/leverage",
                            {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol, margin_type):
        if not margin_type:
            return None
        try:
            return self.request("POST", "/fapi/v1/marginType",
                                {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code in (-4046, -4047):
                return None
            raise

    def get_position(self, symbol):
        d = self.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for p in d:
            amt = D(p.get("positionAmt", "0"))
            if amt != 0:
                return {"symbol": symbol, "amt": amt,
                        "entry_price": D(p.get("entryPrice", "0")),
                        "mark_price": D(p.get("markPrice", "0")),
                        "unrealized": D(p.get("unRealizedProfit", "0")),
                        "leverage": int(p.get("leverage", 0)),
                        "side": "LONG" if amt > 0 else "SHORT"}
        return None

    # ---------------- 下单 ----------------

    def submit_order(self, symbol, payload):
        payload = dict(payload)
        payload.setdefault("newClientOrderId", gen_client_id("ord"))
        payload["symbol"] = symbol
        return self.request("POST", "/fapi/v1/order", payload, signed=True)

    def market_open(self, symbol, side, quantity):
        """
        市价开仓。超时后按 newClientOrderId 反查，避免重复下单。
        """
        cid = gen_client_id("open")
        payload = {"side": side, "type": "MARKET",
                   "quantity": decimal_to_str(quantity),
                   "newClientOrderId": cid,
                   "newOrderRespType": "RESULT"}
        try:
            return self.submit_order(symbol, payload)
        except (requests.Timeout, requests.ConnectionError, RuntimeError) as e:
            logger.error("开仓请求异常，按 cid 反查 | %s | %s", cid, e)
            time.sleep(1.5)
            found = self.find_order_by_cid(symbol, cid)
            if found:
                logger.warning("订单实际已提交 | %s", found.get("orderId"))
                return found
            raise

    def market_close(self, symbol, position_side, quantity):
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.submit_order(symbol, {
            "side": side, "type": "MARKET",
            "quantity": decimal_to_str(quantity),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT"})

    def find_order_by_cid(self, symbol, cid):
        try:
            return self.request("GET", "/fapi/v1/order",
                                {"symbol": symbol, "origClientOrderId": cid}, signed=True)
        except Exception:
            return None

    def get_fill_price(self, symbol, order_id, fallback_qty=None):
        """
        成交均价三级兜底：
          1) /fapi/v1/order 的 avgPrice
          2) /fapi/v1/userTrades 按数量加权的 VWAP
          3) positionRisk.entryPrice
        （MARKET + RESULT 的 avgPrice 经常返回 0，必须有兜底）
        """
        try:
            o = self.request("GET", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)
            ap = D(o.get("avgPrice", "0"))
            if ap > 0:
                return ap
        except Exception as e:
            logger.warning("查订单失败 | %s", e)

        try:
            trades = self.request("GET", "/fapi/v1/userTrades",
                                  {"symbol": symbol, "limit": 20}, signed=True)
            if trades:
                target = [t for t in trades if str(t.get("orderId")) == str(order_id)]
                if target:
                    qty_sum = sum(D(t["qty"]) for t in target)
                    if qty_sum > 0:
                        vwap = sum(D(t["price"]) * D(t["qty"]) for t in target) / qty_sum
                        return vwap
                if fallback_qty:
                    recent = trades[-5:]
                    qty_sum = sum(D(t["qty"]) for t in recent)
                    if qty_sum > 0:
                        return sum(D(t["price"]) * D(t["qty"]) for t in recent) / qty_sum
        except Exception as e:
            logger.warning("查成交明细失败 | %s", e)

        pos = self.get_position(symbol)
        if pos and pos["entry_price"] > 0:
            logger.warning("使用 positionRisk.entryPrice 兜底 | %s", symbol)
            return pos["entry_price"]

        raise RuntimeError("无法获取成交价 | %s | orderId=%s" % (symbol, order_id))

    # ---------------- 条件单（交易所侧止盈止损）----------------

    def get_open_algo_orders(self, symbol):
        try:
            return self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        except Exception:
            return []

    def cancel_algo_order(self, symbol, order_id):
        return self.request("DELETE", "/fapi/v1/order",
                            {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_all_orders(self, symbol):
        try:
            return self.request("DELETE", "/fapi/v1/allOpenOrders",
                                {"symbol": symbol}, signed=True)
        except Exception as e:
            logger.warning("撤单失败 | %s | %s", symbol, e)
            return None

    def place_stop_loss(self, symbol, position_side, trigger_price):
        """止损：STOP_MARKET + closePosition，触发即市价全平，脚本掉线也生效"""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.submit_order(symbol, {
            "side": side, "type": "STOP_MARKET",
            "stopPrice": decimal_to_str(trigger_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newClientOrderId": gen_client_id("sl")})

    def place_take_profit(self, symbol, position_side, trigger_price):
        """止盈：TAKE_PROFIT_MARKET + closePosition"""
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.submit_order(symbol, {
            "side": side, "type": "TAKE_PROFIT_MARKET",
            "stopPrice": decimal_to_str(trigger_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newClientOrderId": gen_client_id("tp")})


# ============================================================================
# 5. 指标（与回测 bt_engine.py 逐行一致，改这里必须同步改回测）
# ============================================================================

def ind_atr(high, low, close, period=14):
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().bfill().round(6)


def ind_adx(high, low, close, period=14):
    up, dn = high.diff(), -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / a
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / a
    dx = ((pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)) * 100
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(20)


def ind_di(high, low, close, period=14):
    """返回 (pdi, mdi)，用于判断趋势方向"""
    up, dn = high.diff(), -low.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    a = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    pdi = (100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / a).fillna(0)
    mdi = (100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / a).fillna(0)
    return pdi, mdi


def add_indicators(df):
    df = df.copy()
    h, l, c = df["high"], df["low"], df["close"]
    df["ATR"] = ind_atr(h, l, c, 14)
    df["ADX"] = ind_adx(h, l, c, 14)
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema60"] = c.ewm(span=60, adjust=False).mean()
    df["pdi"], df["mdi"] = ind_di(h, l, c, 14)
    return df


def add_daily_pivot(df):
    df = df.copy()
    pp = (df["high"] + df["low"] + df["close"]) / 3
    rng = df["high"] - df["low"]
    df["PP"], df["R1"], df["R2"] = pp, 2 * pp - df["low"], pp + rng
    df["S1"], df["S2"] = 2 * pp - df["high"], pp - rng
    return df


def align_higher(higher, lower_close_time, cols, prefix):
    """取 higher 中 close_time <= lower 的最后一根（防未来函数）"""
    hct = higher["close_time"].values
    idx = np.searchsorted(hct, lower_close_time, side="right") - 1
    idx = np.where(idx >= 0, idx, 0)
    return {prefix + c: higher[c].values[idx] for c in cols}


def _roll_prev(a):
    b = np.roll(a, 1)
    b[0] = a[0]
    return b


# ============================================================================
# 6. 综合版信号（与回测 strategies.py::sig_combined 完全一致）
# ============================================================================

def build_signal_frame(d15, d1h, d4h, d1d):
    """把各周期对齐到 15m 上，返回带全部列的 DataFrame"""
    sig = d15.reset_index(drop=True).copy()
    ct = sig["close_time"].values

    h1 = add_indicators(d1h)
    h4 = add_indicators(d4h)
    dd = add_indicators(add_daily_pivot(d1d))

    for name, src in (("1h", h1), ("4h", h4)):
        for k, v in align_higher(src, ct, ["ema20", "ema60", "pdi", "mdi"],
                                 name + "_").items():
            sig[k] = v

    for k, v in align_higher(dd, ct, ["close", "ema20"], "1d_").items():
        sig[k] = v
    return sig


def sig_combined(d):
    """
    综合版信号：三层趋势 + 回踩站回 + ADX 上升 + 量能确认
    返回 int8 数组：1 做多 / -1 做空 / 0 无信号
    """
    c, o = d["close"].values, d["open"].values
    h, l = d["high"].values, d["low"].values
    e20, e60 = d["ema20"].values, d["ema60"].values
    e20_4, e60_4 = d["4h_ema20"].values, d["4h_ema60"].values
    e20_1, e60_1 = d["1h_ema20"].values, d["1h_ema60"].values
    atr = d["ATR"].values
    adx, adx_p = d["ADX"].values, _roll_prev(d["ADX"].values)
    pdi, mdi = d["pdi"].values, d["mdi"].values
    pdi4, mdi4 = d["4h_pdi"].values, d["4h_mdi"].values
    pdi1, mdi1 = d["1h_pdi"].values, d["1h_mdi"].values

    d1c = d["1d_close"].values if "1d_close" in d else None
    d1e = d["1d_ema20"].values if "1d_ema20" in d else None

    up = ((e20_4 > e60_4) & (pdi4 > mdi4) & (e20_1 > e60_1) & (pdi1 > mdi1)
          & (e20 > e60) & (pdi > mdi))
    dn = ((e20_4 < e60_4) & (pdi4 < mdi4) & (e20_1 < e60_1) & (pdi1 < mdi1)
          & (e20 < e60) & (pdi < mdi))

    if d1c is not None:
        up = up & (d1c > d1e)
        dn = dn & (d1c < d1e)

    near = np.abs(c - e20) < atr * NEAR_ATR
    touch_dn = (l <= e20 * 1.003) | (_roll_prev(l) <= _roll_prev(e20) * 1.003)
    touch_up = (h >= e20 * 0.997) | (_roll_prev(h) >= _roll_prev(e20) * 0.997)

    adx_up = adx > adx_p
    strong = adx > ADX_MIN

    vol_ok = True
    if "volume" in d:
        v = d["volume"].values
        vol_ok = v > np.nan_to_num(
            pd.Series(v).rolling(VOL_LOOKBACK).mean().bfill().values, nan=0)

    long_ok = up & near & touch_dn & (c > e20) & (c > o) & adx_up & strong & vol_ok
    short_ok = dn & near & touch_up & (c < e20) & (c < o) & adx_up & strong & vol_ok
    return np.where(long_ok, 1, np.where(short_ok, -1, 0)).astype(np.int8)


def get_signal(client, symbol):
    """
    返回最新一根【已收盘】15m bar 的信号。
    dict(signal, bar_close_ms, close, atr, adx, reason)
    """
    d15 = client.get_klines(symbol, "15m", KLINE_LIMIT["15m"])
    if len(d15) < 120:
        return {"signal": 0, "reason": "K线不足", "atr": None}

    d1h = client.get_klines(symbol, "1h", KLINE_LIMIT["1h"])
    d4h = client.get_klines(symbol, "4h", KLINE_LIMIT["4h"])
    d1d = client.get_klines(symbol, "1d", KLINE_LIMIT["1d"])
    if len(d1h) < 70 or len(d4h) < 70 or len(d1d) < 25:
        return {"signal": 0, "reason": "高周期K线不足", "atr": None}

    frame = build_signal_frame(add_indicators(d15), d1h, d4h, d1d)
    sig = sig_combined(frame)

    i = len(frame) - 1
    row = frame.iloc[i]
    return {
        "signal": int(sig[i]),
        "bar_close_ms": int(row["close_time"]),
        "bar_open_ms": int(row["open_time"]),
        "close": float(row["close"]),
        "atr": float(row["ATR"]),
        "adx": float(row["ADX"]),
        "ema20": float(row["ema20"]),
        "reason": "ok",
    }


# ============================================================================
# 7. 仓位计算（按风险反推数量）
# ============================================================================

def calc_position(client, symbol, side, entry_price, atr, equity,
                  risk_pct=RISK_PER_TRADE, leverage=LEVERAGE):
    """
    核心公式：
        risk_amount = 权益 × risk_pct
        sl_distance = ATR × SL_ATR
        qty         = risk_amount / sl_distance

    这样无论 ATR 大小，单笔止损亏的都是权益的固定百分比。
    返回 dict(qty, notional, margin, sl_price, tp_price, sl_pct, leverage_needed, ok, msg)
    """
    f = client.get_filters(symbol)
    entry_price = D(entry_price)
    atr = D(atr)

    sl_distance = atr * SL_ATR
    tp_distance = atr * TP_ATR

    if sl_distance <= 0:
        return {"ok": False, "msg": "ATR 异常"}
    if entry_price <= 0:
        return {"ok": False, "msg": "价格异常"}

    if side == "BUY":
        sl_price_raw = entry_price - sl_distance
        tp_price_raw = entry_price + tp_distance
    else:
        sl_price_raw = entry_price + sl_distance
        tp_price_raw = entry_price - tp_distance

    if sl_price_raw <= 0:
        return {"ok": False, "msg": "止损价计算为负，ATR 过大"}

    risk_amount = equity * risk_pct
    qty_raw = risk_amount / sl_distance

    # ---- 保证金约束 ----
    available = client.get_available_balance("USDT")
    max_notional = available * MARGIN_USAGE * leverage
    notional_raw = qty_raw * entry_price
    clamped = False
    if notional_raw > max_notional:
        qty_raw = max_notional / entry_price
        notional_raw = max_notional
        clamped = True

    qty = client.format_qty(symbol, qty_raw, for_market=True)
    notional = qty * entry_price
    margin = notional / leverage

    sl_price = client.format_price(symbol, sl_price_raw)
    tp_price = client.format_price(symbol, tp_price_raw)
    sl_pct = abs(entry_price - sl_price) / entry_price
    actual_risk = qty * abs(entry_price - sl_price)

    ok = True
    msg = "ok"
    if qty < f["min_qty"]:
        ok, msg = False, "数量低于 minQty(%s)" % f["min_qty"]
    elif notional < f["min_notional"]:
        ok, msg = False, "名义价值 %s < minNotional %s" % (notional, f["min_notional"])

    return {
        "ok": ok, "msg": msg,
        "qty": qty, "notional": notional, "margin": margin,
        "sl_price": sl_price, "tp_price": tp_price,
        "sl_pct": sl_pct, "sl_distance": sl_distance,
        "actual_risk": actual_risk,
        "risk_pct_actual": (actual_risk / equity) if equity > 0 else Decimal("0"),
        "rr": float(tp_distance / sl_distance),
        "leverage_needed": float(notional / equity) if equity > 0 else 0.0,
        "clamped": clamped,
    }


def print_position_plan(symbol, side, price, atr, plan):
    """把下单金额打印成人能看懂的样子"""
    if not plan.get("ok"):
        logger.warning("  %-10s %-5s 不可下单 | %s", symbol, side, plan.get("msg"))
        return
    logger.info(
        "  %-10s %-5s 价格 %-12s ATR %-10s | 数量 %-12s 名义 %-10s 保证金 %-10s "
        "止损 %-12s(%+.2f%%) 止盈 %-12s 风险 %s (%.2f%%) 实际杠杆 %.2fx",
        symbol, side, price, atr, plan["qty"], plan["notional"], plan["margin"],
        plan["sl_price"], -float(plan["sl_pct"]) * 100, plan["tp_price"],
        plan["actual_risk"], float(plan["risk_pct_actual"]) * 100,
        plan["leverage_needed"])


# ============================================================================
# 8. 状态管理
# ============================================================================

class State(object):

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.data = {"positions": {}, "last_entry_ms": {}, "closed": []}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            logger.info("状态已载入 | 持仓 %s 个", len(self.data.get("positions", {})))
        except Exception as e:
            logger.warning("状态载入失败 | %s", e)

    def save(self):
        """原子写：先写 tmp 再 replace，避免断电写坏文件"""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error("状态保存失败 | %s", e)

    # ---- 持仓 ----

    def get_position(self, symbol):
        return self.data["positions"].get(symbol)

    def set_position(self, symbol, info):
        self.data["positions"][symbol] = info
        self.save()

    def clear_position(self, symbol, record=None):
        self.data["positions"].pop(symbol, None)
        if record:
            self.data.setdefault("closed", []).append(record)
            self.data["closed"] = self.data["closed"][-200:]
        self.save()

    def all_positions(self):
        return dict(self.data["positions"])

    # ---- 冷却 ----

    def last_entry_ms(self, symbol):
        return int(self.data.get("last_entry_ms", {}).get(symbol, 0))

    def mark_entry(self, symbol, ts_ms):
        self.data.setdefault("last_entry_ms", {})[symbol] = int(ts_ms)
        self.save()

    def in_cooldown(self, symbol, now):
        last = self.last_entry_ms(symbol)
        return last > 0 and (now - last) < COOLDOWN_MIN * 60 * 1000

    # ---- 组合风险 ----

    def total_open_risk(self):
        return sum(D(p.get("risk_pct", 0)) for p in self.data["positions"].values())


# ============================================================================
# 9. 交易执行
# ============================================================================

class Trader(object):

    def __init__(self, client):
        self.c = client
        self.state = State()

    # ---------------- 开仓 ----------------

    def open_position(self, symbol, side, signal_info, equity):
        atr = signal_info["atr"]
        bar_close = signal_info["bar_close_ms"]

        # 冷却检查（与回测 COOLDOWN_MS=30min 一致）
        if self.state.in_cooldown(symbol, bar_close):
            logger.info("%s 处于冷却期，跳过", symbol)
            return False

        # 组合总风险上限
        if self.state.total_open_risk() + RISK_PER_TRADE > MAX_TOTAL_RISK:
            logger.warning("%s 组合总风险已达上限 %s，跳过", symbol, MAX_TOTAL_RISK)
            return False

        api_side = "BUY" if side > 0 else "SELL"
        pos_side = "LONG" if side > 0 else "SHORT"

        price, bt = self.c.reference_price(symbol, api_side)
        plan = calc_position(self.c, symbol, api_side, price, atr, equity)
        if not plan["ok"]:
            logger.warning("%s 仓位计算不可行 | %s", symbol, plan["msg"])
            return False

        logger.info("【开仓信号】%s %s | 信号bar收盘 %s | ADX %.1f",
                    symbol, pos_side, ms_to_str(bar_close), signal_info["adx"])
        print_position_plan(symbol, api_side, price, atr, plan)

        if DRY_RUN:
            logger.info("DRY_RUN 模式，跳过实际下单")
            return False

        # 1) 设置杠杆 / 保证金模式
        try:
            self.c.set_margin_type(symbol, MARGIN_TYPE)
            self.c.set_leverage(symbol, LEVERAGE)
        except BinanceError as e:
            logger.warning("杠杆/保证金设置异常 | %s", e)

        # 2) 市价开仓
        order = self.c.market_open(symbol, api_side, plan["qty"])
        order_id = order.get("orderId")
        fill = self.c.get_fill_price(symbol, order_id, plan["qty"])
        logger.info("【开仓成交】%s %s | 委托价 %s | 实际成交价 %s",
                    symbol, pos_side, price, fill)

        # 3) 按【实际成交价】重算止损止盈（关键：用 API 返回的成交价，不用委托价）
        sl_distance = D(atr) * SL_ATR
        tp_distance = D(atr) * TP_ATR
        if side > 0:
            sl_raw, tp_raw = fill - sl_distance, fill + tp_distance
        else:
            sl_raw, tp_raw = fill + sl_distance, fill - tp_distance
        sl_price = self.c.format_price(symbol, sl_raw)
        tp_price = self.c.format_price(symbol, tp_raw)

        # 4) 挂交易所条件单
        sl_order = tp_order = None
        try:
            sl_order = self.c.place_stop_loss(symbol, pos_side, sl_price)
            logger.info("【止损已挂】%s | 触发价 %s | id=%s",
                        symbol, sl_price, sl_order.get("orderId"))
        except BinanceError as e:
            logger.error("止损挂单失败 | %s | %s", symbol, e)

        try:
            tp_order = self.c.place_take_profit(symbol, pos_side, tp_price)
            logger.info("【止盈已挂】%s | 触发价 %s | id=%s",
                        symbol, tp_price, tp_order.get("orderId"))
        except BinanceError as e:
            logger.error("止盈挂单失败 | %s | %s", symbol, e)

        if sl_order is None:
            logger.critical("⚠️ %s 止损未挂上！立即市价平仓以避免裸奔", symbol)
            try:
                self.c.market_close(symbol, pos_side, plan["qty"])
            except Exception as e2:
                logger.critical("平仓也失败，请手动处理 | %s", e2)
            return False

        # 5) 记录状态
        actual_risk = D(plan["qty"]) * abs(fill - sl_price)
        self.state.set_position(symbol, {
            "symbol": symbol, "side": pos_side, "qty": str(plan["qty"]),
            "entry_price": str(fill), "atr": str(atr),
            "sl_price": str(sl_price), "tp_price": str(tp_price),
            "sl_order_id": sl_order.get("orderId"),
            "tp_order_id": tp_order.get("orderId") if tp_order else None,
            "entry_time_ms": now_ms(),
            "bar_close_ms": bar_close,
            "risk_pct": str(actual_risk / equity) if equity > 0 else "0",
        })
        self.state.mark_entry(symbol, bar_close)
        return True

    # ---------------- 持仓管理 ----------------

    def manage_position(self, symbol):
        rec = self.state.get_position(symbol)
        if not rec:
            return

        pos = self.c.get_position(symbol)

        # 仓位已被条件单平掉（止盈或止损成交）
        if pos is None:
            logger.info("【仓位已平】%s | 由交易所条件单触发（止盈/止损）", symbol)
            self.c.cancel_all_orders(symbol)   # 清掉另一条没触发的条件单
            self.state.clear_position(symbol, {
                "symbol": symbol, "side": rec["side"], "entry": rec["entry_price"],
                "exit": "exchange_tpsl", "time": now_ms()})
            return

        # 超时平仓（与回测 MAX_HOLD 一致）
        hold_ms = now_ms() - int(rec["entry_time_ms"])
        if hold_ms > MAX_HOLD_DAYS * 86400 * 1000:
            logger.info("【超时平仓】%s | 持仓 %.1f 天 > %s 天",
                        symbol, hold_ms / 86400000.0, MAX_HOLD_DAYS)
            try:
                self.c.cancel_all_orders(symbol)
                self.c.market_close(symbol, rec["side"], D(rec["qty"]))
                self.state.clear_position(symbol, {
                    "symbol": symbol, "side": rec["side"], "entry": rec["entry_price"],
                    "exit": "timeout", "time": now_ms()})
            except Exception as e:
                logger.error("超时平仓失败 | %s | %s", symbol, e)
            return

        # 条件单掉线检查（脚本重启后可能丢单）
        orders = self.c.get_open_algo_orders(symbol)
        types = {o.get("type") for o in orders}
        if "STOP_MARKET" not in types:
            logger.warning("⚠️ %s 止损单缺失，重新补挂 %s", symbol, rec["sl_price"])
            try:
                self.c.place_stop_loss(symbol, rec["side"], D(rec["sl_price"]))
            except Exception as e:
                logger.error("补挂止损失败 | %s", e)
        if "TAKE_PROFIT_MARKET" not in types:
            logger.warning("⚠️ %s 止盈单缺失，重新补挂 %s", symbol, rec["tp_price"])
            try:
                self.c.place_take_profit(symbol, rec["side"], D(rec["tp_price"]))
            except Exception as e:
                logger.error("补挂止盈失败 | %s", e)

    # ---------------- 主扫描 ----------------

    def scan_once(self):
        try:
            equity = self.c.get_balance("USDT")
        except Exception as e:
            logger.error("获取账户权益失败 | %s", e)
            return
        if equity <= 0:
            logger.error("账户权益为 0，停止")
            return

        logger.info("=" * 100)
        logger.info("账户权益 %.2f USDT | 持仓 %s 个 | 单笔风险 %.2f%% (=%.2f USDT)",
                    equity, len(self.state.all_positions()),
                    float(RISK_PER_TRADE) * 100, float(equity * RISK_PER_TRADE))

        for symbol in SYMBOLS:
            try:
                # 1) 先管理已有持仓
                self.manage_position(symbol)

                # 2) 有持仓就不再开新仓（单标的单仓）
                if self.state.get_position(symbol):
                    rec = self.state.get_position(symbol)
                    pos = self.c.get_position(symbol)
                    if pos:
                        logger.info("  %-10s 持仓中 | %s %.4f | 浮盈 %.2f USDT | 止损 %s 止盈 %s",
                                    symbol, rec["side"], float(rec["entry_price"]),
                                    float(pos["unrealized"]), rec["sl_price"], rec["tp_price"])
                    continue

                # 3) 无持仓则看信号
                info = get_signal(self.c, symbol)
                if info["signal"] == 0:
                    logger.info("  %-10s 无信号 | ADX %.1f | %s",
                                symbol, info.get("adx") or 0, info.get("reason"))
                    continue

                side_name = "做多" if info["signal"] > 0 else "做空"
                logger.info("  %-10s ★ 信号 %s | ADX %.1f | ATR %s",
                            symbol, side_name, info["adx"], info["atr"])
                self.open_position(symbol, info["signal"], info, equity)

            except BinanceError as e:
                logger.error("  %s Binance 错误 | %s", symbol, e)
            except Exception as e:
                logger.exception("  %s 处理异常 | %s", symbol, e)

    def run(self):
        logger.info("=" * 100)
        logger.info("综合版信号交易机器人启动 | 环境 %s | 标的 %s",
                    ENV, ",".join(SYMBOLS))
        logger.info("信号 15m 综合版 | 止损 %s ATR | 止盈 %s ATR | 单笔风险 %.2f%% | 杠杆 %sx",
                    SL_ATR, TP_ATR, float(RISK_PER_TRADE) * 100, LEVERAGE)
        logger.info("冷却 %s 分钟 | 超时 %s 天 | DRY_RUN=%s", COOLDOWN_MIN, MAX_HOLD_DAYS, DRY_RUN)
        logger.info("=" * 100)

        # 强制单向持仓模式（positionSide=BOTH 的前提）
        try:
            self.c.request("POST", "/fapi/v1/positionSide/dual",
                           {"dualSidePosition": "false"}, signed=True)
        except BinanceError as e:
            if e.code != -4059:
                logger.warning("设置单向持仓失败 | %s", e)

        while True:
            try:
                self.scan_once()
            except KeyboardInterrupt:
                logger.info("收到中断，退出")
                break
            except Exception as e:
                logger.exception("主循环异常 | %s", e)
            time.sleep(SCAN_INTERVAL_SEC)


# ============================================================================
# 10. CLI
# ============================================================================

def cmd_calc(client, balance):
    """只算下单金额，不下单 —— 回答「执行策略下单金额」"""
    equity = D(balance)
    print("\n" + "=" * 118)
    print("  下单金额测算  |  权益 %.2f USDT  |  单笔风险 %.2f%%  |  止损 %s ATR / 止盈 %s ATR"
          % (equity, float(RISK_PER_TRADE) * 100, SL_ATR, TP_ATR))
    print("=" * 118)
    print("  风险金额 = %.2f × %.2f%% = %.2f USDT"
          % (equity, float(RISK_PER_TRADE) * 100, float(equity * RISK_PER_TRADE)))
    print("=" * 118)
    print("  %-10s %-11s %-9s %-9s %-13s %-13s %-11s %-11s %-9s"
          % ("标的", "价格", "ATR", "止损距离", "下单数量", "名义价值",
             "占用保证金", "止损价", "止盈价"))
    print("-" * 118)

    for symbol in SYMBOLS:
        try:
            d15 = client.get_klines(symbol, "15m", 200)
            df = add_indicators(d15)
            price = D(df["close"].iloc[-1])
            atr = D(df["ATR"].iloc[-1])
            plan = calc_position(client, symbol, "BUY", price, atr, equity)
            if not plan["ok"]:
                print("  %-10s %s" % (symbol, plan["msg"]))
                continue
            print("  %-10s %-11s %-9.4f %-9.4f %-13s %-13.2f %-11.2f %-11s %-9s"
                  % (symbol, price, atr, plan["sl_distance"], plan["qty"],
                     float(plan["notional"]), float(plan["margin"]),
                     plan["sl_price"], plan["tp_price"]))
        except Exception as e:
            print("  %-10s 计算失败 | %s" % (symbol, e))

    print("-" * 118)
    print("  说明：")
    print("    1. 止损距离 = ATR × %s，是价格波动的实际度量，不同币种差异很大" % SL_ATR)
    print("    2. 下单数量 = 风险金额 ÷ 止损距离，所以每笔止损亏损都锁定在 %.2f USDT"
          % float(equity * RISK_PER_TRADE))
    print("    3. 名义价值可能超过权益，这是正常的（杠杆 %.1fx 下保证金只占名义价值的 1/%s）"
          % (LEVERAGE, LEVERAGE))
    print("    4. 三个标的可能同时持仓，累计风险 = 3 × %.2f%% = %.2f%%"
          % (float(RISK_PER_TRADE) * 100, float(RISK_PER_TRADE) * 300))
    print("    5. 若某标的『数量低于 minQty』，说明权益太小，需要加大本金或提高杠杆")
    print("=" * 118 + "\n")


def cmd_scan(client):
    """只看信号，不下单"""
    print("\n" + "=" * 100)
    print("  信号扫描  |  %s" % ms_to_str(client._timestamp()))
    print("=" * 100)
    print("  %-10s %-8s %-10s %-10s %-14s %-10s"
          % ("标的", "信号", "ADX", "ATR", "信号bar收盘", "是否冷却"))
    print("-" * 100)

    st = State()
    for symbol in SYMBOLS:
        try:
            info = get_signal(client, symbol)
            name = {1: "★做多", -1: "★做空", 0: "-"}[info["signal"]]
            cd = "冷却中" if st.in_cooldown(symbol, info.get("bar_close_ms") or 0) else "-"
            print("  %-10s %-8s %-10.1f %-10.4f %-14s %-10s"
                  % (symbol, name, info.get("adx") or 0, info.get("atr") or 0,
                     ms_to_str(info["bar_close_ms"]) if info.get("bar_close_ms") else "-", cd))
        except Exception as e:
            print("  %-10s 失败 | %s" % (symbol, e))
    print("=" * 100 + "\n")


def main():
    ap = argparse.ArgumentParser(description="综合版信号交易机器人")
    ap.add_argument("--calc", action="store_true", help="只测算下单金额，不下单")
    ap.add_argument("--balance", type=float, default=0, help="--calc 用的权益，默认读账户")
    ap.add_argument("--scan", action="store_true", help="只扫描信号，不下单")
    ap.add_argument("--dry-run", action="store_true", help="跑完整流程但不下单")
    args = ap.parse_args()

    global DRY_RUN
    if args.dry_run:
        DRY_RUN = True

    if not API_KEY or not API_SECRET:
        # --calc / --scan 只需要公开行情，不需要签名
        if args.calc or args.scan:
            globals()["API_KEY"] = ""
            globals()["API_SECRET"] = ""
        else:
            raise RuntimeError("未设置 BINANCE_API_KEY / BINANCE_API_SECRET 环境变量")

    client = BinanceClient()

    if args.calc:
        balance = args.balance if args.balance > 0 else 10000.0
        cmd_calc(client, balance)
        return
    if args.scan:
        cmd_scan(client)
        return

    Trader(client).run()


if __name__ == "__main__":
    main()
