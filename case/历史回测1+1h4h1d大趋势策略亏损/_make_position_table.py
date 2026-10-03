# -*- coding: utf-8 -*-
"""
下单金额对照表生成器。

用本地历史 K 线统计各标的的 ATR 分布，再按「风险反推数量」公式
算出不同账户规模下的实际下单金额。离线运行，不需要网络。

公式：
    止损距离 = ATR × SL_ATR
    下单数量 = 权益 × 风险% ÷ 止损距离
    名义价值 = 下单数量 × 价格
    占用保证金 = 名义价值 ÷ 杠杆
"""

import os
import sys
from decimal import Decimal

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import binance_combined_signal_trader as live   # noqa: E402

DATA = os.path.join(HERE, "backtest", "data")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BALANCES = [1000, 5000, 10000, 50000, 100000]
LEVERAGE = 10

# 各标的的合约最小下单量（近似值，实盘以 exchangeInfo 为准）
MIN_QTY = {"BTCUSDT": Decimal("0.001"),
           "ETHUSDT": Decimal("0.001"),
           "SOLUSDT": Decimal("0.01")}


def atr_stats(symbol):
    """近 3 年 15m ATR 的分位数 + 最新价格"""
    df = pd.read_pickle(os.path.join(DATA, "spot_%s_15m.pkl" % symbol))
    df = df.tail(3 * 365 * 96).reset_index(drop=True)   # 近 3 年
    d = live.add_indicators(df)
    atr = d["ATR"].dropna()
    atr = atr[atr > 0]
    price = float(d["close"].iloc[-1])
    atr_pct = (atr / d["close"].tail(len(atr))).dropna()
    return {
        "price": price,
        "atr_med": float(atr.median()),
        "atr_p10": float(atr.quantile(0.10)),
        "atr_p90": float(atr.quantile(0.90)),
        "atr_pct_med": float(atr_pct.median()),
    }


def main():
    lines = []
    add = lines.append

    add("# 下单金额对照表")
    add("")
    add("策略：综合版信号 | 止损 %.0f ATR | 止盈 %.0f ATR | 单笔风险 %.1f%% | 杠杆 %dx"
        % (float(live.SL_ATR), float(live.TP_ATR),
           float(live.RISK_PER_TRADE) * 100, LEVERAGE))
    add("")
    add("核心逻辑：**先定亏损金额，再反推下单数量**。")
    add("")
    add("```")
    add("风险金额 = 权益 × 2%")
    add("止损距离 = ATR × 5")
    add("下单数量 = 风险金额 ÷ 止损距离")
    add("```")
    add("")
    add("所以 ATR 越大 → 止损越远 → 数量越小 → 亏损金额始终不变。")
    add("")

    stats = {}
    add("## 一、各标的的波动特征（近 3 年 15m 数据）")
    add("")
    add("| 标的 | 最新价 | ATR 中位数 | ATR 10%分位 | ATR 90%分位 | ATR/价格 |")
    add("|---|---|---|---|---|---|")
    for s in SYMBOLS:
        try:
            st = atr_stats(s)
            stats[s] = st
            add("| %s | %.2f | %.4f | %.4f | %.4f | %.3f%% |"
                % (s, st["price"], st["atr_med"], st["atr_p10"], st["atr_p90"],
                   st["atr_pct_med"] * 100))
        except Exception as e:
            add("| %s | 读取失败 | - | - | - | - |" % s)
            print("  %s 失败: %s" % (s, e))
    add("")

    add("## 二、止损止盈距离（ATR 中位数下）")
    add("")
    add("| 标的 | ATR | 止损距离 | 止损幅度 | 止盈距离 | 止盈幅度 | 盈亏比 |")
    add("|---|---|---|---|---|---|---|")
    for s in SYMBOLS:
        if s not in stats:
            continue
        st = stats[s]
        atr = st["atr_med"]
        sl_d = atr * float(live.SL_ATR)
        tp_d = atr * float(live.TP_ATR)
        add("| %s | %.4f | %.4f | %.2f%% | %.4f | %.2f%% | %.1f:1 |"
            % (s, atr, sl_d, sl_d / st["price"] * 100,
               tp_d, tp_d / st["price"] * 100,
               float(live.TP_ATR) / float(live.SL_ATR)))
    add("")

    add("## 三、下单金额对照（ATR 中位数）")
    add("")
    add("| 标的 | 账户权益 | 单笔风险额 | 下单数量 | 名义价值 | 占用保证金 | 实际杠杆 |")
    add("|---|---|---|---|---|---|---|")
    for s in SYMBOLS:
        if s not in stats:
            continue
        st = stats[s]
        atr = Decimal(str(st["atr_med"]))
        price = Decimal(str(st["price"]))
        sl_dist = atr * live.SL_ATR
        for bal in BALANCES:
            eq = Decimal(str(bal))
            risk_amt = eq * live.RISK_PER_TRADE
            qty = live.floor_step(risk_amt / sl_dist, MIN_QTY[s])
            notional = qty * price
            margin = notional / LEVERAGE
            lev = notional / eq
            add("| %s | %s | %.2f | %s | %.2f | %.2f | %.2fx |"
                % (s, "{:,}".format(bal), float(risk_amt), qty,
                   float(notional), float(margin), float(lev)))
    add("")

    add("## 四、三标的同时满仓时的风险")
    add("")
    add("| 账户权益 | 单笔风险 | 三标的同时持仓总风险 | 占权益 |")
    add("|---|---|---|---|")
    for bal in BALANCES:
        r = bal * float(live.RISK_PER_TRADE)
        add("| {:,} | {:.2f} | {:.2f} | {:.1f}% |".format(bal, r, r * 3, r * 3 / bal * 100))
    add("")
    add("> 最坏情况：三个标的同一天全部止损，账户回撤 6%（单笔 2% × 3）。")
    add("> 回测中最大回撤 58%，意味着这种连亏会连续发生多次。")
    add("")

    add("## 五、ATR 极端情况下的数量变化（以 BTC 10000 USDT 为例）")
    add("")
    add("| 行情状态 | ATR | 止损距离 | 下单数量 | 名义价值 | 实际杠杆 |")
    add("|---|---|---|---|---|---|")
    if "BTCUSDT" in stats:
        st = stats["BTCUSDT"]
        price = Decimal(str(st["price"]))
        for tag, atr_v in (("平静(10%分位)", st["atr_p10"]),
                           ("常态(中位数)", st["atr_med"]),
                           ("剧烈(90%分位)", st["atr_p90"])):
            atr = Decimal(str(atr_v))
            sl_dist = atr * live.SL_ATR
            eq = Decimal("10000")
            qty = live.floor_step(eq * live.RISK_PER_TRADE / sl_dist, MIN_QTY["BTCUSDT"])
            notional = qty * price
            add("| %s | %.2f | %.2f | %s | %.2f | %.2fx |"
                % (tag, atr_v, float(sl_dist), qty,
                   float(notional), float(notional / eq)))
    add("")
    add("> 注意：波动小的时候数量会自动变大，名义价值可能超过权益，")
    add("> 这是正常的——因为止损很近，杠杆自然被推高。10x 杠杆足够覆盖。")
    add("")

    out = os.path.join(HERE, "backtest", "results", "下单金额对照表.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("已保存 %s" % out)

    # 同时打印到终端
    print("\n".join(lines[20:60]))


if __name__ == "__main__":
    main()
