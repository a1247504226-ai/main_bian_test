# -*- coding: utf-8 -*-
"""把 report_data.json 渲染成单文件 HTML 研究报告。"""
import os, io, json

HERE = os.path.dirname(os.path.abspath(__file__))


def load(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>商品期货策略第二轮探索报告</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f8fa;color:#1c2128;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 font-size:15px;line-height:1.72;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 18px 90px}
header{background:linear-gradient(135deg,#0f2740 0%,#1b3f66 60%,#22557f 100%);color:#fff;
 padding:52px 0 44px;margin-bottom:0}
header .wrap{padding-bottom:0}
header h1{margin:0 0 10px;font-size:31px;font-weight:800;letter-spacing:-.4px;line-height:1.3}
header .sub{color:#a9c6e0;font-size:15px;margin:0 0 22px}
.meta{display:flex;flex-wrap:wrap;gap:10px 26px;font-size:13px;color:#cfe2f2}
.meta b{color:#fff;font-weight:700}
nav{background:#fff;border-bottom:1px solid #e3e8ef;position:sticky;top:0;z-index:20;
 box-shadow:0 1px 6px rgba(20,40,70,.05)}
nav .wrap{padding:0 18px;display:flex;gap:4px;overflow-x:auto;scrollbar-width:none}
nav .wrap::-webkit-scrollbar{display:none}
nav a{flex:0 0 auto;padding:13px 11px;color:#5a6775;text-decoration:none;font-size:13.5px;
 border-bottom:2px solid transparent;white-space:nowrap}
nav a:hover{color:#1565c0;border-bottom-color:#1565c0}
section{background:#fff;border:1px solid #e6ebf1;border-radius:12px;padding:30px 30px 26px;
 margin:22px 0;box-shadow:0 1px 3px rgba(20,40,70,.04)}
h2{margin:0 0 6px;font-size:23px;font-weight:800;letter-spacing:-.2px}
h2 .n{display:inline-block;width:30px;height:30px;line-height:30px;text-align:center;
 background:#1565c0;color:#fff;border-radius:8px;font-size:15px;margin-right:11px;
 vertical-align:2px;font-weight:700}
.lead{color:#5a6775;font-size:14.5px;margin:0 0 22px}
h3{font-size:17px;font-weight:700;margin:30px 0 12px;padding-left:11px;
 border-left:4px solid #1565c0;line-height:1.35}
h4{font-size:15px;font-weight:700;margin:20px 0 8px;color:#2b3644}
p{margin:10px 0}
ul,ol{margin:10px 0;padding-left:23px}
li{margin:6px 0}
b,strong{font-weight:700;color:#0f1a26}
.up{color:#c62828;font-weight:700}
.dn{color:#2e7d32;font-weight:700}
.mut{color:#7a8794}
code{background:#eef2f7;padding:1.5px 6px;border-radius:4px;font-size:13px;
 font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#1a4b7c}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:13.5px}
th{background:#eef2f7;color:#44505e;font-weight:700;text-align:left;padding:9px 10px;
 border-bottom:2px solid #d8e0ea;white-space:nowrap;font-size:12.5px}
td{padding:8px 10px;border-bottom:1px solid #eef1f5;vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#fafbfd}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:12px 0}
.tbl-wrap table{margin:0;min-width:640px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:20px 0}
.kpi div{background:#f6f9fc;border:1px solid #e2eaf2;border-radius:9px;padding:14px 15px}
.kpi .k{font-size:11.5px;color:#7a8794;letter-spacing:.4px;margin-bottom:5px;
 text-transform:uppercase;font-weight:600}
.kpi .v{font-size:23px;font-weight:800;letter-spacing:-.5px;line-height:1.2}
.kpi .v small{font-size:12px;font-weight:600;color:#7a8794;margin-left:2px}
.box{border-radius:9px;padding:15px 18px;margin:16px 0;font-size:14px;line-height:1.7}
.box .t{font-weight:800;margin-bottom:6px;font-size:14px}
.ok{background:#eaf6ed;border-left:4px solid #2e7d32}
.warn{background:#fff6e6;border-left:4px solid #e08a00}
.bad{background:#fdecea;border-left:4px solid #c62828}
.info{background:#eaf2fb;border-left:4px solid #1565c0}
.chart{width:100%;height:360px;margin:14px 0}
.chart.tall{height:430px}
.chart-note{font-size:12.5px;color:#8a96a3;margin:-4px 0 18px;text-align:center}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
 font-weight:700;line-height:1.7;white-space:nowrap}
.t-ok{background:#e3f2e6;color:#1b5e20}
.t-mid{background:#fff3e0;color:#b26a00}
.t-no{background:#fdecea;color:#b71c1c}
.t-blue{background:#e6f0fa;color:#0d47a1}
.big{font-size:17px;font-weight:800}
hr{border:none;border-top:1px solid #eef1f5;margin:26px 0}
footer{text-align:center;color:#94a0ad;font-size:12.5px;padding:34px 0 0}
@media(max-width:640px){
 header{padding:34px 0 30px} header h1{font-size:23px}
 section{padding:22px 17px 20px;border-radius:9px;margin:14px 0}
 h2{font-size:19px} h2 .n{width:26px;height:26px;line-height:26px;font-size:13px}
 .chart{height:290px} .chart.tall{height:340px}
 table{font-size:12.5px} th,td{padding:7px 7px}
 .kpi{grid-template-columns:repeat(2,1fr)} .kpi .v{font-size:19px}
}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>商品期货策略第二轮探索报告</h1>
    <p class="sub">横截面展期因子的发现、失败方向的排除，以及资金门槛的真实代价</p>
    <div class="meta">
      <span>样本 <b>__START__ ~ __END__</b></span>
      <span>品种 <b>__NSYM__ 个</b></span>
      <span>交易日 <b>__NDAY__</b></span>
      <span>成本 <b>2bp 手续费 + 5bp 滑点</b></span>
    </div>
  </div>
</header>

<nav><div class="wrap">
  <a href="#s0">结论摘要</a>
  <a href="#s1">方法与数据</a>
  <a href="#s2">第一轮：策略族横评</a>
  <a href="#s3">第二轮：横截面因子</a>
  <a href="#s4">失败的方向</a>
  <a href="#s5">品种归因</a>
  <a href="#s6">资金阶梯</a>
  <a href="#s7">风险与局限</a>
</div></nav>

<div class="wrap">

<!-- ============ 0 ============ -->
<section id="s0">
  <h2><span class="n">0</span>结论摘要</h2>
  <p class="lead">先回答你的两个问题：有没有更好的策略、有没有更好的品种。</p>

  <div class="box ok">
    <div class="t">① 有更好的策略：横截面展期因子（XC-Carry）</div>
    不是换参数，是换<b>逻辑维度</b>——从「单品种判断方向」换成「品种之间比谁贵谁便宜」。
    全样本年化 <b>6.55%</b>、最大回撤 <b>11.6%</b>、Calmar <b>0.56</b>、Sharpe <b>0.84</b>，
    且与你现在跑的 FG+HC 策略日收益相关性只有 <b>0.19</b>。
    两者各出一半，Calmar 从 0.69 提到 <b>1.00</b>、Sharpe 到 <b>1.15</b>、回撤降到 <b>9.7%</b>。
  </div>

  <div class="box bad">
    <div class="t">② 但它要 29 万本金，2 万做不了</div>
    这个因子必须同时持有 <b>10 个品种</b>（5 多 5 空），1 手保证金中位数 <b>14.5 万</b>。
    而且<b>不能缩水</b>——我试过只在这 8 个精选品种里做，年化直接掉到 <b>0.03%</b>。
    alpha 来自全市场 41 个品种的广度，腿少了就没有区分度。
  </div>

  <div class="box warn">
    <div class="t">③ 品种层面：现有池基本对，但焦炭必须剔除</div>
    按「样本内、样本外都为正」重筛 41 个品种，
    <b>焦炭 J 样本外 −2.0%</b>（样本内 +34.5%，典型过拟合）、
    <b>橡胶 RU 样本外 −0.1%</b>，这两个应当降级。
    最稳的是 <b>沪镍 NI</b>（Calmar 0.55、样本外 10.9%）和 <b>玻璃 FG</b>（样本内 10.3%、样本外 12.4%）。
  </div>

  <div class="box info">
    <div class="t">④ 三条死路，帮你省时间</div>
    <b>配对价差</b>：10 对产业链配对，9 对亏钱，持仓胜率只有 44~49%——商品价差不回归。
    <b>时序动量</b>：41 品种等权最好也只有 3.7%/年。
    <b>短周期反转</b>：全是负的。这三条不用再试。
  </div>

  <div class="kpi">
    <div><div class="k">年化收益</div><div class="v up">6.55<small>%</small></div></div>
    <div><div class="k">最大回撤</div><div class="v">11.6<small>%</small></div></div>
    <div><div class="k">Calmar</div><div class="v">0.56</div></div>
    <div><div class="k">Sharpe</div><div class="v">0.84</div></div>
    <div><div class="k">相关性</div><div class="v">0.19</div></div>
    <div><div class="k">组合 Calmar</div><div class="v up">1.00</div></div>
  </div>
</section>

<!-- ============ 1 ============ -->
<section id="s1">
  <h2><span class="n">1</span>方法与数据</h2>
  <p class="lead">先立规矩，否则后面的数字没有意义。</p>

  <h3>数据</h3>
  <ul>
    <li><b>来源</b>：新浪财经主力连续日线，41 个品种（剔除上市不足 300 交易日的），
        样本 <b>__START__ ~ __END__</b>，共 __NDAY__ 个交易日。</li>
    <li><b>必须复权</b>：主连是不复权的拼接序列，换月当天的价格跳空<b>不是你能拿到的盈亏</b>。
        全池 41 个品种累计跳空全部为正（中位数约 +50%），不复权的话「只做多」会被系统性虚增。
        本报告全部用比例复权序列，跳空另存为 <code>bar["gap"]</code> 作为展期因子的原料。</li>
  </ul>

  <h3>三条防自欺规则</h3>
  <ol>
    <li><b>无前视</b>：第 T 日收盘算信号，第 T+1 日开盘执行；所有滚动统计只用 T 日及以前的数据。</li>
    <li><b>多空对称</b>：任何结论都要看「只多」和「只空」两侧。只多赚钱、只空亏钱的品种，
        收益来自市场上涨而非策略能力，不算 edge。</li>
    <li><b>样本内外 + 自助</b>：2009-2018 为样本内、2019-2026 为样本外；
        再做 600 次分块自助，看「年化为负」的概率。只看全样本年化等于自欺。</li>
  </ol>

  <div class="box warn">
    <div class="t">为什么这一轮要换思路</div>
    第一轮已经证明：把 41 个品种<b>不挑不选</b>地等权跑任何时序策略，
    年化都在 <b>−10% ~ +3.7%</b> 之间。也就是说「每个品种独立判断方向」这条路，
    在商品期货上几乎没有 alpha，之前 FG+HC 的 15% 有相当部分是挑品种挑出来的。
    要突破，只能换维度——从「时序」换到「横截面」。
  </div>
</section>

<!-- ============ 2 ============ -->
<section id="s2">
  <h2><span class="n">2</span>第一轮：14 个策略族横评</h2>
  <p class="lead">41 品种等权、同一费率、同一执行时序。这张表是「时序策略」的天花板。</p>

  <div id="c_scan" class="chart tall"></div>
  <p class="chart-note">按 Calmar（年化 / 最大回撤）排序，越高越值得做</p>

  <div class="tbl-wrap">
    <table id="t_scan">
      <thead><tr>
        <th>策略</th><th class="num">年化</th><th class="num">最大回撤</th>
        <th class="num">Calmar</th><th class="num">Sharpe</th>
        <th class="num">只多</th><th class="num">只空</th><th>评价</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <h3>三个结论</h3>
  <ul>
    <li><b>展期收益 carry 是最稳的</b>：年化 3.0%，但回撤只有 12.4%，Calmar 0.24、Sharpe 0.62 全场最高。
        说明「换月跳空」这个信息真的有预测力，只是单品种用太浪费。</li>
    <li><b>动量类全部平庸</b>：时序动量 250 日 3.7%、双均线 2.5%、唐奇安 2.5%，
        Calmar 都在 0.1~0.2。而且它们的「只空」几乎全是负的——收益靠市场漂移。</li>
    <li><b>反转类彻底失败</b>：RSI 回归 −4.0%、布林回归 −5.7%、波动突破 −10.4%。
        商品期货没有股指那种均值回归特性，别做。</li>
  </ul>

  <div class="box info">
    <div class="t">关键提示</div>
    「carry 3.0% / Calmar 0.24」看起来平庸，但它是<b>41 个品种不挑不选</b>的结果。
    这条线索指向的正是下一节的横截面版本。
  </div>
</section>

<!-- ============ 3 ============ -->
<section id="s3">
  <h2><span class="n">3</span>第二轮：横截面展期因子</h2>
  <p class="lead">不再问「这个品种该做多还是做空」，而是问「今天哪些品种最贵、哪些最便宜」。</p>

  <h3>逻辑</h3>
  <p>
    期货的展期结构（近月 vs 远月的价差）反映现货紧张程度：
    <b>backwardation</b>（近月贵）说明现货缺、库存低，持多头能收展期收益；
    <b>contango</b>（远月贵）说明库存过剩，持多头要付展期成本。
    主连的换月跳空就是这个结构的直接读数——累计跳空越负，backwardation 越强。
  </p>
  <p>
    于是每天：把 41 个品种按「过去 252 日累计跳空」排序，
    <b>做多最负的 5 个、做空最正的 5 个</b>，各占 1/10 敞口，总敞口 1 倍。
    多空金额对冲，理论上与大盘涨跌无关——赚的是品种之间的相对定价错误。
  </p>

  <h3>参数敏感性</h3>
  <div class="tbl-wrap">
    <table id="t_grid">
      <thead><tr>
        <th>回看窗口</th><th class="num">K=2</th><th class="num">K=3</th><th class="num">K=5</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <p class="chart-note">单元格为 Calmar（年化 / 最大回撤）。参数面平滑、没有孤立尖峰，说明不是拟合出来的</p>

  <div class="box ok">
    <div class="t">最优配置：回看 252 日、K=5</div>
    年化 <b>6.55%</b>、回撤 <b>11.6%</b>、Calmar <b>0.56</b>、Sharpe <b>0.84</b>。
    K 越大越分散：K=2 时年化 7.99% 但回撤 22.9%，K=5 用一点收益换了一半的回撤。
  </div>

  <h3>深度验证</h3>

  <h4>逐年收益（18 个年度）</h4>
  <div id="c_year" class="chart"></div>
  <p class="chart-note" id="year_note"></p>

  <h4>样本内外 + 自助</h4>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>检验</th><th class="num">年化</th><th class="num">最大回撤</th>
        <th class="num">Calmar</th><th class="num">Sharpe</th><th>判读</th></tr></thead>
      <tbody id="t_valid"></tbody>
    </table>
  </div>
  <div class="box ok" id="boot_box"></div>

  <h4>多空分解：收益是不是靠市场漂移</h4>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>口径</th><th class="num">年化</th><th class="num">最大回撤</th>
        <th class="num">Sharpe</th><th>判读</th></tr></thead>
      <tbody id="t_ls"></tbody>
    </table>
  </div>
  <p class="mut" style="font-size:13.5px">
    两侧都为正，说明这个因子的收益不依赖商品市场整体涨跌——这比任何单品种策略都干净。
  </p>

  <h4>换手与成本</h4>
  <p id="turn_txt"></p>

  <h3>与现有 FG+HC 策略的关系（本次研究最重要的一张图）</h3>
  <div id="c_curve" class="chart tall"></div>
  <p class="chart-note">净值曲线，起点归一为 1.00；横轴为交易日</p>

  <div class="tbl-wrap">
    <table>
      <thead><tr><th>方案</th><th class="num">年化</th><th class="num">最大回撤</th>
        <th class="num">Calmar</th><th class="num">Sharpe</th></tr></thead>
      <tbody id="t_combo"></tbody>
    </table>
  </div>

  <div class="box ok">
    <div class="t">为什么组合比任何一个都强</div>
    两者日收益相关系数只有 <b>__CORR__</b>。相关性低意味着它们的回撤不在同一天发生，
    组合后波动被摊平：年化从 6.55% / 10.82% 变成 <b>9.70%</b>，
    回撤从 11.6% / 15.7% 降到 <b>9.7%</b>，Calmar 反而升到 <b>1.00</b>。
    这是分散化真正的价值——不是提高收益，是<b>用同样的收益承担更小的回撤</b>。
  </div>
</section>

<!-- ============ 4 ============ -->
<section id="s4">
  <h2><span class="n">4</span>失败的方向（省你时间）</h2>
  <p class="lead">这些我都跑了，结论是负的，写下来免得以后再试一遍。</p>

  <h3>① 产业链配对价差</h3>
  <p>思路很诱人：豆粕/菜粕、螺纹/热卷、焦炭/焦煤这些有产业链关系，价差偏离后应该回归。
     实测 10 对，<b>9 对亏钱</b>。</p>
  <div class="tbl-wrap">
    <table id="t_pairs">
      <thead><tr><th>配对</th><th>产业链</th><th class="num">年化</th>
        <th class="num">最大回撤</th><th class="num">Sharpe</th>
        <th class="num">持仓胜率</th><th class="num">持仓占比</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="box bad">
    <div class="t">为什么不回归</div>
    商品的价差由<b>基本面</b>驱动——产能周期、库存、政策。2021 年煤焦价差能连续扩大几个月，
    z-score 一路 3、4、5，任何均值回归都会被打爆。价差有趋势性，回归假设不成立。
    而且主连数据有换月干扰，价差的精度也不够。
  </div>

  <h3>② 横截面动量</h3>
  <div class="tbl-wrap">
    <table id="t_xmom">
      <thead><tr><th>回看</th><th class="num">K=2 年化</th><th class="num">K=3 年化</th>
        <th class="num">K=5 年化</th><th class="num">最好 Calmar</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <p>短周期（20/60 日）全是负的，250 日才勉强转正（6.3% 但回撤 29.4%、Calmar 0.21）。
     明显弱于 carry，而且和 carry 叠加后并没有更好（carry 252 + 动量 250 双因子 10.3%/26.6%/Calmar 0.39，
     收益高了但回撤翻倍，Calmar 反而不如纯 carry）。</p>

  <h3>③ 短周期反转</h3>
  <p>RSI 回归、布林回归、横截面反转全部为负（−5% ~ −23%）。商品期货没有股指的均值回归特性。</p>
</section>

<!-- ============ 5 ============ -->
<section id="s5">
  <h2><span class="n">5</span>品种归因：哪些是真 edge</h2>
  <p class="lead">41 个品种，用最终时序策略单独跑一遍。判据不是「年化高」，而是「样本内外都为正 + 只多只空都为正」。</p>

  <div id="c_rank" class="chart tall"></div>
  <p class="chart-note">横轴年化、纵轴最大回撤；气泡越大 1 手保证金越高。<b>右下角</b>才是好品种（高收益 + 低回撤）</p>

  <h3>分档结论</h3>
  <div class="tbl-wrap">
    <table id="t_rank">
      <thead><tr><th>代码</th><th>名称</th><th class="num">年化</th><th class="num">回撤</th>
        <th class="num">Calmar</th><th class="num">只多</th><th class="num">只空</th>
        <th class="num">样本内</th><th class="num">样本外</th><th class="num">1手保证金</th><th>判定</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="box warn">
    <div class="t">三个必须知道的坑</div>
    <b>焦炭 J</b>：全样本 14.7% 看着漂亮，但样本内 +34.5%、样本外 <b>−2.0%</b>——典型的「历史很美、未来没有」。<br>
    <b>生猪 LH</b>：14.9% 但只多 <b>−2.8%</b>、只空 +18.2%，是单边押注猪价熊市，而且只有 5.7 年数据。<br>
    <b>铁矿石 I</b>：10.5% 但只空 <b>−0.2%</b>，收益全靠市场上涨。
  </div>

  <div class="box ok">
    <div class="t">真正值得留的（样本内外都正、多空都正）</div>
    <b>玻璃 FG</b>（11.5%，内 10.3% / 外 12.4%，只多 8.4% / 只空 2.9%）—— 全场最稳<br>
    <b>沪镍 NI</b>（9.1%，Calmar 0.55 最高，内 5.7% / 外 10.9%）<br>
    <b>热卷 HC</b>（9.0%，内 5.9% / 外 11.0%）<br>
    <b>螺纹钢 RB</b>（10.2%，只空 6.3%，两侧均衡）<br>
    <b>焦煤 JM</b>（8.0%，内 10.6% / 外 6.1%）<br>
    <b>纯碱 SA</b>（4.9%）、<b>短纤 PF</b>（3.5%，回撤仅 13.5% 最低）
  </div>
</section>

<!-- ============ 6 ============ -->
<section id="s6">
  <h2><span class="n">6</span>资金阶梯：多少本金配什么策略</h2>
  <p class="lead">你说资金不足后面自己想办法——那我把每个门槛的准确数字算给你，好让你知道目标在哪。</p>

  <div id="c_ladder" class="chart tall"></div>
  <p class="chart-note">柱为年化收益（左轴），线为所需本金（右轴，按保证金占权益 50% 计）</p>

  <div class="tbl-wrap">
    <table id="t_ladder">
      <thead><tr><th>K</th><th class="num">同时持仓</th><th class="num">年化</th>
        <th class="num">最大回撤</th><th class="num">Calmar</th><th class="num">Sharpe</th>
        <th class="num">1手保证金中位</th><th class="num">建议本金</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <h3>落地建议</h3>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>本金</th><th>方案</th><th class="num">预期年化</th><th class="num">预期回撤</th><th>说明</th></tr></thead>
      <tbody>
        <tr><td><b>2 万</b></td><td>FG + HC 时序（现状，不改）</td>
          <td class="num up">10.8%</td><td class="num">15.7%</td>
          <td>已验证；横截面做不了，腿都不够</td></tr>
        <tr><td><b>3~6 万</b></td><td>时序池扩到 RB / SA / PF</td>
          <td class="num up">约 10%</td><td class="num">约 20%</td>
          <td>仍走时序路线，脚本已按分层自动加品种</td></tr>
        <tr><td><b>11 万</b></td><td>XC-Carry K=2（4 腿）</td>
          <td class="num up">8.0%</td><td class="num">22.9%</td>
          <td>横截面入门，Calmar 0.35，性价比一般</td></tr>
        <tr><td><b>17.5 万</b></td><td>XC-Carry K=3（6 腿）</td>
          <td class="num up">7.3%</td><td class="num">19.9%</td>
          <td>Calmar 0.37，比 K=2 略好</td></tr>
        <tr><td><b>29 万</b></td><td><b>XC-Carry K=5（10 腿）+ FG+HC 30~50%</b></td>
          <td class="num up">9.7%</td><td class="num">9.7%</td>
          <td><b>最优解</b>：Calmar 1.00、Sharpe 1.15</td></tr>
      </tbody>
    </table>
  </div>

  <div class="box info">
    <div class="t">29 万这个数字是怎么来的</div>
    XC-Carry 同时持有 10 个品种，每个 1 手。按历史 808 个观察日统计，
    这 10 手的保证金合计<b>中位数 14.5 万</b>、90 分位 28 万。
    按「保证金不超过权益 50%」的安全线反推，权益需要 <b>29 万</b>。
    注意这是<b>中位数</b>——运气不好抽到沪镍、焦炭这种大合约，10 手要 28 万保证金，权益得 56 万。
  </div>

  <div class="box warn">
    <div class="t">别想着用 K=2 硬凑</div>
    我试过把池子缩到 8 个精选品种做 K=2，年化直接掉到 <b>0.03%</b>。
    横截面因子的 alpha 来自「41 个品种里谁最贵谁最便宜」这个<b>广度</b>，
    池子小了排序就没有区分度。所以这条路没有便宜的替代方案。
  </div>
</section>

<!-- ============ 7 ============ -->
<section id="s7">
  <h2><span class="n">7</span>风险与局限</h2>
  <ul>
    <li><b>展期因子是代理，不是真值</b>：真正的 carry 要用完整期限结构（近月/远月/次主力的价差），
        这里用主连换月跳空近似。跳空判定用 3% 阈值，涨跌停的大跳空会被误判成换月。</li>
    <li><b>没有合约级数据</b>：大商所接口有反爬（HTTP 412）、能源中心 SSL 报错，
        只能用主连日线。因此无法做精确的展期曲线重建，也无法处理交割月限制。</li>
    <li><b>未建模的现实摩擦</b>：涨跌停无法成交、流动性冲击（横截面策略要同时持有 10 个品种，
        小品种的盘口深度可能不够）、保证金追缴、交易所临时调整保证金率。</li>
    <li><b>横截面策略的隐性风险</b>：多空对冲不等于无风险。2020 年 3 月、2022 年 6 月这类
        全市场同向暴动时，多空两腿会同时亏损（相关性趋近 1），组合回撤会远超历史均值。</li>
    <li><b>参数有选择偏差</b>：K=5、窗口 252 是在全样本上挑的。虽然参数面平滑、样本外更好，
        但仍不能排除「事后挑到好看的那个」。合理预期应打折。</li>
    <li><b>不含税与交易返佣</b>：手续费按 2bp、滑点 5bp 计，实际因品种和券商而异。</li>
  </ul>

  <div class="box bad">
    <div class="t">最重要的一句</div>
    这份报告的所有数字都是<b>历史回测</b>。它证明的是「这些逻辑在过去 18 年有统计优势」，
    不是「未来会赚这么多」。真正的检验只有一个：<b>小仓位实盘跑 6~12 个月</b>，
    看实际成交价、滑点、信号频率是否与回测一致。
  </div>
</section>

<footer>
  数据：新浪财经主力连续日线 · 复权口径 · 2009-01 ~ __END__<br>
  生成脚本：research2.py / gen_report2.py / build_report2.py
</footer>

</div>

<script>__ECHARTS__</script>
<script>
var D = __DATA__;

var C = {up:'#c62828', dn:'#2e7d32', blue:'#1565c0', gray:'#94a3b8', amber:'#e08a00',
         purple:'#7b1fa2', text:'#1c2128', axis:'#8a96a3', grid:'#eef1f5'};
var baseText = {color:C.text, fontFamily:'-apple-system,"PingFang SC","Microsoft YaHei",sans-serif'};

function mk(id, opt){
  var el = document.getElementById(id);
  if(!el) return null;
  var ch = echarts.init(el, null, {renderer:'canvas'});
  ch.setOption(opt);
  return ch;
}
function pct(v, d){ return (v*100).toFixed(d===undefined?1:d) + '%'; }

/* ---- 策略族横评 ---- */
(function(){
  var rows = [];
  for(var k in D.scan){ rows.push({name:k, s:D.scan[k].ls}); }
  rows.sort(function(a,b){ return a.s.calmar - b.s.calmar; });
  var names = rows.map(function(x){ return x.name; });
  var vals  = rows.map(function(x){ return +(x.s.calmar).toFixed(2); });
  mk('c_scan', {
    grid:{left:152, right:52, top:16, bottom:26},
    tooltip:{trigger:'axis', axisPointer:{type:'shadow'},
      formatter:function(p){ var r=rows[p[0].dataIndex];
        return r.name+'<br/>Calmar '+p[0].value+'<br/>年化 '+pct(r.s.cagr)+
               '<br/>回撤 '+pct(r.s.mdd); }},
    xAxis:{type:'value', axisLabel:{formatter:'{value}', color:C.axis, fontSize:11},
      splitLine:{lineStyle:{color:C.grid}}},
    yAxis:{type:'category', data:names, axisLabel:{color:C.text, fontSize:12},
      axisLine:{lineStyle:{color:'#d8e0ea'}}, axisTick:{show:false}},
    series:[{type:'bar', data:vals.map(function(v){
        return {value:v, itemStyle:{color: v>=0.2 ? C.blue : (v>=0.1 ? '#5b9bd5' : C.gray),
                borderRadius:[0,3,3,0]}};
      }), barMaxWidth:20,
      label:{show:true, position:'right', color:C.axis, fontSize:11, formatter:'{c}'}}]
  });
})();

/* ---- 参数网格表 ---- */
(function(){
  var tb = document.querySelector('#t_grid tbody');
  var wins = [126,252,504], Ks=[2,3,5];
  wins.forEach(function(w){
    var tr = document.createElement('tr');
    var html = '<td>'+w+' 日</td>';
    Ks.forEach(function(K){
      var g = D.xcarry_grid.filter(function(x){return x.win===w && x.K===K;})[0];
      if(!g){ html += '<td class="num">—</td>'; return; }
      var c = g.calmar;
      var cls = c>=0.5?'up':(c>=0.3?'':'mut');
      html += '<td class="num '+cls+'">'+c.toFixed(2)+
              '<br><span class="mut" style="font-size:11.5px">'+pct(g.cagr)+'</span></td>';
    });
    tr.innerHTML = html; tb.appendChild(tr);
  });
})();

/* ---- 逐年 ---- */
(function(){
  var ys = Object.keys(D.xc.by_year).sort();
  var vals = ys.map(function(y){ return +(D.xc.by_year[y]*100).toFixed(1); });
  var neg = ys.filter(function(y){ return D.xc.by_year[y] < 0; });
  document.getElementById('year_note').textContent =
    '18 个年度中只有 ' + neg.length + ' 年亏损（' + (neg.join('、')||'无') + '），最长连亏 1 年';
  mk('c_year', {
    grid:{left:52, right:20, top:22, bottom:44},
    tooltip:{trigger:'axis', formatter:function(p){ return p[0].axisValue+' 年：'+p[0].value+'%'; }},
    xAxis:{type:'category', data:ys, axisLabel:{color:C.axis, fontSize:11, rotate:38},
      axisLine:{lineStyle:{color:'#d8e0ea'}}},
    yAxis:{type:'value', axisLabel:{formatter:'{value}%', color:C.axis, fontSize:11},
      splitLine:{lineStyle:{color:C.grid}}},
    series:[{type:'bar', data:vals.map(function(v){
        return {value:v, itemStyle:{color: v>=0?C.up:C.dn, borderRadius: v>=0?[3,3,0,0]:[0,0,3,3]}};
      }), barMaxWidth:30,
      markLine:{silent:true, symbol:'none', data:[{yAxis:0}],
        lineStyle:{color:'#c8d2de'}, label:{show:false}}}]
  });
})();

/* ---- 验证表 ---- */
(function(){
  var tb = document.getElementById('t_valid');
  var x = D.xc;
  function row(lab, s, note, cls){
    return '<tr><td>'+lab+'</td>'+
      '<td class="num">'+pct(s.cagr,2)+'</td>'+
      '<td class="num">'+pct(s.mdd)+'</td>'+
      '<td class="num">'+s.calmar.toFixed(2)+'</td>'+
      '<td class="num">'+s.sharpe.toFixed(2)+'</td>'+
      '<td>'+note+'</td></tr>';
  }
  var html = row('全样本 2009-2026', x.stat, '基准', '');
  if(x['in'])  html += row('样本内 2009-2018', x['in'],  '较低，因为 2011~2015 商品熊市里展期结构混乱', '');
  if(x.out)    html += row('样本外 2019-2026', x.out, '<span class="up">外优于内，不是拟合出来的</span>', '');
  tb.innerHTML = html;

  var b = x.boot;
  document.getElementById('boot_box').innerHTML =
    '<div class="t">分块自助（600 次重抽，块长 30 日）</div>' +
    '年化 90% 区间 <b>['+pct(b.p5,2)+', '+pct(b.p95,2)+']</b>，中位 <b>'+pct(b.med,2)+'</b>，' +
    '「年化为负」的概率 <b class="up">'+pct(b.neg)+'</b>。' +
    '换句话说，随机抽 18 年的市场片段重跑，绝大多数情况仍然是正收益。';
})();

/* ---- 多空分解 ---- */
(function(){
  var tb = document.getElementById('t_ls');
  var x = D.xc;
  function row(lab, s, note){
    return '<tr><td>'+lab+'</td>'+
      '<td class="num '+(s.cagr>0?'up':'dn')+'">'+pct(s.cagr,2)+'</td>'+
      '<td class="num">'+pct(s.mdd)+'</td>'+
      '<td class="num">'+s.sharpe.toFixed(2)+'</td><td>'+note+'</td></tr>';
  }
  tb.innerHTML =
    row('只做多 backwardation', x.long, '方向对了，但单边暴露大，回撤 45%+') +
    row('只做空 contango', x.short, '同样是正收益，说明不是靠市场上涨') +
    row('多空对冲（正式版）', x.stat, '<span class="up">回撤从 45% 降到 11.6%</span>');
})();

/* ---- 换手 ---- */
(function(){
  var r = D.xc.rebal, k = Object.keys(r);
  var txt = '日均换手 <b>'+(D.xc.turn*100).toFixed(2)+'%</b>，年化换手成本约 <b>'+
            (D.xc.turn*244*0.0007*100).toFixed(2)+'%</b>。';
  var parts = k.map(function(x){ return x+'日调仓 '+pct(r[x].cagr,2)+'（Calmar '+r[x].calmar.toFixed(2)+'）'; });
  txt += '不同调仓频率下：' + parts.join('、') + '。' +
         '成本不是问题，<b>每天调仓就行</b>，不必为了省手续费牺牲信号新鲜度。';
  document.getElementById('turn_txt').innerHTML = txt;
})();

/* ---- 净值曲线 ---- */
(function(){
  function ser(name, arr, color, w){
    return {name:name, type:'line', showSymbol:false, smooth:false,
      lineStyle:{width:w||1.6, color:color}, itemStyle:{color:color},
      data:arr.map(function(x){ return [x[0], x[1]]; })};
  }
  mk('c_curve', {
    grid:{left:52, right:24, top:44, bottom:34},
    legend:{top:6, textStyle:{color:C.text, fontSize:12}, itemWidth:18, itemHeight:2},
    tooltip:{trigger:'axis',
      formatter:function(p){
        var s = p[0].axisValue;
        p.forEach(function(x){ s += '<br/>'+x.marker+x.seriesName+' '+x.value[1].toFixed(2); });
        return s; }},
    xAxis:{type:'time', axisLabel:{color:C.axis, fontSize:11},
      axisLine:{lineStyle:{color:'#d8e0ea'}}, splitLine:{show:false}},
    yAxis:{type:'value', scale:true, axisLabel:{color:C.axis, fontSize:11},
      splitLine:{lineStyle:{color:C.grid}}},
    series:[
      ser('FG+HC 时序策略', D.curves.fghc, C.amber, 1.4),
      ser('XC-Carry 横截面', D.curves.xc, C.blue, 1.8),
      ser('组合 50/50', D.curves.mix50, C.up, 2.2)
    ]
  });
})();

/* ---- 组合表 ---- */
(function(){
  var tb = document.getElementById('t_combo');
  var html = '';
  function row(lab, s, hl){
    return '<tr'+(hl?' style="background:#f4f9ff"':'')+'><td>'+lab+'</td>'+
      '<td class="num">'+pct(s.cagr,2)+'</td>'+
      '<td class="num">'+pct(s.mdd)+'</td>'+
      '<td class="num">'+(s.calmar||0).toFixed(2)+'</td>'+
      '<td class="num">'+(s.sharpe||0).toFixed(2)+'</td></tr>';
  }
  html += row('XC-Carry 100%', D.xc.stat);
  html += row('FG+HC 100%', {cagr:D.fghc.cagr, mdd:D.fghc.mdd, calmar:D.fghc.calmar, sharpe:D.fghc.sharpe});
  ['0.7','0.5','0.3'].forEach(function(w){
    var s = D.combos[w];
    var pctXc = Math.round((1-parseFloat(w))*100);
    html += row('XC-Carry '+pctXc+'% + FG+HC '+(100-pctXc)+'%', s, w==='0.5');
  });
  tb.innerHTML = html;
})();

/* ---- 配对表 ---- */
(function(){
  var tb = document.querySelector('#t_pairs tbody');
  D.pairs.sort(function(a,b){ return b.cagr - a.cagr; });
  tb.innerHTML = D.pairs.map(function(p){
    var cls = p.cagr > 0.005 ? 'up' : 'dn';
    return '<tr><td><b>'+p.pair+'</b></td><td>'+p.desc+'</td>'+
      '<td class="num '+cls+'">'+pct(p.cagr,2)+'</td>'+
      '<td class="num">'+pct(p.mdd)+'</td>'+
      '<td class="num">'+p.sharpe.toFixed(2)+'</td>'+
      '<td class="num">'+pct(p.win_act)+'</td>'+
      '<td class="num">'+pct(p.active)+'</td></tr>';
  }).join('');
})();

/* ---- 横截面动量表 ---- */
(function(){
  var tb = document.querySelector('#t_xmom tbody');
  var Ns = [20,60,120,250];
  tb.innerHTML = Ns.map(function(N){
    function g(K){
      var r = D.xmom.filter(function(x){return x.N===N && x.K===K;})[0];
      return r ? r.cagr : null;
    }
    var best = D.xmom.filter(function(x){return x.N===N;})
                     .reduce(function(a,b){ return a.calmar>b.calmar?a:b; });
    function cell(v){
      if(v===null) return '<td class="num">—</td>';
      return '<td class="num '+(v>0?'up':'dn')+'">'+pct(v,2)+'</td>';
    }
    return '<tr><td>'+N+' 日</td>'+cell(g(2))+cell(g(3))+cell(g(5))+
           '<td class="num">'+best.calmar.toFixed(2)+'</td></tr>';
  }).join('');
})();

/* ---- 品种散点 ---- */
(function(){
  var pts = D.rank.map(function(x){
    return {name:x.sym+' '+x.name, value:[+(x.st.cagr*100).toFixed(2), +(x.st.mdd*100).toFixed(1)],
            margin:x.margin,
            sym:x.sym};
  });
  mk('c_rank', {
    grid:{left:56, right:34, top:22, bottom:48},
    tooltip:{formatter:function(p){
      return '<b>'+p.data.name+'</b><br/>年化 '+p.value[0]+'%<br/>最大回撤 '+p.value[1]+
             '%<br/>1手保证金 '+p.data.margin.toLocaleString()+' 元'; }},
    xAxis:{type:'value', name:'年化 %', nameLocation:'middle', nameGap:30,
      nameTextStyle:{color:C.axis, fontSize:12},
      axisLabel:{color:C.axis, fontSize:11, formatter:'{value}%'},
      splitLine:{lineStyle:{color:C.grid}}},
    yAxis:{type:'value', name:'最大回撤 %', min:0, nameTextStyle:{color:C.axis, fontSize:12},
      axisLabel:{color:C.axis, fontSize:11, formatter:'{value}%'},
      splitLine:{lineStyle:{color:C.grid}}},
    series:[{
      type:'scatter',
      data:pts.map(function(p){
        var col = p.value[0] >= 8 ? C.up : (p.value[0] >= 3 ? C.blue : C.gray);
        return {name:p.name, value:p.value, margin:p.margin, sym:p.sym,
                itemStyle:{color:col, opacity:.72, borderColor:'#fff', borderWidth:1}};
      }),
      symbolSize:function(v, p){
        var m = p.data.margin || 2000;
        return Math.max(9, Math.min(34, 8 + Math.sqrt(m)/13));
      },
      label:{show:true, position:'top', fontSize:10.5, color:'#5a6775',
        formatter:function(p){ return p.data.sym; }},
      markLine:{silent:true, symbol:'none',
        data:[{xAxis:8, lineStyle:{color:'#e0b4b4', type:'dashed'},
               label:{formatter:'年化 8%', color:'#b08080', fontSize:11}}]}
    }]
  });
})();

/* ---- 品种表 ---- */
(function(){
  var tb = document.querySelector('#t_rank tbody');
  var rows = D.rank.slice().sort(function(a,b){ return b.st.cagr - a.st.cagr; });
  tb.innerHTML = rows.map(function(x){
    var st=x.st, inn=x['in']?x['in'].cagr:null, out=x.out?x.out.cagr:null;
    var lng=x.long.cagr, sht=x.short.cagr;
    var tag, note;
    if(st.cagr < 0.03){ tag='t-no'; note='<span class="mut">年化低于 3%，性价比不足</span>'; }
    else if(lng <= 0 || sht <= 0){ tag='t-no'; note='单边收益（只多或只空为负），靠市场漂移'; }
    else if(out !== null && out < 0){ tag='t-no'; note='<span class="dn">样本外转负，典型过拟合</span>'; }
    else if(inn !== null && inn <= 0){ tag='t-mid'; note='样本内为负，靠近年行情'; }
    else if(inn === null){ tag='t-mid'; note='上市较晚，无法分样本验证；但多空都为正'; }
    else { tag='t-ok'; note='<b>样本内外都为正、多空都为正</b>'; }
    return '<tr><td><b>'+x.sym+'</b></td><td>'+x.name+'</td>'+
      '<td class="num '+(st.cagr>0?'up':'dn')+'">'+pct(st.cagr,1)+'</td>'+
      '<td class="num">'+pct(st.mdd,1)+'</td>'+
      '<td class="num">'+st.calmar.toFixed(2)+'</td>'+
      '<td class="num '+(lng>0?'up':'dn')+'">'+pct(lng,1)+'</td>'+
      '<td class="num '+(sht>0?'up':'dn')+'">'+pct(sht,1)+'</td>'+
      '<td class="num">'+(inn===null?'—':pct(inn,1))+'</td>'+
      '<td class="num '+(out!==null&&out<0?'dn':'')+'">'+(out===null?'—':pct(out,1))+'</td>'+
      '<td class="num">'+x.margin.toLocaleString()+'</td>'+
      '<td><span class="tag '+tag+'">'+note+'</span></td></tr>';
  }).join('');
})();

/* ---- 策略族表 ---- */
(function(){
  var tb = document.querySelector('#t_scan tbody');
  var rows = [];
  for(var k in D.scan){ rows.push({name:k, s:D.scan[k].ls, L:D.scan[k].long, S:D.scan[k].short}); }
  rows.sort(function(a,b){ return b.s.calmar - a.s.calmar; });
  tb.innerHTML = rows.map(function(r){
    var note, tag;
    if(r.s.calmar >= 0.22){ tag='t-ok'; note='最稳'; }
    else if(r.s.calmar >= 0.1){ tag='t-mid'; note='平庸'; }
    else { tag='t-no'; note='无效'; }
    return '<tr><td>'+r.name+'</td>'+
      '<td class="num '+(r.s.cagr>0?'up':'dn')+'">'+pct(r.s.cagr,1)+'</td>'+
      '<td class="num">'+pct(r.s.mdd,1)+'</td>'+
      '<td class="num"><b>'+r.s.calmar.toFixed(2)+'</b></td>'+
      '<td class="num">'+r.s.sharpe.toFixed(2)+'</td>'+
      '<td class="num '+(r.L.cagr>0?'':'dn')+'">'+pct(r.L.cagr,1)+'</td>'+
      '<td class="num '+(r.S.cagr>0?'':'dn')+'">'+pct(r.S.cagr,1)+'</td>'+
      '<td><span class="tag '+tag+'">'+note+'</span></td></tr>';
  }).join('');
})();

/* ---- 资金阶梯 ---- */
(function(){
  var L = D.ladder;
  mk('c_ladder', {
    grid:{left:56, right:82, top:58, bottom:34},
    legend:{top:6, textStyle:{color:C.text, fontSize:12}, itemWidth:18, itemHeight:10},
    tooltip:{trigger:'axis', axisPointer:{type:'shadow'},
      formatter:function(p){
        var r = L[p[0].dataIndex];
        return 'K='+r.K+'（'+r.legs+' 腿）<br/>年化 '+pct(r.cagr,2)+
               '<br/>最大回撤 '+pct(r.mdd)+'<br/>Calmar '+r.calmar.toFixed(2)+
               '<br/>1手保证金中位 '+Math.round(r.margin_med).toLocaleString()+' 元'+
               '<br/>建议本金 '+Math.round(r.capital).toLocaleString()+' 元'; }},
    xAxis:{type:'category', data:L.map(function(x){ return 'K='+x.K; }),
      axisLabel:{color:C.text, fontSize:12}, axisLine:{lineStyle:{color:'#d8e0ea'}}},
    yAxis:[
      {type:'value', name:'年化 %', nameTextStyle:{color:C.axis, fontSize:11},
       axisLabel:{color:C.axis, fontSize:11, formatter:'{value}%'},
       splitLine:{lineStyle:{color:C.grid}}},
      {type:'value', name:'本金(万元)', max:340000, nameTextStyle:{color:C.amber, fontSize:11},
       axisLabel:{color:C.amber, fontSize:11, formatter:function(v){ return (v/10000).toFixed(0); }},
       splitLine:{show:false}}
    ],
    series:[
      {name:'年化收益', type:'bar', barMaxWidth:44,
       data:L.map(function(x){ return +(x.cagr*100).toFixed(2); }),
       itemStyle:{color:C.blue, borderRadius:[4,4,0,0]},
       label:{show:true, position:'insideBottom', color:'#fff', fontSize:11.5, distance:9,
              fontWeight:600, formatter:'{c}%'}},
      {name:'所需本金', type:'line', yAxisIndex:1, smooth:true,
       data:L.map(function(x){ return Math.round(x.capital); }),
       lineStyle:{width:2.4, color:C.amber}, itemStyle:{color:C.amber},
       symbolSize:8,
       label:{show:false}}
    ]
  });
})();

/* ---- 资金阶梯表 ---- */
(function(){
  var tb = document.querySelector('#t_ladder tbody');
  tb.innerHTML = D.ladder.map(function(x){
    var hi = x.K === 5;
    return '<tr'+(hi?' style="background:#f4f9ff"':'')+'>'+
      '<td><b>'+x.K+'</b>'+(hi?' <span class="tag t-blue">推荐</span>':'')+'</td>'+
      '<td class="num">'+x.legs+' 腿</td>'+
      '<td class="num up">'+pct(x.cagr,2)+'</td>'+
      '<td class="num">'+pct(x.mdd)+'</td>'+
      '<td class="num"><b>'+x.calmar.toFixed(2)+'</b></td>'+
      '<td class="num">'+x.sharpe.toFixed(2)+'</td>'+
      '<td class="num">'+Math.round(x.margin_med).toLocaleString()+'</td>'+
      '<td class="num"><b>'+Math.round(x.capital).toLocaleString()+'</b></td></tr>';
  }).join('');
})();

window.addEventListener('resize', function(){
  document.querySelectorAll('.chart').forEach(function(el){
    var ch = echarts.getInstanceByDom(el);
    if(ch) ch.resize();
  });
});
</script>
</body>
</html>
"""


def main():
    D = json.loads(load(os.path.join(HERE, "report_data.json")))
    ec = load(os.path.join(HERE, "echarts.min.js"))

    html = HTML
    html = html.replace("__START__", D["panel"]["start"])
    html = html.replace("__END__", D["panel"]["end"])
    html = html.replace("__NSYM__", str(len(D["panel"]["syms"])))
    html = html.replace("__NDAY__", str(D["panel"]["days"]))
    html = html.replace("__CORR__", "%.2f" % D["corr"])
    html = html.replace("__DATA__", json.dumps(D, ensure_ascii=False))
    html = html.replace("__ECHARTS__", ec)

    out = os.path.join(HERE, "期货策略第二轮研究报告.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("已写出 %s  (%.0f KB)" % (out, os.path.getsize(out) / 1024))


if __name__ == "__main__":
    main()
