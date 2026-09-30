"""
全市场回测：用 MACD-X 策略跑币安所有 USDT 现货交易对。

资金模式：起始 1000 U，每笔【梭哈】（全部当前资金买入，卖出后继续全仓下一笔）= 复利全仓。
"""
from __future__ import annotations
import csv, glob, json, math, os, statistics, time
import macdx_strategy as S

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT_CSV = os.path.join(HERE, "全市场回测结果.csv")
OUT_HTML = os.path.join(HERE, "全市场回测报告.html")

INIT_CAPITAL = 1000.0
P = dict(S.PARAMS)
P["init_capital"] = INIT_CAPITAL
P["capital_frac"] = 1.0          # 梭哈
MIN_BARS = 800                   # 至少约 2.2 年数据

files = sorted(glob.glob(os.path.join(DATA, "*_1d.csv")))
print(f"扫描到 {len(files)} 个数据文件")

rows = []
t0 = time.time()
skipped = []

for k, path in enumerate(files, 1):
    sym = os.path.basename(path).replace("_1d.csv", "")
    try:
        bars = S.load_csv(path)
    except Exception as e:
        skipped.append((sym, str(e)[:40]))
        continue
    if len(bars) < MIN_BARS:
        skipped.append((sym, f"仅{len(bars)}根"))
        continue

    r = S.backtest(bars, P)
    s = S.stats(r)
    if not s:
        skipped.append((sym, "无结果"))
        continue

    bh = S.buy_hold(bars, P)
    sb = S.stats(bh)
    yrs = len(bars) / 365.0
    s["cagr"] = (r["equity"][-1] / r["equity"][0]) ** (1 / yrs) - 1
    sb["cagr"] = (bh["equity"][-1] / bh["equity"][0]) ** (1 / yrs) - 1

    rows.append({
        "symbol": sym,
        "bars": len(bars),
        "years": yrs,
        "start": bars[0]["dt"][:10],
        "end": bars[-1]["dt"][:10],
        "final": r["equity"][-1],
        "ret": s["total_return"],
        "cagr": s["cagr"],
        "mdd": s["max_drawdown"],
        "calmar": s["cagr"] / s["max_drawdown"] if s["max_drawdown"] > 0 else 0.0,
        "trades": s["n_trades"],
        "winrate": s["winrate"],
        "pf": s["profit_factor"],
        "exposure": s["exposure"],
        "bh_final": bh["equity"][-1],
        "bh_ret": sb["total_return"],
        "bh_cagr": sb["cagr"],
        "bh_mdd": sb["max_drawdown"],
        "beat": s["total_return"] > sb["total_return"],
        "beat_cagr": s["cagr"] > sb["cagr"],
    })

    if k % 100 == 0:
        print(f"  {k}/{len(files)}  有效 {len(rows)}  用时 {(time.time()-t0)/60:.1f} 分", flush=True)

print(f"\n回测完成：有效 {len(rows)} 个，跳过 {len(skipped)} 个，"
      f"用时 {(time.time()-t0)/60:.1f} 分")

if not rows:
    print("没有可用数据")
    raise SystemExit(1)

# ── 写 CSV ──
cols = list(rows[0].keys())
with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
print("已写入", OUT_CSV)

# ── 汇总统计 ──
rets = [r["ret"] for r in rows]
cagr = [r["cagr"] for r in rows]
mdd = [r["mdd"] for r in rows]
bh_ret = [r["bh_ret"] for r in rows]
bh_mdd = [r["bh_mdd"] for r in rows]
n = len(rows)
profitable = sum(1 for r in rows if r["ret"] > 0)
beat = sum(1 for r in rows if r["beat"])
beat_cagr = sum(1 for r in rows if r["beat_cagr"])
worse_mdd = sum(1 for r in rows if r["mdd"] < r["bh_mdd"])

print("\n" + "=" * 76)
print(f" 全市场汇总（{n} 个交易对，起始 {INIT_CAPITAL:.0f} U，梭哈复利）")
print("=" * 76)
print(f"  策略盈利的币     : {profitable}/{n}  ({profitable/n*100:.1f}%)")
print(f"  买入持有盈利的币 : {sum(1 for x in bh_ret if x>0)}/{n}  ({sum(1 for x in bh_ret if x>0)/n*100:.1f}%)")
print(f"  策略总收益 > 持有: {beat}/{n}  ({beat/n*100:.1f}%)")
print(f"  策略年化   > 持有: {beat_cagr}/{n}  ({beat_cagr/n*100:.1f}%)")
print(f"  策略回撤   < 持有: {worse_mdd}/{n}  ({worse_mdd/n*100:.1f}%)")
print()
print(f"  策略 收益中位数  : {statistics.median(rets)*100:+.1f}%    平均 {statistics.mean(rets)*100:+.1f}%")
print(f"  持有 收益中位数  : {statistics.median(bh_ret)*100:+.1f}%    平均 {statistics.mean(bh_ret)*100:+.1f}%")
print(f"  策略 年化中位数  : {statistics.median(cagr)*100:+.1f}%")
print(f"  持有 年化中位数  : {statistics.median([r['bh_cagr'] for r in rows])*100:+.1f}%")
print(f"  策略 回撤中位数  : {statistics.median(mdd)*100:.1f}%")
print(f"  持有 回撤中位数  : {statistics.median(bh_mdd)*100:.1f}%")

best = sorted(rows, key=lambda r: -r["ret"])[:12]
worst = sorted(rows, key=lambda r: r["ret"])[:12]
print("\n  表现最好的 12 个：")
for r in best:
    print(f"    {r['symbol']:<12}{r['years']:.1f}年  策略{r['ret']*100:>+9.0f}%  持有{r['bh_ret']*100:>+9.0f}%  "
          f"{r['trades']:>3}笔")
print("\n  表现最差的 12 个：")
for r in worst:
    print(f"    {r['symbol']:<12}{r['years']:.1f}年  策略{r['ret']*100:>+9.0f}%  持有{r['bh_ret']*100:>+9.0f}%  "
          f"{r['trades']:>3}笔")

# 按上市年份分组
byyear = {}
for r in rows:
    y = r["start"][:4]
    byyear.setdefault(y, []).append(r)
print("\n  按上市年份分组（策略盈利比例 / 跑赢持有比例 / 收益中位数）：")
for y in sorted(byyear):
    g = byyear[y]
    p1 = sum(1 for r in g if r["ret"] > 0) / len(g)
    p2 = sum(1 for r in g if r["beat"]) / len(g)
    print(f"    {y} 上市 ({len(g):>3}个): 盈利 {p1*100:>4.0f}%  跑赢持有 {p2*100:>4.0f}%  "
          f"中位收益 {statistics.median([r['ret'] for r in g])*100:>+7.1f}%")

json.dump({"n": n, "profitable": profitable, "beat": beat,
           "median_ret": statistics.median(rets),
           "median_bh": statistics.median(bh_ret),
           "median_cagr": statistics.median(cagr),
           "median_mdd": statistics.median(mdd),
           "median_bh_mdd": statistics.median(bh_mdd),
           "beat_cagr": beat_cagr, "worse_mdd": worse_mdd},
          open(os.path.join(HERE, "all_summary.json"), "w"))
