#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pivot Points 压力位支撑位计算工具
====================================
基于经典枢轴点（Classic Pivot Points）计算压力位和支撑位，
包含止盈止损参考、RSI指标和交易策略建议。

支持交易对：
  - BTCUSDT（比特币）
  - SNDKB/USDT（币安 Sandisk bStocks 代币化股票）

数据源：币安公开 API（无需 API Key）

使用方法：
  python pivot_calculator.py                          # 默认计算 BTCUSDT
  python pivot_calculator.py --symbol BTCUSDT        # 指定交易对
  python pivot_calculator.py --symbol SNDKBUSDT      # 计算 SNDK
  python pivot_calculator.py --symbol BTCUSDT --days 3  # 显示最近3天
"""

import argparse
import datetime
import json
import sys
from typing import Dict, Optional, Tuple

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)


# ============================================================
# 配置
# ============================================================
BINANCE_API = "https://api.binance.com/api/v3"
TIMEZONE_OFFSET = 8  # UTC+8（北京时间）

# 交易对配置
SYMBOL_CONFIG = {
    "BTCUSDT": {
        "name": "比特币 BTC",
        "price_precision": 0,      # 价格小数位数
        "pct_precision": 2,         # 涨跌幅小数位数
    },
    "SNDKBUSDT": {
        "name": "闪迪 SNDK (bStocks)",
        "price_precision": 2,
        "pct_precision": 2,
    },
}


# ============================================================
# 数据获取
# ============================================================
def get_klines(symbol: str, interval: str = "1d", limit: int = 10) -> list:
    """
    从币安获取K线数据

    Args:
        symbol: 交易对，如 BTCUSDT
        interval: K线周期，1d=日线, 1h=小时线, 15m=15分钟
        limit: 获取数量

    Returns:
        K线数据列表
    """
    url = f"{BINANCE_API}/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"获取K线数据失败: {e}")
        return []


def get_ticker_24h(symbol: str) -> Optional[Dict]:
    """
    获取24小时行情数据

    Returns:
        包含最新价、24h涨跌幅、最高、最低、成交量等
    """
    url = f"{BINANCE_API}/ticker/24hr"
    params = {"symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"获取24h行情失败: {e}")
        return None


def get_daily_ohlc(symbol: str, days: int = 5) -> list:
    """
    获取按 UTC+8 自然日计算的日线 OHLC 数据

    币安默认日线是 UTC 00:00 开盘，需要转换为 UTC+8。
    方法：使用 1h K线，按 UTC+8 自然日聚合。

    Args:
        symbol: 交易对
        days: 获取天数

    Returns:
        列表，每个元素为 dict: {date, open, high, low, close}
    """
    # 获取足够的小时线数据（days * 24 + 余量）
    hours_needed = days * 24 + 24
    klines = get_klines(symbol, interval="1h", limit=hours_needed)
    if not klines:
        return []

    # 按 UTC+8 自然日聚合
    daily_data = {}
    for k in klines:
        # k[0] = 开盘时间戳（毫秒），k[1]=open, k[2]=high, k[3]=low, k[4]=close
        ts = int(k[0]) / 1000
        # 转换为 UTC+8 时间
        dt_utc8 = datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=TIMEZONE_OFFSET)
        date_str = dt_utc8.strftime("%Y-%m-%d")

        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])

        if date_str not in daily_data:
            daily_data[date_str] = {"open": o, "high": h, "low": l, "close": c}
        else:
            d = daily_data[date_str]
            d["high"] = max(d["high"], h)
            d["low"] = min(d["low"], l)
            d["close"] = c  # 最后一根小时线的收盘价作为日线收盘价

    # 按日期排序，取最近 days 天
    sorted_dates = sorted(daily_data.keys(), reverse=True)[:days]
    result = []
    for date in sorted_dates:
        d = daily_data[date]
        result.append({
            "date": date,
            "open": d["open"],
            "high": d["high"],
            "low": d["low"],
            "close": d["close"],
        })
    return result


# ============================================================
# 枢轴点计算
# ============================================================
def calculate_pivot_points(high: float, low: float, close: float) -> Dict[str, float]:
    """
    经典枢轴点（Classic Pivot Points）计算

    公式：
      PP = (H + L + C) / 3
      R1 = 2 * PP - L
      R2 = PP + (H - L)
      S1 = 2 * PP - H
      S2 = PP - (H - L)

    Args:
        high: 前一交易日最高价
        low: 前一交易日最低价
        close: 前一交易日收盘价

    Returns:
        包含 PP, R1, R2, S1, S2 的字典
    """
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    r2 = pp + (high - low)
    s1 = 2 * pp - high
    s2 = pp - (high - low)

    return {
        "PP": round(pp, 2),
        "R1": round(r1, 2),
        "R2": round(r2, 2),
        "S1": round(s1, 2),
        "S2": round(s2, 2),
    }


# ============================================================
# RSI 计算
# ============================================================
def calculate_rsi(closes: list, period: int = 14) -> Optional[float]:
    """
    计算 RSI（相对强弱指数）

    使用 Wilder's Smoothing 方法

    Args:
        closes: 收盘价列表（从旧到新）
        period: RSI 周期，默认14

    Returns:
        RSI 值（0-100），数据不足返回 None
    """
    if len(closes) < period + 1:
        return None

    # 计算价格变化
    changes = []
    for i in range(1, len(closes)):
        changes.append(closes[i] - closes[i - 1])

    # 初始平均涨跌
    gains = [max(c, 0) for c in changes[:period]]
    losses = [max(-c, 0) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder's Smoothing
    for i in range(period, len(changes)):
        gain = max(changes[i], 0)
        loss = max(-changes[i], 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


# ============================================================
# 止盈止损计算
# ============================================================
def calculate_tp_sl(current_price: float, pivots: Dict[str, float]) -> Dict:
    """
    计算止盈止损参考

    Args:
        current_price: 当前价格
        pivots: 枢轴点字典

    Returns:
        做多和做空策略的入场、止盈、止损、风险收益比
    """
    pp = pivots["PP"]
    r1, r2 = pivots["R1"], pivots["R2"]
    s1, s2 = pivots["S1"], pivots["S2"]

    # 做多策略
    long_entry = pp if current_price > pp else s1
    long_tp1 = r1
    long_tp2 = r2
    long_sl = s2 if current_price > pp else s2 * 0.998  # S2下方0.2%
    long_risk = abs(long_entry - long_sl)
    long_reward1 = abs(long_tp1 - long_entry)
    long_reward2 = abs(long_tp2 - long_entry)
    long_rr1 = round(long_reward1 / long_risk, 2) if long_risk > 0 else 0
    long_rr2 = round(long_reward2 / long_risk, 2) if long_risk > 0 else 0

    # 做空策略
    short_entry = pp if current_price < pp else r1
    short_tp1 = s1
    short_tp2 = s2
    short_sl = r2 if current_price < pp else r2 * 1.002  # R2上方0.2%
    short_risk = abs(short_sl - short_entry)
    short_reward1 = abs(short_entry - short_tp1)
    short_reward2 = abs(short_entry - short_tp2)
    short_rr1 = round(short_reward1 / short_risk, 2) if short_risk > 0 else 0
    short_rr2 = round(short_reward2 / short_risk, 2) if short_risk > 0 else 0

    return {
        "long": {
            "entry": round(long_entry, 2),
            "tp1": round(long_tp1, 2),
            "tp2": round(long_tp2, 2),
            "sl": round(long_sl, 2),
            "rr1": long_rr1,
            "rr2": long_rr2,
        },
        "short": {
            "entry": round(short_entry, 2),
            "tp1": round(short_tp1, 2),
            "tp2": round(short_tp2, 2),
            "sl": round(short_sl, 2),
            "rr1": short_rr1,
            "rr2": short_rr2,
        },
    }


# ============================================================
# 交易策略建议
# ============================================================
def generate_strategy(
    current_price: float,
    pivots: Dict[str, float],
    rsi: Optional[float],
    symbol_config: Dict,
) -> Dict:
    """
    生成交易策略建议

    Args:
        current_price: 当前价格
        pivots: 枢轴点
        rsi: RSI值
        symbol_config: 交易对配置

    Returns:
        策略建议字典
    """
    pp = pivots["PP"]
    r1, r2 = pivots["R1"], pivots["R2"]
    s1, s2 = pivots["S1"], pivots["S2"]

    # 价格位置判断
    if current_price > r1:
        position = "强势上涨（R1上方）"
        bias = "偏多"
    elif current_price > pp:
        position = "PP上方（偏多区域）"
        bias = "偏多"
    elif current_price > s1:
        position = "PP下方（偏空区域）"
        bias = "偏空"
    else:
        position = "弱势下跌（S1下方）"
        bias = "偏空"

    # RSI 判断
    if rsi is None:
        rsi_status = "数据不足"
        rsi_signal = "无法判断"
    elif rsi > 70:
        rsi_status = "超买（>70）"
        rsi_signal = "短期可能回调，谨慎追多"
    elif rsi < 30:
        rsi_status = "超卖（<30）"
        rsi_signal = "可能技术性反弹，谨慎追空"
    else:
        rsi_status = "中性（30-70）"
        rsi_signal = "趋势延续中"

    # 操作建议
    if bias == "偏多" and rsi is not None and rsi < 65:
        suggestion = "轻仓做多"
        trigger = f"回踩 {pp:.2f}(PP) 企稳，或突破 {r1:.2f}(R1) 站稳"
    elif bias == "偏空" and rsi is not None and rsi > 35:
        suggestion = "轻仓做空"
        trigger = f"反弹至 {pp:.2f}(PP) 遇阻，或跌破 {s1:.2f}(S1)"
    elif rsi is not None and rsi > 70:
        suggestion = "观望（超买）"
        trigger = f"等待回调至 {pp:.2f}(PP) 附近再考虑"
    elif rsi is not None and rsi < 30:
        suggestion = "观望（超卖）"
        trigger = f"等待反弹至 {pp:.2f}(PP) 附近再考虑"
    else:
        suggestion = "观望"
        trigger = "等待方向明确"

    # 仓位建议
    if rsi is not None and (rsi > 70 or rsi < 30):
        position_size = "轻仓（5%-10%）"
    else:
        position_size = "轻仓（10%-15%）"

    return {
        "position": position,
        "bias": bias,
        "rsi_status": rsi_status,
        "rsi_signal": rsi_signal,
        "suggestion": suggestion,
        "trigger": trigger,
        "position_size": position_size,
    }


# ============================================================
# 格式化输出
# ============================================================
def format_price(price: float, precision: int) -> str:
    """格式化价格"""
    if precision == 0:
        return f"${price:,.0f}"
    return f"${price:,.{precision}f}"


def print_report(
    symbol: str,
    ticker: Dict,
    daily_data: list,
    pivots: Dict[str, float],
    rsi: Optional[float],
    tp_sl: Dict,
    strategy: Dict,
):
    """打印完整分析报告"""
    config = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["BTCUSDT"])
    name = config["name"]
    p_prec = config["price_precision"]

    current_price = float(ticker["lastPrice"])
    price_change = float(ticker["priceChangePercent"])
    high_24h = float(ticker["highPrice"])
    low_24h = float(ticker["lowPrice"])
    volume = float(ticker["volume"])

    now = datetime.datetime.now()  # 本地时间

    print("=" * 60)
    print(f"  📊 {name} 趋势分析报告")
    print(f"  🕐 {now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    print("=" * 60)

    # 基本行情
    print(f"\n  【基本行情】")
    print(f"  最新价格: {format_price(current_price, p_prec)}")
    print(f"  24h涨跌幅: {price_change:+.2f}%")
    print(f"  24h最高: {format_price(high_24h, p_prec)}")
    print(f"  24h最低: {format_price(low_24h, p_prec)}")
    print(f"  24h成交量: {volume:,.2f}")

    # 前一交易日数据
    if daily_data:
        prev = daily_data[0] if daily_data[0]["date"] != now.strftime("%Y-%m-%d") else (daily_data[1] if len(daily_data) > 1 else daily_data[0])
        print(f"\n  【前一交易日 OHLC（{prev['date']}）】")
        print(f"  开盘: {format_price(prev['open'], p_prec)}")
        print(f"  最高: {format_price(prev['high'], p_prec)}")
        print(f"  最低: {format_price(prev['low'], p_prec)}")
        print(f"  收盘: {format_price(prev['close'], p_prec)}")

    # 枢轴点
    print(f"\n  【压力位和支撑位（经典枢轴点）】")
    print(f"  🔴 R2（第二阻力）: {format_price(pivots['R2'], p_prec)}")
    print(f"  🟠 R1（第一阻力）: {format_price(pivots['R1'], p_prec)}")
    print(f"  ⚪ PP（枢轴点）  : {format_price(pivots['PP'], p_prec)}  ← 多空分界线")
    print(f"  🟢 S1（第一支撑）: {format_price(pivots['S1'], p_prec)}")
    print(f"  🔵 S2（第二支撑）: {format_price(pivots['S2'], p_prec)}")

    # 当前价格位置
    if current_price > pivots["R1"]:
        loc = "R1上方（强势）"
    elif current_price > pivots["PP"]:
        loc = "PP-R1之间（偏多）"
    elif current_price > pivots["S1"]:
        loc = "S1-PP之间（偏空）"
    else:
        loc = "S1下方（弱势）"
    print(f"  📍 当前位置: {loc}")

    # RSI
    print(f"\n  【RSI 相对强弱指数】")
    if rsi is not None:
        if rsi > 70:
            status = "⚠️ 超买"
        elif rsi < 30:
            status = "⚠️ 超卖"
        else:
            status = "✅ 中性"
        print(f"  RSI(14): {rsi:.2f}  {status}")
    else:
        print(f"  RSI(14): 数据不足")

    # 止盈止损
    print(f"\n  【止盈止损参考】")
    long = tp_sl["long"]
    short = tp_sl["short"]
    print(f"  📈 做多策略:")
    print(f"     入场: {format_price(long['entry'], p_prec)}")
    print(f"     止盈1: {format_price(long['tp1'], p_prec)} (RR={long['rr1']})")
    print(f"     止盈2: {format_price(long['tp2'], p_prec)} (RR={long['rr2']})")
    print(f"     止损: {format_price(long['sl'], p_prec)}")
    print(f"  📉 做空策略:")
    print(f"     入场: {format_price(short['entry'], p_prec)}")
    print(f"     止盈1: {format_price(short['tp1'], p_prec)} (RR={short['rr1']})")
    print(f"     止盈2: {format_price(short['tp2'], p_prec)} (RR={short['rr2']})")
    print(f"     止损: {format_price(short['sl'], p_prec)}")

    # 交易策略
    print(f"\n  【交易策略建议】")
    print(f"  价格位置: {strategy['position']}")
    print(f"  多空倾向: {strategy['bias']}")
    print(f"  RSI状态: {strategy['rsi_status']}")
    print(f"  RSI信号: {strategy['rsi_signal']}")
    print(f"  操作建议: {strategy['suggestion']}")
    print(f"  触发条件: {strategy['trigger']}")
    print(f"  仓位建议: {strategy['position_size']}")
    print(f"  ⚠️ 风险提示: 以上仅供参考，不构成投资建议")

    print("\n" + "=" * 60)


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Pivot Points 压力位支撑位计算工具")
    parser.add_argument("--symbol", type=str, default="BTCUSDT",
                        help="交易对，如 BTCUSDT / SNDKBUSDT")
    parser.add_argument("--days", type=int, default=5,
                        help="显示最近N天数据（默认5）")
    parser.add_argument("--json", action="store_true",
                        help="以JSON格式输出")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    if symbol not in SYMBOL_CONFIG:
        print(f"⚠️ 未配置的交易对: {symbol}")
        print(f"支持的交易对: {', '.join(SYMBOL_CONFIG.keys())}")
        print("将使用默认配置（价格精度2位）")
        SYMBOL_CONFIG[symbol] = {"name": symbol, "price_precision": 2, "pct_precision": 2}

    print(f"正在获取 {symbol} 数据...")

    # 获取24h行情
    ticker = get_ticker_24h(symbol)
    if not ticker:
        print("获取行情数据失败，请检查网络或交易对名称")
        sys.exit(1)

    # 获取日线数据（用于计算枢轴点和RSI）
    daily_data = get_daily_ohlc(symbol, days=max(args.days, 20))  # RSI需要至少15天
    if not daily_data:
        print("获取日线数据失败")
        sys.exit(1)

    # 找到前一交易日（不是今天）
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    prev_day = None
    for d in daily_data:
        if d["date"] != today_str:
            prev_day = d
            break
    if not prev_day:
        prev_day = daily_data[0]

    # 计算枢轴点
    pivots = calculate_pivot_points(prev_day["high"], prev_day["low"], prev_day["close"])

    # 计算RSI（使用收盘价，从旧到新）
    closes = [d["close"] for d in reversed(daily_data)]
    rsi = calculate_rsi(closes, period=14)

    # 计算止盈止损
    current_price = float(ticker["lastPrice"])
    tp_sl = calculate_tp_sl(current_price, pivots)

    # 生成策略
    strategy = generate_strategy(current_price, pivots, rsi, SYMBOL_CONFIG[symbol])

    # JSON 输出
    if args.json:
        result = {
            "symbol": symbol,
            "name": SYMBOL_CONFIG[symbol]["name"],
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": {
                "lastPrice": current_price,
                "priceChangePercent": float(ticker["priceChangePercent"]),
                "highPrice": float(ticker["highPrice"]),
                "lowPrice": float(ticker["lowPrice"]),
                "volume": float(ticker["volume"]),
            },
            "previous_day": prev_day,
            "pivot_points": pivots,
            "rsi_14": rsi,
            "tp_sl": tp_sl,
            "strategy": strategy,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # 文本报告
    print_report(symbol, ticker, daily_data, pivots, rsi, tp_sl, strategy)


if __name__ == "__main__":
    main()
