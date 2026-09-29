import requests
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import time
import json
import os
import logging
import traceback
from collections import defaultdict
import pytz
import shutil
import matplotlib.patches as patches
# from mpl_finance import candlestick_ohlc
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
    'font.family': 'WenQuanYi Zen Hei',  # 使用文泉驿正黑
    'axes.unicode_minus': False,
    'savefig.dpi': 150,
    'figure.figsize': (16, 12)
})

# 系统配置参数（短线专用配置）
CONFIG = {
    'email': {
        'from': 'jack.han@sx.xyz',
        'to': '1247504226@qq.com',
        'password': 'v v f w g q g e j u t w i f k m',  # 保留原始密码格式
        'server': 'smtp.gmail.com',
        'port': 465,
        'max_retries': 3,
        'cooldown': timedelta(minutes=1),
        'alert_cooldown': timedelta(minutes=5)  # 警报专用冷却时间
    },
    'trading': {
        'symbols': ['BTCUSDT', 'ETHUSDT'],
        'data_limit': 200,
        'chart_dir': "short_term_charts",
        'volatility_window': 15,
        'signal_cooldown': timedelta(minutes=30),
        'missing_data_threshold': 0.9
    },
    'indicators': {
        'rsi_period': 9,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'bollinger_period': 10,
        'bollinger_std': 1.8,
        'atr_period': 7
    },
    'runtime': {
    'duration': timedelta(hours=1),
    'check_interval': 10  # 每10秒检查一次剩余时间
}
}

# 动态交易参数配置（短线优化）
DYNAMIC_PARAMS = {
    'BTCUSDT': {'volume_threshold': 7e7, 'volatility_coeff': 1.3, 'rsi_threshold': 25},
    'ETHUSDT': {'volume_threshold': 8e7, 'volatility_coeff': 1.2, 'rsi_threshold': 20}
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

# 新增K线形态识别函数
def identify_candle_pattern(df):
    """识别光头阳线和光脚阴线"""
    try:
        df = df.copy()
        tolerance = 1e-8  # 根据实际数据精度调整
        
        # 确保必要列存在
        required_cols = ['Open', 'High', 'Low', 'Close']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"缺失必要列: {col}")

        # 光头阳线：收盘价等于最高价且收盘价大于开盘价
        df['Is_Bald_Yang'] = (df['Close'] > df['Open']) & (abs(df['High'] - df['Close']) < tolerance)

        # 光脚阴线：收盘价等于最低价且收盘价小于开盘价
        df['Is_Barefoot_Yin'] = (df['Close'] < df['Open']) & (abs(df['Low'] - df['Close']) < tolerance)

        return df
    except Exception as e:
        logger.error(f"K线形态识别失败: {str(e)}", exc_info=True)
        # 确保列存在（即使识别失败）
        df['Is_Bald_Yang'] = False
        df['Is_Barefoot_Yin'] = False
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

# 数据获取模块（增强版）
def fetch_market_data(symbol, limit=CONFIG['trading']['data_limit']):
    try:
        endpoint = f'/api/v3/klines?symbol={symbol}&interval=30m&limit={limit}'
        url = 'https://api.binance.com' + endpoint
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        data = response.json()
        if not data:
            send_alert(f"数据接口返回空数据：{symbol}")  # 新增数据缺失警报
            return pd.DataFrame()

        df = process_raw_data(data)
        if df.empty:
            return df

        df = enhance_data_quality(df, symbol)
        return df
    except Exception as e:
        logger.error(f"数据获取失败({symbol}): {str(e)}", exc_info=True)
        return pd.DataFrame()

def process_raw_data(raw_data):
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
        df['RSI'] = calculate_rsi(df['Close'], params['rsi_period'])
        df['DIF'], df['DEA'], df['MACD'] = calculate_macd(df['Close'], params['macd_fast'], params['macd_slow'], params['macd_signal'])
        df['Middle'], df['Upper'], df['Lower'] = calculate_bollinger(df['Close'], params['bollinger_period'], params['bollinger_std'])
        df['ATR'] = calculate_atr(df, params['atr_period'])
        df['Momentum'] = df['Close'].pct_change(periods=3) * 100
        df['Volatility_Ratio'] = df['ATR'] / df['Close']
        return df
    except Exception as e:
        logger.error(f"指标计算失败({symbol}): {str(e)}", exc_info=True)
        return df

def calculate_rsi(series, period=14):
    try:
        series = series.dropna().astype(float)
        delta = series.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        avg_gain = gain.ewm(com=period-1, adjust=False).mean()
        avg_loss = loss.ewm(com=period-1, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-10)
        return (100 - (100 / (1 + rs))).clip(0, 100)
    except Exception as e:
        logger.error(f"RSI计算失败: {str(e)}", exc_info=True)
        return pd.Series([50]*len(series), index=series.index)

def calculate_macd(series, fast=12, slow=26, signal=9):
    try:
        series = series.dropna().astype(float)
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=signal, adjust=False).mean()
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
        return atr.bfill()
    except Exception as e:
        logger.error(f"ATR计算失败: {str(e)}", exc_info=True)
        return pd.Series([0]*len(df), index=df.index)

# 修改后的信号检测模块
def check_signals(df, symbol):
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    df['Signal'] = 0
    current_price = df['Close'].iloc[-1]
    # 新增列校验
    required_columns = ['Is_Bald_Yang', 'Is_Barefoot_Yin', 'DIF', 'DEA', 'RSI', 'Volume', 'Lower', 'Upper']
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

        # 买入条件计数器
        buy_conditions = []
        buy_conditions.append(df['DIF'].iloc[-2] < df['DEA'].iloc[-2] and df['DIF'].iloc[-1] > df['DEA'].iloc[-1])
        buy_conditions.append(current_price < df['Lower'].iloc[-1])
        buy_conditions.append(df['RSI'].iloc[-1] < rsi_threshold)
        buy_conditions.append(df['Volume'].iloc[-1] > volume_threshold)
        # buy_conditions.append(df['Is_Bald_Yang'].iloc[-2])  # 检测已完成的K线，光头阳线
        buy_conditions.append(df['Is_Bald_Yang'].shift(1).iloc[-2] if len(df) > 1 else False)



        # 卖出条件计数器
        sell_conditions = []
        sell_conditions.append(df['DIF'].iloc[-2] > df['DEA'].iloc[-2] and df['DIF'].iloc[-1] < df['DEA'].iloc[-1])
        sell_conditions.append(current_price > df['Upper'].iloc[-1])
        sell_conditions.append(df['RSI'].iloc[-1] > (100 - rsi_threshold))
        sell_conditions.append(df['Volume'].iloc[-1] > volume_threshold)
        # sell_conditions.append(df['Is_Barefoot_Yin'].iloc[-2])    # 检测已完成的K线，新增光脚阴线条件
        sell_conditions.append(df['Is_Barefoot_Yin'].shift(1).iloc[-2] if len(df) > 1 else False)

        buy_count = sum(buy_conditions)
        sell_count = sum(sell_conditions)

        if buy_count >= 2:
            df.at[df.index[-1], 'Signal'] = 1
            logger.info(f"[{symbol}] 触发买入信号（满足{buy_count}个条件）")
        elif sell_count >= 2:
            df.at[df.index[-1], 'Signal'] = -1
            logger.info(f"[{symbol}] 触发卖出信号（满足{sell_count}个条件）")

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
        bald_yang = df[df['Is_Bald_Yang']]
        barefoot_yin = df[df['Is_Barefoot_Yin']]
        ax1.scatter(bald_yang.index, bald_yang['Close'], 
                   marker='^', color='limegreen', s=120, 
                   label='光头阳线', edgecolors='black', linewidths=1)
        ax1.scatter(barefoot_yin.index, barefoot_yin['Close'], 
                   marker='v', color='r', s=120, 
                   label='光脚阴线', edgecolors='black', linewidths=1)
        
        ax1.xaxis_date()
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax1.legend(loc='upper left', fontsize=8)
        ax1.set_title(f'{symbol} 价格通道与K线形态分析（短线）')

        ax2.plot(df.index, df['RSI'], label='RSI', color='purple')
        ax2.axhline(80, color='red', linestyle='--', label='超买')
        ax2.axhline(20, color='green', linestyle='--', label='超卖')
        ax2.legend(loc='upper left')

        ax3.plot(df.index, df['DIF'], label='DIF', color='blue')
        ax3.plot(df.index, df['DEA'], label='DEA', color='orange')
        ax3.bar(df.index, df['MACD'], label='MACD柱', color='gray', alpha=0.5)
        ax3.axhline(0, color='black', linestyle='-', linewidth=0.5)
        ax3.legend(loc='upper left')

        ax4.plot(df.index, df['Volume']/1e8, label='成交量(亿)', color='brown')
        ax4.legend(loc='upper left')

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
        msg['Subject'] = f"【短线策略警报】{symbol} {signal_type}信号触发,恐慌指标{tuisong_zhibiao}"
 
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
            state.last_alerts[symbol] = datetime.now(tz_shanghai)

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
            state.market_volatility[symbol] = round(float(volatility), 4)
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

    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < CONFIG['trading']['signal_cooldown']:
            return

    signal_map = {1: '买入', -1: '卖出'}
    signal_type_str = signal_map[signal_type]

    chart_path = generate_technical_chart(df.tail(50), symbol)
    details = create_signal_details(df, symbol, signal_type_str)
    tuisong_zhibiao=str(get_konghuang())

    if send_notification(symbol, signal_type_str, details, chart_path,tuisong_zhibiao):
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time

def create_signal_details(df, symbol, signal_type):
    try:
        current_price = df['Close'].iloc[-1]
        volatility = state.market_volatility.get(symbol, 0.05)
        normalized_vol = volatility / 100
        rsi_threshold = DYNAMIC_PARAMS[symbol]['rsi_threshold'] + (normalized_vol * 15)

        # 完整条件列表（包含新增加的K线形态条件）
        if signal_type == '买入':
            conditions = [
                ('MACD金叉', df['DIF'].iloc[-2] < df['DEA'].iloc[-2] and df['DIF'].iloc[-1] > df['DEA'].iloc[-1]),
                ('布林下轨突破', df['Close'].iloc[-1] < df['Lower'].iloc[-1]),
                (f'RSI < {rsi_threshold:.1f}', df['RSI'].iloc[-1] < rsi_threshold),
                (f'成交量 > {DYNAMIC_PARAMS[symbol]["volume_threshold"]/1e8:.1f}亿', df['Volume'].iloc[-1] > DYNAMIC_PARAMS[symbol]['volume_threshold']),
                ('光头阳线', df['Is_Bald_Yang'].iloc[-1])
            ]
        else:
            conditions = [
                ('MACD死叉', df['DIF'].iloc[-2] > df['DEA'].iloc[-2] and df['DIF'].iloc[-1] < df['DEA'].iloc[-1]),
                ('布林上轨突破', df['Close'].iloc[-1] > df['Upper'].iloc[-1]),
                (f'RSI > {100 - rsi_threshold:.1f}', df['RSI'].iloc[-1] > (100 - rsi_threshold)),
                (f'成交量 > {DYNAMIC_PARAMS[symbol]["volume_threshold"]/1e8:.1f}亿', df['Volume'].iloc[-1] > DYNAMIC_PARAMS[symbol]['volume_threshold']),
                ('光脚阴线', df['Is_Barefoot_Yin'].iloc[-1])
            ]

        # 生成带状态的详细报告
        details = [
            f"触发时间：{datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}",
            f"当前价格：{current_price:.2f} USDT",
            "详细指标状态：",
            f"满足条件数：{sum(1 for cond in conditions if cond[1])}/{len(conditions)}",
            f"波动率系数（越高风险越大，越小适合长线）：{DYNAMIC_PARAMS[symbol]['volatility_coeff']}",
            f"ATR值：{df['ATR'].iloc[-1]:.4f}",
            "（etc高于30卖，低于10可买，btc高于300卖，低于150买）",
            "（上升波动加剧，警惕反转，下降适合区间交易）",
            *[f"{cond[0]}：{'✓ 满足' if cond[1] else '✗ 不满足'}" for cond in conditions]
        ]
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
    # monitor_market()
    monitor_market()
    

    