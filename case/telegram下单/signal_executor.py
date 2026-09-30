#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安 U 本位合约 · 推送信号执行器（单文件 · 生产版 v2）
=========================================================

对应你的业务流：
    1) OPEN      收到「#ETH 现价空」 -> 市价开仓，默认 ±7% 作为临时止盈止损
    2) SET_TPSL  收到「止盈：1.32-1.39 止损：1.195」-> 撤掉旧条件单，按新价重挂
    3) TP        收到「#BB TP1止盈」-> 手动平掉该档仓位（并防止与交易所条件单重复平仓）
    4) CLOSE     收到平仓消息 -> 全平

相对原版的关键修正（详见文末 CHANGELOG）：
    * [致命] TP 条件单缺 reduceOnly，触发后可能反向开仓
    * [致命] 开仓时 TP 用 closePosition=true，会把分档止盈变成一次性全平
    * [致命] TP 推送与交易所条件单重复平仓（同一档被平两次 / 平错档位）
    * [致命] cancel_symbol_algo_orders 会连用户手动挂的条件单一并撤掉
    * [严重] 无法追踪自己挂的单（没有 clientAlgoId），无法判断某档是否已成交
    * [严重] 每分钟「撤单重挂」造成无保护窗口 + 频繁触发交易所限频
    * [严重] 时间戳不校时，易触发 -1021
    * [严重] 状态文件非原子写入，崩溃即损坏
    * [一般] 信号文件每轮全量重读，O(n) 膨胀；processed 集合无上限
    * [一般] 开仓后 0.5s 就查持仓，偶发「开仓失败」误判
    * [一般] 补仓信号 add 被完全忽略
    * [一般] 邮件密码硬编码在源码里

安全说明：
    * 不要写死 API Key，一律走环境变量
    * API Key 只开「合约交易」权限，禁止提现，务必配 IP 白名单
    * BINANCE_MOCK=1 走内置模拟撮合，不访问网络，用于离线演练

运行：
    export BINANCE_API_KEY=...
    export BINANCE_API_SECRET=...
    python signal_executor.py                # 常驻
    python signal_executor.py --once         # 跑一轮就退出（适合 cron / 调试）
    BINANCE_MOCK=1 python signal_executor.py --once
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

VERSION = "2.0.0"


# ==========================================================================
# 0. 工具
# ==========================================================================
def now_text() -> str:
    if pytz is not None:
        try:
            return datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def dec(value: Any, default: str = "0") -> Decimal:
    """
    安全转 Decimal。
    真实推送里的数字用「逗号」当小数点（止盈：0,108-0,11125-0,1145 / 止损：0,098），
    这里做兼容：仅当字符串里没有「.」且恰好一个「,」时，把逗号当小数点。
    """
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        text = value.strip()
        if "." not in text and text.count(",") == 1:
            text = text.replace(",", ".")
        try:
            return Decimal(text)
        except Exception:
            return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def dec_str(value: Decimal) -> str:
    """Decimal -> 交易所可读字符串，去掉科学计数法与多余 0。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def round_by_step(value: Decimal, step: Decimal) -> Decimal:
    if step is None or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_price(value: Decimal, tick: Decimal) -> Decimal:
    """价格按最小变动单位四舍五入（比 ROUND_DOWN 更贴近策略给的原值）。"""
    if tick is None or tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


# ==========================================================================
# 1. 配置
# ==========================================================================
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载：同目录存在 .env 就自动读入，已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or str(BASE_DIR))
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_FILE = Path(os.getenv("SIGNAL_FILE") or str(DATA_DIR / "signals.jsonl"))
STATE_FILE = DATA_DIR / "executor_state.json"
STATE_TMP = DATA_DIR / "executor_state.json.tmp"


def load_config() -> Dict[str, Any]:
    env = os.getenv("BINANCE_ENV", "MAIN").upper()
    if env not in ("MAIN", "TEST"):
        env = "MAIN"

    base_url = (
        "https://testnet.binancefuture.com"
        if env == "TEST"
        else "https://fapi.binance.com"
    )

    mock = os.getenv("BINANCE_MOCK", "0") == "1" or os.getenv("DRY_RUN", "0") == "1"

    return {
        "version": VERSION,
        "env": env,
        "base_url": base_url,
        "api_key": os.getenv("BINANCE_API_KEY", "").strip(),
        "api_secret": os.getenv("BINANCE_API_SECRET", "").strip(),
        "mock": mock,
        "recv_window": int(os.getenv("RECV_WINDOW", "5000")),

        # ---- 仓位与风控 ----
        "default_margin_usdt": dec(os.getenv("DEFAULT_MARGIN_USDT", os.getenv("DEFAULT_USDT", "20"))),
        "default_leverage": int(os.getenv("DEFAULT_LEVERAGE", "10")),
        "default_tp_pct": dec(os.getenv("DEFAULT_TP_PCT", "0.07")),
        "default_sl_pct": dec(os.getenv("DEFAULT_SL_PCT", "0.07")),
        "max_positions": int(os.getenv("MAX_POSITIONS", "10")),
        "max_notional_usdt": dec(os.getenv("MAX_NOTIONAL_USDT", "0")),  # 0 = 不限制

        # ---- 多档止盈比例（按原始仓位的比例）----
        "tp_ratios": {
            1: dec(os.getenv("TP1_RATIO", "0.50")),
            2: dec(os.getenv("TP2_RATIO", "0.30")),
            3: dec(os.getenv("TP3_RATIO", "0.20")),
        },

        # ---- 行为开关 ----
        "signal_poll_seconds": max(1, int(os.getenv("SIGNAL_POLL_SECONDS", "2"))),
        "sync_seconds": max(10, int(os.getenv("SYNC_SECONDS", "60"))),
        "client_id_prefix": os.getenv("CLIENT_ID_PREFIX", "wbsx"),
        "adopt_unknown_positions": os.getenv("ADOPT_UNKNOWN_POSITIONS", "1") == "1",
        "reverse_on_opposite_open": os.getenv("REVERSE_ON_OPPOSITE_OPEN", "1") == "1",
        "enable_add": os.getenv("ENABLE_ADD", "0") == "1",
        "last_tpsl_ttl": int(os.getenv("LAST_TPSL_TTL", "120")),  # 秒
        "working_type": os.getenv("WORKING_TYPE", "MARK_PRICE").upper(),
        "price_protect": os.getenv("PRICE_PROTECT", "1") == "1",
        "min_notional": dec(os.getenv("MIN_NOTIONAL", "5")),
        # 止盈止损价格合理性带宽：偏离现价超过该比例的值一律拒绝（0 = 关闭校验）
        # 主要用于兜底「不带币对的 SET_TPSL」被误用到其它持仓上
        "tpsl_sanity_pct": dec(os.getenv("TPSL_SANITY_PCT", "0.5")),
        # 不带币对的 SET_TPSL（推送里是「引用上一条消息」）如何对应持仓：
        #   REF    = 优先用「上一条带币对的消息」的币对，其次才回退到最近开仓（默认）
        #   LATEST = 只按开仓时间，取最近开仓的那一笔
        #   ALL    = 作用于所有价位匹配的持仓
        "symbolless_tpsl_mode": os.getenv("SYMBOLLESS_TPSL_MODE", "REF").upper(),
        # 哪些消息类型才允许更新「锚点币对」（即不带币对的消息所引用的那个币对）。
        # 默认只有 OPEN（下单）。TP / CLOSE 等消息虽然也带币对，但绝不能让它们把锚点带偏，
        # 否则一条「平仓 SOLUSDT」就会让随后的「止盈：…止损：…」找错标的。
        # 如需把带币对的 SET_TPSL 也算作锚点，改成 "OPEN,SET_TPSL"。
        "symbolless_anchor_types": {
            t.strip().upper()
            for t in os.getenv("SYMBOLLESS_ANCHOR_TYPES", "OPEN").split(",")
            if t.strip()
        },
        # 不带币对的 TP / CLOSE 消息怎么处理。
        #   SKIP   = 直接跳过并记日志（默认）。真实数据里这类消息绝大多数是营销噪音
        #            （「完美止盈，十个点拿下！」「VIP内部群拿下今日止盈第1️⃣单」），
        #            而且夹在大量无关消息之间，锚点推断不可靠。平错仓的代价远大于漏平。
        #   ANCHOR = 用锚点币对推断（仅在你能确认这类消息确实指向上一单时开启）
        "symbolless_tp_close_mode": os.getenv("SYMBOLLESS_TP_CLOSE_MODE", "SKIP").upper(),
        # 是否处理「出补仓」（撤销补仓挂单）
        "enable_cancel_add": os.getenv("ENABLE_CANCEL_ADD", "1") == "1",
        "notify_cooldown_seconds": int(os.getenv("NOTIFY_COOLDOWN_SECONDS", "120")),

        # ---- 邮件（密码必须走环境变量，不再硬编码）----
        "email": {
            "enabled": os.getenv("EMAIL_ENABLED", "1") == "1",
            "sender": os.getenv("MONITOR_EMAIL_FROM", ""),
            "to": os.getenv("MONITOR_EMAIL_TO", ""),
            "password": os.getenv("MONITOR_EMAIL_PASSWORD", ""),
            "server": os.getenv("MONITOR_EMAIL_SERVER", "smtp.163.com"),
            "port": int(os.getenv("MONITOR_EMAIL_PORT", "465")),
            "max_retries": 3,
            "test_on_start": os.getenv("EMAIL_TEST_ON_START", "0") == "1",
        },
    }


CONFIG = load_config()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "signal_executor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("signal_executor")


# ==========================================================================
# 2. 邮件通知
# ==========================================================================
_notify_last: Dict[str, float] = {}
_notify_lock = threading.Lock()


def notify(subject: str, body: str, cooldown_key: Optional[str] = None, force: bool = False) -> None:
    cfg = CONFIG["email"]
    if not cfg.get("enabled"):
        logger.info("[NOTIFY] %s | %s", subject, body.replace("\n", " | "))
        return
    if not cfg.get("sender") or not cfg.get("to") or not cfg.get("password"):
        logger.warning("[NOTIFY-跳过] 邮件配置不完整（缺少 MONITOR_EMAIL_FROM/TO/PASSWORD）：%s", subject)
        return

    key = cooldown_key or subject
    if not force:
        with _notify_lock:
            last = _notify_last.get(key, 0)
            if time.time() - last < CONFIG["notify_cooldown_seconds"]:
                return
            _notify_last[key] = time.time()

    threading.Thread(target=_send_email, args=(subject, body), daemon=True).start()


def _send_email(subject: str, body: str) -> None:
    cfg = CONFIG["email"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["to"]
    msg["Subject"] = subject

    for attempt in range(cfg["max_retries"]):
        try:
            with smtplib.SMTP_SSL(cfg["server"], cfg["port"], timeout=15) as server:
                server.login(cfg["sender"], cfg["password"])
                server.sendmail(cfg["sender"], cfg["to"], msg.as_string())
            logger.info("通知邮件已发送: %s", subject)
            return
        except Exception as exc:
            logger.error("邮件发送失败 第%s/%s次: %s", attempt + 1, cfg["max_retries"], exc)
            time.sleep(3)


# ==========================================================================
# 3. 状态持久化（原子写 + 线程锁）
# ==========================================================================
_state_lock = threading.RLock()
_EMPTY_STATE: Dict[str, Any] = {
    "positions": {},    # symbol -> 持仓上下文
    "last_tpsl": {},    # symbol -> 最近一次收到的止盈止损
    "last_symbol": None,  # 上一条「带币对」的消息里的币对（不带币对的消息靠它对应）
    "cursor": 0,        # signals.jsonl 已消费字节偏移
}


def load_state() -> Dict[str, Any]:
    with _state_lock:
        if not STATE_FILE.exists():
            return json.loads(json.dumps(_EMPTY_STATE))
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("状态文件损坏，使用空状态: %s", exc)
            return json.loads(json.dumps(_EMPTY_STATE))
        for key, default in _EMPTY_STATE.items():
            data.setdefault(key, json.loads(json.dumps(default)))
        return data


def save_state(state: Dict[str, Any]) -> None:
    with _state_lock:
        try:
            STATE_TMP.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(STATE_TMP, STATE_FILE)  # 原子替换，避免写一半崩溃损坏
        except Exception as exc:
            logger.error("状态写入失败: %s", exc)


# ==========================================================================
# 4. HTTP 层：签名 / 校时 / 重试
# ==========================================================================
_session = requests.Session()
_time_offset_ms = 0
_symbol_cache: Dict[str, Dict[str, Decimal]] = {}
_symbol_filters_loaded = False
_dual_side: Optional[bool] = None


def sync_server_time(force: bool = False) -> None:
    """对齐服务器时间，避免 -1021 Timestamp for this request is outside of the recvWindow。"""
    global _time_offset_ms
    if CONFIG["mock"]:
        _time_offset_ms = 0
        return
    if not force and _time_offset_ms != 0:
        return
    try:
        t0 = int(time.time() * 1000)
        data = _request("GET", "/fapi/v1/time", signed=False, _no_retry=True)
        t1 = int(time.time() * 1000)
        _time_offset_ms = int(data["serverTime"]) - (t0 + t1) // 2
        logger.info("服务器时间偏移 %s ms", _time_offset_ms)
    except Exception as exc:
        logger.warning("校时失败，使用本地时间: %s", exc)


def _ts() -> int:
    return int(time.time() * 1000) + _time_offset_ms


def _sign(params: dict) -> str:
    return hmac.new(
        CONFIG["api_secret"].encode(),
        urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()


class BinanceError(RuntimeError):
    def __init__(self, code: Any, msg: str, endpoint: str = ""):
        super().__init__(f"Binance Error {code}: {msg} [{endpoint}]")
        self.code = code
        self.msg = msg


def _request(
    method: str,
    endpoint: str,
    params: Optional[dict] = None,
    signed: bool = True,
    _no_retry: bool = False,
):
    params = {k: v for k, v in dict(params or {}).items() if v is not None}

    if CONFIG["mock"]:
        return MOCK.request(method, endpoint, params, signed)

    if signed:
        if not CONFIG["api_key"] or not CONFIG["api_secret"]:
            raise RuntimeError("未配置 BINANCE_API_KEY / BINANCE_API_SECRET")
        params["timestamp"] = _ts()
        params["recvWindow"] = CONFIG["recv_window"]
        params["signature"] = _sign(params)

    headers = {"X-MBX-APIKEY": CONFIG["api_key"]} if signed else {}

    attempts = 1 if _no_retry else 3
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        try:
            resp = _session.request(
                method,
                CONFIG["base_url"] + endpoint,
                headers=headers,
                params=params,
                timeout=15,
            )
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(f"非 JSON 响应 HTTP {resp.status_code}: {resp.text[:200]}")

            if isinstance(data, dict) and "code" in data:
                try:
                    code = int(data["code"])
                except (TypeError, ValueError):
                    code = 0
                if code < 0:
                    # -1021 校时后重试一次
                    if code == -1021 and signed and attempt < attempts - 1:
                        sync_server_time(force=True)
                        params["timestamp"] = _ts()
                        params["signature"] = _sign({k: v for k, v in params.items() if k != "signature"})
                        continue
                    raise BinanceError(code, str(data.get("msg")), endpoint)

            return data
        except BinanceError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                logger.warning("请求失败 %s %s 第%s次: %s", method, endpoint, attempt + 1, exc)
                time.sleep(1 + attempt)

    raise RuntimeError(f"请求最终失败: {method} {endpoint} | {last_error}")


# ==========================================================================
# 5. Mock（离线演练）
# ==========================================================================
class MockBinance:
    """极简撮合：支持条件单触发，用于验证完整流程。"""

    def __init__(self) -> None:
        self.positions: Dict[str, Decimal] = {}
        self.entry: Dict[str, Decimal] = {}
        self.algo_orders: Dict[str, Dict[str, dict]] = {}
        self.seq = 100000
        self.dual = os.getenv("MOCK_DUAL_SIDE", "0") == "1"
        self.global_price = dec(os.getenv("MOCK_PRICE", "100"))
        self.prices: Dict[str, Decimal] = {}
        for pair in os.getenv("MOCK_PRICES", "").split(","):
            if "=" in pair:
                sym, _, val = pair.partition("=")
                try:
                    self.prices[sym.strip().upper()] = dec(val.strip())
                except Exception:
                    pass
        self.known = {
            "ETHUSDT", "BTCUSDT", "BBUSDT", "AINUSDT", "NILUSDT", "SOLUSDT", "BNBUSDT",
        }
        self.known.update(self.prices.keys())
        extra = os.getenv("MOCK_SYMBOLS", "").strip()
        if extra:
            self.known.update(s.strip().upper() for s in extra.split(",") if s.strip())

    def _next_id(self) -> str:
        self.seq += 1
        return str(self.seq)

    def _price_for(self, symbol: Optional[str]) -> Decimal:
        if symbol and symbol in self.prices:
            return self.prices[symbol]
        return self.global_price

    def set_price(self, price: Decimal, symbol: Optional[str] = None) -> None:
        if symbol:
            self.prices[symbol] = price
        else:
            self.global_price = price
        self._evaluate_triggers()

    def _evaluate_triggers(self) -> None:
        for symbol in list(self.algo_orders.keys()):
            mark = self._price_for(symbol)
            for cid, order in list(self.algo_orders[symbol].items()):
                trig = dec(order["triggerPrice"])
                otype = order["type"]
                hit = False
                if otype == "TAKE_PROFIT_MARKET":
                    hit = mark >= trig if order["side"] == "SELL" else mark <= trig
                elif otype == "STOP_MARKET":
                    hit = mark <= trig if order["side"] == "SELL" else mark >= trig
                if not hit:
                    continue

                qty = dec(order.get("quantity") or 0)
                if order.get("closePosition"):
                    qty = abs(self.positions.get(symbol, Decimal("0")))
                qty = min(qty, abs(self.positions.get(symbol, Decimal("0"))))
                if qty <= 0:
                    continue

                cur = self.positions.get(symbol, Decimal("0"))
                if order["side"] == "SELL":
                    self.positions[symbol] = cur - qty
                else:
                    self.positions[symbol] = cur + qty

                order["actualOrderId"] = self._next_id()
                order["actualQty"] = dec_str(qty)
                order["algoStatus"] = "CANCELED"
                self.algo_orders[symbol].pop(cid, None)

        # 仓位归零时，交易所会自动撤掉该 symbol 剩余条件单
        for symbol in list(self.algo_orders.keys()):
            if self.positions.get(symbol, Decimal("0")) == 0:
                self.algo_orders[symbol] = {}

    def request(self, method: str, endpoint: str, params: dict, signed: bool):
        symbol = params.get("symbol")

        if endpoint == "/fapi/v1/time":
            return {"serverTime": int(time.time() * 1000)}

        if endpoint == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}

        if endpoint == "/fapi/v1/ticker/price":
            return {"price": dec_str(self._price_for(symbol))}

        if endpoint == "/fapi/v1/exchangeInfo":
            # 真实接口永远返回全量交易对，mock 也要保真
            if symbol:
                self.known.add(symbol)
            return {
                "symbols": [
                    {
                        "symbol": sym,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                    for sym in sorted(self.known)
                ]
            }

        if endpoint == "/fapi/v1/leverage":
            return {"leverage": params.get("leverage")}

        if endpoint == "/fapi/v1/order":
            if method == "POST":
                qty = dec(params["quantity"])
                signed_qty = qty if params.get("side") == "BUY" else -qty
                cur = self.positions.get(symbol, Decimal("0"))
                updated = cur + signed_qty
                if cur != 0 and updated != 0 and (cur > 0) != (updated > 0):
                    updated = signed_qty
                self.positions[symbol] = updated
                if symbol not in self.entry:
                    self.entry[symbol] = self._price_for(symbol)
                return {"orderId": self._next_id(), "status": "FILLED", "executedQty": dec_str(qty)}

        if endpoint == "/fapi/v1/openOrders":
            return []

        if endpoint == "/fapi/v1/allOpenOrders":
            return {"code": 200, "msg": "done"}

        if endpoint == "/fapi/v1/algoOrder":
            if method == "POST":
                cid = params.get("clientAlgoId") or f"auto{self._next_id()}"
                order = {
                    "algoId": self._next_id(),
                    "clientAlgoId": cid,
                    "algoType": "CONDITIONAL",
                    "orderType": params.get("type"),
                    "symbol": symbol,
                    "side": params.get("side"),
                    "positionSide": params.get("positionSide", "BOTH"),
                    "type": params.get("type"),
                    "triggerPrice": params.get("triggerPrice"),
                    "quantity": params.get("quantity"),
                    "closePosition": params.get("closePosition") == "true",
                    "reduceOnly": params.get("reduceOnly") == "true",
                    "algoStatus": "NEW",
                    "actualOrderId": "",
                    "actualQty": "0",
                }
                self.algo_orders.setdefault(symbol, {})[cid] = order
                self._evaluate_triggers()
                return order

            if method == "GET":
                cid = params.get("clientAlgoId")
                algo_id = str(params.get("algoId") or "")
                for order in self.algo_orders.get(symbol, {}).values():
                    if (cid and order["clientAlgoId"] == cid) or (algo_id and order["algoId"] == algo_id):
                        return order
                for order in getattr(self, "_closed_algo", {}).get(symbol, []):
                    if (cid and order["clientAlgoId"] == cid) or (algo_id and order["algoId"] == algo_id):
                        return order
                raise BinanceError(-2013, "Order does not exist.", endpoint)

            if method == "DELETE":
                cid = params.get("clientAlgoId")
                algo_id = str(params.get("algoId") or "")
                bucket = self.algo_orders.get(symbol, {})
                for key in list(bucket.keys()):
                    order = bucket[key]
                    if (cid and order["clientAlgoId"] == cid) or (algo_id and order["algoId"] == algo_id):
                        bucket.pop(key, None)
                        return {"algoId": order["algoId"], "clientAlgoId": key, "code": "200", "msg": "success"}
                raise BinanceError(-2011, "Unknown order sent.", endpoint)

        if endpoint == "/fapi/v1/openAlgoOrders":
            self._evaluate_triggers()
            return list(self.algo_orders.get(symbol, {}).values())

        if endpoint == "/fapi/v1/algoOpenOrders":
            self.algo_orders[symbol] = {}
            return {"code": 200, "msg": "done"}

        if endpoint == "/fapi/v2/positionRisk":
            if symbol:
                amt = self.positions.get(symbol, Decimal("0"))
                return [{
                    "symbol": symbol,
                    "positionAmt": dec_str(amt),
                    "entryPrice": dec_str(self.entry.get(symbol, self._price_for(symbol))),
                    "positionSide": "BOTH",
                }]
            return [
                {"symbol": s, "positionAmt": dec_str(a), "entryPrice": dec_str(self.entry.get(s, self._price_for(s))),
                 "positionSide": "BOTH"}
                for s, a in self.positions.items()
            ]

        return {}


MOCK: Optional[MockBinance] = MockBinance() if CONFIG["mock"] else None


# ==========================================================================
# 6. 交易所基础能力
# ==========================================================================
def is_dual_side() -> bool:
    """双向持仓模式（Hedge Mode）下 positionSide 必须填 LONG/SHORT，且不能用 reduceOnly。"""
    global _dual_side
    if _dual_side is None:
        try:
            _dual_side = bool(_request("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition"))
        except Exception as exc:
            logger.warning("查询持仓模式失败，按单向持仓处理: %s", exc)
            _dual_side = False
        logger.info("持仓模式: %s", "双向(Hedge)" if _dual_side else "单向(One-way)")
    return _dual_side


def position_side_for(direction: Optional[str]) -> str:
    if is_dual_side():
        return (direction or "LONG").upper()
    return "BOTH"


def get_symbol_filters(symbol: str) -> Dict[str, Decimal]:
    """
    注意：币安 U 本位 /fapi/v1/exchangeInfo 不支持按 symbol 过滤，永远返回全量（约 500 个交易对）。
    所以这里一次性拉全量并建索引缓存，避免每个币对都重复拉一次巨大的 payload。
    """
    global _symbol_filters_loaded

    if symbol in _symbol_cache:
        return _symbol_cache[symbol]

    if not _symbol_filters_loaded:
        info = _request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for item in info.get("symbols", []):
            sym = item.get("symbol")
            if not sym:
                continue
            filters = {f["filterType"]: f for f in item.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_size = filters.get("LOT_SIZE", {}) or filters.get("MARKET_LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})

            _symbol_cache[sym] = {
                "tick": dec(price_filter.get("tickSize", "0")),
                "step": dec(lot_size.get("stepSize", "0")),
                "min_qty": dec(lot_size.get("minQty", "0")),
                "min_notional": dec(
                    notional_filter.get("notional", notional_filter.get("minNotional", CONFIG["min_notional"]))
                ),
            }
        _symbol_filters_loaded = True
        logger.info("已加载 %s 个交易对的精度规则", len(_symbol_cache))

    if symbol in _symbol_cache:
        return _symbol_cache[symbol]

    raise RuntimeError(f"未找到交易对: {symbol}（请确认是币安 U 本位合约交易对）")


_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def normalize_symbol(raw: Any) -> Optional[str]:
    """
    币对规范化 + 合法性校验。

    真实推送里混着大量非币对的「#标签」：`#牛来`（牛市来了）、`#牛来了`、`#牛来USDT`、
    `#傲冠VIP内部群金9活动统计🎉` 等等，其中 #牛来 出现 14 次、#傲冠… 出现 29 次。
    这些必须挡在门外，否则会变成 `牛来USDT` 这种不存在的交易对去打交易所。
    """
    if not raw:
        return None
    symbol = str(raw).strip().upper().replace("/", "").replace("-", "")
    if not symbol:
        return None
    if symbol.endswith("PERP"):
        symbol = symbol[:-4]
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    if not _SYMBOL_RE.match(symbol):
        logger.info("忽略非币对标签: %r（规范化后为 %r）", raw, symbol)
        return None
    return symbol


def get_last_price(symbol: str) -> Decimal:
    data = _request("GET", "/fapi/v1/ticker/price", params={"symbol": symbol}, signed=False)
    return dec(data["price"])


def get_position(symbol: str) -> Tuple[Decimal, Decimal]:
    """返回 (带符号持仓量, 开仓均价)。>0 为多，<0 为空。"""
    try:
        data = _request("GET", "/fapi/v2/positionRisk", params={"symbol": symbol})
    except Exception as exc:
        logger.error("%s 查询持仓失败: %s", symbol, exc)
        return Decimal("0"), Decimal("0")

    total = Decimal("0")
    entry = Decimal("0")
    for item in data if isinstance(data, list) else []:
        if item.get("symbol") != symbol:
            continue
        amt = dec(item.get("positionAmt", "0"))
        if amt == 0:
            continue
        total += amt
        entry = dec(item.get("entryPrice", "0"))
    return total, entry


def get_position_qty(symbol: str) -> Decimal:
    return abs(get_position(symbol)[0])


def set_leverage(symbol: str, leverage: int) -> None:
    try:
        _request("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage})
    except Exception as exc:
        logger.warning("%s 设置杠杆失败（忽略，沿用交易所现值）: %s", symbol, exc)


def calc_qty(symbol: str, margin_usdt: Decimal, price: Decimal, leverage: int) -> Decimal:
    filters = get_symbol_filters(symbol)
    notional = margin_usdt * Decimal(leverage)

    cap = CONFIG["max_notional_usdt"]
    if cap > 0 and notional > cap:
        logger.warning("%s 名义价值 %s 超过上限 %s，已截断", symbol, notional, cap)
        notional = cap

    qty = round_by_step(notional / price, filters["step"])

    if qty < filters["min_qty"]:
        qty = filters["min_qty"]

    if qty * price < filters["min_notional"]:
        qty = round_by_step(filters["min_notional"] / price, filters["step"])
        logger.warning("%s 名义价值不足，自动上调 qty=%s", symbol, dec_str(qty))

    return qty


# ==========================================================================
# 7. 订单操作
# ==========================================================================
def _client_id(tag: str) -> str:
    """自己的订单标记，用于精确识别/撤销，绝不误伤用户手动挂的单。"""
    return f"{CONFIG['client_id_prefix']}{tag}{int(time.time() * 1000) % 1000000}"


def place_market_order(symbol: str, side: str, qty: Decimal, direction: Optional[str] = None,
                       reduce_only: bool = False) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": dec_str(qty),
        "newClientOrderId": _client_id("MK"),
    }
    if is_dual_side():
        params["positionSide"] = position_side_for(direction)
    elif reduce_only:
        params["reduceOnly"] = "true"
    return _request("POST", "/fapi/v1/order", params=params)


def place_algo_order(
    symbol: str,
    side: str,
    algo_type: str,
    trigger_price: Decimal,
    quantity: Optional[Decimal] = None,
    close_position: bool = False,
    direction: Optional[str] = None,
    client_algo_id: Optional[str] = None,
) -> dict:
    """
    挂条件单。关键点：
      * closePosition=true  -> 触发后全平，自带只平仓效果，不能与 quantity / reduceOnly 同用
      * quantity + reduceOnly=true -> 分档平仓，绝不允许反向开仓
      * 双向持仓模式下不能用 reduceOnly，用 positionSide 表达
    """
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": algo_type,
        "triggerPrice": dec_str(trigger_price),
        "workingType": CONFIG["working_type"],
        "priceProtect": "true" if CONFIG["price_protect"] else "false",
        "clientAlgoId": client_algo_id or _client_id(algo_type.startswith("STOP") and "SL" or "TP"),
    }

    if is_dual_side():
        params["positionSide"] = position_side_for(direction)

    if close_position:
        params["closePosition"] = "true"
    else:
        if quantity is None or quantity <= 0:
            raise RuntimeError(f"{symbol} 条件单数量非法: {quantity}")
        params["quantity"] = dec_str(quantity)
        if not is_dual_side():
            params["reduceOnly"] = "true"

    return _request("POST", "/fapi/v1/algoOrder", params=params)


def get_open_algo_orders(symbol: str) -> List[dict]:
    data = _request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": symbol})
    return data if isinstance(data, list) else []


def get_our_algo_orders(symbol: str) -> List[dict]:
    prefix = CONFIG["client_id_prefix"]
    return [
        o for o in get_open_algo_orders(symbol)
        if str(o.get("clientAlgoId", "")).startswith(prefix)
    ]


def query_algo_order(symbol: str, client_algo_id: str) -> Optional[dict]:
    """查询条件单状态。返回 None 表示查不到（不存在 / 已彻底结束）。"""
    try:
        return _request(
            "GET",
            "/fapi/v1/algoOrder",
            params={"symbol": symbol, "clientAlgoId": client_algo_id},
        )
    except BinanceError as exc:
        if exc.code in (-2013, -2011):
            return None
        logger.warning("%s 查询条件单失败 %s: %s", symbol, client_algo_id, exc)
        return None
    except Exception as exc:
        logger.warning("%s 查询条件单异常 %s: %s", symbol, client_algo_id, exc)
        return None


def cancel_algo_order(symbol: str, algo_id: Optional[str] = None, client_algo_id: Optional[str] = None) -> bool:
    params = {"symbol": symbol}
    if algo_id:
        params["algoId"] = str(algo_id)
    if client_algo_id:
        params["clientAlgoId"] = client_algo_id
    if len(params) == 1:
        return False
    try:
        _request("DELETE", "/fapi/v1/algoOrder", params=params)
        return True
    except BinanceError as exc:
        if exc.code in (-2011, -2013):  # 已不存在
            return True
        logger.warning("%s 撤销条件单失败 %s: %s", symbol, client_algo_id or algo_id, exc)
        return False
    except Exception as exc:
        logger.warning("%s 撤销条件单异常: %s", symbol, exc)
        return False


def cancel_our_algo_orders(symbol: str) -> int:
    """只撤自己挂的条件单，不动用户手动挂的单。"""
    count = 0
    for order in get_our_algo_orders(symbol):
        if cancel_algo_order(
            symbol,
            algo_id=order.get("algoId"),
            client_algo_id=order.get("clientAlgoId"),
        ):
            count += 1
    if count:
        logger.info("%s 已撤销自有条件单 %s 张", symbol, count)
    return count


# ==========================================================================
# 8. 止盈止损规划
# ==========================================================================
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


def risk_check(direction: str, price: Decimal, tp: Decimal, sl: Decimal) -> Tuple[bool, str]:
    if direction == "LONG":
        if not (tp > price > sl):
            return False, f"LONG 参数不合法 tp={tp} price={price} sl={sl}"
    else:
        if not (tp < price < sl):
            return False, f"SHORT 参数不合法 tp={tp} price={price} sl={sl}"

    band = CONFIG["tpsl_sanity_pct"]
    if band > 0:
        for name, value in (("止盈", tp), ("止损", sl)):
            if price > 0 and abs(value - price) / price > band:
                return False, f"{name} {value} 偏离现价 {price} 超过 {band * 100}%"
    return True, ""


def level_is_sane(price: Decimal, value: Decimal) -> bool:
    """
    价格合理性：拒绝偏离现价过远的止盈止损值。

    这是为「不带币对的 SET_TPSL」兜底的——例如持仓 BTCUSDT（现价 60000）时收到
    「止盈：1.32-1.39 止损：1.195」，1.195 在多头方向上是"合法"的（低于现价），
    交易所会照单接收，结果真实止损被换成一个永远触发不了的废单，等于裸奔。
    """
    band = CONFIG["tpsl_sanity_pct"]
    if band <= 0 or price <= 0:
        return True
    return abs(value - price) / price <= band


def resolve_sl_price(direction: str, price: Decimal, candidates: List[Optional[Decimal]]) -> Optional[Decimal]:
    """
    从候选止损价里挑一个「在正确一侧 + 距离合理」的。
    全都不可用则返回 None，由调用方回退到默认 ±% 重建保护。
    """
    for cand in candidates:
        if cand is None or cand <= 0:
            continue
        if direction == "LONG" and cand >= price:
            continue
        if direction == "SHORT" and cand <= price:
            continue
        if not level_is_sane(price, cand):
            continue
        return cand
    return None


def max_tp_level() -> int:
    return max(CONFIG["tp_ratios"].keys()) if CONFIG["tp_ratios"] else 3


def tp_ratio(level: int) -> Decimal:
    return dec(CONFIG["tp_ratios"].get(level, Decimal("0")))


def plan_tp_quantities(
    base_qty: Decimal,
    levels: List[int],
    step: Decimal,
    min_qty: Decimal,
) -> List[Tuple[int, Decimal]]:
    """
    把「剩余仓位」按剩余档位的比例重新归一化分配。
    这样即使 TP1 已经平掉 50%，重挂 TP2/TP3 时也不会按原始仓位算而超量。
    最后一档吃掉余量，避免精度残留导致裸仓。
    """
    if not levels or base_qty <= 0:
        return []

    weights = [tp_ratio(lvl) for lvl in levels]
    total_w = sum(weights)
    if total_w <= 0:
        weights = [Decimal("1")] * len(levels)
        total_w = Decimal(len(levels))

    plan: List[Tuple[int, Decimal]] = []
    placed = Decimal("0")

    for idx, level in enumerate(levels):
        if idx == len(levels) - 1:
            qty = base_qty - placed
        else:
            qty = round_by_step(base_qty * weights[idx] / total_w, step)

        qty = round_by_step(qty, step)
        if qty <= 0 or qty < min_qty:
            logger.warning("TP%s 计划数量 %s 小于最小下单量 %s，跳过该档", level, qty, min_qty)
            continue

        plan.append((level, qty))
        placed += qty

    return plan


def place_tp_ladder(
    symbol: str,
    direction: str,
    close_side: str,
    current_qty: Decimal,
    tp_values: List[Decimal],
    skip_levels: Optional[List[int]] = None,
    only_levels: Optional[List[int]] = None,
) -> List[dict]:
    """
    按剩余档位挂多张 TAKE_PROFIT_MARKET（reduceOnly，分档平仓）。
    * skip_levels: 已止盈完成的档位，不参与分配
    * only_levels: 只实际下这几档（用于「体检补挂」），但数量仍按全部未完成档位统一分配，
                   避免只对缺失档归一化导致超量下单

    注意：已经被现价击穿的档位必须在「分配数量之前」剔除，否则它那部分额度会凭空消失，
    导致剩余仓位没有止盈保护（例如 TP1 被剔除却仍占 50% 额度）。
    """
    filters = get_symbol_filters(symbol)
    price = get_last_price(symbol)
    skip = set(skip_levels or [])

    levels: List[int] = []
    for i in range(1, len(tp_values) + 1):
        if i in skip:
            continue
        tp = round_price(tp_values[i - 1], filters["tick"])
        if direction == "LONG" and price >= tp:
            logger.warning("%s TP%s 触发价 %s 已被现价 %s 击穿，该档不参与挂单与分配",
                           symbol, i, dec_str(tp), dec_str(price))
            continue
        if direction == "SHORT" and price <= tp:
            logger.warning("%s TP%s 触发价 %s 已被现价 %s 击穿，该档不参与挂单与分配",
                           symbol, i, dec_str(tp), dec_str(price))
            continue
        levels.append(i)

    if not levels:
        return []

    plan = plan_tp_quantities(current_qty, levels, filters["step"], filters["min_qty"])
    if only_levels is not None:
        only = set(only_levels)
        plan = [(lvl, qty) for lvl, qty in plan if lvl in only]
    if not plan:
        return []

    orders: List[dict] = []

    for level, qty in plan:
        tp = round_price(tp_values[level - 1], filters["tick"])
        cid = _client_id(f"TP{level}")
        try:
            resp = place_algo_order(
                symbol=symbol,
                side=close_side,
                algo_type="TAKE_PROFIT_MARKET",
                trigger_price=tp,
                quantity=qty,
                close_position=False,
                direction=direction,
                client_algo_id=cid,
            )
        except Exception as exc:
            logger.error("%s 挂 TP%s 失败: %s", symbol, level, exc)
            continue

        orders.append({
            "level": level,
            "price": dec_str(tp),
            "qty": dec_str(qty),
            "algo_id": str(resp.get("algoId", "")),
            "client_algo_id": cid,
        })
        logger.info("%s 挂 TP%s | 价=%s | 量=%s | 比例=%s%%",
                    symbol, level, dec_str(tp), dec_str(qty), tp_ratio(level) * 100)

    return orders


def place_stop_loss(symbol: str, direction: str, close_side: str, sl: Decimal) -> Optional[dict]:
    """止损用 closePosition=true：触发后全平剩余仓位，且仓位归零时交易所会自动撤掉它。"""
    filters = get_symbol_filters(symbol)
    sl = round_price(sl, filters["tick"])
    cid = _client_id("SL")
    resp = place_algo_order(
        symbol=symbol,
        side=close_side,
        algo_type="STOP_MARKET",
        trigger_price=sl,
        close_position=True,
        direction=direction,
        client_algo_id=cid,
    )
    logger.info("%s 挂 SL | 价=%s", symbol, dec_str(sl))
    return {"price": dec_str(sl), "algo_id": str(resp.get("algoId", "")), "client_algo_id": cid}


# ==========================================================================
# 9. 核心交易动作
# ==========================================================================
def close_market(symbol: str, direction: str, qty: Decimal, tag: str = "CLOSE") -> bool:
    """市价平掉指定数量（reduceOnly 保护，绝不会反向开仓）。"""
    filters = get_symbol_filters(symbol)
    qty = round_by_step(qty, filters["step"])
    actual = get_position_qty(symbol)
    if actual <= 0:
        logger.warning("%s 无持仓可平", symbol)
        return False
    if qty > actual:
        qty = actual
    if qty <= 0:
        return False

    close_side = "SELL" if direction == "LONG" else "BUY"
    place_market_order(symbol, close_side, qty, direction=direction, reduce_only=True)
    logger.info("%s 市价平仓 | 方向=%s | 数量=%s | 标记=%s", symbol, direction, dec_str(qty), tag)
    return True


def open_position(
    symbol: str,
    direction: str,
    margin_usdt: Decimal,
    leverage: int,
    tp: Decimal,
    sl: Decimal,
    source: str = "OPEN",
) -> bool:
    logger.info("开仓开始 | %s | %s | 来源=%s", symbol, direction, source)

    price = get_last_price(symbol)
    filters = get_symbol_filters(symbol)

    ok, msg = risk_check(direction, price, round_price(tp, filters["tick"]), round_price(sl, filters["tick"]))
    if not ok:
        fallback_tp, fallback_sl = build_default_tpsl(direction, price)
        logger.warning("%s 信号止盈止损不合法（%s），回退默认 ±%s%%",
                       symbol, msg, CONFIG["default_tp_pct"] * 100)
        notify(
            subject=f"[警告] {symbol} 止盈止损不合法，已回退默认",
            body=f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n现价: {price}\n"
                 f"信号 TP={tp} SL={sl}\n原因: {msg}\n已回退为 TP={fallback_tp} SL={fallback_sl}\n",
            cooldown_key=f"bad_tpsl:{symbol}",
        )
        tp, sl = fallback_tp, fallback_sl

    tp = round_price(tp, filters["tick"])
    sl = round_price(sl, filters["tick"])

    qty = calc_qty(symbol, margin_usdt, price, leverage)
    open_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"

    set_leverage(symbol, leverage)

    place_market_order(symbol, open_side, qty, direction=direction)
    time.sleep(0.6)

    # 开仓成交确认（重试，避免刚下单就查持仓导致误判失败）
    filled = Decimal("0")
    for _ in range(5):
        filled = get_position_qty(symbol)
        if filled > 0:
            break
        time.sleep(1)

    if filled <= 0:
        raise RuntimeError("开仓后仍未检测到持仓，请人工核对")

    cancel_our_algo_orders(symbol)

    sl_order = place_stop_loss(symbol, direction, close_side, sl)
    tp_orders = place_tp_ladder(symbol, direction, close_side, filled, [tp])

    state = load_state()
    state["positions"][symbol] = {
        "direction": direction,
        "entry_price": dec_str(price),
        "qty": dec_str(filled),          # 开仓时数量（分档比例基准）
        "closed_qty": "0",               # 已平数量
        "tp": dec_str(tp),
        "sl": dec_str(sl),
        "leverage": leverage,
        "margin_usdt": dec_str(margin_usdt),
        "opened_at": time.time(),
        "tp_done": [],
        "tp_orders": tp_orders,
        "sl_order": sl_order,
        "source": source,
    }
    save_state(state)

    logger.info("开仓完成 | %s | TP=%s | SL=%s | QTY=%s", symbol, dec_str(tp), dec_str(sl), dec_str(filled))
    notify(
        subject=f"[开仓成功] {symbol} {direction}",
        body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n杠杆: {leverage}x\n"
              f"保证金: {margin_usdt} USDT\n开仓价: {price}\n数量: {filled}\n"
              f"止盈: {tp}\n止损: {sl}\n"),
        cooldown_key=f"open_ok:{symbol}:{direction}",
    )
    return True


def update_tpsl(symbol: str, tp_list: List[Any], sl_list: List[Any], reason: str = "") -> bool:
    """收到新止盈止损：撤掉自有旧条件单，按新价重挂（SL 全平 + TP 分档）。"""
    state = load_state()
    position = state.get("positions", {}).get(symbol)

    if not position:
        logger.warning("%s 无本地持仓记录，跳过止盈止损更新", symbol)
        return False

    actual = get_position_qty(symbol)
    if actual <= 0:
        logger.warning("%s 实际无持仓，清理本地记录", symbol)
        cleanup_position(symbol)
        return False

    price = get_last_price(symbol)
    direction = position["direction"]
    close_side = "SELL" if direction == "LONG" else "BUY"
    filters = get_symbol_filters(symbol)

    tp_values = [dec(x) for x in tp_list if x is not None]
    sl_values = [dec(x) for x in sl_list if x is not None]

    # 若本次只推了止盈（或只推了止损），用最近一次的值补齐另一半，
    # 否则撤单之后不重挂，会出现一段「无保护」的裸奔窗口。
    last = state.get("last_tpsl", {}).get(symbol, {}) or {}
    if not tp_values and last.get("tp"):
        tp_values = [dec(x) for x in last["tp"] if x is not None]
        logger.info("%s 本次未推送止盈，沿用上次止盈 %s", symbol, [dec_str(x) for x in tp_values])
    if not sl_values and last.get("sl"):
        sl_values = [dec(x) for x in last["sl"] if x is not None]
        logger.info("%s 本次未推送止损，沿用上次止损 %s", symbol, [dec_str(x) for x in sl_values])

    # ---- 价格合理性闸门 ----
    # 关键：不带币对的 SET_TPSL 会被广播到所有持仓。若持仓是 BTCUSDT（现价 60000）
    # 而收到 ETH 的「止损 1.195」，该值在多头方向上是"合法"的（低于现价），交易所会照单接收，
    # 结果真实止损被换成一个永远触发不了的废单 —— 等于裸奔。这里直接拦掉。
    bad = [dec_str(v) for v in tp_values + sl_values if not level_is_sane(price, v)]
    if bad:
        tp_values = [v for v in tp_values if level_is_sane(price, v)]
        sl_values = [v for v in sl_values if level_is_sane(price, v)]
        logger.warning("%s 现价 %s，以下止盈止损偏离过大已丢弃: %s", symbol, dec_str(price), bad)
        notify(
            subject=f"[风控] {symbol} 止盈止损偏离过大已丢弃",
            body=(f"时间: {now_text()}\n标的: {symbol}\n现价: {dec_str(price)}\n"
                  f"丢弃值: {bad}\n允许带宽: ±{CONFIG['tpsl_sanity_pct'] * 100}%\n"
                  f"原持仓保护保持不变。\n"),
            cooldown_key=f"sanity:{symbol}",
        )

    if not tp_values and not sl_values:
        logger.warning("%s 本次止盈止损全部不合法，保留原有保护不动", symbol)
        return False

    # 1) 先撤自有旧条件单（只撤自己的）
    cancel_our_algo_orders(symbol)

    # 2) 挂止损（优先，保证保护不断档）
    #    优先级：本次推送值 > 原止损值 > 默认 ±%
    sl_target = resolve_sl_price(direction, price, [sl_values[0] if sl_values else None, dec(position.get("sl", "0"))])
    if sl_target is None:
        _, default_sl = build_default_tpsl(direction, price)
        sl_target = default_sl
        logger.warning("%s 推送的止损不可用（位置错误或偏离过大），回退默认 ±%s%% -> %s",
                       symbol, CONFIG["default_sl_pct"] * 100, dec_str(sl_target))

    sl_order = None
    try:
        sl_order = place_stop_loss(symbol, direction, close_side, sl_target)
        position["sl"] = dec_str(round_price(sl_target, filters["tick"]))
    except Exception as exc:
        logger.error("%s 挂止损失败: %s", symbol, exc)
        notify(subject=f"[失败] {symbol} 止损挂单失败",
               body=(f"时间: {now_text()}\n标的: {symbol}\n目标止损: {sl_target}\n错误: {exc}\n"
                     f"注意：当前可能处于无止损状态，请尽快人工处理。"),
               cooldown_key=f"sl_fail:{symbol}", force=True)

    # 3) 挂多档止盈（基于当前剩余仓位重新分配）
    tp_orders = position.get("tp_orders", [])
    if tp_values:
        tp_done = position.get("tp_done", [])
        tp_orders = place_tp_ladder(
            symbol, direction, close_side, actual, tp_values, skip_levels=tp_done
        )
        if tp_orders:
            position["tp"] = dec_str(round_price(dec(tp_orders[0]["price"]), filters["tick"]))
        else:
            logger.warning("%s 本次没有成功挂出任何止盈档位", symbol)

    position["tp_orders"] = tp_orders
    if sl_order:
        position["sl_order"] = sl_order

    state["positions"][symbol] = position
    # 只记录「通过合理性校验」的值，避免脏值写进 last_tpsl 后被开仓/补挂逻辑复用。
    # tp 必须是「按档位顺序」的列表（而不是已挂出的价格），否则某档被跳过后索引会错位。
    state["last_tpsl"][symbol] = {
        "tp": [dec_str(v) for v in tp_values],
        "sl": [position["sl"]] if position.get("sl") else [],
        "updated_at": time.time(),
    }
    save_state(state)

    tp_detail = "\n".join(f"TP{o['level']}: 价={o['price']} 量={o['qty']}" for o in tp_orders) or "无"
    logger.info("%s 止盈止损已更新 | SL=%s | TP档=%s", symbol, position.get("sl"), len(tp_orders))
    notify(
        subject=f"[止盈止损已更新] {symbol}",
        body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n现价: {dec_str(price)}\n"
              f"止损: {position.get('sl')}\n止盈分档:\n{tp_detail}\n"),
        cooldown_key=f"tpsl_ok:{symbol}",
    )
    return True


def cleanup_position(symbol: str, cancel_orders: bool = True) -> None:
    if cancel_orders:
        try:
            cancel_our_algo_orders(symbol)
        except Exception as exc:
            logger.warning("%s 清理条件单失败: %s", symbol, exc)
    state = load_state()
    state.get("positions", {}).pop(symbol, None)
    save_state(state)


# ==========================================================================
# 10. TP 信号：与交易所条件单对账后再平仓（核心防重复平仓逻辑）
# ==========================================================================
def handle_tp_signal(symbol: str, level: int) -> bool:
    state = load_state()
    position = state.get("positions", {}).get(symbol)

    if not position:
        logger.warning("%s 无本地持仓记录，无法处理 TP%s", symbol, level)
        return False

    tp_done = list(position.get("tp_done", []))
    if level in tp_done:
        logger.info("%s TP%s 已处理过，忽略重复推送", symbol, level)
        return False

    direction = position["direction"]
    filters = get_symbol_filters(symbol)
    original_qty = dec(position.get("qty", "0"))
    closed_qty = dec(position.get("closed_qty", "0"))
    actual_qty = get_position_qty(symbol)

    if actual_qty <= 0:
        logger.info("%s 已无持仓，标记 TP%s 完成", symbol, level)
        mark_tp_done(symbol, level)
        cleanup_position(symbol)
        return True

    # ---- 对账：交易所侧是否已经帮我们平掉了一部分 ----
    expected_qty = original_qty - closed_qty
    unexplained = expected_qty - actual_qty
    if unexplained > filters["step"]:
        logger.warning(
            "%s 检测到交易所条件单已成交 %s（预期剩余 %s / 实际 %s），不再重复市价平仓",
            symbol, dec_str(unexplained), dec_str(expected_qty), dec_str(actual_qty),
        )
        # 同步账本，避免后续档位重复计算这笔差额
        closed_qty += unexplained
        state = load_state()
        pos = state.get("positions", {}).get(symbol)
        if pos is not None:
            pos["closed_qty"] = dec_str(closed_qty)
            state["positions"][symbol] = pos
            save_state(state)
        mark_tp_done(symbol, level)
        if actual_qty <= 0:
            cleanup_position(symbol, cancel_orders=True)
        notify(
            subject=f"[止盈对账] {symbol} TP{level} 交易所已自动成交",
            body=(f"时间: {now_text()}\n标的: {symbol}\n档位: TP{level}\n"
                  f"交易所已平: {unexplained}\n剩余持仓: {actual_qty}\n已跳过手动平仓，避免重复。\n"),
            cooldown_key=f"tp_recon:{symbol}:{level}",
        )
        return True

    # ---- 找出该档对应的交易所挂单 ----
    rec = next((o for o in position.get("tp_orders", []) if int(o.get("level", 0)) == level), None)

    if rec:
        cid = rec.get("client_algo_id")
        status = query_algo_order(symbol, cid) if cid else None

        if status is None and cid:
            # 查不到单子：无法判断是已成交还是被撤，保守起见不重复平仓，交由人工确认
            logger.error("%s TP%s 条件单状态未知（%s），为避免重复平仓已跳过", symbol, level, cid)
            notify(
                subject=f"[需人工确认] {symbol} TP{level} 状态未知",
                body=(f"时间: {now_text()}\n标的: {symbol}\n档位: TP{level}\n"
                      f"clientAlgoId: {cid}\n无法查询该条件单状态，已跳过自动平仓。\n"
                      f"当前持仓: {actual_qty}\n请人工确认是否需要手动止盈。\n"),
                cooldown_key=f"tp_unknown:{symbol}:{level}", force=True,
            )
            return False

        if status and str(status.get("actualOrderId") or "").strip():
            logger.info("%s TP%s 条件单已触发成交，无需手动平仓", symbol, level)
            mark_tp_done(symbol, level)
            return True

        # 尚未触发 -> 先撤单，再手动市价平该档，避免稍后又触发一次
        if cid:
            cancel_algo_order(symbol, algo_id=rec.get("algo_id"), client_algo_id=cid)
        tranche = dec(rec.get("qty", "0"))
    else:
        # 没有挂单记录（例如从未同步过）-> 按原始仓位比例推算
        if level >= max_tp_level():
            tranche = actual_qty  # 最后一档直接清干净
        else:
            tranche = round_by_step(original_qty * tp_ratio(level), filters["step"])

    if level >= max_tp_level():
        tranche = actual_qty  # 最终档全平，避免残留裸仓

    tranche = min(tranche, actual_qty)
    if tranche <= 0:
        return False

    if not close_market(symbol, direction, tranche, tag=f"TP{level}"):
        return False

    remain = get_position_qty(symbol)
    closed_qty += tranche

    state = load_state()
    pos = state.get("positions", {}).get(symbol)
    if pos is not None:
        pos["closed_qty"] = dec_str(closed_qty)
        state["positions"][symbol] = pos
        save_state(state)

    mark_tp_done(symbol, level)

    if remain <= 0:
        cleanup_position(symbol, cancel_orders=True)

    notify(
        subject=f"[止盈平仓] {symbol} TP{level}",
        body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n档位: TP{level}\n"
              f"平仓数量: {tranche}\n剩余持仓: {remain}\n"),
        cooldown_key=f"tp_ok:{symbol}:{level}",
    )
    return True


def mark_tp_done(symbol: str, level: int) -> None:
    state = load_state()
    pos = state.get("positions", {}).get(symbol)
    if pos is None:
        return
    done = pos.setdefault("tp_done", [])
    if level not in done:
        done.append(level)
    pos["tp_orders"] = [o for o in pos.get("tp_orders", []) if int(o.get("level", 0)) != level]
    state["positions"][symbol] = pos
    save_state(state)


def handle_close_signal(symbol: str) -> bool:
    state = load_state()
    position = state.get("positions", {}).get(symbol)
    direction = position["direction"] if position else None

    if not direction:
        signed, _ = get_position(symbol)
        if signed == 0:
            logger.warning("%s 无持仓，忽略平仓", symbol)
            return False
        direction = "LONG" if signed > 0 else "SHORT"

    qty = get_position_qty(symbol)
    if qty <= 0:
        cleanup_position(symbol)
        return False

    if not close_market(symbol, direction, qty, tag="CLOSE"):
        return False

    remain = get_position_qty(symbol)
    if remain <= 0:
        cleanup_position(symbol, cancel_orders=True)

    notify(
        subject=f"[平仓成功] {symbol}",
        body=f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n平仓数量: {qty}\n剩余: {remain}\n",
        cooldown_key=f"close_ok:{symbol}",
    )
    return True


def handle_add_signal(symbol: str, add_list: List[Any]) -> bool:
    """
    补仓：默认关闭（ENABLE_ADD=1 才启用）。按挂单价挂 LIMIT 单。

    注意真实推送里的几种「补仓」措辞：
      * `补仓：0,01533` / `补仓位：0,482`   -> 真正的挂单价，要下单（本函数处理）
      * `预留好补仓位` / `留好补仓位`        -> 纯提示语，没有价格，绝不能当成下单信号
      * `出补仓` / `分批进场后已回成本，出补仓` -> 撤销之前的补仓挂单（见 cancel_add_orders）
    """
    prices = []
    for raw in add_list:
        value = dec(raw)
        if value > 0:
            prices.append(value)

    if not prices:
        logger.info("%s 补仓信号没有有效价格（可能是「预留好补仓位」这类提示语），忽略", symbol)
        return False

    if not CONFIG["enable_add"]:
        logger.info("%s 收到补仓价 %s，但 ENABLE_ADD=0，已忽略", symbol, [dec_str(p) for p in prices])
        return False

    state = load_state()
    position = state.get("positions", {}).get(symbol)
    if not position:
        logger.warning("%s 无持仓，忽略补仓", symbol)
        return False

    direction = position["direction"]
    filters = get_symbol_filters(symbol)
    price = get_last_price(symbol)
    placed: List[dict] = []

    for add_price in prices:
        add_price = round_price(add_price, filters["tick"])

        # 合理性：不能偏离现价太远，否则一定是解析错误
        if not level_is_sane(price, add_price):
            logger.warning("%s 补仓价 %s 偏离现价 %s 过大，忽略", symbol, dec_str(add_price), dec_str(price))
            continue

        # 只接受比现价更有利的挂单价，否则会变成追单
        if direction == "LONG" and add_price >= price:
            logger.info("%s 补仓价 %s 高于现价 %s，跳过", symbol, dec_str(add_price), dec_str(price))
            continue
        if direction == "SHORT" and add_price <= price:
            logger.info("%s 补仓价 %s 低于现价 %s，跳过", symbol, dec_str(add_price), dec_str(price))
            continue

        qty = calc_qty(
            symbol,
            CONFIG["default_margin_usdt"],
            add_price,
            int(position.get("leverage", CONFIG["default_leverage"])),
        )
        cid = _client_id("ADD")
        params = {
            "symbol": symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": dec_str(add_price),
            "quantity": dec_str(qty),
            "newClientOrderId": cid,
        }
        if is_dual_side():
            params["positionSide"] = position_side_for(direction)
        try:
            resp = _request("POST", "/fapi/v1/order", params=params)
            placed.append({
                "price": dec_str(add_price),
                "qty": dec_str(qty),
                "order_id": str(resp.get("orderId", "")),
                "client_order_id": cid,
            })
            logger.info("%s 补仓挂单 | 价=%s | 量=%s", symbol, dec_str(add_price), dec_str(qty))
        except Exception as exc:
            logger.error("%s 补仓挂单失败: %s", symbol, exc)

    if not placed:
        return False

    state = load_state()
    pos = state.get("positions", {}).get(symbol)
    if pos is not None:
        pos.setdefault("add_orders", []).extend(placed)
        state["positions"][symbol] = pos
        save_state(state)

    notify(
        subject=f"[补仓挂单] {symbol}",
        body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n"
              + "\n".join(f"价={o['price']} 量={o['qty']}" for o in placed) + "\n"),
        cooldown_key=f"add_ok:{symbol}",
    )
    return True


def cancel_add_orders(symbol: str, reason: str = "") -> bool:
    """撤销该币对之前挂出的补仓单（对应推送里的「出补仓」）。"""
    if not CONFIG["enable_cancel_add"]:
        logger.info("%s 收到出补仓，但 ENABLE_CANCEL_ADD=0，已忽略", symbol)
        return False

    state = load_state()
    position = state.get("positions", {}).get(symbol)
    records = list((position or {}).get("add_orders", []))

    cancelled = 0
    for rec in records:
        try:
            _request("DELETE", "/fapi/v1/order", params={
                "symbol": symbol,
                "orderId": rec.get("order_id"),
            })
            cancelled += 1
            logger.info("%s 已撤销补仓单 价=%s 量=%s", symbol, rec.get("price"), rec.get("qty"))
        except Exception as exc:
            logger.warning("%s 撤销补仓单失败（可能已成交或已撤）: %s", symbol, exc)

    if position is not None:
        state["positions"][symbol]["add_orders"] = []
        save_state(state)

    if cancelled:
        notify(
            subject=f"[出补仓] {symbol} 已撤销补仓单",
            body=(f"时间: {now_text()}\n标的: {symbol}\n撤销数量: {cancelled}\n"
                  f"原因: {reason or '收到出补仓信号'}\n"),
            cooldown_key=f"cancel_add:{symbol}",
        )
    else:
        logger.info("%s 没有待撤的补仓单", symbol)
    return cancelled > 0


def order_positions_by_proximity(positions: Dict[str, Any], anchor: Optional[Decimal]) -> List[str]:
    """
    按「开仓价与信号价位的接近程度」给持仓排序，距离近的优先。
    回退时这比单纯按开仓时间可靠得多：TP=1.32/SL=1.195 显然属于开仓价 1.35 的 ETH，
    而不是开仓价 85 的另一个币。距离相同再按开仓时间倒序。
    """
    def sort_key(sym: str):
        info = positions.get(sym) or {}
        entry = dec(info.get("entry_price", "0"))
        opened_at = float(info.get("opened_at", 0) or 0)
        if anchor is None or anchor <= 0 or entry <= 0:
            return (Decimal("1e18"), -opened_at)
        return (abs(entry - anchor), -opened_at)

    return sorted(positions.keys(), key=sort_key)


def resolve_signal_targets(
    state: Dict[str, Any],
    tp_list: Optional[List[Any]] = None,
    sl_list: Optional[List[Any]] = None,
) -> List[str]:
    """
    不带币对的消息要落到哪个持仓上，返回候选顺序（按优先级）。
    顺序 = [锚点币对（若有持仓）] + 其余按开仓价接近度排序。
    """
    positions = state.get("positions", {})
    if not positions:
        return []

    last_symbol = state.get("last_symbol")
    by_recent = sorted(
        positions.keys(),
        key=lambda s: float(positions[s].get("opened_at", 0) or 0),
        reverse=True,
    )

    # 回退时的排序锚点：止损通常最贴近开仓价，优先用它
    anchor = dec(sl_list[0]) if sl_list else None
    if (anchor is None or anchor <= 0) and tp_list:
        anchor = sum(dec(x) for x in tp_list) / Decimal(len(tp_list))
    by_proximity = order_positions_by_proximity(positions, anchor)

    mode = CONFIG["symbolless_tpsl_mode"]
    if mode == "ALL":
        return by_recent
    if mode == "LATEST":
        return by_recent

    # REF（默认）
    if last_symbol and last_symbol in positions:
        return [last_symbol] + [s for s in by_proximity if s != last_symbol]
    if last_symbol:
        logger.info("锚点币对 %s 当前无持仓，按开仓价接近度回退", last_symbol)
    return by_proximity


def describe_anchor(state: Dict[str, Any]) -> str:
    last = state.get("last_symbol")
    held = list((state.get("positions") or {}).keys())
    return f"锚点币对={last}，当前持仓={held}"


# ==========================================================================
# 11. 信号解析与分发
# ==========================================================================
def read_new_signals(state: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """
    增量读取 signals.jsonl（基于字节偏移），并处理写入到一半的行。
    返回 (信号列表, 新偏移)。偏移由调用方在**处理完之后**再提交，
    这样中途崩溃不会丢信号（只会重复处理，而重复处理是幂等的）。
    """
    if not SIGNAL_FILE.exists():
        return [], int(state.get("cursor", 0) or 0)

    size = SIGNAL_FILE.stat().st_size
    cursor = int(state.get("cursor", 0) or 0)
    if size < cursor:      # 文件被清空/轮转
        cursor = 0
    if size == cursor:
        return [], cursor

    with SIGNAL_FILE.open("rb") as fh:
        fh.seek(cursor)
        chunk = fh.read()

    parts = chunk.split(b"\n")
    tail = parts.pop()          # 最后一段可能是不完整的行
    new_cursor = cursor + len(chunk) - len(tail)

    signals: List[Dict[str, Any]] = []
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            sig = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            logger.warning("跳过无法解析的信号行: %s", exc)
            continue
        if isinstance(sig, dict):
            signals.append(sig)

    return signals, new_cursor


def commit_cursor(cursor: int) -> None:
    state = load_state()
    state["cursor"] = cursor
    save_state(state)


def consume_signals() -> None:
    """读取并处理一批新信号，全部处理完后才提交游标。"""
    state = load_state()
    signals, new_cursor = read_new_signals(state)
    if not signals:
        if new_cursor != int(state.get("cursor", 0) or 0):
            commit_cursor(new_cursor)   # 只有空行/坏行，直接推进
        return

    for sig in signals:
        try:
            handle_signal(sig)
        except Exception as exc:
            logger.exception("处理信号失败: %s", json.dumps(sig, ensure_ascii=False))
            notify(
                subject=f"[失败] 信号处理失败 {sig.get('symbol') or '未知标的'}",
                body=(f"时间: {now_text()}\n信号: {json.dumps(sig, ensure_ascii=False)}\n错误: {exc}\n"),
                cooldown_key=f"signal_fail:{sig.get('symbol')}:{sig.get('type')}",
            )

    commit_cursor(new_cursor)


def handle_signal(signal: Dict[str, Any]) -> bool:
    sig_type = str(signal.get("type", "")).upper()
    symbol = normalize_symbol(signal.get("symbol"))
    side = str(signal.get("side") or "").upper()
    tp_list = signal.get("tp") or []
    sl_list = signal.get("sl") or []
    add_list = signal.get("add") or []
    tp_level = signal.get("tp_level")

    logger.info("收到信号: %s", json.dumps(signal, ensure_ascii=False))

    # 记录「锚点币对」：不带币对的 SET_TPSL 是「引用上一条下单消息」，靠它对应到正确持仓。
    # 只允许下单类消息（默认仅 OPEN）更新，TP / CLOSE 等带币对的消息一律不许污染它。
    if symbol and sig_type in CONFIG["symbolless_anchor_types"]:
        _st = load_state()
        if _st.get("last_symbol") != symbol:
            _st["last_symbol"] = symbol
            save_state(_st)
            logger.debug("锚点币对更新为 %s（来自 %s）", symbol, sig_type)

    # ---- OPEN ----
    if sig_type == "OPEN":
        if not symbol or side not in ("LONG", "SHORT"):
            logger.warning("OPEN 信号缺少 symbol/side 或 side 非法，忽略")
            return False

        signed, entry = get_position(symbol)
        if signed != 0:
            current = "LONG" if signed > 0 else "SHORT"
            if current == side:
                logger.info("%s 已有同向持仓，跳过开仓", symbol)
                return False
            if not CONFIG["reverse_on_opposite_open"]:
                logger.info("%s 已有反向持仓且未开启反手，跳过开仓", symbol)
                return False
            logger.info("%s 检测到反向持仓，先平后开", symbol)
            if not handle_close_signal(symbol):
                return False
            time.sleep(1)

        if len(load_state().get("positions", {})) >= CONFIG["max_positions"]:
            logger.warning("持仓数已达上限 %s，跳过开仓 %s", CONFIG["max_positions"], symbol)
            notify(subject=f"[风控] 持仓数达上限，跳过 {symbol}",
                   body=f"时间: {now_text()}\n标的: {symbol}\n上限: {CONFIG['max_positions']}\n",
                   cooldown_key="max_positions")
            return False

        price = get_last_price(symbol)

        # 只有当 last_tpsl 足够新（默认 120s 内）才复用，避免拿上一单的陈旧止盈止损
        if tp_list and sl_list:
            tp, sl = dec(tp_list[0]), dec(sl_list[0])
        else:
            last = load_state().get("last_tpsl", {}).get(symbol)
            fresh = last and (time.time() - float(last.get("updated_at", 0)) < CONFIG["last_tpsl_ttl"])
            if fresh and last.get("tp") and last.get("sl"):
                tp, sl = dec(last["tp"][0]), dec(last["sl"][0])
                logger.info("%s 复用 %ss 内的止盈止损 TP=%s SL=%s", symbol, CONFIG["last_tpsl_ttl"], tp, sl)
            else:
                tp, sl = build_default_tpsl(side, price)
                logger.info("%s 使用默认 ±%s%% 止盈止损", symbol, CONFIG["default_tp_pct"] * 100)

        try:
            return open_position(
                symbol=symbol,
                direction=side,
                margin_usdt=CONFIG["default_margin_usdt"],
                leverage=CONFIG["default_leverage"],
                tp=tp,
                sl=sl,
            )
        except Exception as exc:
            logger.error("开仓失败 | %s | %s | err=%s", symbol, side, exc)
            notify(
                subject=f"[失败] 开仓失败 {symbol} {side}",
                body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {side}\n"
                      f"杠杆: {CONFIG['default_leverage']}x\n保证金: {CONFIG['default_margin_usdt']} USDT\n"
                      f"止盈: {tp}\n止损: {sl}\n错误: {exc}\n"),
                cooldown_key=f"open_fail:{symbol}", force=True,
            )
            return False

    # ---- SET_TPSL ----
    if sig_type == "SET_TPSL":
        state = load_state()

        if symbol:
            if add_list:
                handle_add_signal(symbol, add_list)
            # 止盈止损全为空：多半是「预留好补仓位」这类提示类消息被误判成 SET_TPSL，
            # 直接忽略，不要拿上次的值去无意义地撤单重挂。
            if not tp_list and not sl_list:
                if add_list:
                    return True
                logger.info("%s SET_TPSL 没有任何止盈止损值，忽略（可能是提示类消息）", symbol)
                return False
            # last_tpsl 由 update_tpsl 在「确认生效」后写入，避免脏值被开仓逻辑复用
            return update_tpsl(symbol, tp_list, sl_list)

        # 没带币对：这是「引用上一条下单消息」的止盈止损，按优先级逐个尝试
        state = load_state()
        candidates = resolve_signal_targets(state, tp_list, sl_list)
        if not candidates:
            logger.info("SET_TPSL 未带币对且当前无持仓，忽略")
            return False

        if len(candidates) > 1:
            logger.warning("SET_TPSL 未带币对，按 %s 模式匹配（%s）",
                           CONFIG["symbolless_tpsl_mode"], describe_anchor(state))

        matched: List[str] = []
        if CONFIG["symbolless_tpsl_mode"] == "ALL":
            for sym in candidates:
                if update_tpsl(sym, tp_list, sl_list):
                    matched.append(sym)
        else:
            # 依次尝试，命中一个就停。
            # 正常情况下第一个候选（锚点币对）就会命中；
            # 若它当前无持仓、或这些价位明显对不上（价格闸门会拦），才往后回退。
            for sym in candidates:
                if update_tpsl(sym, tp_list, sl_list):
                    matched.append(sym)
                    break

        if not matched:
            logger.warning("SET_TPSL 未带币对，且没有任何持仓匹配上这些价位，全部忽略")
            notify(
                subject="[风控] 未带币对的止盈止损没有匹配到持仓",
                body=(f"时间: {now_text()}\n止盈: {tp_list}\n止损: {sl_list}\n"
                      f"{describe_anchor(state)}\n"
                      f"所有持仓的价格都对不上，已全部忽略（原保护保持不变）。\n"),
                cooldown_key="symbolless_nomatch", force=True,
            )
        else:
            logger.info("SET_TPSL 未带币对 -> 实际应用到 %s（%s）", matched, describe_anchor(state))
        return True

    # ---- TP ----
    if sig_type in ("TP", "TP_HIT", "TAKE_PROFIT"):
        try:
            level = int(tp_level or 1)
        except (TypeError, ValueError):
            level = 1

        if symbol:
            return handle_tp_signal(symbol, level)

        # 不带币对的 TP：真实数据里这类绝大多数是营销噪音
        # （「完美止盈，十个点拿下！」「VIP内部群拿下今日止盈第1️⃣单」），默认不猜、不执行。
        if CONFIG["symbolless_tp_close_mode"] != "ANCHOR":
            logger.warning("TP 信号未带币对（TP%s），按 SKIP 策略忽略，避免平错仓", level)
            return False

        state = load_state()
        for sym in resolve_signal_targets(state):
            if handle_tp_signal(sym, level):
                logger.info("TP%s 未带币对 -> 锚点推断命中 %s", level, sym)
                return True
        logger.warning("TP%s 未带币对且推断不出持仓，忽略", level)
        return False

    # ---- CLOSE ----
    if sig_type in ("CLOSE", "EXIT", "CLOSE_ALL", "MANUAL_CLOSE"):
        if symbol:
            return handle_close_signal(symbol)

        if CONFIG["symbolless_tp_close_mode"] != "ANCHOR":
            logger.warning("平仓信号未带币对，按 SKIP 策略忽略，避免平错仓")
            return False

        state = load_state()
        for sym in resolve_signal_targets(state):
            if handle_close_signal(sym):
                logger.info("平仓信号未带币对 -> 锚点推断命中 %s", sym)
                return True
        logger.warning("平仓信号未带币对且推断不出持仓，忽略")
        return False

    # ---- 出补仓：撤销之前挂出的补仓单 ----
    if sig_type in ("CANCEL_ADD", "ADD_CANCEL", "CANCEL_ADD_ORDER"):
        if not symbol:
            logger.warning("出补仓信号未带币对，忽略")
            return False
        return cancel_add_orders(symbol, reason="收到出补仓信号")

    logger.warning("未识别的信号类型: %s", sig_type)
    return False


# ==========================================================================
# 12. 每分钟体检：补齐缺失的条件单（而不是无脑撤单重挂）
# ==========================================================================
def ensure_protection(symbol: str, position: Dict[str, Any]) -> None:
    actual = get_position_qty(symbol)
    if actual <= 0:
        logger.info("%s 实际已无持仓，清理本地记录", symbol)
        cleanup_position(symbol)
        return

    our_orders = get_our_algo_orders(symbol)
    by_cid = {str(o.get("clientAlgoId")): o for o in our_orders}

    direction = position["direction"]
    close_side = "SELL" if direction == "LONG" else "BUY"
    filters = get_symbol_filters(symbol)

    # --- 止损缺失则补挂 ---
    sl_rec = position.get("sl_order") or {}
    sl_alive = bool(sl_rec.get("client_algo_id")) and sl_rec["client_algo_id"] in by_cid
    if not sl_alive:
        sl = dec(position.get("sl", "0"))
        if sl > 0:
            price = get_last_price(symbol)
            if (direction == "LONG" and sl >= price) or (direction == "SHORT" and sl <= price):
                logger.warning("%s 止损 %s 已被击穿（现价 %s），改用现价 ±%s%% 重建保护",
                               symbol, sl, price, CONFIG["default_sl_pct"] * 100)
                _, sl = build_default_tpsl(direction, price)
            try:
                position["sl_order"] = place_stop_loss(symbol, direction, close_side, sl)
                position["sl"] = dec_str(round_price(sl, filters["tick"]))
                logger.warning("%s 止损条件单缺失，已自动补挂 SL=%s", symbol, position["sl"])
                notify(subject=f"[自愈] {symbol} 止损已补挂",
                       body=f"时间: {now_text()}\n标的: {symbol}\n补挂止损: {position['sl']}\n",
                       cooldown_key=f"resl:{symbol}")
            except Exception as exc:
                logger.error("%s 补挂止损失败: %s", symbol, exc)

    # --- 止盈档位缺失则补挂 ---
    tp_done = set(position.get("tp_done", []))
    tp_recs = {int(o.get("level", 0)): o for o in position.get("tp_orders", [])}
    alive_recs = [
        rec for lvl, rec in tp_recs.items()
        if lvl not in tp_done and str(rec.get("client_algo_id")) in by_cid
    ]
    missing_levels = [
        lvl for lvl, rec in tp_recs.items()
        if lvl not in tp_done and str(rec.get("client_algo_id")) not in by_cid
    ]

    if missing_levels:
        last = load_state().get("last_tpsl", {}).get(symbol, {})
        tp_values = [dec(x) for x in last.get("tp", [])]
        if tp_values:
            logger.warning("%s 止盈档位 %s 条件单缺失，按当前剩余仓位重新规划", symbol, missing_levels)
            new_orders = place_tp_ladder(
                symbol, direction, close_side, actual, tp_values,
                skip_levels=sorted(tp_done),
                only_levels=missing_levels,
            )
            if new_orders:
                # 合并：保留仍然有效的旧档记录 + 新补的档，避免下轮重复挂
                position["tp_orders"] = alive_recs + new_orders
        else:
            logger.warning("%s 止盈档位缺失，但没有可参考的止盈价，跳过", symbol)

    state = load_state()
    state["positions"][symbol] = position
    save_state(state)


def adopt_position(symbol: str, signed_qty: Decimal, entry: Decimal) -> None:
    """交易所上有持仓但本地无记录（例如状态文件丢失）-> 接管并补上保护。"""
    direction = "LONG" if signed_qty > 0 else "SHORT"
    price = get_last_price(symbol)
    base = entry if entry > 0 else price
    tp, sl = build_default_tpsl(direction, base)
    filters = get_symbol_filters(symbol)
    tp = round_price(tp, filters["tick"])
    sl = round_price(sl, filters["tick"])
    close_side = "SELL" if direction == "LONG" else "BUY"

    logger.warning("%s 发现未接管持仓 %s，按 %s 接管并补挂 ±%s%% 保护",
                   symbol, signed_qty, direction, CONFIG["default_sl_pct"] * 100)

    cancel_our_algo_orders(symbol)
    sl_order = None
    try:
        sl_order = place_stop_loss(symbol, direction, close_side, sl)
    except Exception as exc:
        logger.error("%s 接管时挂止损失败: %s", symbol, exc)
    tp_orders = place_tp_ladder(symbol, direction, close_side, abs(signed_qty), [tp])

    state = load_state()
    state["positions"][symbol] = {
        "direction": direction,
        "entry_price": dec_str(base),
        "qty": dec_str(abs(signed_qty)),
        "closed_qty": "0",
        "tp": dec_str(tp),
        "sl": dec_str(sl),
        "leverage": CONFIG["default_leverage"],
        "margin_usdt": "0",
        "opened_at": time.time(),
        "tp_done": [],
        "tp_orders": tp_orders,
        "sl_order": sl_order,
        "source": "ADOPT",
    }
    save_state(state)

    notify(
        subject=f"[接管] {symbol} 未记录持仓已接管",
        body=(f"时间: {now_text()}\n标的: {symbol}\n方向: {direction}\n数量: {abs(signed_qty)}\n"
              f"补挂止盈: {tp}\n补挂止损: {sl}\n"),
        cooldown_key=f"adopt:{symbol}", force=True,
    )


def sync_positions() -> None:
    """每分钟体检：对账持仓 + 补齐缺失的条件单。不无脑撤单重挂。"""
    state = load_state()
    local = dict(state.get("positions", {}))

    # 1) 本地记录 -> 交易所核对
    for symbol in list(local.keys()):
        try:
            if get_position_qty(symbol) <= 0:
                logger.info("%s 已无持仓（可能被止损/止盈平掉），清理本地记录", symbol)
                cleanup_position(symbol, cancel_orders=True)
                continue
            ensure_protection(symbol, local[symbol])
        except Exception as exc:
            logger.error("同步 %s 失败: %s", symbol, exc)

    # 2) 交易所 -> 本地未记录持仓
    if CONFIG["adopt_unknown_positions"]:
        try:
            data = _request("GET", "/fapi/v2/positionRisk")
            for item in data if isinstance(data, list) else []:
                symbol = item.get("symbol")
                amt = dec(item.get("positionAmt", "0"))
                if not symbol or amt == 0:
                    continue
                if symbol in load_state().get("positions", {}):
                    continue
                adopt_position(symbol, amt, dec(item.get("entryPrice", "0")))
        except Exception as exc:
            logger.error("扫描未接管持仓失败: %s", exc)


# ==========================================================================
# 13. 主循环
# ==========================================================================
def run_once() -> None:
    consume_signals()
    sync_positions()


def main() -> None:
    parser = argparse.ArgumentParser(description="币安合约推送信号执行器")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--check", action="store_true", help="仅做连通性自检")
    args = parser.parse_args()

    logger.info("=" * 64)
    logger.info("币安推送信号执行器 v%s", VERSION)
    logger.info("ENV=%s | MOCK=%s | 信号轮询=%ss | 持仓体检=%ss",
                CONFIG["env"], CONFIG["mock"], CONFIG["signal_poll_seconds"], CONFIG["sync_seconds"])
    logger.info("信号文件=%s", SIGNAL_FILE)
    logger.info("状态文件=%s", STATE_FILE)
    logger.info("=" * 64)

    if not CONFIG["mock"] and (not CONFIG["api_key"] or not CONFIG["api_secret"]):
        logger.error("未配置 BINANCE_API_KEY / BINANCE_API_SECRET，退出")
        sys.exit(1)

    sync_server_time(force=True)
    is_dual_side()

    if args.check:
        logger.info("自检：现价 ETHUSDT=%s", get_last_price("ETHUSDT"))
        logger.info("自检：ETHUSDT 过滤规则=%s", get_symbol_filters("ETHUSDT"))
        logger.info("自检完成")
        return

    if CONFIG["email"]["test_on_start"]:
        notify(subject="[测试] 邮件通知可用", body=f"时间: {now_text()}\n", force=True)

    notify(
        subject="[启动] 币安推送信号执行器已启动",
        body=(f"时间: {now_text()}\n版本: {VERSION}\n环境: {CONFIG['env']}\n模拟: {CONFIG['mock']}\n"
              f"持仓模式: {'双向' if is_dual_side() else '单向'}\n"
              f"默认保证金: {CONFIG['default_margin_usdt']} USDT x {CONFIG['default_leverage']}x\n"
              f"默认止盈/止损: ±{CONFIG['default_tp_pct'] * 100}%\n"),
        cooldown_key="start", force=True,
    )

    if args.once:
        run_once()
        logger.info("单轮执行完成，退出")
        return

    next_signal = 0.0
    next_sync = 0.0
    while True:
        try:
            now = time.time()
            if now >= next_signal:
                consume_signals()
                next_signal = now + CONFIG["signal_poll_seconds"]

            if now >= next_sync:
                try:
                    sync_positions()
                except Exception as exc:
                    logger.error("持仓体检失败: %s", exc)
                    notify(subject="[失败] 持仓体检异常",
                           body=f"时间: {now_text()}\n错误: {exc}\n", cooldown_key="sync_err")
                next_sync = now + CONFIG["sync_seconds"]

            time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            notify(subject="[停止] 执行器已停止", body=f"时间: {now_text()}\n", cooldown_key="stop", force=True)
            break
        except Exception as exc:
            logger.exception("主循环异常")
            notify(subject="[失败] 执行器主循环异常",
                   body=f"时间: {now_text()}\n错误: {exc}\n", cooldown_key="loop_err")
            time.sleep(5)


if __name__ == "__main__":
    main()
