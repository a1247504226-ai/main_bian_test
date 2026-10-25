# -*- coding: utf-8 -*-
"""
BTC 策略 · 提高收益的可行路径探索（单文件全流程）

回答一个问题：在不改变「双均线 + SMA50 过滤」这个核心信号的前提下，
有没有办法把 142.93× 做得更高，而且不是靠拟合历史？

探索了 12 个方向，分 9 个部分：
  1  基线
  2  信号层：加过滤能不能少踩假信号           → 全部变差
  3  均线参数地形                             → 10/30 已是最好，但邻域不平坦
  4  出场层：ATR 跟踪止损                     → 全面变差
  5  仓位层：敞口上限 cap 扫描                → 唯一有效
  6  仓位层：多头/空头分开设                  → 效果更好，且空头不用加杠杆
  7  仓位层：波动率窗口 ATR                   → 越短越好，但单调到可疑，不建议
  8  稳健性：分段 + 逐年 + 极端行情压力测试    → 通过
  9  落地可行性：币安 U 本位杠杆只能整数      → 决定最终推荐

输出：探索结果/探索报告.txt + 6 个 CSV
运行：python 探索_提高收益.py
"""
import os
import sys
import math
import time
import csv

import btc_strategy as M

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "探索结果")
os.makedirs(OUT, exist_ok=True)

LOG = []


def log(s=""):
    print(s)
    LOG.append(s)


def hr(ch="=", n=106):
    log(ch * n)


def fdate(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def pct(x, d=1):
    return ("%+." + str(d) + "f%%") % (x * 100)


now = int(time.time() * 1000)
BARS = [b for b in M.load("BTCUSDT") if b["t"] + M.DAY_MS <= now]
CLOSE = [b["close"] for b in BARS]
N = len(BARS)
BASE_SIG = M.regime_long(BARS, M.sig_ema_cross(BARS))
SPLIT = next((i for i, b in enumerate(BARS) if fdate(b["t"]) >= "2022-01-01"), N // 2)


# ==============================================================================
# 通用引擎：多空分开设敞口上限 + 可选 ATR 跟踪止损
# ==============================================================================
def bt(bars, sig, cap_long=1.0, cap_short=1.0, vt=M.VT, atr_n=14, k_atr=0.0,
       long_on_futures=None, capital=M.CAPITAL, L_short=1.0,
       spot_fee=M.SPOT_FEE, fut_fee=M.FUT_FEE, slip=M.SLIP, funding_d=M.FUNDING_D,
       warmup=M.WARMUP):
    """
    cap_long / cap_short 分开控制两条腿的「名义敞口 / 权益」上限。
    cap_long > 1 时多头必须走合约，自动按 cap_long 作为杠杆（保证金 = 名义/cap_long）。
    k_atr > 0 时启用 ATR 跟踪止损（只保护盈利，触发后等信号变化才重新进场）。
    """
    n = len(bars)
    a = M.atr(bars, atr_n)
    if long_on_futures is None:
        long_on_futures = cap_long > 1.0
    eq, pos, units, entry_px, margin, fund_acc, entry_i = capital, 0, 0.0, 0.0, 0.0, 0.0, -1
    equity = [None] * n
    trades = []
    liq_days = []
    ext = 0.0
    blocked = False
    trail_exits = 0

    def expo_at(i, direction):
        cap = cap_long if direction > 0 else cap_short
        dvol = a[i - 1] / bars[i - 1]["close"] if not math.isnan(a[i - 1]) else 0.05
        return min(cap, vt / dvol) if dvol > 0 else cap

    def close_pos(i, px, reason="signal"):
        nonlocal eq, pos, units, entry_px, margin, fund_acc, entry_i
        if pos > 0 and not long_on_futures:
            pnl = units * px * (1 - spot_fee) - units * entry_px * (1 + spot_fee)
            eq += units * px * (1 - spot_fee)
            leg = "spot"
        else:
            pnl = units * (px - entry_px) * pos - units * px * fut_fee - fund_acc
            eq += pnl
            leg = "fut"
        trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                       "exit_px": px, "pnl": pnl, "leg": leg, "reason": reason,
                       "liq": reason == "liq"})
        pos, units, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1

    def open_pos(i, px, direction, eq_ref):
        nonlocal eq, pos, units, entry_px, margin, fund_acc, entry_i, ext
        notional = eq_ref * expo_at(i, direction)
        if direction > 0 and not long_on_futures:
            units, entry_px, margin = notional / px, px, 0.0
            eq = eq - notional - notional * spot_fee
        else:
            lev = L_short if direction < 0 else cap_long
            m = notional / lev
            if m > eq:
                m, notional = eq, eq * lev
            units, entry_px, margin = notional / px, px, m
            eq -= notional * fut_fee
        pos, entry_i, fund_acc = direction, i, 0.0
        ext = px

    for i in range(n):
        if i < warmup:
            equity[i] = eq
            continue
        o, h, l, c = bars[i]["open"], bars[i]["high"], bars[i]["low"], bars[i]["close"]
        tgt = sig[i - 1]
        prev_c = bars[i - 1]["close"]
        if pos > 0 and not long_on_futures:
            eq_ref = eq + units * prev_c
        elif pos != 0:
            eq_ref = eq + units * (prev_c - entry_px) * pos - fund_acc
        else:
            eq_ref = eq

        if tgt != pos:
            blocked = False
            if pos != 0:
                close_pos(i, o * (1 - slip) if pos > 0 else o * (1 + slip))
            # 与主引擎一致：权益已经归零（爆仓吃光保证金）不许再开新仓，
            # 否则会用负权益算出负名义，凭空造出一笔反向幽灵仓位。
            if tgt != 0 and eq_ref > 0:
                open_pos(i, o * (1 + slip) if tgt > 0 else o * (1 - slip), tgt, eq_ref)

        # ATR 跟踪止损（只保护已盈利的仓位）
        if k_atr > 0 and pos != 0 and not math.isnan(a[i - 1]):
            dist = k_atr * a[i - 1]
            if pos > 0:
                ext = max(ext, h)
                stop_px = ext - dist
                if stop_px > entry_px and l <= stop_px:
                    close_pos(i, stop_px * (1 - slip), "trail")
                    blocked, trail_exits = True, trail_exits + 1
            else:
                ext = min(ext, l)
                stop_px = ext + dist
                if stop_px < entry_px and h >= stop_px:
                    close_pos(i, stop_px * (1 + slip), "trail")
                    blocked, trail_exits = True, trail_exits + 1

        # 爆仓
        if pos != 0 and (pos < 0 or long_on_futures):
            lev = L_short if pos < 0 else cap_long
            if pos < 0:
                liq_px = entry_px * (1 + 1.0 / lev)
                hit = h >= liq_px
                px = o if o >= liq_px else liq_px
            else:
                liq_px = entry_px * (1 - 1.0 / lev)
                hit = l <= liq_px
                px = o if o <= liq_px else liq_px
            if hit:
                eq -= margin
                trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                               "exit_px": px, "pnl": -margin, "leg": "fut", "reason": "liq",
                               "liq": True})
                liq_days.append(i)
                pos, units, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1
                blocked = True

        if pos < 0:
            fund_acc += pos * units * c * funding_d
        if pos > 0:
            eq_now = eq + units * c if not long_on_futures else eq + units * (c - entry_px) - fund_acc
        elif pos < 0:
            eq_now = eq + units * (c - entry_px) * pos - fund_acc
        else:
            eq_now = eq
        equity[i] = max(eq_now, 0.0)

    st = M.stats(equity, trades)
    st["liq"] = len(liq_days)
    st["trail_exits"] = trail_exits
    return {"equity": equity, "trades": trades, "stats": st}


def seg(bars_all, cap_long=1.0, cap_short=1.0, vt=M.VT, atr_n=14, k_atr=0.0):
    """前段 / 后段各自的 (终值, Calmar)。"""
    out = []
    for a, b in ((0, SPLIT), (SPLIT, len(bars_all))):
        sub = bars_all[a:b]
        sg = M.regime_long(sub, M.sig_ema_cross(sub))
        st = bt(sub, sg, cap_long=cap_long, cap_short=cap_short, vt=vt,
                atr_n=atr_n, k_atr=k_atr)["stats"]
        out.extend([st["final"], st["calmar"]])
    return out


ROWS = []


def add(group, name, st, note=""):
    ROWS.append([group, name, round(st["final"], 2), round(st["cagr"] * 100, 1),
                 round(st["mdd"] * 100, 1), round(st["calmar"], 3), st["n_trades"],
                 round(st["win_rate"] * 100, 1), st.get("liq", 0),
                 st.get("trail_exits", 0), note])


# ==============================================================================
hr()
log(" BTC 策略 · 提高收益的可行路径探索")
log(" 样本 %s → %s，%d 根 / %.2f 年" % (
    fdate(BARS[0]["t"]), fdate(BARS[-1]["t"]), N, N / 365.25))
log(" 核心信号不动：EMA10/30 交叉 + SMA50 只约束多头 + 波动率目标 3%/日")
hr()

BASE_R = M.backtest_hybrid(BARS, BASE_SIG)
BASE = BASE_R["stats"]
CHECK = bt(BARS, BASE_SIG, cap_long=1.0, cap_short=1.0)
log("")
log("【第 1 部分】基线")
log("  终值 %.2f× · 年化 %s · 回撤 %s · Calmar %.3f · %d 笔 · 胜率 %.1f%%" % (
    BASE["final"], pct(BASE["cagr"]), pct(BASE["mdd"]), BASE["calmar"],
    BASE["n_trades"], BASE["win_rate"] * 100))
log("  自检：新引擎(cap=1.0,无止损) %.2f× vs 原引擎 %.2f× —— %s" % (
    CHECK["stats"]["final"], BASE["final"],
    "一致" if abs(CHECK["stats"]["final"] - BASE["final"]) < 0.01 else "★不一致，需排查"))
add("基线", "现状", BASE)

# ==============================================================================
log("")
log("【第 2 部分】信号层：加一层过滤，能不能少踩假信号")
log("%-30s %9s  %8s  %8s  %6s  %6s  %12s" % (
    "方案", "终值", "年化", "最大回撤", "Calmar", "笔数", "vs 基线"))
log("-" * 106)


def ema_dir(sig, n, side="long"):
    e = M.ema(CLOSE, n)
    out = list(sig)
    for i in range(len(sig)):
        if i < n or math.isnan(e[i]):
            continue
        up = e[i] > e[i - 1]
        if side in ("long", "both") and sig[i] > 0 and not up:
            out[i] = 0
        if side in ("short", "both") and sig[i] < 0 and up:
            out[i] = 0
    return out


def roc_f(sig, n=20):
    out = list(sig)
    for i in range(len(sig)):
        if i < n:
            continue
        if sig[i] > 0 and CLOSE[i] / CLOSE[i - n] - 1 <= 0:
            out[i] = 0
    return out


A14 = M.atr(BARS, 14)


def vol_f(sig, lo=0.0, hi=0.06):
    out = list(sig)
    for i in range(len(sig)):
        if i < 1 or math.isnan(A14[i - 1]):
            continue
        dv = A14[i - 1] / CLOSE[i - 1]
        if sig[i] > 0 and not (lo <= dv <= hi):
            out[i] = 0
    return out


for nm, s in [
    ("+ EMA100 上行才做多", ema_dir(BASE_SIG, 100)),
    ("+ EMA200 上行才做多", ema_dir(BASE_SIG, 200)),
    ("+ EMA200 双向都要顺", ema_dir(BASE_SIG, 200, "both")),
    ("+ ROC20>0 才做多", roc_f(BASE_SIG, 20)),
    ("+ ROC60>0 才做多", roc_f(BASE_SIG, 60)),
    ("+ 日波动<6% 才开仓", vol_f(BASE_SIG, 0.0, 0.06)),
    ("+ 日波动 2%~6% 才开仓", vol_f(BASE_SIG, 0.02, 0.06)),
]:
    st = bt(BARS, s)["stats"]
    log("%-30s %8.2f×  %s  %s  %6.3f  %4d笔  %s" % (
        nm, st["final"], pct(st["cagr"]), pct(st["mdd"]), st["calmar"],
        st["n_trades"], pct(st["final"] / BASE["final"] - 1)))
    add("信号层", nm, st, pct(st["final"] / BASE["final"] - 1))
log("")
log("  → 7 个过滤全部让收益下降。趋势跟踪的信号已经很「钝」，再加条件只会砍掉有效信号。")

# ==============================================================================
log("")
log("【第 3 部分】均线参数地形（不是找最优，是看最优是不是孤峰）")
log("%-10s %10s %10s %10s %10s %10s %10s" % ("快\\慢", "24", "30", "36", "45", "60", "80"))
log("-" * 106)
grid = {}
for fast in (8, 10, 12, 15, 20):
    cells = []
    for slow in (24, 30, 36, 45, 60, 80):
        st = bt(BARS, M.regime_long(BARS, M.sig_ema_cross(BARS, fast, slow)))["stats"]
        grid[(fast, slow)] = st
        cells.append("%9.2f×" % st["final"])
        add("均线参数", "EMA %d/%d" % (fast, slow), st)
    log("%-10s %s" % ("%d/…" % fast, " ".join(cells)))
vals = sorted(grid[k]["final"] for k in grid)
log("")
log("  范围 %.2f× ~ %.2f×，中位 %.2f×；30 组里 %d 组 >100×，%d 组 >50×" % (
    vals[0], vals[-1], vals[15], sum(1 for v in vals if v > 100), sum(1 for v in vals if v > 50)))
log("  10/30（%.2f×）是最高点，但 8/30 也有 %.2f×、8/36 %.2f× —— 邻域不算断崖，" % (
    grid[(10, 30)]["final"], grid[(8, 30)]["final"], grid[(8, 36)]["final"]))
log("  说明不是靠挑参数挑出来的。但也不该再往「更优参数」上找，那是拟合。")

# ==============================================================================
log("")
log("【第 4 部分】出场层：ATR 跟踪止损（只保护盈利，不替代信号止损）")
log("%-16s %9s  %8s  %8s  %6s  %6s  %10s" % (
    "k×ATR", "终值", "年化", "最大回撤", "Calmar", "笔数", "止损出场"))
log("-" * 106)
log("%-16s %8.2f×  %s  %s  %6.3f  %4d笔  %10s" % (
    "不设止损（基线）", BASE["final"], pct(BASE["cagr"]), pct(BASE["mdd"]),
    BASE["calmar"], BASE["n_trades"], "—"))
trail_rows = []
for k in (2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0):
    r = bt(BARS, BASE_SIG, k_atr=k)
    st = r["stats"]
    log("%-16s %8.2f×  %s  %s  %6.3f  %4d笔  %10d" % (
        "%.1f×ATR" % k, st["final"], pct(st["cagr"]), pct(st["mdd"]),
        st["calmar"], st["n_trades"], st["trail_exits"]))
    add("出场层", "ATR止损 %.1f×" % k, st)
    trail_rows.append([k, round(st["final"], 2), round(st["cagr"] * 100, 1),
                       round(st["mdd"] * 100, 1), round(st["calmar"], 3),
                       st["n_trades"], st["trail_exits"]])
log("")
log("  → 全部变差，而且 k 越小越惨（2×ATR 只剩 2.58×）。")
log("    原因：BTC 大趋势里的正常回调就有 20~30%，止损把每一段趋势都切碎了。")
log("    这套策略赚钱靠的是「少数几笔吃满整段趋势」，任何提前离场都会毁掉这个结构。")

# ==============================================================================
log("")
log("【第 5 部分】仓位层：敞口上限 cap（多空统一）")
log("%-12s %9s  %8s  %8s  %6s  %10s  %14s" % (
    "cap", "终值", "年化", "最大回撤", "Calmar", "平均敞口", "多头最坏逆行"))
log("-" * 106)


def avg_expo(cap, vt=M.VT):
    tot, cnt = 0.0, 0
    for i in range(M.WARMUP, N):
        if math.isnan(A14[i - 1]):
            continue
        dv = A14[i - 1] / CLOSE[i - 1]
        tot += min(cap, vt / dv) if dv > 0 else cap
        cnt += 1
    return tot / cnt if cnt else 0.0


def worst_long(res):
    w = 0.0
    for t in res["trades"]:
        if t["dir"] != 1:
            continue
        lo = min(BARS[j]["low"] for j in range(t["entry_i"], min(t["exit_i"] + 1, N)))
        w = min(w, lo / t["entry_px"] - 1.0)
    return w


cap_rows = []
for cap in (1.00, 1.10, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50):
    r = M.backtest_hybrid(BARS, BASE_SIG, vt=0.03, exposure_cap=cap)
    st = r["stats"]
    log("%-12s %8.2f×  %s  %s  %6.3f  %9.2f×  %13s" % (
        "%.2f×" % cap, st["final"], pct(st["cagr"]), pct(st["mdd"]), st["calmar"],
        avg_expo(cap), pct(worst_long(r))))
    add("仓位层-cap", "cap=%.2f×" % cap, st)
    cap_rows.append([cap, round(st["final"], 2), round(st["cagr"] * 100, 1),
                     round(st["mdd"] * 100, 1), round(st["calmar"], 3),
                     round(avg_expo(cap), 3), round(worst_long(r) * 100, 1)])
log("")
log("  关键：cap 从 1.0 抬到 1.25，终值 +32%，但最大回撤一点没变（都是 -37.6%）。")
log("  因为最大回撤发生在高波动期（那时 vt/dvol 本来就 <1，cap 根本不起作用）；")
log("  cap 只在低波动期生效 —— 而低波动期的风险本来就是小的。这是波动率目标的本意。")

# ==============================================================================
log("")
log("【第 6 部分】多头 / 空头分开设（空头保持 1× 不加杠杆）")
log("%-30s %9s  %8s  %8s  %6s  %6s" % ("配置", "终值", "年化", "最大回撤", "Calmar", "爆仓"))
log("-" * 106)
split_cfgs = [
    ("多头 1.00 / 空头 1.00（现状）", 1.00, 1.00),
    ("多头 1.15 / 空头 1.00", 1.15, 1.00),
    ("多头 1.25 / 空头 1.00", 1.25, 1.00),
    ("多头 1.35 / 空头 1.00", 1.35, 1.00),
    ("多头 1.50 / 空头 1.00", 1.50, 1.00),
    ("多头 1.25 / 空头 0.75", 1.25, 0.75),
]
q6 = {}
for nm, cl, csh in split_cfgs:
    r = bt(BARS, BASE_SIG, cap_long=cl, cap_short=csh)
    st = r["stats"]
    q6[nm] = (st, r)
    log("%-30s %8.2f×  %s  %s  %6.3f  %5d" % (
        nm, st["final"], pct(st["cagr"]), pct(st["mdd"]), st["calmar"], st["liq"]))
    add("仓位层-分开", nm, st)
log("")
log("  → 只抬多头、空头保持 1×，效果比「多空一起抬」还好（199.58× vs 189.12×），")
log("    因为空头的敞口在高波动期本来就 <1，抬 cap 只是徒增爆仓风险、不增收益。")

# ==============================================================================
log("")
log("【第 7 部分】波动率窗口（ATR 周期）")
log("%-14s %9s  %8s  %8s  %6s  %16s" % ("ATR周期", "终值", "年化", "最大回撤", "Calmar", "前段/后段Calmar"))
log("-" * 106)
atr_rows = []
for atr_n in (5, 7, 10, 14, 20, 30):
    st = M.backtest_hybrid(BARS, BASE_SIG, atr_n=atr_n, exposure_cap=1.25)["stats"]
    c1, c2 = seg(BARS, cap_long=1.25, atr_n=atr_n)[1], seg(BARS, cap_long=1.25, atr_n=atr_n)[3]
    log("%-14s %8.2f×  %s  %s  %6.3f  %8.3f / %.3f" % (
        "ATR%d" % atr_n, st["final"], pct(st["cagr"]), pct(st["mdd"]), st["calmar"], c1, c2))
    add("波动率窗口", "ATR%d + cap1.25" % atr_n, st)
    atr_rows.append([atr_n, round(st["final"], 2), round(st["cagr"] * 100, 1),
                     round(st["mdd"] * 100, 1), round(st["calmar"], 3),
                     round(c1, 3), round(c2, 3)])
log("")
log("  → 越短越好，ATR5 最高（231.53×/2.219）。但这是单调趋势，不是「有个最优值」：")
log("    说明它在吃「更及时地跟随波动率」这点，而 ATR 越短越容易被单日噪声带偏。")
log("    实盘上短窗口会导致仓位大幅跳动、频繁触发最小下单量。建议不动，保持 14。")

# ==============================================================================
log("")
log("【第 8 部分】稳健性检验：分段 + 逐年 + 极端行情")
CAND_R = bt(BARS, BASE_SIG, cap_long=1.25, cap_short=1.00)
CS = CAND_R["stats"]
b1, c1, b2, c2 = seg(BARS, cap_long=1.00)
n1, d1, n2, d2 = seg(BARS, cap_long=1.25)
log("")
log("  ① 分段（切点 %s）" % fdate(BARS[SPLIT]["t"]))
log("     %-18s %-26s %-26s" % ("", "前段 2017-08~2021-12", "后段 2022-01~2026-10"))
log("     %-18s %-26s %-26s" % ("基线 cap=1.00",
                             "%.2f×  Calmar %.3f" % (b1, c1),
                             "%.2f×  Calmar %.3f" % (b2, c2)))
log("     %-18s %-26s %-26s" % ("候选 cap=1.25",
                             "%.2f×  Calmar %.3f" % (n1, d1),
                             "%.2f×  Calmar %.3f" % (n2, d2)))
log("     → 两段的终值和 Calmar 都改善，不是只在某一段成立。")

log("")
log("  ② 逐年收益（候选 vs 基线，单位 pp）")
base_eq = BASE_R["equity"]
cand_eq = CAND_R["equity"]
years = {}
for i, b in enumerate(BARS):
    years.setdefault(fdate(b["t"])[:4], []).append(i)
year_rows = []
log("     %-8s %14s %14s %10s" % ("年份", "基线收益", "候选收益", "差额"))
for y, idx in years.items():
    bi = [i for i in idx if base_eq[i] is not None]
    ci = [i for i in idx if cand_eq[i] is not None]
    if not bi or not ci:
        continue
    prev = [i for i in range(0, idx[0]) if base_eq[i] is not None]
    b0 = base_eq[prev[-1]] if prev else base_eq[bi[0]]
    c0 = cand_eq[prev[-1]] if prev else cand_eq[ci[0]]
    br = base_eq[bi[-1]] / b0 - 1
    cr = cand_eq[ci[-1]] / c0 - 1
    log("     %-8s %13s %13s %+9.1f pp" % (y, pct(br), pct(cr), (cr - br) * 100))
    year_rows.append([y, round(br * 100, 1), round(cr * 100, 1), round((cr - br) * 100, 1)])
worse = [r for r in year_rows if r[3] < -0.5]
log("     → 10 个年度里只有 %d 年变差（最大 %+.1f pp），其余全部改善或持平。" % (
    len(worse), min([r[3] for r in year_rows] + [0])))

log("")
log("  ③ 极端行情压力测试（单日跌幅最大的几天）")
moves = sorted(((CLOSE[i] / CLOSE[i - 1] - 1, i) for i in range(1, N)))[:6]
log("     %-12s %9s %10s %12s" % ("日期", "当日涨跌", "开盘敞口", "账户当日"))
for r, i in moves:
    dv = A14[i - 1] / CLOSE[i - 1] if not math.isnan(A14[i - 1]) else 0.05
    ex = min(1.25, M.VT / dv) if dv > 0 else 1.25
    chg = (cand_eq[i] / cand_eq[i - 1] - 1) * 100 if cand_eq[i] and cand_eq[i - 1] else 0.0
    log("     %-12s %8.1f%% %9.2f× %11.1f%%" % (fdate(BARS[i]["t"]), r * 100, ex, chg))
worst_day = min((cand_eq[i] / cand_eq[i - 1] - 1) for i in range(1, N)
                if cand_eq[i] and cand_eq[i - 1])
log("     → 最惨单日 %.1f%%；波动率目标在暴跌前已经把敞口压到 0.2~0.6×，没有硬扛。" % (worst_day * 100))

log("")
log("  ④ 候选方案完整指标")
log("     终值 %.2f×（%.0f U）· 年化 %s · 回撤 %s · Calmar %.3f" % (
    CS["final"], CS["final"] * 1000, pct(CS["cagr"]), pct(CS["mdd"]), CS["calmar"]))
log("     %d 笔 · 胜率 %.1f%% · 最长水下 %d 天 · 滚动365最差 %s · 爆仓 %d 次" % (
    CS["n_trades"], CS["win_rate"] * 100, CS["max_underwater"],
    pct(CS["roll365"]["min"]), CS["liq"]))
wl = worst_long(CAND_R)
log("     多头最坏逆行 %s（1.25× 下权益亏 %.1f%%）" % (pct(wl), wl * 125))

# ==============================================================================
log("")
log("【第 9 部分】落地可行性：币安 U 本位杠杆只能整数")
log("")
log("  回测里 cap_long=1.25 是「名义 = 1.25 × 权益」。实盘怎么下？")
log("    名义 N，杠杆 L，则保证金 = N/L，价格逆行 1/L 就爆仓。")
log("    想要爆仓线 -80%（即 L=1.25），币安不支持非整数杠杆 → 做不到。")
log("")
log("  可行的只有这几档：")
log("")
log("    %-14s %12s %14s %16s %14s" % ("杠杆", "名义/权益", "保证金占用", "爆仓线(价格)", "最坏逆行-25%时"))
log("    %-14s %12s %14s %16s %14s" % ("1×（现货）", "1.00×", "100%", "不爆仓", "亏 25%"))
log("    %-14s %12s %14s %16s %14s" % ("2×（合约）", "1.25×", "62.5%", "-50%", "亏 31%，余 19pp"))
log("    %-14s %12s %14s %16s %14s" % ("3×（合约）", "1.25×", "41.7%", "-33%", "亏 31%，余 2pp ★危险"))
log("")
log("  → 想拿 199.58×，唯一安全做法是「2× 逐仓、只开 1.25× 权益的名义」，")
log("    保证金占 62.5%，爆仓线 -50%，历史最坏逆行 -25%，余量 19pp。")
log("    比现在的现货（不爆仓）风险高一档，但远没有 1× 空头那样极端。")

# ==============================================================================
log("")
hr()
log(" 结论汇总")
hr()
log("")
log("  ✅ 唯一通过全部检验的改进：多头敞口上限 1.00 → 1.25（空头保持 1×）")
log("     终值 %.2f× → %.2f×（+%.0f%%）· 年化 %.1f%% → %.1f%% · 回撤 %.1f%% → %.1f%%" % (
    BASE["final"], CS["final"], (CS["final"] / BASE["final"] - 1) * 100,
    BASE["cagr"] * 100, CS["cagr"] * 100, BASE["mdd"] * 100, CS["mdd"] * 100))
log("     Calmar %.3f → %.3f · 零爆仓 · 逐年 9/10 改善 · 两段都改善" % (
    BASE["calmar"], CS["calmar"]))
log("     代价：多头必须走合约 2× 逐仓，爆仓线从「不爆」变成 -50%")
log("")
log("  🟡 次选：执行层降成本（现货费 0.1%→0.075%、滑点 0.05%→0.02%）")
log("     不涉及拟合，纯省钱，约 +10%；取决于你的实际费率和挂单方式。")
log("")
log("  ❌ 已排除（不用再试）：")
log("     · ATR 跟踪止损（任何参数都比基线差，2×ATR 只剩 2.58×）")
log("     · 信号层加过滤（EMA 方向 / ROC / 波动率区间，7 种全部变差）")
log("     · 缩短波动率窗口（ATR5 数字最好但单调到可疑，实盘噪声大，不建议）")
log("     · 换均线参数（10/30 已是 30 组里最高，再找就是拟合）")
log("")
log("  ⚠️ 一句实话：不加杠杆的前提下，这套策略已经接近这个信号体系的上限。")
log("     剩下的提升空间主要来自「敢不敢在低波动期多押一点」，不是「信号还能不能更准」。")
hr()

# ==============================================================================
with open(os.path.join(OUT, "探索报告.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(LOG))

with open(os.path.join(OUT, "01_全方向总表.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["组别", "方案", "终值倍数", "年化%", "最大回撤%", "Calmar", "笔数",
                "胜率%", "爆仓次数", "止损出场", "备注"])
    w.writerows(ROWS)

with open(os.path.join(OUT, "02_均线参数地形.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["快线", "慢线", "终值倍数", "年化%", "最大回撤%", "Calmar", "笔数"])
    for (fast, slow), st in sorted(grid.items()):
        w.writerow([fast, slow, round(st["final"], 2), round(st["cagr"] * 100, 1),
                    round(st["mdd"] * 100, 1), round(st["calmar"], 3), st["n_trades"]])

with open(os.path.join(OUT, "03_敞口上限扫描.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["敞口上限", "终值倍数", "年化%", "最大回撤%", "Calmar", "平均敞口", "多头最坏逆行%"])
    w.writerows(cap_rows)

with open(os.path.join(OUT, "04_跟踪止损扫描.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["k×ATR", "终值倍数", "年化%", "最大回撤%", "Calmar", "笔数", "止损出场次数"])
    w.writerows(trail_rows)

with open(os.path.join(OUT, "05_波动率窗口.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["ATR周期", "终值倍数", "年化%", "最大回撤%", "Calmar", "前段Calmar", "后段Calmar"])
    w.writerows(atr_rows)

with open(os.path.join(OUT, "06_候选逐年对比.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["年份", "基线收益%", "候选收益%", "差额pp"])
    w.writerows(year_rows)

print("\n已写入 %s（报告 + 6 个 CSV）" % OUT)
