# -*- coding: utf-8 -*-
"""按「9 年」尺度看最终策略收益：
1) 固定切段（从回测起点每 9 年一刀）
2) 滚动 9 年窗口（任取起点，往后 9 年），给最小/中位/最大
3) 顺便给 5 年 / 10 年尺度做参照
产物：回测结果/nine_year.json
"""
import os, json
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


def daily_returns(close):
    close = close.astype(float)
    ret = close.pct_change().fillna(0)
    ma = close.rolling(MA).mean()
    above = close > ma
    sig = np.where(ma.notna(), np.where(above, 1.0, 0.0), 0.0)
    sr = pd.Series(0.0, index=close.index)
    s = np.asarray(sig, float); rv = ret.values
    for t in range(1, len(s)):
        sr.iloc[t] = s[t - 1] * rv[t]
    return sr


def equity(sr):
    eq = np.ones(len(sr))
    for t in range(1, len(sr)):
        eq[t] = eq[t - 1] * (1 + sr.iloc[t])
    return eq


lo_df = pd.DataFrame([daily_returns(load(s)) for s in SYMS]).T.dropna(how="any")
r_LO = lo_df.mean(axis=1)
spy = load("SPY")
spy_bear = (spy < spy.rolling(MA).mean())
final = pd.Series(np.where(spy_bear.reindex(r_LO.index).fillna(False), 0.0, r_LO.values),
                  index=r_LO.index)
bh = pd.DataFrame([load(s).pct_change().fillna(0).reindex(r_LO.index).fillna(0)
                   for s in SYMS]).T.mean(axis=1)

eqs = {"最终策略": equity(final), "纯多": equity(r_LO), "买入持有": equity(bh)}
dates = pd.to_datetime(r_LO.index)
N = len(dates)
print(f"数据区间 {dates[0].date()} ~ {dates[-1].date()}，共 {N/252:.1f} 年，{N} 个交易日\n")

# ---------- 1. 固定 9 年切段 ----------
print("=" * 78)
print(" 一、固定切段（从起点每 9 年一刀）")
print("=" * 78)
print(f"{'区间':<26}{'最终策略':>10}{'纯多':>10}{'买入持有':>10}{'年数':>7}")
fixed = []
for k in range(0, 4):
    a = k * 9 * 252
    b = min((k + 1) * 9 * 252, N - 1)
    if a >= N - 30:
        break
    yrs = (b - a) / 252.0
    row = {}
    for nm, eq in eqs.items():
        row[nm] = eq[b] / eq[a]
    fixed.append(dict(start=str(dates[a].date()), end=str(dates[b].date()),
                      yrs=round(yrs, 2), **{k2: round(v, 2) for k2, v in row.items()}))
    print(f"{str(dates[a].date())}~{str(dates[b].date())}  "
          f"{row['最终策略']:9.2f}x{row['纯多']:9.2f}x{row['买入持有']:9.2f}x{yrs:7.1f}")

# ---------- 2. 滚动窗口 ----------
def roll(window_years, step_days=21):
    w = int(window_years * 252)
    out = {nm: [] for nm in eqs}
    for a in range(0, N - w, step_days):
        b = a + w
        for nm, eq in eqs.items():
            out[nm].append(eq[b] / eq[a])
    return {nm: np.array(v) for nm, v in out.items()}


print("\n" + "=" * 78)
print(" 二、滚动窗口（任取起点，持满 N 年）")
print("=" * 78)
print(f"{'持有期':<8}{'策略':<10}{'最小':>9}{'25分位':>10}{'中位':>9}{'75分位':>10}{'最大':>10}{'亏损占比':>9}")
roll_res = {}
for yrs in [5, 9, 10]:
    r = roll(yrs)
    roll_res[yrs] = {nm: dict(min=float(v.min()), p25=float(np.percentile(v, 25)),
                              med=float(np.median(v)), p75=float(np.percentile(v, 75)),
                              max=float(v.max()), loss=float((v < 1).mean()), n=len(v))
                     for nm, v in r.items()}
    for nm in ["最终策略", "纯多", "买入持有"]:
        d = roll_res[yrs][nm]
        print(f"{str(yrs)+'年':<8}{nm:<10}{d['min']:8.2f}x{d['p25']:9.2f}x{d['med']:8.2f}x"
              f"{d['p75']:9.2f}x{d['max']:9.2f}x{d['loss']*100:8.0f}%")

# ---------- 3. 9 年窗口明细：每个起点 ----------
w9 = 9 * 252
print("\n" + "=" * 78)
print(" 三、9 年窗口逐年明细（每年 1 月首个交易日为起点）")
print("=" * 78)
print(f"{'起点':<14}{'终点':<14}{'最终策略':>11}{'纯多':>10}{'买入持有':>10}")
detail = []
for yr in range(dates[0].year, dates[-1].year):
    idx = np.flatnonzero(dates >= pd.Timestamp(f"{yr}-01-01"))
    if not len(idx):
        continue
    a = idx[0]
    b = a + w9
    if b >= N:
        continue
    row = {nm: eqs[nm][b] / eqs[nm][a] for nm in eqs}
    detail.append(dict(start=str(dates[a].date()), end=str(dates[b].date()),
                       **{k: round(v, 2) for k, v in row.items()}))
    print(f"{str(dates[a].date()):<14}{str(dates[b].date()):<14}"
          f"{row['最终策略']:10.2f}x{row['纯多']:9.2f}x{row['买入持有']:9.2f}x")

json.dump({"range": [str(dates[0].date()), str(dates[-1].date())],
           "years": round(N / 252, 2), "fixed": fixed,
           "roll": {str(k): v for k, v in roll_res.items()}, "detail9": detail},
          open(os.path.join(HERE, "..", "回测结果", "nine_year.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("\n已保存 回测结果/nine_year.json")
