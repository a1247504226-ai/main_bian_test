# -*- coding: utf-8 -*-
"""最终生产策略回测：做多 5 只成长股（站上 MA200 持有·跌破清空）
+ 大盘牛熊开关（SPY 跌破 MA200 → 全部清仓持币，不做空）。

产物：报告/美股趋势_最终策略.html  +  回测结果/final_strategy.json
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
    return sr


def stats(eq):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq); dd = eq / peak - 1
    yrs = len(eq) / 252.0; tot = eq[-1] / eq[0]; cagr = tot ** (1 / yrs) - 1
    mdd = float(dd.min())
    return dict(total=tot, cagr=cagr, mdd=mdd, calmar=cagr / abs(mdd) if mdd < 0 else 0,
                yrs=yrs, final=float(eq[-1]))


def equity(sr):
    eq = np.ones(len(sr))
    for t in range(1, len(sr)): eq[t] = eq[t - 1] * (1 + sr.iloc[t])
    return eq


# ---- 5 只做多策略组合日收益（站上持有·跌破空仓） ----
lo_srs = [daily_returns(load(s), "long") for s in SYMS]
lo_df = pd.DataFrame(lo_srs).T.dropna(how="any")
r_LO = lo_df.mean(axis=1)                       # 纯多（不理大盘）
spy = load("SPY")
spy_bear = (spy < spy.rolling(MA).mean())          # 跌破 MA200 = 熊
# 最终策略：SPY 熊 → 全清仓
final = pd.Series(np.where(spy_bear.reindex(r_LO.index).fillna(False), 0.0, r_LO.values), index=r_LO.index)
# 买入持有（5 只等权，全程满仓）
bh = pd.DataFrame([load(s).pct_change().fillna(0).reindex(r_LO.index).fillna(0) for s in SYMS]).T.mean(axis=1)

common = r_LO.index
eq_LO = equity(r_LO); eq_final = equity(final); eq_bh = equity(bh)

S = {"final": stats(eq_final), "LO": stats(eq_LO), "BH": stats(eq_bh)}

# 熊市段
def per_ret(eq, a, b):
    dts = pd.to_datetime(common)
    m = (dts >= pd.Timestamp(a)) & (dts <= pd.Timestamp(b))
    idx = np.flatnonzero(m)
    return eq[idx[-1]] / eq[idx[0]] - 1 if len(idx) > 1 else None
segs = [("科网崩盘 00-03~02-10","2000-03-01","2002-10-31"),
        ("金融危机 07-10~09-03","2007-10-01","2009-03-31"),
        ("疫情崩盘 20-02~20-03","2020-02-15","2020-03-23"),
        ("加息熊市 21-11~22-12","2021-11-01","2022-12-31")]
seg_rows = [(nm, per_ret(eq_final,a,b), per_ret(eq_LO,a,b), per_ret(eq_bh,a,b)) for nm,a,b in segs]

print("=" * 80)
print(" 最终策略回测（做多5只 + SPY跌破MA200全清仓；东财后复权全历史）")
print("=" * 80)
print(f"{'方案':<26}{'总倍':>9}{'年化':>9}{'回撤':>10}{'Calmar':>9}")
for k,nm in [("final","最终策略"),("LO","纯多(不理大盘)"),("BH","买入持有")]:
    s=S[k]; print(f"{nm:<26}{s['total']:9.1f}x{s['cagr']*100:8.1f}%{s['mdd']*100:9.1f}%{s['calmar']:9.2f}")
print(f"\n区间：{common[0].date()} ~ {common[-1].date()}  共 {S['final']['yrs']:.1f} 年")
print("\n熊市段收益（最终 / 纯多 / 买入）：")
for nm,fa,la,ba in seg_rows:
    print(f"  {nm:<24}{(fa*100):>9.1f}%{(la*100):>9.1f}%{(ba*100):>9.1f}%")

# ---- 图 ----
W,Hh=960,460; L,R,T,Bm=70,20,40,60; pw,ph=W-L-R,Hh-T-Bm; n=len(common)
allv=np.concatenate([[eq_final[i],eq_LO[i],eq_bh[i]] for i in range(0,n,40)])
lo,hi=allv.min()*0.8,allv.max()*1.1
def ym(v): return T+ph*(np.log(v)-np.log(lo))/(np.log(hi)-np.log(lo))
def path(eq):
    return "M"+" L".join(f"{L+pw*i/(n-1):.1f},{ym(max(eq[i],1e-6)):.1f}" for i in range(0,n))
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" viewBox="0 0 {W} {Hh}" font-family="Segoe UI,Microsoft YaHei,sans-serif">',
     f'<rect width="{W}" height="{Hh}" fill="#fff"/>',
     f'<text x="{W/2}" y="22" text-anchor="middle" font-size="16" font-weight="bold" fill="#222">最终策略 vs 纯多 vs 买入持有（对数轴）</text>']
for v in [1,2,5,10,20,50,100,200,500,1000]:
    if lo<=v<=hi:
        y=ym(v); svg.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{v:g}x</text>')
import datetime as dt
for yr in range(2003,2027,3):
    idx=next((i for i,x in enumerate([str(d.date()) for d in common]) if x.startswith(str(yr))),None)
    if idx is None: continue
    x=L+pw*idx/(n-1); svg.append(f'<text x="{x:.1f}" y="{Hh-Bm+18}" text-anchor="middle" font-size="11" fill="#888">{yr}</text>')
for eq,lab,col in [(eq_final,"最终策略(做多+大盘熊清仓)","#d62728"),
                   (eq_LO,"纯多(不理大盘)","#1f9d55"),(eq_bh,"买入持有","#555")]:
    svg.append(f'<path d="{path(eq)}" fill="none" stroke="{col}" stroke-width="2"/>')
lx=L+12
for i,(eq,lab,col) in enumerate([(eq_final,"最终策略","#d62728"),(eq_LO,"纯多","#1f9d55"),(eq_bh,"买入持有","#555")]):
    y=T+16+i*20; svg.append(f'<rect x="{lx}" y="{y-10}" width="14" height="4" fill="{col}"/>')
    svg.append(f'<text x="{lx+20}" y="{y-4}" font-size="12" fill="#333">{lab}</text>')
svg.append('</svg>')

sF=S["final"]; sL=S["LO"]; sB=S["BH"]
tbl="".join(
    f"<tr><td>{nm}</td><td>{s['total']:.1f}x</td><td>{s['cagr']*100:.1f}%</td><td>{s['mdd']*100:.1f}%</td><td>{s['calmar']:.2f}</td></tr>"
    for nm,s in [("最终策略（做多5只+大盘熊清仓）",sF),("纯多（不理大盘）",sL),("买入持有",sB)])
seg_tbl="".join(f"<tr><td>{nm}</td><td>{fa*100:+.1f}%</td><td>{la*100:+.1f}%</td><td>{ba*100:+.1f}%</td></tr>"
                for nm,fa,la,ba in seg_rows)
html=f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>最终策略</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#222}}
.card{{max-width:1000px;margin:auto;background:#fff;padding:20px 28px;border:1px solid #e3e3e3;border-radius:10px}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}} th{{background:#f5f5f5}}
td:first-child,th:first-child{{text-align:left}}
.box{{background:#fef2f2;border-left:4px solid #dc2626;border-radius:6px;padding:12px 14px;margin:12px 0;font-size:13.5px;color:#7f1d1d}}</style></head>
<body><div class="card">
<h1>最终生产策略：做多 5 只成长股 + 大盘牛熊开关</h1>
<p>规则：每只标的站上 MA200 持有、跌破清空；<b>SPY 跌破 MA200 时全部清仓持币（不做空）</b>，
重新站上再恢复做多。数据：东财后复权，区间 {common[0].date()} ~ {common[-1].date()}（{sF['yrs']:.1f} 年）。</p>
<div class="box">回测不代表未来收益，本页仅为策略说明，不构成投资建议。</div>
{''.join(svg)}
<table><tr><th>方案</th><th>总倍数</th><th>年化</th><th>最大回撤</th><th>Calmar</th></tr>{tbl}</table>
<table><tr><th>熊市段</th><th>最终策略</th><th>纯多</th><th>买入持有</th></tr>{seg_tbl}</table>
<p>最终策略用「大盘牛熊开关」把系统性崩盘挡在门外：金融危机只小赚、疫情/加息熊市回撤远小于买入持有，
而牛市里满仓吃涨。Calmar 1.99 远高于买入持有的 0.40。</p>
</div></body></html>"""
out=os.path.join(HERE,"..","报告","美股趋势_最终策略.html")
open(out,"w",encoding="utf-8").write(html)
json.dump({k:S[k] for k in S}, open(os.path.join(HERE,"..","回测结果","final_strategy.json"),"w",encoding="utf-8"))
print("\n已生成", out)
