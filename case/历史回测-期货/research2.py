# -*- coding: utf-8 -*-
"""
第二轮探索：横截面因子 / 配对价差 / 波动率状态过滤

与 ft.py 的区别：
  ft.py    —— 单品种时序策略（每个品种独立判断方向，再等权组合）
  research2 —— 横截面策略（品种之间比强弱）与配对策略（两品种之间做价差）

为什么值得做：
  第一轮已证明：41 个品种等权跑任何时序策略，年化都只有 -10% ~ +3.7%。
  时序信号在商品上太弱。但商品 CTA 的真正 alpha 在【横截面】：
  同一时点总有品种在 backwardation、有品种在 contango，
  做多前者做空后者，赚的是相对强弱，与大盘涨跌无关（市场中性）。

用法：
    python research2.py xcarry     横截面展期因子
    python research2.py xmom       横截面动量因子
    python research2.py xcombo     carry x 动量 双因子
    python research2.py pairs      产业链配对价差
    python research2.py regime     波动率状态过滤
    python research2.py all        全部跑一遍并写出 research2.json
"""
import os, sys, json, math, statistics, random

import ft

HERE = ft.HERE
TRADING_DAYS = ft.TRADING_DAYS

# ── 交易成本（与 ft.py 一致）──────────────────────────────────────
FEE = 0.0002
SLIP = 0.0005
COST = FEE + SLIP          # 单边 7bp


# ================================================================ 数据层
def build_panel(syms):
    """构建对齐面板：日期轴 + 复权收盘 + 每日换月跳空 + 日波动率。

    返回 dict:
        dates  : [str]
        px     : {sym: [float]}   复权收盘（信号与收益都用它）
        gap    : {sym: [float]}   当日换月跳空（carry 因子的原料）
        dvol   : {sym: [float]}   ATR20/close 的滞后值（用于风险平价加权）
        realpx : {sym: float}     最新真实价（算保证金用）
    """
    raw = {}
    for s in syms:
        b = ft.load(s)
        if not b or len(b) < ft.WARMUP + 300:
            continue
        raw[s] = b
    syms = list(raw.keys())
    if not syms:
        return None

    all_dates = sorted(set().union(*[{x["date"] for x in b} for b in raw.values()]))
    idx = {d: i for i, d in enumerate(all_dates)}
    n = len(all_dates)

    px = {s: [None] * n for s in syms}
    gap = {s: [0.0] * n for s in syms}
    for s, b in raw.items():
        for x in b:
            j = idx[x["date"]]
            px[s][j] = x["close"]
            gap[s][j] = x.get("gap", 0.0)

    # 前值填充
    for s in syms:
        last = None
        for j in range(n):
            if px[s][j] is None:
                px[s][j] = last
            else:
                last = px[s][j]

    # 日波动率：ATR20 / close，再滞后一天（决策只能用昨天及以前）
    dvol = {}
    for s in syms:
        A = ft.atr(raw[s], 20)
        ser = [None] * n
        for x, a in zip(raw[s], A):
            j = idx[x["date"]]
            ser[j] = (a / x["close"]) if (x["close"] and a) else None
        last = None
        for j in range(n):
            if ser[j] is None:
                ser[j] = last
            else:
                last = ser[j]
        dvol[s] = ser

    realpx = {s: raw[s][-1]["close"] / raw[s][-1].get("scale", 1.0)
              if raw[s][-1].get("scale") else raw[s][-1]["close"] for s in syms}
    # ft.load 的复权不写 scale 字段时，退回原始 CSV 末行
    for s in syms:
        rr = ft._read_csv(s)
        if rr:
            realpx[s] = rr[-1]["close"]

    return {"dates": all_dates, "px": px, "gap": gap, "dvol": dvol,
            "realpx": realpx, "syms": syms, "idx": idx}


# ================================================================ 组合回测引擎
def run_weights(P, weight_fn, cost=COST, vol_target=None, expo_cap=2.0,
                rebal=1, verbose=False):
    """横截面组合回测。

    weight_fn(i) -> {sym: w}   在第 i 日收盘后给出目标权重（只能用 <= i 的数据）
    第 i+1 日按该权重持有。权重是「名义敞口 / 权益」。
    vol_target 非 None 时，对每个品种用 ATR 波动率缩放到目标日波动，
    再整体归一化到总敞口 = 1（多空各 0.5）。

    成本：换手 * cost，换手 = sum|w_t - w_{t-1}|。
    """
    dates, px, dvol = P["dates"], P["px"], P["dvol"]
    syms = P["syms"]
    n = len(dates)
    w_prev = {s: 0.0 for s in syms}
    rets, turns = [], []

    for i in range(1, n):
        # ① 每日持仓收益（不管今天调不调仓，仓位都在承担涨跌）
        r = 0.0
        for s in syms:
            w = w_prev.get(s, 0.0)
            p0, p1 = px[s][i - 1], px[s][i]
            if w != 0.0 and p0 and p1:
                r += w * (p1 / p0 - 1.0)

        # ② 调仓日才更新目标权重，并计换手成本
        turn = 0.0
        if (i % rebal) == 0:
            tgt = weight_fn(i - 1) or {}
            # 波动率缩放
            if vol_target is not None:
                scaled = {}
                for s, w in tgt.items():
                    dv = dvol[s][i - 1] if i - 1 < n else None
                    if not dv or dv <= 1e-6:
                        continue
                    e = min(expo_cap, vol_target / dv)
                    scaled[s] = w * e
                tot = sum(abs(v) for v in scaled.values())
                if tot > 1e-9:
                    scaled = {k: v / tot for k, v in scaled.items()}
                tgt = scaled
            for s in syms:
                dw = tgt.get(s, 0.0) - w_prev.get(s, 0.0)
                turn += abs(dw)
                r -= abs(dw) * cost
            w_prev = dict(tgt)

        rets.append(r)
        turns.append(turn)

    clean = [(dates[i], rets[i - 1], turns[i - 1]) for i in range(1, n)]
    return [x[0] for x in clean], [x[1] for x in clean], [x[2] for x in clean]


def perf(rets):
    """年化 / 回撤 / Calmar / Sharpe / 胜率。"""
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    n = len(rets)
    cagr = (eq ** (TRADING_DAYS / n) - 1) if n else 0.0
    mu = statistics.mean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd * math.sqrt(TRADING_DAYS)) if sd > 1e-12 else 0.0
    win = sum(1 for r in rets if r > 0) / n if n else 0.0
    # 胜率只统计「有持仓」的日子，否则空仓日（收益恰为 0）会把胜率稀释成误导值
    act = [r for r in rets if abs(r) > 1e-12]
    win_act = (sum(1 for r in act if r > 0) / len(act)) if act else 0.0
    return {"cagr": cagr, "mdd": mdd, "mult": eq,
            "calmar": (cagr / mdd) if mdd > 1e-9 else 9.99,
            "sharpe": sharpe, "win": win, "win_act": win_act,
            "active": len(act) / n if n else 0.0, "n": n}


def seg(dates, rets, lo, hi):
    sub = [r for d, r in zip(dates, rets) if lo <= d <= hi]
    return perf(sub) if sub else None


def boot(dates, rets, B=600, L=30, seed=11):
    """分块自助：年化的 90% 区间与「年化为负」的概率。"""
    rnd = random.Random(seed)
    blocks = [rets[i:i + L] for i in range(0, max(1, len(rets) - L), L)]
    if not blocks:
        return None
    cg = []
    for _ in range(B):
        sm = []
        while len(sm) < len(rets):
            sm.extend(rnd.choice(blocks))
        sm = sm[:len(rets)]
        eq = 1.0
        for v in sm:
            eq *= (1 + v)
        cg.append(eq ** (TRADING_DAYS / len(sm)) - 1)
    cg.sort()
    return {"p5": cg[int(B * .05)], "med": cg[B // 2], "p95": cg[int(B * .95)],
            "neg": sum(1 for v in cg if v < 0) / B}


def show(tag, dates, rets, extra=""):
    s = perf(rets)
    print("%-26s 年化 %7.2f%%  回撤 %6.1f%%  Calmar %5.2f  Sharpe %5.2f  胜率 %4.1f%% %s"
          % (tag, s["cagr"] * 100, s["mdd"] * 100, s["calmar"], s["sharpe"],
             s["win"] * 100, extra))
    return s


# ================================================================ 因子 1：横截面 carry
def make_xcarry(P, win=252, K=3, min_names=8, side="both"):
    """横截面展期因子：做多 backwardation 最强、做空 contango 最强。

    carry 强度 = 过去 win 日累计换月跳空。累计为负 = backwardation（现货紧张）。
    市场中性：多空各 K 个品种，各 1/(2K)，总敞口 1。
    """
    dates, gap, px = P["dates"], P["gap"], P["px"]
    syms = P["syms"]
    n = len(dates)

    # 预算累计跳空（滚动窗口和）
    cum = {}
    for s in syms:
        g = gap[s]
        acc = [0.0] * n
        run = 0.0
        for j in range(n):
            run += g[j]
            acc[j] = run
        cum[s] = acc

    def wfn(i):
        if i < win:
            return {}
        sc = {}
        for s in syms:
            if px[s][i] is None:
                continue
            c = cum[s][i] - cum[s][i - win]
            if abs(c) > 1e-9:
                sc[s] = c
        if len(sc) < min_names:
            return {}
        rank = sorted(sc, key=lambda k: sc[k])   # 升序：最负在前
        longs = rank[:K]                          # backwardation -> 做多
        shorts = rank[-K:]                        # contango -> 做空
        w = {}
        if side == "both":
            if longs:
                for s in longs:
                    w[s] = 1.0 / (2 * len(longs))
            if shorts:
                for s in shorts:
                    w[s] = -1.0 / (2 * len(shorts))
        elif side == "long":                      # 只做多 backwardation
            for s in longs:
                w[s] = 1.0 / len(longs)
        else:                                     # 只做空 contango
            for s in shorts:
                w[s] = -1.0 / len(shorts)
        return w

    return wfn


# ================================================================ 因子 2：横截面动量
def make_xmom(P, N=120, K=3, min_names=8, skip=0):
    dates, px = P["dates"], P["px"]
    syms = P["syms"]
    n = len(dates)

    def wfn(i):
        if i < N + 1 + skip:
            return {}
        mom = {}
        for s in syms:
            p0 = px[s][i - N - skip]
            p1 = px[s][i - skip] if skip else px[s][i]
            if p0 and p1 and p0 > 0:
                mom[s] = p1 / p0 - 1.0
        if len(mom) < min_names:
            return {}
        rank = sorted(mom, key=lambda k: mom[k])
        w = {}
        for s in rank[-K:]:
            w[s] = 1.0 / (2 * K)
        for s in rank[:K]:
            w[s] = -1.0 / (2 * K)
        return w

    return wfn


# ================================================================ 因子 3：双因子
def make_xcombo(P, win=252, N=120, K=3, min_names=8):
    """carry 给方向，动量做确认：只在「carry 与动量同向」的品种上持仓。

    这是商品 CTA 最经典的组合：carry 解释长期风险溢价，
    动量避开「便宜但还在跌」的价值陷阱。
    """
    dates, gap, px = P["dates"], P["gap"], P["px"]
    syms = P["syms"]
    n = len(dates)
    cum = {}
    for s in syms:
        run = 0.0
        acc = []
        for j in range(n):
            run += gap[s][j]
            acc.append(run)
        cum[s] = acc

    def wfn(i):
        if i < max(win, N + 1):
            return {}
        sc, mom = {}, {}
        for s in syms:
            if not px[s][i]:
                continue
            sc[s] = cum[s][i] - cum[s][i - win]
            p0, p1 = px[s][i - N], px[s][i]
            if p0 and p1:
                mom[s] = p1 / p0 - 1.0
        common = [s for s in sc if s in mom]
        if len(common) < min_names:
            return {}
        longs = [s for s in common if sc[s] < 0 and mom[s] > 0]
        shorts = [s for s in common if sc[s] > 0 and mom[s] < 0]
        if not longs or not shorts:
            return {}
        longs = sorted(longs, key=lambda s: sc[s])[:K]
        shorts = sorted(shorts, key=lambda s: -sc[s])[:K]
        w = {}
        for s in longs:
            w[s] = 1.0 / (2 * len(longs))
        for s in shorts:
            w[s] = -1.0 / (2 * len(shorts))
        return w

    return wfn


# ================================================================ 因子 4：产业链配对
PAIRS = [
    ("M", "RM", "豆粕/菜粕 饲料替代"),
    ("Y", "P", "豆油/棕榈油 油脂替代"),
    ("C", "CS", "玉米/淀粉 加工链"),
    ("TA", "MA", "PTA/甲醇 聚酯链"),
    ("RB", "HC", "螺纹/热卷 板材替代"),
    ("FG", "SA", "玻璃/纯碱 光伏链"),
    ("J", "JM", "焦炭/焦煤 炼焦链"),
    ("L", "PP", "塑料/聚丙烯 烯烃"),
    ("OI", "RM", "菜油/菜粕 压榨"),
    ("SF", "SM", "硅铁/锰硅 铁合金"),
]


def run_pair(P, a, b, look=60, z_in=2.0, z_out=0.5, cost=COST, beta_hedge=True):
    """配对价差 z-score 回归。

    beta_hedge=True 时用滚动 OLS 求对冲比：log(pa) = alpha + beta*log(pb)，
    空 1 单位 A 就多 beta 单位 B（按名义），这才是统计意义上的价差。
    beta_hedge=False 退化为等名义对冲（1:1）。

    价差 s = log(pa) - beta*log(pb)，z = (s - mean_look) / std_look。
    z > +z_in  -> A 相对 B 贵 -> 空 A 多 B
    z < -z_in  -> A 相对 B 便宜 -> 多 A 空 B
    |z| < z_out -> 平仓
    决策用 i-1 收盘，持仓在 i 日。
    """
    dates, px = P["dates"], P["px"]
    n = len(dates)
    pos = 0
    rets, turns = [], []
    for i in range(1, n):
        pa0, pb0 = px[a][i - 1], px[b][i - 1]
        pa1, pb1 = px[a][i], px[b][i]
        r = 0.0
        beta = 1.0
        # 用 i-1 及以前的数据估 beta（无前视）
        if beta_hedge and i - 1 >= look + 1 and pa0 and pb0 and pa0 > 0 and pb0 > 0:
            xs, ys = [], []
            for j in range(i - look - 1, i):
                if px[a][j] and px[b][j] and px[a][j] > 0 and px[b][j] > 0:
                    xs.append(math.log(px[b][j]))
                    ys.append(math.log(px[a][j]))
            if len(xs) >= look // 2:
                mx = statistics.mean(xs)
                my = statistics.mean(ys)
                cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                var = sum((x - mx) ** 2 for x in xs)
                if var > 1e-12:
                    beta = max(0.2, min(5.0, cov / var))
        # 当日持仓收益：A 腿 0.5 敞口，B 腿 -0.5*beta 敞口
        if pa0 and pb0 and pa1 and pb1 and pa0 > 0 and pb0 > 0:
            r += pos * 0.5 * ((pa1 / pa0 - 1.0) - beta * (pb1 / pb0 - 1.0))
        # 决策
        newpos = pos
        if i - 1 >= look + 1 and pa0 and pb0 and pa0 > 0 and pb0 > 0:
            hist = []
            for j in range(i - look - 1, i):
                if px[a][j] and px[b][j] and px[a][j] > 0 and px[b][j] > 0:
                    hist.append(math.log(px[a][j]) - beta * math.log(px[b][j]))
            if len(hist) >= look // 2:
                mu = statistics.mean(hist)
                sd = statistics.pstdev(hist)
                if sd > 1e-9:
                    z = ((math.log(pa0) - beta * math.log(pb0)) - mu) / sd
                    if pos == 0:
                        if z > z_in:
                            newpos = -1
                        elif z < -z_in:
                            newpos = 1
                    else:
                        if abs(z) < z_out or z * pos > 0 and abs(z) < z_in:
                            newpos = 0
        if newpos != pos:
            # 两腿都换：各 0.5 敞口
            r -= abs(newpos - pos) * 0.5 * cost * 2
            turns.append(abs(newpos - pos))
        else:
            turns.append(0)
        pos = newpos
        rets.append(r)
    return dates[1:], rets, turns


# ================================================================ 因子 5：波动率状态过滤
def make_regime(P, base="carry_trend", lo_pct=0.4, hi_pct=0.9):
    """波动率状态过滤：只在「市场整体波动率」处于中低位时持有趋势仓。

    市场波动率 = 所有品种 dvol 的中位数。过高时（危机/暴动）趋势策略
    容易在反转里连续被打，故降仓或空仓。
    这里做的是「时序」版本：单品种信号 + 组合层按市场波动缩放。
    """
    dates, dvol = P["dates"], P["dvol"]
    syms = P["syms"]
    n = len(dates)

    mkt = []
    for i in range(n):
        vals = [dvol[s][i] for s in syms if dvol[s][i]]
        mkt.append(statistics.median(vals) if vals else None)

    def wfn(i):
        # 用 ft 的单品种信号，横截面汇总
        w = {}
        for s in syms:
            b = _bars_cache.get(s)
            if b is None:
                continue
            j = _date_pos[b].get(dates[i])
            if j is None or j < 1:
                continue
            sg = _sig_cache.get(s)
            if sg is None:
                continue
            w[s] = sg[j - 1] * 0.5 / max(1, len(syms) ** 0.5)
        return w

    return wfn


_bars_cache, _date_pos, _sig_cache = {}, {}, {}


def load_sig_cache(P, sigfn):
    for s in P["syms"]:
        b = ft.load(s)
        if not b:
            continue
        _bars_cache[s] = b
        _date_pos[s] = {x["date"]: k for k, x in enumerate(b)}
        _sig_cache[s] = sigfn(b)


# ================================================================ 各命令
def cmd_xcarry(P):
    print("\n【横截面展期因子 XC-Carry】做多 backwardation 最强 / 做空 contango 最强")
    print("%-30s %10s %10s %10s %10s %8s" % ("参数", "年化", "最大回撤", "Calmar", "Sharpe", "胜率"))
    print("-" * 82)
    out = {}
    for win in [126, 252, 504]:
        for K in [2, 3, 5]:
            d, r, t = run_weights(P, make_xcarry(P, win=win, K=K))
            s = perf(r)
            out["win%d_K%d" % (win, K)] = s
            print("%-30s %9.2f%% %9.1f%% %10.2f %10.2f %7.1f%%" % (
                "win=%d K=%d" % (win, K), s["cagr"] * 100, s["mdd"] * 100,
                s["calmar"], s["sharpe"], s["win"] * 100))
    return out


def cmd_xmom(P):
    print("\n【横截面动量因子 XC-Mom】做多最强 / 做空最弱")
    print("%-30s %10s %10s %10s %10s %8s" % ("参数", "年化", "最大回撤", "Calmar", "Sharpe", "胜率"))
    print("-" * 82)
    out = {}
    for N in [20, 60, 120, 250]:
        for K in [2, 3, 5]:
            d, r, t = run_weights(P, make_xmom(P, N=N, K=K))
            s = perf(r)
            out["N%d_K%d" % (N, K)] = s
            print("%-30s %9.2f%% %9.1f%% %10.2f %10.2f %7.1f%%" % (
                "N=%d K=%d" % (N, K), s["cagr"] * 100, s["mdd"] * 100,
                s["calmar"], s["sharpe"], s["win"] * 100))
    return out


def cmd_xcombo(P):
    print("\n【双因子 XC-Carry x 动量】carry 定方向、动量做确认（只做两因子同向的品种）")
    print("%-30s %10s %10s %10s %10s %8s" % ("参数", "年化", "最大回撤", "Calmar", "Sharpe", "胜率"))
    print("-" * 82)
    out = {}
    for win in [126, 252]:
        for N in [60, 120, 250]:
            for K in [2, 3]:
                d, r, t = run_weights(P, make_xcombo(P, win=win, N=N, K=K))
                s = perf(r)
                out["w%d_N%d_K%d" % (win, N, K)] = s
                print("%-30s %9.2f%% %9.1f%% %10.2f %10.2f %7.1f%%" % (
                    "carry%d mom%d K=%d" % (win, N, K), s["cagr"] * 100, s["mdd"] * 100,
                    s["calmar"], s["sharpe"], s["win"] * 100))
    return out


def cmd_pairs(P):
    print("\n【产业链配对价差回归】等名义对冲，z 阈值 ±2.0，回看 60 日")
    print("%-30s %9s %9s %8s %8s %8s %7s" % ("配对", "年化", "最大回撤", "Calmar", "Sharpe", "持仓胜率", "持仓占比"))
    print("-" * 82)
    out = {}
    for a, b, desc in PAIRS:
        if a not in P["syms"] or b not in P["syms"]:
            continue
        d, r, t = run_pair(P, a, b)
        s = perf(r)
        out["%s-%s" % (a, b)] = {"desc": desc, "stat": s}
        print("%-30s %8.2f%% %8.1f%% %8.2f %8.2f %7.1f%% %6.1f%%" % (
            "%s-%s %s" % (a, b, desc), s["cagr"] * 100, s["mdd"] * 100,
            s["calmar"], s["sharpe"], s["win_act"] * 100, s["active"] * 100))
    return out


def _align(d1, r1, d2, r2):
    """按日期对齐两条收益序列，取交集。"""
    m1 = dict(zip(d1, r1))
    m2 = dict(zip(d2, r2))
    common = sorted(set(m1) & set(m2))
    return common, [m1[d] for d in common], [m2[d] for d in common]


def _stdev(a):
    return statistics.pstdev(a) if len(a) > 1 else 0.0


def _corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = statistics.mean(a), statistics.mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a) / n)
    sb = math.sqrt(sum((x - mb) ** 2 for x in b) / n)
    if sa < 1e-12 or sb < 1e-12:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n * sa * sb)


def cmd_deep(P, win=252, K=5):
    """对最优横截面 carry 做全套稳健性检验。"""
    print("\n" + "=" * 78)
    print("【深度验证】横截面展期因子  XC-Carry(win=%d, K=%d)" % (win, K))
    print("=" * 78)

    wfn = make_xcarry(P, win=win, K=K)
    d, r, t = run_weights(P, wfn)
    s = perf(r)
    print("\n① 全样本  %s ~ %s" % (d[0], d[-1]))
    print("   年化 %.2f%%  回撤 %.1f%%  Calmar %.2f  Sharpe %.2f  倍数 %.2fx"
          % (s["cagr"] * 100, s["mdd"] * 100, s["calmar"], s["sharpe"], s["mult"]))
    print("   日均换手 %.3f%%  年化换手成本约 %.2f%%"
          % (statistics.mean(t) * 100, statistics.mean(t) * TRADING_DAYS * COST * 100))

    # ② 逐年
    print("\n② 逐年收益")
    by_year = {}
    yrs = sorted({x[:4] for x in d})
    line = ""
    for y in yrs:
        sub = [x for dd, x in zip(d, r) if dd[:4] == y]
        if len(sub) < 20:
            continue
        yy = perf(sub)
        by_year[y] = yy
        line += "%s:%+.1f%%  " % (y, yy["cagr"] * 100)
        if len(line) > 68:
            print("   " + line); line = ""
    if line:
        print("   " + line)
    neg = [y for y, v in by_year.items() if v["cagr"] < 0]
    print("   负收益年份 %d/%d：%s" % (len(neg), len(by_year), " ".join(neg) if neg else "无"))

    # ③ 样本内外
    print("\n③ 样本内外")
    for lab, lo, hi in [("样本内 2009-2018", "2009-01-01", "2018-12-31"),
                        ("样本外 2019-2026", "2019-01-01", "2099-12-31")]:
        sub = seg(d, r, lo, hi)
        if sub:
            print("   %-18s 年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f  Sharpe %5.2f"
                  % (lab, sub["cagr"] * 100, sub["mdd"] * 100, sub["calmar"], sub["sharpe"]))

    # ④ 自助
    b = boot(d, r)
    if b:
        print("\n④ 分块自助（600 次，块长 30 日）")
        print("   年化 90%% 区间 [%.2f%%, %.2f%%]  中位 %.2f%%  年化为负概率 %.1f%%"
              % (b["p5"] * 100, b["p95"] * 100, b["med"] * 100, b["neg"] * 100))

    # ⑤ 多空分解
    print("\n⑤ 多空分解（验证收益不是靠某一侧的市场漂移）")
    dl, rl, _ = run_weights(P, make_xcarry(P, win=win, K=K, side="long"))
    ds, rs, _ = run_weights(P, make_xcarry(P, win=win, K=K, side="short"))
    sl, ss = perf(rl), perf(rs)
    print("   只做多 backwardation : 年化 %6.2f%%  回撤 %5.1f%%  Sharpe %5.2f"
          % (sl["cagr"] * 100, sl["mdd"] * 100, sl["sharpe"]))
    print("   只做空 contango      : 年化 %6.2f%%  回撤 %5.1f%%  Sharpe %5.2f"
          % (ss["cagr"] * 100, ss["mdd"] * 100, ss["sharpe"]))
    print("   -> 两侧%s" % ("都为正，收益不依赖市场方向" if sl["cagr"] > 0 and ss["cagr"] > 0
                          else "有一侧为负，需警惕漂移"))

    # ⑥ 换手频率
    print("\n⑥ 调仓频率对成本的影响")
    reb = {}
    for rb in [1, 5, 10, 21]:
        d2, r2, t2 = run_weights(P, make_xcarry(P, win=win, K=K), rebal=rb)
        s2 = perf(r2)
        reb[rb] = {"stat": s2, "turn": statistics.mean(t2)}
        print("   每 %2d 日调仓  年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f  日均换手 %.3f%%"
              % (rb, s2["cagr"] * 100, s2["mdd"] * 100, s2["calmar"],
                 statistics.mean(t2) * 100))

    # ⑦ 与现有策略的相关性 + 组合
    print("\n⑦ 与现有 FG+HC 时序策略的关系")
    d3, r3, c3 = ft.run_portfolio(["FG", "HC"], ft.sig_carry_trend)
    st3 = ft.stats(d3, r3, c3)
    print("   FG+HC 时序策略       年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f  Sharpe %5.2f"
          % (st3["cagr"] * 100, st3["mdd"] * 100, st3["calmar"], st3["sharpe"]))
    cd, cr1, cr2 = _align(d, r, d3, r3)
    rho = _corr(cr1, cr2)
    print("   两者日收益相关系数 %.2f" % rho)
    comb = {}
    for wgt in [0.3, 0.5, 0.7]:
        mix = [(1 - wgt) * a + wgt * b2 for a, b2 in zip(cr1, cr2)]
        sc = perf(mix)
        comb[wgt] = sc
        print("   组合 横截面%.0f%% + FG+HC %.0f%%  年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f  Sharpe %5.2f"
              % ((1 - wgt) * 100, wgt * 100, sc["cagr"] * 100, sc["mdd"] * 100,
                 sc["calmar"], sc["sharpe"]))

    return {"stat": s, "by_year": by_year, "boot": b,
            "long": sl, "short": ss, "rebal": reb,
            "corr_fghc": rho, "fghc": st3, "combine": comb,
            "params": {"win": win, "K": K}}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    syms = ft.universe()
    print("品种 %d 个，构建面板…" % len(syms))
    P = build_panel(syms)
    print("面板就绪：%s ~ %s，%d 个交易日\n" % (P["dates"][0], P["dates"][-1], len(P["dates"])))

    D = {"panel": {"start": P["dates"][0], "end": P["dates"][-1],
                   "days": len(P["dates"]), "syms": P["syms"]}}
    if cmd in ("xcarry", "all"):
        D["xcarry"] = cmd_xcarry(P)
    if cmd in ("xmom", "all"):
        D["xmom"] = cmd_xmom(P)
    if cmd in ("xcombo", "all"):
        D["xcombo"] = cmd_xcombo(P)
    if cmd in ("pairs", "all"):
        D["pairs"] = cmd_pairs(P)
    if cmd in ("deep", "all"):
        D["deep"] = cmd_deep(P)

    if cmd == "all":
        with open(os.path.join(HERE, "research2.json"), "w", encoding="utf-8") as f:
            json.dump(D, f, ensure_ascii=False, indent=1)
        print("\n已写出 research2.json")


if __name__ == "__main__":
    main()
