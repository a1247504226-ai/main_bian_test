import requests,smtplib,time,json,os,logging,traceback, pytz,shutil
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


# 配置日志系统（企业级增强版）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(module)s - %(message)s',
    handlers=[
        logging.FileHandler("short_term_monitor.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 时区配置（统一使用Asia/Shanghai）
tz_shanghai = pytz.timezone('Asia/Shanghai')

# Matplotlib配置（生产环境专用）
plt.switch_backend('Agg')
# 修改这部分代码
plt.rcParams.update({
    'font.family': 'SimHei',  # 使用文泉驿正黑
    'axes.unicode_minus': False,
    'savefig.dpi': 150,
    'figure.figsize': (16, 12)
})

# 系统配置参数（短线专用配置）
CONFIG = {
    'email': {
        'from': 'a1247504226@163.com',
        'to': '1247504226@qq.com',
        'password': 'XKCINXNMOMMDCAFI',
        'server': 'smtp.163.com',
        'port': 465,
        'max_retries': 3,
        'cooldown': timedelta(minutes=1)
    },
    'trading': {
        'symbols': ['BTCUSDT', 'ETHUSDT','GORKUSDT','DOGEUSDT'],
        'data_limit': 200,
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
        'atr_period': 7
    },
    'runtime': {
        'duration': timedelta(minutes=10),
        'check_interval': 10  # 每10秒检查一次剩余时间
    }
}

DYNAMIC_PARAMS = {
    'BTCUSDT': {
        'volume_threshold': 3e9,'volatility_coeff': 1.3,'rsi_threshold': 10,
        'rsi_sensitivity': 0.6,  # 新增波动敏感度
        'atr_period': 14,  # 默认BTC使用14周期
        'signal_cooldown': timedelta(minutes=31)
    },
    'ETHUSDT': {
        'volume_threshold': 1e9,  'volatility_coeff': 1.1, 'rsi_threshold': 10,  'rsi_sensitivity': 0.4,
        'atr_period': 10,  # ETH使用更敏感的10周期
        'signal_cooldown': timedelta(minutes=35)
    }  ,
    'GORKUSDT': {'volume_threshold': 5e5, 'volatility_coeff': 1.2, 'rsi_threshold': 10, 'rsi_sensitivity': 0.4,
                 'atr_period': 7 ,   # 小额币种使用7周期}
                 'signal_cooldown': timedelta(minutes=35)
                 }  ,
    'DOGEUSDT': {'volume_threshold': 1e8, 'volatility_coeff': 1.2, 'rsi_threshold': 10,  'rsi_sensitivity': 0.4,
                 'atr_period': 7 ,   # 小额币种使用7周期}
                 'signal_cooldown': timedelta(minutes=35)
                 }
}

# 全局状态管理器
class GlobalState:
    def __init__(self):
        self.market_data = defaultdict(pd.DataFrame)
        self.market_volatility = defaultdict(float)
        self.last_signals = {}
        self.last_emails = {}
        self.last_alerts = {}  # 警报冷却跟踪
        self.system_health = {'last_check': datetime.now(tz_shanghai)}

state = GlobalState()

def get_konghuang():
    try:
        konghuang=requests.get("https://blz.bicoin.com.cn/okexFutureData/open/getFearGreedIndex").json()
        print(konghuang)
        print(konghuang.get("data").get("today").get("value"))
        zhibiao=konghuang.get("data").get("today").get("value")
    except:
        zhibiao=0
    return zhibiao


# 系统初始化
def system_init():
    os.makedirs(CONFIG['trading']['chart_dir'], exist_ok=True)
    check_system_health()

#五连阳/五连阴
def identify_candle_pattern(df):
    """识别连续K线形态"""
    try:
        df = df.copy()

        # 连续阳线检测（收盘价>开盘价）
        df['Consecutive_Yang'] = (df['Close'] > df['Open']).astype(int)
        df['Five_Yang'] = (df['Consecutive_Yang'].rolling(5).sum() == 5).astype(bool)

        # 连续阴线检测（收盘价<开盘价）
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
    # print(data)
    if not data:
        send_alert(f"数据接口返回空数据：{symbol}")  # 新增数据缺失警报
        return pd.DataFrame()

    df = process_raw_data(data)

    if df.empty:
        # print("df")
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
        # 新增形态识别
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

# 指标计算模块（修复版）
def calculate_technical_indicators(df, symbol):
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()

    try:
        df['RSI'] = calculate_rsi(df['Close'], params['rsi_period']).fillna(50)
        df['DIF'], df['DEA'], df['MACD'] = calculate_macd(df['Close'], params['macd_fast'], params['macd_slow'], params['macd_signal'])
        df['Middle'], df['Upper'], df['Lower'] = calculate_bollinger(df['Close'], params['bollinger_period'], params['bollinger_std'])
        df['ATR'] = calculate_atr(df, DYNAMIC_PARAMS[symbol]['atr_period'])
        df['Momentum'] = df['Close'].pct_change(periods=3) * 100
        df['Volatility_Ratio'] = df['ATR'] / df['Close']

        # 新增指标计算
        df = calculate_stochastic(df)
        df = calculate_cci(df)
        df = calculate_vwap(df)
        df = calculate_atr_pct(df)

        # 在指标计算模块添加以下代码
        df['MA20'] = df['Close'].rolling(window=20).mean().round(5)
        df['MA50'] = df['Close'].rolling(window=50).mean().round(5)
        df['Trend'] = (df['MA20'] > df['MA50']).astype(int)
        df['Trend'] = df['Trend'].ffill().fillna(0).astype(int)  # 前向填充

        # 确保有足够数据计算均线
        if len(df) < 50:
            logger.warning(f"{symbol} 数据不足50根，无法计算MA50")
            return df
        df['MA50'] = df['Close'].rolling(window=50).mean()
        df['Trend'] = (df['MA20'] > df['MA50']).astype(int)  # 1=上涨趋势 0=下跌趋势

        return df
    except Exception as e:
        logger.error(f"指标计算失败({symbol}): {str(e)}", exc_info=True)
        return df

def calculate_rsi(series, period=14):
    try:
        delta = series.diff(1)  # 明确使用1阶差分
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        # 使用指数移动平均（更接近交易所计算方式）
        avg_gain = gain.ewm(com=period-1, adjust=False).mean()
        avg_loss = loss.ewm(com=period-1, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # 关键修改：显式对齐到前一根K线
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
        rolling_mean = series.rolling(window=period).mean()
        rolling_std = series.rolling(window=period).std()

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

# 修改后的信号检测模块
def check_signals(df, symbol):
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    df['Signal'] = 0
    current_price = df['Close'].iloc[-2]
    epsilon = 1e-5  # 对应5位小数精度
    # 新增列校验
    required_columns = ['Five_Yang', 'Five_Yin', 'DIF', 'DEA', 'RSI', 'Volume', 'Lower', 'Upper']
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        logger.error(f"缺失必要技术指标列: {missing}")
        return df

    try:
        current_volatility = state.market_volatility.get(symbol, 0.05)
        normalized_vol = current_volatility / 100  # 波动率归一化
        volatility_threshold = params['bollinger_std'] * (1 + normalized_vol * params['volatility_coeff'])
        rsi_threshold = max(10, params['rsi_threshold'] - normalized_vol * 10)
        volume_threshold = max(1e6, params['volume_threshold'] * (1 + normalized_vol * 0.5))

        # 新增指标判断条件
        # 随机震荡指标
        stochastic_overbought = df['%K'].iloc[-2] > 80 and df['%D'].iloc[-2] > 80
        stochastic_oversold = df['%K'].iloc[-2] < 20 and df['%D'].iloc[-2] < 20
        stochastic_cross = (df['%K'].iloc[-2] < df['%D'].iloc[-2]) and (df['%K'].iloc[-1] > df['%D'].iloc[-1])

        # 商品通道指数
        cci_overbought = df['CCI'].iloc[-2] > 100
        cci_oversold = df['CCI'].iloc[-2] < -100
        cci_trend = df['CCI'].iloc[-2] > 0  # 正值表示上涨趋势

        # VWAP验证
        vwap_above = current_price > df['VWAP'].iloc[-2]
        vwap_below = current_price < df['VWAP'].iloc[-2]

        # ATR百分比
        atr_pct = df['ATR_PCT'].iloc[-2]
        high_volatility = atr_pct > 2.5  # 波动率阈值

        # 新增：波动率触发条件
        current_atr_pct = df['ATR_PCT'].iloc[-2]
        volatility_threshold = params['volatility_coeff'] * 0.5  # 保守波动率阈值

        # 在 check_signals 函数中添加趋势变化检测
        prev_trend = df['Trend'].iloc[-2] if len(df) > 1 else -1
        current_trend = df['Trend'].iloc[-1]

        # 新增四个关键条件的布尔标记
        bollinger_breakout_buy = current_price < (df['Lower'].iloc[-2] - epsilon)

        # 新增相反条件标记
        bollinger_breakout_sell = current_price > (df['Upper'].iloc[-2] + epsilon)

        # 组合条件检测
        special_buy_combination = all([
            bollinger_breakout_buy,
            stochastic_oversold,
            cci_oversold,
            vwap_below
        ])

        special_sell_combination = all([
            bollinger_breakout_sell,
            stochastic_overbought,
            cci_overbought,
            vwap_above
        ])

        # 买入条件计数器
        buy_conditions = []
        buy_conditions.append(df['DIF'].iloc[-2] < df['DEA'].iloc[-2] and df['DIF'].iloc[-1] > df['DEA'].iloc[-1])
        buy_conditions.append(current_price < (df['Lower'].iloc[-2] - epsilon))
        buy_conditions.append(df['RSI'].iloc[-1] < rsi_threshold)
        buy_conditions.append(df['Volume'].iloc[-2] > volume_threshold)
        buy_conditions.append(current_atr_pct < volatility_threshold)        # 买入条件新增：低波动率环境
        buy_conditions.append(df['Five_Yang'].iloc[-2])  # 检测已完成的K线，五连阳

        # 修改后的买入条件
        buy_conditions.append(stochastic_oversold)
        buy_conditions.append(cci_oversold)
        buy_conditions.append(vwap_below)


        # 卖出条件计数器
        sell_conditions = []
        sell_conditions.append(df['DIF'].iloc[-2] > df['DEA'].iloc[-2] and df['DIF'].iloc[-1] < df['DEA'].iloc[-1])
        sell_conditions.append(current_price > (df['Upper'].iloc[-2] + epsilon))
        sell_conditions.append(df['RSI'].iloc[-1] > (105 - rsi_threshold))
        sell_conditions.append(df['Volume'].iloc[-2] > volume_threshold)
        sell_conditions.append(current_atr_pct > volatility_threshold * 3)         # 卖出条件新增：高波动率环境
        sell_conditions.append(df['Five_Yin'].iloc[-2])    # 检测已完成的K线，五连阴
        # 修改后的卖出条件
        sell_conditions.append(stochastic_overbought)
        sell_conditions.append(cci_overbought)
        sell_conditions.append(vwap_above)

        # 新增形态触发逻辑
        if df['RSI'].iloc[-1] < rsi_threshold:  # 检查当前rsi
            df.at[df.index[-1], 'Signal'] = 1
            logger.info(f"[{symbol}] 触发rsi买入")
        elif df['RSI'].iloc[-1] > (105 - rsi_threshold):  # 检查当前K线rsi
            df.at[df.index[-1], 'Signal'] = -1
            logger.info(f"[{symbol}] 触发rsi卖出")

        else:
            buy_count = sum(buy_conditions)
            sell_count = sum(sell_conditions)
            # 触发逻辑（排除特殊组合）
            if not special_buy_combination and buy_count >=4:
                df.at[df.index[-1], 'Signal'] = 1
            elif not special_sell_combination and sell_count >=4:
                df.at[df.index[-1], 'Signal'] = -1

        return df
    except Exception as e:
        logger.error(f"信号检测失败({symbol}): {str(e)}", exc_info=True)
        return df

# 可视化模块
def generate_technical_chart(df, symbol):
    try:
        if len(df) < 20:
            df = fetch_market_data(symbol, limit=50)
            if df.empty:
                return None

        current_time = datetime.now(tz_shanghai).strftime('%Y%m%d_%H%M%S')
        filename = f"{CONFIG['trading']['chart_dir']}/{symbol}_{current_time}.png"

        fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(16, 12))

        # 转换时间戳格式
        df_reset = df.reset_index()
        df_reset['Date'] = df_reset['Timestamp'].apply(mdates.date2num)
        ohlc = df_reset[['Date', 'Open', 'High', 'Low', 'Close']].values.tolist()

        # 绘制K线图
        candlestick_ohlc(ax1, ohlc, width=0.015, colorup='g', colordown='r', alpha=0.8)

        # 绘制布林带
        ax1.plot(df.index, df['Upper'], '--', color='crimson', label='上轨')
        ax1.plot(df.index, df['Middle'], '--', color='darkorange', label='中轨')
        ax1.plot(df.index, df['Lower'], '--', color='crimson', label='下轨')

        # 添加形态标记
        # 修改形态标记部分
        five_yang = df[df['Five_Yang']]
        five_yin = df[df['Five_Yin']]

        ax1.scatter(five_yang.index, five_yang['Close'],
                    marker='^', color='limegreen', s=120,
                    label='五连阳', edgecolors='black', linewidths=1)
        ax1.scatter(five_yin.index, five_yin['Close'],
                    marker='v', color='r', s=120,
                    label='五连阴', edgecolors='black', linewidths=1)


        ax1.xaxis_date()
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax1.legend(loc='upper left', fontsize=8)
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
def send_notification(symbol, signal_type, details, chart_path, tuisong_zhibiao):
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        msg['Subject'] = f"【中短线策略】尽量买涨__{symbol} {signal_type}信号触发,恐慌指标{tuisong_zhibiao}"

        # 修复后的邮件模板部分（关键修改行）
        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h3 style="color: #2c3e50;">{symbol} {signal_type}信号触发！恐慌指标{tuisong_zhibiao}</h3>
            <hr style="border: 0.5px solid #ecf0f1;">
            <p style="color: #34495e;">{'<br>'.join(details)}</p>
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

        # 警报冷却检查
        if signal_type == "ALERT":
            if (datetime.now(tz_shanghai) - state.last_alerts.get(symbol, datetime.min)) < CONFIG['email']['alert_cooldown']:
                logger.info(f"警报冷却中，跳过 {symbol} 通知")
                return False
            state.last_alerts[symbol] = datetime.now(tz_shanghai)  # 按币种存储

        with smtplib.SMTP_SSL(CONFIG['email']['server'], CONFIG['email']['port']) as server:
            for attempt in range(CONFIG['email']['max_retries']):
                try:
                    server.login(CONFIG['email']['from'], CONFIG['email']['password'])
                    server.sendmail(CONFIG['email']['from'], CONFIG['email']['to'], msg.as_string())
                    logger.info(f"{symbol}邮件发送成功，附加图表：{bool(chart_path)}")
                    return True
                except Exception as e:
                    logger.critical(f"邮件发送失败({symbol}) - 尝试 {attempt+1}/{CONFIG['email']['max_retries']}: {str(e)}")
                    time.sleep(15)
        return False
    except Exception as e:
        logger.error(f"邮件发送失败({symbol}): {str(e)}", exc_info=True)
        return False

# 风险管理模块
def update_volatility(symbol, df):
    try:
        returns = df['Close'].pct_change().dropna()
        if not returns.empty:
            volatility = returns.std() * np.sqrt(252)
            state.market_volatility[symbol] = round(float(volatility), 5)
    except Exception as e:
        logger.error(f"波动率更新失败({symbol}): {str(e)}", exc_info=True)

# 主控制流程
def monitor_market():
    try:
        logger.info("启动短线交易监控系统...")
        system_init()

        # 添加运行时间限制（1小时）
        start_time = datetime.now(tz_shanghai)
        runtime_limit = timedelta(hours=1)

        while (datetime.now(tz_shanghai) - start_time) < CONFIG['runtime']['duration']: # 修改循环条件
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

            # 优化休眠逻辑
            # cycle_time = (datetime.now(tz_shanghai) - cycle_start).total_seconds()
            # sleep_time = max(0, 120 - cycle_time)
            # time.sleep(sleep_time)
            time.sleep(CONFIG['runtime']['check_interval'])

        logger.info("已达到1小时运行时间，正常退出")
        cleanup_system()

    except KeyboardInterrupt:
        logger.info("\n监控被用户中断")
    except Exception as e:
        logger.critical(f"系统崩溃: {str(e)}", exc_info=True)
        send_alert(f"系统崩溃: {str(e)}")
    finally:
        cleanup_system()

def process_signal(symbol, signal_type, df):
    current_time = datetime.now(tz_shanghai)
    # 获取该币种的独立冷却时间
    cooldown = DYNAMIC_PARAMS[symbol]['signal_cooldown']

    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < cooldown:  # 使用独立冷却时间
            return
        # 新增：基于ATR的止盈止损计算
    current_price = df['Close'].iloc[-1]
    atr_value = df['ATR'].iloc[-1]
    atr_pct = df['ATR_PCT'].iloc[-1] / 100  # 转换为小数

    # 保守参数设置（可根据币种调整）
    stop_multiplier = 1.5  # 止损倍数
    take_multiplier = 2.0  # 止盈倍数

    signal_map = {1: '买入', -1: '卖出'}
    signal_type_str = signal_map[signal_type]

    chart_path = generate_technical_chart(df.tail(50), symbol)
    details = create_signal_details(df, symbol, signal_type_str)
    # 计算止损止盈位
    if signal_type_str == '买入':
        stop_loss = current_price * (1 - stop_multiplier * atr_pct)
        take_profit = current_price * (1 + take_multiplier * atr_pct)
    else:
        take_profit = current_price * (1 - take_multiplier * atr_pct)
        stop_loss = current_price * (1 + stop_multiplier * atr_pct)
    details.extend([
        f"------------------------------------",
        "🔒 风险控制参数",
        f"当前ATR值：{atr_value:.5f} (波动率{atr_pct*100:.3f}%)",
        f"保守止损位：{stop_loss:.5f} ({stop_multiplier}倍ATR{(stop_multiplier * atr_pct):.5f})",
        f"保守止盈位：{take_profit:.5f} ({take_multiplier}倍ATR{(take_multiplier * atr_pct):.5f})",
        f"最大可承受亏损：{abs(current_price - stop_loss):.5f}",  # 确保结果为正
        f"预期收益空间：{abs(take_profit - current_price):.5f}"   # 确保结果为正
    ])
    tuisong_zhibiao=str(get_konghuang())

    if send_notification(symbol, signal_type_str, details, chart_path,tuisong_zhibiao):
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time

def create_signal_details(df, symbol, signal_type):
    try:
        current_price = df['Close'].iloc[-1]
        volatility = state.market_volatility.get(symbol, 0.05)
        normalized_vol = volatility / 100

        # 从DataFrame中获取指标值（新增部分）
        params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
        stochastic_oversold = df['%K'].iloc[-2] < 20 and df['%D'].iloc[-2] < 20
        stochastic_overbought = df['%K'].iloc[-2] > 80 and df['%D'].iloc[-2] > 80
        cci_oversold = df['CCI'].iloc[-2] < -100
        cci_overbought = df['CCI'].iloc[-2] > 100
        vwap_below = current_price < df['VWAP'].iloc[-2]
        vwap_above = current_price > df['VWAP'].iloc[-2]
        high_volatility = df['ATR_PCT'].iloc[-2]
        current_trend = "上涨趋势" if df['Trend'].iloc[-1] else "下跌趋势"
        trend_strength = (df['MA20'].iloc[-1] - df['MA50'].iloc[-1]) / df['Close'].iloc[-1] * 100
        prev_trend = df['Trend'].iloc[-2] if len(df) > 1 else -1
        current_trend = df['Trend'].iloc[-1]
        epsilon = 1e-5  # 对应5位小数精度
        rsi_threshold = max(10, params['rsi_threshold'] - normalized_vol * 10)
        volume_threshold = max(1e6, params['volume_threshold'] * (1 + normalized_vol * 0.5))
        # 新增：波动率触发条件
        current_atr_pct = df['ATR_PCT'].iloc[-2]
        volatility_threshold = params['volatility_coeff'] * 0.5  # 保守波动率阈值

        # 完整条件列表（包含新增加的K线形态条件）
        if signal_type == '买入':
            conditions = [
                ('MACD金叉', df['DIF'].iloc[-2] < df['DEA'].iloc[-2] and df['DIF'].iloc[-1] > df['DEA'].iloc[-1]),
                ('布林下轨突破', df['Close'].iloc[-2] < (df['Lower'].iloc[-2] -epsilon )),
                (f'实际值{df["RSI"].iloc[-1]:.1f} ,上次值{df["RSI"].iloc[-2]:.1f} ， RSI < {rsi_threshold:.1f}', df['RSI'].iloc[-1] < rsi_threshold),
                (f'最近成交量实际值{df["Volume"].iloc[-2]/1e7:.3f}千万,成交量 > {DYNAMIC_PARAMS[symbol]["volume_threshold"]/1e7:.3f}千万', df['Volume'].iloc[-2] > volume_threshold),
                (f'低波动率买入：{current_atr_pct:.3f}', current_atr_pct < volatility_threshold),
                ('五连阳：', df['Five_Yang'].iloc[-2]),
                ('随机指标超卖区', stochastic_oversold),
                ('CCI超卖', cci_oversold),
                ('价格在VWAP下方', vwap_below)
            ]
        else:
            conditions = [
                ('MACD死叉', df['DIF'].iloc[-2] > df['DEA'].iloc[-2] and df['DIF'].iloc[-1] < df['DEA'].iloc[-1]),
                ('布林上轨突破', df['Close'].iloc[-2] > (df['Upper'].iloc[-2] + epsilon)),
                (f'实际值{df["RSI"].iloc[-1]:.1f} , 上次值{df["RSI"].iloc[-2]:.1f} ， RSI > {105 - rsi_threshold:.1f}', df['RSI'].iloc[-1] > (105- rsi_threshold)),
                (f'最近成交量实际值{df["Volume"].iloc[-2]/1e7:.3f}千万,成交量 > {DYNAMIC_PARAMS[symbol]["volume_threshold"]/1e7:.3f}千万', df['Volume'].iloc[-2] > volume_threshold),
                (f'高波动率卖出：{current_atr_pct:.3f}', current_atr_pct > volatility_threshold * 3),
                ('五连阴：', df['Five_Yin'].iloc[-2]),
                ('随机指标超买区', stochastic_overbought),
                ('CCI超买', cci_overbought),
                ('价格在VWAP上方', vwap_above)
            ]

        # 生成带状态的详细报告
        details = [
            f"触发时间：{datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}",
            f"当前价格：{current_price:.5f} USDT",
            "详细指标状态：",
            f"满足条件数：{sum(1 for cond in conditions if cond[1])}/{len(conditions)}",
            f"波动率系数（越高风险越大，越小适合长线）：{DYNAMIC_PARAMS[symbol]['volatility_coeff']:.3f} ",
            f"ATR值：{df['ATR'].iloc[-1]:.5f} ,ATR波动率：{df['ATR_PCT'].iloc[-2]:.3f}",
            "（etc高于50卖，低于10可买，btc高于500卖，低于150买）",
            "（上升波动加剧，警惕反转，下降适合区间交易）",
            *[f"{cond[0]}：{'✓ 满足' if cond[1] else '✗ 不满足'}" for cond in conditions]
        ]
        # 在趋势分析部分增加数值显示
        current_trend_value = df['Trend'].iloc[-1]
        current_trend_text = "上涨趋势" if current_trend_value else "下跌趋势"
        details.extend([
            "------------------------------------",
            "📈 中线趋势分析",
            f"当前趋势：{current_trend_text} (数值：{current_trend_value})",  # 同时显示文字和数值
            f"趋势强度：{trend_strength:.3f}% | 20日均线：{df['MA20'].iloc[-1]:.5f} | 50日均线：{df['MA50'].iloc[-1]:.5f}"
        ])
        # 当趋势发生改变时记录
        if prev_trend != current_trend and prev_trend != -1:
            trend_change = "转涨" if current_trend else "转跌"
            details.append(f"⚠️ 趋势变化：{trend_change}（MA20与MA50{('金叉' if current_trend else '死叉')}）")

        return details
    except Exception as e:
        logger.error(f"信号详情生成失败: {str(e)}", exc_info=True)
        return ["数据生成失败，请检查日志"]

def send_alert(message):
    send_notification(
        symbol="SYSTEM",
        signal_type="ALERT",
        details=[message],
        chart_path=None
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

if __name__ == "__main__":
    monitor_market()