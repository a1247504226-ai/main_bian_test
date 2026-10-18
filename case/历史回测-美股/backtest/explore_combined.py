# -*- coding: utf-8 -*-
"""验证两种「多空结合」：

A. 字面结合：5 只成长股，站上 MA200 做多、跌破 MA200 做空（空仓时段换成做空）。
B. 真·结合：核心 = 5 只成长股做多策略（站上持有·跌破空仓）；
   另加一条独立「大盘熊市对冲腿」——SPY 跌破 MA200 就做空 SPY（或切空头 ETF），
   平时这条腿空仓。两腿按权重组合。

对比：纯多LO / 纯对冲腿H / 固定 80/20 组合 / 动态对冲（SPY熊时切 40% 去空头）。
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EM_DIR = os.path.join(HERE, "data", "em")
SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]
MA = 200
BORROW = 0.01


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
    return d.set_index("date")["close"]


def daily_returns(close, mode):
    """mode='long' 站上+1/跌破0；mode='short_bear' 站上0/跌破-1。返回 (日收益序列, 信号序列) 按日期索引。"""
    close = close.astype(float)
    ret = close.pct_change().fillna(0)
    ma = close.rolling(MA).mean()
    above = close > ma
    if mode == "long":
        sig = np.where(ma.notna(), np.where(above, 1.0, 0.0), 0.0)
    else:  # short_bear
        sig = np.where(ma.notna(), np.where(above, 0.0, -1.0), 0.0)
    sr = pd.Series(0.0, index=close.index)
    db = BORROW / 252.0
    s = np.asarray(sig, float)
    rv = ret.values
    for t in range(1, len(s)):
        pos = s[t - 1]
        r = pos * rv[t]
        if pos < 0:
            r -= db
        sr.iloc[t] = r
    return sr, pd.Series(sig, index=close.index)


def stats(eq):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    yrs = len(eq) / 252.0
    tot = eq[-1] / eq[0]
    cagr = tot ** (1 / yrs) - 1
    mdd = float(dd.min())
    return tot, cagr, mdd, cagr / abs(mdd) if mdd < 0 else 0


def equity(sr):
    eq = np.ones(len(sr))
    for t in range(1, len(sr)):
        eq[t] = eq[t - 1] * (1 + sr.iloc[t])
    return eq


# ---- 5 只做多策略（LO）组合日收益 ----
lo_srs = []
for sym in SYMS:
    sr, _ = daily_returns(load(sym), "long")
    lo_srs.append(sr)
lo_df = pd.DataFrame(lo_srs).T.dropna(how="any")
r_LO = lo_df.mean(axis=1)            # 5 只等权做多策略日收益

# ---- 大盘对冲腿：SPY 跌破 MA200 做空 ----
spy = load("SPY")
r_H, sig_H = daily_returns(spy, "short_bear")

# ---- 对齐到共同交易日 ----
common = r_LO.index.intersection(r_H.index)
r_LO = r_LO.reindex(common).fillna(0)
r_H = r_H.reindex(common).fillna(0)
sig_H = sig_H.reindex(common).fillna(0)

# ---- 组合 ----
# 固定 80/20
r_8020 = 0.8 * r_LO + 0.2 * r_H
# 动态对冲：SPY 熊时把 40% 切去空头，平时 0%
w = np.where(sig_H.values < 0, 0.4, 0.0)
r_dyn = (1 - w) * r_LO.values + w * r_H.values
r_dyn = pd.Series(r_dyn, index=common)

eq_LO = equity(r_LO)
eq_H = equity(r_H)
eq_8020 = equity(r_8020)
eq_dyn = equity(r_dyn)

print("=" * 92)
print(" 多空结合验证（5 只成长股做多策略 + 大盘熊市做空 SPY 对冲腿；含 1%/年借息）")
print("=" * 92)
print(f"{'方案':<22}{'总倍数':>9}{'年化':>9}{'最大回撤':>11}{'Calmar':>9}")
print("-" * 92)
for name, eq in [("A. 字面结合(站上多·跌破空)", None),  # 见下方注释
                 ("纯多LO（基线）", eq_LO),
                 ("纯对冲腿H（SPY熊做空）", eq_H),
                 ("组合 80%LO+20%H", eq_8020),
                 ("动态对冲（SPY熊切40%）", eq_dyn)]:
    if eq is None:
        # A 已在 explore_short_timing 测过：任何 below 就空 = 41.2x/Calmar 0.28
        print(f"{name:<22}{'41.2x':>9}{'16.5%':>9}{'-59.1%':>11}{'0.28':>9}  ← 之前测过，最差")
        continue
    tot, cagr, mdd, cal = stats(eq)
    print(f"{name:<22}{tot:9.1f}x{cagr*100:8.1f}%{mdd*100:10.1f}%{cal:9.2f}")

print("\n" + "=" * 92)
print(" 熊市段组合收益（纯多LO vs 动态对冲 vs 纯对冲腿H）")
print("=" * 92)
def per_ret(eq, a, b):
    dts = pd.to_datetime(common)
    m = (dts >= pd.Timestamp(a)) & (dts <= pd.Timestamp(b))
    idx = np.flatnonzero(m)
    return eq[idx[-1]] / eq[idx[0]] - 1 if len(idx) > 1 else None
segs = [("科网崩盘 00-03~02-10","2000-03-01","2002-10-31"),
        ("金融危机 07-10~09-03","2007-10-01","2009-03-31"),
        ("疫情崩盘 20-02~20-03","2020-02-15","2020-03-23"),
        ("加息熊市 21-11~22-12","2021-11-01","2022-12-31")]
print(f"{'阶段':<26}{'纯多LO':>10}{'动态对冲':>12}{'纯对冲H':>12}")
for nm,a,b in segs:
    print(f"  {nm:<24}{(per_ret(eq_LO,a,b)*100):>9.1f}%{(per_ret(eq_dyn,a,b)*100):>11.1f}%{(per_ret(eq_H,a,b)*100):>11.1f}%")

# 保存
import json
out = {"LO":stats(eq_LO),"H":stats(eq_H),"8020":stats(eq_8020),"dyn":stats(eq_dyn),
       "dates":[str(pd.Timestamp(d).date()) for d in common],
       "eq_LO":eq_LO.tolist(),"eq_H":eq_H.tolist(),
       "eq_8020":eq_8020.tolist(),"eq_dyn":eq_dyn.tolist()}
json.dump(out, open(os.path.join(HERE,"..","回测结果","combined.json"),"w",encoding="utf-8"))
print("\n已保存 回测结果/combined.json")
