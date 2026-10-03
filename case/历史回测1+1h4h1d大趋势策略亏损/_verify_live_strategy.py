# -*- coding: utf-8 -*-
"""
实盘脚本逻辑一致性验证（离线，不需要网络）。

验证三件事：
  1. 实盘脚本的 sig_combined 与回测 strategies.sig_combined 逐根完全一致
  2. 实盘脚本的指标计算与回测 bt_engine 完全一致
  3. 仓位计算公式正确（风险金额 / 止损距离 = 数量）

用本地 backtest/data 的历史 K 线做输入，用假 Client 替代网络。
"""

import os
import sys
from decimal import Decimal

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "backtest"))

import binance_combined_signal_trader as live   # noqa: E402
import bt_engine as be                          # noqa: E402
import strategies as st                         # noqa: E402

DATA = os.path.join(HERE, "backtest", "data")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s %s" % ("OK" if cond else "!!", name, detail))


def load(symbol, tf):
    path = os.path.join(DATA, "spot_%s_%s.pkl" % (symbol, tf))
    df = pd.read_pickle(path)
    return df.reset_index(drop=True)


# ============================================================
# 1. 指标一致性
# ============================================================

def test_indicators():
    print("\n[1] 指标一致性（实盘 vs 回测）")
    df = load("BTCUSDT", "15m").tail(800).reset_index(drop=True)
    h, l, c = df["high"], df["low"], df["close"]

    a_live = live.add_indicators(df.copy())
    a_bt = be.add_indicators(df.copy())

    for col in ("ATR", "ADX"):
        d = np.nanmax(np.abs(a_live[col].values - a_bt[col].values))
        check("指标 %s 一致" % col, d < 1e-9, "最大偏差 %.2e" % d)

    for col in ("ema20", "ema60", "pdi", "mdi"):
        src = st.add_extra(df.copy())
        d = np.nanmax(np.abs(a_live[col].values - src[col].values))
        check("指标 %s 一致" % col, d < 1e-9, "最大偏差 %.2e" % d)


# ============================================================
# 2. 信号一致性
# ============================================================

def test_signal():
    print("\n[2] 信号一致性（实盘 get_signal 的算法 vs 回测 sig_combined）")
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        d15 = load(symbol, "15m")
        d1h = load(symbol, "1h")
        d4h = load(symbol, "4h")
        d1d = load(symbol, "1d")

        # --- 实盘路径：build_signal_frame + sig_combined ---
        live_frame = live.build_signal_frame(
            live.add_indicators(d15), d1h, d4h, d1d)
        live_sig = live.sig_combined(live_frame)

        # --- 回测路径：prepare_full + strategies.sig_combined ---
        data = {"5m": load(symbol, "5m"), "15m": d15, "1h": d1h,
                "4h": d4h, "1d": d1d}
        import run_backtest as rb
        bt_frame, _ = st.prepare_full(data, "15m")
        bt_sig = st.sig_combined(bt_frame)

        n = min(len(live_sig), len(bt_sig))
        # 对齐尾部（两边长度可能差 1-2 根，按 close_time 对齐）
        lf = live_frame.tail(n).reset_index(drop=True)
        bf = bt_frame.tail(n).reset_index(drop=True)

        same_len = len(lf) == len(bf)
        check("%s 帧长度可对齐" % symbol, same_len,
              "live=%d bt=%d" % (len(live_frame), len(bt_frame)))

        # 只比较 close_time 相同的部分
        common = np.intersect1d(lf["close_time"].values, bf["close_time"].values)
        li = {t: i for i, t in enumerate(lf["close_time"].values)}
        bi = {t: i for i, t in enumerate(bf["close_time"].values)}
        a = np.array([live_sig[li[t]] for t in common])
        b = np.array([bt_sig[bi[t]] for t in common])

        mismatch = int((a != b).sum())
        check("%s 信号逐根一致" % symbol, mismatch == 0,
              "比对 %d 根，不一致 %d 根，信号数 live=%d bt=%d"
              % (len(common), mismatch, int((a != 0).sum()), int((b != 0).sum())))

        # 附带：信号出现频率是否合理（不是 0 也不是每根都有）
        rate = float((a != 0).mean())
        check("%s 信号频率合理" % symbol, 0.0 < rate < 0.35,
              "%.2f%%" % (rate * 100))


# ============================================================
# 3. 仓位计算
# ============================================================

class FakeClient(object):
    """只提供 filters / 余额的假客户端，用于验证仓位数学"""

    def __init__(self, equity=10000.0):
        self.equity = equity

    def get_filters(self, symbol):
        return {"symbol": symbol, "step": Decimal("0.001"), "tick": Decimal("0.1"),
                "min_qty": Decimal("0.001"), "market_step": Decimal("0.001"),
                "market_min_qty": Decimal("0.001"), "min_notional": Decimal("5"),
                "pricePrecision": 1, "quantityPrecision": 3}

    def get_available_balance(self, asset="USDT"):
        return Decimal(str(self.equity))

    def format_qty(self, symbol, qty, for_market=True):
        return live.floor_step(qty, Decimal("0.001"))

    def format_price(self, symbol, price):
        return live.floor_step(price, Decimal("0.1"))


def test_position():
    print("\n[3] 仓位计算（风险反推数量）")
    c = FakeClient(10000.0)

    # 场景：权益 10000，风险 2% = 200 USDT，ATR=200，止损 5ATR=1000 点
    plan = live.calc_position(c, "BTCUSDT", "BUY",
                              entry_price=Decimal("50000"),
                              atr=Decimal("200"),
                              equity=Decimal("10000"))
    check("止损距离 = ATR×5", plan["sl_distance"] == Decimal("1000"),
          "%s" % plan["sl_distance"])
    # qty = 200 / 1000 = 0.2
    check("数量 = 风险/止损距离", plan["qty"] == Decimal("0.200"), "%s" % plan["qty"])
    check("止损价 = 50000-1000", plan["sl_price"] == Decimal("49000.0"), "%s" % plan["sl_price"])
    check("止盈价 = 50000+1600", plan["tp_price"] == Decimal("51600.0"), "%s" % plan["tp_price"])
    check("止盈/止损 = 8/5 = 1.6", abs(plan["rr"] - 1.6) < 1e-9, "%.3f" % plan["rr"])
    # 实际风险 = 0.2 * 1000 = 200
    check("实际风险 = 200 USDT", plan["actual_risk"] == Decimal("200.000"),
          "%s" % plan["actual_risk"])
    check("实际风险占权益 2%", abs(float(plan["risk_pct_actual"]) - 0.02) < 1e-6,
          "%.4f%%" % (float(plan["risk_pct_actual"]) * 100))
    check("名义价值 = 0.2×50000 = 10000", plan["notional"] == Decimal("10000.000"),
          "%s" % plan["notional"])

    # 做空方向
    ps = live.calc_position(c, "BTCUSDT", "SELL", Decimal("50000"), Decimal("200"),
                            Decimal("10000"))
    check("做空止损在上方", ps["sl_price"] > Decimal("50000"), "%s" % ps["sl_price"])
    check("做空止盈在下方", ps["tp_price"] < Decimal("50000"), "%s" % ps["tp_price"])

    # ATR 变小 -> 数量变大，但风险金额不变（这是风险模型的核心）
    p2 = live.calc_position(c, "BTCUSDT", "BUY", Decimal("50000"), Decimal("100"),
                            Decimal("10000"))
    check("ATR 减半则数量翻倍", p2["qty"] == Decimal("0.400"), "%s" % p2["qty"])
    check("ATR 变化但风险金额不变", p2["actual_risk"] == Decimal("200.000"),
          "%s" % p2["actual_risk"])

    # 保证金约束：杠杆过低时名义价值会被压缩到可用保证金的范围内
    small = FakeClient(500.0)
    p3 = live.calc_position(small, "BTCUSDT", "BUY", Decimal("50000"), Decimal("200"),
                            Decimal("500"), leverage=1)
    # 期望 qty=0.01 -> notional=500，但可用保证金 500×0.9×1=450 -> 压缩到 450
    check("低杠杆触发保证金压缩", p3["clamped"] is True,
          "名义 %s 保证金 %s" % (p3["notional"], p3["margin"]))
    check("压缩后名义价值 <= 可用×0.9×杠杆", p3["notional"] <= Decimal("450.001"),
          "%s" % p3["notional"])

    # 风险比例线性：1% 风险 -> 一半数量
    p4 = live.calc_position(c, "BTCUSDT", "BUY", Decimal("50000"), Decimal("200"),
                            Decimal("10000"), risk_pct=Decimal("0.01"))
    check("风险减半则数量减半", p4["qty"] == Decimal("0.100"), "%s" % p4["qty"])

    # 真实场景：2% 风险 + 5ATR 止损，10x 杠杆足够，不应触发压缩
    real = live.calc_position(c, "BTCUSDT", "BUY", Decimal("50000"), Decimal("100"),
                              Decimal("10000"))
    check("10x 杠杆下 2% 风险不压缩", real["clamped"] is False,
          "实际杠杆 %.2fx" % real["leverage_needed"])
    check("实际杠杆约 2x（2%风险 / 1%止损）",
          1.5 < real["leverage_needed"] < 2.5, "%.2fx" % real["leverage_needed"])


# ============================================================
# 4. 精度工具
# ============================================================

def test_precision():
    print("\n[4] 精度与格式化")
    check("floor_step 消除浮点尾数",
          live.floor_step(Decimal("0.3000000000000000001"), Decimal("0.001"))
          == Decimal("0.300"), "")
    check("floor_step 向下取整",
          live.floor_step(Decimal("1.2345"), Decimal("0.01")) == Decimal("1.23"), "")
    check("decimal_to_str 无科学计数法",
          "E" not in live.decimal_to_str(Decimal("0.00000123")).upper(),
          live.decimal_to_str(Decimal("0.00000123")))
    check("gen_client_id 长度合规",
          len(live.gen_client_id("open")) <= 36, live.gen_client_id("open"))


if __name__ == "__main__":
    print("=" * 90)
    print("  实盘脚本一致性验证")
    print("=" * 90)
    test_indicators()
    test_signal()
    test_position()
    test_precision()
    print("\n" + "=" * 90)
    print("  通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print("    - %s" % f)
    print("=" * 90)
    sys.exit(1 if FAIL else 0)
