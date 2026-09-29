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

# 配置日志系统（企业级增强版）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(module)s - %(message)s',
    handlers=[
        logging.FileHandler("mid_term_monitor.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 时区配置（统一使用Asia/Shanghai）
tz_shanghai = pytz.timezone('Asia/Shanghai')

# Matplotlib配置（生产环境专用）
plt.switch_backend('Agg')
plt.rcParams.update({
    'font.family': 'SimHei',
    'axes.unicode_minus': False,
    'savefig.dpi': 150,
    'figure.figsize': (16, 9)
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
        'cooldown': timedelta(minutes=1)
    },
    'trading': {
        'symbols': ['BTCUSDT', 'ETHUSDT'],
        'data_limit': 200,
        'chart_dir': "mid_term_charts",
        'volatility_window': 30,
        'signal_cooldown': timedelta(minutes=1),
        'missing_data_threshold': 0.95
    },
    'indicators': {
        'rsi_period': 14,
        'macd_fast': 24,
        'macd_slow': 52,
        'macd_signal': 18,
        'bollinger_period': 20,
        'bollinger_std': 2.0,
        'atr_period': 14
    }
}

# 动态交易参数配置
DYNAMIC_PARAMS = {
    'BTCUSDT': {'volume_threshold': 10e8, 'volatility_coeff': 1.2, 'rsi_threshold': 35},
    'ETHUSDT': {'volume_threshold': 5e8, 'volatility_coeff': 1.1, 'rsi_threshold': 30}
}

# 全局状态管理器
class GlobalState:
    def __init__(self):
        self.market_data = defaultdict(pd.DataFrame)
        self.market_volatility = defaultdict(float)
        self.last_signals = {}
        self.last_emails = {}
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

def check_system_health():
    """系统健康检查"""
    try:
        free_space = shutil.disk_usage('./').free / (1024**3)
        if free_space < 5:
            send_alert(f"系统警告：磁盘空间不足，剩余 {free_space:.1f}GB")

        if (datetime.now(tz_shanghai) - state.system_health['last_check']).seconds > 3600:
            send_alert("系统健康检查通过")
            state.system_health['last_check'] = datetime.now(tz_shanghai)
    except Exception as e:
        logger.error(f"健康检查失败: {str(e)}", exc_info=True)

def fetch_market_data(symbol, limit=CONFIG['trading']['data_limit']):
    endpoint = f'api/v2/mix/market/candles?symbol={symbol}&granularity=1D&limit={limit}&productType=usdt-futures'
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
    """原始数据处理（强制时区转换）"""
    try:
        cleaned = []
        for bar in raw_data:
            if len(bar) >= 6:
                try:
                    numeric_bar = [
                        float(bar[1]), float(bar[2]), float(bar[3]),
                        float(bar[4]), float(bar[5]), int(bar[0])
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
    """数据增强处理"""
    try:
        # 异常值处理
        df['Close'] = df['Close'].where(df['Close'] > 1e-8).ffill()
        df['Volume'] = df['Volume'].where(df['Volume'] > 1e-6).ffill()

        # 插值处理
        for col in ['Close', 'Volume']:
            df[col] = df[col].interpolate(method='time').bfill()
          #    df[col] = df[col].interpolate(method='time').fillna(method='bfill')

        # 价格边界检查
        if df['Close'].iloc[-1] < 1e-8:
            logger.warning(f"{symbol} 价格异常，使用前一个值")
            df['Close'].iloc[-1] = df['Close'].iloc[-2]

        return df
    except Exception as e:
        logger.error(f"数据增强失败({symbol}): {str(e)}", exc_info=True)
        return df

# 指标计算模块
def calculate_technical_indicators(df, symbol):
    """技术指标计算引擎"""
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()

    try:
        # 基础指标
        df['RSI'] = calculate_rsi(df['Close'], params['rsi_period'])
        df['DIF'], df['DEA'], df['MACD'] = calculate_macd(df['Close'], params['macd_fast'], params['macd_slow'], params['macd_signal'])
        df['Middle'], df['Upper'], df['Lower'] = calculate_bollinger(df['Close'], params['bollinger_period'], params['bollinger_std'])
        df['ATR'] = calculate_atr(df, params['atr_period'])

        # 衍生指标
        df['Volatility_Band'] = df['Middle'] * (1 + state.market_volatility.get(symbol, 0.05) * 0.6)
        df['Price_Position'] = (df['Close'] - df['Middle']) / (df['Upper'] - df['Lower'])

        return df
    except Exception as e:
        logger.error(f"指标计算失败({symbol}): {str(e)}", exc_info=True)
        return df

def calculate_rsi(series, period=14):
    """稳健型RSI计算"""
    try:
        series = series.dropna().astype(float)
        delta = series.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period-1, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-10)
        return (100 - (100 / (1 + rs))).clip(0, 100)
    except Exception as e:
        logger.error(f"RSI计算失败: {str(e)}", exc_info=True)
        return pd.Series([50]*len(series), index=series.index)

def calculate_macd(series, fast=24, slow=52, signal=18):
    """生产级MACD计算"""
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
    """布林带计算函数"""
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
    """增强型ATR计算（修复版）"""
    try:
        df = df.copy()
        # 计算真实波动范围（TR）
        df['High_Low'] = df['High'] - df['Low']
        df['High_Close'] = abs(df['High'] - df['Close'].shift(1))
        df['Low_Close'] = abs(df['Low'] - df['Close'].shift(1))
        df['TR'] = df[['High_Low', 'High_Close', 'Low_Close']].max(axis=1)

        # 计算ATR
        atr = df['TR'].rolling(period).mean()
        return atr.bfill()
    except Exception as e:
        logger.error(f"ATR计算失败: {str(e)}", exc_info=True)
        return pd.Series([0]*len(df), index=df.index)

# 信号检测模块
def detect_trading_signals(df, symbol):
    """智能信号检测引擎（任意两个条件触发版）"""
    params = {**CONFIG['indicators'], **DYNAMIC_PARAMS.get(symbol, {})}
    df = df.copy()
    df['Signal'] = 0

    try:
        current_volatility = state.market_volatility.get(symbol, 0.05)
        volatility_threshold = params['bollinger_std'] * (1 + current_volatility * params['volatility_coeff'])

        # 动态阈值计算
        rsi_threshold = params['rsi_threshold'] + (current_volatility * 12)
        volume_threshold = params['volume_threshold'] * (1 + current_volatility)

        for i in range(1, len(df)):
            if i < params['macd_signal'] + 1:
                continue

            if df.iloc[i].isnull().any():
                continue

            # 买入条件集合
            buy_conditions = [
                # 条件1：RSI超卖
                df['RSI'].iloc[i] < rsi_threshold,
                # 条件2：MACD金叉
                df['DIF'].iloc[i] > df['DEA'].iloc[i] and df['DIF'].iloc[i-1] <= df['DEA'].iloc[i-1],
                # 条件3：价格突破下轨
                df['Close'].iloc[i] < df['Lower'].iloc[i] - volatility_threshold * df['ATR'].iloc[i],
                # 条件4：成交量放大
                df['Volume'].iloc[i] > volume_threshold
            ]

            # 卖出条件集合
            sell_conditions = [
                # 条件1：RSI超买
                df['RSI'].iloc[i] > (100 - rsi_threshold),
                # 条件2：MACD死叉
                df['DIF'].iloc[i] < df['DEA'].iloc[i] and df['DIF'].iloc[i-1] >= df['DEA'].iloc[i-1],
                # 条件3：价格突破上轨
                df['Close'].iloc[i] > df['Upper'].iloc[i] + volatility_threshold * df['ATR'].iloc[i],
                # 条件4：成交量放大
                df['Volume'].iloc[i] > volume_threshold
            ]

            # 统计满足条件数
            buy_count = sum(buy_conditions)
            sell_count = sum(sell_conditions)

            # 信号触发逻辑（满足任意两个条件）
            if buy_count >= 2 :
                df.at[df.index[i], 'Signal'] = 1
            elif sell_count >= 2 :
                df.at[df.index[i], 'Signal'] = -1

        return df
    except Exception as e:
        logger.error(f"信号检测失败({symbol}): {str(e)}", exc_info=True)
        return df

# 可视化模块
def generate_technical_chart(df, symbol):
    """专业级图表生成"""
    try:
        if len(df) < 20:
            logger.warning(f"{symbol}数据不足，生成扩展时段图表")
            df = fetch_market_data(symbol, limit=50)
            if df.empty:
                return None

        current_time = datetime.now(tz_shanghai).strftime('%Y%m%d_%H%M%S')
        filename = f"{CONFIG['trading']['chart_dir']}/{symbol}_{current_time}.png"

        fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(16, 12))

        # 价格通道
        ax1.plot(df.index, df['Close'], label='收盘价', color='navy')
        ax1.plot(df.index, df['Upper'], '--', color='crimson', label='上轨')
        ax1.plot(df.index, df['Middle'], '--', color='darkorange', label='中轨')
        ax1.plot(df.index, df['Lower'], '--', color='crimson', label='下轨')
        ax1.legend(loc='upper left')
        ax1.set_title(f'{symbol} 价格通道分析（中期）')

        # RSI指标
        ax2.plot(df.index, df['RSI'], label='RSI', color='purple')
        ax2.axhline(80, color='red', linestyle='--', label='超买')
        ax2.axhline(20, color='green', linestyle='--', label='超卖')
        ax2.legend(loc='upper left')

        # MACD指标
        ax3.plot(df.index, df['DIF'], label='DIF', color='blue')
        ax3.plot(df.index, df['DEA'], label='DEA', color='orange')
        ax3.bar(df.index, df['MACD'], label='MACD柱', color='gray', alpha=0.5)
        ax3.axhline(0, color='black', linestyle='-', linewidth=0.5)
        ax3.legend(loc='upper left')

        # 成交量分析
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
def send_notification(symbol, signal_type, details, chart_path):
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

# 风险管理模块
def update_volatility(symbol, df):
    """动态波动率更新"""
    try:
        returns = df['Close'].pct_change().dropna()
        if not returns.empty:
            volatility = returns.std() * np.sqrt(252)
            state.market_volatility[symbol] = round(float(volatility), 4)
    except Exception as e:
        logger.error(f"波动率更新失败({symbol}): {str(e)}", exc_info=True)

# 主控制流程
def monitor_market():
    """主交易监控循环"""
    try:
        logger.info("启动中期交易监控系统...")
        system_init()
        for i in [3]:
            start_time = datetime.now(tz_shanghai)
            for symbol in CONFIG['trading']['symbols']:
                try:
                    logger.info(f"处理{symbol}数据...")
                    df = fetch_market_data(symbol)

                    if df.empty:
                        continue

                    # 更新波动率
                    update_volatility(symbol, df)

                    # 计算指标
                    df = calculate_technical_indicators(df, symbol)

                    # 检测信号
                    df = detect_trading_signals(df, symbol)

                    # 处理最新信号
                    latest_signal = df['Signal'].iloc[-1]
                    if latest_signal != 0:
                        process_signal(symbol, latest_signal, df)

                except Exception as e:
                    logger.error(f"主流程异常({symbol}): {str(e)}", exc_info=True)

            # # 控制循环频率（中期策略）
            # elapsed = datetime.now(tz_shanghai) - start_time
            # sleep_time = max(0, 600 - elapsed.seconds)  # 每10分钟执行一次
            time.sleep(120)

    except KeyboardInterrupt:
        logger.info("\n监控被用户中断")
    except Exception as e:
        logger.critical(f"系统崩溃: {str(e)}", exc_info=True)
        send_alert(f"系统崩溃: {str(e)}")
    finally:
        cleanup_system()

def process_signal(symbol, signal_type, df):
    """信号处理中心"""
    current_time = datetime.now(tz_shanghai)

    # 冷却时间检查（使用带时区的时间对象）
    if symbol in state.last_signals:
        last_signal_time = state.last_signals[symbol]
        if (current_time - last_signal_time) < CONFIG['trading']['signal_cooldown']:
            return

    signal_map = {1: '买入', -1: '卖出'}
    signal_type_str = signal_map[signal_type]

    # 生成图表
    chart_path = generate_technical_chart(df.tail(50), symbol)

    # 创建信号详情
    details = create_signal_details(df, symbol, signal_type_str)

    # 发送通知
    if send_notification(symbol, signal_type_str, details, chart_path):
        state.last_signals[symbol] = current_time
        state.last_emails[symbol] = current_time
    else:
        logger.info("没有检测到触发信号")

def create_signal_details(df, symbol, signal_type):
    """信号详情生成器"""
    try:
        current_price = df['Close'].iloc[-1]
        volatility = state.market_volatility.get(symbol, 0.05)
        rsi_threshold = DYNAMIC_PARAMS[symbol]['rsi_threshold'] + (volatility * 12)

        # 获取触发条件详情
        triggered = []
        if signal_type == '买入':
            conditions = [
                ('RSI超卖', df['RSI'].iloc[-1] < rsi_threshold),
                ('MACD金叉', df['DIF'].iloc[-1] > df['DEA'].iloc[-1] and df['DIF'].iloc[-2] <= df['DEA'].iloc[-2]),
                ('价格突破下轨', df['Close'].iloc[-1] < df['Lower'].iloc[-1]),
                ('成交量放大', df['Volume'].iloc[-1] > DYNAMIC_PARAMS[symbol]['volume_threshold'])
            ]
        else:
            conditions = [
                ('RSI超买', df['RSI'].iloc[-1] > (100 - rsi_threshold)),
                ('MACD死叉', df['DIF'].iloc[-1] < df['DEA'].iloc[-1] and df['DIF'].iloc[-2] >= df['DEA'].iloc[-2]),
                ('价格突破上轨', df['Close'].iloc[-1] > df['Upper'].iloc[-1]),
                ('成交量放大', df['Volume'].iloc[-1] > DYNAMIC_PARAMS[symbol]['volume_threshold'])
            ]

        triggered = [label for label, cond in conditions if cond]

        details = [
            f"触发时间：{datetime.now(tz_shanghai).strftime('%Y-%m-%d %H:%M:%S')}",
            f"当前价格：{current_price:.2f} USDT",
            f"触发条件（{len(triggered)}/4）：{', '.join(triggered)}",
            f"波动率系数：{DYNAMIC_PARAMS[symbol]['volatility_coeff']}",
            f"ATR值：{df['ATR'].iloc[-1]:.4f}"
        ]
        return details
    except Exception as e:
        logger.error(f"信号详情生成失败: {str(e)}", exc_info=True)
        return ["数据生成失败，请检查日志"]

def send_alert(message):
    """系统警报通知"""
    send_notification(
        symbol="SYSTEM",
        signal_type="ALERT",
        details=[message],
        chart_path=None
    )

def cleanup_system():
    """系统清理"""
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