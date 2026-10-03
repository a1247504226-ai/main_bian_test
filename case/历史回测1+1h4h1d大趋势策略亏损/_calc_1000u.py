# -*- coding: utf-8 -*-
"""
1000 USDT 账户的下单链路详解。

用本地历史 K 线（最近一根 + ATR 分位）演示：
  权益 -> 风险金额 -> 止损距离 -> 下单数量 -> 名义价值 -> 占用保证金
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
EQUITY = Decimal("1000")
LEVERAGE = 10
MIN_QTY = {"BTCUSDT": Decimal("0.001"), "ETHUSDT": Decimal("0.001"),
           "SOLUSDT": Decimal("0.01")}
MAX_MARGIN_PER_SYMBOL = EQUITY * Decimal("0.9") * LEVERAGE   # 单标的保证金上限


def load(symbol, tf="15m"):
    return pd.read_pickle(os.path.join(DATA, "spot_%s_%s.pkl" % (symbol, tf))).reset_index(drop=True)


def analyze(symbol):
    df = load(symbol)
    d = live.add_indicators(df)
    price = float(d["close"].iloc[-1])
    atr_now = float(d["ATR"].iloc[-1])

    # 近 3 年 ATR 分位
    tail = d.tail(3 * 365 * 96)
    atr_series = tail["ATR"].dropna()
    atr_series = atr_series[atr_series > 0]
    p10, p50, p90 = (float(atr_series.quantile(q)) for q in (0.10, 0.50, 0.90))
    return {"price": price, "atr_now": atr_now, "p10": p10, "p50": p50, "p90": p90}


def plan(symbol, atr):
    atr_d = Decimal(str(atr))
    price_d = Decimal(str(analyze(symbol)["price"]))
    sl_dist = atr_d * live.SL_ATR
    tp_dist = atr_d * live.TP_ATR
    risk_amt = EQUITY * live.RISK_PER_TRADE
    qty = live.floor_step(risk_amt / sl_dist, MIN_QTY[symbol])
    notional = qty * price_d
    margin = notional / LEVERAGE
    return {
        "atr": atr_d, "sl_dist": sl_dist, "tp_dist": tp_dist,
        "qty": qty, "notional": notional, "margin": margin,
        "sl_pct": sl_dist / price_d * 100,
        "tp_pct": tp_dist / price_d * 100,
        "eff_lev": notional / EQUITY,
        "risk": qty * sl_dist,
        "over_margin": notional > MAX_MARGIN_PER_SYMBOL,
    }


def main():
    print("=" * 116)
    print("  1000 USDT 账户 · 单笔风险 %.1f%% · 止损 %.0f ATR · 止盈 %.0f ATR · 交易所杠杆 %dx"
          % (float(live.RISK_PER_TRADE) * 100, float(live.SL_ATR), float(live.TP_ATR), LEVERAGE))
    print("=" * 116)
    risk_amt = EQUITY * live.RISK_PER_TRADE
    print("  第一步：单笔风险金额 = 1000 × %.0f%% = %.2f USDT   ← 这是每笔最多亏的钱"
          % (float(live.RISK_PER_TRADE) * 100, float(risk_amt)))
    print("=" * 116)

    stats = {}
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        stats[s] = analyze(s)

    # ---- 最近行情快照 ----
    print("\n【A】按最近一根 K 线的真实 ATR 下单\n")
    print("  %-10s %-11s %-9s %-11s %-13s %-13s %-13s %-11s %-9s"
          % ("标的", "价格", "ATR", "止损距离", "下单数量", "名义价值",
             "占用保证金", "实际杠杆", "止损幅度"))
    print("-" * 116)
    total_margin = Decimal("0")
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        p = plan(s, stats[s]["atr_now"])
        total_margin += p["margin"]
        print("  %-10s %-11.2f %-9.4f %-11.2f %-13s %-13.2f %-13.2f %-11.2fx %-9.2f%%"
              % (s, stats[s]["price"], float(p["atr"]), float(p["sl_dist"]), p["qty"],
                 float(p["notional"]), float(p["margin"]), float(p["eff_lev"]),
                 float(p["sl_pct"])))
    print("-" * 116)
    print("  三标的同时满仓：占用保证金合计 %.2f USDT（权益 1000 的 %.1f%%），"
          "累计风险 %.2f USDT（%.1f%%）"
          % (float(total_margin), float(total_margin / EQUITY * 100),
             float(risk_amt * 3), float(risk_amt * 3 / EQUITY * 100)))

    # ---- 波动情景 ----
    print("\n【B】波动变大变小时，数量会自动反向调整（风险金额始终 20 USDT）\n")
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        st = stats[s]
        print("  %s（价格 %.2f）" % (s, st["price"]))
        print("    %-16s %-10s %-11s %-13s %-13s %-11s"
              % ("行情状态", "ATR", "止损距离", "下单数量", "名义价值", "实际杠杆"))
        for tag, atr in (("平静(10%分位)", st["p10"]),
                         ("常态(中位数)", st["p50"]),
                         ("剧烈(90%分位)", st["p90"])):
            p = plan(s, atr)
            warn = "  ⚠ 保证金不足" if p["over_margin"] else ""
            print("    %-16s %-10.4f %-11.2f %-13s %-13.2f %-11.2fx%s"
                  % (tag, float(p["atr"]), float(p["sl_dist"]), p["qty"],
                     float(p["notional"]), float(p["eff_lev"]), warn))
        print()

    # ---- 亏损情景 ----
    print("【C】三种结局下的账户变化（单标的）\n")
    p_btc = plan("BTCUSDT", stats["BTCUSDT"]["atr_now"])
    print("  止损成交  ->  亏 %.2f USDT  ->  权益 1000.00 -> %.2f（-%.1f%%）"
          % (float(p_btc["risk"]), float(EQUITY - p_btc["risk"]),
             float(p_btc["risk"] / EQUITY * 100)))
    print("  止盈成交  ->  赚 %.2f USDT  ->  权益 1000.00 -> %.2f（+%.1f%%）"
          % (float(p_btc["risk"] * live.TP_ATR / live.SL_ATR),
             float(EQUITY + p_btc["risk"] * live.TP_ATR / live.SL_ATR),
             float(p_btc["risk"] * live.TP_ATR / live.SL_ATR / EQUITY * 100)))
    print("  三标的全止损 ->  亏 %.2f USDT  ->  权益 1000.00 -> %.2f（-%.1f%%）"
          % (float(risk_amt * 3), float(EQUITY - risk_amt * 3),
             float(risk_amt * 3 / EQUITY * 100)))

    # ---- 连亏 ----
    print("\n【D】连续亏损的账户衰减（回测中真实发生过）\n")
    print("  %-8s %-14s %-14s" % ("连亏笔数", "账户权益", "相对初始"))
    eq = EQUITY
    for i in (1, 3, 5, 8, 10, 15, 20):
        eq = EQUITY * (Decimal("0.98") ** i)
        print("  %-8s %-14.2f %-14.1f%%" % ("%d 笔" % i, float(eq), float(eq / EQUITY * 100)))

    print("\n" + "=" * 116)


if __name__ == "__main__":
    main()
