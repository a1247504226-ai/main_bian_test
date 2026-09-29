import requests
import smtplib
import time
import json
import os
import logging
import traceback
import pytz
import shutil
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
from matplotlib.gridspec import GridSpec
from bian_demo_creat_order1 import place_trade


# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(module)s - %(message)s',
    handlers=[
        logging.FileHandler("short_term_monitor.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 时区配置
tz_shanghai = pytz.timezone('Asia/Shanghai')
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
os.environ['HTTPS_PROXY'] = 'https://127.0.0.1:7897'

# Matplotlib配置
plt.rcParams.update({
    'font.family': 'SimHei',
    'axes.unicode_minus': False,
    'savefig.dpi': 150,
    'figure.figsize': (16, 12)
})

# 系统配置参数
CONFIG = {
    'email': {
        'from': 'a1247504226@163.com',
        'to': '1247504226@qq.com',
        'password': 'XKCINXNMOMMDCAFI',
        'server': 'smtp.163.com',
        'port': 465,
        'max_retries': 3,
        'cooldown': timedelta(minutes=1),
        'alert_cooldown': timedelta(minutes=30)
    },
    'trading': {
        'symbols': ['BTCUSDT', 'ETHUSDT', 'SOXLUSDT', 'SNDKUSDT', 'BNBUSDT'],
        'data_limit': 500,
        'volatility_window': 15,
        'signal_cooldown': timedelta(minutes=1),
        'missing_data_threshold': 0.9
    },
    'indicators': {
    'rsi_fast': 14,
    'rsi_slow': 27,

    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,

    'kdj_period': 9,

    'atr_period': 14
},
    'runtime': {
        'duration': timedelta(days=7),
        'check_interval': 300
    }
}

# 动态参数系统
DYNAMIC_PARAMS = {
    'BTCUSDT': {'signal_cooldown': 30},
    'ETHUSDT': {'signal_cooldown': 30},
    'SOXLUSDT': {'signal_cooldown': 30},
    'SNDKUSDT': {'signal_cooldown': 30},
    'BNBUSDT': {'signal_cooldown': 30}
}


class GlobalState:
    def __init__(self):
        self.market_data = defaultdict(pd.DataFrame)
        self.last_signals = {}
        self.last_emails = {}
        self.last_alerts = {}
        self.system_health = {'last_check': datetime.now(tz_shanghai)}
        self.trailing_stops = defaultdict(lambda: defaultdict(float))
        self.trade_history = defaultdict(list)
state = GlobalState()

def fetch_market_data(symbol, interval="1h", limit=CONFIG['trading']['data_limit']):
    """获取币安K线数据，带重试机制"""
    try:
        endpoint = f'/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}'
        url = 'https://fapi.binance.com' + endpoint
        for attempt in range(3):
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                data = response.json()
                if not data:
                    send_alert(f"数据接口返回空数据：{symbol}")
                    return pd.DataFrame()
                df = process_raw_data(data)
                if df.empty:
                    return df
                df = enhance_data_quality(df, symbol)
                return df
            except Exception as e:
                logger.warning(f"数据获取尝试 {attempt + 1} 失败({symbol}): {e}")
                time.sleep(60 * (attempt + 1))
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"数据获取彻底失败({symbol}): {str(e)}", exc_info=True)
        return pd.DataFrame()

def calculate_kdj(df, period=9):
    """
    KDJ(9,3,3)
    """

    low = df['Low'].rolling(period, min_periods=period).min()
    high = df['High'].rolling(period, min_periods=period).max()

    price_range = high - low

    rsv = np.where(
        price_range != 0,
        (df['Close'] - low) / price_range * 100,
        50
    )

    rsv = pd.Series(
        rsv,
        index=df.index
    ).clip(0, 100)

    K = rsv.ewm(
        com=2,
        adjust=False
    ).mean()

    D = K.ewm(
        com=2,
        adjust=False
    ).mean()

    J = 3 * K - 2 * D

    return K, D, J

def process_raw_data(raw_data):
    """清洗原始K线数据"""
    try:
        cleaned = []
        for bar in raw_data:
            if len(bar) >= 6:
                try:
                    numeric_bar = [
                        float(bar[1]), float(bar[2]), float(bar[3]),
                        float(bar[4]), float(bar[7]), int(bar[0])
                    ]
                    cleaned.append(numeric_bar)
                except (ValueError, TypeError):
                    continue
        if not cleaned:
            return pd.DataFrame()
        df = pd.DataFrame(
            cleaned,
            columns=['Open', 'High', 'Low', 'Close', 'Volume', 'Timestamp']
        )
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms', utc=True).dt.tz_convert(tz_shanghai)
        df.set_index('Timestamp', inplace=True)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        logger.error(f"数据处理失败: {str(e)}", exc_info=True)
        return pd.DataFrame()


def enhance_data_quality(df, symbol):
    """数据质量增强"""
    try:
        df['Close'] = df['Close'].where(df['Close'] > 1e-8).ffill()
        df['Volume'] = df['Volume'].where(df['Volume'] > 1e-6).ffill()
        for col in ['Close', 'Volume']:
            df[col] = df[col].interpolate(method='linear').bfill()
        if df['Close'].iloc[-1] < 1e-8:
            logger.warning(f"{symbol} 价格异常，使用前一个值")
            df['Close'].iloc[-1] = df['Close'].iloc[-2]
        return df
    except Exception as e:
        logger.error(f"数据增强失败({symbol}): {str(e)}", exc_info=True)
        return df

def calculate_technical_indicators(df, symbol):
    """
    计算多周期策略所需指标
    """
    params = {
        **CONFIG['indicators'],
        **DYNAMIC_PARAMS.get(symbol, {})
    }
    df = df.copy()
    try:
        # ATR
        df['ATR'] = calculate_atr(
            df,
            params['atr_period']
        )
        df['ADX'] = calculate_adx_pure_fixed(df)
        # RSI
        df['RSI14'] = calculate_rsi(
            df['Close'],
            params['rsi_fast']
        )

        df['RSI27'] = calculate_rsi(
            df['Close'],
            params['rsi_slow']
        )
        # KDJ
        df['K'], df['D'], df['J'] = calculate_kdj(
            df,
            params['kdj_period']
        )
        # MACD
        dif, dea, hist = calculate_macd(
            df['Close'],
            params['macd_fast'],
            params['macd_slow'],
            params['macd_signal']
        )
        df['DIF'] = dif
        df['DEA'] = dea
        df['MACD_HIST'] = hist
        return df
    except Exception as e:
        logger.error(
            f"指标计算失败({symbol}): {str(e)}",
            exc_info=True
        )
        return df

def calculate_rsi(series, period=14):
    """RSI计算"""
    try:
        delta = series.diff(1)
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi.clip(0, 100).fillna(50)
    except Exception as e:
        logger.error(f"RSI计算失败: {str(e)}", exc_info=True)
        return pd.Series([50] * len(series), index=series.index)



def calculate_atr_pct(df, period=14):
    """ATR百分比"""
    df['ATR_PCT'] = df['ATR'] / df['Close'] * 100
    return df


def calculate_macd(series, fast=12, slow=26, signal=9):
    """MACD计算"""
    try:
        series = series.dropna().astype(float)
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        dif = (ema_fast - ema_slow).round(5)
        dea = dif.ewm(span=signal, adjust=False).mean().round(5)
        macd = dif - dea
        return dif.fillna(0), dea.fillna(0), macd.fillna(0)
    except Exception as e:
        logger.error(f"MACD计算失败: {str(e)}", exc_info=True)
        return (
    pd.Series([0] * len(series), index=series.index),
    pd.Series([0] * len(series), index=series.index),
    pd.Series([0] * len(series), index=series.index),
)


def calculate_atr(df, period=14):
    """ATR计算"""
    try:
        df = df.copy()
        df['High_Low'] = df['High'] - df['Low']
        df['High_Close'] = abs(df['High'] - df['Close'].shift(1))
        df['Low_Close'] = abs(df['Low'] - df['Close'].shift(1))
        df['TR'] = df[['High_Low', 'High_Close', 'Low_Close']].max(axis=1)
        atr = df['TR'].rolling(period).mean()
        return atr.bfill().round(5)
    except Exception as e:
        logger.error(f"ATR计算失败: {str(e)}", exc_info=True)
        return pd.Series([0] * len(df), index=df.index)

def calculate_adx_pure_fixed(df, period=14):
    """ADX计算（纯numpy实现）"""
    try:
        high = df['High'].values
        low = df['Low'].values
        close = df['Close'].values

        plus_dm = np.zeros_like(high)
        minus_dm = np.zeros_like(high)
        for i in range(1, len(high)):
            high_diff = high[i] - high[i - 1]
            low_diff = low[i - 1] - low[i]
            if high_diff > low_diff and high_diff > 0:
                plus_dm[i] = high_diff
            if low_diff > high_diff and low_diff > 0:
                minus_dm[i] = low_diff

        tr = np.zeros_like(high)
        for i in range(1, len(high)):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

        smooth_plus_dm = np.zeros(len(plus_dm))
        smooth_minus_dm = np.zeros(len(minus_dm))
        smooth_tr = np.zeros(len(tr))

        smooth_plus_dm = plus_dm.copy()
        smooth_minus_dm = minus_dm.copy()
        smooth_tr = tr.copy()

        for i in range(1, len(high)):
            smooth_plus_dm[i] = ((period - 1) * smooth_plus_dm[i - 1] + plus_dm[i]) / period
            smooth_minus_dm[i] = ((period - 1) * smooth_minus_dm[i - 1] + minus_dm[i]) / period
            smooth_tr[i] = ((period - 1) * smooth_tr[i - 1] + tr[i]) / period

        plus_di = np.zeros_like(smooth_plus_dm)
        minus_di = np.zeros_like(smooth_minus_dm)
        non_zero_tr = smooth_tr != 0
        plus_di[non_zero_tr] = smooth_plus_dm[non_zero_tr] / smooth_tr[non_zero_tr] * 100
        minus_di[non_zero_tr] = smooth_minus_dm[non_zero_tr] / smooth_tr[non_zero_tr] * 100

        dx = np.zeros_like(plus_di)
        dx_denom = plus_di + minus_di
        dx[dx_denom != 0] = abs(plus_di[dx_denom != 0] - minus_di[dx_denom != 0]) / dx_denom[dx_denom != 0] * 100

        adx_line = np.zeros(len(dx))
        if len(dx) >= period:
            adx_line[:period] = np.mean(dx[:period])
        for i in range(period, len(dx)):
            adx_line[i] = ((period - 1) * adx_line[i - 1] + dx[i]) / period

        adx_series = pd.Series(adx_line, index=df.index).bfill().fillna(20)
        return adx_series
    except Exception as e:
        logger.error(f"ADX计算失败: {str(e)}")
        return pd.Series([50] * len(df), index=df.index)

def get_previous_day_pivot(symbol):
    """
    直接获取 Binance Futures 前一根已完成 1D K线，
    计算经典 Pivot Points。

    PP = (H + L + C) / 3
    R1 = 2 * PP - L
    R2 = PP + (H - L)
    S1 = 2 * PP - H
    S2 = PP - (H - L)
    """

    try:

        url = 'https://fapi.binance.com/fapi/v1/klines'

        params = {
            'symbol': symbol,
            'interval': '1d',
            'limit': 2
        }

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        if not data or len(data) < 2:
            logger.warning(
                f"{symbol} 日线Pivot数据不足"
            )
            return None

        # [-1] = 当前正在形成的日线
        # [-2] = 上一根已经完成的日线
        candle = data[-2]

        high_price = float(candle[2])
        low_price = float(candle[3])
        close_price = float(candle[4])

        # =========================
        # Classic Pivot
        # =========================

        pp = (
            high_price
            + low_price
            + close_price
        ) / 3

        r1 = 2 * pp - low_price

        r2 = pp + (
            high_price - low_price
        )

        s1 = 2 * pp - high_price

        s2 = pp - (
            high_price - low_price
        )

        pivot = {
            'H': high_price,
            'L': low_price,
            'C': close_price,

            'PP': pp,

            'R1': r1,
            'R2': r2,

            'S1': s1,
            'S2': s2
        }

        logger.info(
            f"{symbol} Pivot | "
            f"H={high_price:.4f} "
            f"L={low_price:.4f} "
            f"C={close_price:.4f} | "
            f"PP={pp:.4f} "
            f"R1={r1:.4f} "
            f"R2={r2:.4f} "
            f"S1={s1:.4f} "
            f"S2={s2:.4f}"
        )

        return pivot

    except Exception as e:
        logger.error(
            f"{symbol} 获取Pivot失败: {e}",
            exc_info=True
        )
        return None

def calculate_optimized_positions(
    signal_type,
    current_price,
    atr,
    pivot=None
):
    """
    Pivot + ATR 止盈止损

    LONG:
        TP -> 当前价格上方最近的 Pivot
        SL -> 当前价格下方最近的 Pivot

    SHORT:
        TP -> 当前价格下方最近的 Pivot
        SL -> 当前价格上方最近的 Pivot

    Pivot不合适时使用ATR兜底。
    """

    # =========================
    # ATR兜底
    # =========================
    if signal_type == 1:

        atr_stop_loss = current_price - atr * 1.0
        atr_take_profit = current_price + atr * 1.5

    else:

        atr_stop_loss = current_price + atr * 1.0
        atr_take_profit = current_price - atr * 1.5

    # =========================
    # 没有Pivot
    # =========================
    if not pivot:

        return (
            atr_stop_loss,
            atr_take_profit,
            atr * 1.5
        )

    # =====================================================
    # 做多
    # =====================================================
    if signal_type == 1:

        # 当前价格上方的压力位
        resistance_levels = [
            pivot['PP'],
            pivot['R1'],
            pivot['R2']
        ]

        resistance_levels = sorted(
            [
                x for x in resistance_levels
                if x > current_price
            ]
        )

        # 当前价格下方的支撑位
        support_levels = [
            pivot['S1'],
            pivot['S2']
        ]

        support_levels = sorted(
            [
                x for x in support_levels
                if x < current_price
            ],
            reverse=True
        )

        # 最近压力位作为TP
        if resistance_levels:

            take_profit = resistance_levels[0]

        else:

            take_profit = atr_take_profit

        # 最近支撑位作为SL
        if support_levels:

            stop_loss = support_levels[0]

        else:

            stop_loss = atr_stop_loss

    # =====================================================
    # 做空
    # =====================================================
    else:

        # 当前价格下方的支撑位
        support_levels = [
            pivot['PP'],
            pivot['S1'],
            pivot['S2']
        ]

        support_levels = sorted(
            [
                x for x in support_levels
                if x < current_price
            ],
            reverse=True
        )

        # 当前价格上方的压力位
        resistance_levels = [
            pivot['R1'],
            pivot['R2']
        ]

        resistance_levels = sorted(
            [
                x for x in resistance_levels
                if x > current_price
            ]
        )

        # 最近支撑位作为TP
        if support_levels:

            take_profit = support_levels[0]

        else:

            take_profit = atr_take_profit

        # 最近压力位作为SL
        if resistance_levels:

            stop_loss = resistance_levels[0]

        else:

            stop_loss = atr_stop_loss

    # =========================
    # 最终方向保护
    # =========================
    if signal_type == 1:

        if take_profit <= current_price:
            take_profit = atr_take_profit

        if stop_loss >= current_price:
            stop_loss = atr_stop_loss

    else:

        if take_profit >= current_price:
            take_profit = atr_take_profit

        if stop_loss <= current_price:
            stop_loss = atr_stop_loss

    # =========================
    # ATR移动止损距离
    # =========================
    trailing_stop = atr * 1.5

    return (
        stop_loss,
        take_profit,
        trailing_stop
    )

def check_multi_timeframe_signal(df4h, df1h, df15m, df5m):
    """
    多周期开仓策略

    做多：
    4h + 1h + 15m：
        DIF > DEA
        MACD_HIST > 0
        40 <= RSI14 <= 90 且 RSI14 > RSI27 > 30
        J > K > D

    5m：
        (KDJ金叉 AND DIF > DEA)
        OR
        (MACD金叉 AND J > K > D)

    做空反向。
    """
    try:
        # 使用已经收盘的K线
        h4 = df4h.iloc[-2]
        h1 = df1h.iloc[-2]
        m15 = df15m.iloc[-2]
        m5 = df5m.iloc[-2]

        # 5m 前一根，用来判断金叉/死叉
        m5_prev = df5m.iloc[-3]

        # 4h 多头趋势过滤
        h4_long = (
            h4['DIF'] > h4['DEA']
            and h4['MACD_HIST'] > 0
            and h4['RSI14'] > h4['RSI27'] > 30
            and h4['J'] > h4['K'] > h4['D']
        )

        # 4h 空头趋势过滤
        h4_short = (
            h4['DIF'] < h4['DEA']
            and h4['MACD_HIST'] < 0
            and h4['RSI14'] < h4['RSI27'] < 80
            and h4['J'] < h4['K'] < h4['D']
        )

        # 1h 多头趋势过滤
        h1_long = (
            h1['DIF'] > h1['DEA']
            and h1['MACD_HIST'] > 0
            and h1['RSI14'] > h1['RSI27'] > 30
            and h1['J'] > h1['K'] > h1['D']
        )

        # 1h 空头趋势过滤
        h1_short = (
            h1['DIF'] < h1['DEA']
            and h1['MACD_HIST'] < 0
            and h1['RSI14'] < h1['RSI27'] < 80
            and h1['J'] < h1['K'] < h1['D']
        )

        # 15m 多头趋势过滤：与1h完全一致
        m15_long = (
            m15['DIF'] > m15['DEA']
            and m15['MACD_HIST'] > 0
            and m15['RSI14'] > m15['RSI27'] > 30
            and m15['J'] > m15['K'] > m15['D']
        )

        # 15m 空头趋势过滤：与1h完全一致
        m15_short = (
            m15['DIF'] < m15['DEA']
            and m15['MACD_HIST'] < 0
            and m15['RSI14'] < m15['RSI27'] < 80
            and m15['J'] < m15['K'] < m15['D']
        )

        # 5m KDJ 金叉
        kdj_golden_cross = (
            m5_prev['K'] <= m5_prev['D']
            and m5['K'] > m5['D']
        )

        # 5m KDJ 死叉
        kdj_death_cross = (
            m5_prev['K'] >= m5_prev['D']
            and m5['K'] < m5['D']
        )

        # 5m MACD 金叉
        macd_golden_cross = (
            m5_prev['DIF'] <= m5_prev['DEA']
            and m5['DIF'] > m5['DEA']
        )

        # 5m MACD 死叉
        macd_death_cross = (
            m5_prev['DIF'] >= m5_prev['DEA']
            and m5['DIF'] < m5['DEA']
        )

        # 5m 多头触发
        # 条件1：KDJ金叉 AND MACD DIF > DEA
        # OR
        # 条件2：MACD金叉 AND J > K > D
        m5_long_trigger = (
            (
                kdj_golden_cross
                and m5['DIF'] > m5['DEA']
            )
            or
            (
                macd_golden_cross
                and m5['J'] > m5['K'] > m5['D']
            )
        )

        # 5m 空头触发
        m5_short_trigger = (
            (
                kdj_death_cross
                and m5['DIF'] < m5['DEA']
            )
            or
            (
                macd_death_cross
                and m5['J'] < m5['K'] < m5['D']
            )
        )

        # 5m RSI 作为唯一 RSI 开仓过滤：
        # 做多：40 <= RSI14 <= 90；做空：RSI14 < 50
        rsi5m = m5['RSI14']
        rsi_long_filter = 40 <= rsi5m <= 90
        rsi_short_filter = rsi5m < 50

        # 5m ADX 必须 > 30 才允许开仓
        adx = df5m['ADX'].iloc[-2]
        trend_strength = adx > 30

        # 最终信号：4h + 1h + 15m趋势一致，5m负责入场
        if h4_long and h1_long and m15_long and m5_long_trigger and rsi_long_filter and trend_strength:
            signal = 1
        elif h4_short and h1_short and m15_short and m5_short_trigger and rsi_short_filter and trend_strength:
            signal = -1
        else:
            signal = 0

        # 返回详细状态
        details = {
            'signal': signal,

            'h4_long': h4_long,
            'h4_short': h4_short,

            'h1_long': h1_long,
            'h1_short': h1_short,

            'm15_long': m15_long,
            'm15_short': m15_short,

            'kdj_golden_cross': kdj_golden_cross,
            'kdj_death_cross': kdj_death_cross,

            'macd_golden_cross': macd_golden_cross,
            'macd_death_cross': macd_death_cross,

            'm5_long_trigger': m5_long_trigger,
            'm5_short_trigger': m5_short_trigger,

            'adx': adx,
            'rsi5m': rsi5m,
            'rsi_long_filter': rsi_long_filter,
            'rsi_short_filter': rsi_short_filter,
            'trend_strength': trend_strength,
        }

        return signal, details

    except Exception as e:
        logger.error(
            f"多周期信号计算失败: {str(e)}",
            exc_info=True
        )
        return 0, {}

def generate_chart(df1h, df4h, df15m, df5m, symbol, signal_type_str):
    """
    生成多周期技术分析图表

    图表结构：
    1. 1h K线 + 信号
    2. 4h MACD
    3. 1h RSI
    4. 1h 成交量 + KDJ

    只使用当前策略已经计算的指标，
    不依赖 BB / EMA / VOL_MA20 / Signal 等不存在的字段。
    """
    try:
        # =========================
        # 基础数据检查
        # =========================
        required_1h = [
            'Open', 'High', 'Low', 'Close',
            'Volume', 'RSI14', 'RSI27',
            'K', 'D', 'J'
        ]

        required_4h = [
            'MACD_HIST', 'DIF', 'DEA'
        ]

        missing_1h = [
            col for col in required_1h
            if col not in df1h.columns
        ]

        missing_4h = [
            col for col in required_4h
            if col not in df4h.columns
        ]

        if missing_1h:
            logger.error(
                f"{symbol} 图表生成失败：1h缺少字段 {missing_1h}"
            )
            return None

        if missing_4h:
            logger.error(
                f"{symbol} 图表生成失败：4h缺少字段 {missing_4h}"
            )
            return None

        # =========================
        # 取最近数据
        # =========================
        candles = df1h.tail(80).copy()
        macd_data = df4h.tail(80).copy()
        rsi_data = df1h.tail(80).copy()
        kdj_data = df1h.tail(80).copy()

        if candles.empty:
            logger.warning(f"{symbol} 图表数据为空")
            return None

        # =========================
        # 创建画布
        # =========================
        fig = plt.figure(
            figsize=(16, 13),
            constrained_layout=False
        )

        gs = GridSpec(
            4,
            1,
            figure=fig,
            height_ratios=[3.2, 1.7, 1.7, 2.0],
            hspace=0.08
        )

        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1])
        ax3 = fig.add_subplot(gs[2])
        ax4 = fig.add_subplot(gs[3])

        # ==========================================================
        # 1. 1h K线
        # ==========================================================
        x = np.arange(len(candles))

        for i, (_, row) in enumerate(candles.iterrows()):

            # 上下影线
            candle_color = (
                '#26a69a'
                if row['Close'] >= row['Open']
                else '#ef5350'
            )

            ax1.plot(
                [i, i],
                [row['Low'], row['High']],
                color=candle_color,
                linewidth=1
            )

            # K线实体
            body_low = min(row['Open'], row['Close'])
            body_high = max(row['Open'], row['Close'])

            body_height = body_high - body_low

            # 避免十字星实体高度为0导致看不清
            if body_height <= 0:
                body_height = max(
                    abs(row['Close']) * 0.0002,
                    1e-8
                )

            ax1.bar(
                i,
                body_height,
                bottom=body_low,
                width=0.65,
                color=candle_color,
                edgecolor=candle_color
            )

        # 当前价格
        current_price = candles['Close'].iloc[-1]

        ax1.axhline(
            current_price,
            linestyle='--',
            linewidth=0.8,
            alpha=0.7
        )

        ax1.text(
            len(candles) - 1,
            current_price,
            f'  {current_price:.4f}',
            fontsize=9,
            verticalalignment='bottom'
        )

        # =========================
        # 信号标记
        # =========================
        if signal_type_str == 'LONG':
            ax1.scatter(
                len(candles) - 1,
                current_price,
                marker='^',
                s=180,
                c='green',
                edgecolors='black',
                linewidths=0.8,
                zorder=10,
                label='LONG'
            )

        elif signal_type_str == 'SHORT':
            ax1.scatter(
                len(candles) - 1,
                current_price,
                marker='v',
                s=180,
                c='red',
                edgecolors='black',
                linewidths=0.8,
                zorder=10,
                label='SHORT'
            )

        # 标题
        signal_cn = {
            'LONG': '做多',
            'SHORT': '做空'
        }.get(signal_type_str, '无信号')

        ax1.set_title(
            f'{symbol} | 1h K线 | 当前信号：{signal_cn}',
            fontsize=14,
            fontweight='bold',
            pad=10
        )

        ax1.set_ylabel('价格')
        ax1.grid(
            True,
            linestyle='--',
            alpha=0.25
        )

        if signal_type_str in ('LONG', 'SHORT'):
            ax1.legend(
                loc='upper left',
                fontsize=9
            )

        # ==========================================================
        # 2. 4h MACD
        # ==========================================================
        x_macd = np.arange(len(macd_data))

        hist_values = macd_data['MACD_HIST'].fillna(0)

        hist_colors = [
            '#26a69a' if value >= 0 else '#ef5350'
            for value in hist_values
        ]

        ax2.bar(
            x_macd,
            hist_values,
            color=hist_colors,
            alpha=0.65,
            width=0.7,
            label='MACD Hist'
        )

        ax2.plot(
            x_macd,
            macd_data['DIF'],
            linewidth=1.2,
            label='DIF'
        )

        ax2.plot(
            x_macd,
            macd_data['DEA'],
            linewidth=1.2,
            label='DEA'
        )

        ax2.axhline(
            0,
            linestyle='--',
            linewidth=0.8,
            alpha=0.6
        )

        ax2.set_ylabel('4h MACD')
        ax2.grid(
            True,
            linestyle='--',
            alpha=0.25
        )

        ax2.legend(
            loc='upper left',
            fontsize=8,
            ncol=3
        )

        # ==========================================================
        # 3. 1h RSI
        # ==========================================================
        x_rsi = np.arange(len(rsi_data))

        ax3.plot(
            x_rsi,
            rsi_data['RSI14'],
            linewidth=1.5,
            label='RSI14'
        )

        ax3.plot(
            x_rsi,
            rsi_data['RSI27'],
            linewidth=1.2,
            label='RSI27'
        )

        # 超买超卖线
        ax3.axhline(
            70,
            linestyle='--',
            linewidth=0.8,
            alpha=0.7
        )

        ax3.axhline(
            30,
            linestyle='--',
            linewidth=0.8,
            alpha=0.7
        )

        # 中轴
        ax3.axhline(
            50,
            linestyle=':',
            linewidth=0.7,
            alpha=0.5
        )

        ax3.set_ylim(0, 100)
        ax3.set_ylabel('1h RSI')

        ax3.grid(
            True,
            linestyle='--',
            alpha=0.25
        )

        ax3.legend(
            loc='upper left',
            fontsize=8
        )

        # ==========================================================
        # 4. 1h 成交量 + KDJ
        # ==========================================================
        x_kdj = np.arange(len(kdj_data))

        volume_colors = [
            '#26a69a'
            if row['Close'] >= row['Open']
            else '#ef5350'
            for _, row in kdj_data.iterrows()
        ]

        ax4.bar(
            x_kdj,
            kdj_data['Volume'],
            color=volume_colors,
            alpha=0.30,
            width=0.7,
            label='Volume'
        )

        ax4.set_ylabel('成交量')

        # =========================
        # KDJ 右轴
        # =========================
        ax4_twin = ax4.twinx()

        ax4_twin.plot(
            x_kdj,
            kdj_data['K'],
            linewidth=1.3,
            label='K'
        )

        ax4_twin.plot(
            x_kdj,
            kdj_data['D'],
            linewidth=1.3,
            label='D'
        )

        ax4_twin.plot(
            x_kdj,
            kdj_data['J'],
            linewidth=1.0,
            linestyle='--',
            label='J'
        )

        ax4_twin.axhline(
            80,
            linestyle='--',
            linewidth=0.7,
            alpha=0.6
        )

        ax4_twin.axhline(
            20,
            linestyle='--',
            linewidth=0.7,
            alpha=0.6
        )

        ax4_twin.set_ylim(-10, 110)
        ax4_twin.set_ylabel('KDJ')

        ax4.grid(
            True,
            linestyle='--',
            alpha=0.25
        )

        # 两边图例
        ax4.legend(
            loc='upper left',
            fontsize=8
        )

        ax4_twin.legend(
            loc='upper right',
            fontsize=8
        )

        # ==========================================================
        # X轴
        # ==========================================================
        # 最后一张图显示时间
        step = max(1, len(candles) // 8)

        tick_positions = list(
            range(0, len(candles), step)
        )

        tick_labels = [
            candles.index[i].strftime('%m-%d\n%H:%M')
            for i in tick_positions
        ]

        ax4.set_xticks(tick_positions)
        ax4.set_xticklabels(
            tick_labels,
            fontsize=8
        )

        ax4.set_xlabel(
            '1h K线时间'
        )

        # 上面三个图隐藏X轴标签
        ax1.tick_params(
            axis='x',
            labelbottom=False
        )

        ax2.tick_params(
            axis='x',
            labelbottom=False
        )

        ax3.tick_params(
            axis='x',
            labelbottom=False
        )

        # ==========================================================
        # 总标题
        # ==========================================================
        fig.suptitle(
            f'{symbol} 多周期技术分析 | '
            f'4h趋势 + 1h趋势 + 15m趋势 + 5m入场',
            fontsize=16,
            fontweight='bold'
        )

        # ==========================================================
        # 保存
        # ==========================================================
        chart_dir = 'charts'
        os.makedirs(
            chart_dir,
            exist_ok=True
        )

        timestamp_str = datetime.now(
            tz_shanghai
        ).strftime('%Y%m%d_%H%M%S')

        chart_path = os.path.join(
            chart_dir,
            f'{symbol}_{timestamp_str}_{signal_type_str}.png'
        )

        plt.savefig(
            chart_path,
            bbox_inches='tight',
            dpi=150
        )

        plt.close(fig)

        logger.info(
            f"图表已生成: {chart_path}"
        )

        return chart_path

    except Exception as e:

        logger.error(
            f"图表生成失败({symbol}): {str(e)}",
            exc_info=True
        )

        # 防止异常情况下 Figure 没有关闭
        try:
            plt.close('all')
        except Exception:
            pass

        return None

def send_notification(symbol, signal_type, details, chart_path):
    """企业级邮件通知系统（含图表）"""
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        signal_cn = '做多' if signal_type == 'LONG' else '做空' if signal_type == 'SHORT' else '警报'

        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f5f6fa;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 10px; margin-bottom: 20px;">
              <h3 style="color: #ffffff; margin: 0;">📊 {symbol} {signal_cn}信号触发！</h3>
            </div>
            <div style="background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px;">
              <h4 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 10px;">📈 市场数据</h4>
              {'<br>'.join(f'<p style="margin: 8px 0; color: #34495e;">• {line}</p>' for line in details)}
            </div>
            {f'<div style="background: white; padding: 15px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center;">' if chart_path else ''}
            {f'<img src="cid:chart" style="max-width: 100%; border-radius: 8px;"><br>' if chart_path else ''}
            {f'<p style="color: #7f8c8d; font-size: 12px; margin-top: 10px;">技术分析图表 - {symbol}</p>' if chart_path else ''}
            {f'</div>' if chart_path else ''}
            <hr style="border: 0.5px solid #ecf0f1; margin: 20px 0;">
            <p style="color: #95a5a6; font-size: 12px; text-align: center;">
              此邮件由中期策略监控系统自动发送 | {datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}
            </p>
          </body>
        </html>
        """

        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        if chart_path and os.path.exists(chart_path):
            try:
                with open(chart_path, 'rb') as f:
                    img_data = f.read()
                img = MIMEImage(img_data, Name=os.path.basename(chart_path))
                img.add_header('Content-ID', '<chart>')
                msg.attach(img)
            except Exception as e:
                logger.error(f"图表附加失败: {str(e)}")

        with smtplib.SMTP_SSL(CONFIG['email']['server'], CONFIG['email']['port']) as server:
            for attempt in range(CONFIG['email']['max_retries']):
                try:
                    server.login(CONFIG['email']['from'], CONFIG['email']['password'])
                    server.sendmail(CONFIG['email']['from'], CONFIG['email']['to'], msg.as_string())
                    logger.info(f"{symbol} 邮件发送成功，附加图表：{bool(chart_path)}")
                    return True
                except Exception as e:
                    logger.error(f"邮件发送失败({symbol}) - 尝试 {attempt + 1}/{CONFIG['email']['max_retries']}: {str(e)}")
                    time.sleep(10)
        return False
    except Exception as e:
        logger.error(f"邮件发送失败({symbol}): {str(e)}", exc_info=True)
        return False


def monitor_market():
    """主监控循环"""
    try:
        logger.info("=" * 60)
        logger.info("启动短期期策略监控系统 v3.0")
        logger.info("=" * 60)
        start_time = datetime.now(tz_shanghai)
        runtime_limit = CONFIG['runtime']['duration']

        cycle_count = 0
        while (datetime.now(tz_shanghai) - start_time) < runtime_limit:
            cycle_count += 1
            cycle_start = datetime.now(tz_shanghai)
            logger.info(f"\n{'=' * 40}")
            logger.info(f"第 {cycle_count} 轮监控开始 | {datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"{'=' * 40}")

            for symbol in CONFIG['trading']['symbols']:
                try:
                    logger.info(f"\n--- 处理 {symbol} ---")

                    # 获取多周期数据
                    df1h = fetch_market_data(symbol, "1h")
                    df4h = fetch_market_data(symbol, "4h")
                    df15m = fetch_market_data(symbol, "15m")
                    df5m = fetch_market_data(symbol, "5m")
                    if df1h.empty or df4h.empty or df15m.empty or df5m.empty:
                        logger.warning(f"{symbol} 数据不完整，跳过")
                        continue
                    # 计算指标
                    df1h = calculate_technical_indicators(df1h, symbol)
                    df4h = calculate_technical_indicators(df4h, symbol)
                    df15m = calculate_technical_indicators(df15m, symbol)
                    df5m = calculate_technical_indicators(df5m, symbol)
                    # 多周期信号检测
                    signal, signal_details = check_multi_timeframe_signal(
                        df4h,
                        df1h,
                        df15m,
                        df5m
                    )
                    current_price = df1h['Close'].iloc[-1]
                    logger.info(f"✓价格 {current_price}")

                    if signal == 1:

                        logger.info(
                            f"🟢 {symbol} 多头信号触发 | "
                            f"4h=多头 1h=多头 15m=多头 5m=触发"
                        )

                        process_signal(
                            symbol,
                            signal,
                            df1h,
                            df4h,
                            df15m,
                            df5m,
                            signal_details
                        )

                    elif signal == -1:

                        logger.info(
                            f"🔴 {symbol} 空头信号触发 | "
                            f"4h=空头 1h=空头 15m=空头 5m=触发"
                        )

                        process_signal(
                            symbol,
                            signal,
                            df1h,
                            df4h,
                            df15m,
                            df5m,
                            signal_details
                        )
                    else:
                        logger.info(
                            f"✓ {symbol} 无信号 | "
                            f"4h多={signal_details.get('h4_long', False)} "
                            f"4h空={signal_details.get('h4_short', False)} "
                            f"1h多={signal_details.get('h1_long', False)} "
                            f"1h空={signal_details.get('h1_short', False)} "
                            f"15m多={signal_details.get('m15_long', False)} "
                             f"15m空={signal_details.get('m15_short', False)} "
                             f"5m多触发={signal_details.get('m5_long_trigger', False)} "
                            f"5m空触发={signal_details.get('m5_short_trigger', False)}"
                        )
                except Exception as e:
                    logger.error(f"主流程异常({symbol}): {str(e)}", exc_info=True)

            elapsed = (datetime.now(tz_shanghai) - cycle_start).total_seconds()
            logger.info(f"本轮耗时: {elapsed:.1f}秒 | 距下次检查: {CONFIG['runtime']['check_interval'] - elapsed:.1f}秒")
            time.sleep(max(0, CONFIG['runtime']['check_interval'] - elapsed))

        logger.info("已达到运行时长上限，正常退出")
        cleanup_system()

    except KeyboardInterrupt:
        logger.info("\n监控被用户中断")
        cleanup_system()
    except Exception as e:
        logger.critical(f"系统崩溃: {str(e)}", exc_info=True)
        send_alert(f"系统崩溃: {str(e)}")
        cleanup_system()


def process_signal(symbol, signal_type, df1h, df4h, df15m, df5m, signal_details):
    """处理交易信号"""
    current_time = datetime.now(tz_shanghai)
    cooldown = DYNAMIC_PARAMS[symbol]['signal_cooldown']

    # 冷却检查
    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < timedelta(minutes=cooldown):
            logger.info(f"{symbol} 信号冷却中，跳过")
            return

    # 使用5m作为实际入场价格、ATR和风险控制周期
    current_price = df5m['Close'].iloc[-1]
    atr_value = df5m['ATR'].iloc[-1]
    adx_value = df5m['ADX'].iloc[-2]
    print("当前币对价格：%s"%current_price)
    # 修复：参数顺序正确传入
    # =========================
    # 获取前一日 Pivot
    # =========================
    pivot = get_previous_day_pivot(symbol)

    # =========================
    # Pivot + ATR 计算止盈止损
    # =========================
    (
        stop_loss,
        take_profit,
        trailing_stop
    ) = calculate_optimized_positions(
        signal_type=signal_type,
        current_price=current_price,
        atr=atr_value,
        pivot=pivot
    )

    signal_map = {1: 'LONG', -1: 'SHORT'}
    signal_type_str = signal_map[signal_type]
    trade_result = place_trade(
                        symbol=f"{symbol}",
                        usdt=50,
                        direction=signal_type_str,
                        leverage=20,
                        take_profit=f"{take_profit:.2f}",
                        stop_loss=f"{stop_loss:.2f}"
                    )

    # 生成图表
    chart_path = generate_chart(df1h, df4h, df15m, df5m, symbol, signal_type_str)

    # 生成信号详情
    details = create_signal_details(df1h, df4h, df15m, df5m, signal_details)
    details.extend([
        "------------------------------------",
        "🔒 风险控制参数",
        f"当前ADX值：{adx_value:.2f} (趋势强度)",
        f"当前价格：{current_price:.2f}",
        f"止损位：{stop_loss:.2f} (基于ATR动态调整)",
        f"止盈位：{take_profit:.2f} (基于ATR动态调整)",
        f"滑动止损位：{trailing_stop:.2f} (价格每变动5%自动上移)",
        f"最大可承受亏损：{abs(current_price - stop_loss):.2f}",
        f"预期收益空间：{abs(take_profit - current_price):.2f}"
    ])


    # 发送邮件
    if trade_result:
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time

        # 记录交易历史
        state.trade_history[symbol].append({
            'time': current_time,
            'signal': signal_type_str,
            'price': current_price,
            'sl': stop_loss,
            'tp': take_profit
        })
        logger.info(f"{symbol} 信号已推送，交易已记录")

def create_signal_details(
        df1h,
        df4h,
        df15m,
        df5m,
        signal_details
):
    try:
        h4 = df4h.iloc[-2]
        h1 = df1h.iloc[-2]
        m15 = df15m.iloc[-2]
        m5 = df5m.iloc[-2]

        return [
            f"当前价格: {df5m['Close'].iloc[-1]:.2f}",
            "========== 4h 趋势过滤 ==========",
            f"4h DIF: {h4['DIF']:.5f}",
            f"4h DEA: {h4['DEA']:.5f}",
            f"4h RSI14/RSI27: {h4['RSI14']:.2f}/{h4['RSI27']:.2f}",
            f"4h K/D/J: {h4['K']:.2f}/{h4['D']:.2f}/{h4['J']:.2f}",
            "========== 1h 趋势过滤 ==========",
            f"1h DIF: {h1['DIF']:.5f}",
            f"1h DEA: {h1['DEA']:.5f}",
            f"1h RSI14/RSI27: {h1['RSI14']:.2f}/{h1['RSI27']:.2f}",
            f"1h K/D/J: {h1['K']:.2f}/{h1['D']:.2f}/{h1['J']:.2f}",
            "========== 15m 趋势过滤 ==========",
            f"15m DIF: {m15['DIF']:.5f}",
            f"15m DEA: {m15['DEA']:.5f}",
            f"15m RSI14/RSI27: {m15['RSI14']:.2f}/{m15['RSI27']:.2f}",
            f"15m K/D/J: {m15['K']:.2f}/{m15['D']:.2f}/{m15['J']:.2f}",
            "========== 5m 入场触发 ==========",
            f"5m K: {m5['K']:.2f}",
            f"5m D: {m5['D']:.2f}",
            f"5m J: {m5['J']:.2f}",
            f"KDJ金叉: {'是' if signal_details['kdj_golden_cross'] else '否'}",
            f"MACD金叉: {'是' if signal_details['macd_golden_cross'] else '否'}",
            f"5m多头触发: {'是' if signal_details['m5_long_trigger'] else '否'}",
            f"5m空头触发: {'是' if signal_details['m5_short_trigger'] else '否'}",
            f"5m ADX: {signal_details.get('adx', 0):.2f}",
        ]

    except Exception as e:
        logger.error(
            f"信号详情生成失败: {str(e)}",
            exc_info=True
        )
        return ["数据生成失败，请检查日志"]

def send_alert(message):
    """发送系统警报"""
    send_notification(
        symbol="SYSTEM",
        signal_type="ALERT",
        details=[message],
        chart_path=None,
    )


def cleanup_system():
    """清理系统资源"""
    logger.info("系统资源清理完成")
    logger.info(f"累计交易记录: {sum(len(v) for v in state.trade_history.values())} 笔")


if __name__ == "__main__":
    monitor_market()
