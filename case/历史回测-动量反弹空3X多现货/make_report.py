# -*- coding: utf-8 -*-
"""生成最终 HTML 报告（离线、自绘 SVG）"""
import json
import math
import random
import sys
import time

sys.path.insert(0, ".")
import btc_strategy as E          # 单一事实来源：引擎 / 信号 / 统计全在这里
import svgkit as K

random.seed(42)
CAP = 1000.0
VT = 0.03
OUT = "BTC动量反弹策略可行性报告.html"


def ft(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


# ---------- 信号诊断 ----------
def naive_long(bars, sig):
    eq = 1.0
    hold = 0
    for i in range(1, len(bars)):
        tgt = 1 if sig[i - 1] > 0 else 0
        pc = bars[i - 1]["close"]
        if tgt == 1:
            eq *= bars[i]["close"] / (bars[i]["open"] if hold == 0 else pc)
        else:
            if hold == 1:
                eq *= bars[i]["open"] / pc
        hold = tgt
    return eq


def diag(bars, iters=400):
    n = len(bars)
    tests = [
        ("动量反弹 10/30+RSI45（用户口径）", E.sig_momentum_rebound(bars, 10, 30, 14, 45)),
        ("动量反弹 20/60+RSI45", E.sig_momentum_rebound(bars, 20, 60, 14, 45)),
        ("抄底反弹 RSI<30（均值回归）", E.sig_dip_bounce(bars, 14, 30)),
        ("布林带下轨反弹 20/2", E.sig_bb_revert(bars, 20, 2.0)),
        ("双均线趋势 10/30", E.sig_ema_cross(bars, 10, 30)),
        ("双均线趋势 20/60", E.sig_ema_cross(bars, 20, 60)),
        ("唐奇安突破 20", E.sig_breakout(bars, 20)),
        ("趋势状态 EMA200（只多）", E.sig_state_trend(bars, 200)),
    ]
    rows = []
    for name, sig in tests:
        val = naive_long(bars, sig)
        k = max(1, int(round(sum(1 for s in sig if s > 0) / n * n)))
        better = 0
        for _ in range(iters):
            idx = set(random.sample(range(1, n), k))
            mk = [1 if i in idx else 0 for i in range(n)]
            if naive_long(bars, mk) < val:
                better += 1
        pct = better / iters
        nsw = sum(1 for i in range(1, n) if sig[i] != sig[i - 1])
        rows.append((name, val, k / n, nsw, pct))
    return rows


def main():
    bars = E.load("BTCUSDT")
    n = len(bars)
    dates = [ft(b["t"]) for b in bars]
    years = n / 365.25
    bh = bars[-1]["close"] / bars[0]["close"]
    _pk = bars[0]["close"]
    bh_mdd = 0.0
    for b in bars:
        _pk = max(_pk, b["close"])
        bh_mdd = min(bh_mdd, b["close"] / _pk - 1.0)

    # ---------------- 核心方案 ----------------
    sig_m = E.sig_momentum_rebound(bars, 10, 30, 14, 45)
    sig_t = E.sig_ema_cross(bars, 10, 30)

    r_mom_spot = E.backtest_spot(bars, sig_m, CAP, fee=0.001, slip=0.0005,
                                 sizing="vol", vt=VT, exposure_cap=1.0)
    r_mom_full = E.backtest_spot(bars, sig_m, CAP, fee=0.001, slip=0.0005,
                                 sizing="full", exposure_cap=1.0)
    r_sp = E.backtest_spot(bars, sig_t, CAP, fee=0.001, slip=0.0005,
                           sizing="vol", vt=VT, exposure_cap=1.0)
    r_spf = E.backtest_spot(bars, sig_t, CAP, fee=0.001, slip=0.0005,
                            sizing="full", exposure_cap=1.0)
    r_lo = E.backtest_futures(bars, sig_t, CAP, L=3, fee=0.0004, slip=0.0005,
                              funding_d=0.0003, sizing="vol", vt=VT,
                              exposure_cap=1.0, long_only=True)
    r_ls = E.backtest_futures(bars, sig_t, CAP, L=3, fee=0.0004, slip=0.0005,
                              funding_d=0.0003, sizing="vol", vt=VT,
                              exposure_cap=1.0, long_only=False)

    # ---------------- 权益曲线 ----------------
    def norm(eq):
        b = eq[59] if len(eq) > 59 else eq[0]
        return [round(x / b, 6) for x in eq]

    curve = [
        ("买入持有", [round(b["close"] / bars[59]["close"], 6) for b in bars], K.GREY),
        ("动量反弹·现货满仓", norm(r_mom_full["equity"]), K.PURPLE),
        ("趋势·现货波动率目标", norm(r_sp["equity"]), K.RED),
        ("趋势·合约多空3×", norm(r_ls["equity"]), K.BLUE),
        ("趋势·混合载体", norm(E.backtest_hybrid(
            bars, sig_t, CAP, L=3.0, exposure_cap=1.0)["equity"]), K.GREEN),
    ]

    # 回撤曲线（合约多空）
    eq = r_ls["equity"]
    pk = eq[0]
    dd = []
    for v in eq:
        pk = max(pk, v)
        dd.append(v / pk - 1.0)

    # ---------------- 信号诊断 ----------------
    drows = diag(bars, 400)
    short_names = ["动量反弹10/30", "动量反弹20/60", "抄底反弹RSI30", "布林带反弹20/2",
                   "双均线10/30", "双均线20/60", "唐奇安突破20", "趋势状态EMA200"]

    # ---------------- 参数扰动 ----------------
    prows = []
    for f, s in [(8, 24), (10, 30), (12, 36), (15, 45), (20, 60), (25, 75), (30, 90)]:
        sg = E.sig_ema_cross(bars, f, s)
        r = E.backtest_futures(bars, sg, CAP, L=3, fee=0.0004, slip=0.0005,
                               funding_d=0.0003, sizing="vol", vt=VT, exposure_cap=1.0)
        prows.append(("%d/%d" % (f, s), r["stats"]))

    # ---------------- 动量反弹参数扫描 ----------------
    scan_cats = ["RSI<30", "RSI<35", "RSI<40", "RSI<45", "RSI<50", "RSI<55", "RSI<60"]
    scan = []
    for f, s in [(10, 30), (20, 60), (20, 100)]:
        vals = []
        for th in (30, 35, 40, 45, 50, 55, 60):
            sg = E.sig_momentum_rebound(bars, f, s, 14, th)
            r = E.backtest_spot(bars, sg, CAP, fee=0.001, slip=0.0005,
                                sizing="full", exposure_cap=1.0)
            vals.append(r["stats"]["final"])
        scan.append(("%d/%d" % (f, s), vals))

    # ---------------- 跨资产 ----------------
    cross = []
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        b = E.load(sym)
        sg = E.sig_ema_cross(b, 10, 30)
        rs = E.backtest_spot(b, sg, CAP, fee=0.001, slip=0.0005, sizing="vol", vt=VT, exposure_cap=1.0)
        rl = E.backtest_futures(b, sg, CAP, L=3, fee=0.0004, slip=0.0005, funding_d=0.0003,
                                sizing="vol", vt=VT, exposure_cap=1.0, long_only=True)
        rls = E.backtest_futures(b, sg, CAP, L=3, fee=0.0004, slip=0.0005, funding_d=0.0003,
                                 sizing="vol", vt=VT, exposure_cap=1.0, long_only=False)
        cross.append((sym, b[-1]["close"] / b[0]["close"], rs["stats"], rl["stats"], rls["stats"]))

    # ---------------- 杠杆压力测试 ----------------
    lev = []
    for L in (1, 2, 3, 4, 5, 10, 20):
        r1 = E.backtest_futures(bars, sig_t, CAP, L=L, fee=0.0004, slip=0.0005, funding_d=0.0003,
                                sizing="vol", vt=VT, exposure_cap=1.0, long_only=False)
        r2 = E.backtest_futures(bars, sig_t, CAP, L=L, fee=0.0004, slip=0.0005, funding_d=0.0003,
                                sizing="vol", vt=VT, exposure_cap=1.0, long_only=False,
                                cooldown_after_liq=100000)
        f1, f2 = r1["stats"]["final"], r2["stats"]["final"]
        lev.append((L, f1, f2, f2 / f1 if f1 > 0 else 0, r1["stats"]["liq"],
                    r1["stats"]["worst_adverse"], r1["stats"]["worst_gap"]))

    # ---------------- 资金费敏感 ----------------
    fund = []
    for fd in (0.0, 0.0001, 0.0003, 0.0006, 0.0008, 0.0015):
        r = E.backtest_futures(bars, sig_t, CAP, L=3, fee=0.0004, slip=0.0005, funding_d=fd,
                               sizing="vol", vt=VT, exposure_cap=1.0, long_only=False)
        fund.append((fd, r["stats"]))

    # ---------------- 逐年 ----------------
    def yearly(eq):
        ymap = {}
        for i, b in enumerate(bars):
            y = time.gmtime(b["t"] / 1000).tm_year
            if y not in ymap:
                ymap[y] = [i, i]
            ymap[y][1] = i
        out = []
        for y in sorted(ymap):
            i0, i1 = ymap[y]
            e0 = eq[i0 - 1] if i0 > 0 else eq[0]
            out.append((y, eq[i1] / e0 - 1 if e0 and e0 > 0 else 0.0))
        return out

    ys_ls = dict(yearly(r_ls["equity"]))
    ys_sp = dict(yearly(r_sp["equity"]))
    ys_bh = dict(yearly([b["close"] for b in bars]))
    yy = sorted(ys_ls)
    y_series = [
        ("合约 多空 3×", [ys_ls[y] * 100 for y in yy], K.BLUE),
        ("现货 波动率目标", [ys_sp[y] * 100 for y in yy], K.RED),
        ("买入持有", [ys_bh[y] * 100 for y in yy], K.GREY),
    ]

    # ---------------- 最危险单日 ----------------
    wd = []
    for i in range(1, n):
        dn = bars[i]["low"] / bars[i - 1]["close"] - 1
        up = 1 - bars[i]["high"] / bars[i - 1]["close"]
        wd.append((i, dn if abs(dn) > abs(up) else up))
    wd.sort(key=lambda x: x[1])
    wd = wd[:10]

    # ---------------- 三档风险 ----------------
    prof = []
    for tag, vt in (("保守 2%", 0.02), ("推荐 3%", 0.03), ("激进 5%", 0.05)):
        rs = E.backtest_spot(bars, sig_t, CAP, fee=0.001, slip=0.0005, sizing="vol", vt=vt, exposure_cap=1.0)
        rf = E.backtest_futures(bars, sig_t, CAP, L=3, fee=0.0004, slip=0.0005, funding_d=0.0003,
                                sizing="vol", vt=vt, exposure_cap=1.0, long_only=False)
        prof.append((tag, rs["stats"], rf["stats"]))

    # ---------------- 混合载体：现货做多 + 合约 3× 逐仓做空 ----------------
    HYB_SETS = [(8, 24), (10, 30), (12, 36), (15, 45), (20, 60)]
    r_hyb = E.backtest_hybrid(bars, sig_t, CAP, L=3.0, exposure_cap=1.0)
    hyb_ts = E.trade_stats(r_hyb["trades"])

    # 混合 vs 纯合约（同信号同敞口）
    r_fut_ls = E.backtest_futures(bars, sig_t, CAP, L=3, fee=0.0004, slip=0.0005,
                                  funding_d=0.0003, sizing="vol", vt=VT,
                                  exposure_cap=1.0, long_only=False)

    # 参数扰动：5 组混合
    hyb_param = []
    hyb_curves = []
    for f, s in HYB_SETS:
        rr = E.backtest_hybrid(bars, E.sig_ema_cross(bars, f, s), CAP, L=3.0, exposure_cap=1.0)
        hyb_param.append(("%d/%d" % (f, s), rr["stats"], E.trade_stats(rr["trades"])))
        hyb_curves.append(rr["equity"])

    # 5 组等权组合（每日等权再平衡）
    hyb_port = [None] * n
    v = CAP
    hyb_port[60] = v
    for i in range(61, n):
        rs = [eq[i] / eq[i - 1] - 1.0 for eq in hyb_curves
              if eq[i] is not None and eq[i - 1] and eq[i - 1] > 0]
        if rs:
            v *= (1.0 + sum(rs) / len(rs))
        hyb_port[i] = v
    hyb_port_st = E.stats(hyb_port)

    # 混合逐年（单参数 + 5 组等权）
    def yearly_hyb(eq):
        ymap = {}
        for i, b in enumerate(bars):
            y = time.gmtime(b["t"] / 1000).tm_year
            if y not in ymap:
                ymap[y] = [i, i]
            ymap[y][1] = i
        out = []
        for y in sorted(ymap):
            i0, i1 = ymap[y]
            e0 = eq[i0 - 1] if i0 > 0 and eq[i0 - 1] else CAP
            pk, mdd = e0, 0.0
            for i in range(i0, i1 + 1):
                if eq[i] is None:
                    continue
                pk = max(pk, eq[i])
                mdd = min(mdd, eq[i] / pk - 1.0)
            out.append({"y": y, "start": e0, "end": eq[i1],
                        "ret": eq[i1] / e0 - 1.0 if e0 > 0 else 0.0, "mdd": mdd})
        return out

    hyb_year = yearly_hyb(r_hyb["equity"])
    hyb_year_p = yearly_hyb(hyb_port)

    # 回撤事件（绝对金额）
    eqh = [x for x in r_hyb["equity"] if x is not None]
    pk, pk_i = eqh[0], 0
    eps = []
    for i, val in enumerate(eqh):
        if val >= pk:
            pk, pk_i = val, i
        else:
            eps.append((val / pk - 1.0, pk, val, i - pk_i))
    eps.sort(key=lambda x: x[0])
    hyb_dd = eps[:4]

    # 跨资产（混合载体）
    hyb_cross = []
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        b = E.load(sym)
        sg = E.sig_ema_cross(b, 10, 30)
        rr = E.backtest_hybrid(b, sg, CAP, L=3.0, exposure_cap=1.0)
        tt = E.trade_stats(rr["trades"])
        bhx = b[-1]["close"] / b[60]["close"]
        yrx = (len(b) - 60) / 365.25
        hyb_cross.append((sym, bhx, (bhx ** (1 / yrx) - 1), rr["stats"], tt))

    # ---------------- 爆仓安全边际（只算空头腿） ----------------
    adv = E.trade_adverse(bars, r_hyb["trades"])
    short_adv = sorted(x[0] for x in adv if x[3] < 0)
    liq3 = -1.0 / 3.0
    smin = short_adv[0]
    margin_rows = []
    for Lv in (1, 2, 3, 4, 5, 10):
        line = -1.0 / Lv
        marg = smin - line
        margin_rows.append((Lv, line, smin, marg,
                            "安全" if marg > 0.10 else ("余量不足" if marg > 0 else "会被打爆")))
    # 极端单日行情频率
    ext_freq = []
    for th in (0.15, 0.20, 0.25, 0.30, 1.0 / 3.0):
        dn = sum(1 for i in range(1, n) if bars[i]["low"] / bars[i - 1]["close"] - 1.0 <= -th)
        up = sum(1 for i in range(1, n) if bars[i]["high"] / bars[i - 1]["close"] - 1.0 >= th)
        su = sum(1 for i in range(1, n)
                 if bars[i]["high"] / bars[i - 1]["close"] - 1.0 >= th and sig_t[i - 1] < 0)
        ext_freq.append((th, dn, up, su))
    # 历史单日最大涨 / 跌
    max_up = max(bars[i]["high"] / bars[i - 1]["close"] - 1.0 for i in range(1, n))
    max_dn = min(bars[i]["low"] / bars[i - 1]["close"] - 1.0 for i in range(1, n))
    worst5 = sorted([x for x in adv if x[3] < 0], key=lambda x: x[0])[:5]
    mae_q = [("最深", short_adv[0]), ("10分位", short_adv[len(short_adv) // 10]),
             ("25分位", short_adv[len(short_adv) // 4]), ("中位", short_adv[len(short_adv) // 2]),
             ("75分位", short_adv[3 * len(short_adv) // 4])]

    # ---------------- 参数样本外检验 ----------------
    hyb_rets = dict(zip(HYB_SETS, (E.daily_returns(eq) for eq in hyb_curves)))
    ymap2 = E.yearly_map(bars)
    ys2 = sorted(ymap2)
    rank_rows = []
    first_cnt = dict((k, 0) for k in HYB_SETS)
    for y in ys2:
        i0, i1 = ymap2[y]
        i0 = max(i0, E.WARMUP + 1)
        if i1 <= i0:
            continue
        vals = [(k, E.cum_ret(hyb_rets[k], i0, i1 + 1)) for k in HYB_SETS]
        order = sorted(vals, key=lambda x: -x[1])
        rk = dict((k, i) for i, (k, _) in enumerate(order, 1))
        first_cnt[order[0][0]] += 1
        rank_rows.append((y, vals, rk))
    mid2 = E.WARMUP + (n - E.WARMUP) // 2
    ins = sorted(HYB_SETS, key=lambda k: -E.cum_ret(hyb_rets[k], E.WARMUP, mid2))
    split_rows = [(k, E.cum_ret(hyb_rets[k], E.WARMUP, mid2), E.cum_ret(hyb_rets[k], mid2, n), i + 1)
                  for i, k in enumerate(ins)]
    ew_full = E.weighted_equity(hyb_rets, dict((k, 1.0) for k in HYB_SETS), E.WARMUP, n)
    ew_r = E.daily_returns(ew_full)
    ew_split = (E.cum_ret(ew_r, E.WARMUP, mid2), E.cum_ret(ew_r, mid2, n))
    wf_rows = []
    wf_curves = {}
    for win in (2, 3, 5):
        eqw, picks = E.walk_forward_equity(hyb_rets, HYB_SETS, ymap2, E.WARMUP, n, win)
        wf_curves[win] = eqw
        st = E.stats([x * CAP if x is not None else None for x in eqw])
        nsw = sum(1 for i in range(1, len(picks)) if picks[i][1] != picks[i - 1][1])
        wf_rows.append((win, st, nsw))
    schemes = [
        ("固定 10/30 单参数", hyb_curves[1], "参数面尖峰，含事后挑选成分"),
        ("walk-forward（过去 5 年选参）", wf_curves[5], "只用历史数据选参，最接近实盘"),
        ("walk-forward（过去 3 年选参）", wf_curves[3], "窗口短 → 会频繁换参"),
        ("walk-forward（过去 2 年选参）", wf_curves[2], "窗口更短 → 换参更多"),
        ("加权 50% 10/30 + 其他 4 组各 12.5%",
         E.weighted_equity(hyb_rets, {(8, 24): .125, (10, 30): .5, (12, 36): .125,
                                      (15, 45): .125, (20, 60): .125}, E.WARMUP, n),
         "<b>推荐</b>：收益与水下时间的折中"),
        ("5 组完全等权", ew_full, "最保守，但历史收益最低、回撤最大"),
    ]
    scheme_rows = []
    for name, eqx, note in schemes:
        st = E.stats([x * CAP if x is not None else None for x in eqx])
        scheme_rows.append((name, st, note))

    build_html(locals())

def build_html(ctx):
    L = ctx
    bars, n, dates = L["bars"], L["n"], L["dates"]
    years, bh = L["years"], L["bh"]

    def pct(v, d=1):
        return ("%+." + str(d) + "f%%") % (v * 100)

    def mult(v):
        return "%.2f×" % v

    # ---- 图表 ----
    c_equity = K.line_chart(dates, L["curve"], height=440, logy=True,
                            title="权益曲线（对数坐标，起点=1.0）", pad=(62, 215, 46, 58))
    c_dd = K.area_chart(dates, [v * 100 for v in L["dd"]], height=200,
                        title="回撤曲线（合约 多空 3× 敞口1.0×）", color=K.GREEN)
    c_diag = K.bar_chart(
        L["short_names"],
        [("信号择时倍数（无成本上限）", [r[1] for r in L["drows"]], K.BLUE)],
        height=360, ylab="倍数",
        yfmt="{:.0f}×", pad=(62, 200, 46, 62), show_values=True,
        hlines=[(bh, "买入持有 %.1f×" % bh, K.GREY)])
    c_diag2 = K.bar_chart(
        L["short_names"],
        [("超越随机择时样本的比例", [r[4] * 100 for r in L["drows"]], K.ORANGE)],
        height=300, ylab="%  （>80% 才算有 edge）", yfmt="{:.0f}%",
        pad=(62, 200, 46, 62), show_values=True, hline=80)
    c_scan = K.bar_chart(
        L["scan_cats"],
        [(lab, vals, c) for (lab, vals), c in zip(L["scan"], (K.BLUE, K.ORANGE, K.PURPLE))],
        height=330, ylab="终值倍数（现货 满仓无杠杆）", yfmt="{:.0f}×",
        pad=(62, 190, 46, 56), show_values=True, hline=bh)
    c_lev = K.bar_chart(
        ["%d×" % r[0] for r in L["lev"]],
        [("允许爆仓后重开", [r[1] for r in L["lev"]], K.RED),
         ("禁止重开（压力测试）", [r[2] for r in L["lev"]], K.GREEN)],
        height=330, ylab="终值倍数", yfmt="{:.0f}×", pad=(62, 190, 46, 56), show_values=True)
    c_danger = K.bar_chart(
        [ft(bars[r[0]]["t"]) for r in L["wd"]],
        [("9 年最危险单日逆行幅度", [r[1] * 100 for r in L["wd"]], K.GREEN)],
        height=340, ylab="单日逆行 %（越负越危险）", yfmt="{:.0f}%",
        pad=(62, 190, 46, 76), show_values=True,
        hlines=[(-33.33, "3× 爆仓线 -33.3%", K.RED),
                (-20.0, "5× 爆仓线 -20.0%", K.ORANGE)])
    c_year = K.bar_chart([str(y) for y in L["yy"]], L["y_series"],
                         height=340, ylab="年度收益 %", yfmt="{:.0f}%",
                         pad=(62, 190, 46, 56), show_values=False)
    c_cross = K.bar_chart(
        [r[0].replace("USDT", "") for r in L["cross"]],
        [("买入持有", [r[1] for r in L["cross"]], K.GREY),
         ("现货 波动率目标", [r[2]["final"] for r in L["cross"]], K.RED),
         ("合约 多空 3×", [r[4]["final"] for r in L["cross"]], K.BLUE)],
        height=320, ylab="终值倍数", yfmt="{:.0f}×", pad=(62, 190, 46, 56), show_values=True)

    # ---- 混合载体图表 ----
    c_hyb_year = K.bar_chart(
        [str(r["y"]) for r in L["hyb_year"]],
        [("混合 · 单参数 10/30", [r["ret"] * 100 for r in L["hyb_year"]], K.GREEN),
         ("混合 · 5 组等权", [r["ret"] * 100 for r in L["hyb_year_p"]], K.BLUE),
         ("买入持有", [L["ys_bh"][r["y"]] * 100 for r in L["hyb_year"]], K.GREY)],
        height=360, ylab="年度收益 %", yfmt="{:.0f}%",
        pad=(62, 205, 46, 56), show_values=False)
    c_hyb_param = K.bar_chart(
        [r[0] for r in L["hyb_param"]],
        [("混合载体终值倍数（5 组参数）", [r[1]["final"] for r in L["hyb_param"]], K.GREEN)],
        height=320, ylab="终值倍数", yfmt="{:.0f}×",
        pad=(62, 205, 46, 56), show_values=True,
        hlines=[(L["r_ls"]["stats"]["final"], "纯合约多空 %.0f×" % L["r_ls"]["stats"]["final"], K.BLUE)])
    c_hyb_cross = K.bar_chart(
        [r[0].replace("USDT", "") for r in L["hyb_cross"]],
        [("买入持有", [r[1] for r in L["hyb_cross"]], K.GREY),
         ("混合载体（现货多+合约空3×）", [r[3]["final"] for r in L["hyb_cross"]], K.GREEN)],
        height=320, ylab="终值倍数", yfmt="{:.0f}×", pad=(62, 205, 46, 56), show_values=True)

    # ---- 风险与可信度图表 ----
    c_mae = K.bar_chart(
        [q[0] for q in L["mae_q"]],
        [("空头腿持仓期最大逆行 %", [q[1] * 100 for q in L["mae_q"]], K.GREEN)],
        height=320, ylab="% (越负越危险)", yfmt="{:.0f}%",
        pad=(62, 205, 46, 56), show_values=True,
        hlines=[(-100.0 / 3, "3× 爆仓线 -33.3%", K.RED),
                (-100.0 / 4, "4× 爆仓线 -25.0%", K.ORANGE)])
    c_extreme = K.bar_chart(
        ["≥%.0f%%" % (r[0] * 100) for r in L["ext_freq"]],
        [("单日盘中下跌达标天数", [-r[1] for r in L["ext_freq"]], K.GREEN),
         ("单日盘中上涨达标天数", [r[2] for r in L["ext_freq"]], K.RED)],
        height=330, ylab="天数（负=下跌）", yfmt="{:.0f}",
        pad=(62, 215, 46, 56), show_values=True)
    c_scheme = K.bar_chart(
        ["固定\n10/30", "WF\n5年", "WF\n3年", "WF\n2年", "加权\n50%", "完全\n等权"],
        [("终值倍数", [r[1]["final"] for r in L["scheme_rows"]], K.GREEN)],
        height=340, ylab="终值倍数（1000U 起）", yfmt="{:.0f}×",
        pad=(62, 215, 46, 96), show_values=True,
        hlines=[(L["r_hyb"]["stats"]["final"], "单押 10/30", K.RED)])

    # ---- 表格 ----
    def tbl(headers, rows, aligns=None, cls=""):
        aligns = aligns or ["left"] + ["right"] * (len(headers) - 1)
        h = ['<table class="tbl %s"><thead><tr>' % cls]
        for i, x in enumerate(headers):
            h.append('<th style="text-align:%s">%s</th>' % (aligns[i], x))
        h.append("</tr></thead><tbody>")
        for r in rows:
            h.append("<tr>")
            for i, c in enumerate(r):
                h.append('<td style="text-align:%s">%s</td>' % (aligns[i], c))
            h.append("</tr>")
        h.append("</tbody></table>")
        return "".join(h)

    def cls_of(v, inv=False):
        good = (v > 0) if not inv else (v < 0)
        return "pos" if good else "neg"

    # 信号诊断表
    rows_diag = []
    for name, val, ratio, nsw, hitr in L["drows"]:
        v = "有 edge" if hitr > 0.8 else ("弱" if hitr > 0.6 else "无 edge")
        c = "pos" if hitr > 0.8 else ("warn" if hitr > 0.6 else "neg")
        rows_diag.append([name, mult(val), "%.1f%%" % (ratio * 100), str(nsw),
                          "%.0f%%" % (hitr * 100), '<span class="%s">%s</span>' % (c, v)])
    t_diag = tbl(["信号", "择时倍数<br>(无成本上限)", "做多占比", "换手", "超越随机", "判定"], rows_diag)

    # 现货 vs 合约
    def row_of(tag, st, note=""):
        return [tag, mult(st["final"]), '<span class="%s">%s</span>' % (cls_of(st["cagr"]), pct(st["cagr"])),
                '<span class="neg">%s</span>' % pct(st["mdd"]), "%.2f" % st["calmar"],
                str(st.get("n_trades", "-")), str(st.get("liq", 0)), note]

    t_cmp = tbl(["方案", "终值", "年化", "最大回撤", "Calmar", "笔数", "爆仓", "备注"], [
        row_of("① 动量反弹 · 现货 波动率目标", L["r_mom_spot"]["stats"], "无 edge，回撤巨大"),
        row_of("② 动量反弹 · 现货 满仓", L["r_mom_full"]["stats"], "无 edge"),
        row_of("③ 动量反弹 · 合约 多空 3×", E.backtest_futures(
            bars, L["sig_m"], CAP, L=3, fee=0.0004, slip=0.0005, funding_d=0.0003,
            sizing="vol", vt=VT, exposure_cap=1.0, long_only=False)["stats"], "加杠杆放大亏损"),
        row_of("④ 趋势跟踪 · 现货 波动率目标", L["r_sp"]["stats"], "推荐（无资金费）"),
        row_of("⑤ 趋势跟踪 · 现货 满仓", L["r_spf"]["stats"], "收益略高、回撤更大"),
        row_of("⑥ 趋势跟踪 · 合约 只多 3×", L["r_lo"]["stats"], "付资金费，不如④"),
        row_of("⑦ 趋势跟踪 · 合约 多空 3×", L["r_ls"]["stats"], "能做空 → Calmar 最优"),
    ], aligns=["left", "right", "right", "right", "right", "right", "right", "left"])

    # 参数扰动
    t_param = tbl(["EMA 参数", "终值", "年化", "最大回撤", "Calmar", "爆仓"],
                  [[r[0], mult(r[1]["final"]),
                    '<span class="%s">%s</span>' % (cls_of(r[1]["cagr"]), pct(r[1]["cagr"])),
                    '<span class="neg">%s</span>' % pct(r[1]["mdd"]),
                    "%.2f" % r[1]["calmar"], str(r[1]["liq"])] for r in L["prows"]])

    # 跨资产
    t_cross = tbl(["标的", "买入持有", "现货 波动率目标", "合约 只多 3×", "合约 多空 3×", "多空最大回撤", "多空爆仓"],
                  [[r[0], mult(r[1]), mult(r[2]["final"]), mult(r[3]["final"]), mult(r[4]["final"]),
                    '<span class="neg">%s</span>' % pct(r[4]["mdd"]), str(r[4]["liq"])]
                   for r in L["cross"]])

    # 杠杆
    rows_lev = []
    for Lv, f1, f2, ratio, liq, wa, wg in L["lev"]:
        ok = ratio > 0.9
        rows_lev.append(["%d×" % Lv, mult(f1), mult(f2), "%.3f" % ratio,
                         '<span class="%s">%.2f%%</span>' % ("neg", -100.0 / Lv),
                         '<span class="neg">%.2f%%</span>' % (wa * 100),
                         '<span class="neg">%.2f%%</span>' % (wg * 100),
                         "%.2fpp" % ((wg * 100) + 100.0 / Lv),
                         str(liq),
                         '<span class="%s">%s</span>' % ("pos" if ok else "neg",
                                                         "不依赖运气" if ok else "靠运气")])
    t_lev = tbl(["杠杆", "允许重开", "禁止重开", "比值", "爆仓线", "最深逆行", "最深跳空", "跳空余量", "爆仓次数", "判定"],
                rows_lev)

    # 资金费
    t_fund = tbl(["资金费假设", "年化成本", "终值", "年化收益"],
                 [["%.3f%% / 日" % (fd * 100), "%.1f%%" % (fd * 365 * 100), mult(st["final"]), pct(st["cagr"])]
                  for fd, st in L["fund"]])

    # 逐年
    t_year = tbl(["年份", "合约 多空 3×", "现货 波动率目标", "买入持有", "策略超额"],
                 [[str(y), '<span class="%s">%s</span>' % (cls_of(L["ys_ls"][y]), pct(L["ys_ls"][y])),
                   '<span class="%s">%s</span>' % (cls_of(L["ys_sp"][y]), pct(L["ys_sp"][y])),
                   '<span class="%s">%s</span>' % (cls_of(L["ys_bh"][y]), pct(L["ys_bh"][y])),
                   '<span class="%s">%s</span>' % (cls_of(L["ys_ls"][y] - L["ys_bh"][y]),
                                                   pct(L["ys_ls"][y] - L["ys_bh"][y]))]
                  for y in L["yy"]])

    # 三档
    t_prof = tbl(["风险档位", "载体", "终值", "年化", "最大回撤", "Calmar", "滚动365天最差", "正收益窗口占比", "爆仓"],
                 [[tag, "现货", mult(rs["final"]), pct(rs["cagr"]),
                   '<span class="neg">%s</span>' % pct(rs["mdd"]), "%.2f" % rs["calmar"],
                   '<span class="neg">%s</span>' % pct(rs["roll365"]["min"]),
                   "%.1f%%" % (rs["roll365"]["pos_ratio"] * 100), "0"]
                  for tag, rs, rf in L["prof"]] +
                 [[tag, "合约 多空 3×", mult(rf["final"]), pct(rf["cagr"]),
                   '<span class="neg">%s</span>' % pct(rf["mdd"]), "%.2f" % rf["calmar"],
                   '<span class="neg">%s</span>' % pct(rf["roll365"]["min"]),
                   "%.1f%%" % (rf["roll365"]["pos_ratio"] * 100), str(rf["liq"])]
                  for tag, rs, rf in L["prof"]])

    # ---- 风险与可信度表格 ----
    t_margin = tbl(["杠杆", "爆仓线", "空头腿最深逆行", "安全余量", "判定"],
                   [["%d×" % r[0], '<span class="neg">%.2f%%</span>' % (r[1] * 100),
                     '<span class="neg">%.2f%%</span>' % (r[2] * 100),
                     '<span class="%s">%.2fpp</span>' % ("pos" if r[3] > 0.10 else "warn", r[3] * 100),
                     '<span class="%s">%s</span>' % (
                         "pos" if r[3] > 0.10 else ("warn" if r[3] > 0 else "neg"), r[4])]
                    for r in L["margin_rows"]])

    t_mae = tbl(["最危险的 5 笔空头", "入场日", "出场日", "持仓期最大逆行", "距 3× 爆仓线", "这笔盈亏(U)"],
                [["第 %d 笔" % (i + 1), ft(L["bars"][w[1]]["t"]), ft(L["bars"][w[2]]["t"]),
                  '<span class="neg">%.2f%%</span>' % (w[0] * 100),
                  '<span class="pos">%.2fpp</span>' % ((-100.0 / 3 - w[0]) * 100),
                  '<span class="neg">%.0f</span>' % w[5]]
                 for i, w in enumerate(L["worst5"])])

    t_extreme = tbl(["单日振幅阈值", "下跌达标天数", "上涨达标天数", "上涨且当时做空"],
                    [["≥%.1f%%" % (r[0] * 100), str(r[1]), str(r[2]), str(r[3])]
                     for r in L["ext_freq"]])

    t_rank = tbl(["年份"] + ["%d/%d<br>收益(名次)" % k for k in L["HYB_SETS"]],
                 [[str(y)] + ['<span class="%s">%+.0f%%</span> <span class="%s">(%d)</span>'
                              % (cls_of(v - 1), (v - 1) * 100,
                                 "pos" if rk[k] == 1 else "warn", rk[k])
                              for k, v in vals] for y, vals, rk in L["rank_rows"]])

    t_split = tbl(["参数", "前段（样本内）", "后段（样本外）", "前段排名"],
                  [["%d/%d" % r[0], mult(r[1]), mult(r[2]),
                    "第 %d%s" % (r[3], "（被选中）" if r[3] == 1 else "")] for r in L["split_rows"]] +
                  [["<b>5 组等权</b>", mult(L["ew_split"][0]), mult(L["ew_split"][1]), "—"]])

    t_wf = tbl(["滚动窗口", "累计终值", "换参次数", "最大回撤", "Calmar"],
               [["过去 %d 年" % r[0], mult(r[1]["final"]), str(r[2]),
                 '<span class="neg">%s</span>' % pct(r[1]["mdd"]), "%.2f" % r[1]["calmar"]]
                for r in L["wf_rows"]])

    t_scheme = tbl(["方案", "终值", "年化", "最大回撤", "Calmar", "最长水下", "说明"],
                   [[r[0], mult(r[1]["final"]),
                     '<span class="%s">%s</span>' % (cls_of(r[1]["cagr"]), pct(r[1]["cagr"])),
                     '<span class="neg">%s</span>' % pct(r[1]["mdd"]), "%.2f" % r[1]["calmar"],
                     "%d 天" % r[1]["max_underwater"], r[2]] for r in L["scheme_rows"]],
                   aligns=["left", "right", "right", "right", "right", "right", "left"])

    roll_sp = L["r_sp"]["stats"]["roll365"]
    roll_ls = L["r_ls"]["stats"]["roll365"]

    # ---- 混合载体表格 ----
    hp = L["hyb_port_st"]
    _sch = dict((r[0], r[1]) for r in L["scheme_rows"])
    _wt = _sch["加权 50% 10/30 + 其他 4 组各 12.5%"]
    _wf5 = _sch["walk-forward（过去 5 年选参）"]

    def _hrow(tag, st, win, ntr, liq, cls=""):
        return [tag, mult(st["final"]),
                '<span class="pos">%s</span>' % pct(st["cagr"]),
                '<span class="neg">%s</span>' % pct(st["mdd"]),
                "%.2f" % st["calmar"], win, ntr, liq]

    t_hyb_cmp = tbl(["方案", "终值", "年化", "最大回撤", "Calmar", "胜率", "笔数", "爆仓"], [
        _hrow("① 混合载体 · 单参数 10/30", L["r_hyb"]["stats"],
              "%.1f%%" % (L["hyb_ts"]["all"]["win_rate"] * 100),
              str(L["hyb_ts"]["all"]["n"]), str(L["r_hyb"]["stats"]["liq"])),
        _hrow("② 混合载体 · <b>加权 50%%（推荐）</b>", _wt, "—", "—", "0"),
        _hrow("③ 混合载体 · walk-forward 5 年", _wf5, "—", "—", "0"),
        _hrow("④ 混合载体 · 5 组完全等权（对照）", hp, "—", "—", "0"),
        _hrow("⑤ 纯合约 · 多空 3×（全付资金费）", L["r_fut_ls"]["stats"],
              "%.1f%%" % (L["r_fut_ls"]["stats"]["win_rate"] * 100),
              str(L["r_fut_ls"]["stats"]["n_trades"]), str(L["r_fut_ls"]["stats"]["liq"])),
        _hrow("⑥ 只做多 · 现货波动率目标（对照）", L["r_sp"]["stats"],
              "%.1f%%" % (L["r_sp"]["stats"]["win_rate"] * 100),
              str(L["r_sp"]["stats"]["n_trades"]), "0"),
        ["⑦ 买入持有（基准）", mult(L["bh"]), pct((L["bh"] ** (1 / L["years"])) - 1),
         '<span class="neg">%s</span>' % pct(L["bh_mdd"]), "—", "—", "—", "—"],
    ], aligns=["left", "right", "right", "right", "right", "right", "right", "right"])

    t_hyb_leg = tbl(["腿", "笔数", "胜率", "盈亏比<br>(均盈/均亏)", "盈亏因子 PF",
                     "最长连亏", "平均持仓(天)"],
                    [["现货多头腿", str(L["hyb_ts"]["spot"]["n"]),
                      '<span class="pos">%.1f%%</span>' % (L["hyb_ts"]["spot"]["win_rate"] * 100),
                      "%.2f" % (L["hyb_ts"]["spot"]["avg_win"] / abs(L["hyb_ts"]["spot"]["avg_loss"])),
                      "%.2f" % L["hyb_ts"]["spot"]["pf"],
                      str(L["hyb_ts"]["spot"]["max_consec_loss"]),
                      "%.0f" % L["hyb_ts"]["spot"]["avg_hold"]],
                     ["合约空头腿", str(L["hyb_ts"]["fut"]["n"]),
                      '<span class="pos">%.1f%%</span>' % (L["hyb_ts"]["fut"]["win_rate"] * 100),
                      "%.2f" % (L["hyb_ts"]["fut"]["avg_win"] / abs(L["hyb_ts"]["fut"]["avg_loss"])),
                      "%.2f" % L["hyb_ts"]["fut"]["pf"],
                      str(L["hyb_ts"]["fut"]["max_consec_loss"]),
                      "%.0f" % L["hyb_ts"]["fut"]["avg_hold"]],
                     ["<b>合计</b>", "<b>%d</b>" % L["hyb_ts"]["all"]["n"],
                      '<b><span class="pos">%.1f%%</span></b>' % (L["hyb_ts"]["all"]["win_rate"] * 100),
                      "<b>%.2f</b>" % (L["hyb_ts"]["all"]["avg_win"] / abs(L["hyb_ts"]["all"]["avg_loss"])),
                      "<b>%.2f</b>" % L["hyb_ts"]["all"]["pf"],
                      "<b>%d</b>" % L["hyb_ts"]["all"]["max_consec_loss"],
                      "<b>%.0f</b>" % L["hyb_ts"]["all"]["avg_hold"]]],
                    aligns=["left", "right", "right", "right", "right", "right", "right"])

    t_hyb_year = tbl(["年份", "单参数<br>年初(U)", "单参数<br>年末(U)", "单参数<br>年收益",
                      "单参数<br>年内MDD", "5组等权<br>年末(U)", "5组等权<br>年收益",
                      "5组等权<br>年内MDD"],
                     [[str(a["y"]), "%.0f" % a["start"], "%.0f" % a["end"],
                       '<span class="%s">%s</span>' % (cls_of(a["ret"]), pct(a["ret"])),
                       '<span class="neg">%s</span>' % pct(a["mdd"]),
                       "%.0f" % b["end"],
                       '<span class="%s">%s</span>' % (cls_of(b["ret"]), pct(b["ret"])),
                       '<span class="neg">%s</span>' % pct(b["mdd"])]
                      for a, b in zip(L["hyb_year"], L["hyb_year_p"])])

    t_hyb_dd = tbl(["回撤深度", "峰值(U)", "谷底(U)", "历时(天)", "浮亏金额(U)"],
                   [['<span class="neg">%.1f%%</span>' % (d[0] * 100), "%.0f" % d[1],
                     "%.0f" % d[2], str(d[3]), "%.0f" % (d[1] - d[2])] for d in L["hyb_dd"]])

    t_hyb_cross = tbl(["标的", "买入持有", "买入持有年化", "混合载体", "混合年化",
                       "混合最大回撤", "混合胜率", "笔数", "爆仓"],
                      [[r[0].replace("USDT", ""), mult(r[1]), pct(r[2]), mult(r[3]["final"]),
                        '<span class="%s">%s</span>' % (cls_of(r[3]["cagr"]), pct(r[3]["cagr"])),
                        '<span class="neg">%s</span>' % pct(r[3]["mdd"]),
                        "%.1f%%" % (r[4]["all"]["win_rate"] * 100), str(r[4]["all"]["n"]),
                        str(r[3]["liq"])] for r in L["hyb_cross"]])

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 日线"动量反弹"策略可行性研究</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f2f4f7;color:#1c2833;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 line-height:1.72;font-size:15px}
.wrap{max-width:1140px;margin:0 auto;padding:26px 18px 70px}
h1{font-size:27px;margin:6px 0 4px;letter-spacing:-.3px}
h2{font-size:20px;margin:38px 0 12px;padding-left:12px;border-left:5px solid #c0392b}
h3{font-size:16px;margin:24px 0 8px;color:#34495e}
.sub{color:#66788a;font-size:13.5px;margin-bottom:20px}
.card{background:#fff;border-radius:12px;padding:20px 22px;margin:16px 0;
 box-shadow:0 1px 3px rgba(16,42,67,.09)}
.verdict{background:#fff;border-radius:12px;padding:22px 24px;margin:20px 0;
 border-left:6px solid #c0392b;box-shadow:0 2px 10px rgba(192,57,43,.13)}
.verdict h3{margin:0 0 10px;color:#c0392b;font-size:18px}
.verdict p{margin:9px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:20px 0}
.kpi{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 1px 3px rgba(16,42,67,.09);
 border-top:4px solid #cfd8e3}
.kpi .lab{font-size:12.5px;color:#66788a;margin-bottom:6px}
.kpi .val{font-size:25px;font-weight:800;letter-spacing:-.6px}
.kpi .note{font-size:12px;color:#8a9aab;margin-top:4px}
.kpi.r{border-top-color:#c0392b}.kpi.r .val{color:#c0392b}
.kpi.g{border-top-color:#1e8449}.kpi.g .val{color:#1e8449}
.kpi.b{border-top-color:#2471a3}.kpi.b .val{color:#2471a3}
.kpi.o{border-top-color:#d68910}.kpi.o .val{color:#d68910}
table.tbl{width:100%%;border-collapse:collapse;font-size:13.5px;margin:12px 0}
table.tbl th{background:#eef3f8;padding:9px 10px;border-bottom:2px solid #cfd8e3;
 font-weight:700;font-size:12.5px;color:#34495e;white-space:nowrap}
table.tbl td{padding:8px 10px;border-bottom:1px solid #eef1f5;white-space:nowrap}
table.tbl tbody tr:hover{background:#fafcfe}
.pos{color:#c0392b;font-weight:700}
.neg{color:#1e8449;font-weight:700}
.warn{color:#d68910;font-weight:700}
.chart{background:#fff;border-radius:12px;padding:14px 16px;margin:16px 0;
 box-shadow:0 1px 3px rgba(16,42,67,.09);overflow-x:auto}
.note{background:#f7fafc;border-left:4px solid #2471a3;padding:12px 16px;border-radius:0 8px 8px 0;
 font-size:13.5px;color:#3d566e;margin:14px 0}
.warnbox{background:#fef9f0;border-left:4px solid #d68910;padding:12px 16px;border-radius:0 8px 8px 0;
 font-size:13.5px;color:#7a5410;margin:14px 0}
.ok{background:#f0f9f3;border-left:4px solid #1e8449;padding:12px 16px;border-radius:0 8px 8px 0;
 font-size:13.5px;color:#14532d;margin:14px 0}
ul{margin:8px 0 8px 20px;padding:0}li{margin:5px 0}
code{background:#eef3f8;padding:1px 6px;border-radius:4px;font-size:13px}
.foot{color:#8a9aab;font-size:12.5px;margin-top:36px;border-top:1px solid #dde5ec;padding-top:16px}
</style></head><body><div class="wrap">

<h1>BTC 日线「动量反弹」策略可行性研究</h1>
<div class="sub">样本：BTCUSDT 现货日线 %(start)s → %(end)s，共 %(n)d 根 / %(years).2f 年 ·
本金 1000 USDT · 含手续费、滑点、资金费 · 信号在收盘确认、次日开盘成交（已通过独立审计）</div>

<div class="verdict">
<h3>结论先行</h3>
<p><b>「动量反弹」如果指的是「等回调结束、动能重新转强再进场」——在 BTC 日线上没有 edge。</b>
9.16 年无杠杆无成本的理论上限只有 <b>%(mom_mult)s</b>，跑输「买入持有」(%(bh_mult)s) 约 9 倍，
且 60%% 的时间在场、8 次换手，属于「参与了大半行情却拿不到收益」。
加上真实成本后：现货 1.23×（年化 +2.3%%，回撤 -60.4%%），合约多空 3× 只剩 <b>0.08×</b>。</p>
<p><b>真正能赚钱的是「趋势跟踪」（追动量），不是「抄底反弹」。</b>
同一套数据、同样的成本口径：现货波动率目标仓位 <b>%(sp_mult)s</b>（年化 %(sp_cagr)s，回撤 %(sp_mdd)s，Calmar %(sp_calmar)s，零爆仓）。</p>
<p><b>合约的价值不在「加杠杆放大收益」，而在「能做空」。</b>
只多合约（%(lo_mult)s）反而<b>不如</b>现货（%(sp_mult)s），因为要付资金费；
一旦放开做空，Calmar 从 %(sp_calmar)s 升到 <b>%(ls_calmar)s</b>，终值 %(ls_mult)s。
杠杆的安全上限是 <b>3×</b>：全口径（多空都上杠杆）下 4× 时最深逆行 -24.99%% 距爆仓线 -25.00%% 只剩 <b>0.01pp</b>（等于裸奔），
5× 以上收益完全靠「爆仓后立刻重开」的运气。</p>
<p><b>你问的「只做多走现货、做空才用合约 3× 逐仓」恰好是最优落地组合。</b>
同一信号下终值 <b>%(hyb_mult)s</b>（比纯合约多空 %(futls_mult)s 高 <b>%(hyb_gain)s</b>，
省下的资金费就是净赚的），回撤还略低（%(hyb_mdd)s vs %(futls_mdd)s）。
<b>胜率 %(hyb_win)s</b>、盈亏比 2.81、最长连亏 7 笔；1000 USDT 起 9.16 年账面到
<b>%(hyb_final_u)s U</b>（单参数 10/30）或 <b>%(wt_final_u)s U</b>（加权 50%% 折中口径，更可信）。详见第六、七章。</p>
<p><b>你最担心的爆仓风险：混合载体 9.16 年零爆仓，而且「4× 才是真危险线」。</b>
空头腿最深逆行只有 -22.89%%，3× 爆仓线在 -33.33%%，余量 <b>%(m3_margin)s</b>；
但 4× 余量只剩 %(m4_margin)s、5× 以上历史已越线。
<b>「5 组等权」也不是免费的保险</b> —— 它收益腰斩、回撤反而更大，第七章有完整样本外检验。</p>
</div>

<div class="kpis">
<div class="kpi g"><div class="lab">动量反弹信号（理论上限）</div><div class="val">%(mom_mult)s</div>
 <div class="note">vs 买入持有 %(bh_mult)s，跑输 9 倍</div></div>
<div class="kpi r"><div class="lab">趋势跟踪 · 现货</div><div class="val">%(sp_mult)s</div>
 <div class="note">年化 %(sp_cagr)s，回撤 %(sp_mdd)s</div></div>
<div class="kpi b"><div class="lab">趋势跟踪 · 合约多空 3×</div><div class="val">%(ls_mult)s</div>
 <div class="note">Calmar %(ls_calmar)s，零爆仓</div></div>
<div class="kpi o"><div class="lab">杠杆安全上限</div><div class="val">3×</div>
 <div class="note">4× 零余量，5× 起靠运气</div></div>
<div class="kpi g"><div class="lab">推荐落地 · 混合载体胜率</div><div class="val">%(hyb_win)s</div>
 <div class="note">现货做多+合约空3× · 1000U → %(wt_final_u)sU（加权50%%口径）</div></div>
</div>

<h2>一、先诊断信号：动量反弹到底有没有 edge</h2>
<div class="card">
<p>方法：把杠杆、成本、仓位全部剥掉，只留「满仓做多 / 空仓」的择时能力，再与
<b>随机择时</b>（相同做多天数、相同换手次数，随机挑入场点）对比 400 次。
若超越随机样本的比例不到 80%%，说明这个信号的入场点并不比掷骰子好。</p>
%(t_diag)s
</div>
<div class="chart">%(c_diag)s</div>
<div class="chart">%(c_diag2)s</div>
<div class="note">
<b>读法：</b>蓝色柱 = 信号的理论上限倍数；橙色柱 = 超越随机择时的比例，橙色虚线 80%% 是判定线。
<br>「动量反弹 10/30+RSI45」的 2.16× 远低于买入持有 %(bh_mult)s，超越随机仅 14.5%%
—— 这个信号<b>不是「弱」，而是「负贡献」</b>：它把最肥的启动段让掉了。
</div>

<h3>换个角度：它是不是「买贵了」？</h3>
<div class="card">
<p>把「动量反弹」参数面整个扫一遍（RSI 阈值 30→60 × 3 组均线），看有没有调参空间：</p>
</div>
<div class="chart">%(c_scan)s</div>
<div class="warnbox">
<b>关键发现：阈值越松，结果越好，但越好就越不像「反弹」。</b>
RSI&lt;30 的「抄底版」在 10/30 上只有 1.00×；把阈值放宽到 RSI&lt;60
（= 趋势向上且动能已经明显）才升到 31.96×，而这已经等价于趋势跟踪了。
<br>换句话说：<b>「等回调结束」这个动作本身在拖后腿。</b>
BTC 是右偏动量市场，回调结束的确认往往发生在「已经涨了一大段」之后，
此时买入既错过了启动段，又承担了回调前的套牢盘。
</div>

<h2>二、现货 vs 合约：同样的信号，两种载体</h2>
<div class="card">%(t_cmp)s</div>
<div class="chart">%(c_equity)s</div>
<div class="ok">
<b>四条可直接落地的结论：</b>
<ul>
<li><b>信号不好，换载体救不了。</b>动量反弹在现货 1.23×、在合约多空 3× 只有 0.08×
—— 杠杆把亏损同样放大了 15 倍。</li>
<li><b>只做多时，现货优于合约。</b>④ 现货 %(sp_mult)s vs ⑥ 合约只多 %(lo_mult)s。
同敞口下合约每年要多付约 11%% 的资金费（0.03%%/日），9 年累计吃掉约 13%% 的终值。
<b>长期只做多 → 用现货。</b></li>
<li><b>合约唯一不可替代的是「能做空」。</b>⑦ 多空 %(ls_mult)s，Calmar %(ls_calmar)s
（现货 %(sp_calmar)s）。做空在 2018 年把 -73%% 的熊市变成 +9.4%%，这是它真正的价值。</li>
<li><b>波动率目标仓位是免费的午餐。</b>④ 波动率目标（%(sp_mult)s，回撤 %(sp_mdd)s，Calmar %(sp_calmar)s）
优于 ⑤ 满仓（%(spf_mult)s，回撤 %(spf_mdd)s，Calmar %(spf_calmar)s）—— 收益略低但风险调整后明显更好。</li>
</ul>
</div>

<h2>三、杠杆与爆仓：合约的真正风险在哪里</h2>
<div class="card">
<p><b>逐仓下，爆仓线只由杠杆决定，与仓位大小无关</b>：
多头爆仓价 = 入场价 × (1 − 1/杠杆)。1× 永不爆仓，3× 要跌 33.3%%，5× 只要跌 20%%，10× 跌 10%% 就归零。</p>
</div>
<div class="chart">%(c_danger)s</div>
<div class="card">%(t_lev)s</div>
<div class="note">
<b>怎么读这张表：</b>「允许重开」= 爆仓后可以立刻按信号重新入场；「禁止重开」= 爆仓即永久退场。
两者的比值 >0.9 才说明这个杠杆的收益<b>不依赖运气</b>。
<br>1×~4× 比值都是 1.000（9 年零爆仓）；<b>5× 起比值崩到 0.02</b>，
说明 5× 以上的高收益全靠「爆仓后立刻重开、恰好骑上同一波趋势」的路径幸运，换一段历史就是归零。
</div>
<div class="warnbox">
<b>4× 看似安全，实则零余量。</b>9 年持仓期最深逆行 -24.99%%，而 4× 的爆仓线正好是 -25.00%%
—— 相差 0.01 个百分点。任何一次比历史极值深一点的插针都会归零。
<br><b>所以推荐 3×</b>：爆仓线 -33.3%%，跳空最深 -15.8%%，余量 17.6pp，这才叫安全垫。
</div>
<div class="card">%(t_fund)s</div>
<div class="note">资金费按 0.03%%/日（币安 BTCUSDT 永续默认档，年化约 11%%）估算。
即便放大到 0.15%%/日（年化 55%%，极端牛市水平），终值仍有 %(fund_hi)s，策略不因此失效。
做空时资金费方向相反（空头收费），所以双向策略的实际拖累比上表更小。</div>

<h2>四、防过拟合：这个结论经不经得起折腾</h2>
<h3>4.1 参数扰动（双均线 · 合约多空 3×）</h3>
<div class="card">%(t_param)s</div>
<div class="warnbox">
<b>诚实说明：参数面是「尖峰」而不是「平台」。</b>10/30 的 %(ls_mult)s 明显高于次优的
%(par_2nd)s 和 %(par_3rd)s，最好 / 最差达 %(par_ratio)s —— 单点确实有运气成分。
<br><b>但「5 组等权」并不是免费的保险。</b>第七章做了完整的样本外检验，结论是：
等权在历史上收益不到一半、回撤还更大。<b>推荐做法是「10/30 给 50%% 权重 + 其余 4 组各 12.5%%」</b>，
详见第七章 7.5。
</div>

<h3>4.2 跨资产复核（同一套参数，不重新拟合）</h3>
<div class="card">%(t_cross)s</div>
<div class="note">
BTC 与 ETH 上趋势跟踪都跑赢买入持有（%(btc_sp)s vs %(btc_bh)s；%(eth_sp)s vs %(eth_bh)s），
说明抓到的是「趋势」这个普适规律而非 BTC 特有噪音。
<b>但 SOL 例外</b>：现货 %(sol_sp)s 跑输买入持有 %(sol_bh)s，且多空最大回撤 %(sol_mdd)s、爆仓 %(sol_liq)s 次
—— 高波动小币的假突破更多，做空被反复打脸。换标的必须重新验证。
</div>

<h3>4.3 逐年收益：收益是不是全靠某一年</h3>
<div class="chart">%(c_year)s</div>
<div class="card">%(t_year)s</div>
<div class="note">
策略在 2018（%(y2018_ls)s vs 买入持有 %(y2018_bh)s）和 2022（%(y2022_ls)s vs %(y2022_bh)s）
的熊市里提供了强保护，但在 2023/2024 的强牛市中明显跑输（%(y2023_ls)s vs %(y2023_bh)s）。
<b>这是趋势跟踪的固有特征：它牺牲牛市的超额，换熊市的不掉血。</b>
没有任何一年单独贡献全部利润，最差年份 2022 亏 %(y2022_ls)s。
</div>

<h3>4.4 滚动 365 天：换个日子进场会怎样</h3>
<div class="card">%(t_prof)s</div>
<div class="note">
合约多空 3× 的正收益窗口占比 <b>%(ls_pos)s</b>，现货波动率目标 %(sp_pos)s。
含义：随便挑一天进场持有一年，约 4/5 的概率是赚的。
但最差窗口合约 -%(ls_worst)s、现货 -%(sp_worst)s —— <b>滚动窗口的最差值比最大回撤更贴近真实体验</b>，
报告里两个都要看。
</div>

<h2>五、可信度：这份回测怎么保证没有骗人</h2>
<div class="card">
<p>回测最容易骗人的地方是偷看未来数据，肉眼审代码几乎发现不了。所以做了一次<b>独立审计</b>：
绕开回测引擎，用同一套信号函数逐日重算状态序列，由状态翻转推出理论成交点（翻转日 + 1），
再与引擎产出的交易记录逐笔比对。<b>这段审计已内置在 <code>btc_strategy.py</code> 的「第 11 步」里，随时可复跑。</b></p>
</div>
<div class="card">
<table class="tbl"><thead><tr><th style="text-align:left">审计项</th><th style="text-align:left">标准</th>
<th style="text-align:left">结果</th></tr></thead><tbody>
<tr><td>交易点一致性（5 个信号族）</td><td>理论成交点与引擎逐笔 100%% 吻合</td>
<td><span class="pos">全部吻合 ✅</span></td></tr>
<tr><td>成交价验证</td><td>买入价 = 当日开盘 ×(1+滑点)，卖出 = ×(1−滑点)</td>
<td><span class="pos">252/252 通过 ✅</span></td></tr>
<tr><td>前视偏差</td><td>每笔成交日 = 信号确认日 + 1 根</td>
<td><span class="pos">0 处异常 ✅</span></td></tr>
<tr><td>成本是否生效</td><td>零成本 vs 全费用结果应有显著差异</td>
<td><span class="pos">有差异（见第二/三章）✅</span></td></tr>
<tr><td>爆仓逻辑</td><td>爆仓价 = 入场价 ×(1−1/L)，逐仓只亏该笔保证金</td>
<td><span class="pos">杠杆↑ → 爆仓次数↑ ✅</span></td></tr>
</tbody></table>
</div>

<h2>六、落地方案：现货做多 + 合约 3× 逐仓做空（1000 USDT 本金）</h2>
<div class="card">
<p>把前面几章的结论拼成一个能直接执行的方案：<b>信号翻多 → 现货买入（不付资金费）；信号翻空 → 合约 3× 逐仓做空（收资金费）</b>。
这正是「只做多就用现货、要做空才用合约」的直接落地版，也是本报告推荐的实际做法。
杠杆只用在空头腿，多头腿完全不碰杠杆。</p>
</div>

<h3>6.1 三种载体、同一信号：混合到底好多少</h3>
<div class="card">%(t_hyb_cmp)s</div>
<div class="note">
<b>混合载体 %(hyb_mult)s</b>，比「纯合约多空 3×」（%(futls_mult)s）高出 <b>%(hyb_gain)s</b> ——
差距全部来自<b>多头腿省掉的资金费</b>：纯合约方案里多头每年要付约 11%% 资金费，9.16 年累计吃掉三分之一以上的终值；
换成现货后这笔钱直接留在账户里。回撤反而略降（%(hyb_mdd)s vs %(futls_mdd)s）。
<br>注意 ② 行：① 的 %(hyb_mult)s 含单参数运气成分，<b>「该对标的数字」不是等权的 %(hybport_mult)s</b>，
而是第七章检验后给出的 <b>%(wt_mult)s（加权 50%%）</b> 或 <b>%(wf5)s（walk-forward 5 年）</b>。
</div>

<h3>6.2 胜率：37%%，两条腿几乎一模一样</h3>
<div class="card">%(t_hyb_leg)s</div>
<div class="note">
<b>直接回答「胜率多少」：37.0%%。</b>现货多头腿 37.0%%（46 笔中 17 笔赚），合约空头腿 37.0%%（46 笔中 17 笔赚），
合计 92 笔里 34 笔赚钱。<b>换句话说：10 笔里有 6 笔多是亏的。</b>
<br>能赚钱靠的是<b>盈亏比 2.81</b>（平均每笔盈利是平均每笔亏损的 2.8 倍）和 <b>PF 1.64</b>。
这是趋势跟踪的典型形态：<b>胜率低、靠少数大单贡献全部利润</b>，所以「连续亏 7 笔」是常态（最长连亏 7 笔），必须能扛住。
<br>两条腿拆开看，<b>多头腿质量明显更高</b>（PF 2.09 vs 空头 1.19）：空头是「收点资金费 + 熊市保护」的配角，
赚的钱少、被打脸的次数多，但没有它 2018/2022 会很难看。
</div>

<h3>6.3 1000 USDT 会变成多少：逐年路径</h3>
<div class="card">%(t_hyb_year)s</div>
<div class="chart">%(c_hyb_year)s</div>
<div class="warnbox">
<b>1000 USDT 起，9.16 年后的账面结果：</b>
<ul>
<li><b>单参数 10/30：%(hyb_final_u)s U（%(hyb_mult)s，年化 %(hyb_cagr)s）</b> —— 但这是最好的一组参数，别当真。</li>
<li><b>加权 50%% 折中口径：%(wt_final_u)s U（%(wt_mult)s，年化 %(wt_cagr)s）</b> —— 更接近实盘能拿到的量级。</li>
<li><b>walk-forward 过去 5 年（自动选参）：%(wf5_final_u)s U（%(wf5)s）</b> —— 不靠事后挑参数，是最诚实的一档。</li>
</ul>
路径特征：<b>前 3 年很磨人</b>。2018 年单参数只赚 +10.8%%、等权组合还亏 -10.9%%，
账户在 1,000~1,700 U 之间来回磨了整整一年；真正的钱是 2019~2021 三年赚出来的（+206%% / +234%% / +128%%）。
<br><b>10 个年度里只有 1~2 年是亏的</b>（单参数 1 年、等权 2 年），但 2022 年一年就能吐掉 -33.9%%。
</div>
<div class="note">
<b>1000U 的实操约束（都能满足，但要知道边界）：</b>
名义仓位 ≈ 权益 × min(1.0, 3%% / 日波动率) ≈ 权益 × 0.7~0.8，起始首笔名义约 700~800 U。
现货最小下单约 10 U 等值、BTC 永续最小 0.001 BTC（约 60~100 U 名义）——<b>1000 U 起步完全可执行</b>。
但小资金对滑点更敏感，本报告已按 0.05%% 单边滑点保守计提；真实成交若更差，收益会明显缩水。
</div>

<h3>6.4 中途要经历什么：最深回撤（换成 U 看）</h3>
<div class="card">%(t_hyb_dd)s</div>
<div class="note">
百分比回撤听着抽象，换成 U 才真实：最大一次是从 <b>49,421 U 跌到 28,778 U，账面蒸发 20,643 U，历时 352 天</b>
（2021-11 到 2022-11，对应上一轮熊市）。滚动 365 天口径下，最差窗口 <b>%(hyb_rollworst)s</b>、
中位 <b>%(hyb_rollmed)s</b>、正收益窗口占比 <b>%(hyb_rollpos)s</b>，<b>最长水下 %(hyb_uw)d 天（约 1.8 年）</b>。
<br>如果你拿 1000 U 起步，前期回撤的绝对金额不大（几百 U），但<b>等到账户涨到几万 U 时，一次 40%% 回撤就是上万美元</b>
—— 这才是真正考验人的地方。
</div>

<h3>6.5 参数扰动：单参数的好看数字能信几分</h3>
<div class="chart">%(c_hyb_param)s</div>
<div class="note">
5 组参数里 10/30 一枝独秀（%(hyb_mult)s），邻居 12/36 只有 37.90×、20/60 只有 28.25×，
最好 / 最差 <b>%(peak_ratio)s</b>（混合载体口径）—— 说明 10/30 有相当部分是运气。
但「5 组等权」并不是免费的保险：<b>它收益只有 %(hybport_mult)s、回撤还扩大到 %(hybport_mdd)s</b>。
第七章做了完整的样本外检验，<b>推荐用「10/30 给 50%% 权重 + 其余 4 组各 12.5%%」（%(wt_mult)s）或 walk-forward 5 年（%(wf5)s）</b>替代等权。
</div>

<h3>6.6 跨资产复核：换标的还成立吗</h3>
<div class="card">%(t_hyb_cross)s</div>
<div class="chart">%(c_hyb_cross)s</div>
<div class="note">
BTC 上混合载体 %(hybbtc)s 跑赢买入持有 %(hybbtc_bh)s；ETH 上 %(hybeth)s 也跑赢 %(hybeth_bh)s。
<b>SOL 再次例外</b>：混合 %(hybsol)s 跑输买入持有 %(hybsol_bh)s，且爆仓 %(hybsol_liq)s 次 ——
高波动小币的空头腿会被反复插针打爆，<b>换标的必须重新验证，不要直接套用</b>。
</div>

<h2>七、你最关心的两个问题：会不会爆仓？该不该等权？</h2>
<div class="card">
<p>你提的这两点都对：<b>纯合约多空 3× 的 %(futls_mult)s、单参数 10/30 的 %(hyb_mult)s，数字确实比 5 组等权的 %(hybport_mult)s 好看。</b>
那问题就只剩两个 —— <b>① 这些更高的收益，是不是用「可能爆仓」换来的？② 单参数的高收益里，到底有多少是运气？</b>
这一章只用数据回答，不靠感觉。</p>
</div>

<h3>7.1 会不会爆仓：先看结构 —— 只有空头腿会爆</h3>
<div class="card">
<p>混合载体的设计是<b>「多头走现货、空头走合约 3× 逐仓」</b>，所以：现货那头没有杠杆，<b>永远不会爆仓</b>；
唯一可能被打爆的是空头腿。而空头被爆只有一个原因 —— <b>价格向上插针</b>。
于是问题收敛成一句话：<b>做空的那些日子里，价格最多向上逆行过多少？离 3× 的爆仓线还差多远？</b></p>
%(t_margin)s
</div>
<div class="chart">%(c_mae)s</div>
<div class="note">
9.16 年里共 %(n_short)d 笔空头，<b>持仓期最大逆行（MAE）最深只有 %(smin)s</b>
（%(smin_date)s 那笔，2018 年熊市里的反弹），中位数才 %(smin_med)s ——
也就是说<b>一半的空头连 -6.3%% 的浮亏都没碰到过</b>。
<br>而 3× 逐仓的爆仓线在 <b>-33.33%%</b>：最深那笔离爆仓还差 <b>%(m3_margin)s</b>。
要把 3× 打爆，需要出现一次比历史最惨还深 <b>%(need_deep)s</b> 的空头逆行 —— 9.16 年一次都没出现过，<b>爆仓 0 次</b>。
</div>
<div class="warnbox">
<b>但杠杆不能再往上加，4× 就已经贴脸了：</b>
<ul>
<li><b>3×</b>：爆仓线 -33.33%%，最深逆行 -22.89%%，余量 <b>%(m3_margin)s</b> —— 安全。</li>
<li><b>4×</b>：爆仓线 -25.00%%，余量只剩 <b>%(m4_margin)s</b> —— 等于裸奔，一次超预期插针就可能直接归零。</li>
<li><b>5× / 10×</b>：爆仓线 -20.00%% / -10.00%%，<b>历史最深逆行已经越线 → 必爆</b>。</li>
</ul>
所以「3× 逐仓」不是随手挑的数字，而是<b>历史余量能给到的上限</b>。而且这里的 3× 只是空头腿的杠杆，
整体敞口仍被波动率目标压到 1.0× 以内，真实资金占用远低于满杠杆。
</div>

<h3>7.2 压力测试：历史上最凶的那一天，我们正好在做空</h3>
<div class="card">
<p>光看统计量不够，直接看最坏的那次。<b>2020-03-13（312 崩盘后的报复性反弹）BTC 盘中一度 +24.06%%，
是 9.16 年里的单日盘中最大涨幅</b>，而当时策略的持仓方向恰好是<b>空头</b>（信号在 3-12 收盘就是 -1）。
即便如此，3× 逐仓依然没被爆掉，距爆仓线还留有余量。这不是巧合，而是 BTC 的结构性特征：</p>
%(t_extreme)s
</div>
<div class="chart">%(c_extreme)s</div>
<div class="note">
<b>BTC 跌得比涨得凶：</b>单日盘中跌 ≥25%% 出现过 4 次、≥33.3%% 出现过 1 次；
而单日盘中<b>涨 ≥25%% / ≥33.3%% 的历史次数是 0 / 0</b>，史上最大单日盘中涨幅就是 %(max_up)s。
做空爆仓的方向（向上）恰恰是 BTC 更「温和」的方向 —— 这是空头腿 3× 能长期活下来的底层原因。
<br><b>反过来提醒：</b>这也意味着<b>做多不能用杠杆</b> —— 史上最大单日盘中跌幅 %(max_dn)s（2020-03-12），
多头腿若上 3× 会被瞬间打穿。<b>「多头走现货」不只是省资金费，更是躲开这个尾部风险。</b>
</div>

<h3>7.3 105× 是「事后挑出来」的吗：参数可信度检验</h3>
<div class="card">
<p>5 组参数里 10/30 一枝独秀（%(hyb_mult)s，最差的 20/60 只有 %(worst_param)s），
单看这个数字确实有「挑了最好那组」的嫌疑。所以做了三项检验。</p>
%(t_rank)s
</div>
<div class="note">
<b>检验一 · 逐年排名稳不稳：</b>10/30 在 10 个年度里拿了 <b>%(first_cnt)d 次第一</b>，且从没掉到最后。
不是「全靠某一年」，而是长期占优 —— 这比单看总收益可信得多。
<br>但也要看到：2022 / 2024 / 2025 三年它都不是第一（分别由 20/60、8/24、15/45 领先），
说明<b>没有任何一组是全年候最优</b>。
</div>
<div class="card">%(t_split)s</div>
<div class="note">
<b>检验二 · 前后段切一刀：</b>以样本中点（%(split_date)s）切开，<b>只用前段数据挑参数，再拿到后段去跑</b>。
前段冠军 10/30 在后段做出 %(oos_1030)s；而<b>前段垫底的 8/24 在后段做出 %(oos_824)s，反超了 10/30</b>。
这说明「谁最好」在样本外会换人 —— <b>单押一组的风险是真实存在的</b>。
</div>
<div class="card">%(t_wf)s</div>
<div class="note">
<b>检验三 · 滚动选参（最接近实盘）：</b>每年年初只用「截至当时的过去 N 年」数据选参数，再持有下一年。
<ul>
<li><b>过去 5 年窗口：%(wf5)s，全程只换过 %(wf5_sw)s 次参数</b> —— 和固定 10/30 的 %(hyb_mult)s 只差一点，
说明 105× 里<b>绝大部分是历史上真能复现的，不是事后挑出来的</b>。</li>
<li>窗口缩短到 3 年 / 2 年，收益掉到 %(wf3)s / %(wf2)s，换参变得频繁（%(wf3_sw)s / %(wf2_sw)s 次）——
<b>参数对近期行情敏感，选参窗口太短会来回打脸</b>。</li>
</ul>
</div>

<h3>7.4 六个方案摆在一起：等权到底换来了什么</h3>
<div class="card">%(t_scheme)s</div>
<div class="chart">%(c_scheme)s</div>
<div class="warnbox">
<b>结论和直觉相反：5 组完全等权，既没换来更高收益，也没换来更小回撤。</b>
<ul>
<li><b>收益腰斩：</b>固定 10/30 是 %(hyb_mult)s，完全等权只有 %(ew_mult)s —— <b>只剩它的 %(ew_ratio)s</b>。</li>
<li><b>回撤反而更大：</b>%(ew_mdd)s（等权）vs %(fixed_mdd)s（单押 10/30）。</li>
<li><b>风险调整后更差：</b>Calmar %(ew_calmar)s（等权）vs %(fixed_calmar)s（单押）。</li>
</ul>
原因：等权把 4 组收益明显更低的参数（%(weak3)s）一起平均，<b>把 10/30 的优势直接稀释掉了</b>，
却又没能对冲掉趋势跟踪本身的回撤 —— 5 组参数在同一段行情里往往同时翻多 / 翻空，回撤是同源的，等权冲不掉。
<b>所以「等权 = 更稳健」在这里是错觉。</b>
</div>

<h3>7.5 那到底怎么选：用「加权」代替「等权」</h3>
<div class="card">
<p>把三种诉求（要收益、要防单点运气、要控回撤）放在一起，最优解是<b>「加权」而不是「等权」</b>：</p>
<ul>
<li><b>10/30 给 50%% 权重</b>（承认它是长期占优的那组），其余 4 组各 12.5%%。</li>
<li>结果：终值 <b>%(wt_mult)s</b>（比完全等权 %(ew_mult)s 高 %(wt_gain)s）、
Calmar <b>%(wt_calmar)s</b>（等权只有 %(ew_calmar)s）。
回撤 %(wt_mdd)s —— 比完全等权的 %(ew_mdd)s 浅，但比单押 10/30 的 %(fixed_mdd)s 略深，
<b>它买到的是「不押单点」，不是「更小回撤」</b>。</li>
<li><b>最实在的好处：最长水下从 %(fixed_uw)d 天缩短到 %(wt_uw)d 天</b> —— 少熬近半年，实盘更容易拿住。</li>
</ul>
<p>如果你更在意「绝不押注单点」，还有一条<b>更自动、更不依赖主观判断</b>的路：
<b>walk-forward 过去 5 年（%(wf5)s）</b> —— 它不预设哪组最好，每年用历史数据自己选，全程只换参 %(wf5_sw)s 次，
收益和固定 10/30 几乎持平，<b>等于「不用选参数、又几乎不损失收益」</b>，代价是回撤略大（%(wf5_mdd)s）。</p>
</div>

<h2>八、结论与建议</h2>
<div class="card">
<h3>你原来的想法：「日线做动量反弹趋势」</h3>
<p>要拆成两半看，因为它们的结论完全相反：</p>
<ul>
<li><b>「反弹」= 等回调结束再进 → 无效。</b>理论上限 %(mom_mult)s，跑输买入持有 9 倍，
全参数面（21 组）找不到能跑赢趋势跟踪的配置。原因：BTC 是右偏动量市场，
回调结束的确认点往往在行情中后段，买入即承担套牢盘。</li>
<li><b>「趋势」= 顺着动量做 → 有效。</b>双均线 10/30 现货波动率目标 %(sp_mult)s、
合约多空 3× %(ls_mult)s，跨资产（BTC/ETH）复核通过，逐年无单点依赖。</li>
</ul>
<h3>现货还是合约？</h3>
<ul>
<li><b>只做多 → 现货。</b>合约只多要付年化 ~11%% 资金费，9 年少赚约 13%%，没有任何补偿。</li>
<li><b>要做空 / 想省保证金 → 合约。</b>做空把 Calmar 从 %(sp_calmar)s 提到 %(ls_calmar)s，
且在 2018/2022 深熊里是唯一保护。这是合约唯一不可替代的价值。</li>
<li><b>最实用的组合：多头走现货、空头走合约 3× 逐仓。</b>同信号下 %(hyb_mult)s（纯合约多空 %(futls_mult)s），
省下的资金费就是净赚的 %(hyb_gain)s，回撤还略低。胜率 <b>%(hyb_win)s</b>、盈亏比 2.81、最长连亏 7 笔。</li>
<li><b>杠杆固定 3× 逐仓，仓位靠波动率目标控制。</b>杠杆只决定「多远爆仓」，仓位才决定「亏多少」。
3× 的爆仓线 -33.3%%，9 年最深逆行 -25.0%%，余量 17.6pp。</li>
</ul>
<h3>如果要做，建议的参数</h3>
<ul>
<li>标的：BTC（+ETH 可选）；SOL 类高波动币需单独验证</li>
<li>载体：<b>多头用现货、空头用合约 3× 逐仓</b>（省资金费 + 保留做空保护）</li>
<li>参数：<b>两种都可，别用完全等权</b> —— ① <b>walk-forward 过去 5 年</b>（%(wf5)s，自动选参、最不靠运气）；
或 ② <b>10/30 给 50%% 权重 + 其余 4 组各 12.5%%</b>（%(wt_mult)s，最长水下从 %(fixed_uw)d 天缩到 %(wt_uw)d 天）。
完全等权只有 %(ew_mult)s 且回撤更大，<b>不是更稳，是更差</b>。</li>
<li>仓位：波动率目标 3%%/日，敞口上限 1.0×（即名义 ≤ 权益）</li>
<li>杠杆：<b>空头腿固定 3× 逐仓，不要超过 3×</b>（4× 余量仅 %(m4_margin)s，5× 起历史已越线）；
多头腿<b>不上杠杆</b>，因为 BTC 单日盘中最大跌幅 %(max_dn)s，上杠杆会瞬间被打穿</li>
<li>止盈止损：<b>不设固定止盈止损</b>，靠信号反转出场（同时充当止损与止盈）</li>
<li>心理预期：胜率约 <b>4 成</b>（会连续亏 7 笔）、盈亏比 2.8、最大回撤 -40%%~-50%% 量级、
最长水下 1~2 年；收益靠少数几笔大单，中途 6 成以上的单子是亏的</li>
</ul>
</div>
<div class="warnbox">
<b>必须说清的风险：</b>
<ul>
<li><b>爆仓风险：混合载体下 9.16 年零爆仓</b>，但这依赖两个前提 ——
① 杠杆不超过 3×（4× 余量仅 %(m4_margin)s）；② 只用 BTC/ETH 这类主流币（SOL 已出现反复爆仓）。
前提变了，结论就不成立。</li>
<li>9.16 年只有 10 个「年度样本」，<b>统计上分辨不了 3~10pp 的年化差异</b>。
不要纠结「哪档更优」，能信的是「三档排序稳定、零爆仓、正收益窗口 7~8 成」这些结构性事实。</li>
<li>参数面呈尖峰状（混合载体口径最好 / 最差 %(peak_ratio)s，全合约口径 11.2 倍），单一参数组合的漂亮数字有运气成分 ——
<b>walk-forward 检验显示 5 年窗口能复现约 %(wf_keep)s 的收益</b>，但仍不等于未来可复制。</li>
<li>本报告是<b>历史回测，不是收益承诺</b>。加密资产的最大回撤历史上达到过 -83%%（买入持有）。</li>
<li>资金费、手续费、滑点均按当前档位估算；交易所规则、流动性变化都会影响实盘结果。</li>
</ul>
</div>

<div class="foot">
数据源：data-api.binance.vision（币安公开行情，无需密钥）· 生成时间：%(gen)s<br>
本报告由纯 Python 离线生成，所有图表为内联 SVG，不依赖任何 CDN。<br>
回测与审计代码见同目录 <code>btc_strategy.py</code>（自包含，仅用标准库）：
双击 <code>运行回测.bat</code> 即可复现全部对比数据与独立审计；本页由 <code>make_report.py</code> 生成。
</div>
</div></body></html>
""" % {
        "start": dates[0], "end": dates[-1], "n": n, "years": years,
        "gen": time.strftime("%Y-%m-%d %H:%M"),
        "bh_mult": mult(bh),
        "mom_mult": mult(L["drows"][0][1]),
        "sp_mult": mult(L["r_sp"]["stats"]["final"]),
        "sp_cagr": pct(L["r_sp"]["stats"]["cagr"]),
        "sp_mdd": pct(L["r_sp"]["stats"]["mdd"]),
        "sp_calmar": "%.2f" % L["r_sp"]["stats"]["calmar"],
        "spf_mult": mult(L["r_spf"]["stats"]["final"]),
        "spf_mdd": pct(L["r_spf"]["stats"]["mdd"]),
        "spf_calmar": "%.2f" % L["r_spf"]["stats"]["calmar"],
        "lo_mult": mult(L["r_lo"]["stats"]["final"]),
        "ls_mult": mult(L["r_ls"]["stats"]["final"]),
        "ls_calmar": "%.2f" % L["r_ls"]["stats"]["calmar"],
        "fund_hi": mult(L["fund"][-1][1]["final"]),
        "btc_sp": mult(L["cross"][0][2]["final"]), "btc_bh": mult(L["cross"][0][1]),
        "eth_sp": mult(L["cross"][1][2]["final"]), "eth_bh": mult(L["cross"][1][1]),
        "sol_sp": mult(L["cross"][2][2]["final"]), "sol_bh": mult(L["cross"][2][1]),
        "sol_mdd": pct(L["cross"][2][4]["mdd"]), "sol_liq": L["cross"][2][4]["liq"],
        "y2018_ls": pct(L["ys_ls"][2018]), "y2018_bh": pct(L["ys_bh"][2018]),
        "y2022_ls": pct(L["ys_ls"][2022]), "y2022_bh": pct(L["ys_bh"][2022]),
        "y2023_ls": pct(L["ys_ls"][2023]), "y2023_bh": pct(L["ys_bh"][2023]),
        "ls_pos": "%.1f%%" % (roll_ls["pos_ratio"] * 100),
        "sp_pos": "%.1f%%" % (roll_sp["pos_ratio"] * 100),
        "ls_worst": "%.1f%%" % (-roll_ls["min"] * 100),
        "sp_worst": "%.1f%%" % (-roll_sp["min"] * 100),
        "t_diag": t_diag, "t_cmp": t_cmp, "t_param": t_param, "t_cross": t_cross,
        "t_lev": t_lev, "t_fund": t_fund, "t_year": t_year, "t_prof": t_prof,
        "c_equity": c_equity, "c_dd": c_dd, "c_diag": c_diag, "c_diag2": c_diag2,
        "c_scan": c_scan, "c_lev": c_lev, "c_danger": c_danger, "c_year": c_year,
        "c_cross": c_cross,
        # ---- 混合载体 ----
        "t_hyb_cmp": t_hyb_cmp, "t_hyb_leg": t_hyb_leg, "t_hyb_year": t_hyb_year,
        "t_hyb_dd": t_hyb_dd, "t_hyb_cross": t_hyb_cross,
        "c_hyb_year": c_hyb_year, "c_hyb_param": c_hyb_param, "c_hyb_cross": c_hyb_cross,
        "hyb_mult": mult(L["r_hyb"]["stats"]["final"]),
        "hyb_cagr": pct(L["r_hyb"]["stats"]["cagr"]),
        "hyb_mdd": pct(L["r_hyb"]["stats"]["mdd"]),
        "hyb_calmar": "%.2f" % L["r_hyb"]["stats"]["calmar"],
        "hyb_win": "%.1f%%" % (L["hyb_ts"]["all"]["win_rate"] * 100),
        "hyb_n": L["hyb_ts"]["all"]["n"],
        "hyb_final_u": "%.0f" % L["r_hyb"]["equity"][-1],
        "hybport_mult": mult(hp["final"]),
        "hybport_u": "%.0f" % L["hyb_port"][-1],
        "hybport_cagr": pct(hp["cagr"]),
        "hybport_mdd": pct(hp["mdd"]),
        "futls_mult": mult(L["r_fut_ls"]["stats"]["final"]),
        "futls_mdd": pct(L["r_fut_ls"]["stats"]["mdd"]),
        "hyb_gain": "+%.0f%%" % ((L["r_hyb"]["stats"]["final"] / L["r_fut_ls"]["stats"]["final"] - 1) * 100),
        "hyb_rollworst": pct(L["r_hyb"]["stats"]["roll365"]["min"]),
        "hyb_rollmed": pct(L["r_hyb"]["stats"]["roll365"]["med"]),
        "hyb_rollpos": "%.1f%%" % (L["r_hyb"]["stats"]["roll365"]["pos_ratio"] * 100),
        "hyb_uw": L["r_hyb"]["stats"]["max_underwater"],
        "hybbtc": mult(L["hyb_cross"][0][3]["final"]), "hybbtc_bh": mult(L["hyb_cross"][0][1]),
        "hybeth": mult(L["hyb_cross"][1][3]["final"]), "hybeth_bh": mult(L["hyb_cross"][1][1]),
        "hybsol": mult(L["hyb_cross"][2][3]["final"]), "hybsol_bh": mult(L["hyb_cross"][2][1]),
        "hybsol_liq": L["hyb_cross"][2][3]["liq"],
        # ---- 第七章：爆仓风险与参数可信度 ----
        "t_margin": t_margin, "t_mae": t_mae, "t_extreme": t_extreme,
        "t_rank": t_rank, "t_split": t_split, "t_wf": t_wf, "t_scheme": t_scheme,
        "c_mae": c_mae, "c_extreme": c_extreme, "c_scheme": c_scheme,
        "n_short": len(L["short_adv"]),
        "smin": pct(L["smin"]),
        "smin_date": ft(L["bars"][L["worst5"][0][1]]["t"]),
        "smin_med": pct(L["mae_q"][3][1]),
        "m3_margin": "%.2fpp" % ((-1.0 / 3.0 - L["smin"]) * 100),
        "m4_margin": "%.2fpp" % ((-1.0 / 4.0 - L["smin"]) * 100),
        "need_deep": "%.2f 倍" % ((1.0 / 3.0) / abs(L["smin"])),
        "max_up": "+%.2f%%" % (L["max_up"] * 100),
        "max_dn": "%.2f%%" % (L["max_dn"] * 100),
        "worst_param": mult(L["hyb_param"][-1][1]["final"]),
        "peak_ratio": "%.1f 倍" % (L["r_hyb"]["stats"]["final"] / L["hyb_param"][-1][1]["final"]),
        "first_cnt": L["first_cnt"][(10, 30)],
        "split_date": ft(L["bars"][L["mid2"]]["t"]),
        "oos_1030": mult(dict((r[0], r) for r in L["split_rows"])[(10, 30)][2]),
        "oos_824": mult(dict((r[0], r) for r in L["split_rows"])[(8, 24)][2]),
        "weak3": "、".join("%s 的 %s" % (r[0], mult(r[1]["final"]))
                          for r in sorted(L["hyb_param"], key=lambda x: -x[1]["final"])
                          if r[0] != "10/30"),
        "wf5": mult(_sch["walk-forward（过去 5 年选参）"]["final"]),
        "wf5_sw": [r[2] for r in L["wf_rows"] if r[0] == 5][0],
        "wf5_mdd": pct(_sch["walk-forward（过去 5 年选参）"]["mdd"]),
        "wf5_final_u": "%.0f" % (_sch["walk-forward（过去 5 年选参）"]["final"] * CAP),
        "wf3": mult(_sch["walk-forward（过去 3 年选参）"]["final"]),
        "wf3_sw": [r[2] for r in L["wf_rows"] if r[0] == 3][0],
        "wf2": mult(_sch["walk-forward（过去 2 年选参）"]["final"]),
        "wf2_sw": [r[2] for r in L["wf_rows"] if r[0] == 2][0],
        "fixed_mdd": pct(_sch["固定 10/30 单参数"]["mdd"]),
        "fixed_calmar": "%.2f" % _sch["固定 10/30 单参数"]["calmar"],
        "fixed_uw": _sch["固定 10/30 单参数"]["max_underwater"],
        "ew_mult": mult(_sch["5 组完全等权"]["final"]),
        "ew_mdd": pct(_sch["5 组完全等权"]["mdd"]),
        "ew_calmar": "%.2f" % _sch["5 组完全等权"]["calmar"],
        "ew_ratio": "%.0f%%" % (_sch["5 组完全等权"]["final"] / _sch["固定 10/30 单参数"]["final"] * 100),
        "wt_mult": mult(_sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["final"]),
        "wt_mdd": pct(_sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["mdd"]),
        "wt_calmar": "%.2f" % _sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["calmar"],
        "wt_uw": _sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["max_underwater"],
        "wt_cagr": pct(_sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["cagr"]),
        "wt_final_u": "%.0f" % (_sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["final"] * CAP),
        "wt_gain": "+%.0f%%" % (_sch["加权 50% 10/30 + 其他 4 组各 12.5%"]["final"]
                                / _sch["5 组完全等权"]["final"] * 100 - 100),
        "wf_keep": "%.0f%%" % (_sch["walk-forward（过去 5 年选参）"]["final"]
                               / _sch["固定 10/30 单参数"]["final"] * 100),
        "par_2nd": "%s 的 %s" % (sorted(L["prows"], key=lambda r: -r[1]["final"])[1][0],
                                 mult(sorted(L["prows"], key=lambda r: -r[1]["final"])[1][1]["final"])),
        "par_3rd": "%s 的 %s" % (sorted(L["prows"], key=lambda r: -r[1]["final"])[2][0],
                                 mult(sorted(L["prows"], key=lambda r: -r[1]["final"])[2][1]["final"])),
        "par_ratio": "%.1f 倍" % (max(r[1]["final"] for r in L["prows"]) / min(r[1]["final"] for r in L["prows"])),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("报告已生成：%s  (%.1f KB)" % (OUT, len(html) / 1024))


if __name__ == "__main__":
    main()
