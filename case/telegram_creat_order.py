# -*- coding: utf-8 -*-

"""
============================================================
Telegram → SignalParser → TradeManager → Binance Futures
============================================================

交易流程：

1. Telegram:
   #ETH 现价空
        ↓
   OPEN
        ↓
   Binance 市价开仓
        ↓
   获取真实成交价
        ↓
   默认 ±7% 保护性 TP/SL


2. Telegram:
   #ETHUSDT 止盈：1.32-1.39 止损：1.20
        ↓
   SET_TPSL
        ↓
   删除原 ±7% TP/SL
        ↓
   重新挂正式 TP/SL


3. Telegram:
   #ETH TP1止盈
        ↓
   TP
        ↓
   平 TP1 对应仓位


4. Telegram:
   #ETH TP2止盈
        ↓
   平 TP2 对应仓位


5. Telegram:
   #ETH TP3止盈
        ↓
   剩余全部平仓


注意：
- 本程序使用 Binance USDⓈ-M Futures
- 默认单向持仓 positionSide=BOTH
- TP/SL 使用 algoOrder
- 默认保护距离 7%
- TP1/TP2/TP3 默认比例：
    TP1 = 30%
    TP2 = 30%
    TP3 = 剩余全部
"""

import os
import re
import json
import time
import hmac
import hashlib
import logging
import traceback
import requests

from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode


# ============================================================
# 1. 基础配置
# ============================================================

ENV = os.getenv("BINANCE_ENV", "TEST").upper()

CONFIG = {
    "TEST": {
        "BASE_URL": "https://testnet.binancefuture.com",
    },
    "MAIN": {
        "BASE_URL": "https://fapi.binance.com",
    }
}

BASE_URL = CONFIG[ENV]["BASE_URL"]

# ------------------------------------------------------------
# API KEY 不要写死
#
# Windows:
#
# set BINANCE_API_KEY=你的KEY
# set BINANCE_API_SECRET=你的SECRET
#
# PowerShell:
#
# $env:BINANCE_API_KEY="你的KEY"
# $env:BINANCE_API_SECRET="你的SECRET"
# ------------------------------------------------------------

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "❌ 未设置 BINANCE_API_KEY / BINANCE_API_SECRET 环境变量"
    )


# ============================================================
# 2. 策略配置
# ============================================================

# 默认保护距离
DEFAULT_PROTECTION_PERCENT = Decimal("0.07")

# TP 手动平仓比例
#
# TP1：30%
# TP2：30%
# TP3：剩余全部
#
TP_CLOSE_PERCENT = {
    1: Decimal("0.30"),
    2: Decimal("0.30"),
}

# 状态文件
STATE_FILE = "trade_state.json"

# 请求超时时间
REQUEST_TIMEOUT = 10

# 默认杠杆
DEFAULT_LEVERAGE = 10

# 默认下单金额
DEFAULT_USDT = Decimal("20")

# 最低名义价值
MIN_NOTIONAL = Decimal("100")

# Binance 某些错误可以忽略
IGNORE_ERROR_CODES = {-4059}


# ============================================================
# 3. 日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("TradeManager")


# ============================================================
# 4. Decimal 工具
# ============================================================

def D(value):
    """
    Decimal 快捷转换
    """
    return Decimal(str(value))


def decimal_to_str(value):
    """
    Decimal → Binance 可接受字符串
    """
    value = D(value)

    text = format(value, "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text


def floor_step(value, step):
    """
    按 Binance stepSize 向下取整
    """
    value = D(value)
    step = D(step)

    if step <= 0:
        return value

    return (value // step) * step


# ============================================================
# 5. Binance API
# ============================================================

class BinanceClient:

    def __init__(self):
        self.base_url = BASE_URL
        self.api_key = API_KEY
        self.api_secret = API_SECRET

        # symbol 精度缓存
        self.symbol_filters = {}

        # 初始化交易规则
        self.load_exchange_info()

    # --------------------------------------------------------
    # 签名
    # --------------------------------------------------------

    def _sign(self, params):

        query_string = urlencode(params)

        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    # --------------------------------------------------------
    # HTTP 请求
    # --------------------------------------------------------

    def request(
        self,
        method,
        endpoint,
        params=None,
        signed=False
    ):

        params = params.copy() if params else {}

        headers = {
            "X-MBX-APIKEY": self.api_key
        }

        if signed:
            params["timestamp"] = int(time.time() * 1000)

            params["signature"] = self._sign(params)

        url = self.base_url + endpoint

        try:

            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()

            data = response.json()

        except Exception as e:

            logger.error(
                "Binance 请求失败 | %s | %s | %s",
                method,
                endpoint,
                e
            )

            raise

        if isinstance(data, dict):

            code = data.get("code")

            if code is not None and code < 0:

                if code in IGNORE_ERROR_CODES:

                    logger.warning(
                        "Binance 可忽略错误 | code=%s | %s",
                        code,
                        data.get("msg")
                    )

                else:

                    raise RuntimeError(
                        f"Binance Error {code}: {data.get('msg')}"
                    )

        return data

    # ========================================================
    # Exchange Info
    # ========================================================

    def load_exchange_info(self):

        logger.info("加载 Binance ExchangeInfo...")

        data = self.request(
            "GET",
            "/fapi/v1/exchangeInfo"
        )

        for symbol_info in data.get("symbols", []):

            symbol = symbol_info["symbol"]

            lot_step = None
            tick_size = None
            min_qty = None

            for f in symbol_info.get("filters", []):

                if f["filterType"] == "LOT_SIZE":

                    lot_step = D(f["stepSize"])
                    min_qty = D(f["minQty"])

                elif f["filterType"] == "PRICE_FILTER":

                    tick_size = D(f["tickSize"])

            if lot_step and tick_size:

                self.symbol_filters[symbol] = {
                    "step": lot_step,
                    "tick": tick_size,
                    "min_qty": min_qty or D("0")
                }

        logger.info(
            "ExchangeInfo 加载完成 | symbols=%s",
            len(self.symbol_filters)
        )

    # ========================================================
    # Symbol 精度
    # ========================================================

    def get_symbol_filters(self, symbol):

        symbol = symbol.upper()

        if symbol not in self.symbol_filters:

            self.load_exchange_info()

        if symbol not in self.symbol_filters:

            raise RuntimeError(
                f"❌ Binance 未找到交易对：{symbol}"
            )

        return self.symbol_filters[symbol]

    # ========================================================
    # 最新价格
    # ========================================================

    def get_last_price(self, symbol):

        data = self.request(
            "GET",
            "/fapi/v1/ticker/price",
            {
                "symbol": symbol
            }
        )

        return D(data["price"])

    # ========================================================
    # 设置杠杆
    # ========================================================

    def set_leverage(self, symbol, leverage):

        logger.info(
            "设置杠杆 | %s | %sx",
            symbol,
            leverage
        )

        return self.request(
            "POST",
            "/fapi/v1/leverage",
            {
                "symbol": symbol,
                "leverage": leverage
            },
            signed=True
        )

    # ========================================================
    # 查询账户持仓
    # ========================================================

    def get_position(self, symbol):

        positions = self.request(
            "GET",
            "/fapi/v2/positionRisk",
            signed=True
        )

        for p in positions:

            if p["symbol"] != symbol:
                continue

            qty = D(p["positionAmt"])

            if qty != 0:

                return {
                    "symbol": symbol,
                    "qty": abs(qty),
                    "position_amt": qty,
                    "entry_price": D(p["entryPrice"]),
                    "mark_price": D(p["markPrice"]),
                    "unrealized_profit": D(
                        p["unRealizedProfit"]
                    ),
                    "leverage": int(p["leverage"]),
                    "side": (
                        "LONG"
                        if qty > 0
                        else "SHORT"
                    )
                }

        return None

    # ========================================================
    # 查询所有持仓
    # ========================================================

    def get_all_positions(self):

        return self.request(
            "GET",
            "/fapi/v2/positionRisk",
            signed=True
        )

    # ========================================================
    # 市价开仓
    # ========================================================

    def market_open(
        self,
        symbol,
        side,
        quantity
    ):

        order_side = (
            "BUY"
            if side == "LONG"
            else "SELL"
        )

        logger.info(
            "市价开仓 | %s | %s | qty=%s",
            symbol,
            side,
            quantity
        )

        return self.request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": order_side,
                "type": "MARKET",
                "quantity": decimal_to_str(quantity),
                "newOrderRespType": "RESULT"
            },
            signed=True
        )

    # ========================================================
    # 市价平仓
    # ========================================================

    def market_close(
        self,
        symbol,
        position_side,
        quantity
    ):

        close_side = (
            "SELL"
            if position_side == "LONG"
            else "BUY"
        )

        logger.info(
            "市价平仓 | %s | %s | qty=%s",
            symbol,
            position_side,
            quantity
        )

        return self.request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": decimal_to_str(quantity),
                "reduceOnly": "true",
                "newOrderRespType": "RESULT"
            },
            signed=True
        )

    # ========================================================
    # 查询普通订单
    # ========================================================

    def get_order(self, symbol, order_id):

        return self.request(
            "GET",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "orderId": order_id
            },
            signed=True
        )

    # ========================================================
    # 查询 Algo Orders
    # ========================================================

    def get_open_algo_orders(self, symbol=None):

        params = {}

        if symbol:
            params["symbol"] = symbol

        return self.request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            params,
            signed=True
        )

    # ========================================================
    # 删除 Algo Order
    # ========================================================

    def cancel_algo_order(
        self,
        symbol,
        algo_id
    ):

        logger.info(
            "取消 Algo Order | %s | algoId=%s",
            symbol,
            algo_id
        )

        return self.request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {
                "symbol": symbol,
                "algoId": algo_id
            },
            signed=True
        )

    # ========================================================
    # 删除某币种全部保护单
    # ========================================================

    def cancel_all_algo_orders(self, symbol):

        logger.info(
            "清理旧保护单 | %s",
            symbol
        )

        try:

            orders = self.get_open_algo_orders(symbol)

        except Exception as e:

            logger.error(
                "获取 Algo Orders 失败 | %s | %s",
                symbol,
                e
            )

            return

        if not orders:
            return

        for order in orders:

            algo_id = (
                order.get("algoId")
                or order.get("orderId")
            )

            if not algo_id:
                continue

            try:

                self.cancel_algo_order(
                    symbol,
                    algo_id
                )

            except Exception as e:

                logger.warning(
                    "取消保护单失败 | %s | %s | %s",
                    symbol,
                    algo_id,
                    e
                )

    # ========================================================
    # 创建 STOP_MARKET
    # ========================================================

    def place_stop_loss(
        self,
        symbol,
        position_side,
        trigger_price
    ):

        close_side = (
            "SELL"
            if position_side == "LONG"
            else "BUY"
        )

        logger.info(
            "挂止损 | %s | %s | %s",
            symbol,
            position_side,
            trigger_price
        )

        return self.request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "triggerPrice": decimal_to_str(
                    trigger_price
                ),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "TRUE"
            },
            signed=True
        )

    # ========================================================
    # 创建 TAKE_PROFIT_MARKET
    # ========================================================

    def place_take_profit(
        self,
        symbol,
        position_side,
        trigger_price
    ):

        close_side = (
            "SELL"
            if position_side == "LONG"
            else "BUY"
        )

        logger.info(
            "挂止盈 | %s | %s | %s",
            symbol,
            position_side,
            trigger_price
        )

        return self.request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "positionSide": "BOTH",
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": decimal_to_str(
                    trigger_price
                ),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "TRUE"
            },
            signed=True
        )


# ============================================================
# 6. SignalParser
# ============================================================

class SignalParser:

    SYMBOL_ALIASES = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "BB": "BBUSDT",
        "AIN": "AINUSDT",
        "NIL": "NILUSDT",
    }

    @classmethod
    def normalize_symbol(cls, symbol):

        symbol = symbol.upper().strip()

        symbol = symbol.replace("#", "")

        if symbol.endswith("USDT"):
            return symbol

        if symbol in cls.SYMBOL_ALIASES:
            return cls.SYMBOL_ALIASES[symbol]

        return symbol + "USDT"

    @classmethod
    def parse_symbol(cls, text):

        match = re.search(
            r"#([A-Za-z0-9]+)",
            text
        )

        if not match:
            return None

        return cls.normalize_symbol(
            match.group(1)
        )

    @classmethod
    def parse_side(cls, text):

        text = text.lower()

        # 做多
        if (
            "现价多" in text
            or "开多" in text
            or "做多" in text
            or "多单" in text
        ):
            return "LONG"

        # 做空
        if (
            "现价空" in text
            or "开空" in text
            or "做空" in text
            or "空单" in text
        ):
            return "SHORT"

        return None

    @classmethod
    def parse_tp_level(cls, text):

        # TP1
        match = re.search(
            r"TP\s*([123])",
            text,
            re.IGNORECASE
        )

        if match:
            return int(match.group(1))

        # TP1️⃣ / TP2️⃣ / TP3️⃣
        emoji_match = re.search(
            r"TP\s*([123])️⃣",
            text,
            re.IGNORECASE
        )

        if emoji_match:
            return int(
                emoji_match.group(1)
            )

        return None

    @classmethod
    def parse_tpsl(cls, text):

        tp_match = re.search(
            r"止盈\s*[:：]\s*([0-9.\-]+)",
            text
        )

        sl_match = re.search(
            r"止损\s*[:：]\s*([0-9.\-]+)",
            text
        )

        add_match = re.search(
            r"补仓\s*[:：]\s*([0-9.\-]+)",
            text
        )

        tp = []
        sl = []
        add = []

        if tp_match:

            tp = [
                D(x)
                for x in tp_match.group(1).split("-")
                if x
            ]

        if sl_match:

            sl = [
                D(x)
                for x in sl_match.group(1).split("-")
                if x
            ]

        if add_match:

            add = [
                D(x)
                for x in add_match.group(1).split("-")
                if x
            ]

        return add, tp, sl

    @classmethod
    def parse_signal(cls, text):

        text = text.strip()

        symbol = cls.parse_symbol(text)

        side = cls.parse_side(text)

        # ====================================================
        # TP 事件
        # ====================================================

        tp_level = cls.parse_tp_level(text)

        if (
            tp_level is not None
            and "止盈" in text
        ):

            return {
                "type": "TP",
                "symbol": symbol,
                "side": None,
                "add": [],
                "tp": [],
                "sl": [],
                "tp_level": tp_level
            }

        # ====================================================
        # OPEN
        # ====================================================

        if (
            symbol
            and side
            and (
                "现价" in text
                or "开多" in text
                or "开空" in text
            )
        ):

            return {
                "type": "OPEN",
                "symbol": symbol,
                "side": side,
                "add": [],
                "tp": [],
                "sl": [],
                "tp_level": None
            }

        # ====================================================
        # SET TPSL
        # ====================================================

        add, tp, sl = cls.parse_tpsl(text)

        if tp or sl or add:

            return {
                "type": "SET_TPSL",
                "symbol": symbol,
                "side": side,
                "add": add,
                "tp": tp,
                "sl": sl,
                "tp_level": None
            }

        return {
            "type": "UNKNOWN",
            "symbol": symbol,
            "side": side,
            "add": [],
            "tp": [],
            "sl": [],
            "tp_level": None
        }


# ============================================================
# 7. TradeManager
# ============================================================

class TradeManager:

    def __init__(self):

        self.client = BinanceClient()

        self.active_trades = {}

        self.last_symbol = None

        self.load_state()

    # ========================================================
    # 状态保存
    # ========================================================

    def save_state(self):

        data = {}

        for symbol, trade in self.active_trades.items():

            data[symbol] = {
                k: self._serialize_value(v)
                for k, v in trade.items()
            }

        try:

            with open(
                STATE_FILE,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

        except Exception as e:

            logger.error(
                "保存交易状态失败：%s",
                e
            )

    def _serialize_value(self, value):

        if isinstance(value, Decimal):
            return str(value)

        if isinstance(value, dict):
            return {
                k: self._serialize_value(v)
                for k, v in value.items()
            }

        if isinstance(value, list):
            return [
                self._serialize_value(x)
                for x in value
            ]

        return value

    # ========================================================
    # 状态恢复
    # ========================================================

    def load_state(self):

        if not os.path.exists(STATE_FILE):

            return

        try:

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            for symbol, trade in data.items():

                if "entry_price" in trade:
                    trade["entry_price"] = D(
                        trade["entry_price"]
                    )

                if "qty" in trade:
                    trade["qty"] = D(
                        trade["qty"]
                    )

                if "tp" in trade:
                    trade["tp"] = [
                        D(x)
                        for x in trade["tp"]
                    ]

                if "sl" in trade:
                    trade["sl"] = [
                        D(x)
                        for x in trade["sl"]
                    ]

                if "tp_executed" in trade:
                    trade["tp_executed"] = [
                        int(x)
                        for x in trade["tp_executed"]
                    ]

                self.active_trades[symbol] = trade

            logger.info(
                "恢复交易状态 | %s",
                list(self.active_trades.keys())
            )

        except Exception as e:

            logger.error(
                "恢复状态失败：%s",
                e
            )

    # ========================================================
    # 找最近活跃币种
    # ========================================================

    def resolve_symbol(self, symbol):

        if symbol:
            return symbol

        if self.last_symbol:
            return self.last_symbol

        if len(self.active_trades) == 1:
            return list(
                self.active_trades.keys()
            )[0]

        raise RuntimeError(
            "❌ 消息没有 symbol，且当前无法确定对应交易"
        )

    # ========================================================
    # 计算默认 ±7%
    # ========================================================

    def calculate_default_tpsl(
        self,
        entry_price,
        side
    ):

        entry_price = D(entry_price)

        if side == "LONG":

            tp = (
                entry_price
                * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
            )

            sl = (
                entry_price
                * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
            )

        else:

            tp = (
                entry_price
                * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
            )

            sl = (
                entry_price
                * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
            )

        filters = self.client.get_symbol_filters(
            self.last_symbol
            if self.last_symbol
            else ""
        )

        return tp, sl

    # ========================================================
    # 风控检查
    # ========================================================

    def validate_tpsl(
        self,
        side,
        price,
        tp,
        sl
    ):

        if side == "LONG":

            if not (tp > price > sl):

                raise RuntimeError(
                    f"❌ LONG TP/SL错误 | "
                    f"price={price} "
                    f"tp={tp} "
                    f"sl={sl}"
                )

        elif side == "SHORT":

            if not (tp < price < sl):

                raise RuntimeError(
                    f"❌ SHORT TP/SL错误 | "
                    f"price={price} "
                    f"tp={tp} "
                    f"sl={sl}"
                )

    # ========================================================
    # 格式化价格
    # ========================================================

    def format_price(
        self,
        symbol,
        price
    ):

        filters = self.client.get_symbol_filters(
            symbol
        )

        return floor_step(
            D(price),
            filters["tick"]
        )

    # ========================================================
    # 格式化数量
    # ========================================================

    def format_qty(
        self,
        symbol,
        qty
    ):

        filters = self.client.get_symbol_filters(
            symbol
        )

        return floor_step(
            D(qty),
            filters["step"]
        )

    # ========================================================
    # 根据 USDT 计算数量
    # ========================================================

    def calc_qty(
        self,
        symbol,
        usdt,
        price,
        leverage
    ):

        filters = self.client.get_symbol_filters(
            symbol
        )

        notional = (
            D(usdt)
            * D(leverage)
        )

        qty = (
            notional
            / D(price)
        )

        qty = floor_step(
            qty,
            filters["step"]
        )

        # 最低名义价值
        if qty * price < MIN_NOTIONAL:

            qty = (
                MIN_NOTIONAL
                / price
            )

            qty = floor_step(
                qty,
                filters["step"]
            )

            logger.warning(
                "名义价值不足，自动调整 qty=%s",
                qty
            )

        if qty < filters["min_qty"]:

            qty = filters["min_qty"]

        return qty

    # ========================================================
    # OPEN
    # ========================================================

    def handle_open(self, signal):

        symbol = signal["symbol"]
        side = signal["side"]

        if not symbol or not side:

            raise RuntimeError(
                "❌ OPEN 缺少 symbol / side"
            )

        self.last_symbol = symbol

        logger.info(
            "========== OPEN =========="
        )

        logger.info(
            "symbol=%s | side=%s",
            symbol,
            side
        )

        # ----------------------------------------------------
        # 防止重复开仓
        # ----------------------------------------------------

        position = self.client.get_position(
            symbol
        )

        if position:

            logger.warning(
                "⚠ %s 已经存在持仓，跳过重复开仓",
                symbol
            )

            self.active_trades.setdefault(
                symbol,
                {
                    "side": position["side"],
                    "entry_price": position["entry_price"],
                    "qty": position["qty"],
                    "tp": [],
                    "sl": [],
                    "tp_executed": []
                }
            )

            return

        # ----------------------------------------------------
        # 设置杠杆
        # ----------------------------------------------------

        leverage = DEFAULT_LEVERAGE

        self.client.set_leverage(
            symbol,
            leverage
        )

        # ----------------------------------------------------
        # 当前价格
        # ----------------------------------------------------

        market_price = (
            self.client.get_last_price(
                symbol
            )
        )

        # ----------------------------------------------------
        # 计算数量
        # ----------------------------------------------------

        qty = self.calc_qty(
            symbol,
            DEFAULT_USDT,
            market_price,
            leverage
        )

        logger.info(
            "开仓数量=%s",
            qty
        )

        # ----------------------------------------------------
        # 市价开仓
        # ----------------------------------------------------

        order = self.client.market_open(
            symbol,
            side,
            qty
        )

        logger.info(
            "开仓订单=%s",
            order
        )

        # ----------------------------------------------------
        # 等待持仓
        # ----------------------------------------------------

        position = None

        for _ in range(10):

            time.sleep(0.3)

            position = self.client.get_position(
                symbol
            )

            if position:
                break

        if not position:

            raise RuntimeError(
                "❌ 开仓后未检测到持仓"
            )

        real_entry = position["entry_price"]
        real_qty = position["qty"]

        logger.info(
            "真实成交 | entry=%s | qty=%s",
            real_entry,
            real_qty
        )

        # ----------------------------------------------------
        # 计算默认 ±7%
        # ----------------------------------------------------

        if side == "LONG":

            default_tp = (
                real_entry
                * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
            )

            default_sl = (
                real_entry
                * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
            )

        else:

            default_tp = (
                real_entry
                * (Decimal("1") - DEFAULT_PROTECTION_PERCENT)
            )

            default_sl = (
                real_entry
                * (Decimal("1") + DEFAULT_PROTECTION_PERCENT)
            )

        default_tp = self.format_price(
            symbol,
            default_tp
        )

        default_sl = self.format_price(
            symbol,
            default_sl
        )

        self.validate_tpsl(
            side,
            real_entry,
            default_tp,
            default_sl
        )

        # ----------------------------------------------------
        # 先清理旧保护单
        # ----------------------------------------------------

        self.client.cancel_all_algo_orders(
            symbol
        )

        # ----------------------------------------------------
        # 挂默认止损
        # ----------------------------------------------------

        self.client.place_stop_loss(
            symbol,
            side,
            default_sl
        )

        # ----------------------------------------------------
        # 挂默认止盈
        # ----------------------------------------------------

        self.client.place_take_profit(
            symbol,
            side,
            default_tp
        )

        # ----------------------------------------------------
        # 保存状态
        # ----------------------------------------------------

        self.active_trades[symbol] = {

            "side": side,

            "entry_price": real_entry,

            "qty": real_qty,

            "default_tp": default_tp,

            "default_sl": default_sl,

            "tp": [],

            "sl": [],

            "tp_executed": [],

            "created_at": int(time.time())
        }

        self.save_state()

        logger.info(
            "✅ OPEN 完成 | %s | %s | entry=%s | TP=%s | SL=%s",
            symbol,
            side,
            real_entry,
            default_tp,
            default_sl
        )

    # ========================================================
    # SET TPSL
    # ========================================================

    def handle_set_tpsl(self, signal):

        symbol = self.resolve_symbol(
            signal.get("symbol")
        )

        self.last_symbol = symbol

        logger.info(
            "========== SET TPSL =========="
        )

        logger.info(
            "symbol=%s | tp=%s | sl=%s",
            symbol,
            signal["tp"],
            signal["sl"]
        )

        # ----------------------------------------------------
        # 查询持仓
        # ----------------------------------------------------

        position = self.client.get_position(
            symbol
        )

        if not position:

            logger.warning(
                "⚠ %s 当前无持仓，无法设置 TPSL",
                symbol
            )

            return

        side = position["side"]
        entry_price = position["entry_price"]

        # ----------------------------------------------------
        # TP / SL
        # ----------------------------------------------------

        tp_list = signal.get("tp", [])
        sl_list = signal.get("sl", [])

        if not tp_list and not sl_list:

            logger.warning(
                "⚠ TPSL消息没有有效 TP/SL"
            )

            return

        # ----------------------------------------------------
        # 格式化
        # ----------------------------------------------------

        formatted_tp = []

        for tp in tp_list:

            tp = self.format_price(
                symbol,
                tp
            )

            formatted_tp.append(tp)

        formatted_sl = []

        for sl in sl_list:

            sl = self.format_price(
                symbol,
                sl
            )

            formatted_sl.append(sl)

        # ----------------------------------------------------
        # 风控检查
        # ----------------------------------------------------

        if formatted_tp and formatted_sl:

            # 只检查第一个 TP / 第一个 SL
            self.validate_tpsl(
                side,
                self.client.get_last_price(
                    symbol
                ),
                formatted_tp[0],
                formatted_sl[0]
            )

        # ----------------------------------------------------
        # 删除旧保护单
        # ----------------------------------------------------

        self.client.cancel_all_algo_orders(
            symbol
        )

        # ----------------------------------------------------
        # 正式 SL
        # ----------------------------------------------------

        if formatted_sl:

            self.client.place_stop_loss(
                symbol,
                side,
                formatted_sl[0]
            )

        # ----------------------------------------------------
        # 正式 TP
        #
        # 注意：
        # 这里不直接挂多个 closePosition TP。
        #
        # 因为 TP1/TP2/TP3 是 Telegram 事件，
        # 后续由 handle_tp() 手动部分平仓。
        #
        # 所以正式 TP 数值保存到状态即可。
        # ----------------------------------------------------

        trade = self.active_trades.get(
            symbol,
            {}
        )

        trade.update({

            "side": side,

            "entry_price": entry_price,

            "qty": position["qty"],

            "tp": formatted_tp,

            "sl": formatted_sl,

            "tp_executed": [],

            "updated_at": int(time.time())
        })

        self.active_trades[symbol] = trade

        self.save_state()

        logger.info(
            "✅ 正式 TPSL 已更新 | %s | TP=%s | SL=%s",
            symbol,
            formatted_tp,
            formatted_sl
        )

    # ========================================================
    # TP1 / TP2 / TP3
    # ========================================================

    def handle_tp(self, signal):

        symbol = self.resolve_symbol(
            signal.get("symbol")
        )

        tp_level = signal.get(
            "tp_level"
        )

        self.last_symbol = symbol

        logger.info(
            "========== TP EVENT =========="
        )

        logger.info(
            "symbol=%s | TP%s",
            symbol,
            tp_level
        )

        if tp_level not in (1, 2, 3):

            logger.warning(
                "⚠ 不支持 TP%s",
                tp_level
            )

            return

        # ----------------------------------------------------
        # 获取当前持仓
        # ----------------------------------------------------

        position = self.client.get_position(
            symbol
        )

        if not position:

            logger.warning(
                "⚠ %s 已经没有持仓",
                symbol
            )

            return

        # ----------------------------------------------------
        # 状态
        # ----------------------------------------------------

        trade = self.active_trades.get(
            symbol
        )

        if not trade:

            logger.warning(
                "⚠ %s 没有本地交易状态，"
                "将根据 Binance 当前持仓处理",
                symbol
            )

            trade = {
                "side": position["side"],
                "qty": position["qty"],
                "tp_executed": []
            }

            self.active_trades[symbol] = trade

        executed = trade.setdefault(
            "tp_executed",
            []
        )

        # ----------------------------------------------------
        # 防止重复 TP
        # ----------------------------------------------------

        if tp_level in executed:

            logger.warning(
                "⚠ %s TP%s 已经执行过，跳过",
                symbol,
                tp_level
            )

            return

        current_qty = position["qty"]

        # ----------------------------------------------------
        # 计算本次平仓比例
        # ----------------------------------------------------

        if tp_level == 1:

            close_percent = Decimal("0.30")

        elif tp_level == 2:

            close_percent = Decimal("0.30")

        else:

            # TP3 剩余全部
            close_percent = Decimal("1")

        # ----------------------------------------------------
        # TP1 / TP2
        # ----------------------------------------------------

        if tp_level in (1, 2):

            close_qty = (
                current_qty
                * close_percent
            )

        else:

            close_qty = current_qty

        # ----------------------------------------------------
        # 数量精度
        # ----------------------------------------------------

        close_qty = self.format_qty(
            symbol,
            close_qty
        )

        if close_qty <= 0:

            logger.warning(
                "⚠ %s TP%s 计算出的平仓数量为0",
                symbol,
                tp_level
            )

            return

        if close_qty > current_qty:

            close_qty = current_qty

        # ----------------------------------------------------
        # 执行市价平仓
        # ----------------------------------------------------

        self.client.market_close(
            symbol,
            position["side"],
            close_qty
        )

        executed.append(
            tp_level
        )

        trade["tp_executed"] = executed

        # ----------------------------------------------------
        # TP3 或者持仓归零
        # ----------------------------------------------------

        time.sleep(0.5)

        new_position = self.client.get_position(
            symbol
        )

        if not new_position:

            logger.info(
                "🎯 %s 仓位已经全部平仓",
                symbol
            )

            self.active_trades.pop(
                symbol,
                None
            )

            self.save_state()

            return

        # ----------------------------------------------------
        # 更新剩余数量
        # ----------------------------------------------------

        trade["qty"] = new_position["qty"]

        self.active_trades[symbol] = trade

        self.save_state()

        logger.info(
            "✅ TP%s 执行完成 | %s | 本次平仓=%s | 剩余=%s",
            tp_level,
            symbol,
            close_qty,
            new_position["qty"]
        )

    # ========================================================
    # 总入口
    # ========================================================

    def handle_signal(self, signal):

        signal_type = signal.get(
            "type"
        )

        logger.info(
            "收到交易信号：%s",
            signal
        )

        try:

            if signal_type == "OPEN":

                return self.handle_open(
                    signal
                )

            elif signal_type == "SET_TPSL":

                return self.handle_set_tpsl(
                    signal
                )

            elif signal_type == "TP":

                return self.handle_tp(
                    signal
                )

            elif signal_type == "UNKNOWN":

                logger.info(
                    "ℹ 非交易消息，忽略：%s",
                    signal
                )

            else:

                logger.warning(
                    "⚠ 未知 signal type：%s",
                    signal_type
                )

        except Exception as e:

            logger.error(
                "❌ 交易执行失败：%s",
                e
            )

            traceback.print_exc()

    # ========================================================
    # 启动时同步 Binance 状态
    # ========================================================

    def sync_positions(self):

        logger.info(
            "========== 同步 Binance 持仓 =========="
        )

        try:

            positions = (
                self.client.get_all_positions()
            )

        except Exception as e:

            logger.error(
                "同步持仓失败：%s",
                e
            )

            return

        real_symbols = set()

        for p in positions:

            qty = D(
                p["positionAmt"]
            )

            if qty == 0:
                continue

            symbol = p["symbol"]

            real_symbols.add(symbol)

            side = (
                "LONG"
                if qty > 0
                else "SHORT"
            )

            trade = self.active_trades.get(
                symbol,
                {}
            )

            trade["side"] = side

            trade["qty"] = abs(qty)

            trade["entry_price"] = D(
                p["entryPrice"]
            )

            trade.setdefault(
                "tp",
                []
            )

            trade.setdefault(
                "sl",
                []
            )

            trade.setdefault(
                "tp_executed",
                []
            )

            self.active_trades[symbol] = trade

        # 删除本地已经不存在的持仓
        for symbol in list(
            self.active_trades.keys()
        ):

            if symbol not in real_symbols:

                logger.info(
                    "本地状态已无实际持仓，清理：%s",
                    symbol
                )

                self.active_trades.pop(
                    symbol,
                    None
                )

        self.save_state()

        logger.info(
            "持仓同步完成 | %s",
            list(real_symbols)
        )


# ============================================================
# 8. Telegram 接入层
# ============================================================

class TelegramTradeBot:

    """
    这里只负责：

    Telegram消息
          ↓
    SignalParser
          ↓
    TradeManager

    你现在如果已经有 Telegram API / Telethon / Pyrogram
    的监听代码，只需要把：

        bot.on_message(text)

    接进去即可。

    不需要修改 TradeManager。
    """

    def __init__(self):

        self.manager = TradeManager()

    def on_message(self, text):

        logger.info(
            "\n========== Telegram ==========\n%s",
            text
        )

        # ----------------------------------------------------
        # 解析
        # ----------------------------------------------------

        signal = SignalParser.parse_signal(
            text
        )

        logger.info(
            "解析结果：%s",
            signal
        )

        # ----------------------------------------------------
        # 非交易消息
        # ----------------------------------------------------

        if signal["type"] == "UNKNOWN":

            return

        # ----------------------------------------------------
        # 执行
        # ----------------------------------------------------

        self.manager.handle_signal(
            signal
        )


# ============================================================
# 9. 本地测试
# ============================================================

def test_parser():

    test_messages = [

        "#ETH 现价空",

        "#BB 现价多",

        "#BB TP1止盈",

        "#AIN 手动TP1止盈",

        "#NIL TP3️⃣止盈",

        "止盈：1.32-1.39 止损：1.195",

        "止盈：1.32-1.39-1.49 止损：1.195",

        "#ETHUSDT 止盈：1.32-1.39 止损：1.195",

        "#ETHUSDT 补仓：1.25 止盈：1.39 止损：1.20",

        "#IRYSUSDT 空单 20x 93,34%🚀",

    ]

    for text in test_messages:

        print("=" * 70)

        print("原消息：", text)

        print(
            "解析结果：",
            SignalParser.parse_signal(text)
        )


# ============================================================
# 10. 程序入口
# ============================================================

if __name__ == "__main__":

    print("=" * 70)
    print("Telegram → SignalParser → TradeManager → Binance")
    print("=" * 70)

    print(
        f"环境：{ENV}"
    )

    # --------------------------------------------------------
    # 创建交易机器人
    # --------------------------------------------------------

    bot = TelegramTradeBot()

    # --------------------------------------------------------
    # 启动同步 Binance 当前真实持仓
    # --------------------------------------------------------

    bot.manager.sync_positions()

    # --------------------------------------------------------
    # 先测试解析
    # --------------------------------------------------------

    test_parser()

    # --------------------------------------------------------
    # 下面是模拟 Telegram 消息
    #
    # 第一次测试建议全部使用 TESTNET
    # --------------------------------------------------------

    # bot.on_message(
    #     "#ETH 现价空"
    # )

    # 等待 Telegram 推送正式 TPSL

    # bot.on_message(
    #     "#ETHUSDT 止盈：3200-3100-3000 止损：3600"
    # )

    # TP1
    # bot.on_message(
    #     "#ETH TP1止盈"
    # )

    # TP2
    # bot.on_message(
    #     "#ETH TP2止盈"
    # )

    # TP3
    # bot.on_message(
    #     "#ETH TP3止盈"
    # )

    print("\n程序初始化完成。")
    print("等待 Telegram 消息...")

