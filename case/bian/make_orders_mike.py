import requests
import json
import time
from datetime import datetime, timedelta
import pytz
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
import hmac
import hashlib
import base64

# 配置部分
CONFIG = {
    'email': {
        'from': 'jack.han@sx.xyz',
        'to': '1247504226@qq.com',
        'password': 'v v f w g q g e j u t w i f k m',
        'server': 'smtp.gmail.com',
        'port': 465,
        'max_retries': 3,
        'cooldown': timedelta(minutes=1)
    }
}

API_CONFIGS = [
    {
        "url": "https://capi.websea.com/webApi/follow/trader-operation?trader=485142&pageNo=1&pageSize=10",
        "webhook_key": "default1",
        "name": "短线大佬_Mike",
        "state_file": "last_trade_time_11.txt",
        "trade_enabled": True,  # 启用交易
    }
]

FEISHU_WEBHOOKS = {
    "default1": "https://www.feishu.cn/flow/api/trigger-webhook/2fce2ab758d76da4bc3678415f33426c"
}

CHECK_INTERVAL = 3
MAX_RUNTIME = 3600

TRADE_TYPE_MAP = {
    ("1", 1): "开多",
    ("1", 2): "开空",
    ("2", 3): "平多",
    ("2", 4): "平空"
}

BINANCE_API_CONFIG = {
    "api_key": "your_api_key",
    "secret_key": "your_secret_key",
    "base_url": "https://api.binance.com",
    "recv_window": 5000,
    "leverage": 50,
    "margin_type": "ISOLATED",
    "max_position_ratio": 0.1
}

# 币对映射表，将交易员使用的币对格式转换为币安的格式
SYMBOL_MAPPING = {
    "ETH/USDT": "ETHUSDT",
    "BTC/USDT": "BTCUSDT",
    # 添加更多需要的币对映射
}


def generate_binance_signature(secret_key, query_string):
    """生成币安API签名"""
    return hmac.new(
        secret_key.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


def binance_api_request(method, path, params=None, body=None):
    """币安API请求通用方法"""
    try:
        timestamp = str(int(time.time() * 1000))
        params = params or {}
        params['timestamp'] = timestamp
        params['recvWindow'] = BINANCE_API_CONFIG['recv_window']

        # 排序参数并生成查询字符串
        sorted_params = sorted(params.items())
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])

        # 生成签名
        signature = generate_binance_signature(BINANCE_API_CONFIG['secret_key'], query_string)
        full_url = f"{BINANCE_API_CONFIG['base_url']}{path}?{query_string}&signature={signature}"

        headers = {
            "X-MBX-APIKEY": BINANCE_API_CONFIG["api_key"],
            "Content-Type": "application/json"
        }

        response = requests.request(method, full_url, headers=headers, data=json.dumps(body) if body else None)
        result = response.json()

        if response.status_code != 200:
            print(f"币安API请求失败: {result}")
            return None
        return result
    except Exception as e:
        print(f"币安API请求异常: {str(e)}")
        return None


def get_current_price(symbol):
    """获取当前市场价格"""
    try:
        # 如果是币安格式的符号，直接使用
        std_symbol = symbol.replace("_UMCBL", "")
        path = "/api/v3/ticker/price"
        params = {"symbol": std_symbol}

        response = requests.get(
            BINANCE_API_CONFIG["base_url"] + path,
            params=params
        )

        result = response.json()
        if 'price' in result:
            return float(result['price'])
        return None
    except Exception as e:
        print(f"获取当前价格失败: {str(e)}")
        return None


def convert_to_binance_symbol(trader_symbol):
    """将交易员的币对格式转换为币安的格式"""
    # 移除可能的U本位后缀
    base_symbol = trader_symbol.replace("_UMCBL", "")
    # 如果已经是币安格式，直接返回
    if trader_symbol in SYMBOL_MAPPING.values():
        return trader_symbol

    # 尝试从映射表中找到对应关系
    binance_symbol = SYMBOL_MAPPING.get(trader_symbol)
    if binance_symbol:
        return binance_symbol

    # 如果没有找到映射，尝试自动转换（假设是标准格式）
    if "/" in trader_symbol:
        return trader_symbol.replace("/", "")

    return None


def set_leverage(symbol, leverage):
    """设置杠杆"""
    path = "/sapi/v1/position/leverage"
    params = {
        "symbol": symbol,
        "leverage": leverage,
        "marginType": BINANCE_API_CONFIG['margin_type']
    }
    return binance_api_request("POST", path, params)


def change_initial_margin(symbol, amount):
    """调整初始保证金"""
    path = "/sapi/v1/position/margin"
    params = {
        "symbol": symbol,
        "amount": amount,
        "type": 1,  # 1: 增加保证金, 2: 减少保证金
        "recvWindow": BINANCE_API_CONFIG['recv_window']
    }
    return binance_api_request("POST", path, params)


def place_binance_order(api_config, trade_data):
    """在币安执行下单"""
    if not api_config.get("trade_enabled", False):
        print(f"⚠️ {api_config['name']} 的自动交易未启用")
        return False

    try:
        # 1. 解析交易信号 - 动态获取币对
        trader_symbol = trade_data["symbol"]
        binance_symbol = convert_to_binance_symbol(trader_symbol)

        if not binance_symbol:
            print(f"❌ 无法识别的币对格式: {trader_symbol}")
            return False

        # 解析交易类型
        order_type = trade_data["orderType"]
        buy_or_sell = trade_data["buyOrSell"]
        trade_type = parse_trade_type(order_type, buy_or_sell)
        
        # 确定持仓方向
        position_side = None
        if order_type == "1":  # 开仓
            position_side = "LONG" if buy_or_sell == 1 else "SHORT"
        elif order_type == "2":  # 平仓
            position_side = "SHORT" if buy_or_sell == 3 else "LONG"  # 平多需要平空头仓位，反之亦然
        order_type = "MARKET"
        signal_price = float(trade_data["price"])

        # 2. 计算下单金额 (USDT金额除以100取整)
        raw_amount = float(trade_data["amountConvertU"])
        adjusted_usdt_amount = int(raw_amount / 10000)  # 除以100取整

        # 3. 获取当前市场价格并验证滑点
        current_price = get_current_price(binance_symbol)
        if not current_price:
            print("❌ 获取当前价格失败，取消下单")
            return False

        price_diff = abs(current_price - signal_price) / signal_price
        print(f"信号价格: {signal_price}, 当前价格: {current_price}, 滑点: {price_diff * 100:.2f}%")

        if price_diff > 0.001:  # 0.1%滑点限制
            print(f"❌ 价格滑点超过限制: {price_diff * 100:.2f}%")
            return False

        # 4. 设置杠杆
        leverage_result = set_leverage(binance_symbol, BINANCE_API_CONFIG['leverage'])
        if not leverage_result:
            print("❌ 设置杠杆失败")
            return False

        # 5. 计算下单数量 (合约数量 = USDT金额 / 价格)
        order_size = adjusted_usdt_amount / current_price
        order_size = round(order_size, 6)  # 币安合约通常需要更多小数位

        if order_size <= 0:
            print("❌ 计算出的下单数量无效")
            return False
        

        # 6. 构建请求参数
        path = "/sapi/v1/order"
        params = {
        "symbol": binance_symbol,
        "side": "BUY" if buy_or_sell == 1 else "SELL",
        "type": order_type,
        "quantity": order_size,
        "positionSide": position_side,  # 使用动态计算的持仓方向
        "newOrderRespType": "RESULT",
        "recvWindow": BINANCE_API_CONFIG['recv_window']
    }

        # 7. 发送下单请求
        result = binance_api_request("POST", path, params)
        if result and 'orderId' in result:
            print(f"✅ 下单成功: {result['orderId']}, 数量: {order_size}, 价格: {current_price}")

            # 可选：调整保证金
            # position_amount = order_size * current_price
            # if position_amount > 0:
            #     margin_result = change_initial_margin(binance_symbol, position_amount * BINANCE_API_CONFIG['max_position_ratio'])
            #     if margin_result:
            #         print("保证金调整成功")

            return True
        else:
            print(f"❌ 下单失败: {result.get('msg', '未知错误')}")
            return False

    except Exception as e:
        print(f"下单过程中发生异常: {str(e)}")
        return False


def parse_trade_type(order_type, buy_or_sell):
    """根据订单类型和买卖方向解析交易类型"""
    return TRADE_TYPE_MAP.get((order_type, buy_or_sell), "未知交易类型")


def load_last_time(state_file):
    """加载指定API的最新交易时间"""
    try:
        with open(state_file, 'r') as f:
            return int(f.read().strip())
    except:
        return 0


def save_last_time(timestamp, state_file):
    """保存指定API的最新交易时间"""
    with open(state_file, 'w') as f:
        f.write(str(timestamp))


def check_single_api(api_config):
    """检查单个API的更新"""
    last_time = load_last_time(api_config["state_file"])

    try:
        response = requests.get(api_config["url"], timeout=15)
        data = response.json()

        if data['errno'] != 0 or not data['result']['list']:
            return

        sorted_trades = sorted(data['result']['list'],
                               key=lambda x: x['time'],
                               reverse=True)

        new_trades = [t for t in sorted_trades if t['time'] > last_time]

        if not new_trades:
            return

        latest_trade = new_trades[0]

        # 时间窗口校验
        current_time = time.time()
        time_diff = current_time - latest_trade['time']
        if time_diff > 30:
            print(f"⏰ 交易时间超过30秒，跳过处理: {latest_trade['symbol']}")
            return

        # 保存最新交易时间
        save_last_time(latest_trade['time'], api_config["state_file"])

        # 执行交易
        if api_config.get("trade_enabled", False):
            print(f"准备为 {api_config['name']} 执行交易...")
            place_binance_order(api_config, latest_trade)

    except Exception as e:
        print(f"[{api_config['url']}] 请求异常: {str(e)}")


def send_notification(api_config, trade_data):
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        msg['Subject'] = f"【中产线策略警报】-{api_config['name']}"

        # 动态获取币对信息
        trader_symbol = trade_data["symbol"]
        num_unit = trader_symbol.split("/")[0] if "/" in trader_symbol else trader_symbol
        price_unit = "USDT"

        try:
            profit = float(trade_data.get('profitLoss', 0) or 0)
            amount = float(trade_data.get('amountConvertU', 0) or 0)

            if amount != 0:
                ratio = profit * 10000 / amount
                ratio_str = f"{ratio:.2f}%"
                pl_ratio = f" {ratio_str}"
            else:
                pl_ratio = "0.00% (无交易金额)"
            trade_amountConvertU = amount / 100
        except Exception as e:
            print(f"盈亏计算异常: {str(e)}")
            pl_ratio = "计算异常"
            trade_amountConvertU = "N/A"

        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h3 style="color: #e74c3c;">🚨 交易警报</h3>
            <p><strong>交易员：</strong>{api_config['name']}</p>
            <p><strong>币对：</strong>{trader_symbol}</p>
            <p><strong>方向：</strong>{parse_trade_type(trade_data['orderType'], trade_data['buyOrSell'])}</p>
            <p><strong>时间：</strong>{datetime.utcfromtimestamp(trade_data['time']).astimezone(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p><strong>开仓价格：</strong>{trade_data['price']} {price_unit}</p>
            <p><strong>杠杆：</strong>{trade_data['multiple']} 倍</p>
            <p><strong>数量：</strong>{trade_data['amountConvert']} {num_unit}</p>
            <p><strong>交易金额：</strong>{trade_amountConvertU} USDT</p>
            <p><strong>盈亏：</strong>{profit} USDT</p>
            <p><strong>盈亏比：</strong>{pl_ratio}</p>
          </body>
        </html>
        """
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        with smtplib.SMTP_SSL(CONFIG['email']['server'], CONFIG['email']['port']) as server:
            server.login(CONFIG['email']['from'], CONFIG['email']['password'])
            server.send_message(msg)
        print("邮件通知发送成功")
        return True
    except Exception as e:
        print(f"邮件发送失败: {str(e)}")
        return False


if __name__ == '__main__':
    start_time = time.time()

    while True:
        if time.time() - start_time > MAX_RUNTIME:
            print(f"\n⏰ 已达到最大运行时间 {MAX_RUNTIME // 3600} 小时，程序自动退出")
            break

        for config in API_CONFIGS:
            check_single_api(config)

        elapsed = time.time() - start_time
        sleep_time = max(0, CHECK_INTERVAL - (elapsed % CHECK_INTERVAL))
        time.sleep(sleep_time)