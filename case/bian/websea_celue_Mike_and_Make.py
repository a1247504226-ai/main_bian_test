import requests
import json
import time
from datetime import datetime, timedelta
import pytz  # 🔴 新增时区处理库
# 新增邮件模块依赖
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib  # 🔴 新增SMTP支持

CONFIG = {
    'email': {
        'from': 'a1247504226@163.com',
        'to': '1247504226@qq.com',
        'password': 'XKCINXNMOMMDCAFI',
        'server': 'smtp.163.com',
        'port': 465,
        'max_retries': 3,
        'cooldown': timedelta(minutes=1)
      #   'alert_cooldown': timedelta(minutes=1)  # 警报专用冷却时间
    }}
# 多配置支持
API_CONFIGS = [
    {
        "url": "https://capi.websea.com/webApi/follow/trader-operation?trader=485142&pageNo=1&pageSize=10",
        "webhook_key": "default1",
        "name": "短线大佬_Mike",
        "state_file": "last_trade_time_01.txt"
    } ,
   {
        "url": "https://capi.websea.com/webApi/follow/trader-operation?trader=493679&pageNo=1&pageSize=10",
        "webhook_key": "default2",
        "name": "短线_Make",
        "state_file": "last_trade_time_02.txt"
    },
    {
        "url": "https://capi.websea.com/webApi/follow/trader-operation?trader=485087&pageNo=1&pageSize=10",
        "webhook_key": "default3",
        "name": "长线大佬_迷人等待",
        "state_file": "last_trade_time_03.txt"
    }
    # ,{
    #     "url": "https://capi.websea.com/webApi/follow/trader-operation?trader=486111&pageNo=1&pageSize=10",
    #     "webhook_key": "default4",
    #     "name": "短线大佬_币圈庄见愁",
    #     "state_file": "last_trade_time_3.txt"
    # }
]

FEISHU_WEBHOOKS = {
    "default1": "https://www.feishu.cn/flow/api/trigger-webhook/2fce2ab758d76da4bc3678415f33426c",
    "default2": "https://www.feishu.cn/flow/api/trigger-webhook/308e8b19ec1d6f0a9eff55da95f24e51",
    "default3": "https://www.feishu.cn/flow/api/trigger-webhook/f2cf530098a14e51dddc1613c2d4286e",
    "default4": "https://www.feishu.cn/flow/api/trigger-webhook/13bb46291460cbd9e0082a25340802db"
}

CHECK_INTERVAL = 3  # 检查间隔（秒）
MAX_RUNTIME = 3600   # 最大运行时间（秒）

TRADE_TYPE_MAP = {
    ("1", 1): "开多",
    ("1", 2): "开空",
    ("2", 3): "平多",
    ("2", 4): "平空"
}

def parse_trade_type(order_type, buy_or_sell):
    """根据订单类型和买卖方向解析交易类型"""
    return TRADE_TYPE_MAP.get((order_type, buy_or_sell), "未知交易类型")

def format_feishu_message(trade, api_config):
    """格式化飞书消息（增加盈亏比计算）"""
    # 🔴 明确指定UTC时区并转换为中国时区（UTC+8）
    utc_time = datetime.utcfromtimestamp(trade['time']).replace(tzinfo=pytz.utc)
    beijing_time = utc_time.astimezone(pytz.timezone('Asia/Shanghai'))
    num_unit = trade["symbol"].split("/")[0]
    price_unit = trade["symbol"].split("/")[1]

    # 🔴 新增盈亏比计算逻辑
    try:
        # 处理空值并转换为浮点数
        profit = float(trade.get('profitLoss', 0) or 0)
        amount = float(trade.get('amountConvertU', 0) or 0)

        # 计算盈亏比例（保留两位小数）
        if amount != 0:
            ratio = profit * 10000 / amount
            ratio_str = f"{ratio:.2f}%"  # 转换为百分比格式
            pl_ratio = f" {ratio_str}"
        else:
            pl_ratio = "0.00% (无交易金额)"
        trade_amountConvertU = amount / 100
    except Exception as e:
        print(f"盈亏计算异常: {str(e)}")
        pl_ratio = "计算异常"
        trade_amountConvertU = "N/A"

    return {
        "message": f"🚨 交易员：{api_config['name']} 产生新交易",
        "token": f"币对：{trade['symbol']}",
        "type": f"方向：{parse_trade_type(trade['orderType'], trade['buyOrSell'])}",
        "time": beijing_time.strftime('%Y-%m-%d %H:%M:%S'),
        "price": f"开仓价格：{trade['price']} {price_unit}",
        "multiple": f"杠杆：{trade['multiple']} 倍",
        "num": f"数量：{trade['amountConvert']} {num_unit}",
        "volume": f"交易金额：{trade_amountConvertU} USDT",
        "profitLoss": f"盈亏：{profit} USDT，盈亏比：{pl_ratio}"  # 🔴 使用计算后的值
    }

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

        # 🔴 新增时间窗口校验
        current_time = time.time()
        time_diff = current_time - latest_trade['time']

        if time_diff > 30:  # 超过20秒（20秒）
            print(f"⏰ 交易时间超过20秒，跳过推送: {latest_trade['symbol']}")
            return

        message = format_feishu_message(latest_trade, api_config)
        webhook_url = FEISHU_WEBHOOKS[api_config["webhook_key"]]

        resp = requests.post(
            webhook_url,
            headers={'Content-Type': 'application/json'},
            data=json.dumps(message)
        )

        if resp.status_code == 200:
            save_last_time(latest_trade['time'], api_config["state_file"])
            print(f"{datetime.now()} [{api_config['url']}] 推送成功: {latest_trade['symbol']}")

            # 🔴 修改条件判断和参数传递
            if api_config["name"] == "长线大佬_迷人等待":
                send_notification(api_config, latest_trade)  # 传递当前配置和交易数据
        else:
            print(f"推送失败: {resp.text}")

    except Exception as e:
        print(f"[{api_config['url']}] 请求异常: {str(e)}")

# 邮件通知模块修正
def send_notification(api_config, trade_data):
    try:
        msg = MIMEMultipart('related')
        msg['From'] = CONFIG['email']['from']
        msg['To'] = CONFIG['email']['to']
        msg['Subject'] = f"【中产线策略警报】-{api_config['name']}"
        num_unit = trade_data["symbol"].split("/")[0]
        price_unit = trade_data["symbol"].split("/")[1]

        # 🔴 新增盈亏比计算逻辑
        try:
            # 处理空值并转换为浮点数
            profit = float(trade_data.get('profitLoss', 0) or 0)
            amount = float(trade_data.get('amountConvertU', 0) or 0)

            # 计算盈亏比例（保留两位小数）
            if amount != 0:
                ratio = profit * 10000 / amount
                ratio_str = f"{ratio:.2f}%"  # 转换为百分比格式
                pl_ratio = f" {ratio_str}"
            else:
                pl_ratio = "0.00% (无交易金额)"
            trade_amountConvertU = amount / 100
        except Exception as e:
            print(f"盈亏计算异常: {str(e)}")
            pl_ratio = "计算异常"
            trade_amountConvertU = "N/A"

        # 修正后的邮件模板（使用实际交易数据）
        html_content = f"""
        <html>
          <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h3 style="color: #e74c3c;">🚨 交易警报</h3>
            <p><strong>交易员：</strong>{api_config['name']}</p>
            <p><strong>币对：</strong>{trade_data['symbol']}</p>
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

        # 🔴 新增邮件发送逻辑
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
            print(f"\n⏰ 已达到最大运行时间 {MAX_RUNTIME//3600} 小时，程序自动退出")
            break

        for config in API_CONFIGS:
            check_single_api(config)

        # 🔴 精确控制执行频率
        elapsed = time.time() - start_time
        sleep_time = max(0, CHECK_INTERVAL - (elapsed % CHECK_INTERVAL))
        time.sleep(sleep_time)