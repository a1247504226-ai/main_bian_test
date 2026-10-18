# -*- coding: utf-8 -*-
"""生成《熊市检验》报告：趋势策略在 2000 科网崩盘 / 2008 / 2022 里到底怎样。"""
import io
import json
import os

import numpy as np
import pandas as pd

import bear_test as B
from svgkit import bar_chart, line_chart

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "报告", "美股趋势策略_熊市检验.html")

F = json.load(open(os.path.join(HERE, "..", "回测结果", "bear.json"), encoding="utf-8"))
CURVE = json.load(open(os.path.join(HERE, "..", "回测结果", "curve2000.json"), encoding="utf-8"))

SYMS4 = ["AAPL", "MSFT", "NVDA", "AMZN"]


def pct(v, d=1):
    return f"{v*100:+.{d}f}%"


def main():
    # ---- A 组：全历史 ----
    tr4 = B.port(SYMS4, "2000-01-03", mode="ma")
    bh4 = B.port(SYMS4, "2000-01-03", mode="bh")
    from engine import _stats
    a4, b4 = _stats(tr4.values, None), _stats(bh4.values, None)

    tr5 = B.port(["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"], "2002-05-23", mode="ma")
    bh5 = B.port(["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"], "2002-05-23", mode="bh")
    a5, b5 = _stats(tr5.values, None), _stats(bh5.values, None)

    # 相对本金的最低点（精确值，不用采样点）
    tr_min = float(tr4.min()) / 10000 - 1
    bh_min = float(bh4.min()) / 10000 - 1

    # ---- 图 1：2000-2026 权益曲线 ----
    g1 = line_chart(
        [("趋势过滤", CURVE["tr"]),
         ("等权买入", CURVE["bh"])],
        CURVE["dates"], W=1080, H=430, logy=True,
        title="2000-01 科网顶入场，1 万本金 26.7 年后（对数轴，纵轴为倍数）",
        colors=["#c0392b", "#9ca3af"], ylabel="")

    # ---- 图 2：四个熊市段 ----
    bears = [s for s in F["segs"] if s["tr"]["ret"] < 0.10]

    def _yr(a, b):
        ya, yb = a[:4], b[:4]
        return ya if ya == yb else f"{ya}-{yb}"

    lab = [f'{s["name"]} {_yr(s["a"], s["b"])}' for s in bears]
    g2 = bar_chart(lab,
                   [("趋势过滤", [s["tr"]["ret"] * 100 for s in bears]),
                    ("等权买入", [s["bh"]["ret"] * 100 for s in bears])],
                   W=1080, H=300, title="四个熊市段：趋势过滤 vs 买入持有（段内总收益）",
                   yfmt="{:.0f}%", colors=["#c0392b", "#9ca3af"], pad=(58, 24, 44, 40))

    # ---- 表：分段 ----
    seg_rows = ""
    for s in F["segs"]:
        d = (s["tr"]["ret"] - s["bh"]["ret"]) * 100
        cls = "pos" if d > 0 else "neg"
        seg_rows += (
            f'<tr><td><b>{s["name"]}</b></td>'
            f'<td style="color:#667085;font-size:12.5px">{s["a"]} ~ {s["b"]}</td>'
            f'<td class="n">{pct(s["tr"]["ret"])}</td>'
            f'<td class="n">{pct(s["bh"]["ret"])}</td>'
            f'<td class="n {cls}">{d:+.1f}pp</td>'
            f'<td class="n">{pct(s["tr"]["mdd"])}</td>'
            f'<td class="n">{pct(s["bh"]["mdd"])}</td></tr>')

    # ---- 表：大顶入场 ----
    top_rows = ""
    for t in F["tops"]:
        first = True
        for yrs in ("1", "2", "3"):
            y = t["yrs"].get(yrs)
            if not y:
                continue
            d = (y["tr"]["ret"] - y["bh"]["ret"]) * 100
            name = (f'<b>{t["label"]}</b>' if first else "")
            top_rows += (
                f'<tr>{f"<td rowspan=3>{name}</td>" if first else ""}'
                f'<td class="n">{yrs} 年</td>'
                f'<td class="n"><b>{pct(y["tr"]["ret"])}</b></td>'
                f'<td class="n" style="color:#6b7280">{pct(y["tr"]["mdd"])}</td>'
                f'<td class="n">{pct(y["bh"]["ret"])}</td>'
                f'<td class="n" style="color:#6b7280">{pct(y["bh"]["mdd"])}</td>'
                f'<td class="n pos">{d:+.1f}pp</td></tr>')
            first = False

    # ---- 当前状态 ----
    cache = os.path.join(HERE, "..", "signal", "cache")
    now = []
    for s in ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"]:
        d = B.get(s)
        dev = (d.close / d.close.rolling(200).mean() - 1).dropna()
        # 东财是后复权，价格不能直接给人看；真实价从 notify 的前复权缓存取
        raw_p = os.path.join(cache, f"{s}.csv")
        px = (float(pd.read_csv(raw_p)["close"].iloc[-1]) if os.path.exists(raw_p)
              else float(d.close.iloc[-1]))
        now.append((s, float(dev.iloc[-1]), float((dev < dev.iloc[-1]).mean()), px))
    now.sort(key=lambda x: -x[1])

    now_rows = ""
    for s, cur, pctile, px in now:
        state = "持有" if cur > 0 else "空仓"
        col = "#c0392b" if cur > 0 else "#1e8449"
        hot = "#b45309" if pctile > 0.8 else "#374151"
        now_rows += (
            f'<tr><td><b>{s}</b></td><td class="n">{px:,.2f}</td>'
            f'<td class="n" style="color:{col};font-weight:600">{pct(cur)}</td>'
            f'<td class="n" style="color:{hot}">{pctile*100:.0f}%</td>'
            f'<td class="n" style="color:{col}">{state}</td></tr>')

    # 翻空阶梯
    hold = sorted([(s, c) for s, c, _, _ in now if c > 0], key=lambda x: x[1])
    ladder = ""
    for drop in [0, 3, 6, 10, 15, 18, 21]:
        left = [s for s, c in hold if c > drop / 100]
        out = [s for s, c in hold if c <= drop / 100]
        ladder += (f'<tr><td class="n">{"持平" if drop == 0 else f"−{drop}%"}</td>'
                   f'<td class="n"><b>{len(left)}/5</b></td>'
                   f'<td class="n">{len(left)*20}%</td>'
                   f'<td style="color:#1e8449;font-size:12.5px">{", ".join(out) or "—"}</td></tr>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>熊市检验 · 趋势策略在崩盘里到底怎样</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f6f7f9;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
color:#15171a;line-height:1.7;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1140px;margin:0 auto;padding:44px 26px 80px}}
h1{{font-size:31px;margin:0 0 8px;letter-spacing:-.6px;font-weight:750}}
h2{{font-size:19px;margin:44px 0 14px;padding-left:11px;border-left:3px solid #c0392b;font-weight:700}}
h3{{font-size:15px;margin:22px 0 8px;color:#374151}}
.sub{{color:#6b7280;font-size:14px;margin-bottom:26px}}
.card{{background:#fff;border:1px solid #e8eaed;border-radius:10px;padding:22px 24px;margin:14px 0}}
.lead{{background:#fff;border-left:4px solid #c0392b;border-radius:8px;padding:20px 24px;margin:18px 0 8px}}
.lead b{{color:#c0392b}}
p{{margin:10px 0;font-size:14.5px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;font-weight:600;color:#6b7280;font-size:12px;padding:9px 10px;border-bottom:1px solid #e8eaed;background:#fafbfc}}
td{{padding:9px 10px;border-bottom:1px solid #f0f2f5;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:#c0392b}}
.neg{{color:#1e8449}}
.note{{font-size:12.5px;color:#6b7280;margin-top:10px}}
.warn{{background:#fffbeb;border-radius:8px;padding:14px 18px;font-size:13.5px;color:#92400e;margin:12px 0}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:820px){{.grid2{{grid-template-columns:1fr}}}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}}
.k{{background:#fff;border:1px solid #e8eaed;border-radius:9px;padding:14px 16px}}
.k .v{{font-size:22px;font-weight:700;letter-spacing:-.4px}}
.k .l{{font-size:12px;color:#6b7280;margin-top:2px}}
svg{{max-width:100%;height:auto;display:block}}
</style></head><body><div class="wrap">

<h1>熊市检验：这套策略在崩盘里到底怎样</h1>
<div class="sub">数据：东财后复权日线，1990-2026 · 组合：AAPL / MSFT / NVDA / AMZN 等权 · 规则：收盘站上 MA200 则次日开盘持有，跌破则清空</div>

<div class="lead">
<b>会亏，但亏得少很多，而且长期反而赚得更多。</b><br>
从 2000-01 科网顶入场，26.7 年后：趋势过滤 <b>{a4['total']:,.0f} 倍</b>，等权买入 <b>{b4['total']:,.0f} 倍</b>。<br>
更关键的是过程 —— 趋势策略的最低点只比本金低 <b>{abs(tr_min)*100:.1f}%</b>，买入持有最低跌到 <b>{bh_min*100:.0f}%</b>。
</div>

<div class="kpi">
  <div class="k"><div class="v" style="color:#c0392b">{a4['total']:,.0f}x</div><div class="l">趋势过滤终值</div></div>
  <div class="k"><div class="v" style="color:#6b7280">{b4['total']:,.0f}x</div><div class="l">买入持有终值</div></div>
  <div class="k"><div class="v" style="color:#c0392b">{a4['mdd']*100:.1f}%</div><div class="l">趋势最大回撤</div></div>
  <div class="k"><div class="v" style="color:#6b7280">{b4['mdd']*100:.1f}%</div><div class="l">买入最大回撤</div></div>
</div>

<div class="card">{g1}</div>

<h2>一、如果现在就是顶，接下来会怎样</h2>
<p>我把历史上三个大顶当天作为起点，分别跑 1 / 2 / 3 年。这是最直接回答「现在入场会怎样」的实验。</p>

<div class="card">
<table>
<thead><tr><th>顶部</th><th class="n">持有</th><th class="n">趋势收益</th><th class="n">趋势最深</th>
<th class="n">买入收益</th><th class="n">买入最深</th><th class="n">差</th></tr></thead>
<tbody>{top_rows}</tbody>
</table>
<div class="note">「最深」是这 1/2/3 年内的最大回撤，不是最终收益。</div>
</div>

<div class="warn">
<b>要接受的现实：</b>顶部入场的第一年，趋势策略照样亏 11%~25%。
它不是「熊市不亏钱」，是「熊市少亏一半」。真正救命的是第 2、3 年 ——
2000 顶入场，买入持有 3 年后还亏 62%，趋势只亏 3.6%。
</div>

<h2>二、按牛熊段切开看</h2>
<div class="card">
<table>
<thead><tr><th>区间</th><th>起止</th><th class="n">趋势</th><th class="n">买入</th>
<th class="n">差</th><th class="n">趋势MDD</th><th class="n">买入MDD</th></tr></thead>
<tbody>{seg_rows}</tbody>
</table>
</div>

<div class="card">{g2}</div>

<h3>三个结论</h3>
<p><b>1. 慢熊里它是真救命。</b>科网崩盘（2000-03 到 2002-10，纳指跌 78%）：趋势 <b>+4.8%</b>，买入 <b>−73.0%</b>。
这是 26 年里最严酷的一段，也是这套策略唯一一次在熊市里做到正收益。</p>
<p><b>2. 快崩里它几乎没用。</b>2020 疫情崩（1 个月跌 34%）：趋势 −26.9%，买入 −27.9%，只差 1 个点。
年线是滞后指标，跌太快根本来不及反应。这是这套规则的结构性缺陷。</p>
<p><b>3. 牛市里它一定跑输。</b>长牛段（2009-2020）趋势 +1917%，买入 +3166%，差了 1249 个百分点。
你要的是「少亏」，就必须接受「少赚」。</p>

<h2>三、你现在实际承担的风险</h2>
<p>下面这张表比任何历史回测都更直接：从今天的价格开始同步下跌，仓位会怎么变。</p>

<div class="grid2">
<div class="card">
<h3 style="margin-top:0">距 MA200 的距离</h3>
<table>
<thead><tr><th>代码</th><th class="n">收盘</th><th class="n">距均线</th><th class="n">历史分位</th><th class="n">信号</th></tr></thead>
<tbody>{now_rows}</tbody>
</table>
<div class="note">「历史分位」= 当前偏离度在该标的历史上的位置。MSFT 处于 87% 分位，属于偏热；NVDA、AMZN 只有 34~37%，已经不热了。</div>
</div>

<div class="card">
<h3 style="margin-top:0">下跌时的翻空阶梯</h3>
<table>
<thead><tr><th class="n">组合跌幅</th><th class="n">剩余持仓</th><th class="n">仓位</th><th>已离场</th></tr></thead>
<tbody>{ladder}</tbody>
</table>
<div class="note">假设各标的同步下跌、MA200 短期不动。实际 MA200 也会下移，翻空会稍慢一点。</div>
</div>
</div>

<div class="warn">
<b>这张表说明了一件不舒服的事：</b>前 6% 的跌幅几乎没有保护 —— AMZN 只高出年线 3.0%、NVDA 高出 5.7%，
一有风吹草动就翻空，但 AAPL 要跌 17%、MSFT 要跌 21% 才出。
也就是说组合要跌掉 21%，仓位才会清零。这不是「见顶就跑」，是「跌破才跑」。
</div>

<h2>四、这套策略什么时候会失效</h2>
<p>诚实说，有三种情况它会让你难受：</p>
<p><b>① 闪崩。</b>2020 年 3 月那种 4 周跌 34% 的行情，年线来不及反应，趋势策略跟买入持有几乎一样惨。
如果你担心的是「某天突然崩」，这套规则给不了保护。</p>
<p><b>② 假突破反复打脸。</b>震荡市里价格在年线上下反复穿，每次翻仓都要付滑点。
全池测试里，随机挑一只标的，这套择时的预测力等同于抛硬币（相位检验中位分位 47%）。</p>
<p><b>③ 你只有 5 只科技股。</b>它们同涨同跌，2022 年有 4 只同时跌破年线。
分散化在这个组合里是假的 —— 真正的分散需要跨资产（比如加 JNK、GLD、TLT），
而全池测试里趋势过滤效果最好的恰恰是信用债（JNK +0.50、HYG +0.33）。</p>

<h2>五、那到底该不该现在上</h2>
<p>这个问题我没法替你回答，但可以把账算清楚：</p>
<p><b>如果现在是顶</b>：第一年大概率亏 11%~25%，比你自己死扛少亏 20~30 个百分点。
第二、三年开始分化 —— 2000 那种慢熊，三年后你只亏 3.6%，死扛的人亏 62%。</p>
<p><b>如果现在不是顶</b>：你会跑输，2021 顶入场后 3 年趋势 +96.8% vs 买入 +99.5%，只差 2.6 个点；
但如果是 2009-2020 那种长牛，你会少赚一半以上。</p>
<p><b>这套规则买的是保险，不是彩票。</b>它用「牛市少赚」换「熊市少亏」。
如果你现在心里已经在担心牛顶，那说明你需要的正是这个 —— 因为它替你做了「跌破就走」这个你自己大概率做不到的决定。</p>

<div class="note" style="margin-top:34px;padding-top:18px;border-top:1px solid #e8eaed">
数据源：东方财富后复权日线（复权接缝已按比例修正，非截断）· 交易成本：单边滑点 0.05%，无手续费<br>
回测不构成投资建议。历史表现不代表未来。
</div>

</div></body></html>"""

    io.open(OUT, "w", encoding="utf-8").write(html)
    print(f"已生成 {OUT}  ({len(html):,} 字符)")
    print(f"4只 2000起: 趋势 {a4['total']:.0f}x / Cal {a4['calmar']:.2f}   买入 {b4['total']:.0f}x / Cal {b4['calmar']:.2f}")
    print(f"5只 2002起: 趋势 {a5['total']:.0f}x / Cal {a5['calmar']:.2f}   买入 {b5['total']:.0f}x / Cal {b5['calmar']:.2f}")


if __name__ == "__main__":
    main()
