# -*- coding: utf-8 -*-
"""回答两个问题：
1) 320x/649x 跨多少年？
2) 用户提议：SPY 熊时「全清仓」或「全空」，是不是更优？
"""
import os, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EM_DIR = os.path.join(HERE, "data", "em")
SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]
MA = 200; BORROW = 0.01


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
    close = close.astype(float)
    ret = close.pct_change().fillna(0)
    ma = close.rolling(MA).mean()
    above = close > ma
    if mode == "long":
        sig = np.where(ma.notna(), np.where(above, 1.0, 0.0), 0.0)
    else:
        sig = np.where(ma.notna(), np.where(above, 0.0, -1.0), 0.0)
    sr = pd.Series(0.0, index=close.index)
    db = BORROW / 252.0
    s = np.asarray(sig, float); rv = ret.values
    for t in range(1, len(s)):
        pos = s[t - 1]; r = pos * rv[t]
        if pos < 0: r -= db
        sr.iloc[t] = r
    return sr, pd.Series(sig, index=close.index)


def stats(eq):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq); dd = eq / peak - 1
    yrs = len(eq) / 252.0; tot = eq[-1] / eq[0]; cagr = tot ** (1 / yrs) - 1
    mdd = float(dd.min())
    return tot, cagr, mdd, cagr / abs(mdd) if mdd < 0 else 0


def equity(sr):
    eq = np.ones(len(sr))
    for t in range(1, len(sr)): eq[t] = eq[t - 1] * (1 + sr.iloc[t])
    return eq


# 5 只做多策略组合日收益
lo_srs = [daily_returns(load(s), "long")[0] for s in SYMS]
lo_df = pd.DataFrame(lo_srs).T.dropna(how="any")
r_LO = lo_df.mean(axis=1)
spy = load("SPY")
r_H, sig_H = daily_returns(spy, "short_bear")
common = r_LO.index.intersection(r_H.index)
r_LO = r_LO.reindex(common).fillna(0)
r_H = r_H.reindex(common).fillna(0)
sig_H = sig_H.reindex(common).fillna(0)
spy_below = sig_H.values < 0

print("=" * 90)
print(f" 跨度：{common[0].date()} ~ {common[-1].date()}  共 {len(common)/252:.1f} 年")
print("=" * 90)

# 方案
# A: SPY 熊 → 全清仓（100% 现金）
r_A = pd.Series(np.where(spy_below, 0.0, r_LO.values), index=common)
# B: SPY 熊 → 全空（100% 做空 SPY）
r_B = r_H  # short SPY below MA200 else 0
# C: 动态对冲（之前最优，SPY 熊切 40% 做空）用作对照
w = np.where(spy_below, 0.4, 0.0)
r_C = pd.Series((1 - w) * r_LO.values + w * r_H.values, index=common)

def per_ret(eq, a, b):
    dts = pd.to_datetime(common)
    m = (dts >= pd.Timestamp(a)) & (dts <= pd.Timestamp(b))
    idx = np.flatnonzero(m)
    return eq[idx[-1]] / eq[idx[0]] - 1 if len(idx) > 1 else None

print(f"{'方案':<34}{'总倍数':>9}{'年化':>9}{'最大回撤':>11}{'Calmar':>9}")
print("-" * 90)
for name, r in [("纯多LO（基线，不理SPY）", r_LO),
                ("A. SPY熊→全清仓", r_A),
                ("B. SPY熊→全空(SPY)", r_B),
                ("C. 动态对冲(SPY熊切40%空)", r_C)]:
    eq = equity(r); tot, cagr, mdd, cal = stats(eq)
    print(f"{name:<34}{tot:9.1f}x{cagr*100:8.1f}%{mdd*100:10.1f}%{cal:9.2f}")

print("\n" + "=" * 90)
print(" 熊市段收益")
print("=" * 90)
segs = [("科网崩盘 00-03~02-10","2000-03-01","2002-10-31"),
        ("金融危机 07-10~09-03","2007-10-01","2009-03-31"),
        ("疫情崩盘 20-02~20-03","2020-02-15","2020-03-23"),
        ("加息熊市 21-11~22-12","2021-11-01","2022-12-31")]
print(f"{'阶段':<26}{'纯多LO':>10}{'全清仓A':>11}{'全空B':>11}{'动态C':>11}")
for nm,a,b in segs:
    print(f"  {nm:<24}{(per_ret(equity(r_LO),a,b)*100):>9.1f}%"
          f"{(per_ret(equity(r_A),a,b)*100):>10.1f}%"
          f"{(per_ret(equity(r_B),a,b)*100):>10.1f}%"
          f"{(per_ret(equity(r_C),a,b)*100):>10.1f}%")

# 在 SPY 熊时段里，全清仓A 与 动态C 谁更能扛？统计 SPY 熊期间两策略收益
bear_mask = spy_below
eqA = equity(r_A); eqC = equity(r_C)
# 逐段熊市（连续 below 段）取收益
print("\nSPY 熊市期间（below MA200 整段）收益：")
segs_bear = []
start = None
for i,v in enumerate(bear_mask):
    if v and start is None: start = i
    if (not v or i==len(bear_mask)-1) and start is not None:
        end = i if not v else i
        segs_bear.append((start,end)); start=None
for s,e in segs_bear[-6:]:
    ra = eqA[e]/eqA[s]-1; rc = eqC[e]/eqC[s]-1
    print(f"  {common[s].date()}~{common[e].date()}  全清仓A {(ra*100):+.1f}%   动态C {(rc*100):+.1f}%")
