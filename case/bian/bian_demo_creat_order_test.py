# 测试网使用testnet.binancefuture.com
# 主网使用fapi.binance.com

# 安装依赖：pip install ccxt
# 替换YOUR_API_KEY等占位符
# 先在测试网验证逻辑
# 主网操作前进行风险评估

import ccxt  # 需提前安装ccxt库
import time

# 测试网配置
class TestnetConfig:
    API_KEY = 'YOUR_TESTNET_API_KEY'
    SECRET = 'YOUR_TESTNET_SECRET'
    BASE_URL = 'https://testnet.binancefuture.com'
    SYMBOL = 'BTC/USDT'
    AMOUNT = 0.01
    LEVERAGE = 10
    ORDER_TYPE = 'market'  # market/limit
    SIDE = 'buy'
    LIMIT_PRICE = 50000
    STOP_PROFIT = 52000
    STOP_LOSS = 48000

def testnet_operations():
    # 初始化交易所对象
    exchange = ccxt.binance({
        'apiKey': TestnetConfig.API_KEY,
        'secret': TestnetConfig.SECRET,
        'enableRateLimit': True,
        'urls': {'test': TestnetConfig.BASE_URL}
    })
    
    # 设置全仓模式
    exchange.private_post_futures_positionmargin_type({'marginType': 'CROSSED'})
    
    # 设置杠杆
    exchange.private_post_futures_positionmargin_leverage({
        'symbol': TestnetConfig.SYMBOL.replace('/', ''),
        'leverage': TestnetConfig.LEVERAGE
    })
    
    # 下单（市价/限价）
    order_params = {
        'symbol': TestnetConfig.SYMBOL.replace('/', ''),
        'type': TestnetConfig.ORDER_TYPE,
        'side': TestnetConfig.SIDE,
        'amount': TestnetConfig.AMOUNT,
    }
    if TestnetConfig.ORDER_TYPE == 'limit':
        order_params['price'] = TestnetConfig.LIMIT_PRICE
    
    order = exchange.create_order(**order_params)
    print(f"订单创建成功，ID: {order['id']}")
    
    # 设置止盈止损
    stop_params = {
        'stopPrice': TestnetConfig.STOP_PROFIT,
        'closePosition': True
    }
    exchange.create_order(
        symbol=TestnetConfig.SYMBOL.replace('/', ''),
        type='stop_market',
        side='sell' if TestnetConfig.SIDE == 'buy' else 'buy',
        amount=TestnetConfig.AMOUNT,
        price=TestnetConfig.STOP_PROFIT - 10,  # 略低于止盈价
        params=stop_params
    )

if __name__ == '__main__':
    testnet_operations()