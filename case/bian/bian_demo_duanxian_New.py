import requests, smtplib, time, json, os, logging, traceback, pytz, shutil
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from collections import defaultdict
import matplotlib.patches as patches
from mplfinance.original_flavor import candlestick_ohlc
import matplotlib.dates as mdates

# 配置日志系统
from bian_demo_creat_order_demo import place_trade

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

# Matplotlib配置
plt.switch_backend('Agg')
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
        'symbols': ['BTCUSDT','ETHUSDT'],
        'data_limit': 1000,
        'chart_dir': "short_term_charts",
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
        # 'duration': timedelta(hours=480),  #控制运行时长小时
        'duration': timedelta(days=7),  #控制运行时长天
        'check_interval': 300   #控制运行间隔时间秒
    }
}

# 动态参数系统终极版
DYNAMIC_PARAMS = {
    'BTCUSDT': {
        'volume_threshold': 5e8,
        'volatility_coeff': 1.5,
        'rsi_threshold_base': 10,  # 优化点1：降低RSI买入阈值
        'rsi_sensitivity': 0.6,
        'atr_period': 14,
        'signal_cooldown': 20,  # 优化点2：缩短冷却时间至20分钟
        'trend_filter': True,
        'adx_threshold': 40,
        'stop_multiplier': lambda adx: 1.2 + (adx-35)*0.05 if adx > 35 else 1.8,
        'take_multiplier': lambda adx: 2.5 + (adx-35)*0.03 if adx > 35 else 2.0
    },
    'ETHUSDT': {
        'volume_threshold': 3e8,
        'volatility_coeff': 1.3,
        'rsi_threshold_base': 10,
        'rsi_sensitivity': 0.6,
        'atr_period': 14,
        'signal_cooldown': 20,
        'trend_filter': True,
        'adx_threshold': 35,
        'stop_multiplier': lambda adx: 1.3 + (adx-30)*0.04 if adx > 30 else 1.9,
        'take_multiplier': lambda adx: 2.2 + (adx-30)*0.02 if adx > 30 else 1.8
    }
}

class GlobalState:
    def __init__(self):
        self.market_data = defaultdict(pd.DataFrame)
        self.market_volatility = defaultdict(float)
        self.last_signals = {}
        self.last_emails = {}
        self.last_alerts = {}
        self.system_health = {'last_check': datetime.now(tz_shanghai)}
        self.trailing_stops = defaultdict(lambda: defaultdict(float))

state = GlobalState()

def get_konghuang():
    try:
        konghuang = requests.get("https://blz.bicoin.com.cn/okexFutureData/open/getFearGreedIndex").json()
        zhibiao = konghuang.get("data").get("today").get("value")
    except:
        zhibiao = 0
    return zhibiao

def system_init():
    os.makedirs(CONFIG['trading']['chart_dir'], exist_ok=True)
    check_system_health()

def identify_candle_pattern(df):
    try:
        df = df.copy()
        df['Consecutive_Yang'] = (df['Close'] > df['Open']).astype(int)
        df['Five_Yang'] = (df['Consecutive_Yang'].rolling(5).sum() == 5).astype(bool)
        df['Consecutive_Yin'] = (df['Close'] < df['Open']).astype(int)
        df['Five_Yin'] = (df['Consecutive_Yin'].rolling(5).sum() == 5).astype(bool)
        return df
    except Exception as e:
        logger.error(f"形态识别失败: {str(e)}", exc_info=True)
        df['Five_Yang'] = False
        df['Five_Yin'] = False
        return df

def check_system_health():
    try:
        free_space = shutil.disk_usage('./').free / (1024**3)
        if free_space < 5:
            send_alert(f"系统警告：磁盘空间不足，剩余 {free_space:.1f}GB")
        current_time = datetime.now(tz_shanghai)
        if (current_time - state.system_health['last_check']).seconds > 300:
            if (current_time - state.last_alerts.get('health_check', current_time - CONFIG['email']['alert_cooldown']*2)) > CONFIG['email']['alert_cooldown']:
                send_alert("系统健康检查通过")
                state.system_health['last_check'] = current_time
                state.last_alerts['health_check'] = current_time
    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}", exc_info=True)

def fetch_market_data(symbol, limit=CONFIG['trading']['data_limit']):
    endpoint = f'api/v2/mix/market/candles?symbol={symbol}&granularity=1H&limit={limit}&productType=usdt-futures'
    url = 'https://api.bitget.com/' + endpoint
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()['data']
    if not data:
        send_alert(f"数据接口返回空数据：{symbol}")
        return pd.DataFrame()
    df = process_raw_data(data)
    if df.empty:
        return df
    df = enhance_data_quality(df, symbol)
    return df

def process_raw_data(raw_data):
    try:
        cleaned = []
        for bar in raw_data:
            if len(bar) >= 6:
                try:
                    numeric_bar = [
                        float(bar[1]), float(bar[2]), float(bar[3]),
                        float(bar[4]), float(bar[6]), int(bar[0])
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
        df = identify_candle_pattern(df)
        return df
    except Exception as e:
        logger.error(f"数据处理失败: {str(e)}", exc_info=True)
        return pd.DataFrame()

def enhance_data_quality(df, symbol):
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
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    try:
        df['RSI'] = calculate_rsi(df['Close'], params['rsi_period']).fillna(50)
        df['DIF'], df['DEA'], df['MACD'] = calculate_macd(df['Close'], params['macd_fast'], params['macd_slow'], params['macd_signal'])
        df['Middle'], df['Upper'], df['Lower'] = calculate_bollinger(df['Close'], params['bollinger_period'], params['bollinger_std'])
        df['ATR'] = calculate_atr(df, params['atr_period'])
        df['Momentum'] = df['Close'].pct_change(periods=3) * 100
        df['Volatility_Ratio'] = df['ATR'] / df['Close']
        df['ADX'] = calculate_adx_pure_fixed(df, period=params.get('adx_threshold', 14))
        df['MA20'] = df['Close'].rolling(20).mean()
        df['MA60'] = df['Close'].rolling(60).mean()
        df['Trend_MA'] = (df['MA20'] > df['MA60']).astype(int)
        df = calculate_stochastic(df)
        df = calculate_cci(df)
        df = calculate_vwap(df)
        df = calculate_atr_pct(df)
        return df
    except Exception as e:
        logger.error(f"指标计算失败({symbol}): {str(e)}", exc_info=True)
        return df

def calculate_rsi(series, period=14):
    try:
        delta = series.diff(1)
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(com=period-1, adjust=False).mean()
        avg_loss = loss.ewm(com=period-1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi.shift(1).clip(0, 100).fillna(50)
    except Exception as e:
        logger.error(f"RSI计算失败: {str(e)}", exc_info=True)
        return pd.Series([50]*len(series), index=series.index)

def calculate_stochastic(df, period=14, smooth=3):
    df = df.copy()
    low_min = df['Low'].rolling(period).min()
    high_max = df['High'].rolling(period).max()
    df['%K'] = 100 * (df['Close'] - low_min) / (high_max - low_min).replace(0, 1e-10)
    df['%D'] = df['%K'].rolling(smooth).mean()
    return df

def calculate_cci(df, period=20):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    tp_sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean())
    df['CCI'] = (tp - tp_sma) / (0.015 * mad)
    return df

def calculate_vwap(df):
    df['VWAP'] = (df['Close'] * df['Volume']).cumsum() / df['Volume'].cumsum()
    return df

def calculate_atr_pct(df, period=14):
    df['ATR_PCT'] = df['ATR'] / df['Close'] * 100
    return df

def calculate_macd(series, fast=12, slow=26, signal=9):
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
        return (pd.Series([0]*len(series), index=series.index),
                pd.Series([0]*len(series), index=series.index),
                pd.Series([0]*len(series), index=series.index))

def calculate_bollinger(series, period=20, std=2):
    try:
        series = series.dropna().astype(float)
        rolling_mean = series.rolling(period).mean()
        rolling_std = series.rolling(period).std()
        middle = rolling_mean.shift()
        upper = middle + std * rolling_std.shift()
        lower = middle - std * rolling_std.shift()
        return middle.bfill(), upper.bfill(), lower.bfill()
    except Exception as e:
        logger.error(f"布林带计算失败: {str(e)}", exc_info=True)
        return (pd.Series([0]*len(series), index=series.index),
                pd.Series([0]*len(series), index=series.index),
                pd.Series([0]*len(series), index=series.index))

def calculate_atr(df, period=14):
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
        return pd.Series([0]*len(df), index=df.index)

def calculate_adx_pure_fixed(df, period=14):
    try:
        high = df['High'].values
        low = df['Low'].values
        close = df['Close'].values

        plus_dm = np.zeros_like(high)
        minus_dm = np.zeros_like(high)
        for i in range(1, len(high)):
            high_diff = high[i] - high[i-1]
            low_diff = low[i-1] - low[i]
            if high_diff > low_diff and high_diff > 0:
                plus_dm[i] = high_diff
            if low_diff > high_diff and low_diff > 0:
                minus_dm[i] = low_diff

        tr = np.zeros_like(high)
        for i in range(1, len(high)):
            tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))

        smooth_plus_dm = np.zeros(len(plus_dm))
        smooth_minus_dm = np.zeros(len(minus_dm))
        smooth_tr = np.zeros(len(tr))

        smooth_plus_dm[0] = plus_dm[0]
        smooth_minus_dm[0] = minus_dm[0]
        smooth_tr[0] = tr[0]

        for i in range(1, len(high)):
            smooth_plus_dm[i] = ((period-1)*smooth_plus_dm[i-1] + plus_dm[i]) / period
            smooth_minus_dm[i] = ((period-1)*smooth_minus_dm[i-1] + minus_dm[i]) / period
            smooth_tr[i] = ((period-1)*smooth_tr[i-1] + tr[i]) / period

        plus_di = np.zeros_like(smooth_plus_dm)
        minus_di = np.zeros_like(smooth_minus_dm)
        non_zero_tr = smooth_tr != 0
        plus_di[non_zero_tr] = smooth_plus_dm[non_zero_tr] / smooth_tr[non_zero_tr] * 100
        minus_di[non_zero_tr] = smooth_minus_dm[non_zero_tr] / smooth_tr[non_zero_tr] * 100

        dx = np.zeros_like(plus_di)
        dx_denom = plus_di + minus_di
        dx[dx_denom != 0] = abs(plus_di[dx_denom != 0] - minus_di[dx_denom != 0]) / dx_denom[dx_denom != 0] * 100

        adx = np.zeros_like(dx)
        adx_line = np.zeros(len(dx))

        if len(dx) >= period:
            adx_line[:period] = np.mean(dx[:period])

        for i in range(period, len(dx)):
            adx_line[i] = ((period - 1) * adx_line[i-1] + dx[i]) / period

        adx_series = pd.Series(adx_line, index=df.index).bfill().fillna(50)
        return adx_series
    except Exception as e:
        logger.error(f"ADX计算失败: {str(e)}")
        return pd.Series([50]*len(df), index=df.index)

def calculate_optimized_positions(adx_value, current_price, atr_value, symbol):
    params = DYNAMIC_PARAMS[symbol]
    if adx_value > params['adx_threshold']:
        stop_loss = current_price - params['stop_multiplier'](adx_value) * atr_value
        take_profit = current_price + params['take_multiplier'](adx_value) * atr_value
    else:
        stop_loss = current_price - 2.0 * atr_value
        take_profit = current_price + 1.8 * atr_value

    # 添加滑动止损机制
    trailing_stop = current_price - 0.03 * atr_value
    return stop_loss, take_profit, trailing_stop

def check_signals(df, symbol):
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    df['Signal'] = 0
    current_price = df['Close'].iloc[-1]
    atr_value = df['ATR'].iloc[-1]
    adx_value = df['ADX'].iloc[-1]
    current_trend = df['Trend_MA'].iloc[-1]
    stochastic_oversold = (df['%K'].iloc[-1] < 20) & (df['%D'].iloc[-1] < 20)
    stochastic_overbought = (df['%K'].iloc[-1] > 80) & (df['%D'].iloc[-1] > 80)
    cci_oversold = df['CCI'].iloc[-1] < -100
    cci_overbought = df['CCI'].iloc[-1] > 100
    vwap_below = current_price < df['VWAP'].iloc[-1]
    vwap_above = current_price > df['VWAP'].iloc[-1]
    boll_lower = current_price < df['Lower'].iloc[-1]
    boll_upper = current_price > df['Upper'].iloc[-1]
    trend_strength = (df['MA20'].iloc[-1] - df['MA60'].iloc[-1]) / df['Close'].iloc[-1] * 100

    # 动态RSI阈值
    current_volatility = state.market_volatility.get(symbol, 0.05)
    normalized_vol = current_volatility / 100
    rsi_threshold = max(10, params['rsi_threshold_base'] - normalized_vol * 10)
    volume_threshold = max(1e6, params['volume_threshold'] * (1 + normalized_vol * 0.5))

    # 信号确认逻辑
    buy_conditions = [
        (df['DIF'].iloc[-1] < df['DEA'].iloc[-1]) and (df['DIF'].iloc[-2] < df['DEA'].iloc[-2]),  # MACD金叉
        boll_lower,  # 布林下轨突破
        (df['RSI'].iloc[-1] < rsi_threshold),  # RSI超卖
        (df['Volume'].iloc[-1] > volume_threshold),  # 成交量确认
        vwap_below,  # 价格在VWAP下方
        (adx_value > params['adx_threshold']) and (current_trend == 1),  # 趋势确认
        stochastic_oversold,  # 随机指标超卖
        df['Five_Yang'].iloc[-1]  # 五连阳
    ]

    sell_conditions = [
        (df['DIF'].iloc[-1] > df['DEA'].iloc[-1]) and (df['DIF'].iloc[-2] > df['DEA'].iloc[-2]),  # MACD死叉
        boll_upper,  # 布林上轨突破
        (df['RSI'].iloc[-1] > (100 - rsi_threshold)),  # RSI超买
        (df['Volume'].iloc[-1] > volume_threshold),  # 成交量确认
        vwap_above,  # 价格在VWAP上方
        (adx_value > params['adx_threshold']) and (current_trend == 0),  # 趋势确认
        stochastic_overbought,  # 随机指标超买
        df['Five_Yin'].iloc[-1]  # 五连阴
    ]

    # 动态权重系统
    weight_buy = sum([1 if cond else 0 for cond in buy_conditions])
    weight_sell = sum([1 if cond else 0 for cond in sell_conditions])

    # 最终信号决策
    if weight_buy >= 5:
        df.at[df.index[-1], 'Signal'] = 1
    elif weight_sell >= 5:
        df.at[df.index[-1], 'Signal'] = -1

    return df

def generate_technical_chart(df, symbol):
    try:
        if len(df) < 20:
            df = fetch_market_data(symbol, limit=50)
            if df.empty:
                return None
        current_time = datetime.now(tz_shanghai).strftime('%Y%m%d_%H%M%S')
        filename = f"{CONFIG['trading']['chart_dir']}/{symbol}_{current_time}.png"
        fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(16, 12))
        df_reset = df.reset_index()
        df_reset['Date'] = df_reset['Timestamp'].apply(mdates.date2num)
        ohlc = df_reset[['Date', 'Open', 'High', 'Low', 'Close']].values.tolist()
        candlestick_ohlc(ax1, ohlc, width=0.015, colorup='g', colordown='r', alpha=0.8)
        ax1.plot(df.index, df['Upper'], '--', color='crimson', label='上轨')
        ax1.plot(df.index, df['Middle'], '--', color='darkorange', label='中轨')
        ax1.plot(df.index, df['Lower'], '--', color='crimson', label='下轨')
        five_yang = df[df['Five_Yang']]
        five_yin = df[df['Five_Yin']]
        ax1.scatter(five_yang.index, five_yang['Close'], marker='^', color='limegreen', s=120, label='五连阳')
        ax1.scatter(five_yin.index, five_yin['Close'], marker='v', color='r', s=120, label='五连阴')
        ax1.xaxis_date()
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax1.legend(loc='upper left')
        ax1.set_title(f'{symbol} 价格通道与K线形态分析（中短线）')
        ax2.plot(df.index, df['RSI'], label='RSI', color='purple')
        ax2.axhline(85, color='red', linestyle='--', label='超买*')
        ax2.axhline(90, color='darkred', linestyle='--', label='超买**')
        ax2.axhline(95, color='maroon', linestyle='--', label='超买***')
        ax2.axhline(15, color='green', linestyle='--', label='超卖*')
        ax2.axhline(10, color='darkgreen', linestyle='--', label='超卖**')
        ax2.axhline(5, color='darkcyan', linestyle='--', label='超卖***')
        ax2.legend(loc='upper left')
        ax2.set_title(f'RSI形态分析（中短线）')
        ax3.plot(df.index, df['DIF'], label='DIF', color='blue')
        ax3.plot(df.index, df['DEA'], label='DEA', color='orange')
        ax3.bar(df.index, df['MACD'], label='MACD柱', color='gray', alpha=0.5)
        ax3.axhline(0, color='black', linestyle='-', linewidth=0.5)
        ax3.legend(loc='upper left')
        ax3.set_title(f'MACD形态分析（中短线）')
        ax4.plot(df.index, df['Volume']/1e7, label='成交量(千万)', color='brown')
        ax4.legend(loc='upper left')
        ax4.set_title(f'成交量形态分析（中短线）')
        plt.tight_layout()
        plt.savefig(filename)
        plt.close()
        if os.path.getsize(filename) < 5000:
            raise ValueError("图表生成失败")
        return filename
    except Exception as e:
        logger.error(f"图表生成失败({symbol}): {str(e)}", exc_info=True)
        return None

# 邮件通知模块
def send_notification(symbol, signal_type, details, chart_path,zhongxian_konghuang):
    """企业级邮件通知系统"""
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        zhongxian_konghuang=str(get_konghuang())
        msg['Subject'] = f"【中期策略警报】{symbol} {signal_type}信号触发,恐慌指数{zhongxian_konghuang}"

        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h3 style="color: #2c3e50;">{symbol} {signal_type}信号触发！恐慌指数{zhongxian_konghuang}</h3>
            <hr style="border: 0.5px solid #ecf0f1;">
            <p style="color: #34495e;">{'<br>'.join(details)}</p>
            <hr style="border: 0.5px solid #ecf0f1;">
            <h4 style="color: #2c3e50;">市场分析：</h4>
            <ul style="color: #34495e;">
              <li>当前波动率：{state.market_volatility.get(symbol, 0.05):.2%}</li>
              <li>价格位置：{'下轨下方' if signal_type=='买入' else '上轨上方'}</li>
            </ul>
            {f'<img src="cid:chart" style="max-width: 100%; margin-top: 20px;"><br>' if chart_path else ''}
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
                    logger.info(f"{symbol}邮件发送成功，附加图表：{bool(chart_path)}")
                    return True
                except Exception as e:
                    logger.error(f"邮件发送失败({symbol}) - 尝试 {attempt+1}/{CONFIG['email']['max_retries']}: {str(e)}")
                    time.sleep(10)
        return False
    except Exception as e:
        logger.error(f"邮件发送失败({symbol}): {str(e)}", exc_info=True)
        return False


def update_volatility(symbol, df):
    try:
        returns = df['Close'].pct_change().dropna()
        if not returns.empty:
            volatility = returns.std() * np.sqrt(252)
            state.market_volatility[symbol] = round(float(volatility), 5)
    except Exception as e:
        logger.error(f"波动率更新失败({symbol}): {str(e)}", exc_info=True)

def monitor_market():
    try:
        logger.info("启动短线交易监控系统...")
        system_init()
        start_time = datetime.now(tz_shanghai)
        runtime_limit = CONFIG['runtime']['duration']
        while (datetime.now(tz_shanghai) - start_time) < runtime_limit:
            cycle_start = datetime.now(tz_shanghai)
            for symbol in CONFIG['trading']['symbols']:
                try:
                    df = fetch_market_data(symbol)
                    if df.empty:
                        continue
                    update_volatility(symbol, df)
                    df = calculate_technical_indicators(df, symbol)
                    df = check_signals(df, symbol)
                    latest_signal = df['Signal'].iloc[-1]
                    if latest_signal != 0:
                        process_signal(symbol, latest_signal, df)
                except Exception as e:
                    logger.error(f"主流程异常({symbol}): {str(e)}", exc_info=True)
            time.sleep(CONFIG['runtime']['check_interval'])
            print("运行一次成功")
        logger.info("已达到1小时运行时间，正常退出")
        cleanup_system()
    except KeyboardInterrupt:
        logger.info("\n监控被用户中断")
    except Exception as e:
        logger.critical(f"系统崩溃: {str(e)}", exc_info=True)
        send_alert(f"系统崩溃: {str(e)}")
    finally:
        # 添加止盈成功率计算
        btc_success_rate = calculate_take_profit_success_rate('BTCUSDT')
        logger.info(f"BTCUSDT 止盈成功率: {btc_success_rate:.2%}")
        cleanup_system()

def process_signal(symbol, signal_type, df):
    current_time = datetime.now(tz_shanghai)
    cooldown = DYNAMIC_PARAMS[symbol]['signal_cooldown']
    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < timedelta(minutes=cooldown):
            return
    current_price = df['Close'].iloc[-1]
    atr_value = df['ATR'].iloc[-1]
    adx_value = df['ADX'].iloc[-1]
    current_trend = df['Trend_MA'].iloc[-1]
    stop_loss, take_profit, trailing_stop = calculate_optimized_positions(adx_value, current_price, atr_value, symbol)
    signal_map = {1: 'LONG', -1: 'SHORT'}
    signal_type_str = signal_map[signal_type]
    chart_path = generate_technical_chart(df.tail(50), symbol)
    details = create_signal_details(df, symbol, signal_type_str)
    state.trailing_stops[symbol][current_time] = trailing_stop
    details.extend([
        "------------------------------------",
        "🔒 风险控制参数",
        f"当前ADX值：{adx_value:.2f} (趋势强度)",
        f"止损位：{stop_loss:.5f} (基于ADX动态调整)",
        f"止盈位：{take_profit:.5f} (基于ADX动态调整)",
        f"滑动止损位：{trailing_stop:.5f} (价格每上涨5%自动上移)",
        f"最大可承受亏损：{abs(current_price - stop_loss):.5f}",
        f"预期收益空间：{abs(take_profit - current_price):.5f}"
    ])
    # place_trade(
    #     symbol=symbol,
    #     usdt=20,
    #     direction=signal_type_str,
    #     leverage=10,
    #     take_profit=take_profit,
    #     stop_loss=stop_loss
    # )
    tuisong_zhibiao = str(get_konghuang())
    if send_notification(symbol, signal_type_str, details, chart_path, tuisong_zhibiao):
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time

def create_signal_details(df, symbol, signal_type):
    try:
        current_price = df['Close'].iloc[-1]
        volatility = state.market_volatility.get(symbol, 0.05)
        normalized_vol = volatility / 100
        params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
        stochastic_oversold = (df['%K'].iloc[-1] < 20) & (df['%D'].iloc[-1] < 20)
        stochastic_overbought = (df['%K'].iloc[-1] > 80) & (df['%D'].iloc[-1] > 80)
        cci_oversold = df['CCI'].iloc[-1] < -100
        cci_overbought = df['CCI'].iloc[-1] > 100
        vwap_below = current_price < df['VWAP'].iloc[-1]
        vwap_above = current_price > df['VWAP'].iloc[-1]
        adx_value = df['ADX'].iloc[-1]
        current_trend = df['Trend_MA'].iloc[-1]
        trend_strength = (df['MA20'].iloc[-1] - df['MA60'].iloc[-1]) / df['Close'].iloc[-1] * 100
        if signal_type == 'LONG':
            conditions = [
                ('MACD金叉', (df['DIF'].iloc[-1] < df['DEA'].iloc[-1]) and (df['DIF'].iloc[-2] < df['DEA'].iloc[-2])),
                ('布林下轨突破', current_price < df['Lower'].iloc[-1]),
                (f'RSI {df["RSI"].iloc[-1]:.1f} < {params["rsi_threshold_base"]}', df['RSI'].iloc[-1] < params["rsi_threshold_base"]),
                (f'成交量 {df["Volume"].iloc[-1]/1e7:.3f}千万 > {params["volume_threshold"]/1e7:.3f}千万', df['Volume'].iloc[-1] > params["volume_threshold"]),
                ('价格在VWAP下方', vwap_below),
                (f'ADX {adx_value:.1f} > {params["adx_threshold"]}', adx_value > params["adx_threshold"]),
                ('随机指标超卖', stochastic_oversold),
                ('五连阳', df['Five_Yang'].iloc[-1])
            ]
        else:
            conditions = [
                ('MACD死叉', (df['DIF'].iloc[-1] > df['DEA'].iloc[-1]) and (df['DIF'].iloc[-2] > df['DEA'].iloc[-2])),
                ('布林上轨突破', current_price > df['Upper'].iloc[-1]),
                (f'RSI {df["RSI"].iloc[-1]:.1f} > {100 - params["rsi_threshold_base"]}', df['RSI'].iloc[-1] > (100 - params["rsi_threshold_base"])),
                (f'成交量 {df["Volume"].iloc[-1]/1e7:.3f}千万 > {params["volume_threshold"]/1e7:.3f}千万', df['Volume'].iloc[-1] > params["volume_threshold"]),
                ('价格在VWAP上方', vwap_above),
                (f'ADX {adx_value:.1f} > {params["adx_threshold"]}', adx_value > params["adx_threshold"]),
                ('随机指标超买', stochastic_overbought),
                ('五连阴', df['Five_Yin'].iloc[-1])
            ]
        details = [
            f"触发时间：{datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}",
            f"当前价格：{current_price:.5f} USDT",
            "详细指标状态：",
            f"满足条件数：{sum(1 for cond in conditions if cond[1])}/{len(conditions)}",
            f"波动率系数：{DYNAMIC_PARAMS[symbol]['volatility_coeff']:.3f}",
            f"ATR值：{df['ATR'].iloc[-1]:.5f}, ATR波动率：{df['ATR_PCT'].iloc[-1]:.3f}%",
            "（etc高于50卖，低于10可买，btc高于500卖，低于150买）",
            "（上升波动加剧，警惕反转，下降适合区间交易）"
        ]
        details.extend([f"{cond[0]}：{'✓ 满足' if cond[1] else '✗ 不满足'}" for cond in conditions])
        details.extend([
            "------------------------------------",
            "📈 中线趋势分析",
            f"当前趋势：{'上涨' if current_trend else '下跌'} (数值：{current_trend})",
            f"趋势强度：{trend_strength:.3f}% | 20日均线：{df['MA20'].iloc[-1]:.5f} | 60日均线：{df['MA60'].iloc[-1]:.5f}"
        ])
        return details
    except Exception as e:
        logger.error(f"信号详情生成失败: {str(e)}", exc_info=True)
        return ["数据生成失败，请检查日志"]

def send_alert(message):
    send_notification(
        symbol="SYSTEM",
        signal_type="ALERT",
        details=[message],
        chart_path=None,
        tuisong_zhibiao=0
    )

def cleanup_system():
    try:
        for f in os.listdir(CONFIG['trading']['chart_dir']):
            file_path = os.path.join(CONFIG['trading']['chart_dir'], f)
            if os.path.isfile(file_path):
                os.remove(file_path)
        logger.info("系统资源已清理")
    except Exception as e:
        logger.error(f"资源清理失败: {str(e)}", exc_info=True)

def calculate_take_profit_success_rate(symbol):
    """计算止盈成功率"""
    try:
        # 获取完整历史数据
        start_time = int(datetime(2022, 1, 1).timestamp() * 1000)
        end_time = int(datetime.now().timestamp() * 1000)
        all_data = fetch_full_history(symbol, start_time, end_time)

        if all_data.empty:
            return 0.0

        # 初始化统计变量
        total_signals = 0
        successful_take_profits = 0
        active_signals = {}

        # 遍历历史数据
        for i in range(1, len(all_data)):
            current_row = all_data.iloc[i]
            current_time = all_data.index[i]

            # 检查信号
            signals = check_signals(current_row.to_frame().T, symbol)
            signal = signals['Signal'].iloc[-1]

            # 记录新信号
            if signal != 0:
                stop_loss, take_profit, _ = calculate_optimized_positions(
                    current_row['ADX'],
                    current_row['Close'],
                    current_row['ATR'],
                    symbol
                )

                active_signals[current_time] = {
                    'type': 'buy' if signal == 1 else 'sell',
                    'take_profit': take_profit,
                    'stop_loss': stop_loss,
                    'triggered': False
                }
                total_signals += 1

            # 检查现有信号是否触发
            for signal_time in list(active_signals.keys()):
                signal_data = active_signals[signal_time]
                if signal_data['triggered']:
                    continue

                # 检查止盈/止损触发
                if signal_data['type'] == 'buy':
                    if current_row['High'] >= signal_data['take_profit']:
                        successful_take_profits += 1
                        signal_data['triggered'] = True
                    elif current_row['Low'] <= signal_data['stop_loss']:
                        signal_data['triggered'] = True
                else:
                    if current_row['Low'] <= signal_data['take_profit']:
                        successful_take_profits += 1
                        signal_data['triggered'] = True
                    elif current_row['High'] >= signal_data['stop_loss']:
                        signal_data['triggered'] = True

        # 计算成功率
        return successful_take_profits / total_signals if total_signals > 0 else 0.0

    except Exception as e:
        logger.error(f"止盈成功率计算失败: {str(e)}", exc_info=True)
        return 0.0

def fetch_full_history(symbol, start_time, end_time):
    """获取完整历史数据"""
    data = []
    while start_time < end_time:
        # 每次获取200条数据
        batch = fetch_market_data(symbol,1000)
        if batch.empty:
            break
        data.append(batch)
        # 更新开始时间为最后一批数据的结束时间
        start_time = batch.index[-1].timestamp() * 1000 + 1
    return pd.concat(data) if data else pd.DataFrame()

if __name__ == "__main__":
    monitor_market()
    # calculate_take_profit_success_rate("BTCUSDT")