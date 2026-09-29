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
        'symbols': ['BTCUSDT', 'ETHUSDT', 'BNBUSDT','SOXLUSDT'],
        'data_limit': 1000,
        'volatility_window': 15,
        'signal_cooldown': timedelta(minutes=5),
        'missing_data_threshold': 0.9
    },
    'indicators': {
        'rsi_period': 6,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'bollinger_period': 10,
        'bollinger_std': 2.0,
        'atr_period': 14
    },
    'runtime': {
        'duration': timedelta(days=7),
        'check_interval': 300
    }
}

# 动态参数系统
DYNAMIC_PARAMS = {
    'BTCUSDT': {'signal_cooldown': 20},
    'ETHUSDT': {'signal_cooldown': 20},
    'BNBUSDT': {'signal_cooldown': 20},
    'SOXLUSDT': {'signal_cooldown': 20}
    
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


def get_konghuang():
    """获取恐慌贪婪指数"""
    try:
        response = requests.get("https://blz.bicoin.com.cn/okexFutureData/open/getFearGreedIndex", timeout=10)
        konghuang = response.json()
        zhibiao = konghuang.get("data", {}).get("today", {}).get("value", 0)
        return zhibiao
    except Exception as e:
        logger.warning(f"恐慌指数获取失败: {e}")
        return 0


def system_init():
    check_system_health()


def check_system_health():
    """系统健康检查"""
    try:
        free_space = shutil.disk_usage('./').free / (1024 ** 3)
        if free_space < 5:
            send_alert(f"系统警告：磁盘空间不足，剩余 {free_space:.1f}GB")
        current_time = datetime.now(tz_shanghai)
        if (current_time - state.system_health['last_check']).seconds > 300:
            if (current_time - state.last_alerts.get('health_check',
                                                     current_time - CONFIG['email']['alert_cooldown'] * 2)) > \
                    CONFIG['email']['alert_cooldown']:
                send_alert("系统健康检查通过")
                state.system_health['last_check'] = current_time
                state.last_alerts['health_check'] = current_time
    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}", exc_info=True)


def fetch_market_data(symbol, interval="15m", limit=CONFIG['trading']['data_limit']):
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
    """计算所有技术指标"""
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    try:
        df['RSI'] = calculate_rsi(df['Close'], params['rsi_period']).fillna(50)
        df['DIF'], df['DEA'], df['MACD'] = calculate_macd(df['Close'], params['macd_fast'], params['macd_slow'],
                                                          params['macd_signal'])
        df['ATR'] = calculate_atr(df, params['atr_period'])
        df['Momentum'] = df['Close'].pct_change(periods=3) * 100
        df['EMA50'] = df['Close'].ewm(span=50).mean()
        df['EMA200'] = df['Close'].ewm(span=200).mean()
        df['VOL_MA20'] = df['Volume'].rolling(20).mean()
        df['ADX'] = calculate_adx_pure_fixed(df, period=14)
        df['MA20'] = df['Close'].rolling(20).mean()
        df['MA60'] = df['Close'].rolling(60).mean()
        df['MACD_HIST'] = df['DIF'] - df['DEA']
        df = calculate_atr_pct(df)

        # 布林带
        df['BB_MIDDLE'] = df['Close'].rolling(params['bollinger_period']).mean()
        bb_std = df['Close'].rolling(params['bollinger_period']).std()
        df['BB_UPPER'] = df['BB_MIDDLE'] + bb_std * params['bollinger_std']
        df['BB_LOWER'] = df['BB_MIDDLE'] - bb_std * params['bollinger_std']

        return df
    except Exception as e:
        logger.error(f"指标计算失败({symbol}): {str(e)}", exc_info=True)
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
        return rsi.shift(1).clip(0, 100).fillna(50)
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
        return (pd.Series([0] * len(series), index=series.index),
                pd.Series([0] * len(series), index=series.index),
                pd.Series([0] * len(series), index=series.index))


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

        smooth_plus_dm[0] = plus_dm[0]
        smooth_minus_dm[0] = minus_dm[0]
        smooth_tr[0] = tr[0]

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

        adx_series = pd.Series(adx_line, index=df.index).bfill().fillna(50)
        return adx_series
    except Exception as e:
        logger.error(f"ADX计算失败: {str(e)}")
        return pd.Series([50] * len(df), index=df.index)


def calculate_optimized_positions(signal_type, current_price, atr):
    """
    根据信号类型计算止损止盈位
    signal_type: 1=做多, -1=做空
    """
    if signal_type == 1:
        stop_loss = current_price - atr * 1
        take_profit = current_price + atr * 1.5
    else:
        stop_loss = current_price + atr * 1
        take_profit = current_price - atr * 1.5

    trailing_stop = atr * 1.5
    return stop_loss, take_profit, trailing_stop


def check_signals(df15, df1h, df4h, symbol):
    """多周期信号检测"""
    signal = 0

    volume_ok = df15['Volume'].iloc[-1] > df15['VOL_MA20'].iloc[-1] * 1.5
    adx_ok = df15['ADX'].iloc[-1] > 25

    trend_long = df4h['EMA50'].iloc[-1] > df4h['EMA200'].iloc[-1]
    trend_short = df4h['EMA50'].iloc[-1] < df4h['EMA200'].iloc[-1]

    macd_long = df1h['MACD_HIST'].iloc[-1] > 0
    macd_short = df1h['MACD_HIST'].iloc[-1] < 0

    rsi_long = df15['RSI'].iloc[-2] <= 35 and df15['RSI'].iloc[-1] >= 40
    rsi_short = df15['RSI'].iloc[-2] >= 70 and df15['RSI'].iloc[-1] <= 65
    print("信号指标信息")
    print(trend_long)
    print(macd_long)
    print(rsi_long)
    print(volume_ok)
    print(adx_ok)

    if trend_long and macd_long and rsi_long and (volume_ok or adx_ok):
        signal = 1
    elif trend_short and macd_short and rsi_short and (volume_ok or adx_ok):
        signal = -1

    df15['Signal'] = 0
    df15.at[df15.index[-1], 'Signal'] = signal
    return df15


def generate_chart(df15, df1h, df4h, symbol, signal_type):
    """
    生成综合技术分析图表并保存
    返回图表文件路径
    """
    try:
        fig = plt.figure(figsize=(16, 12))
        gs = fig.add_gridspec(4, 1, height_ratios=[3, 1, 1, 1], hspace=0.3)

        # === 子图1: K线 + 布林带 + 均线 ===
        ax1 = fig.add_subplot(gs[0])
        candles = df15.tail(80)

        # 绘制K线（简化版：用高低线+收盘标记）
        for i, (idx, row) in enumerate(candles.iterrows()):
            color = '#26a69a' if row['Close'] >= row['Open'] else '#ef5350'
            ax1.plot([i, i], [row['Low'], row['High']], color=color, linewidth=1)
            ax1.plot([i, i], [row['Open'], row['Close']], color=color, linewidth=4)

        # 布林带
        ax1.plot(range(len(candles)), candles['BB_UPPER'], 'b--', alpha=0.5, label='BB Upper')
        ax1.plot(range(len(candles)), candles['BB_MIDDLE'], 'b-', alpha=0.5, label='BB Middle')
        ax1.plot(range(len(candles)), candles['BB_LOWER'], 'b--', alpha=0.5, label='BB Lower')
        ax1.fill_between(range(len(candles)), candles['BB_UPPER'], candles['BB_LOWER'], alpha=0.1, color='blue')

        # 均线
        ax1.plot(range(len(candles)), candles['EMA50'], 'orange', label='EMA50', linewidth=1.5)
        ax1.plot(range(len(candles)), candles['EMA200'], 'purple', label='EMA200', linewidth=1.5)

        # 信号标记
        last_signal = candles['Signal'].iloc[-1]
        if last_signal == 1:
            ax1.scatter(len(candles) - 1, candles['Close'].iloc[-1], marker='^', s=200, c='green', zorder=5,
                        label='BUY SIGNAL')
        elif last_signal == -1:
            ax1.scatter(len(candles) - 1, candles['Close'].iloc[-1], marker='v', s=200, c='red', zorder=5,
                        label='SELL SIGNAL')

        ax1.set_title(f'{symbol} 技术分析图 | 信号: {"做多" if last_signal == 1 else "做空" if last_signal == -1 else "无"}',
                      fontsize=14, fontweight='bold')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.set_ylabel('价格')
        ax1.grid(True, alpha=0.3)

        # === 子图2: MACD ===
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        macd_data = df1h.tail(80)
        x_macd = range(len(macd_data))
        ax2.bar(x_macd, macd_data['MACD_HIST'],
                color=['#26a69a' if v >= 0 else '#ef5350' for v in macd_data['MACD_HIST']], alpha=0.6,
                label='MACD Hist')
        ax2.plot(x_macd, macd_data['DIF'], 'blue', label='DIF', linewidth=1)
        ax2.plot(x_macd, macd_data['DEA'], 'orange', label='DEA', linewidth=1)
        ax2.axhline(0, color='gray', linestyle='--', linewidth=0.5)
        ax2.set_ylabel('MACD')
        ax2.legend(loc='upper left', fontsize=8)
        ax2.grid(True, alpha=0.3)

        # === 子图3: RSI ===
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        rsi_data = df15.tail(80)
        ax3.plot(range(len(rsi_data)), rsi_data['RSI'], 'purple', linewidth=1.5)
        ax3.axhline(70, color='red', linestyle='--', linewidth=0.8, alpha=0.7)
        ax3.axhline(30, color='green', linestyle='--', linewidth=0.8, alpha=0.7)
        ax3.axhline(50, color='gray', linestyle='-', linewidth=0.5, alpha=0.5)
        ax3.fill_between(range(len(rsi_data)), 30, 70, alpha=0.1, color='gray')
        ax3.set_ylabel('RSI')
        ax3.set_ylim(0, 100)
        ax3.grid(True, alpha=0.3)

        # === 子图4: 成交量 + ADX ===
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        vol_data = df15.tail(80)
        colors = ['#26a69a' if row['Close'] >= row['Open'] else '#ef5350' for _, row in vol_data.iterrows()]
        ax4.bar(range(len(vol_data)), vol_data['Volume'], color=colors, alpha=0.6)
        ax4.plot(range(len(vol_data)), vol_data['VOL_MA20'], 'orange', linewidth=1.5, label='VOL MA20')

        ax4_twin = ax4.twinx()
        ax4_twin.plot(range(len(vol_data)), vol_data['ADX'], 'blue', linewidth=1.5, label='ADX')
        ax4_twin.axhline(25, color='red', linestyle='--', linewidth=0.8, alpha=0.7)
        ax4_twin.set_ylabel('ADX')
        ax4_twin.set_ylim(0, 100)

        ax4.set_ylabel('成交量')
        ax4.set_xlabel('K线序号（最近80根）')
        ax4.legend(loc='upper left', fontsize=8)
        ax4_twin.legend(loc='upper right', fontsize=8)
        ax4.grid(True, alpha=0.3)

        # 保存图表
        chart_dir = 'charts'
        os.makedirs(chart_dir, exist_ok=True)
        timestamp_str = datetime.now(tz_shanghai).strftime('%Y%m%d_%H%M%S')
        chart_path = os.path.join(chart_dir, f'{symbol}_{timestamp_str}_signal.png')
        plt.savefig(chart_path, bbox_inches='tight', dpi=150)
        plt.close()
        logger.info(f"图表已生成: {chart_path}")
        return chart_path

    except Exception as e:
        logger.error(f"图表生成失败({symbol}): {str(e)}", exc_info=True)
        return None


def send_notification(symbol, signal_type, details, chart_path, zhongxian_konghuang):
    """企业级邮件通知系统（含图表）"""
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        zhongxian_konghuang = str(zhongxian_konghuang)

        signal_cn = '做多' if signal_type == 'LONG' else '做空' if signal_type == 'SHORT' else '警报'

        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background-color: #f5f6fa;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 10px; margin-bottom: 20px;">
              <h3 style="color: #ffffff; margin: 0;">📊 {symbol} {signal_cn}信号触发！</h3>
              <p style="color: #e0e0e0; margin: 5px 0 0 0;">恐慌贪婪指数: {zhongxian_konghuang}</p>
            </div>

            <div style="background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px;">
              <h4 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 10px;">📈 市场数据</h4>
              {'<br>'.join(f'<p style="margin: 8px 0; color: #34495e;">• {line}</p>' for line in details)}
            </div>

            <div style="background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px;">
              <h4 style="color: #2c3e50; border-bottom: 2px solid #ecf0f1; padding-bottom: 10px;">🔒 风险控制参数</h4>
              {'<br>'.join(f'<p style="margin: 8px 0; color: #34495e;">• {line}</p>' for line in details[-6:])}
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
        logger.info("启动中期策略监控系统 v2.0")
        logger.info("=" * 60)
        system_init()
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
                    df15 = fetch_market_data(symbol, "15m")
                    df1h = fetch_market_data(symbol, "1h")
                    df4h = fetch_market_data(symbol, "4h")

                    if df15.empty or df1h.empty or df4h.empty:
                        logger.warning(f"{symbol} 数据不完整，跳过")
                        continue

                    # 计算指标
                    df15 = calculate_technical_indicators(df15, symbol)
                    df1h = calculate_technical_indicators(df1h, symbol)
                    df4h = calculate_technical_indicators(df4h, symbol)

                    # 检测信号
                    df15 = check_signals(df15, df1h, df4h, symbol)
                    latest_signal = df15['Signal'].iloc[-1]

                    if latest_signal != 0:
                        logger.info(f"⚠️ {symbol} 检测到信号: {'做多' if latest_signal == 1 else '做空'}")
                        process_signal(symbol, latest_signal, df15, df1h, df4h)
                    else:
                        logger.info(f"✓ {symbol} 无信号")
                        current_price = df15['Close'].iloc[-1]
                        logger.info(f"✓价格 {current_price}")

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


def process_signal(symbol, signal_type, df15, df1h, df4h):
    """处理交易信号"""
    current_time = datetime.now(tz_shanghai)
    cooldown = DYNAMIC_PARAMS[symbol]['signal_cooldown']

    # 冷却检查
    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < timedelta(minutes=cooldown):
            logger.info(f"{symbol} 信号冷却中，跳过")
            return

    # 修复：使用 df15 而非未定义的 df
    current_price = df15['Close'].iloc[-1]
    atr_value = df15['ATR'].iloc[-1]
    adx_value = df15['ADX'].iloc[-1]
    print("当前币对价格：%s"%current_price)
    # 修复：参数顺序正确传入
    stop_loss, take_profit, trailing_stop = calculate_optimized_positions(signal_type, current_price, atr_value)

    signal_map = {1: 'LONG', -1: 'SHORT'}
    signal_type_str = signal_map[signal_type]

    # 生成图表
    chart_path = generate_chart(df15, df1h, df4h, symbol, signal_type_str)

    # 生成信号详情
    details = create_signal_details(df15, df1h, df4h)
    details.extend([
        "------------------------------------",
        "🔒 风险控制参数",
        f"当前ADX值：{adx_value:.2f} (趋势强度)",
        f"止损位：{stop_loss:.2f} (基于ATR动态调整)",
        f"止盈位：{take_profit:.2f} (基于ATR动态调整)",
        f"滑动止损位：{trailing_stop:.2f} (价格每变动5%自动上移)",
        f"最大可承受亏损：{abs(current_price - stop_loss):.2f}",
        f"预期收益空间：{abs(take_profit - current_price):.2f}"
    ])

    # 发送邮件
    konghuang = get_konghuang()
    if send_notification(symbol, signal_type_str, details, chart_path, konghuang):
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


def create_signal_details(df15, df1h, df4h):
    """生成信号详情列表"""
    try:
        return [
            f"当前价格: {df15['Close'].iloc[-1]:.2f}",
            f"ATR(14): {df15['ATR'].iloc[-1]:.2f}",
            f"4H EMA50: {df4h['EMA50'].iloc[-1]:.2f}",
            f"4H EMA200: {df4h['EMA200'].iloc[-1]:.2f}",
            f"1H MACD_HIST: {df1h['MACD_HIST'].iloc[-1]:.4f}",
            f"15M RSI: {df15['RSI'].iloc[-1]:.2f}",
            f"ADX(14): {df15['ADX'].iloc[-1]:.2f}",
            f"成交量倍数: {df15['Volume'].iloc[-1] / df15['VOL_MA20'].iloc[-1]:.2f}x",
            f"恐慌贪婪指数: {get_konghuang()}"
        ]
    except Exception as e:
        logger.error(f"信号详情生成失败: {str(e)}", exc_info=True)
        return ["数据生成失败，请检查日志"]


def send_alert(message):
    """发送系统警报"""
    send_notification(
        symbol="SYSTEM",
        signal_type="ALERT",
        details=[message],
        chart_path=None,
        zhongxian_konghuang=0
    )


def cleanup_system():
    """清理系统资源"""
    logger.info("系统资源清理完成")
    logger.info(f"累计交易记录: {sum(len(v) for v in state.trade_history.values())} 笔")


if __name__ == "__main__":
    monitor_market()
