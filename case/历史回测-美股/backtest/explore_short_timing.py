# -*- coding: utf-8 -*-
"""探索「做多主策略不变 + 选择性做空」的做空时机。

做多腿：站上 MA200 → +1（不变）。
做空叠加：跌破 MA200 时，仅当「做空时机条件」成立才 −1，否则继续 0（空仓）。

候选时机：
  LO       纯多（基线，永不空）
  any      跌破就空（之前测过，最差）
  ma_slope 跌破 + MA200 下行（确认下跌趋势）
  death    跌破 + MA50<MA200（死叉）
  deep5    跌破 + 价格低于均线 5%+
  deep10   跌破 + 低于均线 10%+
  deep15   跌破 + 低于均线 15%+
  broad    跌破 + SPY 也跌破其 MA200（大盘共振）

做空计 1%/年借息。5 只等权东财后复权全历史。
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EM_DIR = os.path.join(HERE, "data", "em")
SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]
MA = 200
BORROW = 0.01  # 年借息


def load(sym):
    d = pd.read_csv(os.path.join(EM_DIR, f"{sym}.csv"), parse_dates=["date"])
    d = d.sort_values("date").reset_index(drop=True)
    r = d["close"].pct_change()
    bad = np.flatnonzero((r.abs() > 0.55).values)
    for col in ["open", "high", "low", "close"]:
        d[col] = d[col].astype(float)
    for i in bad[::-1]:
        f = float(d["close"].iloc[i]) / float(d["close"].iloc[i - 1])
        d.loc[:i - 1, col] = d.loc[:i - 1, col] * f
    return d.set_index("date")


def ma_slope_down(close):
    ma = close.rolling(MA).mean()
    return (ma - ma.shift(20)) < 0


def spy_below():
    s = load("SPY")["close"]
    ma = s.rolling(MA).mean()
    return (s < ma)


def short_mask(close, variant, X, spy_mask_series):
    ma = close.rolling(MA).mean()
    above = close > ma
    below = (~above) & ma.notna()
    if variant == "any":
        return below
    if variant == "ma_slope":
        return below & ma_slope_down(close)
    if variant == "death":
        ma50 = close.rolling(50).mean()
        return below & (ma50 < ma)
    if variant.startswith("deep"):
        depth = close / ma - 1
        return below & (depth < -X)
    if variant == "broad":
        sm = spy_mask_series.reindex(close.index).ffill().fillna(False)
        return below & sm
    return pd.Series(False, index=close.index)


def signal(close, variant, X, spy):
    ma = close.rolling(MA).mean()
    above = close > ma
    mask = short_mask(close, variant, X, spy) if variant != "LO" else pd.Series(False, index=close.index)
    sig = np.where(above.values, 1.0, np.where(mask.values, -1.0, 0.0))
    return sig


def backtest_equity(close, sig):
    ret = close.pct_change().fillna(0).values
    n = len(ret)
    daily_borrow = BORROW / 252.0
    eq = np.ones(n)
    for t in range(1, n):
        pos = sig[t - 1]
        r = pos * ret[t]
        if pos < 0:
            r -= daily_borrow * abs(pos)
        eq[t] = eq[t - 1] * (1 + r)
    return eq


def stats(eq):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    yrs = len(eq) / 252.0
    tot = eq[-1] / eq[0]
    cagr = tot ** (1 / yrs) - 1
    mdd = float(dd.min())
    return tot, cagr, mdd, cagr / abs(mdd) if mdd < 0 else 0


def portfolio(variant, X, spy):
    eqs = []
    for sym in SYMS:
        d = load(sym)
        close = d["close"]
        sig = signal(close, variant, X, spy)
        eq = backtest_equity(close, sig)
        eqs.append(pd.Series(eq, index=d.index))
    df = pd.DataFrame({s: eqs[i] for i, s in enumerate(SYMS)}).dropna(how="any")
    return df.mean(axis=1).values, df.index.values


spy = spy_below()

variants = [("LO", 0.0), ("any", 0.0), ("ma_slope", 0.0), ("death", 0.0),
            ("deep5", 0.05), ("deep10", 0.10), ("deep15", 0.15), ("broad", 0.0)]

print("=" * 96)
print(" 做多主策略 + 选择性做空叠加：不同做空时机的 5 只等权组合表现（含 1%/年借息）")
print("=" * 96)
print(f"{'时机规则':<12}{'总倍数':>9}{'年化':>9}{'最大回撤':>11}{'Calmar':>9}{'空头占用':>11}   说明")
print("-" * 96)
rows = []
for name, X in variants:
    eq, dates = portfolio(name, X, spy)
    tot, cagr, mdd, cal = stats(eq)
    # 空头占用 = 平均有多少比例时间在做空（用 AAPL 信号近似）
    d = load("AAPL"); sig = signal(d["close"], name, X, spy)
    short_occ = float(np.mean(sig < 0))
    note = {
        "LO": "永不空（基线）", "any": "跌破就空", "ma_slope": "跌破+MA200下行",
        "death": "跌破+死叉", "deep5": "跌破+低均线5%+", "deep10": "跌破+低均线10%+",
        "deep15": "跌破+低均线15%+", "broad": "跌破+SPY共振",
    }[name]
    rows.append((name, tot, cagr, mdd, cal, short_occ, note))
    print(f"{name:<12}{tot:9.1f}x{cagr*100:8.1f}%{mdd*100:10.1f}%{cal:9.2f}{short_occ*100:10.1f}%   {note}")

print("\n" + "=" * 96)
print(" 熊市段组合收益对比（做多LO vs 最优叠加 vs 买入BH 见上表）")
print("=" * 96)
# 取 LO 与各叠加的熊段
def per_ret(eq, dates, a, b):
    dts = pd.to_datetime(dates)
    m = (dts >= pd.Timestamp(a)) & (dts <= pd.Timestamp(b))
    idx = np.flatnonzero(m)
    return eq[idx[-1]] / eq[idx[0]] - 1 if len(idx) > 1 else None

segs = [("科网崩盘 00-03~02-10", "2000-03-01", "2002-10-31"),
        ("金融危机 07-10~09-03", "2007-10-01", "2009-03-31"),
        ("疫情崩盘 20-02~20-03", "2020-02-15", "2020-03-23"),
        ("加息熊市 21-11~22-12", "2021-11-01", "2022-12-31")]
print(f"{'阶段':<26}" + "".join(f"{n[0]:>12}" for n in variants))
for name, a, b in segs:
    line = f"  {name:<24}"
    for vn, X in variants:
        eq, dates = portfolio(vn, X, spy)
        line += f"{(per_ret(eq, dates, a, b)*100):>11.1f}%"
    print(line)

# 最优叠加曲线保存
best = max(rows, key=lambda r: r[4])  # 按 Calmar
print(f"\nCalmar 最高：{best[0]}  ({best[6]})  Calmar={best[4]:.2f}")
import json
out = {"variants": [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]}
json.dump(out, open(os.path.join(HERE, "..", "回测结果", "short_timing.json"), "w", encoding="utf-8"))
print("已保存 回测结果/short_timing.json")
