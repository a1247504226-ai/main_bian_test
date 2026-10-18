# -*- coding: utf-8 -*-
"""把报告需要的全部数字算出来，落成 facts3.json（报告只做渲染，不再算数）。"""
import os
import json
import numpy as np
import pandas as pd
from engine import backtest, _stats
from em import load, EM, CN, port, stats, ma_sig, COST
from diagnose import atr

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "回测结果", "facts3.json")

CORE5 = ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]
CORE6 = CORE5 + ["GOOGL"]
START5, START6 = "2006-01-03", "2008-04-01"
MAIN_N = 200


def pack(s):
    return dict(total=s["total"], cagr=s["cagr"], mdd=s["mdd"], calmar=s["calmar"],
                sharpe=s["sharpe"], longest=int(s["longest_dd"]))


def const_port(syms, start, fixed=None, vt=None, rebal=21):
    curves = {}
    for s in syms:
        d = load(s)
        s0 = pd.Timestamp(start) - pd.Timedelta(days=420)
        d = d[d.date >= s0].reset_index(drop=True)
        if fixed is not None:
            r = backtest(d, np.ones(len(d)), fixed_expo=fixed, rebal=rebal, **COST)
        else:
            r = backtest(d, np.ones(len(d)), vol_target=vt, vol_cap=1.0, rebal=rebal, **COST)
        ser = pd.Series(np.asarray(r.equity, float), index=pd.DatetimeIndex(d.date))
        ser = ser[~ser.index.duplicated(keep="last")]
        ser = ser[ser.index >= pd.Timestamp(start)]
        curves[s] = ser / ser.iloc[0] * 10000.0
    df = pd.DataFrame(curves).ffill().dropna()
    return df.sum(axis=1) / len(syms)


def yearly(tr, bh):
    y1 = tr.groupby(tr.index.year).agg(["first", "last"])
    y2 = bh.groupby(bh.index.year).agg(["first", "last"])
    t = pd.DataFrame({"tr": (y1["last"] / y1["first"] - 1) * 100,
                      "bh": (y2["last"] / y2["first"] - 1) * 100})
    t["d"] = t["tr"] - t["bh"]
    return t


def main():
    out = {}
    bh = port(CORE5, MAIN_N, 1.0, START5, "bh")
    tr = port(CORE5, MAIN_N, 1.0, START5, "ma")
    out["main"] = dict(syms=CORE5, start=START5, first=str(tr.index[0].date()),
                       last=str(tr.index[-1].date()), years=len(tr) / 252,
                       ma=MAIN_N, tr=pack(stats(tr)), bh=pack(stats(bh)))

    # 五路对照
    ex = []
    for s in CORE5:
        d = load(s)
        s0 = pd.Timestamp(START5) - pd.Timedelta(days=420)
        d = d[d.date >= s0].reset_index(drop=True)
        cut = int((d.date < pd.Timestamp(START5)).sum())
        r = backtest(d, ma_sig(d, MAIN_N), vol_target=1.0, vol_cap=1.0, **COST)
        ex.append(float(np.asarray(r.exposure)[cut:].mean()))
    ex_avg = float(np.mean(ex))
    out["expo_avg"] = ex_avg
    out["variants"] = [
        dict(k="买入持有", d="第一天满仓，什么都不做", s=pack(stats(bh))),
        dict(k=f"固定敞口 {ex_avg:.2f} 常驻", d="始终按 73% 仓位持有，不看趋势", s=pack(stats(const_port(CORE5, START5, fixed=ex_avg)))),
        dict(k="常驻 + 波动率目标 2.5%", d="始终持有，但按 ATR 缩放仓位", s=pack(stats(const_port(CORE5, START5, vt=0.025)))),
        dict(k="趋势过滤 + 波动率目标 2.5%", d="跌破年线空仓 + 按 ATR 缩放仓位", s=pack(stats(port(CORE5, 250, 0.025, START5, "ma")))),
        dict(k=f"趋势过滤满仓 MA{MAIN_N}", d="跌破 MA200 空仓，站上满仓", s=pack(stats(tr))),
        dict(k="趋势过滤满仓 MA250", d="跌破 MA250 空仓，站上满仓", s=pack(stats(port(CORE5, 250, 1.0, START5, "ma")))),
    ]
    for v in out["variants"]:
        print(f"  {v['k']:<26}{v['s']['total']:8.2f}x {v['s']['cagr']*100:5.1f}% "
              f"{v['s']['mdd']*100:6.1f}% Cal {v['s']['calmar']:.2f} Sh {v['s']['sharpe']:.2f}")

    # 逐年
    y = yearly(tr, bh)
    out["yearly"] = [dict(y=int(i), tr=float(r.tr), bh=float(r.bh), d=float(r.d))
                     for i, r in y.iterrows()]
    out["win_years"] = [int(i) for i in y.index[y.d > 0]]
    out["win_n"] = int((y.d > 0).sum())

    # MA 网格（满仓）
    out["ma_grid"] = []
    for n in [100, 125, 150, 175, 200, 225, 250, 275, 300]:
        s = stats(port(CORE5, n, 1.0, START5, "ma"))
        out["ma_grid"].append(dict(n=n, **pack(s)))
        print(f"  MA{n:<4} {s['total']:8.2f}x {s['cagr']*100:5.1f}% {s['mdd']*100:6.1f}% Cal {s['calmar']:.2f}")

    # 仓位缩放网格（MA200）
    out["vt_grid"] = []
    for vt in [1.0, 0.05, 0.04, 0.035, 0.030, 0.025, 0.020]:
        s = stats(port(CORE5, MAIN_N, vt, START5, "ma"))
        out["vt_grid"].append(dict(vt=vt, **pack(s)))
        print(f"  目标{vt*100:4.1f}%  {s['total']:8.2f}x {s['cagr']*100:5.1f}% {s['mdd']*100:6.1f}% Cal {s['calmar']:.2f}")

    # 起点
    out["starts"] = []
    for st in ["2006-01-03", "2010-01-04", "2013-01-02", "2016-01-04", "2020-01-02"]:
        a, b = stats(port(CORE5, MAIN_N, 1.0, st, "ma")), stats(port(CORE5, MAIN_N, 1.0, st, "bh"))
        out["starts"].append(dict(start=st, years=len(port(CORE5, MAIN_N, 1.0, st, "ma")) / 252,
                                  tr=pack(a), bh=pack(b)))
        print(f"  {st} {out['starts'][-1]['years']:5.1f}年 Cal {a['calmar']:.2f} vs {b['calmar']:.2f} "
              f"CAGR {a['cagr']*100:5.1f} vs {b['cagr']*100:5.1f}")

    # 别的篮子
    baskets = [
        ("5 只 + GOOGL", CORE6, START6),
        ("科技 ETF XLK", ["XLK"], START5),
        ("纳指 100 QQQ", ["QQQ"], START5),
        ("标普 500 SPY", ["SPY"], START5),
        ("半导体 SOXX", ["SOXX"], START5),
        ("行业 ETF × 8", ["XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "XLY"], START5),
        ("宽基 + 行业 × 5", ["SPY", "QQQ", "XLK", "XLV", "XLE"], START5),
    ]
    out["baskets"] = []
    for name, syms, st in baskets:
        a, b = stats(port(syms, MAIN_N, 1.0, st, "ma")), stats(port(syms, MAIN_N, 1.0, st, "bh"))
        out["baskets"].append(dict(name=name, n=len(syms), start=st,
                                   years=len(port(syms, MAIN_N, 1.0, st, "ma")) / 252,
                                   tr=pack(a), bh=pack(b)))
        print(f"  {name:<16}Cal {a['calmar']:.2f} vs {b['calmar']:.2f}  Δ{a['calmar']-b['calmar']:+.2f}")

    # 单标的
    out["single"] = []
    tot_tr = 0
    for s in CORE5:
        d = load(s)
        s0 = pd.Timestamp(START5) - pd.Timedelta(days=420)
        d = d[d.date >= s0].reset_index(drop=True)
        cut = int((d.date < pd.Timestamp(START5)).sum())
        r = backtest(d, ma_sig(d, MAIN_N), **COST)
        b = backtest(d, np.ones(len(d)), **COST)
        eq_t = np.asarray(r.equity)[cut:] / np.asarray(r.equity)[cut - 1] * 10000
        eq_b = np.asarray(b.equity)[cut:] / np.asarray(b.equity)[cut - 1] * 10000
        st, sb = _stats(eq_t, None), _stats(eq_b, None)
        ntr = len([t for t in r.trades if t["entry_i"] >= cut])
        tot_tr += ntr
        out["single"].append(dict(sym=s, cn=CN[s], tr=pack(st), bh=pack(sb),
                                  trades=ntr, win=float(r.win)))
        print(f"  {s:5} 趋势 {st['total']:7.2f}x/Cal {st['calmar']:.2f}  买入 {sb['total']:7.2f}x/"
              f"Cal {sb['calmar']:.2f}  {ntr} 笔")
    out["trades_total"] = tot_tr
    out["trades_per_year"] = tot_tr / out["main"]["years"]

    # 回撤区间
    v = np.asarray(tr, float)
    dd = v / np.maximum.accumulate(v) - 1
    eps, in_dd, st_i = [], False, 0
    for i in range(len(dd)):
        if dd[i] < -0.10 and not in_dd:
            in_dd, st_i = True, i
        elif dd[i] >= -0.001 and in_dd:
            in_dd = False
            eps.append((tr.index[st_i], tr.index[i], float(dd[st_i:i].min())))
    if in_dd:
        eps.append((tr.index[st_i], tr.index[-1], float(dd[st_i:].min())))
    out["dd_eps"] = [dict(a=str(a.date()), b=str(b.date()), m=m, yrs=(b - a).days / 365)
                     for a, b, m in eps]
    out["dd_n"] = len(eps)
    out["dd_worst"] = dict(m=float(dd.min()), at=str(tr.index[int(np.argmin(dd))].date()))

    # 当前状态（真实价格）
    rows = []
    for s in CORE5:
        h = load(s)
        last = h.iloc[-1]
        dev = float(last["close"] / h["close"].rolling(MAIN_N).mean().iloc[-1] - 1)
        av = float(atr(h, 14).iloc[-1] / last["close"])
        raw = pd.read_csv(os.path.join(os.path.dirname(EM), "univ", f"{s}.csv"), parse_dates=["date"])
        px = float(raw["close"].iloc[-1])
        hold = dev > 0
        rows.append(dict(sym=s, cn=CN[s], date=str(pd.Timestamp(last["date"]).date()),
                         close=px, ma=px / (1 + dev), dev=dev, atr=av, hold=hold,
                         notional=10000 / len(CORE5) if hold else 0.0))
    out["now"] = rows
    out["now_date"] = rows[0]["date"]
    out["n_hold"] = sum(1 for r in rows if r["hold"])
    out["expo_now"] = sum(r["notional"] for r in rows) / 10000

    # 全池
    vs = pd.read_csv(os.path.join(EM, "_scan.csv"))
    out["univ"] = dict(
        n=len(vs), n_pos=int((vs.d_calmar > 0).sum()), n_mc90=int((vs.mc > 0.9).sum()),
        med_mc=float(vs.mc.median()), corr_vol_cal=float(vs.vol.corr(vs.d_calmar)),
        corr_vol_mc=float(vs.vol.corr(vs.mc)),
        top=[dict(sym=r.sym, cn=r.cn, vol=float(r.vol), mc=float(r.mc), d=float(r.d_calmar),
                  bh_calmar=float(r.bh_calmar), tr_calmar=float(r.tr_calmar),
                  years=float(r.years)) for _, r in vs.nlargest(10, "d_calmar").iterrows()],
        bot=[dict(sym=r.sym, cn=r.cn, vol=float(r.vol), mc=float(r.mc), d=float(r.d_calmar),
                  bh_calmar=float(r.bh_calmar), tr_calmar=float(r.tr_calmar),
                  years=float(r.years)) for _, r in vs.nsmallest(10, "d_calmar").iterrows()],
        mc_top=[dict(sym=r.sym, cn=r.cn, vol=float(r.vol), mc=float(r.mc), d=float(r.d_calmar))
                for _, r in vs.nlargest(6, "mc").iterrows()],
        mc_bot=[dict(sym=r.sym, cn=r.cn, vol=float(r.vol), mc=float(r.mc), d=float(r.d_calmar))
                for _, r in vs.nsmallest(6, "mc").iterrows()],
        all=[dict(sym=r.sym, cn=r.cn, vol=float(r.vol), mc=float(r.mc), d=float(r.d_calmar),
                  bh_cagr=float(r.bh_cagr), tr_cagr=float(r.tr_cagr),
                  bh_mdd=float(r.bh_mdd), tr_mdd=float(r.tr_mdd),
                  years=float(r.years)) for _, r in vs.iterrows()],
    )

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    print(f"\n已保存 {OUT}")
    print(f"主方案 {out['main']['tr']['total']:.2f}x / {out['main']['tr']['cagr']*100:.1f}% / "
          f"{out['main']['tr']['mdd']*100:.1f}% / Cal {out['main']['tr']['calmar']:.2f}")
    print(f"当前持有 {out['n_hold']}/5，仓位 {out['expo_now']*100:.0f}%")


if __name__ == "__main__":
    main()
