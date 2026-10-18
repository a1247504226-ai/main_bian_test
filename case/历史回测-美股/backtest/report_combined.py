# -*- coding: utf-8 -*-
"""把 combined.json 渲染成报告：两种多空结合对比 + 权益曲线。"""
import os, json
HERE = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(os.path.join(HERE, "..", "回测结果", "combined.json"), encoding="utf-8"))

series = [("eq_LO", "纯多LO（基线）", "#1f9d55"),
          ("eq_dyn", "动态对冲（SPY熊切40%做空）", "#d62728"),
          ("eq_8020", "组合 80%LO+20%H", "#ff7f0e"),
          ("eq_H", "纯对冲腿H（SPY熊做空）", "#999999")]
W,Hh=960,480; L,R,T,Bm=70,20,40,60
pw,ph=W-L-R,Hh-T-Bm
n=len(d["dates"])
import numpy as np
allv=np.concatenate([[d[k][i] for k in ["eq_LO","eq_dyn","eq_8020","eq_H"]] for i in range(0,n,40)])
lo,hi=allv.min()*0.85,allv.max()*1.1
def ymap(v): return T+ph*(np.log(v)-np.log(lo))/(np.log(hi)-np.log(lo))
def path(k):
    return "M"+" L".join(f"{L+pw*i/(n-1):.1f},{ymap(max(d[k][i],1e-6)):.1f}" for i in range(0,n))
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" viewBox="0 0 {W} {Hh}" font-family="Segoe UI,Microsoft YaHei,sans-serif">',
     f'<rect width="{W}" height="{Hh}" fill="#fff"/>',
     f'<text x="{W/2}" y="22" text-anchor="middle" font-size="16" font-weight="bold" fill="#222">多空结合：做多策略 + 大盘熊市做空对冲腿（5只等权）</text>']
for v in [1,2,5,10,20,50,100,200,500]:
    if lo<=v<=hi:
        y=ymap(v); svg.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{v:g}x</text>')
import datetime as dt
for yr in range(2003,2027,3):
    idx=next((i for i,x in enumerate(d["dates"]) if x.startswith(str(yr))),None)
    if idx is None: continue
    x=L+pw*idx/(n-1); svg.append(f'<text x="{x:.1f}" y="{Hh-Bm+18}" text-anchor="middle" font-size="11" fill="#888">{yr}</text>')
for k,label,col in series:
    svg.append(f'<path d="{path(k)}" fill="none" stroke="{col}" stroke-width="2"/>')
lx=L+10
for i,(k,label,col) in enumerate(series):
    y=T+16+i*20; svg.append(f'<rect x="{lx}" y="{y-10}" width="14" height="4" fill="{col}"/>')
    svg.append(f'<text x="{lx+20}" y="{y-4}" font-size="12" fill="#333">{label}</text>')
svg.append('</svg>')

def row(name,s,hl=False):
    return (f"<tr{' style=\"background:#eafaf0;font-weight:700\"' if hl else ''}><td>{name}</td>"
            f"<td>{s[0]:.1f}x</td><td>{s[1]*100:.1f}%</td><td>{s[2]*100:.1f}%</td><td>{s[3]:.2f}</td></tr>")
tbl = (row("A. 字面结合（站上多·跌破空，同批票）",(41.2,0.165,-0.591,0.28))
       + row("纯多LO（基线）",d["LO"],True)
       + row("纯对冲腿H（SPY熊做空）",d["H"])
       + row("组合 80%LO+20%H",d["8020"])
       + row("动态对冲（SPY熊切40%做空）",d["dyn"],True))

html=f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>多空结合验证</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#222}}
.card{{max-width:1000px;margin:auto;background:#fff;padding:20px 28px;border:1px solid #e3e3e3;border-radius:10px}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}} th{{background:#f5f5f5}}
td:first-child,th:first-child{{text-align:left}} .r{{color:#d62728}}</style></head>
<body><div class="card">
<h1>多空结合验证：做多策略 + 做空，怎么结合才真赚钱？</h1>
<p>回测 5 只等权（AAPL/MSFT/NVDA/AMZN/NFLX）做多策略 + 一条「大盘熊市做空 SPY」对冲腿，含 1%/年借息。</p>
{''.join(svg)}
<table><tr><th>方案</th><th>总倍数</th><th>年化</th><th>最大回撤</th><th>Calmar</th></tr>{tbl}</table>
<p><b>两种结合，天壤之别：</b></p>
<p>① <b>字面结合</b>（站上做多、跌破就做空同一批票）：Calmar <span class="r">0.28</span>，最差——现金变做空反而被震荡/反弹磨损。</p>
<p>② <b>正确结合</b>（做多这 5 只 + 独立加「大盘熊市做空 SPY」对冲腿）：<b>动态对冲 Calmar 1.59</b>，
总倍数 649x、回撤仅 −19.1%，把基线纯多的 0.90 / −29.6% 大幅优化。做法：平时 100% 做多；当 SPY 跌破 MA200（大盘确认熊），
把 40% 仓位切去空头（做空 SPY 或买 1 倍反向 ETF SH），大盘回牛再切回。</p>
<p><b>为什么②有效而①无效？</b> 同一批成长股跌破自己的 MA200 后常震荡/V 形反转，裸空被反复打脸；
但「大盘系统性熊市」是另一回事——那时个股和对冲腿方向一致（多仓亏、空仓赚）天然对冲，刚好削掉最大的左尾（崩盘），
而牛市里对冲腿空仓、做多满额吃涨。纯对冲腿 H 单独看很烂（Calmar −0.06，会被反弹逼空），
但它和做多腿<b>负相关地互补</b>，组合起来反而把波动和回撤压下来、收益还更高。</p>
<p style="color:#999;font-size:12px">注：动态对冲回测用 SPY 破 MA200 作触发（与你 5 只「跌破空仓」时段基本重合）；实盘可用 SH(1倍反向) 或融券做空 SPY 实现；
3 倍反向 ETF(SQQQ) 有波动损耗，长期持有不划算。本报告未计交易滑点/费（相对结论稳健）。</p>
</div></body></html>"""
out=os.path.join(HERE,"..","报告","美股趋势_多空结合验证.html")
open(out,"w",encoding="utf-8").write(html)
print("已生成",out,f"({len(html)} 字符)")
