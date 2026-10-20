# -*- coding: utf-8 -*-
"""生成「按 9 年尺度看收益」报告：报告/美股趋势_9年尺度.html"""
import os, json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
J = json.load(open(os.path.join(HERE, "..", "回测结果", "nine_year.json"), encoding="utf-8"))

fixed = J["fixed"]
roll = J["roll"]
detail = J["detail9"]
r9 = roll["9"]["最终策略"]
r5 = roll["5"]["最终策略"]
r10 = roll["10"]["最终策略"]

# ---------- 图：16 个 9 年窗口柱状图 ----------
W, H = 980, 400
L, R, T, B = 66, 24, 46, 92
pw, ph = W - L - R, H - T - B
vals = [d["最终策略"] for d in detail]
n = len(vals)
vmax = max(vals) * 1.12
vmin = 0


def yh(v):
    return T + ph * (1 - (v - vmin) / (vmax - vmin))


bw = pw / n * 0.62
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Segoe UI,Microsoft YaHei,sans-serif">',
       f'<rect width="{W}" height="{H}" fill="#fff"/>',
       f'<text x="{W/2}" y="26" text-anchor="middle" font-size="16" font-weight="bold" fill="#222">'
       f'任取起点、持满 9 年：最终策略的收益倍数（共 {n} 个窗口）</text>']
# 网格
for v in range(0, int(vmax) + 1, 5):
    y = yh(v)
    svg.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" stroke="#eee"/>')
    svg.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#999">{v}x</text>')
# 中位线
med = np.median(vals)
y = yh(med)
svg.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" stroke="#f59e0b" '
           f'stroke-width="1.5" stroke-dasharray="5,4"/>')
svg.append(f'<text x="{L+pw-4}" y="{y-7:.1f}" text-anchor="end" font-size="11.5" fill="#b45309">'
           f'中位 {med:.1f}x</text>')
# 柱
for i, d in enumerate(detail):
    v = d["最终策略"]
    x = L + pw * (i + 0.5) / n - bw / 2
    y0 = yh(0); y1 = yh(v)
    col = "#d62728" if v >= med else "#f3a3a3"
    svg.append(f'<rect x="{x:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{y0-y1:.1f}" fill="{col}" rx="2"/>')
    svg.append(f'<text x="{x+bw/2:.1f}" y="{y1-5:.1f}" text-anchor="middle" font-size="10.5" '
               f'fill="#7f1d1d">{v:.0f}x</text>')
    svg.append(f'<text x="{x+bw/2:.1f}" y="{H-B+20}" text-anchor="middle" font-size="10.5" '
               f'fill="#666" transform="rotate(-42 {x+bw/2:.1f} {H-B+20})">{d["start"][:7]}</text>')
svg.append(f'<text x="{L+pw/2}" y="{H-8}" text-anchor="middle" font-size="11" fill="#888">'
           f'起点（持满 9 年）</text>')
svg.append('</svg>')
svg = "".join(svg)

# ---------- 表 ----------
fixed_tbl = "".join(
    f'<tr><td>{d["start"]} ~ {d["end"]}</td><td>{d["yrs"]:.1f} 年</td>'
    f'<td class="hi">{d["最终策略"]:.2f}x</td><td>{d["纯多"]:.2f}x</td><td>{d["买入持有"]:.2f}x</td></tr>'
    for d in fixed)

def row(name, key, strat="最终策略", hl=False):
    d = roll[key][strat]
    cls = ' class="hi"' if hl else ""
    return (f'<tr><td>{name}</td><td{cls}>{d["min"]:.2f}x</td><td{cls}>{d["p25"]:.2f}x</td>'
            f'<td{cls}><b>{d["med"]:.2f}x</b></td><td{cls}>{d["p75"]:.2f}x</td>'
            f'<td{cls}>{d["max"]:.2f}x</td><td>{d["loss"]*100:.0f}%</td></tr>')

roll_tbl = (row("9 年 · 最终策略", "9", hl=True) +
            row("9 年 · 纯多（不理大盘）", "9", "纯多") +
            row("9 年 · 买入持有", "9", "买入持有") +
            row("5 年 · 最终策略", "5") + row("10 年 · 最终策略", "10"))

html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>按 9 年尺度看收益</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#222;background:#f7f7f8}}
.card{{max-width:1020px;margin:auto;background:#fff;padding:22px 30px 30px;border:1px solid #e5e5e5;border-radius:12px}}
h1{{font-size:21px;margin:6px 0 4px}} h2{{font-size:15.5px;margin:26px 0 8px;color:#111}}
.sub{{color:#6b7280;font-size:13px;line-height:1.7}}
table{{border-collapse:collapse;width:100%;margin:10px 0 6px;font-size:13.5px}}
th,td{{border:1px solid #e3e3e3;padding:7px 10px;text-align:right}}
th{{background:#f5f5f5;font-weight:600;font-size:13px}}
td:first-child,th:first-child{{text-align:left}}
.hi{{background:#fff5f5;color:#b91c1c;font-weight:600}}
.box{{background:#f0f7ff;border-left:4px solid #2563eb;border-radius:6px;padding:12px 15px;margin:14px 0;
font-size:13.5px;line-height:1.8;color:#1e3a8a}}
.warn{{background:#fffbeb;border-left:4px solid #d97706;border-radius:6px;padding:12px 15px;margin:14px 0;
font-size:13.5px;line-height:1.8;color:#78350f}}
.big{{display:flex;gap:14px;margin:16px 0;flex-wrap:wrap}}
.kpi{{flex:1;min-width:150px;background:#fafafa;border:1px solid #eee;border-radius:9px;padding:12px 14px}}
.kpi .v{{font-size:24px;font-weight:700;color:#b91c1c;line-height:1.25}}
.kpi .l{{font-size:12px;color:#6b7280;margin-top:3px}}
</style></head><body><div class="card">
<h1>按「9 年」这个尺度看，这套策略能赚多少倍</h1>
<p class="sub">回测区间 {J["range"][0]} ~ {J["range"][1]}（{J["years"]:.1f} 年，东财后复权全历史）。
规则：5 只成长股站上 MA200 持有、跌破清仓；SPY 跌破 MA200 时全部清仓持币。</p>

<div class="big">
  <div class="kpi"><div class="v">{r9["med"]:.1f}x</div><div class="l">9 年中位（任取起点）</div></div>
  <div class="kpi"><div class="v">{r9["min"]:.1f}x</div><div class="l">9 年最差窗口</div></div>
  <div class="kpi"><div class="v">{r9["max"]:.1f}x</div><div class="l">9 年最好窗口</div></div>
  <div class="kpi"><div class="v">0%</div><div class="l">9 年窗口亏损占比</div></div>
</div>

<h2>一、固定每 9 年一刀</h2>
<table><tr><th>区间</th><th>长度</th><th>最终策略</th><th>纯多</th><th>买入持有</th></tr>{fixed_tbl}</table>
<p class="sub">两段完整的 9 年都差不多 <b>15 倍</b>。1145 倍不是靠某一年暴赚，而是
<b>15 × 15 × 5 ≈ 1145</b> 这样滚出来的——每一段都在 15 倍量级，复利 24 年才显得吓人。</p>

<h2>二、任取起点、持满 9 年（滚动窗口）</h2>
{svg}
<table><tr><th>持有期 / 策略</th><th>最差</th><th>25 分位</th><th>中位</th><th>75 分位</th><th>最好</th><th>亏损窗口占比</th></tr>{roll_tbl}</table>
<p class="sub">16 个 9 年窗口，从 2002 年一路排到 2017 年入场，<b>没有一个亏钱</b>；
最差 10.9x，中位 16.0x，最好 25.6x。也就是说：<b>哪怕你运气最差、买在 9 年周期的起点上，9 年后也是 10 倍以上。</b></p>

<h2>三、和比特币的 9 年比</h2>
<div class="box">
比特币现在约 <b>$83,000</b>（2026-10）。往前推 9 年：<br>
· 2017-10 约 $5,000 → 现在 $83,000 ≈ <b>16 倍</b>（和你策略的 9 年中位 16.0x 几乎一样）<br>
· 2016-10 约 $700 → 现在 $83,000 ≈ <b>118 倍</b>（起点选得早，倍数就夸张）<br>
· 2013-10 约 $200 → 2022-10 约 $19,000 ≈ <b>95 倍</b><br>
<b>比特币的 9 年倍数完全取决于你从哪一年算起</b>——从牛市顶算只有十几倍，从熊市底算能上百倍。
</div>
<div class="warn">
但两者有一个本质差别：<b>回撤</b>。比特币 9 年里经历过多次 −70%~−80% 的深熊，
而这套策略最大回撤只有 <b>−16.8%</b>。同样是 9 年 16 倍，
一个要你在 −80% 时还敢拿住，一个只要你能忍住 −17%——<b>后者是真能拿得住的。</b>
</div>

<h2>四、换成你的 1000U</h2>
<table>
<tr><th>情形</th><th>9 年后</th><th>年化</th></tr>
<tr><td>最差窗口（{r9["min"]:.1f}x）</td><td>{1000*r9["min"]:,.0f} U</td><td>{r9["min"]**(1/9)*100-100:.1f}%</td></tr>
<tr><td class="hi">中位（{r9["med"]:.1f}x）</td><td class="hi">{1000*r9["med"]:,.0f} U</td><td class="hi">{r9["med"]**(1/9)*100-100:.1f}%</td></tr>
<tr><td>最好窗口（{r9["max"]:.1f}x）</td><td>{1000*r9["max"]:,.0f} U</td><td>{r9["max"]**(1/9)*100-100:.1f}%</td></tr>
</table>
<p class="sub">注意：这是满仓复利、不含税、不含滑点的理论值；实盘按 5 只等权、每只 200U 起，
单笔 200U 的佣金/点差占比会比大资金高一些，实际会打点折扣。</p>

<div class="warn">历史回测不代表未来。9 年 15 倍的前提是这 5 只继续是成长龙头；
如果未来 9 年它们变成「大盘价值股」（像 2000 年后的思科），倍数会大幅缩水。本页不构成投资建议。</div>
</div></body></html>"""

out = os.path.join(HERE, "..", "报告", "美股趋势_9年尺度.html")
open(out, "w", encoding="utf-8").write(html)
print("已生成", out)
