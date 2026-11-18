# -*- coding: utf-8 -*-
"""
空头腿杠杆到底影响什么？—— 只玩 BTC，同一套策略，只换空头腿的杠杆倍数。

对比对象（信号、过滤、仓位规则完全相同，只有空头腿杠杆不同）
    A. SMA50 只约束多头 + 空头 1× 逐仓   ← 当前方案
    B. SMA50 只约束多头 + 空头 2× 逐仓
    C. SMA50 只约束多头 + 空头 3× 逐仓

结论（先看这里）
    名义仓位 = 当时权益 × 敞口比例，这个公式里**没有杠杆** ——
    所以 1× / 2× / 3× / 4× 的净值曲线几乎完全相同（实测 1× 只低 0.09%，
    因为 1× 逐仓占满 100% 保证金，个别开仓日会被引擎按「保证金不够」自动缩仓）。
    杠杆只决定两件事：① 押多少保证金；② 价格逆行多少就爆仓。

        1× → 保证金占 100.0%，价格逆行 +100% 才爆（基本等于不会爆）
        2× → 保证金占  50.0%，价格逆行  +50% 爆
        3× → 保证金占  33.3%，价格逆行 +33.3% 爆

    历史空头最深逆行 +22.89%，所以 1× 的安全余量有 77.11pp，2× 只剩 27.11pp。
    ⚠️ 1× 的代价：保证金占满 100%，实盘下单必须留 1% 缓冲（否则交易所按
       「保证金不足」拒单），代码里已自动处理（btc_monitor.short_size_factor）。
    ⚠️ 币安 U 本位合约杠杆只能设整数，所以 1.5× / 2.5× 在回测里能看到
       「收益恒定」，实际下单只能选 1× 或 2×。

输出
    ./对比结果/01_杠杆总览.csv
    ./对比结果/02_分腿统计.csv
    ./对比结果/03_逐年收益.csv
    ./对比结果/04_杠杆扫描.csv
    ./对比结果/05_空头尾部风险.csv
"""
import csv
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btc_strategy as B          # noqa: E402

SYM = "BTCUSDT"
BARS = B.load(SYM)
# 与 btc_strategy.py / btc_monitor.py 完全一致：剔除当天还没收盘的那根日线，
# 否则这里的终值会比回测脚本高（含了一根没走完的 K 线）。
_now = int(time.time() * 1000)
BARS = [b for b in BARS if b["t"] + B.DAY_MS <= _now]
N = len(BARS)
OUT = os.path.join(HERE, "对比结果")


def regime_long(sig, ma=50):
    """只约束多头：收盘价 <= SMA(ma) 时把做多信号置 0，空头照做。"""
    if not ma or ma <= 1:
        return list(sig)
    c = [x["close"] for x in BARS]
    m = B.sma(c, ma)
    out = list(sig)
    for i in range(len(BARS)):
        if m[i] != m[i] or (sig[i] > 0 and c[i] <= m[i]):
            out[i] = 0
    return out


def run(sig, L=1.0, cap=1.0, vt=0.03):
    return B.backtest_hybrid(BARS, sig, capital=B.CAPITAL, L=L, sizing="vol",
                             vt=vt, exposure_cap=cap)


BASE = B.sig_ema_cross(BARS, 10, 30)
F50 = regime_long(BASE, 50)

A = run(F50, L=1.0)      # 空头 1× ← 当前方案
Bb = run(F50, L=2.0)     # 空头 2×
Cc = run(F50, L=3.0)     # 空头 3×


def pct(v, nd=1):
    return "—" if v is None else ("%+.*f%%" % (nd, v * 100))


def info(res):
    s = res["stats"]
    r = s.get("roll365") or {}
    return {"final": s["final"], "cagr": s["cagr"], "mdd": s["mdd"], "calmar": s["calmar"],
            "uw": s.get("max_underwater", 0), "r365": r.get("min"),
            "liq": sum(1 for t in res["trades"] if t.get("liq")), "n": len(res["trades"])}


ia, ib, ic = info(A), info(Bb), info(Cc)

print("=" * 134)
print(" 只玩 BTC · SMA50 只约束多头 · 空头腿 1× / 2× / 3×")
print(" 样本 %s ~ %s，共 %d 根日线" % (B.fdate(BARS[0]["t"]), B.fdate(BARS[-1]["t"]), N))
print("=" * 134)
print("  %-30s %10s %8s %9s %8s %9s %10s %6s %11s %10s"
      % ("方案", "终值倍数", "CAGR", "最大回撤", "Calmar", "最长水下", "滚365最差",
         "爆仓", "保证金占比", "爆仓线"))
print("-" * 134)
for lab, x, L in (("A. SMA50过滤 + 空头 1×  ★当前", ia, 1.0),
                  ("B. SMA50过滤 + 空头 2×", ib, 2.0),
                  ("C. SMA50过滤 + 空头 3×", ic, 3.0)):
    print("  %-30s %9.2f× %7.1f%% %8.1f%% %8.3f %7d 天 %9s %6d %10.1f%% %10s"
          % (lab, x["final"], x["cagr"] * 100, x["mdd"] * 100, x["calmar"], x["uw"],
             pct(x["r365"]), x["liq"], 100.0 / L, "+%.1f%%" % (100.0 / L)))
print("-" * 134)
print("  1× 与 2× 终值差 %.4f×（%.4f%%）—— 差异只来自「1× 保证金占满 100%%，个别开仓日被自动缩仓」。"
      % (abs(ia["final"] - ib["final"]), abs(ia["final"] - ib["final"]) / ib["final"] * 100))
print("  2× 与 3× 终值差 %.6f× —— 除爆仓线外，收益逐日完全一致。"
      % abs(ib["final"] - ic["final"]))
print("  所以：杠杆不影响收益，只决定「保证金占用」与「价格逆行多少爆仓」。")

# ---------------- 分腿 ----------------
print()
print("=" * 134)
print(" 分腿统计")
print("=" * 134)
print("  %-30s %-12s %6s %8s %13s %12s" % ("方案", "腿", "笔数", "胜率", "合计盈亏", "平均盈亏"))
print("-" * 134)


def legs(res):
    out = {}
    for t in res["trades"]:
        k = "多腿(现货)" if t["dir"] > 0 else "空腿(合约)"
        d = out.setdefault(k, {"n": 0, "win": 0, "pnl": 0.0})
        d["n"] += 1
        d["win"] += 1 if t["pnl"] > 0 else 0
        d["pnl"] += t["pnl"]
    return out


for lab, res in (("A. SMA50过滤 + 空头 1×", A),
                 ("B. SMA50过滤 + 空头 2×", Bb),
                 ("C. SMA50过滤 + 空头 3×", Cc)):
    for k, d in legs(res).items():
        print("  %-30s %-12s %5d %7.1f%% %+12.2f %+11.2f"
              % (lab, k, d["n"], d["win"] / d["n"] * 100, d["pnl"], d["pnl"] / d["n"]))
    print("-" * 134)

# ---------------- 逐年 ----------------
print()
print("=" * 134)
print(" 逐年收益（滚仓口径）")
print("=" * 134)


def yearly(eq):
    """每年收益 = 该年最后一个交易日的权益 / 该年第一个交易日的权益 - 1。"""
    out, cur_y, base_v, last_v = {}, None, None, None
    for i, b in enumerate(BARS):
        v = eq[i]
        if v is None:
            continue
        y = B.fdate(b["t"])[:4]
        if y != cur_y:
            if cur_y is not None and base_v:
                out[cur_y] = last_v / base_v - 1.0
            cur_y, base_v = y, v
        last_v = v
    if cur_y is not None and base_v:
        out[cur_y] = last_v / base_v - 1.0
    return out


ya, yb, yc = yearly(A["equity"]), yearly(Bb["equity"]), yearly(Cc["equity"])
YEARS = sorted(set(ya) | set(yb) | set(yc))
print("  %-8s %13s %13s %13s" % ("年份", "A 空头1×", "B 空头2×", "C 空头3×"))
print("-" * 134)
for y in YEARS:
    print("  %-8s %13s %13s %13s" % (y, pct(ya.get(y)), pct(yb.get(y)), pct(yc.get(y))))

# ---------------- 杠杆扫描 ----------------
print()
print("=" * 134)
print(" 空头腿杠杆全扫描：确认收益在哪个区间恒定、从哪里开始爆仓")
print("=" * 134)
print("  %-8s %11s %8s %9s %8s %6s %12s %12s"
      % ("空头腿", "终值倍数", "CAGR", "最大回撤", "Calmar", "爆仓", "保证金占权益", "爆仓线"))
print("-" * 134)
LEV = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0, 10.0, 20.0]
scan = []
for L in LEV:
    x = info(run(F50, L=L))
    scan.append((L, x))
    print("  %-8s %10.2f× %7.1f%% %8.1f%% %8.3f %6d %11.1f%% %11s"
          % ("%.1f×" % L, x["final"], x["cagr"] * 100, x["mdd"] * 100, x["calmar"],
             x["liq"], 100.0 / L, "+%.1f%%" % (100.0 / L)))
print("-" * 134)
print("  1.5× ~ 4× 收益完全一致（逐日相同）；1× 略低（保证金占满时的自动缩仓）；")
print("  5× 起开始爆仓，收益被削 —— 因为历史最深逆行 +22.89% 已经接近 5× 的 +20% 爆仓线。")
print("  币安实际只能设整数杠杆，所以可选范围是 1× / 2× / 3× / 4×。")

# ---------------- 空头尾部风险 ----------------
print()
print("=" * 134)
print(" 空头持仓期间「最大不利波动」经验分布（真实历史逐笔，与回测脚本同口径）")
print("=" * 134)
# 用 1× 的真实持仓集来测：1× 下历史最深逆行 22.89%（远低于 +100% 爆仓线），
# 全程零爆仓 —— 持仓笔数与杠杆无关（1×~4× 都是同一批），所以这就是策略真正经历的每一笔空头。
# （旧版这里用 100× 测，爆仓会把一笔持仓打断成好几笔，样本被虚增到 242 笔。）
r = run(F50, L=1.0)
adv = []
for t in r["trades"]:
    if t["dir"] < 0:
        hi = max(BARS[k]["high"] for k in range(t["entry_i"], min(t["exit_i"] + 1, len(BARS))))
        adv.append(hi / t["entry_px"] - 1.0)
adv.sort(reverse=True)
m = len(adv)
print("  实际空头笔数 %d 笔（零爆仓，持仓集与杠杆无关）" % m)
for p, name in ((0, "最坏一笔"), (int(m * 0.05), "前 5%"), (int(m * 0.10), "前 10%"),
                (int(m * 0.25), "前 25%"), (int(m * 0.50), "中位数")):
    print("  %-10s %+7.2f%%" % (name, adv[min(p, m - 1)] * 100))
print()
for L in (1.0, 2.0, 3.0):
    thr = 1.0 / L
    n = sum(1 for a in adv if a >= thr)
    print("  空头 %.1f×：爆仓需逆行 ≥ %5.1f%%，历史 %d/%d 笔触发（%.1f%%），安全余量 %6.2f pp"
          % (L, thr * 100, n, m, n / m * 100, (thr - adv[0]) * 100))

# ---------------- CSV ----------------
os.makedirs(OUT, exist_ok=True)


def w(name, headers, rows):
    with open(os.path.join(OUT, name), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(headers)
        wr.writerows(rows)
    print("  ✔ 写出 对比结果/%s" % name)


print()
w("01_杠杆总览.csv",
  ["方案", "空头腿", "终值倍数", "CAGR", "最大回撤", "Calmar", "最长水下(天)", "滚365最差",
   "爆仓次数", "保证金占权益", "爆仓线", "总成交笔数"],
  [["A. SMA50过滤 + 空头 1×  ★当前", "1×", "%.4f" % ia["final"], "%.4f" % ia["cagr"],
    "%.4f" % ia["mdd"], "%.4f" % ia["calmar"], ia["uw"],
    "" if ia["r365"] is None else "%.4f" % ia["r365"], ia["liq"], "1.0000", "1.0000", ia["n"]],
   ["B. SMA50过滤 + 空头 2×", "2×", "%.4f" % ib["final"], "%.4f" % ib["cagr"],
    "%.4f" % ib["mdd"], "%.4f" % ib["calmar"], ib["uw"],
    "" if ib["r365"] is None else "%.4f" % ib["r365"], ib["liq"], "0.5000", "0.5000", ib["n"]],
   ["C. SMA50过滤 + 空头 3×", "3×", "%.4f" % ic["final"], "%.4f" % ic["cagr"],
    "%.4f" % ic["mdd"], "%.4f" % ic["calmar"], ic["uw"],
    "" if ic["r365"] is None else "%.4f" % ic["r365"], ic["liq"], "0.3333", "0.3333", ic["n"]]])

w("02_分腿统计.csv", ["方案", "腿", "笔数", "胜率", "合计盈亏", "平均盈亏"],
  [[lab, k, d["n"], "%.4f" % (d["win"] / d["n"]), "%.2f" % d["pnl"], "%.2f" % (d["pnl"] / d["n"])]
   for lab, res in (("A. SMA50过滤 + 空头 1×", A),
                    ("B. SMA50过滤 + 空头 2×", Bb),
                    ("C. SMA50过滤 + 空头 3×", Cc))
   for k, d in legs(res).items()])

w("03_逐年收益.csv", ["年份", "A_空头1x", "B_空头2x", "C_空头3x"],
  [[y, "" if ya.get(y) is None else "%.4f" % ya[y],
    "" if yb.get(y) is None else "%.4f" % yb[y],
    "" if yc.get(y) is None else "%.4f" % yc[y]] for y in YEARS])

w("04_杠杆扫描.csv",
  ["空头腿", "终值倍数", "CAGR", "最大回撤", "Calmar", "爆仓次数", "保证金占权益", "爆仓线"],
  [["%.1f×" % L, "%.4f" % x["final"], "%.4f" % x["cagr"], "%.4f" % x["mdd"],
    "%.4f" % x["calmar"], x["liq"], "%.4f" % (1.0 / L), "%.4f" % (1.0 / L)]
   for L, x in scan])

w("05_空头尾部风险.csv", ["空头腿", "爆仓需逆行", "历史触发笔数", "总空头笔数", "触发占比", "安全余量"],
  [["%.1f×" % L, "%.4f" % (1.0 / L), sum(1 for a in adv if a >= 1.0 / L), m,
    "%.4f" % (sum(1 for a in adv if a >= 1.0 / L) / m), "%.4f" % (1.0 / L - adv[0])]
   for L in (1.0, 2.0, 3.0, 4.0, 5.0, 10.0)])

print()
print("完成。")
