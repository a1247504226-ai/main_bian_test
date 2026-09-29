import json
import logging
import os
import smtplib
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import matplotlib
import numpy as np
import pandas as pd
import pytz
import requests
from matplotlib.gridspec import GridSpec
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from bian_demo_creat_order1 import place_trade
except ImportError:
    def place_trade(**kwargs):
        logging.getLogger(__name__).warning("未找到 bian_demo_creat_order1.place_trade，当前仅做模拟记录")
        return {
            "success": False,
            "message": "place_trade 未导入，未实际下单",
            "request": kwargs,
        }


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "short_term_monitor_5m_entry.log"
CHART_DIR = BASE_DIR / "charts"
STATE_FILE = BASE_DIR / "monitor_state_5m_entry.json"
CHART_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(module)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


tz_shanghai = pytz.timezone("Asia/Shanghai")

plt.rcParams.update(
    {
        "font.family": "SimHei",
        "axes.unicode_minus": False,
        "savefig.dpi": 150,
        "figure.figsize": (16, 12),
    }
)

CONFIG: Dict[str, Dict[str, Any]] = {
    "email": {
        "from": os.getenv("MONITOR_EMAIL_FROM", ""),
        "to": os.getenv("MONITOR_EMAIL_TO", ""),
        "password": os.getenv("MONITOR_EMAIL_PASSWORD", ""),
        "server": os.getenv("MONITOR_EMAIL_SERVER", "smtp.163.com"),
        "port": int(os.getenv("MONITOR_EMAIL_PORT", "465")),
        "max_retries": 3,
        "cooldown": timedelta(minutes=1),
        "alert_cooldown": timedelta(minutes=30),
    },
    "network": {
        "http_proxy": os.getenv("HTTP_PROXY", ""),
        "https_proxy": os.getenv("HTTPS_PROXY", ""),
        "request_timeout": 20,
    },
    "trading": {
        "symbols": ["BTCUSDT", "ETHUSDT", "SOXLUSDT", "SNDKUSDT", "BNBUSDT"],
        "data_limit": 500,
        "signal_cooldown_minutes": 30,
        "missing_data_threshold": 0.05,
        "min_bars": 120,
        "execution_mode": "real_time_ticker",  # real_time_ticker | closed_bar_close
        "timeframe_alignment": {
            "4h": pd.Timedelta(hours=4),
            "1h": pd.Timedelta(hours=1),
            "15m": pd.Timedelta(minutes=15),
            "5m": pd.Timedelta(minutes=5),
        },
    },
    "indicators": {
        "rsi_fast": 14,
        "rsi_slow": 27,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "kdj_period": 9,
        "atr_period": 14,
        "adx_period": 14,
    },
    "signal": {
        "adx_min": 30,
        "rsi_long_min": 40,
        "rsi_long_max": 90,
        "rsi_short_max": 50,
    },
    "risk": {
        "usdt_per_trade": 50,
        "leverage": 20,
        "min_rr": 1.25,
        "max_stop_pct": 0.035,
        "max_take_profit_pct": 0.05,
        "weak_trend_adx": 30,
        "strong_trend_adx": 45,
        "support_buffer_atr": 0.18,
        "resistance_buffer_atr": 0.15,
        "stop_atr_floor": 0.8,
        "stop_atr_cap": 1.4,
        "tp_atr_floor": 1.1,
        "tp_atr_cap": 2.2,
        "structure_lookback_5m": 48,
    },
    "runtime": {
        "duration": timedelta(days=7),
        "check_interval": 300,
    },
}

DYNAMIC_PARAMS = {
    "BTCUSDT": {"signal_cooldown_minutes": 30},
    "ETHUSDT": {"signal_cooldown_minutes": 30},
    "SOXLUSDT": {"signal_cooldown_minutes": 30},
    "SNDKUSDT": {"signal_cooldown_minutes": 30},
    "BNBUSDT": {"signal_cooldown_minutes": 30},
}


@dataclass
class TradePlan:
    stop_loss: float
    take_profit: float
    trailing_stop: float
    rr: float
    stop_loss_pct: float
    take_profit_pct: float
    reason: str
    signal_price: float
    execution_price: float
    signal_time: str
    adx: float
    atr: float


class GlobalState:
    def __init__(self) -> None:
        self.last_signals: Dict[str, datetime] = {}
        self.last_emails: Dict[str, datetime] = {}
        self.last_alerts: Dict[str, datetime] = {}
        self.system_health = {"last_check": datetime.now(tz_shanghai)}
        self.trade_history = defaultdict(list)
        self.exchange_filters: Dict[str, Dict[str, Any]] = {}


state = GlobalState()


def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    proxies = {}
    if CONFIG["network"]["http_proxy"]:
        proxies["http"] = CONFIG["network"]["http_proxy"]
    if CONFIG["network"]["https_proxy"]:
        proxies["https"] = CONFIG["network"]["https_proxy"]
    if proxies:
        session.proxies.update(proxies)

    return session


SESSION = build_http_session()


def serialize_datetime_map(data: Dict[str, datetime]) -> Dict[str, str]:
    return {
        key: value.astimezone(tz_shanghai).isoformat() if isinstance(value, datetime) else str(value)
        for key, value in data.items()
    }



def load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.last_signals = {
            key: datetime.fromisoformat(value).astimezone(tz_shanghai)
            for key, value in raw.get("last_signals", {}).items()
        }
        state.last_emails = {
            key: datetime.fromisoformat(value).astimezone(tz_shanghai)
            for key, value in raw.get("last_emails", {}).items()
        }
        state.last_alerts = {
            key: datetime.fromisoformat(value).astimezone(tz_shanghai)
            for key, value in raw.get("last_alerts", {}).items()
        }
        state.trade_history = defaultdict(list, raw.get("trade_history", {}))
        logger.info("已加载历史状态文件")
    except Exception as exc:
        logger.warning("状态文件加载失败，继续使用空状态: %s", exc)



def save_state() -> None:
    try:
        payload = {
            "last_signals": serialize_datetime_map(state.last_signals),
            "last_emails": serialize_datetime_map(state.last_emails),
            "last_alerts": serialize_datetime_map(state.last_alerts),
            "trade_history": dict(state.trade_history),
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("保存状态文件失败: %s", exc, exc_info=True)


def decimal_from_str(value: Any) -> Decimal:
    return Decimal(str(value))



def round_by_tick(value: float, tick_size: float, direction: str = "nearest") -> float:
    if tick_size <= 0:
        return float(value)
    tick = decimal_from_str(tick_size)
    price = decimal_from_str(value)
    steps = price / tick

    if direction == "up":
        rounded = steps.to_integral_value(rounding=ROUND_CEILING) * tick
    elif direction == "down":
        rounded = steps.to_integral_value(rounding=ROUND_FLOOR) * tick
    else:
        rounded = steps.to_integral_value(rounding=ROUND_HALF_UP) * tick

    return float(rounded)



def load_exchange_info() -> None:
    if state.exchange_filters:
        return
    try:
        response = SESSION.get(
            "https://fapi.binance.com/fapi/v1/exchangeInfo",
            timeout=CONFIG["network"]["request_timeout"],
        )
        response.raise_for_status()
        data = response.json()
        for symbol_info in data.get("symbols", []):
            filters = {f["filterType"]: f for f in symbol_info.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            state.exchange_filters[symbol_info.get("symbol")] = {
                "tick_size": float(price_filter.get("tickSize", 0) or 0),
                "price_precision": symbol_info.get("pricePrecision", 8),
            }
        logger.info("已加载 Binance Futures 价格精度规则")
    except Exception as exc:
        logger.error("加载交易规则失败: %s", exc, exc_info=True)



def format_trade_prices(symbol: str, signal_type: int, stop_loss: float, take_profit: float) -> Tuple[float, float]:
    tick_size = state.exchange_filters.get(symbol, {}).get("tick_size", 0)

    if signal_type == 1:
        return round_by_tick(stop_loss, tick_size, "down"), round_by_tick(take_profit, tick_size, "down")
    return round_by_tick(stop_loss, tick_size, "up"), round_by_tick(take_profit, tick_size, "up")



def fetch_latest_price(symbol: str) -> Optional[float]:
    try:
        response = SESSION.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=CONFIG["network"]["request_timeout"],
        )
        response.raise_for_status()
        return float(response.json()["price"])
    except Exception as exc:
        logger.warning("获取实时价格失败(%s): %s", symbol, exc)
        return None


def process_raw_data(raw_data: list) -> pd.DataFrame:
    try:
        cleaned = []
        for bar in raw_data:
            if len(bar) >= 6:
                try:
                    cleaned.append(
                        [
                            float(bar[1]),
                            float(bar[2]),
                            float(bar[3]),
                            float(bar[4]),
                            float(bar[5]),  # 修复：使用 base asset volume，而不是 quote volume(bar[7])
                            int(bar[0]),
                        ]
                    )
                except (ValueError, TypeError):
                    continue

        if not cleaned:
            return pd.DataFrame()

        df = pd.DataFrame(
            cleaned,
            columns=["Open", "High", "Low", "Close", "Volume", "Timestamp"],
        )
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="ms", utc=True).dt.tz_convert(tz_shanghai)
        df = df.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp")
        return df.set_index("Timestamp")
    except Exception as exc:
        logger.error("数据处理失败: %s", exc, exc_info=True)
        return pd.DataFrame()



def validate_market_data(df: pd.DataFrame, symbol: str, interval: str) -> pd.DataFrame:
    try:
        if df.empty:
            return df

        if len(df) < CONFIG["trading"]["min_bars"]:
            logger.warning("%s %s K线不足，当前仅 %s 根", symbol, interval, len(df))
            return pd.DataFrame()

        alignment = CONFIG["trading"]["timeframe_alignment"]
        if interval not in alignment:
            logger.error("%s 不支持的周期: %s，跳过", symbol, interval)
            return pd.DataFrame()

        delta = alignment[interval]
        expected_gaps = max(len(df) - 1, 1)
        actual_gaps = df.index.to_series().diff().dropna()
        missing_ratio = float((actual_gaps > delta * 1.5).sum()) / expected_gaps
        if missing_ratio > CONFIG["trading"]["missing_data_threshold"]:
            logger.warning("%s %s 缺口比例过高 %.2f%%，跳过", symbol, interval, missing_ratio * 100)
            return pd.DataFrame()

        numeric_cols = ["Open", "High", "Low", "Close", "Volume"]
        if df[numeric_cols].isnull().any().any():
            logger.warning("%s %s 存在空值，使用前向填充后再次校验", symbol, interval)
            df[numeric_cols] = df[numeric_cols].ffill()
        if df[numeric_cols].isnull().any().any():
            logger.warning("%s %s 清洗后仍有空值，跳过", symbol, interval)
            return pd.DataFrame()

        if (df["Close"] <= 0).any() or (df["Volume"] < 0).any():
            logger.warning("%s %s 存在非法价格或成交量", symbol, interval)
            return pd.DataFrame()

        return df
    except Exception as exc:
        logger.error("数据完整性校验失败(%s-%s): %s", symbol, interval, exc, exc_info=True)
        return pd.DataFrame()



def fetch_market_data(symbol: str, interval: str = "1h", limit: int = CONFIG["trading"]["data_limit"]) -> pd.DataFrame:
    try:
        response = SESSION.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=CONFIG["network"]["request_timeout"],
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            send_alert(f"数据接口返回空数据：{symbol}-{interval}")
            return pd.DataFrame()

        return validate_market_data(process_raw_data(data), symbol, interval)
    except Exception as exc:
        logger.error("数据获取失败(%s-%s): %s", symbol, interval, exc, exc_info=True)
        return pd.DataFrame()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = series.diff(1)
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.clip(0, 100).fillna(50)
    except Exception as exc:
        logger.error("RSI计算失败: %s", exc, exc_info=True)
        return pd.Series([50] * len(series), index=series.index)



def calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    try:
        series = series.astype(float)
        dif = series.ewm(span=fast, adjust=False).mean() - series.ewm(span=slow, adjust=False).mean()
        dea = dif.ewm(span=signal, adjust=False).mean()
        return dif.fillna(0), dea.fillna(0), (dif - dea).fillna(0)
    except Exception as exc:
        logger.error("MACD计算失败: %s", exc, exc_info=True)
        return (
            pd.Series([0] * len(series), index=series.index),
            pd.Series([0] * len(series), index=series.index),
            pd.Series([0] * len(series), index=series.index),
        )



def calculate_kdj(df: pd.DataFrame, period: int = 9):
    low = df["Low"].rolling(period, min_periods=period).min()
    high = df["High"].rolling(period, min_periods=period).max()
    price_range = (high - low).replace(0, np.nan)
    rsv = ((df["Close"] - low) / price_range * 100).clip(0, 100).fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d, 3 * k - 2 * d



def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().bfill().round(6)



def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    try:
        high, low, close = df["High"], df["Low"], df["Close"]

        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

        tr = pd.concat(
            [
                (high - low),
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
        return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(20)
    except Exception as exc:
        logger.error("ADX计算失败: %s", exc, exc_info=True)
        return pd.Series([20] * len(df), index=df.index)



def calculate_technical_indicators(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    params = {**CONFIG["indicators"], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    try:
        df["ATR"] = calculate_atr(df, params["atr_period"])
        df["ADX"] = calculate_adx(df, params["adx_period"])
        df["RSI14"] = calculate_rsi(df["Close"], params["rsi_fast"])
        df["RSI27"] = calculate_rsi(df["Close"], params["rsi_slow"])
        df["K"], df["D"], df["J"] = calculate_kdj(df, params["kdj_period"])
        df["DIF"], df["DEA"], df["MACD_HIST"] = calculate_macd(
            df["Close"], params["macd_fast"], params["macd_slow"], params["macd_signal"]
        )
        return df
    except Exception as exc:
        logger.error("指标计算失败(%s): %s", symbol, exc, exc_info=True)
        return df


def get_previous_day_pivot(symbol: str) -> Optional[Dict[str, float]]:
    try:
        response = SESSION.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 2},
            timeout=CONFIG["network"]["request_timeout"],
        )
        response.raise_for_status()
        data = response.json()
        if not data or len(data) < 2:
            logger.warning("%s 日线Pivot数据不足", symbol)
            return None

        candle = data[-2]
        high_price = float(candle[2])
        low_price = float(candle[3])
        close_price = float(candle[4])

        pp = (high_price + low_price + close_price) / 3
        return {
            "H": high_price,
            "L": low_price,
            "C": close_price,
            "PP": pp,
            "R1": 2 * pp - low_price,
            "R2": pp + (high_price - low_price),
            "S1": 2 * pp - high_price,
            "S2": pp - (high_price - low_price),
        }
    except Exception as exc:
        logger.error("%s 获取Pivot失败: %s", symbol, exc, exc_info=True)
        return None



def get_recent_structure_levels(df: pd.DataFrame, lookback: int) -> Dict[str, float]:
    recent = df.iloc[-(lookback + 2):-2].copy()
    if recent.empty:
        recent = df.iloc[:-2].copy()

    return {
        "recent_high": float(recent["High"].max()) if not recent.empty else float(df["High"].iloc[-2]),
        "recent_low": float(recent["Low"].min()) if not recent.empty else float(df["Low"].iloc[-2]),
        "swing_high": float(recent.tail(8)["High"].max()) if len(recent) >= 8 else float(df["High"].iloc[-2]),
        "swing_low": float(recent.tail(8)["Low"].min()) if len(recent) >= 8 else float(df["Low"].iloc[-2]),
    }



def compute_rr(entry_price: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    if risk <= 1e-12:
        return 0.0
    return reward / risk



def calculate_optimized_positions(
    signal_type: int,
    signal_price: float,
    execution_price: float,
    atr: float,
    adx: float,
    pivot: Optional[Dict[str, float]],
    structure: Dict[str, float],
) -> TradePlan:
    risk_cfg = CONFIG["risk"]

    if adx < risk_cfg["weak_trend_adx"]:
        stop_atr_mult, tp_atr_mult, trail_atr_mult = 0.9, 1.2, 0.8
        trend_label = "弱趋势，采用更近的止盈和更紧的保护"
    elif adx >= risk_cfg["strong_trend_adx"]:
        stop_atr_mult, tp_atr_mult, trail_atr_mult = 1.15, 2.0, 1.2
        trend_label = "强趋势，允许更大的止盈空间"
    else:
        stop_atr_mult, tp_atr_mult, trail_atr_mult = 1.0, 1.6, 1.0
        trend_label = "中等趋势，使用平衡型止盈止损"

    stop_min_dist = atr * risk_cfg["stop_atr_floor"]
    stop_max_dist = atr * risk_cfg["stop_atr_cap"]
    tp_min_dist = atr * risk_cfg["tp_atr_floor"]
    tp_max_dist = atr * risk_cfg["tp_atr_cap"]

    atr_stop_distance = atr * stop_atr_mult
    atr_tp_distance = atr * tp_atr_mult

    if signal_type == 1:
        support_candidates = [structure["recent_low"], structure["swing_low"]]
        resistance_candidates = [structure["recent_high"], structure["swing_high"]]
        if pivot:
            support_candidates.extend([pivot["PP"], pivot["S1"], pivot["S2"]])
            resistance_candidates.extend([pivot["PP"], pivot["R1"], pivot["R2"]])

        support_candidates = sorted({x for x in support_candidates if x < signal_price}, reverse=True)
        resistance_candidates = sorted({x for x in resistance_candidates if x > signal_price})

        structure_stop = (
            support_candidates[0] - atr * risk_cfg["support_buffer_atr"]
            if support_candidates
            else signal_price - atr_stop_distance
        )
        structure_tp = (
            resistance_candidates[0] - atr * risk_cfg["resistance_buffer_atr"]
            if resistance_candidates
            else signal_price + atr_tp_distance
        )

        stop_loss = signal_price - min(max(signal_price - structure_stop, stop_min_dist), stop_max_dist)
        take_profit = signal_price + min(max(structure_tp - signal_price, tp_min_dist), tp_max_dist)

        if take_profit <= signal_price:
            take_profit = signal_price + atr_tp_distance
        if stop_loss >= signal_price:
            stop_loss = signal_price - atr_stop_distance
    else:
        support_candidates = [structure["recent_low"], structure["swing_low"]]
        resistance_candidates = [structure["recent_high"], structure["swing_high"]]
        if pivot:
            support_candidates.extend([pivot["PP"], pivot["S1"], pivot["S2"]])
            resistance_candidates.extend([pivot["R1"], pivot["R2"], pivot["PP"]])

        support_candidates = sorted({x for x in support_candidates if x < signal_price}, reverse=True)
        resistance_candidates = sorted({x for x in resistance_candidates if x > signal_price})

        structure_stop = (
            resistance_candidates[0] + atr * risk_cfg["resistance_buffer_atr"]
            if resistance_candidates
            else signal_price + atr_stop_distance
        )
        structure_tp = (
            support_candidates[0] + atr * risk_cfg["resistance_buffer_atr"]
            if support_candidates
            else signal_price - atr_tp_distance
        )

        stop_loss = signal_price + min(max(structure_stop - signal_price, stop_min_dist), stop_max_dist)
        take_profit = signal_price - min(max(signal_price - structure_tp, tp_min_dist), tp_max_dist)

        if take_profit >= signal_price:
            take_profit = signal_price - atr_tp_distance
        if stop_loss <= signal_price:
            stop_loss = signal_price + atr_stop_distance

    rr = compute_rr(execution_price, stop_loss, take_profit)
    min_rr = risk_cfg["min_rr"]

    if rr < min_rr:
        risk_distance = abs(execution_price - stop_loss)
        target_reward = max(risk_distance * min_rr, tp_min_dist)
        if signal_type == 1:
            take_profit = max(take_profit, execution_price + min(target_reward, tp_max_dist))
        else:
            take_profit = min(take_profit, execution_price - min(target_reward, tp_max_dist))
        rr = compute_rr(execution_price, stop_loss, take_profit)

    return TradePlan(
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_stop=atr * trail_atr_mult,
        rr=rr,
        stop_loss_pct=abs(execution_price - stop_loss) / max(execution_price, 1e-12),
        take_profit_pct=abs(take_profit - execution_price) / max(execution_price, 1e-12),
        reason=(
            f"{trend_label}；止损参考5m结构位并留{risk_cfg['support_buffer_atr']:.2f}ATR缓冲，"
            f"止盈挂在最近阻力/支撑位前{risk_cfg['resistance_buffer_atr']:.2f}ATR，"
            f"再用最小RR={min_rr:.2f}校准。"
        ),
        signal_price=signal_price,
        execution_price=execution_price,
        signal_time=datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        adx=adx,
        atr=atr,
    )


def check_multi_timeframe_signal(df4h: pd.DataFrame, df1h: pd.DataFrame, df15m: pd.DataFrame, df5m: pd.DataFrame):
    try:
        h4, h1, m15 = df4h.iloc[-2], df1h.iloc[-2], df15m.iloc[-2]
        m5, m5_prev = df5m.iloc[-2], df5m.iloc[-3]

        h4_long = h4["DIF"] > h4["DEA"] and h4["MACD_HIST"] > 0 and h4["RSI14"] > h4["RSI27"] > 30 and h4["J"] > h4["K"] > h4["D"]
        h4_short = h4["DIF"] < h4["DEA"] and h4["MACD_HIST"] < 0 and h4["RSI14"] < h4["RSI27"] < 80 and h4["J"] < h4["K"] < h4["D"]

        h1_long = h1["DIF"] > h1["DEA"] and h1["MACD_HIST"] > 0 and h1["RSI14"] > h1["RSI27"] > 30 and h1["J"] > h1["K"] > h1["D"]
        h1_short = h1["DIF"] < h1["DEA"] and h1["MACD_HIST"] < 0 and h1["RSI14"] < h1["RSI27"] < 80 and h1["J"] < h1["K"] < h1["D"]

        m15_long = m15["DIF"] > m15["DEA"] and m15["MACD_HIST"] > 0 and m15["RSI14"] > m15["RSI27"] > 30 and m15["J"] > m15["K"] > m15["D"]
        m15_short = m15["DIF"] < m15["DEA"] and m15["MACD_HIST"] < 0 and m15["RSI14"] < m15["RSI27"] < 80 and m15["J"] < m15["K"] < m15["D"]

        kdj_golden_cross = m5_prev["K"] <= m5_prev["D"] and m5["K"] > m5["D"]
        kdj_death_cross = m5_prev["K"] >= m5_prev["D"] and m5["K"] < m5["D"]
        macd_golden_cross = m5_prev["DIF"] <= m5_prev["DEA"] and m5["DIF"] > m5["DEA"]
        macd_death_cross = m5_prev["DIF"] >= m5_prev["DEA"] and m5["DIF"] < m5["DEA"]

        m5_long_trigger = (kdj_golden_cross and m5["DIF"] > m5["DEA"]) or (macd_golden_cross and m5["J"] > m5["K"] > m5["D"])
        m5_short_trigger = (kdj_death_cross and m5["DIF"] < m5["DEA"]) or (macd_death_cross and m5["J"] < m5["K"] < m5["D"])

        signal_cfg = CONFIG["signal"]
        rsi5m = float(m5["RSI14"])
        rsi_long_filter = signal_cfg["rsi_long_min"] <= rsi5m <= signal_cfg["rsi_long_max"]
        rsi_short_filter = rsi5m < signal_cfg["rsi_short_max"]

        adx = float(m5["ADX"])
        trend_strength = adx > signal_cfg["adx_min"]

        if h4_long and h1_long and m15_long and m5_long_trigger and rsi_long_filter and trend_strength:
            signal = 1
        elif h4_short and h1_short and m15_short and m5_short_trigger and rsi_short_filter and trend_strength:
            signal = -1
        else:
            signal = 0

        details = {
            "signal": signal,
            "h4_long": h4_long,
            "h4_short": h4_short,
            "h1_long": h1_long,
            "h1_short": h1_short,
            "m15_long": m15_long,
            "m15_short": m15_short,
            "kdj_golden_cross": kdj_golden_cross,
            "kdj_death_cross": kdj_death_cross,
            "macd_golden_cross": macd_golden_cross,
            "macd_death_cross": macd_death_cross,
            "m5_long_trigger": m5_long_trigger,
            "m5_short_trigger": m5_short_trigger,
            "adx": adx,
            "rsi5m": rsi5m,
            "rsi_long_filter": rsi_long_filter,
            "rsi_short_filter": rsi_short_filter,
            "trend_strength": trend_strength,
        }
        return signal, details
    except Exception as exc:
        logger.error("多周期信号计算失败: %s", exc, exc_info=True)
        return 0, {}


def generate_chart(
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
    df5m: pd.DataFrame,
    symbol: str,
    signal_type_str: str,
    trade_plan: Optional[TradePlan] = None,
) -> Optional[str]:
    try:
        required_1h = ["Open", "High", "Low", "Close", "Volume", "RSI14", "RSI27", "K", "D", "J"]
        required_4h = ["MACD_HIST", "DIF", "DEA"]
        if any(col not in df1h.columns for col in required_1h) or any(col not in df4h.columns for col in required_4h):
            logger.error("%s 图表生成失败：指标字段不完整", symbol)
            return None

        candles = df1h.tail(80).copy()
        macd_data = df4h.tail(80).copy()
        if candles.empty:
            return None

        fig = plt.figure(figsize=(16, 13), constrained_layout=False)
        gs = GridSpec(4, 1, figure=fig, height_ratios=[3.2, 1.7, 1.7, 2.0], hspace=0.08)
        ax1, ax2, ax3, ax4 = (fig.add_subplot(gs[i]) for i in range(4))

        for i, (_, row) in enumerate(candles.iterrows()):
            # 中国市场默认：涨=红，跌=绿
            candle_color = "#ef5350" if row["Close"] >= row["Open"] else "#26a69a"
            ax1.plot([i, i], [row["Low"], row["High"]], color=candle_color, linewidth=1)

            body_low, body_high = min(row["Open"], row["Close"]), max(row["Open"], row["Close"])
            body_height = body_high - body_low
            if body_height <= 0:
                body_height = max(abs(row["Close"]) * 0.0002, 1e-8)

            ax1.bar(i, body_height, bottom=body_low, width=0.65, color=candle_color, edgecolor=candle_color)

        current_price = candles["Close"].iloc[-1]
        ax1.axhline(current_price, linestyle="--", linewidth=0.8, alpha=0.7)
        ax1.text(len(candles) - 1, current_price, f"  现价 {current_price:.4f}", fontsize=9, verticalalignment="bottom")

        if trade_plan:
            ax1.axhline(trade_plan.stop_loss, linestyle="--", linewidth=1.0, color="#2e7d32", alpha=0.8)
            ax1.axhline(trade_plan.take_profit, linestyle="--", linewidth=1.0, color="#c62828", alpha=0.8)
            ax1.text(len(candles) - 1, trade_plan.stop_loss, f"  SL {trade_plan.stop_loss:.4f}", color="#2e7d32")
            ax1.text(len(candles) - 1, trade_plan.take_profit, f"  TP {trade_plan.take_profit:.4f}", color="#c62828")

        if signal_type_str == "LONG":
            ax1.scatter(len(candles) - 1, current_price, marker="^", s=180, c="#ef5350", edgecolors="black", linewidths=0.8, zorder=10, label="LONG")
        elif signal_type_str == "SHORT":
            ax1.scatter(len(candles) - 1, current_price, marker="v", s=180, c="#26a69a", edgecolors="black", linewidths=0.8, zorder=10, label="SHORT")

        signal_cn = {"LONG": "做多", "SHORT": "做空"}.get(signal_type_str, "无信号")
        ax1.set_title(f"{symbol} | 1h K线 | 当前信号：{signal_cn}", fontsize=14, fontweight="bold", pad=10)
        ax1.set_ylabel("价格")
        ax1.grid(True, linestyle="--", alpha=0.25)
        if signal_type_str in ("LONG", "SHORT"):
            ax1.legend(loc="upper left", fontsize=9)

        x_macd = np.arange(len(macd_data))
        hist_values = macd_data["MACD_HIST"].fillna(0)
        hist_colors = ["#ef5350" if value >= 0 else "#26a69a" for value in hist_values]
        ax2.bar(x_macd, hist_values, color=hist_colors, alpha=0.65, width=0.7, label="MACD Hist")
        ax2.plot(x_macd, macd_data["DIF"], linewidth=1.2, label="DIF")
        ax2.plot(x_macd, macd_data["DEA"], linewidth=1.2, label="DEA")
        ax2.axhline(0, linestyle="--", linewidth=0.8, alpha=0.6)
        ax2.set_ylabel("4h MACD")
        ax2.grid(True, linestyle="--", alpha=0.25)
        ax2.legend(loc="upper left", fontsize=8, ncol=3)

        x_rsi = np.arange(len(candles))
        ax3.plot(x_rsi, candles["RSI14"], linewidth=1.5, label="RSI14")
        ax3.plot(x_rsi, candles["RSI27"], linewidth=1.2, label="RSI27")
        ax3.axhline(70, linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.axhline(30, linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.axhline(50, linestyle=":", linewidth=0.7, alpha=0.5)
        ax3.set_ylim(0, 100)
        ax3.set_ylabel("1h RSI")
        ax3.grid(True, linestyle="--", alpha=0.25)
        ax3.legend(loc="upper left", fontsize=8)

        x_kdj = np.arange(len(candles))
        volume_colors = ["#ef5350" if row["Close"] >= row["Open"] else "#26a69a" for _, row in candles.iterrows()]
        ax4.bar(x_kdj, candles["Volume"], color=volume_colors, alpha=0.30, width=0.7, label="Volume")
        ax4.set_ylabel("成交量")
        ax4_twin = ax4.twinx()
        ax4_twin.plot(x_kdj, candles["K"], linewidth=1.3, label="K")
        ax4_twin.plot(x_kdj, candles["D"], linewidth=1.3, label="D")
        ax4_twin.plot(x_kdj, candles["J"], linewidth=1.0, linestyle="--", label="J")
        ax4_twin.axhline(80, linestyle="--", linewidth=0.7, alpha=0.6)
        ax4_twin.axhline(20, linestyle="--", linewidth=0.7, alpha=0.6)
        ax4_twin.set_ylim(-10, 110)
        ax4_twin.set_ylabel("KDJ")
        ax4.grid(True, linestyle="--", alpha=0.25)
        ax4.legend(loc="upper left", fontsize=8)
        ax4_twin.legend(loc="upper right", fontsize=8)

        step = max(1, len(candles) // 8)
        tick_positions = list(range(0, len(candles), step))
        tick_labels = [candles.index[i].strftime("%m-%d\n%H:%M") for i in tick_positions]
        ax4.set_xticks(tick_positions)
        ax4.set_xticklabels(tick_labels, fontsize=8)
        ax4.set_xlabel("1h K线时间")

        ax1.tick_params(axis="x", labelbottom=False)
        ax2.tick_params(axis="x", labelbottom=False)
        ax3.tick_params(axis="x", labelbottom=False)
        fig.suptitle(f"{symbol} 多周期技术分析 | 4h + 1h + 15m趋势 + 5m入场", fontsize=16, fontweight="bold")

        chart_path = CHART_DIR / f"{symbol}_{datetime.now(tz_shanghai).strftime('%Y%m%d_%H%M%S')}_{signal_type_str}.png"
        plt.savefig(chart_path, bbox_inches="tight", dpi=150)
        plt.close(fig)
        logger.info("图表已生成: %s", chart_path)
        return str(chart_path)
    except Exception as exc:
        logger.error("图表生成失败(%s): %s", symbol, exc, exc_info=True)
        plt.close("all")
        return None



def should_send_email() -> bool:
    email_cfg = CONFIG["email"]
    return all(bool(item) for item in [email_cfg["from"], email_cfg["to"], email_cfg["password"]])



def send_notification(symbol: str, signal_type: str, details: list, chart_path: Optional[str]) -> bool:
    if not should_send_email():
        logger.warning("邮件配置未完成，跳过邮件发送")
        return False

    try:
        msg = MIMEMultipart("related")
        msg["From"] = CONFIG["email"]["from"]
        msg["To"] = CONFIG["email"]["to"]
        signal_cn = "做多" if signal_type == "LONG" else "做空" if signal_type == "SHORT" else "警报"

        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f5f6fa;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 10px; margin-bottom: 20px;">
              <h3 style="color: #ffffff; margin: 0;">{symbol} {signal_cn}信号触发</h3>
            </div>
            <div style="background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px;">
              <h4 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 10px;">监控摘要</h4>
              {'<br>'.join(f'<p style="margin: 8px 0; color: #34495e;">• {line}</p>' for line in details)}
            </div>
            {f'<div style="background: white; padding: 15px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center;">' if chart_path else ''}
            {f'<img src="cid:chart" style="max-width: 100%; border-radius: 8px;"><br>' if chart_path else ''}
            {f'<p style="color: #7f8c8d; font-size: 12px; margin-top: 10px;">技术分析图表 - {symbol}</p>' if chart_path else ''}
            {f'</div>' if chart_path else ''}
            <hr style="border: 0.5px solid #ecf0f1; margin: 20px 0;">
            <p style="color: #95a5a6; font-size: 12px; text-align: center;">
              此邮件由短线策略监控系统自动发送 | {datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}
            </p>
          </body>
        </html>
        """
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        if chart_path and os.path.exists(chart_path):
            with open(chart_path, "rb") as file_obj:
                image = MIMEImage(file_obj.read(), Name=os.path.basename(chart_path))
                image.add_header("Content-ID", "<chart>")
                msg.attach(image)

        for attempt in range(CONFIG["email"]["max_retries"]):
            try:
                with smtplib.SMTP_SSL(CONFIG["email"]["server"], CONFIG["email"]["port"]) as server:
                    server.login(CONFIG["email"]["from"], CONFIG["email"]["password"])
                    server.sendmail(CONFIG["email"]["from"], CONFIG["email"]["to"], msg.as_string())
                logger.info("%s 邮件发送成功，附加图表：%s", symbol, bool(chart_path))
                return True
            except Exception as exc:
                logger.error("邮件发送失败(%s) - 尝试 %s/%s: %s", symbol, attempt + 1, CONFIG["email"]["max_retries"], exc)
                time.sleep(5)
        return False
    except Exception as exc:
        logger.error("邮件发送失败(%s): %s", symbol, exc, exc_info=True)
        return False



def send_alert(message: str) -> None:
    now = datetime.now(tz_shanghai)
    last_alert_time = state.last_alerts.get("SYSTEM")
    if last_alert_time and (now - last_alert_time) < CONFIG["email"]["alert_cooldown"]:
        logger.warning("系统警报冷却中，跳过重复告警")
        return
    state.last_alerts["SYSTEM"] = now
    send_notification("SYSTEM", "ALERT", [message], chart_path=None)


def is_trade_success(trade_result: Any) -> bool:
    if isinstance(trade_result, bool):
        return trade_result
    if isinstance(trade_result, dict):
        if trade_result.get("success") is True:
            return True
        return str(trade_result.get("status", "")).upper() in {"OK", "SUCCESS", "FILLED", "NEW", "ACCEPTED"}
    return bool(trade_result)



def create_signal_details(
    symbol: str,
    signal_type_str: str,
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
    df15m: pd.DataFrame,
    df5m: pd.DataFrame,
    signal_details: Dict[str, Any],
    trade_plan: TradePlan,
) -> list:
    h4, h1, m15, m5 = df4h.iloc[-2], df1h.iloc[-2], df15m.iloc[-2], df5m.iloc[-2]

    return [
        f"标的: {symbol}",
        f"方向: {signal_type_str}",
        f"信号时间(5m收盘): {df5m.index[-2].strftime('%Y-%m-%d %H:%M:%S')}",
        f"信号价: {trade_plan.signal_price:.4f}",
        f"执行参考价: {trade_plan.execution_price:.4f}",
        "========== 4h 趋势过滤 ==========",
        f"4h DIF/DEA: {h4['DIF']:.5f} / {h4['DEA']:.5f}",
        f"4h RSI14/RSI27: {h4['RSI14']:.2f} / {h4['RSI27']:.2f}",
        f"4h K/D/J: {h4['K']:.2f} / {h4['D']:.2f} / {h4['J']:.2f}",
        "========== 1h 趋势过滤 ==========",
        f"1h DIF/DEA: {h1['DIF']:.5f} / {h1['DEA']:.5f}",
        f"1h RSI14/RSI27: {h1['RSI14']:.2f} / {h1['RSI27']:.2f}",
        f"1h K/D/J: {h1['K']:.2f} / {h1['D']:.2f} / {h1['J']:.2f}",
        "========== 15m 趋势过滤 ==========",
        f"15m DIF/DEA: {m15['DIF']:.5f} / {m15['DEA']:.5f}",
        f"15m RSI14/RSI27: {m15['RSI14']:.2f} / {m15['RSI27']:.2f}",
        f"15m K/D/J: {m15['K']:.2f} / {m15['D']:.2f} / {m15['J']:.2f}",
        "========== 5m 入场触发 ==========",
        f"5m K/D/J: {m5['K']:.2f} / {m5['D']:.2f} / {m5['J']:.2f}",
        f"KDJ金叉: {'是' if signal_details.get('kdj_golden_cross') else '否'}",
        f"MACD金叉: {'是' if signal_details.get('macd_golden_cross') else '否'}",
        f"5m多头触发: {'是' if signal_details.get('m5_long_trigger') else '否'}",
        f"5m空头触发: {'是' if signal_details.get('m5_short_trigger') else '否'}",
        f"5m ADX: {trade_plan.adx:.2f}",
        f"5m RSI14: {signal_details.get('rsi5m', 0):.2f}",
        "========== 风险控制 ==========",
        f"止损位: {trade_plan.stop_loss:.4f} ({trade_plan.stop_loss_pct * 100:.2f}%)",
        f"止盈位: {trade_plan.take_profit:.4f} ({trade_plan.take_profit_pct * 100:.2f}%)",
        f"追踪止损距离: {trade_plan.trailing_stop:.4f}",
        f"风险收益比RR: {trade_plan.rr:.2f}",
        f"止盈止损逻辑: {trade_plan.reason}",
    ]



def process_signal(
    symbol: str,
    signal_type: int,
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
    df15m: pd.DataFrame,
    df5m: pd.DataFrame,
    signal_details: Dict[str, Any],
) -> None:
    current_time = datetime.now(tz_shanghai)
    cooldown_minutes = DYNAMIC_PARAMS.get(symbol, {}).get(
        "signal_cooldown_minutes",
        CONFIG["trading"]["signal_cooldown_minutes"],
    )

    if symbol in state.last_signals:
        if (current_time - state.last_signals[symbol]) < timedelta(minutes=cooldown_minutes):
            logger.info("%s 信号冷却中，跳过", symbol)
            return

    signal_bar = df5m.iloc[-2]
    signal_price = float(signal_bar["Close"])
    atr_value = float(signal_bar["ATR"])
    adx_value = float(signal_bar["ADX"])

    if CONFIG["trading"]["execution_mode"] == "real_time_ticker":
        execution_price = fetch_latest_price(symbol) or signal_price
    else:
        execution_price = signal_price

    structure = get_recent_structure_levels(df5m, CONFIG["risk"]["structure_lookback_5m"])
    trade_plan = calculate_optimized_positions(
        signal_type=signal_type,
        signal_price=signal_price,
        execution_price=execution_price,
        atr=atr_value,
        adx=adx_value,
        pivot=get_previous_day_pivot(symbol),
        structure=structure,
    )

    if trade_plan.stop_loss_pct > CONFIG["risk"]["max_stop_pct"]:
        logger.info("%s 止损过宽 %.2f%%，跳过", symbol, trade_plan.stop_loss_pct * 100)
        return
    if trade_plan.take_profit_pct > CONFIG["risk"]["max_take_profit_pct"]:
        logger.info("%s 止盈过远 %.2f%%，跳过", symbol, trade_plan.take_profit_pct * 100)
        return
    if trade_plan.rr < CONFIG["risk"]["min_rr"]:
        logger.info("%s RR不足 %.2f，跳过", symbol, trade_plan.rr)
        return

    signal_type_str = {1: "LONG", -1: "SHORT"}[signal_type]
    stop_loss, take_profit = format_trade_prices(symbol, signal_type, trade_plan.stop_loss, trade_plan.take_profit)
    trade_plan.stop_loss, trade_plan.take_profit = stop_loss, take_profit
    trade_plan.rr = compute_rr(trade_plan.execution_price, stop_loss, take_profit)
    trade_plan.stop_loss_pct = abs(trade_plan.execution_price - stop_loss) / max(trade_plan.execution_price, 1e-12)
    trade_plan.take_profit_pct = abs(take_profit - trade_plan.execution_price) / max(trade_plan.execution_price, 1e-12)

    trade_result = place_trade(
        symbol=symbol,
        usdt=CONFIG["risk"]["usdt_per_trade"],
        direction=signal_type_str,
        leverage=CONFIG["risk"]["leverage"],
        take_profit=f"{take_profit:.8f}",
        stop_loss=f"{stop_loss:.8f}",
    )

    chart_path = generate_chart(df1h, df4h, df5m, symbol, signal_type_str, trade_plan=trade_plan)
    details = create_signal_details(symbol, signal_type_str, df1h, df4h, df15m, df5m, signal_details, trade_plan)

    if is_trade_success(trade_result):
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time
        state.trade_history[symbol].append(
            {
                "time": current_time.astimezone(tz_shanghai).isoformat(),
                "signal": signal_type_str,
                "signal_price": signal_price,
                "execution_price": execution_price,
                "sl": stop_loss,
                "tp": take_profit,
                "rr": round(trade_plan.rr, 4),
                "adx": round(adx_value, 4),
            }
        )
        save_state()
        send_notification(symbol, signal_type_str, details, chart_path)
        logger.info("%s 信号已执行并记录", symbol)
    else:
        logger.warning("%s 下单未确认成功，trade_result=%s", symbol, trade_result)


def cleanup_system() -> None:
    logger.info("系统资源清理完成")
    logger.info("累计交易记录: %s 笔", sum(len(v) for v in state.trade_history.values()))
    save_state()



def monitor_market() -> None:
    try:
        load_state()
        load_exchange_info()

        logger.info("=" * 60)
        logger.info("启动短线多周期策略监控系统 v5.0 | 4h+1h+15m趋势 + 5m入场")
        logger.info("=" * 60)

        start_time = datetime.now(tz_shanghai)
        runtime_limit = CONFIG["runtime"]["duration"]
        cycle_count = 0

        while (datetime.now(tz_shanghai) - start_time) < runtime_limit:
            cycle_count += 1
            cycle_start = datetime.now(tz_shanghai)
            logger.info("\n%s", "=" * 40)
            logger.info("第 %s 轮监控开始 | %s", cycle_count, datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"))
            logger.info("%s", "=" * 40)

            for symbol in CONFIG["trading"]["symbols"]:
                try:
                    logger.info("\n--- 处理 %s ---", symbol)
                    df1h = fetch_market_data(symbol, "1h")
                    df4h = fetch_market_data(symbol, "4h")
                    df15m = fetch_market_data(symbol, "15m")
                    df5m = fetch_market_data(symbol, "5m")
                    if df1h.empty or df4h.empty or df15m.empty or df5m.empty:
                        logger.warning("%s 数据不完整，跳过", symbol)
                        continue

                    df1h = calculate_technical_indicators(df1h, symbol)
                    df4h = calculate_technical_indicators(df4h, symbol)
                    df15m = calculate_technical_indicators(df15m, symbol)
                    df5m = calculate_technical_indicators(df5m, symbol)

                    signal, signal_details = check_multi_timeframe_signal(df4h, df1h, df15m, df5m)
                    logger.info("%s 上一根已收盘5m参考价: %.4f", symbol, float(df5m["Close"].iloc[-2]))

                    if signal == 1:
                        logger.info("🟢 %s 多头信号触发 | 4h=多头 1h=多头 15m=多头 5m=触发", symbol)
                        process_signal(symbol, signal, df1h, df4h, df15m, df5m, signal_details)
                    elif signal == -1:
                        logger.info("🔴 %s 空头信号触发 | 4h=空头 1h=空头 15m=空头 5m=触发", symbol)
                        process_signal(symbol, signal, df1h, df4h, df15m, df5m, signal_details)
                    else:
                        logger.info(
                            "✓ %s 无信号 | 4h多=%s 4h空=%s 1h多=%s 1h空=%s 15m多=%s 15m空=%s 5m多触发=%s 5m空触发=%s ADX=%.2f",
                            symbol,
                            signal_details.get("h4_long", False),
                            signal_details.get("h4_short", False),
                            signal_details.get("h1_long", False),
                            signal_details.get("h1_short", False),
                            signal_details.get("m15_long", False),
                            signal_details.get("m15_short", False),
                            signal_details.get("m5_long_trigger", False),
                            signal_details.get("m5_short_trigger", False),
                            signal_details.get("adx", 0),
                        )
                except Exception as exc:
                    logger.error("主流程异常(%s): %s", symbol, exc, exc_info=True)

            elapsed = (datetime.now(tz_shanghai) - cycle_start).total_seconds()
            wait_seconds = max(0, CONFIG["runtime"]["check_interval"] - elapsed)
            logger.info("本轮耗时: %.1f秒 | 距下次检查: %.1f秒", elapsed, wait_seconds)
            time.sleep(wait_seconds)

        logger.info("已达到运行时长上限，正常退出")
        cleanup_system()
    except KeyboardInterrupt:
        logger.info("监控被用户中断")
        cleanup_system()
    except Exception as exc:
        logger.critical("系统崩溃: %s", exc, exc_info=True)
        send_alert(f"系统崩溃: {exc}")
        cleanup_system()


if __name__ == "__main__":
    monitor_market()
