# -*- coding: utf-8 -*-
"""把 longshort.json 画成对比图：做多LO vs 做空LS vs 做空+借息 vs 买入BH。"""
import os, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
data = json.load(open(os.path.join(HERE, "..", "回测结果", "longshort.json"), encoding="utf-8"))

series = [("LO", "做多（站上持有·跌破空仓）", "#1f9d55"),
          ("LS", "做空（站上持有·跌破做空）", "#d62728"),
          ("LSb", "做空+借息1%/年", "#ff7f0e"),
          ("BH", "买入持有", "#555555")]
W, H = 960, 480
L, R, T, B = 70, 20, 40, 60
pw, ph = W - L - R, H - T - B
n = len(data["dates"])

def ymap(v, lo, hi):
    return T + ph * (np.log(v) - np.log(lo)) / (np.log(hi) - np.log(lo))

allv = np.concatenate([[data[k][i] for k in ["LO","LS","LSb","BH"]] for i in range(0, n, 50)])
lo, hi = allv.min() * 0.9, allv.max() * 1.1

def path(key):
    pts = []
    for i in range(0, n, 1):
        x = L + pw * i / (n - 1)
        y = ymap(max(data[key][i], 1e-6), lo, hi)
        pts.append(f"{x:.1f},{y:.1f}")
    return "M" + " L".join(pts)

# y 轴刻度
ticks = []
e = np.floor(np.log10(lo)); E = np.ceil(np.log10(hi))
for p in range(int(e), int(E) + 1):
    for m in [1, 2, 5]:
        v = m * 10 ** p
        if lo <= v <= hi:
            ticks.append(v)

svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Segoe UI,Microsoft YaHei,sans-serif">']
svg.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
svg.append(f'<text x="{W/2}" y="22" text-anchor="middle" font-size="16" font-weight="bold" fill="#222">美股 MA200 趋势：做多 vs 做空（5只等权·后复权全历史）</text>')
# y 轴
for v in ticks:
    y = ymap(v, lo, hi)
    svg.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" stroke="#eee"/>')
    svg.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#888">{v:g}x</text>')
# x 轴年份
import datetime as dt
for yr in range(2003, 2027, 3):
    # 找该年起点
    idx = next((i for i,d in enumerate(data["dates"]) if d.startswith(str(yr))), None)
    if idx is None: continue
    x = L + pw * idx / (n - 1)
    svg.append(f'<text x="{x:.1f}" y="{H-B+18}" text-anchor="middle" font-size="11" fill="#888">{yr}</text>')
# 曲线
for key, label, color in series:
    svg.append(f'<path d="{path(key)}" fill="none" stroke="{color}" stroke-width="2"/>')
# 图例
lx = L + 10
for i,(key,label,color) in enumerate(series):
    y = T + 16 + i*20
    svg.append(f'<rect x="{lx}" y="{y-10}" width="14" height="4" fill="{color}"/>')
    svg.append(f'<text x="{lx+20}" y="{y-4}" font-size="12" fill="#333">{label}</text>')
svg.append('</svg>')

html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>美股趋势 做多vs做空</title><style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#222}}
.card{{max-width:1000px;margin:auto;background:#fff;padding:20px 28px;border:1px solid #e3e3e3;border-radius:10px}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}} th{{background:#f5f5f5}}
td:first-child,th:first-child{{text-align:left}} .g{{color:#1f9d55;font-weight:700}} .r{{color:#d62728}}</style></head>
<body><div class="card">
<h1>做多 vs 做空：同一个 MA200 信号，只是把「跌破空仓」换成「跌破做空」</h1>
<p>信号同源（收盘站上 200 日均线 → +1；跌破 → 0 或 −1）。数据：5 只等权（AAPL/MSFT/NVDA/AMZN/NFLX），东财后复权全历史（与熊市检验同源）。</p>
{''.join(svg)}
<table>
<tr><th>策略</th><th>总倍数</th><th>年化</th><th>最大回撤</th><th>Calmar</th><th>说明</th></tr>
<tr><td>做多 LO</td><td class="g">286.5x</td><td>26.1%</td><td class="g">−31.6%</td><td class="g">0.83</td><td>站上持有·跌破空仓（当前实盘）</td></tr>
<tr><td>做空 LS</td><td class="r">43.7x</td><td>16.8%</td><td class="r">−58.5%</td><td class="r">0.29</td><td>站上持有·跌破做空</td></tr>
<tr><td>做空+借息</td><td>41.2x</td><td>16.5%</td><td>−59.1%</td><td>0.28</td><td>做空加 1%/年借贷成本</td></tr>
<tr><td>买入持有</td><td>156.9x</td><td>23.1%</td><td>−57.3%</td><td>0.40</td><td>全程不动</td></tr>
</table>
<p><b>结论：加做空反而把策略做差了（286x → 44x，Calmar 0.83 → 0.29）。</b>
趋势过滤的价值在于「跌破均线就空仓躲回撤」，不在于「靠下跌赚钱」。这 5 只都是长期成长的龙头，
跌破 MA200 的阶段大多是震荡/刚企稳，做空会被反复打脸、扛不住 V 形反转；真正能靠做空赚钱的大崩盘（2000/2008）太少，
抵消不了磨损。借贷成本（1%/年）几乎可忽略，不是主因。</p>
<p>想真正参与下跌，更划算的做法是另开一条「大盘择时」腿（如 SPY 跌破 MA200 就切去空头 ETF SH/SQQQ），
而不是拿这 5 只龙头去裸空。当前纯多+空仓对这个篮子已是回撤/收益比最优解。</p>
<p style="color:#999;font-size:12px">注：本对比未计交易滑点/手续费（LO 与 LS 换手相近，相对结论稳健；绝对倍数略乐观）。</p>
</div></body></html>"""

out = os.path.join(HERE, "..", "报告", "美股趋势_做多vs做空.html")
open(out, "w", encoding="utf-8").write(html)
print("已生成", out, f"({len(html)} 字符)")
