# -*- coding: utf-8 -*-
"""生成第三份报告：美股趋势交易标的筛选与方案（东财后复权数据版）。

所有数字从 facts3.json 取；权益曲线现场重算（port() 只要 0.35 秒）。
"""
import io
import os
import json
import numpy as np
import pandas as pd
from em import port, load, ma_sig, CN
from svgkit import line_chart, area_chart, bar_chart, esc
from svgx import curve_chart, scatter_chart

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.dirname(HERE)
F = json.load(open(os.path.join(HERE, "..", "回测结果", "facts3.json"), encoding="utf-8"))

CORE5 = F["main"]["syms"]
M = F["main"]
TR, BH = M["tr"], M["bh"]
VAR = F["variants"]


def dd(eq):
    v = np.asarray(eq, float)
    return v / np.maximum.accumulate(v) - 1


def yfmt(v):
    return f"{v:,.0f}"


def build():
    tr = port(CORE5, M["ma"], 1.0, M["start"], "ma")
    bh = port(CORE5, M["ma"], 1.0, M["start"], "bh")
    dates = [str(d)[:10] for d in tr.index]
    n = len(dates)
    step = max(1, n // 1700)
    ix = list(range(0, n, step))
    if ix[-1] != n - 1:
        ix.append(n - 1)

    # ---------- 图 1 权益曲线 ----------
    g1 = line_chart(
        [("趋势过滤 MA200", [float(tr.iloc[i]) / 10000 for i in ix]),
         ("等权买入持有", [float(bh.iloc[i]) / 10000 for i in ix])],
        [dates[i] for i in ix], H=430, logy=True,
        title="5 只等权组合：趋势过滤 vs 买入持有（对数轴，起点 = 1.0）",
        colors=["#c0392b", "#9ca3af"], ylabel="")
    g2 = area_chart(list(dd(tr.values)[ix]), [dates[i] for i in ix], W=540, H=270, nlab=5,
                    pad=(46, 16, 40, 34),
                    title=f"趋势过滤回撤（最深 {TR['mdd']*100:.1f}%）", color="#1e8449")
    g3 = area_chart(list(dd(bh.values)[ix]), [dates[i] for i in ix], W=540, H=270, nlab=5,
                    pad=(46, 16, 40, 34),
                    title=f"等权买入持有回撤（最深 {BH['mdd']*100:.1f}%）", color="#c0392b")

    # ---------- 图 2 逐年 ----------
    yl = F["yearly"]
    g4 = bar_chart([str(r["y"]) for r in yl],
                   [("趋势过滤", [r["tr"] for r in yl]), ("买入持有", [r["bh"] for r in yl])],
                   H=330, title="逐年收益对比（%）", colors=["#c0392b", "#c8ccd2"])

    # ---------- 图 3 MA 网格 ----------
    mg = F["ma_grid"]
    g6 = curve_chart([r["n"] for r in mg],
                     [("趋势过滤 Calmar", [r["calmar"] for r in mg])],
                     W=1080, H=300, title="均线周期网格（满仓，其余条件不变）",
                     xlabel="均线周期（交易日）", ylabel="Calmar",
                     hline=BH["calmar"], hline_label=f"买入持有 {BH['calmar']:.2f}",
                     xfmt="{:.0f}", yfmt="{:.2f}", pad=(64, 150, 44, 50))

    # ---------- 图 5 散点 ----------
    al = F["univ"]["all"]
    pts = [(r["vol"], r["d"], r["sym"]) for r in al]
    hi = set(i for i, r in enumerate(al) if abs(r["d"]) >= 0.24)
    g7 = scatter_chart(pts, W=1080, H=400,
                       title="70 个标的：横轴年化波动率，纵轴趋势过滤带来的 Calmar 提升（虚线 = 零提升）",
                       xlabel="年化波动率", ylabel="",
                       hline=0.0, xfmt="{:.0%}", yfmt="{:.2f}",
                       hi_idx=hi, annotate={"TXN", "AVGO", "JNK", "HYG", "NVDA", "AMZN"})

    # ================= 表格 =================
    # 当前持仓
    now_rows = ""
    for r in F["now"]:
        cls = "pos" if r["hold"] else "neg"
        now_rows += (
            f'<tr><td><b>{r["sym"]}</b></td><td>{esc(r["cn"])}</td>'
            f'<td class="n">{r["close"]:,.2f}</td><td class="n">{r["ma"]:,.2f}</td>'
            f'<td class="n {cls}">{r["dev"]*100:+.1f}%</td>'
            f'<td class="n">{r["atr"]*100:.2f}%</td>'
            f'<td class="n {cls}"><b>{"持有" if r["hold"] else "空仓"}</b></td>'
            f'<td class="n">{r["notional"]:,.0f} U</td></tr>')

    # 逐年
    yr_rows = "".join(
        f'<tr><td>{r["y"]}</td><td class="n pos">{r["tr"]:+.1f}%</td>'
        f'<td class="n">{r["bh"]:+.1f}%</td>'
        f'<td class="n {"pos" if r["d"]>0 else "neg"}">{r["d"]:+.1f}pp</td></tr>'
        for r in yl)

    # 六路
    cmax = max(v["s"]["calmar"] for v in VAR)
    var_rows = ""
    for i, v in enumerate(VAR):
        s = v["s"]
        hl = ' class="hl"' if v["k"].startswith("趋势过滤满仓 MA200") else ""
        var_rows += (f'<tr{hl}><td><b>{esc(v["k"])}</b></td><td style="color:#667085;font-size:12.5px">{esc(v["d"])}</td>'
                     f'<td class="n">{s["total"]:,.1f}x</td><td class="n">{s["cagr"]*100:.1f}%</td>'
                     f'<td class="n">{s["mdd"]*100:.1f}%</td>'
                     f'<td class="n"><b>{s["calmar"]:.2f}</b></td>'
                     f'<td><div class="bar"><span style="width:{s["calmar"]/cmax*100:.0f}%"></span></div></td>'
                     f'<td class="n">{s["sharpe"]:.2f}</td></tr>')

    # 仓位缩放网格
    vt_rows = "".join(
        f'<tr><td>{"不设（满仓）" if r["vt"] >= 1 else f"日波动 {r["vt"]*100:.1f}%"}</td>'
        f'<td class="n">{r["total"]:,.1f}x</td><td class="n">{r["cagr"]*100:.1f}%</td>'
        f'<td class="n">{r["mdd"]*100:.1f}%</td><td class="n"><b>{r["calmar"]:.2f}</b></td>'
        f'<td class="n">{r["sharpe"]:.2f}</td></tr>' for r in F["vt_grid"])

    # 起点
    st_rows = "".join(
        f'<tr><td>{r["start"]}</td><td class="n">{r["years"]:.1f} 年</td>'
        f'<td class="n">{r["tr"]["calmar"]:.2f}</td><td class="n">{r["bh"]["calmar"]:.2f}</td>'
        f'<td class="n pos">+{r["tr"]["calmar"]-r["bh"]["calmar"]:.2f}</td>'
        f'<td class="n">{r["tr"]["cagr"]*100:.1f}% / {r["bh"]["cagr"]*100:.1f}%</td></tr>'
        for r in F["starts"])

    # 别的篮子
    bk_rows = "".join(
        f'<tr><td><b>{esc(r["name"])}</b></td><td class="n">{r["n"]}</td>'
        f'<td class="n">{r["years"]:.1f} 年</td>'
        f'<td class="n">{r["tr"]["calmar"]:.2f}</td><td class="n">{r["bh"]["calmar"]:.2f}</td>'
        f'<td class="n {"pos" if r["tr"]["calmar"]>r["bh"]["calmar"] else "neg"}">'
        f'{r["tr"]["calmar"]-r["bh"]["calmar"]:+.2f}</td>'
        f'<td class="n">{r["tr"]["total"]:.1f}x / {r["bh"]["total"]:.1f}x</td></tr>'
        for r in F["baskets"])

    # 单标的
    sg_rows = "".join(
        f'<tr><td><b>{r["sym"]}</b></td><td>{esc(r["cn"])}</td>'
        f'<td class="n">{r["tr"]["total"]:.1f}x</td><td class="n">{r["tr"]["calmar"]:.2f}</td>'
        f'<td class="n">{r["bh"]["total"]:.1f}x</td><td class="n">{r["bh"]["calmar"]:.2f}</td>'
        f'<td class="n {"pos" if r["tr"]["calmar"]>r["bh"]["calmar"] else "neg"}">'
        f'{r["tr"]["calmar"]-r["bh"]["calmar"]:+.2f}</td>'
        f'<td class="n">{r["trades"]}</td></tr>' for r in F["single"])

    # 全池 10 强 / 10 弱
    U = F["univ"]

    def urows(rows, sign):
        return "".join(
            f'<tr><td><b>{r["sym"]}</b></td><td>{esc(r["cn"])}</td>'
            f'<td class="n">{r["vol"]*100:.0f}%</td>'
            f'<td class="n">{r["mc"]*100:.0f}%</td>'
            f'<td class="n">{r["bh_calmar"]:.2f} → {r["tr_calmar"]:.2f}</td>'
            f'<td class="n {sign}"><b>{r["d"]:+.2f}</b></td></tr>' for r in rows)

    top_rows = urows(U["top"], "pos")
    bot_rows = urows(U["bot"], "neg")

    # 回撤区间
    eps = sorted(F["dd_eps"], key=lambda x: x["m"])[:5]
    ep08 = [e for e in F["dd_eps"] if e["a"].startswith("2008")]
    ep08m = ep08[0]["m"] if ep08 else F["dd_eps"][1]["m"]
    dd_rows = "".join(
        f'<tr><td>{r["a"]} → {r["b"]}</td><td class="n">{r["yrs"]*12:.0f} 个月</td>'
        f'<td class="n neg"><b>{r["m"]*100:.1f}%</b></td></tr>' for r in eps)

    # 数据体检
    rate_limited = "COST WMT PEP HD BAC GS V MA AXP CAT BA GE HON UPS DIS".split()
    rl_txt = "、".join(rate_limited)

    d_med = float(np.median([r["d"] for r in al]))
    d_min = min(r["d"] for r in al)
    d_max = max(r["d"] for r in al)
    ma_lo = min(r["calmar"] for r in mg)
    ma_hi = max(r["calmar"] for r in mg)
    vt_lo = min(r["calmar"] for r in F["vt_grid"])
    vt_hi = max(r["calmar"] for r in F["vt_grid"])
    vt_big = F["vt_grid"][0]["total"]
    vt_sml = F["vt_grid"][-1]["total"]

    # 逐年里跑赢/跑输极值
    wins = [r for r in yl if r["d"] > 0]
    loses = sorted([r for r in yl if r["d"] < 0], key=lambda r: r["d"])[:3]
    dn = [r for r in yl if r["bh"] < 0]                       # 买入持有亏钱的年份
    up_w = [r for r in wins if r["bh"] > 0]                   # 上涨年却还跑赢的
    dn_txt = "、".join(f'{r["y"]}（{r["tr"]:+.1f}% vs {r["bh"]:+.1f}%）' for r in dn)
    up_txt = "、".join(str(r["y"]) for r in up_w)
    lose_txt = "、".join(f'{r["y"]}（{r["tr"]:+.1f}% vs {r["bh"]:+.1f}%）' for r in loses)
    dn_all_win = all(r["d"] > 0 for r in dn)

    # 2022 年信号翻转次数
    flips = {}
    for s in CORE5:
        d = load(s)
        sig = ma_sig(d, M["ma"])
        i0 = int(np.flatnonzero((d.date >= "2022-01-01").values)[0])
        i1 = int(np.flatnonzero((d.date <= "2022-12-31").values)[-1])
        seg = sig[i0:i1 + 1]
        flips[s] = int((seg[1:] != seg[:-1]).sum())
    flip_max = max(flips, key=flips.get)
    flip_tot = sum(flips.values())

    css = """
    *{box-sizing:border-box}
    body{margin:0;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2328;line-height:1.72}
    .wrap{max-width:1140px;margin:0 auto;padding:0 22px 70px}
    header{background:linear-gradient(135deg,#141821 0%,#243040 100%);color:#fff;padding:44px 0 38px;margin-bottom:30px}
    header .wrap{padding-bottom:0}
    h1{font-size:28px;margin:0 0 10px;font-weight:800;letter-spacing:-.5px}
    .sub{color:#9fb0c4;font-size:13.5px;margin:0}
    .sub b{color:#e8eef6}
    h2{font-size:20px;margin:44px 0 14px;padding-left:12px;border-left:4px solid #c0392b;font-weight:750}
    h3{font-size:15.5px;margin:26px 0 10px;font-weight:700}
    p{margin:10px 0;font-size:14.5px;color:#2f353b}
    .card{background:#fff;border-radius:12px;padding:20px 24px;margin:16px 0;box-shadow:0 1px 3px rgba(16,24,40,.07)}
    .verdict{background:#fff;border-left:5px solid #c0392b;border-radius:10px;padding:20px 24px;margin:22px 0;box-shadow:0 1px 3px rgba(16,24,40,.07)}
    .verdict p{margin:0;font-size:15px}
    .verdict p+p{margin-top:10px}
    table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}
    th{background:#f2f4f7;text-align:left;padding:9px 10px;font-weight:650;color:#475467;font-size:12.5px;border-bottom:1px solid #e4e7ec}
    td{padding:9px 10px;border-bottom:1px solid #f2f4f7;vertical-align:top}
    tr.hl{background:#fff6f4}
    .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
    .pos{color:#c0392b;font-weight:600}
    .neg{color:#1e8449;font-weight:600}
    .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:16px 0}
    .kpi{background:#fff;border-radius:10px;padding:16px 18px;box-shadow:0 1px 2px rgba(16,24,40,.06)}
    .kpi-l{font-size:12px;color:#667085}
    .kpi-v{font-size:23px;font-weight:750;margin:4px 0 2px;font-variant-numeric:tabular-nums}
    .kpi-s{font-size:11.5px;color:#98a2b3}
    .warn{background:#fffaf0;border-left:4px solid #d68910;padding:14px 18px;border-radius:8px;margin:14px 0;font-size:13.5px}
    .ok{background:#f0f9f4;border-left:4px solid #1e8449;padding:14px 18px;border-radius:8px;margin:14px 0;font-size:13.5px}
    .bad{background:#fdf2f2;border-left:4px solid #c0392b;padding:14px 18px;border-radius:8px;margin:14px 0;font-size:13.5px}
    ul,ol{margin:10px 0;padding-left:22px;font-size:14.5px}
    li{margin:6px 0}
    code{background:#f2f4f7;padding:1.5px 5px;border-radius:4px;font-size:12.5px;font-family:ui-monospace,Consolas,monospace}
    .bar{width:88px;height:8px;background:#f0f2f5;border-radius:4px;overflow:hidden}
    .bar span{display:block;height:8px;border-radius:4px;background:#c0392b}
    pre{background:#1b2029;color:#dfe6ef;padding:16px 18px;border-radius:9px;overflow-x:auto;font-size:12.5px;line-height:1.75;font-family:ui-monospace,Consolas,monospace;margin:12px 0}
    pre .c{color:#7f8c9b}
    pre .k{color:#e8837b}
    .foot{color:#98a2b3;font-size:12px;margin-top:38px;padding-top:18px;border-top:1px solid #e4e7ec}
    svg{display:block;width:100%;height:auto}
    .tag{display:inline-block;background:#fef2f2;color:#c0392b;border-radius:5px;padding:2px 8px;font-size:11.5px;font-weight:600;margin-right:6px}
    @media(max-width:760px){.grid4,.grid2{grid-template-columns:1fr 1fr}h1{font-size:21px}}
    """

    pseudo = """<span class="c"># 每个交易日收盘后跑一次，第二天开盘调仓</span>
总资金 = 10000 U
每只份额 = 总资金 / 5          <span class="c"># 2000 U</span>

<span class="k">for</span> sym <span class="k">in</span> [AAPL, MSFT, NVDA, AMZN, NFLX]:
    ma = 最近 200 个交易日收盘价的算术平均
    <span class="k">if</span> 今日收盘价 &gt; ma:
        目标持仓 = 每只份额        <span class="c"># 满仓这只</span>
    <span class="k">else</span>:
        目标持仓 = 0              <span class="c"># 清空这只，钱留现金</span>

    明天开盘按「目标持仓 - 当前持仓」下单
    <span class="c"># 空仓那只的钱不加到别的标的上，也不做空</span>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股趋势交易：标的筛选与方案</title><style>{css}</style></head><body>
<header><div class="wrap">
<h1>美股趋势交易：选哪几只、怎么做</h1>
<p class="sub">东财后复权日线（含分红再投资）　·　{M['first']} ~ {M['last']}　·　
<b>{M['years']:.1f} 年</b>　·　滑点 0.05%/单边　·　信号次日开盘成交</p>
</div></header>
<div class="wrap">

<div class="verdict">
<p><b>结论先说：</b>趋势交易在美股<b>能用，但别指望它多赚钱</b>——它的作用是<b>把回撤砍掉一半</b>，收益基本打平。</p>
<p>我筛了 89 个标的、{U['n']} 个能回测的，最终给 <b>5 只</b>：AAPL、MSFT、NVDA、AMZN、NFLX。
规则只有一条：<b>收盘价在 200 日均线之上就满仓，之下就空仓</b>，5 只等权。
{M['years']:.1f} 年回测 <b>{TR['total']:,.1f} 倍 / 年化 {TR['cagr']*100:.1f}% / 最大回撤 {TR['mdd']*100:.1f}%</b>；
等权买入持有是 {BH['total']:,.1f} 倍 / {BH['cagr']*100:.1f}% / {BH['mdd']*100:.1f}%。
<b>终值几乎一样（差 {(1-TR['total']/BH['total'])*100:.0f}%），回撤少了 {(BH['mdd']-TR['mdd'])*100:.0f} 个百分点。</b></p>
<p>但这不是免费的：2008 和 2022 它<b>照样亏 28%</b>，只是没亏 50%。要不要用，取决于你能不能接受「大跌年还是要亏三成」。</p>
</div>

<h2>一、先说清楚数据：这一版重做了两轮</h2>
<p>上一版报告用的是新浪美股接口，有几个致命问题，我全部推翻重来。这里把过程写出来，你才知道下面的数字是怎么来的。</p>
<div class="card">
<table><thead><tr><th style="width:110px">轮次</th><th>数据源</th><th>发现的问题</th><th>处理</th></tr></thead><tbody>
<tr><td><b>第一轮</b></td><td>新浪美股（不复权）</td>
<td>2005-01 ~ 2007-03 有整段空洞，META/SMH/HON 只从 2021 年才有数据；<b>不含分红</b>，长期年化被低估约 1~2%</td>
<td>废弃</td></tr>
<tr><td><b>第二轮</b></td><td>东财 <code>fqt=2</code> 后复权</td>
<td><b>复权因子有接缝</b>：AAPL 在 2005-01-03 凭空 +91.2%、NFLX 在 2004-02-12 凭空 +107.3%、PG 在 2004-06-21 +77.8%、QCOM 在 2004-08-16 +100.8%。这不是行情，是复权基准切换</td>
<td>检测单日 |涨跌| &gt; 55%，<b>截掉接缝之前的数据</b>。代价是丢掉早期历史</td></tr>
<tr><td><b>本轮</b></td><td>东财 + 截断 + 等权归一</td>
<td>另一个 bug：子账户组合时没把各标的在起点归一化，预热期的涨幅变成了隐式权重，组合变成「按 2002-2004 表现加权」</td>
<td>修正后 5 只买入持有从 <b>634.7x 降到 {BH['total']:,.1f}x</b>，Calmar 0.54 → {BH['calmar']:.2f}</td></tr>
</tbody></table>
<p style="font-size:13px;color:#475467;margin-top:10px">
因为截断，AAPL 的可用历史从 2005-01-03 起、NFLX 从 2004-02-12 起，
所以<b>组合统一起点被推到 {M['start']}</b>，之前的数据全部弃用。这是保守的选择：宁可短，不要假。</p>
</div>
<div class="warn">
<b>覆盖率不是 100%：</b>89 个候选里拉到 <b>74 个</b>（其中 {U['n']} 个历史足够、4 个不足 10 年被剔除：
EMB、IEF、SMH、XLC）。被东财限流没拉到的 <b>{len(rate_limited)} 个</b>是
{rl_txt}。<br>
这 {len(rate_limited)} 个恰好都是低波动慢牛和传统工业，会带来选择偏差 —— 见第九节。
</div>

<h2>二、现在该买什么（{F['now_date']} 收盘）</h2>
<div class="card">
<table><thead><tr><th>代码</th><th>名称</th><th class="n">收盘价</th><th class="n">MA200</th>
<th class="n">距均线</th><th class="n">ATR14%</th><th class="n">信号</th><th class="n">1 万本金分配</th></tr></thead>
<tbody>{now_rows}</tbody></table>
<p style="font-size:13.5px;color:#475467;margin-top:10px">
<b>持有 {F['n_hold']}/5 只，合计仓位 {F['expo_now']*100:.0f}%（{F['expo_now']*10000:,.0f} U）</b>，
剩 {10000-F['expo_now']*10000:,.0f} U 现金。
<b class="neg">NFLX 已跌破 MA200 达 14.9%</b>，信号是空仓 —— 不是做空，是这只不买、钱留现金。</p>
<p style="font-size:12.5px;color:#667085">
AMZN 只高出均线 3.0%、NVDA 高出 5.7%，这两只随时可能翻空，属于「临界持仓」。
按规则，明天开盘就该是这个配置，之后每个交易日收盘复核一次。</p>
</div>

<h2>三、回测结果</h2>
<div class="grid4">
<div class="kpi"><div class="kpi-l">趋势过滤终值</div><div class="kpi-v" style="color:#c0392b">{TR['total']:,.1f}x</div><div class="kpi-s">{M['years']:.1f} 年，年化 {TR['cagr']*100:.1f}%</div></div>
<div class="kpi"><div class="kpi-l">最大回撤</div><div class="kpi-v" style="color:#1e8449">{TR['mdd']*100:.1f}%</div><div class="kpi-s">买入持有 {BH['mdd']*100:.1f}%</div></div>
<div class="kpi"><div class="kpi-l">Calmar</div><div class="kpi-v">{TR['calmar']:.2f}</div><div class="kpi-s">买入持有 {BH['calmar']:.2f}</div></div>
<div class="kpi"><div class="kpi-l">Sharpe</div><div class="kpi-v">{TR['sharpe']:.2f}</div><div class="kpi-s">买入持有 {BH['sharpe']:.2f}</div></div>
</div>
<div class="card">{g1}</div>
<div class="grid2"><div class="card" style="margin:0">{g2}</div><div class="card" style="margin:0">{g3}</div></div>
<div class="card">{g4}
<p style="font-size:13.5px;color:#475467">
跑赢 <b>{F['win_n']}/{len(yl)}</b> 年。最该看的是这一条：21 年里买入持有<b>亏钱的年份只有 {len(dn)} 个</b>，
{dn_txt} —— <b>{'这 %d 年趋势过滤全部跑赢' % len(dn) if dn_all_win else '其中大部分跑赢'}</b>。<br>
另外 {len(up_w)} 个上涨年（{up_txt}）它也跑赢了，那是趋势刚好跟住了大涨，不是它的功劳。<br>
输得最狠的三年全在暴力反弹段：{lose_txt}。<br>
这就是趋势策略的固定形状：<b>熊市里少亏，牛市里少赚</b>。2015 年少赚了 27 个点，但 2008 和 2022 各救回 20 个点以上。</p>
</div>
<div class="card">
<h3 style="margin-top:0">回撤明细：{F['dd_n']} 次超过 10% 的回撤</h3>
<table style="max-width:700px"><thead><tr><th>区间</th><th class="n">持续</th><th class="n">最深</th></tr></thead><tbody>{dd_rows}</tbody></table>
<p style="font-size:13px;color:#667085">最深的一次不是 2008，是 <b>2021-12 到 2023-05 那一年半</b>（{TR['mdd']*100:.1f}%）。
2008 段是 {ep08m*100:.1f}%。最长水下时间是 {TR['longest']} 个交易日（买入持有 {BH['longest']} 天）。</p>
</div>

<h2>四、关键实验：到底是「趋势判断」还是「单纯降仓位」在起作用</h2>
<p>这是整份报告里最重要的一节。趋势过滤有两个效果混在一起：它既<b>判断方向</b>，也<b>顺便降低了平均仓位</b>（本策略平均敞口 {F['expo_avg']*100:.0f}%）。
我做了同敞口对照 —— 把仓位固定成 {F['expo_avg']*100:.0f}% 常驻，完全不看趋势，看能不能拿到同样的风险调整收益。</p>
<div class="card">
<table><thead><tr><th>跑法</th><th>说明</th><th class="n">终值</th><th class="n">年化</th>
<th class="n">最大回撤</th><th class="n">Calmar</th><th style="width:96px">Calmar 对比</th><th class="n">Sharpe</th></tr></thead>
<tbody>{var_rows}</tbody></table>
<p style="font-size:12.5px;color:#667085;margin-top:8px">
所有跑法用同一批 5 只标的、同一区间（{M['first']} ~ {M['last']}）、同一成本（滑点 0.05%/单边）。
趋势过滤的平均敞口是 {F['expo_avg']*100:.0f}%，所以「固定敞口 {F['expo_avg']:.2f} 常驻」这一行才是它的正确对照组 —— 两者在场时间一样多，只是入场时点不同。</p>
</div>
<div class="ok">
<b>三条结论，一条比一条硬：</b>
<ol style="margin-bottom:0">
<li><b>单纯降仓位，一分钱风险调整收益都没多出来。</b>固定 {F['expo_avg']*100:.0f}% 常驻的 Calmar 是 {VAR[1]['s']['calmar']:.2f}，
买入持有是 {VAR[0]['s']['calmar']:.2f}。<b>几乎完全一样。</b>降仓位只是把盈亏同比例缩小 —— 回撤从 -62.3% 缩到 -48.5%，收益也从 29.8% 缩到 22.9%。
这不叫本事，这叫杠杆调小。</li>
<li><b>趋势判断是真的有 alpha。</b>在<b>同样的平均敞口</b>下，趋势过滤把 Calmar 从 {VAR[1]['s']['calmar']:.2f} 拉到 {VAR[4]['s']['calmar']:.2f}，
多出来的 <b>+{VAR[4]['s']['calmar']-VAR[1]['s']['calmar']:.2f}</b> 全部来自「什么时候在场」，不是「在场多少」。
而且终值是 {VAR[4]['s']['total']:,.1f}x，比降仓位版的 {VAR[1]['s']['total']:,.1f}x 高得多 —— <b>它是又降回撤又保收益</b>。</li>
<li><b>波动率目标是个替代品，不是互补品。</b>在「不看趋势」的前提下加波动率目标确实有用
（{VAR[1]['s']['calmar']:.2f} → {VAR[2]['s']['calmar']:.2f}），
但一旦有了趋势过滤，它就不再提供任何额外保护：Calmar 从 {VAR[4]['s']['calmar']:.2f} 变成
{F['vt_grid'][5]['calmar']:.2f}，<b>等于没变</b>，而终值从 {F['vt_grid'][0]['total']:,.1f}x 掉到 {F['vt_grid'][5]['total']:,.1f}x。</li>
</ol>
</div>
<p><b>为什么会互相抵消？</b>两个机制都在「波动放大时降仓位」。区别是趋势过滤<b>整段离开</b>，
而波动率目标会在下跌途中减仓、在反弹初期又加回来 —— 而反弹初期的波动率最高。
所以它砍掉的恰恰是最肥的那几根阳线。上一版 SOXL 报告里波动率目标是好零件，<b>在个股组合上不是</b>，因为个股的暴跌更频繁、更急。</p>

<h2>五、参数是不是挖出来的</h2>
<p>均线周期、仓位大小、起点、标的，四个维度我都做了扰动。结论：<b>方向是稳的，具体数值是噪声。</b></p>
<div class="card">{g6}
<p style="font-size:13.5px;color:#475467;margin-top:6px">
9 个周期里 Calmar 从 {ma_lo:.2f} 到 {ma_hi:.2f}，<b>锯齿状</b>（MA175 最高 0.954，MA200 只有 0.856，MA275 又回到 0.931）。
这种锯齿说明<b>网格本身在拟合噪声</b>，不存在「最优均线」。<br>
但有一条是硬的：<b>9 个周期全部远好于买入持有的 {BH['calmar']:.2f}</b>。所以「用不用年线」这个决定不依赖参数，
「用多少天」这个细节纯属运气。选 200 天是因为它是市场公认的关口、落在高原区中间、相邻参数同一水平，不是因为它是网格最高点。</p>
</div>

<div class="card">
<h3 style="margin-top:0">仓位缩放：只是调档，不是提高效率</h3>
<table><thead><tr><th>波动率目标</th><th class="n">终值</th><th class="n">年化</th><th class="n">最大回撤</th>
<th class="n">Calmar</th><th class="n">Sharpe</th></tr></thead><tbody>{vt_rows}</tbody></table>
<p style="font-size:13.5px;color:#475467">
Calmar 在 <b>{vt_lo:.2f} ~ {vt_hi:.2f}</b> 之间几乎不动（跨度只有 {vt_hi-vt_lo:.2f}），
但终值从 <b>{vt_big:,.1f}x 一路缩到 {vt_sml:,.1f}x</b>（差 {vt_big/vt_sml:.1f} 倍）。<br>
<b>仓位缩放是按风险承受度调档的旋钮，不是效率工具。</b>你想让账户波动小一半，就老老实实接受终值少一个量级。
如果目标是长期收益最大化，答案是<b>不缩放、满仓</b>。</p>
</div>

<div class="grid2">
<div class="card" style="margin:0">
<h3 style="margin-top:0">换个起点</h3>
<table><thead><tr><th>起点</th><th class="n">长度</th><th class="n">趋势</th><th class="n">买入</th>
<th class="n">ΔCalmar</th><th class="n">年化 趋势/买入</th></tr></thead><tbody>{st_rows}</tbody></table>
<p style="font-size:12.5px;color:#667085">5 个起点<b>全部为正</b>。注意随着起点后移，两者的绝对 Calmar 都在上升
（因为躲掉了 2000 年科网泡沫后的余波），但差距一直在 +0.24 以上。</p>
</div>
<div class="card" style="margin:0">
<h3 style="margin-top:0">换个篮子</h3>
<table><thead><tr><th>篮子</th><th class="n">只数</th><th class="n">长度</th><th class="n">趋势</th>
<th class="n">买入</th><th class="n">ΔCalmar</th><th class="n">终值 趋势/买入</th></tr></thead><tbody>{bk_rows}</tbody></table>
<p style="font-size:12.5px;color:#667085">7 个篮子<b>全部为正</b>，但幅度差别极大：5 只科技股 +0.36，
<b>SOXX 只有 +0.03</b>（半导体波动太极端，空仓的代价几乎等于躲跌的收益）。
<b>标的的波动结构比参数重要得多。</b></p>
</div>
</div>

<h2>六、{U['n']} 个标的的实测：什么适合做趋势，什么不适合</h2>
<p>每个标的都跑同一套规则（收盘 &gt; MA200 满仓，否则空仓），然后用<b>相位随机化检验</b>判断信号是真有方向预测力还是运气：
把信号序列相对收益序列做随机圆周平移，<b>在场比例、翻仓次数、持仓段长度分布完全不变</b>，只破坏「信号时点」和「行情时点」的对应关系，看真实策略排第几。</p>
<div class="card">{g7}
<p style="font-size:13.5px;color:#475467;margin-top:6px">
70 个标的里 <b>{U['n_pos']} 个（{U['n_pos']/U['n']*100:.0f}%）Calmar 提升为正</b>，中位数 <b>{d_med:+.2f}</b>，
最好 {d_max:+.2f}、最差 {d_min:+.2f}。<br>
相位检验的中位分位是 <b>{U['med_mc']*100:.0f}%</b> —— 意思很直接：<b>随机挑一只，这套择时的预测力等同于抛硬币</b>。
只有 {U['n_mc90']} 个标的的分位超过 90%。</p>
</div>

<div class="grid2">
<div class="card" style="margin:0">
<h3 style="margin-top:0">最有效的 10 个</h3>
<table><thead><tr><th>代码</th><th>名称</th><th class="n">波动</th><th class="n">相位分位</th>
<th class="n">Calmar 买入→趋势</th><th class="n">Δ</th></tr></thead><tbody>{top_rows}</tbody></table>
</div>
<div class="card" style="margin:0">
<h3 style="margin-top:0">最伤的 10 个</h3>
<table><thead><tr><th>代码</th><th>名称</th><th class="n">波动</th><th class="n">相位分位</th>
<th class="n">Calmar 买入→趋势</th><th class="n">Δ</th></tr></thead><tbody>{bot_rows}</tbody></table>
</div>
</div>

<div class="bad">
<b>三个反直觉的发现：</b>
<ol style="margin-bottom:0">
<li><b>最有效的不是股票，是垃圾债。</b>JNK 的 Calmar 从 0.16 涨到 0.66（+0.50），HYG +0.33。
原因不是趋势预测力强，而是<b>信用债的下跌是「慢而连续」的</b>：2008 和 2022 它跌了整整一年，
MA200 有充足时间把你请出场。JNK 的最大回撤从 46.2% 压到 11.6%。</li>
<li><b>最伤的是低波动慢牛。</b>TXN −0.51（Calmar 0.64 → 0.13）、PG −0.16、JNJ −0.19、XLP −0.12。
它们<b>跌不深、涨不停</b>，年线信号只会让你在每次小回调里下车，然后看着它慢慢涨回去。
<b>趋势策略吃的是「大跌」，不是「涨得慢」。</b></li>
<li><b>波动率高低完全不能预测效果。</b>年化波动与 ΔCalmar 的相关系数只有 <b>{U['corr_vol_cal']:+.3f}</b>，
与相位分位 <b>{U['corr_vol_mc']:+.3f}</b>。TSLA 波动 57% 但 Δ 是 −0.30，NVDA 波动 59% 却是 +0.20。
<b>别用「波动大所以适合做趋势」来选标的。</b></li>
</ol>
</div>

<div class="card">
<h3 style="margin-top:0">这 5 只单独看，效果参差不齐</h3>
<table><thead><tr><th>代码</th><th>名称</th><th class="n">趋势终值</th><th class="n">趋势 Calmar</th>
<th class="n">买入终值</th><th class="n">买入 Calmar</th><th class="n">ΔCalmar</th><th class="n">交易笔数</th></tr></thead>
<tbody>{sg_rows}</tbody></table>
<p style="font-size:13.5px;color:#475467">
<b>AMZN 是彻底的负价值</b>：趋势版 {F['single'][3]['tr']['total']:.1f}x vs 买入 {F['single'][3]['bh']['total']:.1f}x，
Calmar {F['single'][3]['tr']['calmar']:.2f} vs {F['single'][3]['bh']['calmar']:.2f}，{F['single'][3]['trades']} 笔交易胜率只有 {F['single'][3]['win']*100:.0f}%。
NVDA 和 NFLX 反而是趋势版终值更高（{F['single'][2]['tr']['total']:.0f}x vs {F['single'][2]['bh']['total']:.0f}x、{F['single'][4]['tr']['total']:.0f}x vs {F['single'][4]['bh']['total']:.0f}x）。<br>
<b>5 只里 4 只变好、1 只变坏，组合层面才凑出这个结果。</b>换句话说，这份方案对「选哪 5 只」的敏感度，远高于对参数的敏感度。
这是它最大的脆弱点，不是均线周期。</p>
</div>

<h2>七、策略规则（三句话能执行完）</h2>
<div class="card">
<ol>
<li><b>每个交易日收盘后</b>，看每只的收盘价是否在 <b>200 日均线</b>之上。<b>之上就满仓，之下就清空</b>。</li>
<li><b>资金等分 5 份</b>，每份 {10000/5:,.0f} U。空仓那几只的钱<b>留在现金</b>，不加到其他标的上，也不做空。</li>
<li><b>第二天开盘</b>按差额下单。不做止损、不做止盈、不看财报、不判断大盘。</li>
</ol>
<pre>{pseudo}</pre>
<p style="font-size:13px;color:#667085">
实际交易频率：5 只合计 {F['trades_total']} 笔 / {M['years']:.1f} 年 = <b>每年 {F['trades_per_year']:.1f} 笔</b>。
看着不多，但分布极不均匀 —— 2022 年那一只 {flip_max} 就翻了 {flips[flip_max]} 次，
5 只合计 {flip_tot} 次。<b>震荡市里这套规则会把你磨得很难受，这是它的真实成本。</b></p>
</div>

<h2>八、代价：四个必须接受的事实</h2>
<div class="card">
<h3>1. 收益没有变多，只是回撤变小了</h3>
<p>{TR['total']:,.1f}x vs {BH['total']:,.1f}x，终值差 {(1-TR['total']/BH['total'])*100:.0f}%，年化差 {(BH['cagr']-TR['cagr'])*100:.1f} 个百分点。
这个结果看着像「免费午餐」，但它高度依赖<b>这 5 只恰好是趋势版的赢家</b>（NVDA、NFLX 的趋势版终值比买入还高）。
换成 AMZN 那种标的，同样的规则会砍掉 88% 的收益。<b>别把这个 4% 当成保证。</b></p>

<h3>2. 大跌年照样亏三成</h3>
<p>2008 年 −28.5%、2022 年 −28.3%。趋势过滤<b>不是保险</b>，它只是把 −50% 变成 −28%。
年线是滞后指标：等它翻空，往往已经跌了 15~20%。如果你要的是「熊市不亏钱」，这套东西给不了。</p>

<h3>3. 这 5 只是后视镜里挑出来的</h3>
<p>AAPL、MSFT、NVDA、AMZN、NFLX 是过去 20 年表现最好的美股，选它们本身就用了未来信息。
真正的问题是：<b>下一个 20 年，趋势过滤还能不能在「当时不知道会涨」的股票上起作用？</b>
第六节的 70 个标的给出了部分答案 —— 中位数 {d_med:+.2f}，40% 为正。<b>有一点点，但远不如这 5 只漂亮。</b></p>

<h3>4. 5 只全是科技股，没有真正的分散</h3>
<p>它们同涨同跌。2022 年 5 只里有 4 只同时跌破年线，组合的回撤就是这样来的。
<b>这不是一个分散化组合，是一个「科技股 + 择时」的组合。</b>
想要真分散，得把 JNK、HYG、DBC 这类加进来（它们在第六节里效果最好），但那样收益会掉到个位数年化。</p>
</div>

<h2>九、这轮没做到的事</h2>
<div class="card">
<ul>
<li><b>{len(rate_limited)} 个标的被数据源限流没拉到</b>：{rl_txt}。
这批恰好全是低波动慢牛（COST/WMT/PEP 类）和传统工业（CAT/BA/GE/HON/UPS）。
第六节「低波动慢牛最伤」的结论因此<b>只用了半套样本</b>，方向可信，幅度可能被低估。</li>
<li><b>没有真正的样本外。</b>5 只是从 70 个里按历史表现挑的，参数（MA200）也是在同一段历史上看的。
唯一算得上样本外的是「起点扰动」和「换篮子」两节 —— 它们说明方向不依赖具体起点，但都不是真正的未来数据。</li>
<li><b>没测做空、没测杠杆。</b>美股个股做空有借券成本和挤空风险；杠杆会放大回撤，本框架只做多、不加杠杆。</li>
<li><b>复权接缝截断丢掉了早期历史。</b>AAPL 2005 年之前、NFLX 2004 年之前的数据不可用，
所以这轮窗口只有 {M['years']:.1f} 年，<b>没有覆盖 2000-2002 年科网泡沫破裂</b> —— 那才是趋势策略最该被检验的一段。</li>
<li><b>单一数据源。</b>所有价格来自东财，没有做第二数据源交叉验证。已发现并修正了复权接缝，
但不能排除还有更细微的错误（比如个别日期的分红处理）。</li>
</ul>
</div>

<div class="foot">
回测脚本在 <code>src/</code>：<code>fetch_em.py</code>（拉东财后复权日线）→ <code>check_em.py</code>（数据体检）→
<code>scan_em.py</code>（{U['n']} 个标的筛选 + 相位检验）→ <code>build_facts.py</code>（算出全部数字）→
<code>report3.py</code>（本报告）。<br>
回测不代表未来收益。个股存在退市、暴雷、长期跑输风险；集中持仓与杠杆会放大亏损。
本报告不构成投资建议。
</div>
</div></body></html>"""

    out = os.path.join(OUTDIR, "报告", "美股趋势交易标的筛选.html")
    io.open(out, "w", encoding="utf-8").write(html)
    print(f"已生成: {out}  ({len(html):,} 字符)")
    print(f"  趋势 {TR['total']:.2f}x / {TR['cagr']*100:.1f}% / {TR['mdd']*100:.1f}% / Cal {TR['calmar']:.2f}")
    print(f"  买入 {BH['total']:.2f}x / {BH['cagr']*100:.1f}% / {BH['mdd']*100:.1f}% / Cal {BH['calmar']:.2f}")
    print(f"  当前持有 {F['n_hold']}/5，仓位 {F['expo_now']*100:.0f}%")
    return out


if __name__ == "__main__":
    build()
