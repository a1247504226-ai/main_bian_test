"""
币安合约推送信号执行器（单文件版，循环运行）

业务流程：
1. 接收外部推送信号：OPEN / SET_TPSL / TP / CLOSE
2. OPEN：市价开仓，默认上下浮动 7% 作为止盈止损
3. SET_TPSL：更新该币对的最新止盈止损，并立即同步到币安 algoOrder
4. 每分钟循环：自动把最新的 TP/SL 同步到币安，避免推送更新后交易所订单没跟上
   - 只有止盈止损真的发生变化时才重挂，避免每分钟无意义撤单
5. 多 TP 分档挂单：tp=[TP1, TP2, TP3] 会拆成 3 个 TAKE_PROFIT_MARKET
   - 默认比例：TP1=50% / TP2=30% / TP3=20%
   - 最后一档会吃掉剩余数量，避免数量精度造成残留仓位失去保护
6. CLOSE：手动全平

安全说明：
- 不要在代码里写死 API Key，改用环境变量：
    BINANCE_API_KEY
    BINANCE_API_SECRET
    BINANCE_ENV=MAIN 或 TEST
- 建议 API Key 只开合约交易权限，禁止提现，并配置 IP 白名单。

离线演练：
- 设置 BINANCE_MOCK=1 时不会访问真实币安，使用内置模拟撮合，用于验证流程。
"""

import hashlib
import hmac
import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

try:
    import pytz
except ImportError:
    pytz = None

# =========================
# 路径与日志
# =========================
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_FILE = BASE_DIR / "signals.jsonl"
STATE_FILE = BASE_DIR / "signal_executor_state.json"
PROCESSED_FILE = BASE_DIR / "processed_signals.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "signal_executor_single.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("signal_executor_single")


def now_text() -> str:
    """当前时间文本，优先上海时区。"""
    if pytz is not None:
        try:
            return datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> Dict[str, Any]:
    env = os.getenv("BINANCE_ENV", "MAIN").upper()
    if env not in ("MAIN", "TEST"):
        env = "MAIN"

    base_url = (
        "https://testnet.binancefuture.com"
        if env == "TEST"
        else "https://fapi.binance.com"
    )

    return {
        "env": env,
        "base_url": base_url,
        "api_key": os.getenv("BINANCE_API_KEY", ""),
        "api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "mock": os.getenv("BINANCE_MOCK", "0") == "1",
        "default_tp_pct": Decimal(os.getenv("DEFAULT_TP_PCT", "0.07")),
        "default_sl_pct": Decimal(os.getenv("DEFAULT_SL_PCT", "0.07")),
        "default_leverage": int(os.getenv("DEFAULT_LEVERAGE", "10")),
        "default_usdt": Decimal(os.getenv("DEFAULT_USDT", "20")),
        "poll_seconds": int(os.getenv("POLL_SECONDS", "60")),
        # 多 TP 分档比例：TP1=50% / TP2=30% / TP3=20%
        "tp_ratios": {
            1: Decimal(os.getenv("TP1_RATIO", "0.50")),
            2: Decimal(os.getenv("TP2_RATIO", "0.30")),
            3: Decimal(os.getenv("TP3_RATIO", "0.20")),
        },
        "min_notional": Decimal(os.getenv("MIN_NOTIONAL", "100")),
        "email": {
            "enabled": os.getenv("EMAIL_ENABLED", "1") == "1",
            "from": os.getenv("MONITOR_EMAIL_FROM", "a1247504226@163.com"),
            "to": os.getenv("MONITOR_EMAIL_TO", "1247504226@qq.com"),
            # 163 邮箱这里填“SMTP授权码”，不是网页登录密码
            "password": os.getenv("MONITOR_EMAIL_PASSWORD", "XKCINXNMOMMDCAFI"),
            "server": os.getenv("MONITOR_EMAIL_SERVER", "smtp.163.com"),
            "port": int(os.getenv("MONITOR_EMAIL_PORT", "465")),
            "max_retries": 3,
            "test_on_start": os.getenv("EMAIL_TEST_ON_START", "0") == "1",
        },
    }


CONFIG = load_config()

# 通知冷却：避免同一个异常在每分钟循环里反复刷邮件
_notify_last: Dict[str, float] = {}
_notify_lock = threading.Lock()
NOTIFY_COOLDOWN_SECONDS = int(os.getenv("NOTIFY_COOLDOWN_SECONDS", "120"))


def notify(subject: str, body: str, cooldown_key: Optional[str] = None, force: bool = False) -> None:
    """发送通知邮件。默认异步发送，避免阻塞交易循环。"""
    email_cfg = CONFIG.get("email", {})
    if not email_cfg.get("enabled"):
        return

    if not email_cfg.get("from") or not email_cfg.get("to") or not email_cfg.get("password"):
        logger.warning("邮件配置不完整，跳过通知")
        return

    key = cooldown_key or subject

    if not force:
        now = time.time()
        with _notify_lock:
            last = _notify_last.get(key, 0)
            if now - last < NOTIFY_COOLDOWN_SECONDS:
                logger.debug("通知冷却中，跳过: %s", key)
                return
            _notify_last[key] = now

    def _worker() -> None:
        try:
            _send_email(subject, body)
        except Exception as exc:
            logger.error("通知邮件发送失败: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()


def _send_email(subject: str, body: str) -> None:
    email_cfg = CONFIG["email"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]
    msg["Subject"] = subject

    for attempt in range(email_cfg["max_retries"]):
        try:
            with smtplib.SMTP_SSL(email_cfg["server"], email_cfg["port"]) as server:
                server.login(email_cfg["from"], email_cfg["password"])
                server.sendmail(email_cfg["from"], email_cfg["to"], msg.as_string())
            logger.info("通知邮件已发送: %s", subject)
            return
        except Exception as exc:
            logger.error(
                "邮件发送失败 第%s/%s次: %s",
                attempt + 1,
                email_cfg["max_retries"],
                exc,
            )
            time.sleep(3)

MIN_NOTIONAL_DEFAULT = Decimal("100")
_session = requests.Session()
_symbol_cache: Dict[str, Dict[str, Decimal]] = {}


# =========================
# 状态持久化
# =========================
def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"positions": {}, "last_tpsl": {}, "processed": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": {}, "last_tpsl": {}, "processed": []}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_processed() -> set:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_processed(processed: set) -> None:
    PROCESSED_FILE.write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_new_signals(processed: set) -> List[Dict[str, Any]]:
    if not SIGNAL_FILE.exists():
        return []

    signals: List[Dict[str, Any]] = []
    with SIGNAL_FILE.open("r", encoding="utf-8") as file_obj:
        for line in file_obj:
            line = line.strip()
            if not line:
                continue
            try:
                sig = json.loads(line)
            except Exception:
                continue

            sig_id = sig.get("id") or json.dumps(sig, sort_keys=True, ensure_ascii=False)
            if sig_id in processed:
                continue

            sig["_id"] = sig_id
            signals.append(sig)

    return signals


# =========================
# 请求封装（真实 / 模拟）
# =========================
class MockBinance:
    """极简模拟撮合，仅用于离线验证流程，不访问网络。"""

    def __init__(self) -> None:
        self.positions: Dict[str, Decimal] = {}
        self.algo_orders: Dict[str, List[dict]] = {}
        self.algo_id = 1

    def _next_id(self) -> int:
        self.algo_id += 1
        return self.algo_id

    def request(self, method: str, endpoint: str, params: dict, signed: bool):
        symbol = params.get("symbol")

        if endpoint == "/fapi/v1/ticker/price":
            return {"price": "100"}

        if endpoint == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": symbol,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "100"},
                        ],
                    }
                ]
            }

        if endpoint == "/fapi/v1/leverage":
            return {"leverage": params.get("leverage")}

        if endpoint == "/fapi/v1/order":
            qty = Decimal(str(params["quantity"]))
            signed_qty = qty if params.get("side") == "BUY" else -qty
            current = self.positions.get(symbol, Decimal("0"))
            updated = current + signed_qty

            # 若方向翻转，先按平仓处理，再开新仓
            if current != 0 and updated != 0 and (current > 0) != (updated > 0):
                updated = signed_qty

            self.positions[symbol] = updated
            return {"orderId": self._next_id(), "status": "FILLED"}

        if endpoint == "/fapi/v1/algoOrder" and method == "POST":
            order = {
                "algoId": str(self._next_id()),
                "symbol": symbol,
                "type": params.get("type"),
                "triggerPrice": params.get("triggerPrice"),
                "quantity": params.get("quantity"),
                "closePosition": params.get("closePosition"),
            }
            self.algo_orders.setdefault(symbol, []).append(order)
            return order

        if endpoint == "/fapi/v1/openAlgoOrders":
            return self.algo_orders.get(symbol, [])

        if endpoint == "/fapi/v1/algoOrder" and method == "DELETE":
            algo_id = str(params.get("algoId"))
            orders = self.algo_orders.get(symbol, [])
            self.algo_orders[symbol] = [o for o in orders if str(o.get("algoId")) != algo_id]
            return {"algoId": algo_id}

        if endpoint == "/fapi/v2/positionRisk":
            result = []
            for sym, qty in self.positions.items():
                result.append({"symbol": sym, "positionAmt": str(qty)})
            return result

        return {}


MOCK = MockBinance() if CONFIG["mock"] else None


def _sign(params: dict) -> str:
    return hmac.new(
        CONFIG["api_secret"].encode(),
        urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()


def _request(method: str, endpoint: str, params: Optional[dict] = None, signed: bool = True):
    params = dict(params or {})

    if CONFIG["mock"]:
        return MOCK.request(method, endpoint, params, signed)

    if signed:
        if not CONFIG["api_key"] or not CONFIG["api_secret"]:
            raise RuntimeError("未配置 BINANCE_API_KEY / BINANCE_API_SECRET")
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)

    headers = {"X-MBX-APIKEY": CONFIG["api_key"]} if signed else {}

    last_error = None
    for attempt in range(3):
        try:
            resp = _session.request(
                method,
                CONFIG["base_url"] + endpoint,
                headers=headers,
                params=params,
                timeout=15,
            )
            data = resp.json()

            if isinstance(data, dict) and "code" in data and data["code"] < 0:
                raise RuntimeError(f"Binance Error {data['code']}: {data.get('msg')}")

            return data
        except Exception as exc:
            last_error = exc
            logger.warning("请求失败 %s %s 第%s次: %s", method, endpoint, attempt + 1, exc)
            time.sleep(1 + attempt)

    raise RuntimeError(f"请求最终失败: {method} {endpoint} | {last_error}")


# =========================
# 交易所精度
# =========================
def get_symbol_filters(symbol: str) -> Dict[str, Decimal]:
    if symbol in _symbol_cache:
        return _symbol_cache[symbol]

    info = _request("GET", "/fapi/v1/exchangeInfo", params={"symbol": symbol}, signed=False)

    for item in info.get("symbols", []):
        if item.get("symbol") != symbol:
            continue

        filters = {f["filterType"]: f for f in item.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_size = filters.get("LOT_SIZE", {})
        min_notional = filters.get("MIN_NOTIONAL", {})

        data = {
            "tick": Decimal(str(price_filter.get("tickSize", "0"))),
            "step": Decimal(str(lot_size.get("stepSize", "0"))),
            "min_qty": Decimal(str(lot_size.get("minQty", "0"))),
            "min_notional": Decimal(str(min_notional.get("notional", MIN_NOTIONAL_DEFAULT))),
        }
        _symbol_cache[symbol] = data
        return data

    raise RuntimeError(f"未找到交易对: {symbol}")


def round_by_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def get_last_price(symbol: str) -> Decimal:
    data = _request("GET", "/fapi/v1/ticker/price", params={"symbol": symbol}, signed=False)
    return Decimal(str(data["price"]))


# =========================
# 风控与数量
# =========================
def risk_check(direction: str, price: Decimal, tp: Decimal, sl: Decimal) -> None:
    if direction == "LONG" and not (tp > price > sl):
        raise RuntimeError(f"LONG 参数错误 tp={tp} price={price} sl={sl}")
    if direction == "SHORT" and not (tp < price < sl):
        raise RuntimeError(f"SHORT 参数错误 tp={tp} price={price} sl={sl}")


def calc_qty(symbol: str, usdt: Decimal, price: Decimal, leverage: int) -> Decimal:
    filters = get_symbol_filters(symbol)
    notional = usdt * Decimal(leverage)
    qty = round_by_step(notional / price, filters["step"])

    if qty < filters["min_qty"]:
        qty = filters["min_qty"]

    if qty * price < filters["min_notional"]:
        qty = round_by_step(filters["min_notional"] / price, filters["step"])
        logger.warning("%s 名义价值不足，自动调整 qty=%s", symbol, qty)

    return qty


def get_position_qty(symbol: str) -> Decimal:
    positions = _request("GET", "/fapi/v2/positionRisk")
    for item in positions:
        if item.get("symbol") == symbol:
            return abs(Decimal(str(item.get("positionAmt", "0"))))
    return Decimal("0")


# =========================
# 下单与 algoOrder
# =========================
def set_leverage(symbol: str, leverage: int) -> None:
    _request("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage})


def place_market_order(symbol: str, side: str, qty: Decimal) -> dict:
    return _request(
        "POST",
        "/fapi/v1/order",
        params={"symbol": symbol, "side": side, "type": "MARKET", "quantity": str(qty)},
    )


def close_position_quantity(symbol: str, target_qty: Decimal) -> bool:
    """按指定数量平仓，用于 TP 分档精确平仓。"""
    try:
        return _close_position_quantity_inner(symbol, target_qty)
    except Exception as exc:
        logger.error("按数量平仓失败 | %s | err=%s", symbol, exc)
        notify(
            subject=f"[失败] 分档平仓失败 {symbol}",
            body=(
                "分档平仓异常\n"
                f"时间: {now_text()}\n"
                f"标的: {symbol}\n"
                f"目标平仓数量: {target_qty}\n"
                f"错误: {exc}\n"
            ),
            cooldown_key=f"close_qty_fail:{symbol}",
        )
        return False


def _close_position_quantity_inner(symbol: str, target_qty: Decimal) -> bool:
    qty = get_position_qty(symbol)
    if qty == 0:
        logger.warning("%s 无持仓，无需平仓", symbol)
        return False

    state = load_state()
    position = state.get("positions", {}).get(symbol)
    direction = position["direction"] if position else "LONG"
    close_side = "SELL" if direction == "LONG" else "BUY"
    filters = get_symbol_filters(symbol)

    target_qty = round_by_step(target_qty, filters["step"])

    if target_qty <= 0:
        logger.warning("%s 平仓数量为 0，跳过", symbol)
        return False

    # 不允许平掉超过实际持仓的数量
    if target_qty > qty:
        target_qty = qty

    place_market_order(symbol, close_side, target_qty)
    logger.info("%s 按数量平仓完成，数量=%s", symbol, target_qty)

    remain = get_position_qty(symbol)

    if remain == 0:
        cancel_symbol_algo_orders(symbol)
        state = load_state()
        state.get("positions", {}).pop(symbol, None)
        save_state(state)

    notify(
        subject=f"[分档平仓成功] {symbol}",
        body=(
            "已按分档数量平仓\n"
            f"时间: {now_text()}\n"
            f"标的: {symbol}\n"
            f"方向: {direction}\n"
            f"平仓数量: {target_qty}\n"
            f"剩余持仓: {remain}\n"
        ),
        cooldown_key=f"close_qty_ok:{symbol}",
    )
    return True


def place_algo_order(
    symbol: str,
    side: str,
    algo_type: str,
    trigger_price: Decimal,
    quantity: Optional[Decimal] = None,
    close_position: bool = False,
) -> dict:
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "positionSide": "BOTH",
        "type": algo_type,
        "triggerPrice": str(trigger_price),
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE",
    }

    if close_position:
        params["closePosition"] = "true"
    else:
        params["quantity"] = str(quantity)

    return _request("POST", "/fapi/v1/algoOrder", params=params)


def get_open_algo_orders(symbol: str) -> List[dict]:
    return _request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": symbol})


def cancel_algo_order(symbol: str, algo_id: str) -> dict:
    return _request("DELETE", "/fapi/v1/algoOrder", params={"symbol": symbol, "algoId": algo_id})


def cancel_symbol_algo_orders(symbol: str) -> int:
    orders = get_open_algo_orders(symbol)
    count = 0
    for order in orders:
        try:
            cancel_algo_order(symbol, str(order.get("algoId")))
            count += 1
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("%s 撤销 algoOrder 失败 algoId=%s err=%s", symbol, order.get("algoId"), exc)
    return count


def tp_ratios_for_levels(count: int) -> List[Decimal]:
    """根据 TP 档位数量返回每档平仓比例。默认 50% / 30% / 20%。"""
    if count <= 0:
        return []

    ratios = [CONFIG["tp_ratios"].get(i) for i in range(1, count + 1)]
    ratios = [Decimal(str(r)) if r is not None else Decimal("0") for r in ratios]

    total = sum(ratios)
    if total <= 0:
        # 未配置时按档位均分
        ratios = [Decimal("1") / Decimal(count) for _ in range(count)]
        total = sum(ratios)

    if total > Decimal("1"):
        logger.warning("TP 比例合计 %.2f%% 超过 100%%，按比例归一化", float(total) * 100)
        ratios = [r / total for r in ratios]

    return ratios


def tpsl_fingerprint(tp_list: List[float], sl_list: List[float]) -> str:
    """生成止盈止损指纹，用于判断是否真的需要同步到币安。"""
    return json.dumps(
        {
            "tp": [str(x) for x in tp_list],
            "sl": [str(x) for x in sl_list],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def build_default_tpsl(direction: str, price: Decimal) -> Tuple[Decimal, Decimal]:
    if direction == "LONG":
        return (
            price * (Decimal("1") + CONFIG["default_tp_pct"]),
            price * (Decimal("1") - CONFIG["default_sl_pct"]),
        )
    return (
        price * (Decimal("1") - CONFIG["default_tp_pct"]),
        price * (Decimal("1") + CONFIG["default_sl_pct"]),
    )


def place_trade(
    symbol: str,
    usdt: float,
    direction: str,
    leverage: int,
    take_profit: float,
    stop_loss: float,
) -> dict:
    try:
        return _place_trade_inner(
            symbol=symbol,
            usdt=usdt,
            direction=direction,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
    except Exception as exc:
        logger.error("下单失败 | %s | %s | err=%s", symbol, direction, exc)
        notify(
            subject=f"[失败] 下单失败 {symbol} {direction}",
            body=(
                "交易执行异常\n"
                f"时间: {now_text()}\n"
                f"标的: {symbol}\n"
                f"方向: {direction}\n"
                f"杠杆: {leverage}x\n"
                f"计划投入: {usdt} USDT\n"
                f"计划止盈: {take_profit}\n"
                f"计划止损: {stop_loss}\n"
                f"错误: {exc}\n"
            ),
            cooldown_key=f"place_trade_fail:{symbol}:{direction}",
        )
        raise


def _place_trade_inner(
    symbol: str,
    usdt: float,
    direction: str,
    leverage: int,
    take_profit: float,
    stop_loss: float,
) -> dict:
    logger.info("下单开始 | %s | %s", symbol, direction)

    price = get_last_price(symbol)
    tp = Decimal(str(take_profit))
    sl = Decimal(str(stop_loss))

    risk_check(direction, price, tp, sl)

    filters = get_symbol_filters(symbol)
    tp = round_by_step(tp, filters["tick"])
    sl = round_by_step(sl, filters["tick"])

    qty = calc_qty(symbol, Decimal(str(usdt)), price, leverage)

    set_leverage(symbol, leverage)

    open_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    place_market_order(symbol, open_side, qty)
    time.sleep(0.5)

    if get_position_qty(symbol) == 0:
        raise RuntimeError("开仓失败，当前无持仓")

    cancel_symbol_algo_orders(symbol)

    place_algo_order(
        symbol=symbol,
        side=close_side,
        algo_type="STOP_MARKET",
        trigger_price=sl,
        close_position=True,
    )

    place_algo_order(
        symbol=symbol,
        side=close_side,
        algo_type="TAKE_PROFIT_MARKET",
        trigger_price=tp,
        close_position=True,
    )

    state = load_state()
    state.setdefault("positions", {})[symbol] = {
        "direction": direction,
        "entry_price": str(price),
        "qty": str(qty),
        "tp": str(tp),
        "sl": str(sl),
        "opened_at": time.time(),
        "tp_done": [],
    }
    save_state(state)

    logger.info("下单完成 | %s | TP=%s | SL=%s | QTY=%s", symbol, tp, sl, qty)

    notify(
        subject=f"[开仓成功] {symbol} {direction}",
        body=(
            "已按信号开仓\n"
            f"时间: {now_text()}\n"
            f"标的: {symbol}\n"
            f"方向: {direction}\n"
            f"杠杆: {leverage}x\n"
            f"名义投入: {usdt} USDT\n"
            f"开仓价: {price}\n"
            f"数量: {qty}\n"
            f"止盈: {tp}\n"
            f"止损: {sl}\n"
        ),
        cooldown_key=f"place_trade_ok:{symbol}:{direction}",
    )

    return {"success": True, "symbol": symbol, "tp": str(tp), "sl": str(sl), "qty": str(qty)}


def update_tpsl(symbol: str, tp_list: List[float], sl_list: List[float]) -> bool:
    try:
        return _update_tpsl_inner(symbol, tp_list, sl_list)
    except Exception as exc:
        logger.error("止盈止损更新失败 | %s | err=%s", symbol, exc)
        notify(
            subject=f"[失败] 止盈止损更新失败 {symbol}",
            body=(
                "止盈止损同步异常\n"
                f"时间: {now_text()}\n"
                f"标的: {symbol}\n"
                f"目标止盈: {tp_list}\n"
                f"目标止损: {sl_list}\n"
                f"错误: {exc}\n"
                "注意：当前币安条件单可能仍是旧值，请尽快人工核对。\n"
            ),
            cooldown_key=f"update_tpsl_fail:{symbol}",
        )
        return False


def _update_tpsl_inner(symbol: str, tp_list: List[float], sl_list: List[float]) -> bool:
    state = load_state()
    position = state.get("positions", {}).get(symbol)

    if not position:
        logger.warning("%s 无本地持仓记录，跳过 TP/SL 更新", symbol)
        return False

    if get_position_qty(symbol) == 0:
        logger.warning("%s 实际无持仓，跳过 TP/SL 更新", symbol)
        return False

    if not tp_list and not sl_list:
        return False

    direction = position["direction"]
    close_side = "SELL" if direction == "LONG" else "BUY"
    filters = get_symbol_filters(symbol)

    cancel_symbol_algo_orders(symbol)
    time.sleep(0.3)

    if sl_list:
        sl = round_by_step(Decimal(str(sl_list[0])), filters["tick"])
        place_algo_order(
            symbol=symbol,
            side=close_side,
            algo_type="STOP_MARKET",
            trigger_price=sl,
            close_position=True,
        )
        logger.info("%s 更新止损 SL=%s", symbol, sl)
    else:
        sl = Decimal(position.get("sl", "0"))

    if tp_list:
        tp_values = [Decimal(str(x)) for x in tp_list]
        ratios = tp_ratios_for_levels(len(tp_values))
        total_qty = Decimal(position.get("qty", "0"))

        if total_qty <= 0:
            total_qty = get_position_qty(symbol)

        placed_qty = Decimal("0")
        tp_orders = []

        for idx, (raw_tp, ratio) in enumerate(zip(tp_values, ratios), start=1):
            tp = round_by_step(raw_tp, filters["tick"])

            if idx == len(tp_values):
                # 最后一档吃掉剩余数量，避免数量精度导致残留仓位失去保护
                qty = total_qty - placed_qty
            else:
                qty = round_by_step(total_qty * ratio, filters["step"])

            if qty < filters["min_qty"]:
                logger.warning("%s TP%s 数量过小，跳过该档", symbol, idx)
                continue

            if qty <= 0:
                continue

            place_algo_order(
                symbol=symbol,
                side=close_side,
                algo_type="TAKE_PROFIT_MARKET",
                trigger_price=tp,
                quantity=qty,
                close_position=False,
            )
            placed_qty += qty
            tp_orders.append({"level": idx, "tp": str(tp), "qty": str(qty)})
            logger.info("%s 挂 TP%s | 价格=%s | 数量=%s | 比例=%.2f%%", symbol, idx, tp, qty, float(ratio) * 100)

        tp = round_by_step(tp_values[0], filters["tick"])
    else:
        tp = Decimal(position.get("tp", "0"))
        tp_orders = []
        placed_qty = Decimal("0")

    state = load_state()
    state["positions"][symbol]["tp"] = str(tp)
    state["positions"][symbol]["sl"] = str(sl)
    if tp_orders:
        state["positions"][symbol]["tp_orders"] = tp_orders
        state["positions"][symbol]["tp_covered_qty"] = str(placed_qty)
    state["positions"][symbol]["tpsl_fingerprint"] = tpsl_fingerprint(tp_list, sl_list)
    state.setdefault("last_tpsl", {})[symbol] = {
        "tp": [str(x) for x in tp_list],
        "sl": [str(x) for x in sl_list],
        "updated_at": time.time(),
    }
    save_state(state)

    tp_detail = "\n".join(
        f"TP{o['level']}: 价格={o['tp']} 数量={o['qty']}"
        for o in tp_orders
    ) or "无"

    notify(
        subject=f"[止盈止损已更新] {symbol}",
        body=(
            "已同步止盈止损到币安\n"
            f"时间: {now_text()}\n"
            f"标的: {symbol}\n"
            f"方向: {direction}\n"
            f"止损: {sl}\n"
            f"止盈分档:\n{tp_detail}\n"
        ),
        cooldown_key=f"update_tpsl_ok:{symbol}",
    )
    return True


def close_position(symbol: str, ratio: Optional[Decimal] = None) -> bool:
    try:
        return _close_position_inner(symbol, ratio)
    except Exception as exc:
        logger.error("平仓失败 | %s | err=%s", symbol, exc)
        notify(
            subject=f"[失败] 平仓失败 {symbol}",
            body=(
                "平仓异常\n"
                f"时间: {now_text()}\n"
                f"标的: {symbol}\n"
                f"平仓比例: {'全部' if ratio is None else ratio}\n"
                f"错误: {exc}\n"
                "注意：请尽快人工检查持仓，可能需要手动平仓。\n"
            ),
            cooldown_key=f"close_fail:{symbol}",
        )
        return False


def _close_position_inner(symbol: str, ratio: Optional[Decimal] = None) -> bool:
    qty = get_position_qty(symbol)
    if qty == 0:
        logger.warning("%s 无持仓，无需平仓", symbol)
        return False

    state = load_state()
    position = state.get("positions", {}).get(symbol)
    direction = position["direction"] if position else "LONG"
    close_side = "SELL" if direction == "LONG" else "BUY"
    filters = get_symbol_filters(symbol)

    if ratio is None:
        target_qty = qty
    else:
        target_qty = round_by_step(qty * ratio, filters["step"])
        if target_qty < filters["min_qty"]:
            target_qty = qty

    place_market_order(symbol, close_side, target_qty)
    logger.info("%s 平仓完成，数量=%s", symbol, target_qty)

    remain = get_position_qty(symbol)

    if remain == 0:
        cancel_symbol_algo_orders(symbol)
        state = load_state()
        state.get("positions", {}).pop(symbol, None)
        save_state(state)

    notify(
        subject=f"[平仓成功] {symbol}",
        body=(
            "已执行平仓\n"
            f"时间: {now_text()}\n"
            f"标的: {symbol}\n"
            f"方向: {direction}\n"
            f"平仓数量: {target_qty}\n"
            f"剩余持仓: {remain}\n"
        ),
        cooldown_key=f"close_ok:{symbol}",
    )
    return True


def get_tp_level_qty(symbol: str, tp_level: int) -> Optional[Decimal]:
    """优先使用已挂 TP 分档里记录的数量，避免部分成交后按比例算错。"""
    state = load_state()
    position = state.get("positions", {}).get(symbol)

    if not position:
        return None

    for order in position.get("tp_orders", []):
        if int(order.get("level", 0)) == tp_level:
            return Decimal(str(order.get("qty", "0")))

    return None


def close_position_by_tp_level(symbol: str, tp_level: int) -> bool:
    state = load_state()
    position = state.get("positions", {}).get(symbol)

    if not position:
        logger.warning("%s 无持仓记录，无法按 TP%s 平仓", symbol, tp_level)
        return False

    tp_done = set(position.get("tp_done", []))
    if tp_level in tp_done:
        logger.info("%s TP%s 已处理，忽略重复推送", symbol, tp_level)
        return False

    stored_qty = get_tp_level_qty(symbol, tp_level)

    if stored_qty is not None and stored_qty > 0:
        ok = close_position_quantity(symbol, stored_qty)
    else:
        ratio = CONFIG["tp_ratios"].get(tp_level)
        if ratio is None:
            logger.warning("%s 未知 TP 级别 TP%s，默认全平", symbol, tp_level)
            ratio = Decimal("1")
        ok = close_position(symbol, ratio)

    if ok:
        state = load_state()
        state["positions"][symbol].setdefault("tp_done", []).append(tp_level)
        save_state(state)

    return ok


# =========================
# 信号处理
# =========================
def handle_signal(signal: Dict[str, Any]) -> bool:
    sig_type = str(signal.get("type", "")).upper()
    symbol = signal.get("symbol")
    side = signal.get("side")
    tp_list = signal.get("tp") or []
    sl_list = signal.get("sl") or []
    add_list = signal.get("add") or []
    tp_level = signal.get("tp_level")

    logger.info("收到信号: %s", {k: v for k, v in signal.items() if k != "_id"})

    if sig_type == "OPEN":
        if not symbol or not side:
            logger.warning("OPEN 信号缺少 symbol/side，忽略")
            return False

        side = str(side).upper()
        if side not in ("LONG", "SHORT"):
            logger.warning("OPEN 信号 side 非法: %s", side)
            return False

        if get_position_qty(symbol) > 0:
            logger.info("%s 已有持仓，跳过开仓", symbol)
            return False

        state = load_state()
        last_tpsl = state.get("last_tpsl", {}).get(symbol, {})
        price = get_last_price(symbol)

        if tp_list and sl_list:
            tp = Decimal(str(tp_list[0]))
            sl = Decimal(str(sl_list[0]))
        elif last_tpsl.get("tp") and last_tpsl.get("sl"):
            tp = Decimal(str(last_tpsl["tp"][0]))
            sl = Decimal(str(last_tpsl["sl"][0]))
        else:
            tp, sl = build_default_tpsl(side, price)

        result = place_trade(
            symbol=symbol,
            usdt=float(CONFIG["default_usdt"]),
            direction=side,
            leverage=CONFIG["default_leverage"],
            take_profit=float(tp),
            stop_loss=float(sl),
        )
        return bool(result.get("success"))

    if sig_type == "SET_TPSL":
        state = load_state()

        if symbol:
            state.setdefault("last_tpsl", {})[symbol] = {
                "tp": [str(x) for x in tp_list],
                "sl": [str(x) for x in sl_list],
                "add": [str(x) for x in add_list],
                "updated_at": time.time(),
            }
            save_state(state)
            return update_tpsl(symbol, tp_list, sl_list)

        ok = False
        for sym in list(state.get("positions", {}).keys()):
            state.setdefault("last_tpsl", {})[sym] = {
                "tp": [str(x) for x in tp_list],
                "sl": [str(x) for x in sl_list],
                "add": [str(x) for x in add_list],
                "updated_at": time.time(),
            }
            ok = update_tpsl(sym, tp_list, sl_list) or ok
        save_state(state)
        return ok

    if sig_type in ("TP", "TP_HIT", "TAKE_PROFIT"):
        if not symbol:
            logger.warning("TP 信号缺少 symbol，忽略")
            return False
        level = int(tp_level or 1)
        return close_position_by_tp_level(symbol, level)

    if sig_type in ("CLOSE", "EXIT", "CLOSE_ALL", "MANUAL_CLOSE"):
        if not symbol:
            logger.warning("CLOSE 信号缺少 symbol，忽略")
            return False
        return close_position(symbol)

    logger.warning("未识别的信号类型: %s", sig_type)
    return False


def sync_open_positions(force: bool = False) -> None:
    """每分钟同步：只在止盈止损真的变化时，才撤销旧单并重挂新单。"""
    state = load_state()

    for symbol in list(state.get("positions", {}).keys()):
        try:
            if get_position_qty(symbol) == 0:
                continue

            last = state.get("last_tpsl", {}).get(symbol)
            if not last:
                continue

            tp_list = [float(x) for x in last.get("tp", [])]
            sl_list = [float(x) for x in last.get("sl", [])]

            if not tp_list and not sl_list:
                continue

            fingerprint = tpsl_fingerprint(tp_list, sl_list)
            position = state["positions"].get(symbol, {})

            if not force and position.get("tpsl_fingerprint") == fingerprint:
                logger.debug("%s 止盈止损未变化，跳过同步", symbol)
                continue

            update_tpsl(symbol, tp_list, sl_list)
        except Exception as exc:
            logger.error("同步 %s 止盈止损失败: %s", symbol, exc)
            notify(
                subject=f"[失败] 每分钟同步失败 {symbol}",
                body=(
                    "止盈止损同步循环异常\n"
                    f"时间: {now_text()}\n"
                    f"标的: {symbol}\n"
                    f"错误: {exc}\n"
                ),
                cooldown_key=f"sync_fail:{symbol}",
            )


def run_once() -> None:
    """单轮：先消费新推送，再同步持仓 TP/SL。"""
    processed = load_processed()

    signals = read_new_signals(processed)
    for sig in signals:
        try:
            handle_signal(sig)
            processed.add(sig["_id"])
            save_processed(processed)
        except Exception as exc:
            logger.error("处理信号失败: %s | err=%s", sig, exc)
            notify(
                subject=f"[失败] 信号处理失败 {sig.get('symbol') or '未知标的'}",
                body=(
                    "推送信号处理异常\n"
                    f"时间: {now_text()}\n"
                    f"信号类型: {sig.get('type')}\n"
                    f"标的: {sig.get('symbol')}\n"
                    f"方向: {sig.get('side')}\n"
                    f"止盈: {sig.get('tp')}\n"
                    f"止损: {sig.get('sl')}\n"
                    f"TP档位: {sig.get('tp_level')}\n"
                    f"错误: {exc}\n"
                ),
                cooldown_key=f"signal_fail:{sig.get('symbol')}:{sig.get('type')}",
            )

    sync_open_positions()


def main() -> None:
    poll_seconds = max(5, CONFIG["poll_seconds"])

    logger.info("=" * 60)
    logger.info("推送信号执行器启动")
    logger.info("ENV=%s | MOCK=%s | 轮询间隔=%s秒", CONFIG["env"], CONFIG["mock"], poll_seconds)
    logger.info("信号文件=%s", SIGNAL_FILE)
    logger.info("状态文件=%s", STATE_FILE)
    logger.info("=" * 60)

    if not CONFIG["mock"] and (not CONFIG["api_key"] or not CONFIG["api_secret"]):
        logger.error("未配置 BINANCE_API_KEY / BINANCE_API_SECRET，退出")
        return

    notify(
        subject="[启动] 推送信号执行器已启动",
        body=(
            "执行器已启动\n"
            f"时间: {now_text()}\n"
            f"环境: {CONFIG['env']}\n"
            f"模拟模式: {CONFIG['mock']}\n"
            f"轮询间隔: {poll_seconds} 秒\n"
            f"默认止盈比例: {CONFIG['default_tp_pct']}\n"
            f"默认止损比例: {CONFIG['default_sl_pct']}\n"
        ),
        cooldown_key="system_start",
        force=True,
    )

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            notify(
                subject="[停止] 推送信号执行器已停止",
                body=f"执行器已手动停止\n时间: {now_text()}\n",
                cooldown_key="system_stop",
                force=True,
            )
            break
        except Exception as exc:
            logger.error("主循环异常: %s", exc)
            notify(
                subject="[失败] 执行器主循环异常",
                body=(
                    "主循环发生异常\n"
                    f"时间: {now_text()}\n"
                    f"错误: {exc}\n"
                ),
                cooldown_key="main_loop_error",
            )

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
