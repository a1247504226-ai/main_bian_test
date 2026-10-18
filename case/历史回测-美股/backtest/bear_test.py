# -*- coding: utf-8 -*-
"""熊市检验：把回测拉回到 2000 年科网泡沫顶，看趋势过滤在真正的崩盘里表现如何。

三个实验：
  A. 全历史（2000-01 起，4 只）趋势 vs 买入
  B. 按牛熊段切开，逐段对比
  C. 在历史大顶当天入场，持有 1/2/3 年
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "signal"))
from daily_signal import fetch_sina            # noqa: E402
from engine import backtest, _stats            # noqa: E402

SINA_DIR = os.path.join(HERE, "data", "sina")
EM_DIR = os.path.join(HERE, "data", "em")
os.makedirs(SINA_DIR, exist_ok=True)

USE_EM = True      # True=东财后复权（1990 起、无空洞）；False=新浪（有 2009 空洞）


def _fix_breaks(d, th=0.55):
    """复权接缝用比例修正，而不是截断。

    东财后复权序列在若干处有因子切换（AAPL 2005-01-03 凭空 +91%）。
    之前 em.py 的做法是「截掉接缝之前的数据」，代价是丢掉 1990-2004 的历史。
    这里改成把接缝之前的整段按比例缩放回去，序列连续、历史也保住了。
    """
    r = d["close"].pct_change()
    bad = np.flatnonzero((r.abs() > th).values)
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = d[col].astype(float)
    for i in bad[::-1]:
        f = float(d["close"].iloc[i]) / float(d["close"].iloc[i - 1])
        # 接缝是「新基准 = 旧基准 × f」，要让接缝前与接缝后可比，
        # 接缝前的价格必须乘 f（不是除），这样 close[i]/close[i-1] 才回到 ~1。
        for col in ["open", "high", "low", "close"]:
            d.loc[:i - 1, col] = d.loc[:i - 1, col] * f
        d.loc[:i - 1, "volume"] = d.loc[:i - 1, "volume"] / f
    if len(bad):
        print(f"  复权接缝修正 {len(bad)} 处：", [str(d['date'].iloc[i].date()) for i in bad])
    return d


def get(sym):
    if USE_EM:
        p = os.path.join(EM_DIR, f"{sym}.csv")
        d = pd.read_csv(p, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
        return _fix_breaks(d)
    p = os.path.join(SINA_DIR, f"{sym}.csv")
    if os.path.exists(p):
        return pd.read_csv(p, parse_dates=["date"])
    d = fetch_sina(sym)
    d.to_csv(p, index=False, encoding="utf-8")
    return d


def curve(sym, n=200, start="2000-01-03", mode="ma"):
    d = get(sym)
    d = d[d.date >= pd.Timestamp(start) - pd.Timedelta(days=420)].reset_index(drop=True)
    if mode == "ma":
        sig = (d.close > d.close.rolling(n).mean()).astype(float).fillna(0).values
    else:
        sig = np.ones(len(d))
    r = backtest(d, sig, fee=0.0, slip=0.0005)
    eq = pd.Series(np.asarray(r.equity, float), index=pd.DatetimeIndex(d.date))
    eq = eq[eq.index >= pd.Timestamp(start)]
    return eq / eq.iloc[0] * 10000.0


def port(syms, start, n=200, mode="ma"):
    cs = {s: curve(s, n, start, mode) for s in syms}
    df = pd.DataFrame(cs).ffill().dropna()
    return df.sum(axis=1) / len(syms)


def seg(eq, a, b):
    """段内表现：段初到段末的收益、段内最大回撤。"""
    x = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b))]
    if len(x) < 2:
        return None
    v = x.values.astype(float)
    dd = (v / np.maximum.accumulate(v) - 1).min()
    return dict(ret=v[-1] / v[0] - 1, mdd=float(dd), n=len(x))


SEGS = [
    ("科网崩盘", "2000-03-24", "2002-10-09"),
    ("复苏牛", "2002-10-09", "2007-10-09"),
    ("金融危机", "2007-10-09", "2009-03-09"),
    ("长牛", "2009-03-09", "2020-02-19"),
    ("疫情崩", "2020-02-19", "2020-03-23"),
    ("放水牛", "2020-03-23", "2021-11-19"),
    ("加息熊", "2021-11-19", "2022-12-28"),
    ("AI 牛", "2022-12-28", "2026-10-16"),
]

TOPS = [
    ("2000-03-24 科网顶", "2000-03-24"),
    ("2007-10-09 危机前顶", "2007-10-09"),
    ("2021-11-19 放水顶", "2021-11-19"),
]


def main():
    print("=" * 78)
    print("A. 全历史：2000-01-03 起（科网泡沫顶附近入场）")
    print("=" * 78)
    sets = [
        ("4 只（AAPL MSFT NVDA AMZN）", ["AAPL", "MSFT", "NVDA", "AMZN"], "2000-01-03"),
        ("5 只（+NFLX，2002-05 起）", ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"], "2002-05-23"),
        ("5 只（2006-01 起，报告口径）", ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"], "2006-01-03"),
    ]
    for name, syms, st in sets:
        tr = port(syms, st, mode="ma")
        bh = port(syms, st, mode="bh")
        a, b = _stats(tr.values, None), _stats(bh.values, None)
        print(f"\n{name}   {st} ~ {tr.index[-1].date()}  ({len(tr)/252:.1f} 年)")
        print(f"  趋势  {a['total']:9.2f}x  年化 {a['cagr']*100:6.2f}%  MDD {a['mdd']*100:7.1f}%  Cal {a['calmar']:.2f}")
        print(f"  买入  {b['total']:9.2f}x  年化 {b['cagr']*100:6.2f}%  MDD {b['mdd']*100:7.1f}%  Cal {b['calmar']:.2f}")

    print()
    print("=" * 78)
    print("B. 按牛熊段切开（4 只，2000-01 起）")
    print("=" * 78)
    tr = port(["AAPL", "MSFT", "NVDA", "AMZN"], "2000-01-03", mode="ma")
    bh = port(["AAPL", "MSFT", "NVDA", "AMZN"], "2000-01-03", mode="bh")
    print(f"{'区间':<12}{'起止':<26}{'趋势':>10}{'买入':>10}{'差':>9}{'趋势MDD':>10}{'买入MDD':>10}")
    for name, a, b in SEGS:
        s1, s2 = seg(tr, a, b), seg(bh, a, b)
        if not s1:
            continue
        print(f"{name:<12}{a}~{b[2:]:<14}{s1['ret']*100:>9.1f}%{s2['ret']*100:>9.1f}%"
              f"{(s1['ret']-s2['ret'])*100:>+8.1f}pp{s1['mdd']*100:>9.1f}%{s2['mdd']*100:>9.1f}%")

    print()
    print("=" * 78)
    print("C. 在历史大顶当天入场，持有 N 年")
    print("=" * 78)
    for label, top in TOPS:
        print(f"\n{label}")
        for yrs in (1, 2, 3):
            end = pd.Timestamp(top) + pd.Timedelta(days=365 * yrs)
            t1 = port(["AAPL", "MSFT", "NVDA", "AMZN"], top, mode="ma")
            b1 = port(["AAPL", "MSFT", "NVDA", "AMZN"], top, mode="bh")
            s1, s2 = seg(t1, top, end), seg(b1, top, end)
            if not s1:
                continue
            print(f"  {yrs} 年：趋势 {s1['ret']*100:+7.1f}%（最深 {s1['mdd']*100:6.1f}%）"
                  f"   买入 {s2['ret']*100:+7.1f}%（最深 {s2['mdd']*100:6.1f}%）"
                  f"   Δ {(s1['ret']-s2['ret'])*100:+6.1f}pp")

    # 存一份给报告用
    out = {}
    tr = port(["AAPL", "MSFT", "NVDA", "AMZN"], "2000-01-03", mode="ma")
    bh = port(["AAPL", "MSFT", "NVDA", "AMZN"], "2000-01-03", mode="bh")
    out["segs"] = []
    for name, a, b in SEGS:
        s1, s2 = seg(tr, a, b), seg(bh, a, b)
        if s1:
            out["segs"].append(dict(name=name, a=a, b=b, tr=s1, bh=s2))
    out["tops"] = []
    for label, top in TOPS:
        row = dict(label=label, top=top, yrs={})
        for yrs in (1, 2, 3):
            end = pd.Timestamp(top) + pd.Timedelta(days=365 * yrs)
            t1 = port(["AAPL", "MSFT", "NVDA", "AMZN"], top, mode="ma")
            b1 = port(["AAPL", "MSFT", "NVDA", "AMZN"], top, mode="bh")
            s1, s2 = seg(t1, top, end), seg(b1, top, end)
            if s1:
                row["yrs"][yrs] = dict(tr=s1, bh=s2)
        out["tops"].append(row)
    import json
    json.dump(out, open(os.path.join(HERE, "..", "回测结果", "bear.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=float)
    print("\n已保存 bear.json")


if __name__ == "__main__":
    main()
