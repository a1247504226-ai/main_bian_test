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

matplotlib.use('Agg')

import matplotlib.pyplot as plt

from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from collections import defaultdict


# ============================================================
# Rumors Magic Lines V1.0
# ============================================================
#
# 核心逻辑：
#
# LONG:
# 1. 今天价格跌破昨天 Low
# 2. 跌破后出现15m阳线
# 3. 后一根已收盘15m K线 High > 前一根 High
# 4. -> 做多
# 5. TP = 昨天 High
#
# SHORT:
# 1. 今天价格突破昨天 High
# 2. 突破后出现15m阴线
# 3. 后一根已收盘15m K线 Low < 前一根 Low
# 4. -> 做空
# 5. TP = 昨天 Low
#
# 重要：
# 所有信号只使用已经收盘的15m K线。
#
# ============================================================


# ============================================================
# 日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(module)s - %(message)s',
    handlers=[
        logging.FileHandler(
            "rumors_magic_lines_v1.log",
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


# ============================================================
# 时区
# ============================================================

tz_shanghai = pytz.timezone("Asia/Shanghai")


# ============================================================
# 代理
# ============================================================

# 如果不需要代理，把下面两行删除即可
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"


# ============================================================
# Matplotlib
# ============================================================

plt.rcParams.update({
    "font.family": "SimHei",
    "axes.unicode_minus": False,
    "savefig.dpi": 150,
    "figure.figsize": (16, 10)
})


# ============================================================
# 系统配置
# ============================================================

CONFIG = {

    # --------------------------------------------------------
    # 邮件
    # --------------------------------------------------------

    "email": {

        "enabled": True,

        "from": "你的163邮箱@163.com",

        "to": "你的QQ邮箱@qq.com",

        # !!! 不要写真实授权码到公开代码 !!!
        "password": "请填写新的163邮箱授权码",

        "server": "smtp.163.com",

        "port": 465,

        "max_retries": 3,

        "retry_interval": 10,

        "alert_cooldown": timedelta(minutes=30)
    },


    # --------------------------------------------------------
    # Binance
    # --------------------------------------------------------

    "binance": {

        "base_url": "https://fapi.binance.com",

        "klines_endpoint": "/fapi/v1/klines",

        "timeout": 30,

        "max_retries": 3,

        "retry_interval": 10
    },


    # --------------------------------------------------------
    # 交易
    # --------------------------------------------------------

    "trading": {

        "symbols": [
            "BTCUSDT",
            "ETHUSDT",
            "SOXLUSDT",
            "SNDKUSDT",
            "BNBUSDT"
        ],

        "interval": "15m",

        # 至少需要覆盖昨天 + 今天
        "data_limit": 500,

        # 每个币的信号冷却时间
        "signal_cooldown_minutes": 30,

        # 同一天同一个方向只允许一次
        "one_signal_per_direction_per_day": True,

        # 是否要求入场价格已经位于昨日区间内
        #
        # LONG:
        # Entry >= Yesterday Low
        #
        # SHORT:
        # Entry <= Yesterday High
        #
        "entry_inside_yesterday_range": False
    },


    # --------------------------------------------------------
    # 风控
    # --------------------------------------------------------

    "risk": {

        # 结构止损
        #
        # LONG:
        # SL = 确认K线之前的最低点
        #
        # SHORT:
        # SL = 确认K线之前的最高点
        #

        "stop_loss_buffer_pct": 0.001,

        # 最低风险收益比
        #
        # 例如：
        # Entry 100
        # SL 98
        # TP 105
        #
        # RR = 2.5
        #
        # 如果RR低于这个值，可以选择不发信号
        #
        "minimum_rr": 1.0,

        # 是否启用RR过滤
        "enable_rr_filter": False
    },


    # --------------------------------------------------------
    # 运行
    # --------------------------------------------------------

    "runtime": {

        # 运行7天
        "duration": timedelta(days=7),

        # 每5分钟检查一次
        "check_interval": 300
    },


    # --------------------------------------------------------
    # 图表
    # --------------------------------------------------------

    "chart": {

        "enabled": True,

        "directory": "rumors_magic_lines_charts",

        "candles": 100
    }
}


# ============================================================
# 动态参数
# ============================================================

DYNAMIC_PARAMS = {

    "BTCUSDT": {
        "signal_cooldown": 30
    },

    "ETHUSDT": {
        "signal_cooldown": 30
    },

    "SOXLUSDT": {
        "signal_cooldown": 30
    },

    "SNDKUSDT": {
        "signal_cooldown": 30
    },

    "BNBUSDT": {
        "signal_cooldown": 30
    }
}


# ============================================================
# 全局状态
# ============================================================

class GlobalState:

    def __init__(self):

        self.last_signals = {}

        self.last_emails = {}

        self.last_alerts = {}

        self.trade_history = defaultdict(list)

        # ----------------------------------------------------
        # 当天已经触发过的信号
        #
        # {
        #   BTCUSDT:
        #       {
        #           "LONG": "2026-08-31",
        #           "SHORT": "2026-08-31"
        #       }
        # }
        # ----------------------------------------------------

        self.daily_signals = defaultdict(dict)

        # ----------------------------------------------------
        # 记录当前突破状态
        #
        # LONG_BREAK:
        # 跌破昨天Low
        #
        # SHORT_BREAK:
        # 突破昨天High
        # ----------------------------------------------------

        self.breakout_state = defaultdict(
            lambda: {
                "long_broken": False,
                "short_broken": False,

                "long_break_time": None,
                "short_break_time": None,

                "yesterday_high": None,
                "yesterday_low": None,

                "date": None
            }
        )

        self.system_health = {
            "last_check": datetime.now(tz_shanghai)
        }


state = GlobalState()


# ============================================================
# 获取Binance数据
# ============================================================

def fetch_market_data(
        symbol,
        interval="15m",
        limit=500
):
    """
    获取Binance Futures K线数据
    """

    try:

        endpoint = CONFIG["binance"]["klines_endpoint"]

        url = (
            CONFIG["binance"]["base_url"]
            + endpoint
        )

        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        for attempt in range(
            CONFIG["binance"]["max_retries"]
        ):

            try:

                response = requests.get(
                    url,
                    params=params,
                    timeout=CONFIG["binance"]["timeout"]
                )

                response.raise_for_status()

                data = response.json()

                if not data:

                    logger.warning(
                        f"{symbol} Binance返回空数据"
                    )

                    return pd.DataFrame()

                df = process_raw_data(data)

                if df.empty:

                    return df

                df = enhance_data_quality(
                    df,
                    symbol
                )

                return df

            except Exception as e:

                logger.warning(
                    f"{symbol} 数据获取失败 "
                    f"第{attempt + 1}次：{e}"
                )

                if attempt < CONFIG["binance"]["max_retries"] - 1:

                    time.sleep(
                        CONFIG["binance"]["retry_interval"]
                        * (attempt + 1)
                    )

        return pd.DataFrame()

    except Exception as e:

        logger.error(
            f"{symbol} 获取行情彻底失败：{e}",
            exc_info=True
        )

        return pd.DataFrame()


# ============================================================
# K线数据处理
# ============================================================

def process_raw_data(raw_data):

    try:

        cleaned = []

        for bar in raw_data:

            if len(bar) < 8:
                continue

            try:

                numeric_bar = [
                    float(bar[1]),  # Open
                    float(bar[2]),  # High
                    float(bar[3]),  # Low
                    float(bar[4]),  # Close
                    float(bar[5]),  # Volume
                    int(bar[0])     # Timestamp
                ]

                cleaned.append(numeric_bar)

            except (ValueError, TypeError):

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


# ============================================================
# 数据质量
# ============================================================

def enhance_data_quality(
        df,
        symbol
):

    try:

        df = df.copy()

        for col in [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]:

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

        # ----------------------------------------------------
        # 基础价格合法性
        # ----------------------------------------------------

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
                f"{symbol} 发现异常K线："
                f"{invalid.sum()}根"
            )

            df = df.loc[~invalid]

        return df

    except Exception as e:

        logger.error(
            f"{symbol} 数据质量处理失败：{e}",
            exc_info=True
        )

        return df


# ============================================================
# 获取昨天高低点
# ============================================================

def get_yesterday_levels(
        df15m
):
    """
    根据上海时区获取昨天完整交易日的：

    Yesterday High
    Yesterday Low
    """

    try:

        if df15m.empty:

            return None, None

        today = datetime.now(
            tz_shanghai
        ).date()

        yesterday = today - timedelta(days=1)

        yesterday_data = df15m[
            df15m.index.date == yesterday
        ]

        if yesterday_data.empty:

            logger.warning(
                "没有找到昨天的15m数据"
            )

            return None, None

        yesterday_high = (
            yesterday_data["High"].max()
        )

        yesterday_low = (
            yesterday_data["Low"].min()
        )

        return (
            float(yesterday_high),
            float(yesterday_low)
        )

    except Exception as e:

        logger.error(
            f"获取昨日高低点失败：{e}",
            exc_info=True
        )

        return None, None


# ============================================================
# 获取今天数据
# ============================================================

def get_today_data(df15m):

    today = datetime.now(
        tz_shanghai
    ).date()

    return df15m[
        df15m.index.date == today
    ]


# ============================================================
# 更新突破状态
# ============================================================

def update_breakout_state(
        symbol,
        df15m,
        yesterday_high,
        yesterday_low
):
    """
    记录今天是否已经发生：

    跌破 Yesterday Low

    或

    突破 Yesterday High
    """

    try:

        today = datetime.now(
            tz_shanghai
        ).date()

        state_data = state.breakout_state[symbol]

        # ----------------------------------------------------
        # 新的一天，状态清空
        # ----------------------------------------------------

        if state_data["date"] != today:

            state_data["date"] = today

            state_data["long_broken"] = False

            state_data["short_broken"] = False

            state_data["long_break_time"] = None

            state_data["short_break_time"] = None

            state_data["yesterday_high"] = yesterday_high

            state_data["yesterday_low"] = yesterday_low

            # 清理当天信号
            state.daily_signals[symbol] = {}

            logger.info(
                f"{symbol} 新交易日初始化 | "
                f"Yesterday High={yesterday_high:.4f} | "
                f"Yesterday Low={yesterday_low:.4f}"
            )

        # ----------------------------------------------------
        # 今天的数据
        # ----------------------------------------------------

        today_data = get_today_data(
            df15m
        )

        if today_data.empty:

            return

        today_low = today_data["Low"].min()

        today_high = today_data["High"].max()

        # ----------------------------------------------------
        # 跌破昨日Low
        # ----------------------------------------------------

        if today_low <= yesterday_low:

            if not state_data["long_broken"]:

                state_data["long_broken"] = True

                state_data["long_break_time"] = (
                    datetime.now(tz_shanghai)
                )

                logger.info(
                    f"🟢 {symbol} 已跌破昨日Low "
                    f"{yesterday_low:.4f}"
                )

        # ----------------------------------------------------
        # 突破昨日High
        # ----------------------------------------------------

        if today_high >= yesterday_high:

            if not state_data["short_broken"]:

                state_data["short_broken"] = True

                state_data["short_break_time"] = (
                    datetime.now(tz_shanghai)
                )

                logger.info(
                    f"🔴 {symbol} 已突破昨日High "
                    f"{yesterday_high:.4f}"
                )

    except Exception as e:

        logger.error(
            f"{symbol} 更新突破状态失败：{e}",
            exc_info=True
        )


# ============================================================
# 计算风险收益比
# ============================================================

def calculate_rr(
        signal,
        entry,
        stop_loss,
        take_profit
):

    try:

        if signal == 1:

            risk = entry - stop_loss

            reward = take_profit - entry

        else:

            risk = stop_loss - entry

            reward = entry - take_profit

        if risk <= 0:

            return 0

        if reward <= 0:

            return 0

        return reward / risk

    except Exception:

        return 0


# ============================================================
# Magic Lines核心策略
# ============================================================

def check_rumors_magic_lines_signal(
        symbol,
        df15m
):
    """
    Rumors Magic Lines V1.0

    ------------------------------------------------------------
    LONG
    ------------------------------------------------------------

    ① 今天跌破昨天Low

    ② 前一根已收盘15m K线为阳线

    ③ 最近一根已收盘15m K线 High
       > 前一根 High

    ④ -> LONG

    TP = Yesterday High


    ------------------------------------------------------------
    SHORT
    ------------------------------------------------------------

    ① 今天突破昨天High

    ② 前一根已收盘15m K线为阴线

    ③ 最近一根已收盘15m K线 Low
       < 前一根 Low

    ④ -> SHORT

    TP = Yesterday Low
    """

    try:

        if df15m.empty:

            return 0, {}

        if len(df15m) < 20:

            return 0, {}

        # ====================================================
        # 昨日高低点
        # ====================================================

        yesterday_high, yesterday_low = (
            get_yesterday_levels(df15m)
        )

        if (
            yesterday_high is None
            or
            yesterday_low is None
        ):

            return 0, {}

        # ====================================================
        # 更新突破状态
        # ====================================================

        update_breakout_state(
            symbol,
            df15m,
            yesterday_high,
            yesterday_low
        )

        breakout = state.breakout_state[symbol]

        # ====================================================
        # 只使用已经收盘的K线
        # ====================================================

        last_closed = df15m.iloc[-2]

        prev_closed = df15m.iloc[-3]

        current_price = float(
            df15m["Close"].iloc[-1]
        )

        # ====================================================
        # K线形态
        # ====================================================

        prev_bullish = (
            prev_closed["Close"]
            >
            prev_closed["Open"]
        )

        prev_bearish = (
            prev_closed["Close"]
            <
            prev_closed["Open"]
        )

        # ====================================================
        # LONG确认
        # ====================================================

        long_structure = (

            prev_bullish

            and

            last_closed["High"]
            >
            prev_closed["High"]
        )

        # ====================================================
        # SHORT确认
        # ====================================================

        short_structure = (

            prev_bearish

            and

            last_closed["Low"]
            <
            prev_closed["Low"]
        )

        # ====================================================
        # 今日是否已经触发
        # ====================================================

        today = datetime.now(
            tz_shanghai
        ).date()

        today_str = today.strftime(
            "%Y-%m-%d"
        )

        long_already_signal = (
            state.daily_signals[symbol]
            .get("LONG")
            == today_str
        )

        short_already_signal = (
            state.daily_signals[symbol]
            .get("SHORT")
            == today_str
        )

        # ====================================================
        # LONG
        # ====================================================

        long_signal = (

            breakout["long_broken"]

            and

            long_structure

            and

            not long_already_signal
        )

        # ====================================================
        # SHORT
        # ====================================================

        short_signal = (

            breakout["short_broken"]

            and

            short_structure

            and

            not short_already_signal
        )

        # ====================================================
        # Entry
        # ====================================================

        entry_price = current_price

        # ====================================================
        # 止盈
        # ====================================================

        if long_signal:

            take_profit = yesterday_high

        elif short_signal:

            take_profit = yesterday_low

        else:

            take_profit = None

        # ====================================================
        # 止损
        # ====================================================

        if long_signal:

            # 使用前两根K线的最低点
            structure_low = min(
                prev_closed["Low"],
                last_closed["Low"]
            )

            stop_loss = (
                structure_low
                *
                (
                    1
                    -
                    CONFIG["risk"]
                    ["stop_loss_buffer_pct"]
                )
            )

        elif short_signal:

            structure_high = max(
                prev_closed["High"],
                last_closed["High"]
            )

            stop_loss = (
                structure_high
                *
                (
                    1
                    +
                    CONFIG["risk"]
                    ["stop_loss_buffer_pct"]
                )
            )

        else:

            stop_loss = None

        # ====================================================
        # RR
        # ====================================================

        if (
            take_profit is not None
            and
            stop_loss is not None
        ):

            rr = calculate_rr(
                1 if long_signal else -1,
                entry_price,
                stop_loss,
                take_profit
            )

        else:

            rr = 0

        # ====================================================
        # RR过滤
        # ====================================================

        if (
            long_signal
            and
            CONFIG["risk"]["enable_rr_filter"]
            and
            rr < CONFIG["risk"]["minimum_rr"]
        ):

            logger.info(
                f"{symbol} LONG被RR过滤 | RR={rr:.2f}"
            )

            long_signal = False

        if (
            short_signal
            and
            CONFIG["risk"]["enable_rr_filter"]
            and
            rr < CONFIG["risk"]["minimum_rr"]
        ):

            logger.info(
                f"{symbol} SHORT被RR过滤 | RR={rr:.2f}"
            )

            short_signal = False

        # ====================================================
        # 最终信号
        # ====================================================

        if long_signal:

            signal = 1

        elif short_signal:

            signal = -1

        else:

            signal = 0

        # ====================================================
        # 详细信息
        # ====================================================

        details = {

            "signal": signal,

            "current_price": current_price,

            "entry_price": entry_price,

            "yesterday_high": yesterday_high,

            "yesterday_low": yesterday_low,

            "prev_open": float(
                prev_closed["Open"]
            ),

            "prev_high": float(
                prev_closed["High"]
            ),

            "prev_low": float(
                prev_closed["Low"]
            ),

            "prev_close": float(
                prev_closed["Close"]
            ),

            "last_open": float(
                last_closed["Open"]
            ),

            "last_high": float(
                last_closed["High"]
            ),

            "last_low": float(
                last_closed["Low"]
            ),

            "last_close": float(
                last_closed["Close"]
            ),

            "prev_bullish": prev_bullish,

            "prev_bearish": prev_bearish,

            "long_broken": breakout[
                "long_broken"
            ],

            "short_broken": breakout[
                "short_broken"
            ],

            "long_structure": long_structure,

            "short_structure": short_structure,

            "stop_loss": stop_loss,

            "take_profit": take_profit,

            "rr": rr,

            "long_already_signal":
                long_already_signal,

            "short_already_signal":
                short_already_signal
        }

        return signal, details

    except Exception as e:

        logger.error(
            f"{symbol} Magic Lines策略计算失败：{e}",
            exc_info=True
        )

        return 0, {}


# ============================================================
# 生成图表
# ============================================================

def generate_chart(
        df15m,
        symbol,
        signal_type,
        details
):
    """
    生成15m Magic Lines图表
    """

    if not CONFIG["chart"]["enabled"]:

        return None

    try:

        candles = df15m.tail(
            CONFIG["chart"]["candles"]
        ).copy()

        if candles.empty:

            return None

        fig, ax = plt.subplots(
            figsize=(18, 10)
        )

        # ====================================================
        # K线
        # ====================================================

        for i, (_, row) in enumerate(
            candles.iterrows()
        ):

            if row["Close"] >= row["Open"]:

                candle_color = "#26a69a"

            else:

                candle_color = "#ef5350"

            # 影线
            ax.plot(
                [i, i],
                [
                    row["Low"],
                    row["High"]
                ],
                color=candle_color,
                linewidth=1
            )

            body_low = min(
                row["Open"],
                row["Close"]
            )

            body_high = max(
                row["Open"],
                row["Close"]
            )

            body_height = (
                body_high
                -
                body_low
            )

            if body_height <= 0:

                body_height = (
                    row["Close"]
                    *
                    0.0002
                )

            ax.bar(
                i,
                body_height,
                bottom=body_low,
                width=0.65,
                color=candle_color,
                edgecolor=candle_color
            )

        # ====================================================
        # 昨日 High
        # ====================================================

        yesterday_high = details[
            "yesterday_high"
        ]

        yesterday_low = details[
            "yesterday_low"
        ]

        ax.axhline(
            yesterday_high,
            linestyle="--",
            linewidth=1.5,
            color="red",
            alpha=0.8,
            label=f"Yesterday High: {yesterday_high:.4f}"
        )

        ax.axhline(
            yesterday_low,
            linestyle="--",
            linewidth=1.5,
            color="green",
            alpha=0.8,
            label=f"Yesterday Low: {yesterday_low:.4f}"
        )

        # ====================================================
        # 当前价格
        # ====================================================

        current_price = details[
            "current_price"
        ]

        ax.axhline(
            current_price,
            linestyle=":",
            linewidth=1,
            alpha=0.7,
            label=f"Current: {current_price:.4f}"
        )

        # ====================================================
        # TP
        # ====================================================

        take_profit = details.get(
            "take_profit"
        )

        if take_profit is not None:

            ax.axhline(
                take_profit,
                linestyle="-.",
                linewidth=1.2,
                alpha=0.8,
                label=f"TP: {take_profit:.4f}"
            )

        # ====================================================
        # SL
        # ====================================================

        stop_loss = details.get(
            "stop_loss"
        )

        if stop_loss is not None:

            ax.axhline(
                stop_loss,
                linestyle="-.",
                linewidth=1.2,
                alpha=0.8,
                label=f"SL: {stop_loss:.4f}"
            )

        # ====================================================
        # 信号
        # ====================================================

        if signal_type == "LONG":

            signal_index = len(candles) - 1

            ax.scatter(
                signal_index,
                current_price,
                marker="^",
                s=250,
                c="green",
                edgecolors="black",
                linewidths=1,
                zorder=10
            )

        elif signal_type == "SHORT":

            signal_index = len(candles) - 1

            ax.scatter(
                signal_index,
                current_price,
                marker="v",
                s=250,
                c="red",
                edgecolors="black",
                linewidths=1,
                zorder=10
            )

        # ====================================================
        # X轴
        # ====================================================

        step = max(
            1,
            len(candles) // 10
        )

        tick_positions = list(
            range(
                0,
                len(candles),
                step
            )
        )

        tick_labels = [
            candles.index[i].strftime(
                "%m-%d %H:%M"
            )
            for i in tick_positions
        ]

        ax.set_xticks(
            tick_positions
        )

        ax.set_xticklabels(
            tick_labels,
            rotation=30,
            fontsize=8
        )

        # ====================================================
        # 标题
        # ====================================================

        signal_cn = {

            "LONG": "🟢 做多",

            "SHORT": "🔴 做空",

            "NONE": "⚪ 无信号"

        }.get(
            signal_type,
            "无信号"
        )

        ax.set_title(
            f"Rumors Magic Lines V1.0 | "
            f"{symbol} | 15m | {signal_cn}",
            fontsize=16,
            fontweight="bold"
        )

        ax.set_ylabel(
            "Price"
        )

        ax.grid(
            True,
            linestyle="--",
            alpha=0.25
        )

        ax.legend(
            loc="best",
            fontsize=9
        )

        plt.tight_layout()

        # ====================================================
        # 保存
        # ====================================================

        chart_dir = CONFIG[
            "chart"
        ]["directory"]

        os.makedirs(
            chart_dir,
            exist_ok=True
        )

        timestamp = datetime.now(
            tz_shanghai
        ).strftime(
            "%Y%m%d_%H%M%S"
        )

        chart_path = os.path.join(
            chart_dir,
            f"{symbol}_{timestamp}_{signal_type}.png"
        )

        plt.savefig(
            chart_path,
            dpi=150,
            bbox_inches="tight"
        )

        plt.close(fig)

        logger.info(
            f"{symbol} 图表生成成功："
            f"{chart_path}"
        )

        return chart_path

    except Exception as e:

        logger.error(
            f"{symbol} 图表生成失败：{e}",
            exc_info=True
        )

        plt.close("all")

        return None


# ============================================================
# 邮件
# ============================================================

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

            "LONG": "🟢 做多",

            "SHORT": "🔴 做空",

            "ALERT": "⚠️ 系统警报"

        }.get(
            signal_type,
            signal_type
        )

        # ====================================================
        # 生成详情
        # ====================================================

        html_lines = ""

        for line in details:

            html_lines += (
                f"<p style='margin:6px 0;'>"
                f"{line}"
                f"</p>"
            )

        html_content = f"""
        <html>

        <body style="
            font-family:Arial;
            background:#f5f6fa;
            padding:20px;
        ">

        <div style="
            max-width:800px;
            margin:auto;
            background:white;
            padding:25px;
            border-radius:12px;
        ">

        <h2>
            Rumors Magic Lines V1.0
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

        Rumors Magic Lines V1.0
        <br>

        {datetime.now(tz_shanghai).strftime(
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


# ============================================================
# 创建信号详情
# ============================================================

def create_signal_details(
        symbol,
        details
):

    signal = details[
        "signal"
    ]

    signal_name = {

        1: "🟢 LONG",

        -1: "🔴 SHORT",

        0: "⚪ NONE"

    }.get(
        signal,
        "NONE"
    )

    return [

        "========== Rumors Magic Lines V1.0 ==========",

        f"交易对：{symbol}",

        f"信号：{signal_name}",

        "========== 昨日关键价位 ==========",

        f"昨日最高："
        f"{details['yesterday_high']:.4f}",

        f"昨日最低："
        f"{details['yesterday_low']:.4f}",

        "========== 当前行情 ==========",

        f"当前价格："
        f"{details['current_price']:.4f}",

        "========== 15m结构 ==========",

        f"前一根Open："
        f"{details['prev_open']:.4f}",

        f"前一根High："
        f"{details['prev_high']:.4f}",

        f"前一根Low："
        f"{details['prev_low']:.4f}",

        f"前一根Close："
        f"{details['prev_close']:.4f}",

        f"前一根阳线："
        f"{'✅' if details['prev_bullish'] else '❌'}",

        f"最近一根High："
        f"{details['last_high']:.4f}",

        f"最近一根Low："
        f"{details['last_low']:.4f}",

        "========== Magic Lines ==========",

        f"跌破昨日Low："
        f"{'✅' if details['long_broken'] else '❌'}",

        f"突破昨日High："
        f"{'✅' if details['short_broken'] else '❌'}",

        f"多头结构确认："
        f"{'✅' if details['long_structure'] else '❌'}",

        f"空头结构确认："
        f"{'✅' if details['short_structure'] else '❌'}",

        "========== 风控 ==========",

        f"止损："
        f"{details['stop_loss']:.4f}"
        if details["stop_loss"] is not None
        else "无",

        f"止盈："
        f"{details['take_profit']:.4f}"
        if details["take_profit"] is not None
        else "无",

        f"风险收益比："
        f"{details['rr']:.2f}"

    ]


# ============================================================
# 处理信号
# ============================================================

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
                state.last_signals[symbol]
            )

            if (
                current_time - last_time
            ) < timedelta(
                minutes=cooldown_minutes
            ):

                logger.info(
                    f"{symbol} "
                    f"信号冷却中，跳过"
                )

                return

        # ====================================================
        # 信号类型
        # ====================================================

        signal_map = {

            1: "LONG",

            -1: "SHORT"

        }

        signal_type = signal_map[
            signal
        ]

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
        # 邮件详情
        # ====================================================

        email_details = create_signal_details(
            symbol,
            details
        )

        # ====================================================
        # 发送
        # ====================================================

        success = send_notification(
            symbol,
            signal_type,
            email_details,
            chart_path
        )

        if not success:

            logger.error(
                f"{symbol} "
                f"{signal_type} 邮件发送失败"
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

        # ====================================================
        # 交易记录
        # ====================================================

        state.trade_history[
            symbol
        ].append({

            "time": current_time,

            "signal": signal_type,

            "price": details[
                "current_price"
            ],

            "stop_loss": details[
                "stop_loss"
            ],

            "take_profit": details[
                "take_profit"
            ],

            "rr": details[
                "rr"
            ]

        })

        logger.info(
            f"🔥🔥🔥 {symbol} "
            f"{signal_type} "
            f"信号已确认"
        )

    except Exception as e:

        logger.error(
            f"{symbol} 处理信号失败：{e}",
            exc_info=True
        )


# ============================================================
# 系统警报
# ============================================================

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
            current_time - last_alert
            < cooldown
        ):

            return

        details = [
            "========== 系统警报 ==========",
            message,
            f"时间：{current_time.strftime('%Y-%m-%d %H:%M:%S')}"
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


# ============================================================
# 打印策略状态
# ============================================================

def log_strategy_status(
        symbol,
        details
):

    try:

        logger.info(
            f"{symbol} | "
            f"Price={details.get('current_price', 0):.4f} | "
            f"YHigh={details.get('yesterday_high', 0):.4f} | "
            f"YLow={details.get('yesterday_low', 0):.4f} | "
            f"破Low={details.get('long_broken', False)} | "
            f"破High={details.get('short_broken', False)} | "
            f"多确认={details.get('long_structure', False)} | "
            f"空确认={details.get('short_structure', False)}"
        )

    except Exception:

        pass


# ============================================================
# 主监控
# ============================================================

def monitor_market():

    logger.info("=" * 70)

    logger.info(
        "🚀 Rumors Magic Lines V1.0 启动"
    )

    logger.info("=" * 70)

    logger.info(
        "策略：昨日高低点 + 15m反转确认"
    )

    logger.info(
        "LONG：跌破昨日Low → 阳线 → High突破"
    )

    logger.info(
        "SHORT：突破昨日High → 阴线 → Low突破"
    )

    logger.info(
        "止盈：昨日另一侧极值"
    )

    logger.info("=" * 70)

    start_time = datetime.now(
        tz_shanghai
    )

    runtime_limit = CONFIG[
        "runtime"
    ]["duration"]

    cycle_count = 0

    while (
        datetime.now(tz_shanghai)
        -
        start_time
    ) < runtime_limit:

        cycle_count += 1

        cycle_start = datetime.now(
            tz_shanghai
        )

        logger.info("")
        logger.info("=" * 60)

        logger.info(
            f"第 {cycle_count} 轮监控 | "
            f"{cycle_start.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        logger.info("=" * 60)

        # ====================================================
        # 每个交易对
        # ====================================================

        for symbol in CONFIG[
            "trading"
        ]["symbols"]:

            try:

                logger.info(
                    f"\n--- {symbol} ---"
                )

                # ====================================================
                # 获取15m
                # ====================================================

                df15m = fetch_market_data(
                    symbol,
                    CONFIG[
                        "trading"
                    ]["interval"],
                    CONFIG[
                        "trading"
                    ]["data_limit"]
                )

                if df15m.empty:

                    logger.warning(
                        f"{symbol} "
                        f"15m数据为空"
                    )

                    continue

                # ====================================================
                # 策略
                # ====================================================

                signal, details = (
                    check_rumors_magic_lines_signal(
                        symbol,
                        df15m
                    )
                )

                # ====================================================
                # 日志
                # ====================================================

                log_strategy_status(
                    symbol,
                    details
                )

                # ====================================================
                # 信号
                # ====================================================

                if signal == 1:

                    logger.info(
                        f"🟢🟢🟢 {symbol} "
                        f"Rumors Magic Lines "
                        f"LONG"
                    )

                    process_signal(
                        symbol,
                        signal,
                        df15m,
                        details
                    )

                elif signal == -1:

                    logger.info(
                        f"🔴🔴🔴 {symbol} "
                        f"Rumors Magic Lines "
                        f"SHORT"
                    )

                    process_signal(
                        symbol,
                        signal,
                        df15m,
                        details
                    )

                else:

                    logger.info(
                        f"⚪ {symbol} 无信号"
                    )

            except Exception as e:

                logger.error(
                    f"{symbol} "
                    f"主流程异常：{e}",
                    exc_info=True
                )

        # ====================================================
        # 本轮耗时
        # ====================================================

        elapsed = (
            datetime.now(tz_shanghai)
            -
            cycle_start
        ).total_seconds()

        wait_seconds = max(
            0,
            CONFIG[
                "runtime"
            ]["check_interval"]
            -
            elapsed
        )

        logger.info(
            f"本轮耗时：{elapsed:.1f}秒 | "
            f"下次检查：{wait_seconds:.1f}秒"
        )

        time.sleep(
            wait_seconds
        )

    logger.info(
        "运行时间达到设定上限，退出"
    )


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":

    try:

        monitor_market()

    except KeyboardInterrupt:

        logger.info(
            "用户手动停止程序"
        )

    except Exception as e:

        logger.critical(
            f"系统发生致命错误：{e}",
            exc_info=True
        )

        send_alert(
            f"系统发生致命错误：{e}"
        )