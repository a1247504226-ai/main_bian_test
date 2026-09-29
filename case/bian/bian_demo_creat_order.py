# 测试网使用testnet.binancefuture.com
# 主网使用fapi.binance.com

# 安装依赖：pip install ccxt
# 替换YOUR_API_KEY等占位符
# 先在测试网验证逻辑
# 主网操作前进行风险评估

import ccxt  # 需本地安装ccxt库
import time

# 主网配置
class MainnetConfig:
    API_KEY = 'YOUR_MAINNET_API_KEY'
    SECRET = 'YOUR_MAINNET_SECRET'
    SYMBOL = 'BTC/USDT'
    AMOUNT = 0.001
    LEVERAGE = 5
    ORDER_TYPE = 'LIMIT'  # MARKET/LIMIT
    SIDE = 'BUY'
    LIMIT_PRICE = 50000
    STOP_PROFIT = 51000
    STOP_LOSS = 49000

def mainnet_operations():
    # 初始化交易所
    exchange = ccxt.binance({
        'apiKey': MainnetConfig.API_KEY,
        'secret': MainnetConfig.SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    # 设置全仓模式
    exchange.private_post_futures_positionmargin_type({'marginType': 'CROSSED'})
    
    # 设置杠杆
    exchange.private_post_futures_positionmargin_leverage({
        'symbol': MainnetConfig.SYMBOL.replace('/', ''),
        'leverage': MainnetConfig.LEVERAGE
    })
    
    # 下单
    order = exchange.create_order(
        symbol=MainnetConfig.SYMBOL.replace('/', ''),
        type=MainnetConfig.ORDER_TYPE,
        side=MainnetConfig.SIDE,
        amount=MainnetConfig.AMOUNT,
        price=MainnetConfig.LIMIT_PRICE if MainnetConfig.ORDER_TYPE == 'LIMIT' else None
    )
    
    # 设置止盈止损
    stop_params = {
        'stopPrice': MainnetConfig.STOP_PROFIT,
        'closePosition': True
    }
    exchange.create_order(
        symbol=MainnetConfig.SYMBOL.replace('/', ''),
        type='stop_market',
        side='SELL' if MainnetConfig.SIDE == 'BUY' else 'BUY',
        amount=MainnetConfig.AMOUNT,
        price=MainnetConfig.STOP_PROFIT - 10,
        params=stop_params
    )

if __name__ == '__main__':
    mainnet_operations()