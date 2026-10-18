# -*- coding: utf-8 -*-
"""做多 vs 做空：把「跌破 MA200 空仓」改成「跌破 MA200 做空」，看差多少。

三种模式（信号同源：收盘站上 MA200 = +1，跌破 = 0 / -1）：
  LO  : long-only   上方 +1，下方 0（现金）—— 当前实盘策略
  LS  : long-short  上方 +1，下方 -1（做空）
  BH  : buy&hold    全程 +1

另给 LS 加一个「做空借贷成本」版本（年化 1% 计在空头敞口上），看真实摩擦。

数据：backtest/data/em 后复权（与熊市检验同源，全历史无空洞）。
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EM_DIR = os.path.join(HERE, "data", "em")
SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]
MA = 200


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
    return d


def stats(eq):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    yrs = len(eq) / 252.0
    tot = eq[-1] / eq[0]
    cagr = tot ** (1 / yrs) - 1
    mdd = float(dd.min())
    return dict(total=tot, cagr=cagr, mdd=mdd, calmar=cagr / abs(mdd) if mdd < 0 else 0)


def strat_returns(ret, sig, borrow_annual=0.0):
    """sig[t] 是 t 日收盘确认的头寸（用 sig[t-1] 持有到 t）。"""
    n = len(ret)
    sr = np.zeros(n)
    daily_borrow = borrow_annual / 252.0
    for t in range(1, n):
        pos = sig[t - 1]
        r = pos * ret[t]
        if pos < 0:
            r -= daily_borrow * abs(pos)
        sr[t] = r
    return sr


def equity(sr):
    eq = np.ones(len(sr))
    for t in range(1, len(sr)):
        eq[t] = eq[t - 1] * (1 + sr[t])
    return eq


def signal(close, mode):
    """mode='LO' 上方+1/下方0(现金)；mode='LS' 上方+1/下方-1(做空)。"""
    ma = close.rolling(MA).mean().values
    above = (close.values > ma)
    below = 0.0 if mode == "LO" else -1.0
    sig = np.where(np.isnan(ma), 0.0, np.where(above, 1.0, below))
    return sig.astype(float)


def period_ret(eq, dates, start, end):
    m = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    idx = np.flatnonzero(m)
    if len(idx) < 2:
        return None
    return eq[idx[-1]] / eq[idx[0]] - 1


# ---------- 逐标的 ----------
rows = []
equities = {}  # sym -> {mode: (dates, eq)}
common_mode = ["LO", "LS", "LSb", "BH"]
for sym in SYMS:
    d = load(sym)
    close = d["close"]
    ret = close.pct_change().fillna(0).values
    dates = d["date"]
    res = {}
    for mode in ["LO", "LS"]:
        sig = signal(close, mode)
        sr = strat_returns(ret, sig, borrow_annual=0.0)
        res[mode] = equity(sr)
    sig = signal(close, "LS")
    sr_b = strat_returns(ret, sig, borrow_annual=0.01)
    res["LSb"] = equity(sr_b)
    sr = strat_returns(ret, np.ones(len(ret)))
    res["BH"] = equity(sr)
    equities[sym] = {m: pd.Series(res[m], index=dates) for m in common_mode}
    sLO, sLS, sLSb, sBH = stats(res["LO"]), stats(res["LS"]), stats(res["LSb"]), stats(res["BH"])
    rows.append((sym, sLO, sLS, sLSb, sBH))

# ---------- 等权组合（5 只平均归一化权益，按共同交易日对齐） ----------
def portfolio(mode):
    df = pd.DataFrame({s: equities[s][mode] for s in SYMS})
    df = df.dropna(how="any")          # 取 5 只都有数据的交集区间
    return df.mean(axis=1).values

pLO, pLS, pLSb, pBH = portfolio("LO"), portfolio("LS"), portfolio("LSb"), portfolio("BH")
dates_all = pd.DataFrame({s: equities[s]["LO"] for s in SYMS}).dropna(how="any").index.values

print("=" * 92)
print(" 美股 MA200 趋势：做多 vs 做空  （5 只等权组合，后复权全历史）")
print("=" * 92)
fmt = lambda s: f"{s['total']:8.1f}x  年化{s['cagr']*100:6.1f}%  回撤{s['mdd']*100:7.1f}%  Calmar{s['calmar']:5.2f}"
print(f"{'组合':<10}{'做多LO':<26}{'做空LS':<26}{'做空+借息LSb':<26}{'买入BH':<20}")
print(f"{'':<10}{fmt(stats(pLO)):<26}{fmt(stats(pLS)):<26}{fmt(stats(pLSb)):<26}{fmt(stats(pBH)):<20}")
print("-" * 92)
print("逐标的：")
for sym, sLO, sLS, sLSb, sBH in rows:
    print(f"  {sym:<6} LO {sLO['total']:8.1f}x/ Cal{sLO['calmar']:4.2f}   "
          f"LS {sLS['total']:8.1f}x/ Cal{sLS['calmar']:4.2f}   "
          f"LSb{sLSb['total']:8.1f}x/ Cal{sLSb['calmar']:4.2f}   "
          f"BH {sBH['total']:8.1f}x/ Cal{sBH['calmar']:4.2f}")

print("\n" + "=" * 92)
print(" 熊市段组合收益（做多LO vs 做空LS vs 买入BH）")
print("=" * 92)
segs = [
    ("科网崩盘 2000-03~2002-10", "2000-03-01", "2002-10-31"),
    ("金融危机 2007-10~2009-03", "2007-10-01", "2009-03-31"),
    ("疫情崩盘 2020-02~2020-03", "2020-02-15", "2020-03-23"),
    ("加息熊市 2021-11~2022-12", "2021-11-01", "2022-12-31"),
]
print(f"{'阶段':<28}{'LO':>10}{'LS':>12}{'BH':>12}")
for name, a, b in segs:
    lo = period_ret(pLO, dates_all, a, b)
    ls = period_ret(pLS, dates_all, a, b)
    bh = period_ret(pBH, dates_all, a, b)
    print(f"  {name:<26}{(lo*100):>9.1f}%{(ls*100):>11.1f}%{(bh*100):>11.1f}%")

# 在场时间 / 空头暴露
sigLO = signal(load(SYMS[0])["close"], "LO")
sigLS = signal(load(SYMS[0])["close"], "LS")
print("\n持仓结构（以 AAPL 信号为例）：")
print(f"  做多LO：在场(多头) {np.mean(sigLO>0)*100:.1f}%  空仓 {np.mean(sigLO==0)*100:.1f}%")
print(f"  做空LS：多头 {np.mean(sigLS>0)*100:.1f}%  空头 {np.mean(sigLS<0)*100:.1f}%")

# 保存组合曲线供画图
import json
out = {
    "dates": [str(pd.Timestamp(d).date()) for d in dates_all],
    "LO": portfolio("LO").tolist(),
    "LS": portfolio("LS").tolist(),
    "LSb": portfolio("LSb").tolist(),
    "BH": portfolio("BH").tolist(),
}
json.dump(out, open(os.path.join(HERE, "..", "回测结果", "longshort.json"), "w", encoding="utf-8"))
print("\n已保存 回测结果/longshort.json")
