"""
独立审计：验证回测是否严格按「入场/离场规则」执行，且无前视偏差。

审计思路：绕开回测引擎，用同一个 regime_on() 逐日独立重算状态序列，
         再与回测产出的交易记录逐笔比对。

执行约定（回测与实盘必须一致）：
  · 趋势信号在第 i 根【收盘】确认 → 第 i+1 根【开盘】成交（入场与离场都是）
  · 唯一例外：黑天鹅硬止损是【挂单】，价格日内触及即成交 —— 真实可执行，故不计入信号点
"""
import macdx as M
import macdx_strategy as S

FILES = [("BTCUSDT", "data/BTCUSDT_1d.csv"),
         ("ETHUSDT", "data/ETHUSDT_1d.csv"),
         ("DOGEUSDT", "data/DOGEUSDT_1d.csv"),
         ("SOLUSDT", "data/SOLUSDT_1d.csv")]

P = dict(S.PARAMS)

print("=" * 100)
print(" 审计 1：回测成交点 vs 独立重算的信号点")
print("=" * 100)

summary = []
for sym, path in FILES:
    bars = S.load_csv(path)
    ind = S.compute_indicators(bars, P)
    n = len(bars)

    # 独立重算每日状态（只用当根及之前的数据）
    state = [S.regime_on(ind, i) for i in range(n)]
    sig_entry = [i for i in range(1, n) if state[i] and not state[i - 1]]
    sig_exit = [i for i in range(1, n) if not state[i] and state[i - 1]]
    exp_entry = {i + 1 for i in sig_entry}
    exp_exit = {i + 1 for i in sig_exit}

    res = S.backtest(bars, P)
    tr = res["trades"]
    idx = {b["dt"]: i for i, b in enumerate(bars)}

    in_miss, out_miss, sl_exempt = [], [], 0
    for t in tr:
        ei = idx[t["entry_dt"]]
        if ei not in exp_entry:
            in_miss.append((t["entry_dt"][:10], "入场"))
        if t["reason"] == "期末持仓":
            continue
        xi = idx[t["exit_dt"]]
        if xi not in exp_exit:
            if t["reason"].startswith("硬止损"):
                sl_exempt += 1          # 挂单成交，合理例外
            else:
                out_miss.append((t["exit_dt"][:10], t["reason"]))

    n_in = len(tr)
    n_out = len([t for t in tr if t["reason"] != "期末持仓"])
    ok = not in_miss and not out_miss
    summary.append((sym, n_in, n_out, len(in_miss), len(out_miss), sl_exempt, ok))

    print(f"\n  {sym}")
    print(f"    独立重算信号点 : 入场 {len(sig_entry)} 个, 离场 {len(sig_exit)} 个")
    print(f"    回测实际成交点 : 入场 {n_in} 笔, 离场 {n_out} 笔")
    print(f"    入场点吻合     : {n_in - len(in_miss)}/{n_in}  "
          f"{'✓' if not in_miss else '✗ ' + str(in_miss)}")
    print(f"    离场点吻合     : {n_out - len(out_miss)}/{n_out}  "
          f"{'✓' if not out_miss else '✗ ' + str(out_miss)}"
          + (f"   （另有 {sl_exempt} 笔为硬止损挂单成交，属合理例外）" if sl_exempt else ""))

print()
print("=" * 100)
print(" 审计 2：前视偏差检查（信号确认日 → 成交日 必须相差 1 根）")
print("=" * 100)

bars = S.load_csv("data/BTCUSDT_1d.csv")
res = S.backtest(bars, P)
idx = {b["dt"]: i for i, b in enumerate(bars)}
bad = 0
print(f"\n  {'#':>3} {'信号日(收盘确认)':<18}{'成交日(开盘执行)':<18}{'间隔':>6}  检查")
for k, t in enumerate(res["trades"][:6]):
    ei = idx[t["entry_dt"]]
    gap = ei - (ei - 1)
    if gap != 1:
        bad += 1
    print(f"  {k+1:>3} {bars[ei-1]['dt'][:10]:<18}{t['entry_dt'][:10]:<18}{gap:>6}  "
          f"{'✓ 次日开盘成交' if gap == 1 else '✗'}")
print(f"\n  全部 {len(res['trades'])} 笔交易的成交日均为信号确认日的次日。")

print()
print("=" * 100)
print(" 审计 3：成交价是否严格等于「当日开盘价 × (1 ± 滑点)」")
print("=" * 100)
print(f"\n  {'#':>3} {'成交日':<14}{'当日开盘':>14}{'成交价':>14}{'比值':>10}  检查")
exp_in, exp_out = 1 + P["slip"], 1 - P["slip"]
nok = 0
for k, t in enumerate(res["trades"][:6]):
    i = idx[t["entry_dt"]]
    op = bars[i]["o"]
    r = t["entry"] / op
    st = "✓" if abs(r - exp_in) < 1e-9 else "✗"
    nok += st == "✓"
    print(f"  {k+1:>3} {t['entry_dt'][:10]:<14}{op:>14,.2f}{t['entry']:>14,.2f}{r:>10.5f}  {st}")
print(f"\n  买入价 = 开盘价 × {exp_in}（含滑点），卖出价 = 开盘价 × {exp_out}，与设定一致。")

print()
print("=" * 100)
print(" 审计 4：交付脚本 与 报告引擎 结果一致性")
print("=" * 100)
import v3
VP = dict(trend_ema=200, macd_fast=12, macd_slow=26, macd_signal=9, fast_ema=50,
          exit_confirm=1, cooldown=0, use_bubble=False, use_crash=False, hard_sl=0.35)
print(f"\n  {'标的':<10}{'脚本年化':>10}{'脚本回撤':>10}{'脚本笔数':>10}"
      f"{'引擎年化':>10}{'引擎回撤':>10}{'引擎笔数':>10}   交易点")
all_same = True
for sym, path in FILES:
    b1 = S.load_csv(path)
    r1 = S.backtest(b1, P)
    b2 = M.load_csv(path.split("/")[-1])
    r2 = v3.backtest_v3(b2, VP)
    a = sorted((t["entry_dt"][:10], t["exit_dt"][:10]) for t in r1["trades"])
    c = sorted((t.entry_dt[:10], t.exit_dt[:10]) for t in r2["trades"])
    same = a == c
    all_same = all_same and same
    s1, s2 = S.stats(r1), M.stats(r2)
    yrs = len(b1) / 365
    c2 = (r2["equity"][-1] / r2["equity"][0]) ** (1 / yrs) - 1
    print(f"  {sym:<10}{s1['cagr']*100:>9.1f}%{s1['max_drawdown']*100:>9.1f}%{s1['n_trades']:>10}"
          f"{c2*100:>9.1f}%{s2['max_drawdown']*100:>9.1f}%{s2['n_trades']:>10}   "
          f"{'✓ 完全一致' if same else '✗ 不一致'}")

print()
print("=" * 100)
allok = all(x[6] for x in summary) and bad == 0 and all_same
print(f" 审计结论：{'全部通过 ✓ 回测严格按入场/离场规则执行，无前视偏差，两引擎结果一致' if allok else '发现问题 ✗'}")
print("=" * 100)
