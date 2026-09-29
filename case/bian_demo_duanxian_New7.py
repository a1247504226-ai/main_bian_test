import requests
import smtplib
import time
import os
import logging
import traceback
import pytz
import pandas as pd
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from collections import defaultdict

# ============================================================
# Trend Follow V2.0
# ============================================================
# 顺势策略：
# 4H = 大趋势方向（EMA50 / EMA200）
# 1H = 趋势确认（EMA20 / EMA50 + ADX14）
# 15m = 20根收盘K线突破确认
# 5m = 突破后的回踩 + K线确认，负责实际入场时机
#
# LONG：
# 4H EMA50 > EMA200 且 EMA50向上
# 1H EMA20 > EMA50，Close > EMA20，ADX14 >= 20
# 15m K1收盘价 > 前20根15m收盘K线最高价，且K1实体/振幅 >= 0.5
# 5m最近6根收盘K线出现回踩突破位，最新5m收盘重新站上突破位且为阳线
#
# SHORT为完全镜像。
#
# 风控：
# LONG SL = 5m回踩低点 - ATR5m*0.2
# SHORT SL = 5m回踩高点 + ATR5m*0.2
# TP1 = 1.5R；达到TP1后建议移动止损到成本，并使用5m EMA20跟踪趋势。
# 程序本身只负责信号与邮件，不自动下单。
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(module)s - %(message)s",
    handlers=[
        logging.FileHandler("trend_follow_v1.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
tz_shanghai = pytz.timezone("Asia/Shanghai")

# ============================================================
# 代理
# ============================================================
# 如果不需要代理，可以注释掉下面两行。
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"

plt.rcParams.update({
    "font.family": "SimHei",
    "axes.unicode_minus": False,
    "savefig.dpi": 150,
    "figure.figsize": (18, 10)
})

# ============================================================
# CONFIG
# ============================================================
CONFIG = {
    "email": {
        "enabled": True,
        "from": "a1247504226@163.com",
        "to": "1247504226@qq.com",
        "password": os.getenv("RUMORS_EMAIL_PASSWORD", ""),
        "server": "smtp.163.com",
        "port": 465,
        "max_retries": 3,
        "retry_interval": 10,
        "alert_cooldown": timedelta(minutes=30)
    },
    "binance": {
        "base_url": "https://fapi.binance.com",
        "klines_endpoint": "/fapi/v1/klines",
        "timeout": 30,
        "max_retries": 3,
        "retry_interval": 10
    },
    "trading": {
        "symbols": ["BTCUSDT", "ETHUSDT", "SOXLUSDT", "SNDKUSDT", "BNBUSDT"],
        "check_interval": 300,
        "duration": timedelta(days=7),
        "data_limits": {"4h": 500, "1h": 1000, "15m": 1000, "5m": 1000},
        "adx_min": 20.0,
        "trend_score_min": 65,
        "entry_zone_pct": 0.0020,
        "sl_atr_buffer": 0.25,
        "tp1_rr": 1.50,
        "min_rr": 1.50,
        "signal_cooldown_minutes": 30,
        "one_signal_per_direction_per_day": True,
        "stop_lookback_5m": 12,
        "entry_breakout_lookback_5m": 3,
        "shock_atr_multiple_5m": 1.80,
        "shock_body_ratio_5m": 0.60,
        "shock_cooldown_bars_5m": 6,
        "early_exit_ema_buffer": 0.0010
    },
    "levels": {
        "pivot_left": 3,
        "pivot_right": 3,
        "swing_lookback": 300,
        "max_levels": 5,
        "cluster_atr": 0.30
    },
    "chart": {
        "enabled": True,
        "directory": "trend_follow_charts",
        "candles": 120
    }
}

DYNAMIC_PARAMS = {
    symbol: {"signal_cooldown": CONFIG["trading"]["signal_cooldown_minutes"]}
    for symbol in CONFIG["trading"]["symbols"]
}

class GlobalState:

    def __init__(self):

        self.last_signals = {}

        self.last_emails = {}

        self.last_alerts = {}

        self.trade_history = defaultdict(list)

        self.daily_signals = defaultdict(dict)

        # 防止同一根K线重复触发
        #
        # {
        #   BTCUSDT: {
        #       "LONG": timestamp,
        #       "SHORT": timestamp
        #   }
        # }
        self.last_signal_candle = defaultdict(dict)

        self.system_health = {
            "last_check": datetime.now(
                tz_shanghai
            )
        }


# 全局运行状态：process_signal / monitor_market 共用
state = GlobalState()

def fetch_market_data(
        symbol,
        interval="15m",
        limit=3000
):
    """
    获取 Binance Futures K线。

    Binance 单次K线请求存在 limit 上限，因此当 limit > 1500 时，
    自动分页向历史方向补齐数据。

    目的：Magic Lines 的 Swing High / Swing Low 必须能够在
    昨日 High / Low 左侧找到历史结构，单纯 500/1000 根15m K线
    有时历史范围不够，导致无法找到有效 Swing。
    """
    try:
        endpoint = CONFIG["binance"]["klines_endpoint"]
        url = CONFIG["binance"]["base_url"] + endpoint
        target_limit = max(1, int(limit))
        page_size = min(1500, target_limit)
        all_rows = []
        end_time = None

        # 最多按需要分页；每页向更早历史移动。
        while len(all_rows) < target_limit:
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": page_size
            }
            if end_time is not None:
                params["endTime"] = end_time

            page = None
            for attempt in range(CONFIG["binance"]["max_retries"]):
                try:
                    response = requests.get(
                        url,
                        params=params,
                        timeout=CONFIG["binance"]["timeout"]
                    )
                    response.raise_for_status()
                    page = response.json()
                    break
                except Exception as e:
                    logger.warning(
                        f"{symbol} 数据获取失败 第{attempt + 1}次：{e}"
                    )
                    if attempt < CONFIG["binance"]["max_retries"] - 1:
                        time.sleep(
                            CONFIG["binance"]["retry_interval"] * (attempt + 1)
                        )

            if not page:
                if not all_rows:
                    logger.warning(f"{symbol} Binance返回空数据")
                    return pd.DataFrame()
                break

            all_rows.extend(page)

            # 已经拿到最早一根，再向前翻页。
            oldest_open_time = int(page[0][0])
            next_end_time = oldest_open_time - 1

            if end_time is not None and next_end_time >= end_time:
                break

            end_time = next_end_time

            if len(page) < page_size:
                break

        # 去重，按时间升序；只保留最近 target_limit 根。
        unique = {int(row[0]): row for row in all_rows}
        raw_data = [unique[k] for k in sorted(unique)]
        raw_data = raw_data[-target_limit:]

        if not raw_data:
            return pd.DataFrame()

        df = process_raw_data(raw_data)
        if df.empty:
            return df

        return enhance_data_quality(df, symbol)

    except Exception as e:
        logger.error(
            f"{symbol} 获取行情彻底失败：{e}",
            exc_info=True
        )
        return pd.DataFrame()

def process_raw_data(raw_data):

    try:

        cleaned = []

        for bar in raw_data:

            if len(bar) < 8:

                continue

            try:

                numeric_bar = [

                    float(bar[1]),

                    float(bar[2]),

                    float(bar[3]),

                    float(bar[4]),

                    float(bar[5]),

                    int(bar[0])
                ]

                cleaned.append(
                    numeric_bar
                )

            except (
                ValueError,
                TypeError
            ):

                continue

        if not cleaned:

            return pd.DataFrame()

        df = pd.DataFrame(
            cleaned,
            columns=[
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "Timestamp"
            ]
        )

        df["Timestamp"] = (
            pd.to_datetime(
                df["Timestamp"],
                unit="ms",
                utc=True
            )
            .dt
            .tz_convert(tz_shanghai)
        )

        df.set_index(
            "Timestamp",
            inplace=True
        )

        df.sort_index(
            inplace=True
        )

        return df

    except Exception as e:

        logger.error(
            f"K线数据处理失败：{e}",
            exc_info=True
        )

        return pd.DataFrame()

def enhance_data_quality(
        df,
        symbol
):

    try:

        df = df.copy()

        numeric_columns = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

        for col in numeric_columns:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df.replace(
            [np.inf, -np.inf],
            np.nan,
            inplace=True
        )

        df.dropna(
            subset=[
                "Open",
                "High",
                "Low",
                "Close"
            ],
            inplace=True
        )

        if df.empty:

            return df

        invalid = (

            (df["High"] < df["Low"])

            |

            (df["High"] < df["Open"])

            |

            (df["High"] < df["Close"])

            |

            (df["Low"] > df["Open"])

            |

            (df["Low"] > df["Close"])
        )

        if invalid.any():

            logger.warning(
                f"{symbol}发现异常K线："
                f"{invalid.sum()}根"
            )

            df = df.loc[
                ~invalid
            ]

        return df

    except Exception as e:

        logger.error(
            f"{symbol} 数据质量处理失败：{e}",
            exc_info=True
        )

        return df


# ============================================================
# 技术指标
# ============================================================

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_atr(df, period=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def calculate_adx(df, period=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index
    )

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr

    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx


def add_indicators(df):
    df = df.copy()
    df["EMA20"] = calculate_ema(df["Close"], 20)
    df["EMA50"] = calculate_ema(df["Close"], 50)
    df["EMA200"] = calculate_ema(df["Close"], 200)
    df["ATR14"] = calculate_atr(df, CONFIG["trading"]["atr_period"])
    df["ADX14"] = calculate_adx(df, 14)
    return df


def latest_closed(df):
    """只返回已经收盘的K线；最后一根通常是正在形成的K线。"""
    if df is None or len(df) < 3:
        return pd.DataFrame()
    return df.iloc[:-1].copy()


def safe_float(value):
    try:
        value = float(value)
        return value if np.isfinite(value) else None
    except (TypeError, ValueError):
        return None


# ============================================================
# 顺势信号
# ============================================================

def calculate_trend_indicators(df):
    """计算真正用于趋势判断的指标，不依赖 Magic Lines。"""
    df = df.copy()
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)

    for n in (20, 50, 200):
        df[f"EMA{n}"] = close.ewm(span=n, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["ATR14"] = atr
    df["PLUS_DI"] = plus_di
    df["MINUS_DI"] = minus_di
    df["ADX14"] = dx.ewm(alpha=1/14, adjust=False).mean()

    return df


def trend_score(df, window=12):
    """趋势评分。

    V2.0原则：评分只描述趋势强弱；真正能否交易还要经过
    trend_health / shock_filter / 5m结构确认三道闸门。
    """
    c = latest_closed(df)
    if len(c) < 205:
        return {"direction": "NONE", "score": 0, "long_score": 0,
                "short_score": 0, "row": None, "reasons": []}

    k = c.iloc[-1]
    p = c.iloc[-2]
    e20, e50, e200 = map(float, (k["EMA20"], k["EMA50"], k["EMA200"]))
    score_long = 0
    score_short = 0
    long_reasons, short_reasons = [], []

    if e20 > e50: score_long += 15; long_reasons.append("EMA20>EMA50")
    if e50 > e200: score_long += 15; long_reasons.append("EMA50>EMA200")
    if k["Close"] > e20: score_long += 10; long_reasons.append("价格>EMA20")
    if k["EMA20"] > p["EMA20"]: score_long += 10; long_reasons.append("EMA20上升")
    if k["EMA50"] > p["EMA50"]: score_long += 10; long_reasons.append("EMA50上升")
    if k["PLUS_DI"] > k["MINUS_DI"]: score_long += 15; long_reasons.append("+DI>-DI")
    if k["ADX14"] >= CONFIG["trading"]["adx_min"]: score_long += 15; long_reasons.append("ADX达标")
    recent = c.tail(window)
    if len(recent) > 1:
        if recent["High"].iloc[-1] >= recent["High"].iloc[:-1].max(): score_long += 5; long_reasons.append("近期创新高")
        if recent["Low"].iloc[-1] > recent["Low"].iloc[:-1].min(): score_long += 5; long_reasons.append("低点抬高")

    if e20 < e50: score_short += 15; short_reasons.append("EMA20<EMA50")
    if e50 < e200: score_short += 15; short_reasons.append("EMA50<EMA200")
    if k["Close"] < e20: score_short += 10; short_reasons.append("价格<EMA20")
    if k["EMA20"] < p["EMA20"]: score_short += 10; short_reasons.append("EMA20下降")
    if k["EMA50"] < p["EMA50"]: score_short += 10; short_reasons.append("EMA50下降")
    if k["MINUS_DI"] > k["PLUS_DI"]: score_short += 15; short_reasons.append("-DI>+DI")
    if k["ADX14"] >= CONFIG["trading"]["adx_min"]: score_short += 15; short_reasons.append("ADX达标")
    if len(recent) > 1:
        if recent["Low"].iloc[-1] <= recent["Low"].iloc[:-1].min(): score_short += 5; short_reasons.append("近期创新低")
        if recent["High"].iloc[-1] < recent["High"].iloc[:-1].max(): score_short += 5; short_reasons.append("高点降低")

    if score_long >= CONFIG["trading"]["trend_score_min"] and score_long > score_short:
        direction, score, reasons = "LONG", score_long, long_reasons
    elif score_short >= CONFIG["trading"]["trend_score_min"] and score_short > score_long:
        direction, score, reasons = "SHORT", score_short, short_reasons
    else:
        direction, score, reasons = "NONE", max(score_long, score_short), []

    return {"direction": direction, "score": score,
            "long_score": score_long, "short_score": score_short,
            "row": k, "reasons": reasons}


def trend_health(df, direction):
    """识别趋势是否正在失效。比等EMA20/50死叉更快。"""
    c = latest_closed(df)
    if len(c) < 8 or direction not in ("LONG", "SHORT"):
        return {"healthy": False, "reason": "数据不足或无方向"}

    k = c.iloc[-1]
    prev3 = c.tail(3)
    close = float(k["Close"])
    ema20 = float(k["EMA20"])
    ema50 = float(k["EMA50"])
    plus_di = float(k["PLUS_DI"])
    minus_di = float(k["MINUS_DI"])

    if direction == "LONG":
        below_ema = close < ema20 * (1 - CONFIG["trading"]["early_exit_ema_buffer"])
        di_reversal = minus_di > plus_di
        two_closes_below = (prev3["Close"] < prev3["EMA20"] * (1 - CONFIG["trading"]["early_exit_ema_buffer"])).sum() >= 2
        ema_turn_down = float(k["EMA20"]) < float(c.iloc[-3]["EMA20"])
        if (below_ema and di_reversal) or two_closes_below:
            return {"healthy": False, "reason": "1H/15m趋势失效：价格跌破EMA20且空方DI占优或连续收在EMA20下方"}
        if ema_turn_down and di_reversal:
            return {"healthy": False, "reason": "趋势预警：EMA20拐头向下且-DI>+DI"}
    else:
        above_ema = close > ema20 * (1 + CONFIG["trading"]["early_exit_ema_buffer"])
        di_reversal = plus_di > minus_di
        two_closes_above = (prev3["Close"] > prev3["EMA20"] * (1 + CONFIG["trading"]["early_exit_ema_buffer"])).sum() >= 2
        ema_turn_up = float(k["EMA20"]) > float(c.iloc[-3]["EMA20"])
        if (above_ema and di_reversal) or two_closes_above:
            return {"healthy": False, "reason": "趋势失效：价格突破EMA20且多方DI占优或连续收在EMA20上方"}
        if ema_turn_up and di_reversal:
            return {"healthy": False, "reason": "趋势预警：EMA20拐头向上且+DI>-DI"}

    return {"healthy": True, "reason": "趋势结构尚未失效"}


def detect_shock(df5m):
    """瀑布/急拉保护：宁可错过，不在异常波动中接飞刀。"""
    c = latest_closed(df5m)
    if len(c) < 10:
        return {"shock": False, "direction": None, "bars": 0, "reason": "数据不足"}

    look = c.tail(6).copy()
    atr = float(look["ATR14"].iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        return {"shock": False, "direction": None, "bars": 0, "reason": "ATR无效"}

    shock_up = 0
    shock_down = 0
    reasons = []
    for _, row in look.tail(3).iterrows():
        candle_range = float(row["High"] - row["Low"])
        body = abs(float(row["Close"] - row["Open"]))
        if candle_range <= 0:
            continue
        body_ratio = body / candle_range
        move = float(row["Close"] - row["Open"])
        if candle_range >= atr * CONFIG["trading"]["shock_atr_multiple_5m"] and body_ratio >= CONFIG["trading"]["shock_body_ratio_5m"]:
            if move < 0:
                shock_down += 1
            else:
                shock_up += 1

    last3 = c.tail(3)
    cumulative_move = float(last3["Close"].iloc[-1] - last3["Close"].iloc[0])
    if cumulative_move <= -atr * 1.8:
        shock_down += 1
        reasons.append("近3根累计快速下跌")
    if cumulative_move >= atr * 1.8:
        shock_up += 1
        reasons.append("近3根累计快速上涨")

    if shock_down >= 1:
        return {"shock": True, "direction": "DOWN", "bars": CONFIG["trading"]["shock_cooldown_bars_5m"],
                "reason": "5m出现瀑布/急跌冲击，禁止逆势抄底" + ("；" + "、".join(reasons) if reasons else "")}
    if shock_up >= 1:
        return {"shock": True, "direction": "UP", "bars": CONFIG["trading"]["shock_cooldown_bars_5m"],
                "reason": "5m出现急拉冲击，禁止逆势追空" + ("；" + "、".join(reasons) if reasons else "")}
    return {"shock": False, "direction": None, "bars": 0, "reason": "未检测到异常波动"}


def find_pressure_levels(df15m, current_price, atr_value=None, df1h=None, df4h=None):
    """只使用4H级别计算真正的关键压力/支撑。

    4H作为大级别结构来源：
    - 不再使用15m/1H小波段作为压力支撑。
    - 使用4H Swing High / Swing Low。
    - 使用4H自身ATR作为动态距离过滤，避免把离现价很近的噪声当关键位。
    - Pivot采用4/4确认，要求一个Swing前后都有4根4H K线确认。
    """
    if current_price is None or not np.isfinite(current_price) or current_price <= 0:
        return [], [], None, None

    if df4h is None or df4h.empty:
        return [], [], None, None

    c4 = latest_closed(df4h)
    if len(c4) < 30:
        return [], [], None, None

    # 4H ATR：压力/支撑距离必须由4H自身波动决定，不能继续拿15m ATR。
    high = c4["High"].astype(float)
    low = c4["Low"].astype(float)
    close = c4["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr4 = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])

    pivot_left = 4
    pivot_right = 4

    # 至少距离现价：1.0% + 0.8个4H ATR，取更严格者。
    min_distance = max(current_price * 0.010, atr4 * 0.80)

    raw_highs = []
    raw_lows = []

    for i in range(pivot_left, len(c4) - pivot_right):
        h = float(c4["High"].iloc[i])
        l = float(c4["Low"].iloc[i])

        is_pivot_high = (
            h >= float(c4["High"].iloc[i-pivot_left:i].max())
            and h > float(c4["High"].iloc[i+1:i+pivot_right+1].max())
        )
        is_pivot_low = (
            l <= float(c4["Low"].iloc[i-pivot_left:i].min())
            and l < float(c4["Low"].iloc[i+1:i+pivot_right+1].min())
        )

        if is_pivot_high and h >= current_price + min_distance:
            raw_highs.append(h)
        if is_pivot_low and l <= current_price - min_distance:
            raw_lows.append(l)

    def cluster(levels):
        if not levels:
            return []

        # 4H关键位允许一定价格误差，避免同一片区域出现大量重复水平线。
        tolerance = max(atr4 * 0.50, current_price * 0.005)
        levels = sorted(levels)
        groups = []

        for price in levels:
            if not groups or abs(price - groups[-1]["mean"]) > tolerance:
                groups.append({"prices": [price], "mean": price})
            else:
                groups[-1]["prices"].append(price)
                groups[-1]["mean"] = float(np.mean(groups[-1]["prices"]))

        # 多次4H触及同一区域的价格，优先作为关键位。
        result = []
        for g in groups:
            result.append((g["mean"], len(g["prices"])))
        return result

    resistance_groups = cluster(raw_highs)
    support_groups = cluster(raw_lows)

    # 最近的有效4H关键位优先；距离相同时，多次形成的区域优先。
    resistance_groups.sort(key=lambda x: (abs(x[0] - current_price), -x[1]))
    support_groups.sort(key=lambda x: (abs(x[0] - current_price), -x[1]))

    max_levels = CONFIG["levels"].get("max_levels", 5)
    resistances = sorted(round(float(x[0]), 8) for x in resistance_groups[:max_levels])
    supports = sorted(round(float(x[0]), 8) for x in support_groups[:max_levels])

    next_resistance = min((x for x in resistances if x > current_price), default=None)
    next_support = max((x for x in supports if x < current_price), default=None)

    return supports, resistances, next_resistance, next_support

def check_trend_follow_signal(symbol, df4h, df1h, df15m, df5m):
    """Trend Follow V2.0：顺大势 + 早期趋势失效拦截 + 5m结构确认 + 瀑布保护。"""
    try:
        df4h = calculate_trend_indicators(df4h)
        df1h = calculate_trend_indicators(df1h)
        df15m = calculate_trend_indicators(df15m)
        df5m = calculate_trend_indicators(df5m)

        t4 = trend_score(df4h, 12)
        t1 = trend_score(df1h, 16)
        t15 = trend_score(df15m, 20)
        t5 = trend_score(df5m, 12)
        c4, c1, c15, c5 = map(latest_closed, (df4h, df1h, df15m, df5m))
        if min(map(len, (c4, c1, c15, c5))) < 10:
            return 0, {}, (df4h, df1h, df15m, df5m)

        k4, k1, k15, k5 = c4.iloc[-1], c1.iloc[-1], c15.iloc[-1], c5.iloc[-1]
        # 信号价格严格使用已收盘5m，避免正在形成的K线把信号“骗”出来。
        current = float(k5["Close"])
        atr5 = float(k5["ATR14"])
        atr15 = float(k15["ATR14"])

        supports, resistances, next_resistance, next_support = find_pressure_levels(
            df15m, current, atr15, df1h, df4h
        )

        # 第一层：4H大方向必须明确。
        long_structure = t4["direction"] == "LONG"
        short_structure = t4["direction"] == "SHORT"

        # 第二层：1H / 15m不仅要同向，还不能处于趋势失效状态。
        h1_long = t1["direction"] == "LONG" and trend_health(df1h, "LONG")["healthy"]
        h1_short = t1["direction"] == "SHORT" and trend_health(df1h, "SHORT")["healthy"]
        m15_long = t15["direction"] == "LONG" and trend_health(df15m, "LONG")["healthy"]
        m15_short = t15["direction"] == "SHORT" and trend_health(df15m, "SHORT")["healthy"]

        long_trend = long_structure and h1_long and m15_long
        short_trend = short_structure and h1_short and m15_short

        health4_long = trend_health(df4h, "LONG") if t4["direction"] == "LONG" else {"healthy": False, "reason": "4H非多头"}
        health4_short = trend_health(df4h, "SHORT") if t4["direction"] == "SHORT" else {"healthy": False, "reason": "4H非空头"}
        if long_trend and not health4_long["healthy"]:
            long_trend = False
        if short_trend and not health4_short["healthy"]:
            short_trend = False

        shock = detect_shock(df5m)

        ema20_5 = float(k5["EMA20"])
        ema50_5 = float(k5["EMA50"])
        prev5 = c5.iloc[-2]
        breakout_lookback = CONFIG["trading"]["entry_breakout_lookback_5m"]
        prior = c5.iloc[-(breakout_lookback + 1):-1]
        if len(prior) < 2:
            prior = c5.iloc[-4:-1]

        # 5m执行：回踩EMA20 + 止跌 + 突破局部结构。
        pullback_long = float(k5["Low"]) <= ema20_5 * (1 + CONFIG["trading"]["entry_zone_pct"])
        recovery_long = float(k5["Close"]) > ema20_5 and float(k5["Close"]) > float(k5["Open"])
        breakout_long = float(k5["Close"]) > float(prev5["High"]) and float(k5["Close"]) >= float(prior["High"].max())
        hl_long = float(k5["Low"]) >= float(prior["Low"].min())

        pullback_short = float(k5["High"]) >= ema20_5 * (1 - CONFIG["trading"]["entry_zone_pct"])
        recovery_short = float(k5["Close"]) < ema20_5 and float(k5["Close"]) < float(k5["Open"])
        breakout_short = float(k5["Close"]) < float(prev5["Low"]) and float(k5["Close"]) <= float(prior["Low"].min())
        lh_short = float(k5["High"]) <= float(prior["High"].max())

        # 避免已经脱离EMA20很远才追单。
        not_chasing_long = current <= ema20_5 * (1 + CONFIG["trading"]["entry_zone_pct"] * 1.5)
        not_chasing_short = current >= ema20_5 * (1 - CONFIG["trading"]["entry_zone_pct"] * 1.5)

        long_entry_ready = (
            long_trend and not shock["shock"] and shock["direction"] != "DOWN"
            and t5["direction"] in ("LONG", "NONE")
            and pullback_long and recovery_long and breakout_long and hl_long and not_chasing_long
        )
        short_entry_ready = (
            short_trend and not shock["shock"] and shock["direction"] != "UP"
            and t5["direction"] in ("SHORT", "NONE")
            and pullback_short and recovery_short and breakout_short and lh_short and not_chasing_short
        )

        recent5 = c5.tail(CONFIG["trading"]["stop_lookback_5m"])
        swing_low = float(recent5["Low"].min())
        swing_high = float(recent5["High"].max())
        entry = current

        if long_trend:
            suggested_entry_low = min(ema20_5, float(k15["EMA20"]))
            suggested_entry_high = max(ema20_5, float(k15["EMA20"]))
            stop_loss = swing_low - atr5 * CONFIG["trading"]["sl_atr_buffer"]
            target = next_resistance if next_resistance is not None and next_resistance > entry else None
        elif short_trend:
            suggested_entry_low = min(ema20_5, float(k15["EMA20"]))
            suggested_entry_high = max(ema20_5, float(k15["EMA20"]))
            stop_loss = swing_high + atr5 * CONFIG["trading"]["sl_atr_buffer"]
            target = next_support if next_support is not None and next_support < entry else None
        else:
            suggested_entry_low = suggested_entry_high = current
            stop_loss = target = None

        risk = abs(entry - stop_loss) if stop_loss is not None else 0.0
        rr = abs(target - entry) / risk if target is not None and risk > 0 else 0.0

        # 如果没有合格的4H关键位，理论目标只按最低RR计算；先确定目标，再做RR硬门槛。
        if target is None and risk > 0:
            target = entry + risk * CONFIG["trading"]["tp1_rr"] if long_trend else (entry - risk * CONFIG["trading"]["tp1_rr"] if short_trend else None)

        rr = abs(target-entry)/risk if target is not None and risk > 0 else 0.0
        # 硬性RR门槛：关键位不够1.5R就不做，禁止“为了有信号硬发”。
        rr_ok = rr >= CONFIG['trading']['min_rr']
        if long_entry_ready and not rr_ok:
            long_entry_ready = False
        if short_entry_ready and not rr_ok:
            short_entry_ready = False

        signal = 1 if long_entry_ready else (-1 if short_entry_ready else 0)

        reasons = []
        if shock["shock"]:
            reasons.append(shock["reason"])
        if long_trend and not health4_long["healthy"]:
            reasons.append("4H多头趋势失效")
        if short_trend and not health4_short["healthy"]:
            reasons.append("4H空头趋势失效")
        if not long_trend and not short_trend:
            reasons.append("4H/1H/15m未形成健康的一致趋势")
        if not rr_ok and (long_trend or short_trend):
            reasons.append(f"关键位RR不足{CONFIG['trading']['min_rr']:.1f}R")
        if long_trend and not (pullback_long and recovery_long and breakout_long and hl_long):
            reasons.append("5m尚未完成回踩→止跌→结构突破")
        if short_trend and not (pullback_short and recovery_short and breakout_short and lh_short):
            reasons.append("5m尚未完成反抽→转弱→结构跌破")

        details = {
            "signal": signal, "current_price": current, "entry_price": entry,
            "stop_loss": stop_loss, "take_profit": target, "rr": rr,
            "trend_4h": t4["direction"], "trend_1h": t1["direction"], "trend_15m": t15["direction"], "trend_5m": t5["direction"],
            "score_4h": t4["score"], "score_1h": t1["score"], "score_15m": t15["score"], "score_5m": t5["score"],
            "long_score_4h": t4.get("long_score", 0), "short_score_4h": t4.get("short_score", 0),
            "long_score_1h": t1.get("long_score", 0), "short_score_1h": t1.get("short_score", 0),
            "ema20_4h": safe_float(k4["EMA20"]), "ema50_4h": safe_float(k4["EMA50"]), "ema200_4h": safe_float(k4["EMA200"]), "adx14_4h": safe_float(k4["ADX14"]),
            "ema20_1h": safe_float(k1["EMA20"]), "ema50_1h": safe_float(k1["EMA50"]), "ema200_1h": safe_float(k1["EMA200"]), "adx14_1h": safe_float(k1["ADX14"]),
            "plus_di_1h": safe_float(k1["PLUS_DI"]), "minus_di_1h": safe_float(k1["MINUS_DI"]),
            "ema20_15m": safe_float(k15["EMA20"]), "ema50_15m": safe_float(k15["EMA50"]), "ema200_15m": safe_float(k15["EMA200"]), "adx14_15m": safe_float(k15["ADX14"]),
            "ema20_5m": safe_float(k5["EMA20"]), "ema50_5m": safe_float(k5["EMA50"]), "adx14_5m": safe_float(k5["ADX14"]), "atr14_5m": atr5,
            "supports": supports, "resistances": resistances, "next_support": next_support, "next_resistance": next_resistance,
            "swing_low": swing_low, "swing_high": swing_high,
            "suggested_entry_low": suggested_entry_low, "suggested_entry_high": suggested_entry_high,
            "k15_time": c15.index[-1], "k5_time": c5.index[-1],
            "long_entry_ready": long_entry_ready, "short_entry_ready": short_entry_ready,
            "near_long": pullback_long, "near_short": pullback_short,
            "pullback_long": pullback_long, "recovery_long": recovery_long, "breakout_long": breakout_long, "hl_long": hl_long,
            "pullback_short": pullback_short, "recovery_short": recovery_short, "breakout_short": breakout_short, "lh_short": lh_short,
            "not_chasing_long": not_chasing_long, "not_chasing_short": not_chasing_short,
            "shock": shock["shock"], "shock_direction": shock["direction"], "shock_reason": shock["reason"],
            "health_4h": health4_long.get("reason") if t4["direction"] == "LONG" else health4_short.get("reason"),
            "health_1h": trend_health(df1h, t1["direction"])["reason"] if t1["direction"] in ("LONG", "SHORT") else "无明确趋势",
            "health_15m": trend_health(df15m, t15["direction"])["reason"] if t15["direction"] in ("LONG", "SHORT") else "无明确趋势",
            "rr_ok": rr_ok, "decision_reasons": reasons,
            "trend_reasons_4h": t4["reasons"], "trend_reasons_1h": t1["reasons"], "trend_reasons_15m": t15["reasons"],
            "long_structure": long_trend, "short_structure": short_trend,
        }
        return signal, details, (df4h, df1h, df15m, df5m)
    except Exception as e:
        logger.error(f"{symbol} Trend Follow V2.0计算失败：{e}", exc_info=True)
        return 0, {}, (df4h, df1h, df15m, df5m)


def create_signal_details(symbol, details):
    signal = details.get("signal", 0)
    signal_name = {1: "LONG", -1: "SHORT", 0: "NO TRADE"}.get(signal, "NO TRADE")
    direction = details.get("trend_4h", "NONE")
    def fmt(v, digits=4):
        if v is None: return "无"
        try: return f"{float(v):.{digits}f}"
        except (TypeError, ValueError): return str(v)
    def levels(vals):
        return " / ".join(fmt(v) for v in vals) if vals else "无"
    return [
        "========== Trend Follow V2.0 ==========",
        f"交易对：{symbol}", f"最终建议：{signal_name}", f"当前价格：{fmt(details.get('current_price'))}",
        "========== 趋势计算 ==========",
        f"4H：{details.get('trend_4h')} | 趋势分：{details.get('score_4h',0)}/100 | ADX：{fmt(details.get('adx14_4h'),2)}",
        f"4H EMA20/50/200：{fmt(details.get('ema20_4h'))} / {fmt(details.get('ema50_4h'))} / {fmt(details.get('ema200_4h'))}",
        f"1H：{details.get('trend_1h')} | 趋势分：{details.get('score_1h',0)}/100 | ADX：{fmt(details.get('adx14_1h'),2)}",
        f"1H EMA20/50/200：{fmt(details.get('ema20_1h'))} / {fmt(details.get('ema50_1h'))} / {fmt(details.get('ema200_1h'))}",
        f"1H +DI/-DI：{fmt(details.get('plus_di_1h'),2)} / {fmt(details.get('minus_di_1h'),2)}",
        f"15m：{details.get('trend_15m')} | 趋势分：{details.get('score_15m',0)}/100 | ADX：{fmt(details.get('adx14_15m'),2)}",
        f"5m：{details.get('trend_5m')} | 趋势分：{details.get('score_5m',0)}/100",
        f"趋势一致性：4H={details.get('trend_4h')} / 1H={details.get('trend_1h')} / 15m={details.get('trend_15m')}",
        "========== 压力 / 支撑 ==========",
        f"最近支撑位：{levels(details.get('supports', []))}",
        f"最近压力位：{levels(details.get('resistances', []))}",
        f"最近支撑：{fmt(details.get('next_support'))}", f"最近压力：{fmt(details.get('next_resistance'))}",
        "========== 建议入场 ==========",
        f"建议方向：{direction}",
        f"建议入场区间：{fmt(details.get('suggested_entry_low'))} ~ {fmt(details.get('suggested_entry_high'))}",
        f"当前5m入场条件：LONG={details.get('long_entry_ready')} / SHORT={details.get('short_entry_ready')}",
        "原则：顺势回踩EMA20附近，不追离均线过远的价格。",
        f"瀑布保护：{details.get('shock_reason', '未检测到异常波动')}",
        f"RR门槛：{CONFIG['trading']['min_rr']:.1f}R | 当前RR：{details.get('rr',0):.2f} | RR通过={details.get('rr_ok')}",
        f"5m结构：回踩={details.get('pullback_long') or details.get('pullback_short')} | 止跌/转弱确认={details.get('recovery_long') or details.get('recovery_short')} | 突破={details.get('breakout_long') or details.get('breakout_short')}",
        "========== 止盈 / 止损 ==========",
        f"建议Entry：{fmt(details.get('entry_price'))}", f"SL：{fmt(details.get('stop_loss'))}",
        f"TP1：{fmt(details.get('take_profit'))}（优先最近压力/支撑；否则按 {CONFIG['trading']['tp1_rr']:.1f}R）",
        f"预估RR：{details.get('rr',0):.2f}",
        "TP1后：建议移动SL到成本；剩余仓位可用5m EMA20跟踪。",
        "========== 趋势依据 ==========",
        f"4H依据：{'、'.join(details.get('trend_reasons_4h', [])) or '不足'}",
        f"1H依据：{'、'.join(details.get('trend_reasons_1h', [])) or '不足'}",
        f"15m依据：{'、'.join(details.get('trend_reasons_15m', [])) or '不足'}",
        "本程序只提供分析/推送，不自动下单。"
    ]


def generate_chart(df15m, symbol, signal_type, details):
    if not CONFIG["chart"]["enabled"]:
        return None
    try:
        candles = df15m.tail(CONFIG["chart"]["candles"]).copy()
        if candles.empty:
            return None
        fig, ax = plt.subplots(figsize=(18, 10))
        for i, (_, row) in enumerate(candles.iterrows()):
            candle_color = "#26a69a" if row["Close"] >= row["Open"] else "#ef5350"
            ax.plot([i, i], [row["Low"], row["High"]], color=candle_color, linewidth=1)
            body_low = min(row["Open"], row["Close"])
            body_high = max(row["Open"], row["Close"])
            body_height = max(body_high - body_low, row["Close"] * 0.0002)
            ax.bar(i, body_height, bottom=body_low, width=0.65, color=candle_color, edgecolor=candle_color)

        line_items = []
        for i, value in enumerate(details.get("resistances", [])):
            line_items.append((value, f"Resistance {i+1}", "--", "orange", 1.5))
        for i, value in enumerate(reversed(details.get("supports", []))):
            line_items.append((value, f"Support {i+1}", "--", "green", 1.5))
        line_items += [
            (details.get("take_profit"), "TP1", "-.", "blue", 2.0),
            (details.get("stop_loss"), "SL", "-.", "purple", 2.0),
            (details.get("current_price"), "Current", ":", "black", 1.2),
        ]
        for value, name, style, color, width in line_items:
            if value is not None:
                ax.axhline(float(value), linestyle=style, linewidth=width, color=color, alpha=0.8, label=f"{name}: {float(value):.4f}")

        # EMA20/50/200用于可视化真实趋势。
        if "EMA20" in candles:
            ax.plot(range(len(candles)), candles["EMA20"], linewidth=1.2, label="EMA20")
        if "EMA50" in candles:
            ax.plot(range(len(candles)), candles["EMA50"], linewidth=1.2, label="EMA50")
        if "EMA200" in candles:
            ax.plot(range(len(candles)), candles["EMA200"], linewidth=1.2, label="EMA200")

        signal_index = len(candles) - 1
        if signal_type == "LONG":
            ax.scatter(signal_index, details.get("current_price"), marker="^", s=300, c="green", edgecolors="black", zorder=10)
        elif signal_type == "SHORT":
            ax.scatter(signal_index, details.get("current_price"), marker="v", s=300, c="red", edgecolors="black", zorder=10)

        step = max(1, len(candles) // 10)
        positions = list(range(0, len(candles), step))
        ax.set_xticks(positions)
        ax.set_xticklabels([candles.index[i].strftime("%m-%d %H:%M") for i in positions], rotation=30, fontsize=8)
        ax.set_title(f"Trend Follow V2.0 | {symbol} | 15m | {signal_type}", fontsize=16, fontweight="bold")
        ax.set_ylabel("Price")
        ax.grid(True, linestyle="--", alpha=0.25)
        ax.legend(loc="best", fontsize=8)
        plt.tight_layout()

        chart_dir = CONFIG["chart"]["directory"]
        os.makedirs(chart_dir, exist_ok=True)
        timestamp = datetime.now(tz_shanghai).strftime("%Y%m%d_%H%M%S")
        chart_path = os.path.join(chart_dir, f"{symbol}_{timestamp}_{signal_type}.png")
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return chart_path
    except Exception as e:
        logger.error(f"{symbol} 趋势图表生成失败：{e}", exc_info=True)
        plt.close("all")
        return None


def send_notification(
        symbol,
        signal_type,
        details,
        chart_path=None
):

    if not CONFIG["email"]["enabled"]:

        logger.info(
            "邮件通知已关闭"
        )

        return True

    try:

        msg = MIMEMultipart(
            "related"
        )

        msg["From"] = CONFIG[
            "email"
        ]["from"]

        msg["To"] = CONFIG[
            "email"
        ]["to"]

        signal_cn = {

            "LONG": "LONG",

            "SHORT": "SHORT",

            "ALERT": "SYSTEM ALERT"

        }.get(
            signal_type,
            signal_type
        )

        html_lines = ""

        for line in details:

            html_lines += (
                "<p style='margin:6px 0;'>"
                f"{line}"
                "</p>"
            )

        html_content = f"""
        <html>

        <body style="
            font-family:Arial;
            background:#f5f6fa;
            padding:20px;
        ">

        <div style="
            max-width:900px;
            margin:auto;
            background:white;
            padding:25px;
            border-radius:12px;
        ">

        <h2>
            Trend Follow V2.0
        </h2>

        <h3>
            {symbol} {signal_cn}
        </h3>

        <hr>

        {html_lines}
        """

        if chart_path:

            html_content += """
            <hr>

            <div style="text-align:center;">

                <img
                    src="cid:chart"
                    style="
                        max-width:100%;
                        border-radius:8px;
                    "
                >

            </div>
            """

        html_content += f"""

        <hr>

        <p style="
            color:#888;
            font-size:12px;
        ">

        Trend Follow V2.0

        <br>

        {datetime.now(
            tz_shanghai
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )}

        </p>

        </div>

        </body>

        </html>
        """

        msg.attach(
            MIMEText(
                html_content,
                "html",
                "utf-8"
            )
        )

        # ====================================================
        # 图表
        # ====================================================

        if (
            chart_path
            and
            os.path.exists(chart_path)
        ):

            with open(
                chart_path,
                "rb"
            ) as f:

                img_data = f.read()

            img = MIMEImage(
                img_data,
                Name=os.path.basename(
                    chart_path
                )
            )

            img.add_header(
                "Content-ID",
                "<chart>"
            )

            msg.attach(img)

        # ====================================================
        # SMTP
        # ====================================================

        for attempt in range(
            CONFIG["email"]["max_retries"]
        ):

            try:

                with smtplib.SMTP_SSL(
                    CONFIG["email"]["server"],
                    CONFIG["email"]["port"]
                ) as server:

                    server.login(
                        CONFIG["email"]["from"],
                        CONFIG["email"]["password"]
                    )

                    server.sendmail(
                        CONFIG["email"]["from"],
                        CONFIG["email"]["to"],
                        msg.as_string()
                    )

                logger.info(
                    f"{symbol} {signal_type} "
                    f"邮件发送成功"
                )

                return True

            except Exception as e:

                logger.error(
                    f"{symbol} 邮件发送失败 "
                    f"{attempt + 1}/"
                    f"{CONFIG['email']['max_retries']}："
                    f"{e}"
                )

                if (
                    attempt
                    <
                    CONFIG["email"]["max_retries"] - 1
                ):

                    time.sleep(
                        CONFIG["email"]["retry_interval"]
                    )

        return False

    except Exception as e:

        logger.error(
            f"邮件系统异常：{e}",
            exc_info=True
        )

        return False

def process_signal(
        symbol,
        signal,
        df15m,
        details
):

    try:

        current_time = datetime.now(
            tz_shanghai
        )

        signal_type = {

            1: "LONG",

            -1: "SHORT"

        }.get(signal)

        if signal_type is None:

            return

        # ====================================================
        # 冷却
        # ====================================================

        cooldown_minutes = (
            DYNAMIC_PARAMS
            .get(
                symbol,
                {}
            )
            .get(
                "signal_cooldown",
                CONFIG["trading"]
                ["signal_cooldown_minutes"]
            )
        )

        if symbol in state.last_signals:

            last_time = (
                state.last_signals[
                    symbol
                ]
            )

            if (
                current_time
                -
                last_time
            ) < timedelta(
                minutes=cooldown_minutes
            ):

                logger.info(
                    f"{symbol} "
                    f"{signal_type} "
                    f"信号冷却中，跳过"
                )

                return

        # ====================================================
        # 防重复：同一15m突破K线，同一方向只发送一次
        # ====================================================
        signal_candle_time = details.get("k15_time", df15m.index[-2])
        last_candle = state.last_signal_candle.get(symbol, {}).get(signal_type)
        if last_candle == signal_candle_time:
            logger.info(
                f"{symbol} {signal_type} 已经处理过该15m突破K线，跳过重复邮件"
            )
            return

        # 同一天同方向只允许一次（沿用原脚本行为）
        if CONFIG["trading"].get("one_signal_per_direction_per_day", True):
            today_str = current_time.strftime("%Y-%m-%d")
            if state.daily_signals.get(symbol, {}).get(signal_type) == today_str:
                logger.info(
                    f"{symbol} {signal_type} 今日已经发送过信号，跳过"
                )
                return

        # ====================================================
        # 图表
        # ====================================================

        chart_path = generate_chart(
            df15m,
            symbol,
            signal_type,
            details
        )

        # ====================================================
        # 邮件
        # ====================================================

        email_details = (
            create_signal_details(
                symbol,
                details
            )
        )

        success = send_notification(
            symbol,
            signal_type,
            email_details,
            chart_path
        )

        if not success:

            logger.error(
                f"{symbol} "
                f"{signal_type} "
                f"邮件发送失败"
            )

            return

        # ====================================================
        # 更新状态
        # ====================================================

        state.last_signals[
            symbol
        ] = current_time

        state.last_emails[
            symbol
        ] = current_time

        today_str = current_time.strftime(
            "%Y-%m-%d"
        )

        state.daily_signals[
            symbol
        ][signal_type] = today_str

        # 当前确认K线
        signal_candle_time = details.get("k15_time", df15m.index[-2])

        state.last_signal_candle[
            symbol
        ][signal_type] = (
            signal_candle_time
        )

        # ====================================================
        # 交易历史
        # ====================================================

        state.trade_history[
            symbol
        ].append({

            "time":
                current_time,

            "signal":
                signal_type,

            "price":
                details["current_price"],

            "stop_loss":
                details["stop_loss"],

            "take_profit":
                details["take_profit"],

            "rr":
                details["rr"],

            "signal_candle":
                signal_candle_time
        })

        logger.info(
            f"🔥🔥🔥 {symbol} "
            f"{signal_type} "
            f"Trend Follow "
            f"信号已确认"
        )

    except Exception as e:

        logger.error(
            f"{symbol} 处理信号失败：{e}",
            exc_info=True
        )

def send_alert(
        message
):

    try:

        current_time = datetime.now(
            tz_shanghai
        )

        last_alert = (
            state.last_alerts.get(
                "SYSTEM"
            )
        )

        cooldown = CONFIG[
            "email"
        ]["alert_cooldown"]

        if (
            last_alert
            and
            current_time
            -
            last_alert
            <
            cooldown
        ):

            return

        details = [

            "========== 系统警报 ==========",

            message,

            (
                f"时间："
                f"{current_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        ]

        if send_notification(
            "SYSTEM",
            "ALERT",
            details,
            None
        ):

            state.last_alerts[
                "SYSTEM"
            ] = current_time

    except Exception as e:

        logger.error(
            f"发送系统警报失败：{e}"
        )


def log_strategy_status(symbol, details):
    try:
        if not details: return
        logger.info(
            f"{symbol} | Price={details.get('current_price',0):.4f} | "
            f"4H={details.get('trend_4h')}({details.get('score_4h',0)}/100) | "
            f"1H={details.get('trend_1h')}({details.get('score_1h',0)}/100) | "
            f"15m={details.get('trend_15m')}({details.get('score_15m',0)}/100) | "
            f"5m={details.get('trend_5m')}({details.get('score_5m',0)}/100) | "
            f"ADX1H={details.get('adx14_1h',0):.2f} | "
            f"Support={details.get('next_support')} | Resistance={details.get('next_resistance')} | "
            f"EntryZone={details.get('suggested_entry_low')}~{details.get('suggested_entry_high')} | "
            f"LONG_READY={details.get('long_entry_ready')} | SHORT_READY={details.get('short_entry_ready')}"
        )
    except Exception:
        pass


def log_trend_levels(symbol, details):
    try:
        logger.info(
            f"{symbol} 趋势关键位 | "
            f"支撑={details.get('supports',[])} | 压力={details.get('resistances',[])} | "
            f"最近支撑={details.get('next_support')} | 最近压力={details.get('next_resistance')} | "
            f"SL={details.get('stop_loss')} | TP={details.get('take_profit')}"
        )
    except Exception:
        pass


def monitor_market():
    logger.info("=" * 75)
    logger.info("Trend Follow V2.0 启动")
    logger.info("=" * 75)
    logger.info("V2.0：4H定大方向；1H/15m做趋势健康检查；5m只做回踩后的结构突破")
    logger.info(f"风控：趋势失效提前拦截 + 5m瀑布保护 + RR最低{CONFIG['trading']['min_rr']:.1f}R")
    logger.info("5m入场：回踩EMA20 → 止跌/转强 → 突破局部结构；禁止直接接飞刀")
    logger.info("压力/支撑：只取4H Swing High / Swing Low，作为大级别关键压力/支撑")
    logger.info(f"风控：TP优先取顺势方向最近压力/支撑；无关键位时按 {CONFIG['trading']['tp1_rr']:.1f}R，SL=5m结构点 ± ATR")
    logger.info("=" * 75)

    start_time = datetime.now(tz_shanghai)
    runtime_limit = CONFIG["trading"]["duration"]
    cycle_count = 0

    while datetime.now(tz_shanghai) - start_time < runtime_limit:
        cycle_count += 1
        cycle_start = datetime.now(tz_shanghai)
        logger.info("")
        logger.info("=" * 65)
        logger.info(f"第 {cycle_count} 轮监控 | {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 65)

        for symbol in CONFIG["trading"]["symbols"]:
            try:
                logger.info(f"\n--- {symbol} ---")
                limits = CONFIG["trading"]["data_limits"]

                df4h = fetch_market_data(symbol, "4h", limits["4h"])
                df1h = fetch_market_data(symbol, "1h", limits["1h"])
                df15m = fetch_market_data(symbol, "15m", limits["15m"])
                df5m = fetch_market_data(symbol, "5m", limits["5m"])

                if any(df.empty for df in [df4h, df1h, df15m, df5m]):
                    logger.warning(f"{symbol} 多周期数据不完整，跳过")
                    continue

                signal, details, frames = check_trend_follow_signal(
                    symbol, df4h, df1h, df15m, df5m
                )

                if not details:
                    logger.warning(f"{symbol} 策略没有产生详情")
                    continue

                # 用计算后的15m数据生成图表。
                df15m_calc = frames[2]
                log_trend_levels(symbol, details)
                log_strategy_status(symbol, details)

                if signal == 1:
                    logger.info(
                        f"🟢🟢🟢 {symbol} Trend Follow LONG | "
                        f"Entry={details['entry_price']:.4f} | "
                        f"TP={details['take_profit']:.4f} | "
                        f"SL={details['stop_loss']:.4f} | RR={details['rr']:.2f}"
                    )
                    process_signal(symbol, signal, df15m_calc, details)
                elif signal == -1:
                    logger.info(
                        f"🔴🔴🔴 {symbol} Trend Follow SHORT | "
                        f"Entry={details['entry_price']:.4f} | "
                        f"TP={details['take_profit']:.4f} | "
                        f"SL={details['stop_loss']:.4f} | RR={details['rr']:.2f}"
                    )
                    process_signal(symbol, signal, df15m_calc, details)
                else:
                    logger.info(f"⚪ {symbol} 无信号")

            except Exception as e:
                logger.error(f"{symbol} 主流程异常：{e}", exc_info=True)

        elapsed = (datetime.now(tz_shanghai) - cycle_start).total_seconds()
        wait_seconds = max(0, CONFIG["trading"]["check_interval"] - elapsed)
        logger.info(f"本轮耗时：{elapsed:.1f}秒 | 下次检查：{wait_seconds:.1f}秒")
        time.sleep(wait_seconds)

    logger.info("运行时间达到设定上限，程序退出")


if __name__ == "__main__":
    try:
        monitor_market()
    except KeyboardInterrupt:
        logger.info("用户手动停止程序")
    except Exception as e:
        logger.critical(f"系统发生致命错误：{e}", exc_info=True)
        try:
            send_alert(f"系统发生致命错误：{e}")
        except Exception:
            pass
