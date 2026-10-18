# -*- coding: utf-8 -*-
"""把 short_timing.json 渲染成报告：做多主策略 + 不同做空时机的对比。"""
import os, json
HERE = os.path.dirname(os.path.abspath(__file__))
data = json.load(open(os.path.join(HERE, "..", "回测结果", "short_timing.json"), encoding="utf-8"))
rows = data["variants"]
names = {"LO":"纯多(基线)","any":"跌破就空","ma_slope":"跌破+MA200下行","death":"跌破+死叉",
         "deep5":"跌破+低均线5%+","deep10":"跌破+低均线10%+","deep15":"跌破+低均线15%+","broad":"跌破+SPY共振"}
notes = {r[0]:names.get(r[0],r[0]) for r in rows}

# Calmar 横向条形图
W,H = 760, 320; L=120; R=40; T=30; B=30
pw = W-L-R; ph=H-T-B
maxc = max(r[4] for r in rows) or 1
bars=[]
for i,r in enumerate(rows):
    y=T+ i*(ph/len(rows))+6
    bw = pw* (r[4]/maxc)
    col = "#1f9d55" if r[0]=="LO" else ("#d62728" if r[4]<0.4 else "#ff7f0e")
    bars.append(f'<rect x="{L}" y="{y:.1f}" width="{bw:.1f}" height="20" fill="{col}"/>')
    bars.append(f'<text x="{L+bw+6:.1f}" y="{y+15:.1f}" font-size="12" fill="#333">{r[4]:.2f}</text>')
    bars.append(f'<text x="{L-8}" y="{y+15:.1f}" text-anchor="end" font-size="12" fill="#333">{notes[r[0]]}</text>')
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Segoe UI,Microsoft YaHei,sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>'
       + "".join(bars) + '</svg>')

tbl = "".join(
    f"<tr{' style=\"background:#eafaf0;font-weight:700\"' if r[0]=='LO' else ''}>"
    f"<td>{notes[r[0]]}</td><td>{r[1]:.1f}x</td><td>{r[2]*100:.1f}%</td>"
    f"<td>{r[3]*100:.1f}%</td><td>{r[4]:.2f}</td><td>{r[5]*100:.1f}%</td></tr>"
    for r in rows)

html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>做空时机探索</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#222}}
.card{{max-width:900px;margin:auto;background:#fff;padding:20px 28px;border:1px solid #e3e3e3;border-radius:10px}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}} th{{background:#f5f5f5}}
td:first-child,th:first-child{{text-align:left}} .g{{color:#1f9d55;font-weight:700}}</style></head>
<body><div class="card">
<h1>做多主策略 + 选择性做空：8 种做空时机对比</h1>
<p>做多腿（站上 MA200 持有）不变；跌破 MA200 时，仅当「做空时机条件」成立才开空 −1，否则继续空仓 0。
做空计 1%/年借息。5 只等权（AAPL/MSFT/NVDA/AMZN/NFLX），东财后复权全历史。</p>
<h3>各时机 Calmar（越高越好，绿=纯多基线）</h3>
{svg}
<table>
<tr><th>做空时机</th><th>总倍数</th><th>年化</th><th>最大回撤</th><th>Calmar</th><th>空头占用</th></tr>
{tbl}
</table>
<p><b>结论：没有任何一种做空时机能打败「纯多+跌破空仓」。</b>纯多基线 Calmar 0.83；
最好的过滤（跌破且低于均线 10%+，deep10）也只到 0.48，仍差一大截。原因：这些龙头跌破均线后
常先震荡、再 V 形反转，开空会被反复打脸+逼空磨损；而现金在「躲下跌」的同时也躲开了「反弹的来回抽耳光」。
连 2008 那种大崩盘，deep10 的危机收益（−22%）也比纯多现金（−20%）略差——因为空单在崩盘后反弹里被轧。
<b>所以对这个成长龙头篮子，趋势过滤的价值 100% 来自「空仓躲回撤」，不在「靠跌赚钱」。</b></p>
<p style="color:#999;font-size:12px">若真想参与下跌，正确做法是另开一条<b>大盘择时腿</b>（SPY/QQQ 跌破 MA200 就切空头 ETF SH/SQQQ），
与这 5 只成长股是两套逻辑，不要拿龙头裸空。本表不含交易滑点/费（相对结论稳健）。</p>
</div></body></html>"""
out = os.path.join(HERE, "..", "报告", "美股趋势_做空时机探索.html")
open(out,"w",encoding="utf-8").write(html)
print("已生成", out, f"({len(html)} 字符)")
