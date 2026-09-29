import time
import hmac
import hashlib
import requests
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

# =========================
# 基础配置（主网 / 测试网）
# =========================
ENV = "TEST"   # "TEST" or "MAIN"
IGNORE_ERROR_CODES = {-4059}

CONFIG = {
    "TEST": {
        "BASE_URL": "https://testnet.binancefuture.com",
        "API_KEY": "riufKUh5kdZNF0eOiHo2eLRPVXOFlPAxbVIdePgK3e0EIbeOmFqGIaryTTT0u9fb",
        "API_SECRET": "FG6lfFCmNnG0ieStXhQ9QTfaM3ix1Pb5tLksdwvnwq8aUVSinGuzyH6ID2gZJQpO",
    },
    "MAIN": {
        "BASE_URL": "https://fapi.binance.com",
        "API_KEY": "r7VH9su6ab0QAURp645Dj4jHPtvbNrFRMslM8MvEZRYaQrtwtKIz1ejuyFeu30nL",
        "API_SECRET": "DJHelGTTbxyOAx4lbtFDDNsscy2ZM0JjEZIsiC40jzXGUDRkIFVsiGEUlolJ0jmG",
    }
}

BASE_URL = CONFIG[ENV]["BASE_URL"]
API_KEY = CONFIG[ENV]["API_KEY"]
API_SECRET = CONFIG[ENV]["API_SECRET"]

# =====================================================
# 请求封装
# =====================================================
def _sign(params: dict) -> str:
    return hmac.new(
        API_SECRET.encode(),
        urlencode(params).encode(),
        hashlib.sha256
    ).hexdigest()


def _request(method: str, endpoint: str, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = _sign(params)

    r = requests.request(
        method,
        BASE_URL + endpoint,
        headers={"X-MBX-APIKEY": API_KEY},
        params=params,
        timeout=10
    ).json()

    if "code" in r and r["code"] < 0:
        raise Exception(f"❌ Binance Error {r['code']}: {r['msg']}")

    return r

# =====================================================
# 交易所精度
# =====================================================
def get_symbol_filters(symbol):
    info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=5).json()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            step = tick = None
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = Decimal(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    tick = Decimal(f["tickSize"])
            return step, tick
    raise Exception("❌ 未找到交易对精度")

def format_by_step(value: Decimal, step: Decimal) -> Decimal:
    return (value // step) * step

# =====================================================
# 市场数据
# =====================================================
def get_last_price(symbol) -> Decimal:
    r = requests.get(
        BASE_URL + "/fapi/v1/ticker/price",
        params={"symbol": symbol},
        timeout=5
    ).json()
    return Decimal(r["price"])

# =====================================================
# 风控校验
# =====================================================
def risk_check(direction, price, tp, sl):
    if direction == "LONG" and not (tp > price > sl):
        raise Exception(f"❌ LONG 参数错误 tp:{tp} price:{price} sl:{sl}")
    if direction == "SHORT" and not (tp < price < sl):
        raise Exception(f"❌ SHORT 参数错误 tp:{tp} price:{price} sl:{sl}")

# =====================================================
# USDT → 下单数量
# =====================================================
def calc_qty(symbol, usdt, price, leverage):
    step, _ = get_symbol_filters(symbol)
    notional = Decimal(usdt) * Decimal(leverage)
    qty = format_by_step(notional / price, step)

    # Binance 最低名义价值 100 USDT
    if qty * price < Decimal("100"):
        qty = format_by_step(Decimal("100") / price, step)
        print(f"⚠ 名义价值不足，自动调整 qty={qty}")

    return qty

def get_position_qty(symbol):
    positions = _request("GET", "/fapi/v2/positionRisk")
    for p in positions:
        if p["symbol"] == symbol:
            return abs(Decimal(p["positionAmt"]))
    return Decimal("0")

# =====================================================
# 核心下单
# =====================================================
def place_trade(
        symbol: str,
        usdt: float,
        direction: str,     # LONG / SHORT
        leverage: int,
        take_profit: float,
        stop_loss: float
):
    print(f"\n🚀 下单开始 | {symbol} | {direction}")

    # 当前价格
    price = get_last_price(symbol)
    print(f"📈 当前价格 {price}")

    tp = Decimal(str(take_profit))
    sl = Decimal(str(stop_loss))

    # 风控
    risk_check(direction, price, tp, sl)

    # 精度
    step, tick = get_symbol_filters(symbol)

    # 数量
    qty = calc_qty(symbol, Decimal(usdt), price, leverage)
    qty = format_by_step(qty, step)

    # 价格精度
    tp = format_by_step(tp, tick)
    sl = format_by_step(sl, tick)

    print(f"QTY={qty} | TP={tp} | SL={sl}")

    # 设置杠杆
    _request("POST", "/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage
    })

    open_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    # 市价开仓
    _request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": open_side,
        "type": "MARKET",
        "quantity": str(qty)
    })

    time.sleep(0.3)

    # 确认持仓
    if get_position_qty(symbol) == 0:
        raise Exception("❌ 开仓失败，当前无持仓")

    # =========================================================
    # ✅ 止损单（STOP_MARKET）
    # =========================================================
    _request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "positionSide": "BOTH",
        "type": "STOP_MARKET",
        "triggerPrice": str(sl),
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE"
    })

    # =========================================================
    # ✅ 止盈单（TAKE_PROFIT_MARKET）
    # =========================================================
    _request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "positionSide": "BOTH",
        "type": "TAKE_PROFIT_MARKET",
        "triggerPrice": str(tp),
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE"
    })

    print("✅ 下单完成（已稳定挂 SL / TP）\n")


# =====================================================
# 程序入口
# =====================================================
if __name__ == "__main__":

    # 👉 健康检查通过才会执行到这里
    place_trade(
        symbol="BTCUSDT",
        usdt=20,
        direction="LONG",
        leverage=10,
        take_profit=89000,
        stop_loss=85000
    )
