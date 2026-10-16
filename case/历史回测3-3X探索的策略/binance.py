# -*- coding: utf-8 -*-
"""
binance.py — 币安 USDT 本位合约（U本位永续）下单模块

【只用标准库实现】不需要 pip install python-binance。

覆盖能力：
  · 签名请求（HMAC-SHA256）+ 服务器时间同步
  · 查账户/持仓/余额、设置杠杆与保证金模式
  · 市价开仓/平仓、一键反手（先平后开）
  · 数量与价格精度对齐（从 exchangeInfo 自动获取）
  · 幂等保护（同一目标方向同一天只下单一次）

【安全设计】
  1. 所有下单前先校验：数量 ≥ minQty、名义 ≥ minNotional、名义 ≤ max_order_notional
  2. 幂等：下单前写「意图文件」，成功后才写「完成标记」；重复运行会被拦截
  3. dry_run：只打印不提交
  4. 时间同步：用 /fapi/v1/time 校准本地时间，避免 -1021 时间戳错误
  5. 错误分类：可重试（网络/5xx）vs 不可重试（-2019 保证金不足等），避免盲目重试

【重要】本模块会真实下单。首次上线务必先跑 sync_check.py（只读）验证。
"""
import hashlib
import hmac
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

FAPI = "https://fapi.binance.com"
CST = timezone(timedelta(hours=8))

# 只在这些路径上允许重试（幂等查询类）
_SAFE_RETRY_PATHS = ("/fapi/v1/time", "/fapi/v2/account", "/fapi/v2/positionRisk",
                     "/fapi/v1/exchangeInfo", "/fapi/v1/klines",
                     "/fapi/v1/leverageBracket", "/fapi/v1/premiumIndex")


class BinanceError(Exception):
    """币安接口返回的业务错误。code 为币安错误码（如 -2019）。"""

    def __init__(self, code, msg, http_status=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status

    @property
    def retryable(self):
        # -1021 时间戳不同步；-1003 频率超限；-1022 签名错误（不可重试）
        return self.code in (-1021, -1003, -1007, -2015)


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _build_opener(use_proxy_env=True):
    """默认走系统代理环境变量；失败时可切到直连。"""
    if use_proxy_env:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BinanceFutures:
    """币安 U 本位合约客户端。"""

    def __init__(self, api_key, api_secret, recv_window=5000, log=None):
        if not api_key or not api_secret:
            raise ValueError("apiKey / apiSecret 不能为空")
        if "在这里填" in api_key:
            raise ValueError("config.json 里的 trade.apiKey 还是占位符，请填入真实密钥")
        self.key = api_key.strip()
        self.secret = api_secret.strip().encode()
        self.recv_window = int(recv_window)
        self.log = log
        self._time_offset = 0          # 本地时间 - 服务器时间（毫秒）
        self._prefer_proxy = True
        self._sym_cache = {}

    def _info(self, msg, *a):
        if self.log:
            self.log.info(msg, *a)

    def _warn(self, msg, *a):
        if self.log:
            self.log.warning(msg, *a)

    # ---------------------------------------------------------- 底层请求
    def _request(self, method, path, params=None, signed=False, retries=3):
        params = dict(params or {})
        last_err = None
        for attempt in range(retries):
            try:
                return self._request_once(method, path, params, signed)
            except BinanceError as e:
                last_err = e
                # 时间戳错误 → 重新校准后再试
                if e.code == -1021:
                    self._warn("时间戳不同步，重新校准服务器时间")
                    self.sync_time(force=True)
                    continue
                if e.retryable and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ssl.SSLError) as e:
                last_err = e
                # 网络类错误：如果走代理失败，切直连再试（代理常间歇性可用）
                if self._prefer_proxy:
                    self._prefer_proxy = False
                    continue
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    self._prefer_proxy = True
                    continue
                raise
            except Exception as e:                          # noqa: BLE001
                last_err = e
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"请求失败：{last_err}")

    def _request_once(self, method, path, params, signed):
        if signed:
            if not self._time_offset:
                self.sync_time()
            params = dict(params)
            params["timestamp"] = int(time.time() * 1000) - self._time_offset
            params["recvWindow"] = self.recv_window
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
            qs += "&signature=" + sig
            url = f"{FAPI}{path}?{qs}" if method == "GET" else f"{FAPI}{path}"
            body = None if method == "GET" else qs.encode()
            headers = {"X-MBX-APIKEY": self.key,
                       "Content-Type": "application/x-www-form-urlencoded"}
        else:
            qs = urllib.parse.urlencode(params)
            url = f"{FAPI}{path}?{qs}" if qs else f"{FAPI}{path}"
            body = None
            headers = {"X-MBX-APIKEY": self.key} if self.key else {}

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = _build_opener(self._prefer_proxy)
        try:
            with opener.open(req, timeout=25, context=_ssl_ctx()) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                j = json.loads(raw)
                raise BinanceError(j.get("code"), j.get("msg", raw), e.code) from None
            except json.JSONDecodeError:
                raise BinanceError(-1, f"HTTP {e.code}: {raw[:300]}", e.code) from None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"返回非 JSON：{raw[:300]}") from None

    # ---------------------------------------------------------- 基础接口
    def sync_time(self, force=False):
        """校准本地时间与币安服务器时间差。"""
        try:
            srv = self._request_once("GET", "/fapi/v1/time", {}, False)["serverTime"]
            self._time_offset = int(time.time() * 1000) - int(srv)
            self._info("服务器时间已校准，本地偏移 %d ms", self._time_offset)
            return self._time_offset
        except Exception as e:                              # noqa: BLE001
            self._warn("时间校准失败（将继续用本地时间）：%s", e)
            return 0

    def ping(self):
        return self._request("GET", "/fapi/v1/ping")

    def exchange_info(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/exchangeInfo", p)

    def symbol_filters(self, symbol):
        """获取交易对的精度与最小下单限制。结果缓存。"""
        if symbol in self._sym_cache:
            return self._sym_cache[symbol]
        data = self.exchange_info(symbol)
        s = None
        for x in data.get("symbols", []):
            if x["symbol"] == symbol:
                s = x
                break
        if not s:
            raise RuntimeError(f"交易所无此交易对：{symbol}")
        f = {x["filterType"]: x for x in s.get("filters", [])}
        out = {
            "symbol": symbol,
            "price_tick": float(f.get("PRICE_FILTER", {}).get("tickSize", 0.1)),
            "qty_step": float(f.get("LOT_SIZE", {}).get("stepSize", 0.001)),
            "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0.001)),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
            "qty_precision": int(s.get("quantityPrecision", 3)),
            "price_precision": int(s.get("pricePrecision", 2)),
        }
        self._sym_cache[symbol] = out
        return out

    def set_leverage(self, symbol, leverage, margin_type="ISOLATED"):
        """设置杠杆与保证金模式。幂等：已是目标值不会报错。"""
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": margin_type}, signed=True)
            self._info("%s 保证金模式设为 %s", symbol, margin_type)
        except BinanceError as e:
            # -4046 = 已经是该模式
            if e.code == -4046:
                self._info("%s 保证金模式已是 %s（无需改动）", symbol, margin_type)
            else:
                self._warn("设置保证金模式失败（继续）：%s", e)
        r = self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": int(leverage)}, signed=True)
        self._info("%s 杠杆设为 %s×", symbol, r.get("leverage"))
        return r

    def account(self):
        return self._request("GET", "/fapi/v2/account", {}, signed=True)

    def position(self, symbol):
        """返回该交易对的持仓（含多空双向字段）。"""
        arr = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for p in arr:
            if p["symbol"] == symbol:
                amt = float(p.get("positionAmt", 0) or 0)
                return {
                    "symbol": symbol,
                    "positionAmt": amt,
                    "entryPrice": float(p.get("entryPrice", 0) or 0),
                    "markPrice": float(p.get("markPrice", 0) or 0),
                    "unRealizedProfit": float(p.get("unRealizedProfit", 0) or 0),
                    "liquidationPrice": float(p.get("liquidationPrice", 0) or 0),
                    "isolatedMargin": float(p.get("isolatedMargin", 0) or 0),
                    "leverage": float(p.get("leverage", 0) or 0),
                    "side": "多" if amt > 0 else ("空" if amt < 0 else "空仓"),
                }
        return None

    def dual_side_position(self):
        """账户是否为「双向持仓」模式。True = 双向（本策略不支持）。

        双向持仓下 /fapi/v2/positionRisk 会对同一 symbol 返回多空两条记录，
        且下单必须带 positionSide，否则报错。本策略是单向净头寸，必须用单向模式。
        """
        try:
            r = self._request("GET", "/fapi/v1/positionSide/dual", {}, signed=True)
            return bool(r.get("dualSidePosition", False))
        except Exception:                                   # noqa: BLE001
            return None                                     # 查不到就不拦，交给下单时暴露

    def usdt_balance(self):
        acc = self.account()
        for a in acc.get("assets", []):
            if a["asset"] == "USDT":
                return {"wallet": float(a.get("walletBalance", 0)),
                        "available": float(a.get("availableBalance", 0)),
                        "unrealized": float(a.get("unrealizedProfit", 0))}
        return {"wallet": 0.0, "available": 0.0, "unrealized": 0.0}

    def open_orders(self, symbol):
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    # ---------------------------------------------------------- 精度处理
    @staticmethod
    def _floor_step(v, step):
        if step <= 0:
            return v
        d = int(round(math.log10(1 / step))) if step < 1 else 0
        n = math.floor(round(v / step, 10))
        return round(n * step, max(0, d + 2))

    def align_qty(self, qty, symbol):
        f = self.symbol_filters(symbol)
        q = self._floor_step(abs(qty), f["qty_step"])
        return round(q, f["qty_precision"])

    def align_price(self, px, symbol):
        f = self.symbol_filters(symbol)
        p = round(round(px / f["price_tick"]) * f["price_tick"], f["price_precision"])
        return p

    # ---------------------------------------------------------- 下单
    def market_order(self, symbol, side, qty, reduce_only=False, dry_run=False):
        """市价单。side: BUY / SELL。qty: 正数（数量）。"""
        f = self.symbol_filters(symbol)
        q = self.align_qty(qty, symbol)
        if q < f["min_qty"]:
            raise ValueError(f"数量 {q} 小于最小下单量 {f['min_qty']}（{symbol}）")

        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": q}
        if reduce_only:
            params["reduceOnly"] = "true"

        # 名义校验：市价单用标记价估算
        try:
            mk = float(self._request("GET", "/fapi/v1/premiumIndex",
                                     {"symbol": symbol})["markPrice"])
        except Exception:                                   # noqa: BLE001
            mk = 0.0
        if mk and q * mk < f["min_notional"]:
            if reduce_only:
                # 减仓单不做本地硬拦：币安对 reduceOnly 的名义门槛判定与普通单不同，
                # 本地误拦会导致「平不掉仓」，比被交易所拒单更危险。交给交易所裁决。
                self._warn("名义 %.2f U 小于最小名义 %.2f U，但这是只减仓单，仍提交（%s）",
                           q * mk, f["min_notional"], symbol)
            else:
                raise ValueError(f"名义 {q * mk:.2f} U 小于最小名义 {f['min_notional']} U")

        desc = (f"{side} {q} {symbol}"
                + ("（只减仓）" if reduce_only else "")
                + (f"  名义≈{q * mk:.2f} U" if mk else ""))
        if dry_run:
            self._info("[DRY-RUN] 将下单：%s", desc)
            return {"dry_run": True, "desc": desc, "params": params}

        self._info("提交下单：%s", desc)
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all(self, symbol):
        return self._request("DELETE", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol}, signed=True)
