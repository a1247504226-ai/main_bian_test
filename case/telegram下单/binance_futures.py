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
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"

# 合约 API 权重上限（每分钟）
WEIGHT_LIMIT_1M = 2400


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
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = TESTNET if testnet else MAINNET
        self.recv_window = recv_window
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

        # 精度缓存 {symbol: {"step": Decimal, "tick": Decimal, "minQty": Decimal, "minNotional": Decimal, "qtyPrecision": int, "pricePrecision": int}}
        self._filters: Dict[str, Dict[str, Any]] = {}
        # 时间偏移（本机与服务器时差）
        self._time_offset = 0

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def _sync_time(self):
        """同步服务器时间，避免 -1021 timestamp outside recvWindow

        用短超时：这只是优化项，失败也不影响下单（签名请求遇到 -1021 会自动重试）
        """
        r = self.session.get(f"{self.base}/fapi/v1/time", timeout=5)
        r.raise_for_status()
        server_ts = r.json()["serverTime"]
        self._time_offset = server_ts - int(time.time() * 1000)
        log.info("时间同步完成，偏移 %d ms", self._time_offset)

    def _sign(self, params: Dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        sig = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={sig}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = True,
        max_retries: int = 3,
    ) -> Any:
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            params["recvWindow"] = self.recv_window

        url = f"{self.base}{path}"
        last_err: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                if method.upper() == "GET":
                    q = self._sign(params) if signed else urlencode(params)
                    resp = self.session.get(f"{url}?{q}", timeout=self.timeout)
                else:
                    q = self._sign(params) if signed else urlencode(params)
                    resp = self.session.post(f"{url}?{q}", timeout=self.timeout)

                # 限频监控
                used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
                if used:
                    ratio = int(used) / WEIGHT_LIMIT_1M
                    if ratio > 0.8:
                        log.warning("API 权重已用 %.0f%% (%s/%d)，降速 2s",
                                    ratio * 100, used, WEIGHT_LIMIT_1M)
                        time.sleep(2)

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
                    log.warning("时间戳偏差，重新同步时间")
                    self._sync_time()
                    params["timestamp"] = int(time.time() * 1000) + self._time_offset
                    continue

                # 5xx 服务端错误 / 429 限频 / 418 封禁：退避重试
                if resp.status_code >= 500 or resp.status_code in (429, 418):
                    wait = 2 ** attempt
                    log.warning("HTTP %d，%d 秒后重试 (%d/%d)",
                                resp.status_code, wait, attempt, max_retries)
                    time.sleep(wait)
                    last_err = BinanceError(code, msg, resp.status_code)
                    continue

                raise BinanceError(code, msg, resp.status_code)

            except (requests.Timeout, requests.ConnectionError) as e:
                wait = 2 ** attempt
                log.warning("网络异常 %s，%d 秒后重试 (%d/%d)",
                            type(e).__name__, wait, attempt, max_retries)
                time.sleep(wait)
                last_err = e

        raise last_err if last_err else RuntimeError("请求失败")

    # ------------------------------------------------------------------
    # 交易规则 / 精度
    # ------------------------------------------------------------------
    def load_exchange_info(self, symbols: Optional[List[str]] = None):
        """加载交易对精度规则，缓存到 self._filters"""
        data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        want = {s.upper() for s in symbols} if symbols else None
        for s in data["symbols"]:
            sym = s["symbol"]
            if want and sym not in want:
                continue
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
        log.info("已加载 %d 个交易对的精度规则", len(self._filters))

    def get_filters(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
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

    def round_price(self, symbol: str, price: float) -> float:
        f = self.get_filters(symbol)
        tick = f["tick"]
        if not tick:
            return round(price, f["pricePrecision"])
        d = Decimal(str(price))
        n = (d / tick).to_integral_value(rounding=ROUND_DOWN)
        return float(n * tick)

    # ------------------------------------------------------------------
    # 账户 / 持仓
    # ------------------------------------------------------------------
    def get_balance(self, asset: str = "USDT") -> Dict[str, float]:
        data = self._request("GET", "/fapi/v2/balance")
        for b in data:
            if b["asset"] == asset:
                return {
                    "total": float(b["balance"]),
                    "available": float(b["availableBalance"]),
                    "unrealized": float(b["crossUnPnl"]),
                }
        return {"total": 0.0, "available": 0.0, "unrealized": 0.0}

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        data = self._request("GET", "/fapi/v2/positionRisk", params)
        out = []
        for p in data:
            amt = float(p["positionAmt"])
            if abs(amt) < 1e-12:
                continue
            out.append({
                "symbol": p["symbol"],
                "amount": amt,
                "side": "LONG" if amt > 0 else "SHORT",
                "entry": float(p["entryPrice"]),
                "mark": float(p["markPrice"]),
                "unrealized": float(p["unRealizedProfit"]),
                "leverage": int(p["leverage"]),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol.upper(), "leverage": leverage})

    def set_margin_type(self, symbol: str, isolated: bool = True):
        """设置逐仓/全仓。已是该模式时币安返回 -4046，忽略即可"""
        try:
            return self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol.upper(),
                "marginType": "ISOLATED" if isolated else "CROSSED",
            })
        except BinanceError as e:
            if e.code == -4046:  # No need to change margin type
                return {"msg": "margin type unchanged"}
            raise

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

        try:
            return self._request("POST", "/fapi/v1/order", params)
        except BinanceError as e:
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

    def stop_loss(
        self,
        symbol: str,
        position_side: str,
        stop_price: float,
        client_order_id: str,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """
        止损单：STOP_MARKET + closePosition=true
        position_side: 当前持仓方向 LONG / SHORT（平仓方向自动反向）
        closePosition=true 会平掉整个仓位，无需指定 quantity
        """
        close_side = "SELL" if position_side.upper() == "LONG" else "BUY"
        params = {
            "symbol": symbol.upper(),
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
            "closePosition": "true",
            "workingType": working_type,
            "newClientOrderId": client_order_id,
        }
        if position_side:
            params["positionSide"] = position_side.upper()
        try:
            return self._request("POST", "/fapi/v1/order", params)
        except BinanceError as e:
            if e.code == -4116:
                log.warning("止损单 %s 已存在，查询状态", client_order_id)
                try:
                    return self.get_order(symbol, client_order_id)
                except Exception:
                    pass
            raise

    def take_profit(
        self,
        symbol: str,
        position_side: str,
        stop_price: float,
        client_order_id: str,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """止盈单：TAKE_PROFIT_MARKET + closePosition=true"""
        close_side = "SELL" if position_side.upper() == "LONG" else "BUY"
        params = {
            "symbol": symbol.upper(),
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
            "closePosition": "true",
            "workingType": working_type,
            "newClientOrderId": client_order_id,
        }
        try:
            return self._request("POST", "/fapi/v1/order", params)
        except BinanceError as e:
            if e.code == -4116:
                log.warning("止盈单 %s 已存在，查询状态", client_order_id)
                try:
                    return self.get_order(symbol, client_order_id)
                except Exception:
                    pass
            raise

    def cancel_all(self, symbol: str) -> Any:
        return self._request("POST", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol.upper()})

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params)

    def get_mark_price(self, symbol: str) -> float:
        data = self._request("GET", "/fapi/v1/premiumIndex",
                             {"symbol": symbol.upper()}, signed=False)
        return float(data["markPrice"])

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{self.base}/fapi/v1/ping", timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False
