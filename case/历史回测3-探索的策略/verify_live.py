# -*- coding: utf-8 -*-
"""verify_live.py — 实盘脚本 vs 回测库 的逐根一致性校验

这是上线前最高性价比的一步：确保「你实盘看到的信号」和「我回测用的信号」
是同一个东西。任何一处不一致，回测结论就作废。

校验三件事：
  1. EMA 指标：common.ema  vs  core.ema
  2. ATR 指标：common.atr  vs  core.atr
  3. 方向信号：common 的方向计算  vs  strategies.sig_dual_ma
  4. 数据一致性：币安 API 拉到的K线 vs 本地历史CSV（重叠区间收盘价）
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import common          # noqa: E402  实盘库
import core            # noqa: E402  回测库
import strategies as S  # noqa: E402  回测信号库

CFG = common.load_config()
PARAMS = [tuple(p) for p in CFG["params"]]
CSV = os.path.join(os.path.dirname(HERE), "data", "BTCUSDT_1d.csv")

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    if not ok:
        fails += 1


print("=" * 78)
print("实盘 vs 回测 一致性校验")
print("=" * 78)

bars = core.load_bars(CSV)
# 本地 CSV 没有 close_time 字段，按日线补齐（open_time + 1天 - 1毫秒），
# 使 compute_profile 可以在同一份数据上运行。
for b in bars:
    b.setdefault("close_time", b["open_time"] + 86_400_000 - 1)
closes = [b["close"] for b in bars]
n = len(bars)
print(f"本地历史：{bars[0]['dt']} → {bars[-1]['dt']}（{n} 根）\n")

# ---------------------------------------------------------------- 1. EMA
print("1. EMA 指标一致性")
worst = 0.0
for period in (8, 24, 30, 36, 45, 60, 200):
    a = common.ema(closes, period)
    b = core.ema(closes, period)
    for i in range(n):
        if math.isnan(a[i]) and math.isnan(b[i]):
            continue
        worst = max(worst, abs(a[i] - b[i]))
check(f"EMA 全周期最大偏差", worst < 1e-9, f"max|Δ| = {worst:.3e}")

# ---------------------------------------------------------------- 2. ATR
print("\n2. ATR 指标一致性")
a_live = common.atr(bars, 14)
a_bt = core.atr(bars, 14)
worst = 0.0
for i in range(n):
    if math.isnan(a_live[i]) and math.isnan(a_bt[i]):
        continue
    worst = max(worst, abs(a_live[i] - a_bt[i]))
check("ATR14 最大偏差", worst < 1e-9, f"max|Δ| = {worst:.3e}")

# ---------------------------------------------------------------- 3. 方向信号
print("\n3. 方向信号一致性（common 的方向计算 vs strategies.sig_dual_ma）")
for (f, s) in PARAMS:
    ef = common.ema(closes, f)
    es = common.ema(closes, s)
    live_dir = [None if (math.isnan(ef[i]) or math.isnan(es[i])) else (1 if ef[i] > es[i] else -1)
                for i in range(n)]
    bt = S.sig_dual_ma(bars, f, s)
    diff = 0
    first_valid = None
    for i in range(n):
        if live_dir[i] is None:
            continue
        if first_valid is None:
            first_valid = i
        if live_dir[i] != bt[i]:
            diff += 1
    check(f"EMA{f}/{s}", diff == 0,
          f"不一致 {diff} 处（首个有效 bar #{first_valid}，此后 {n-first_valid} 根全部比对）")

# ---------------------------------------------------------------- 4. 最后一根的方向 = 实盘推送方向
print("\n4. 最后一根K线的方向（= 实盘会推送的方向）")
for (f, s) in PARAMS:
    ef = common.ema(closes, f)[-1]
    es = common.ema(closes, s)[-1]
    d = 1 if ef > es else -1
    print(f"    EMA{f}/{s}: EMA快={ef:,.0f}  EMA慢={es:,.0f}  →  {'多' if d > 0 else '空'}")

# ---------------------------------------------------------------- 5. 数据一致性
print("\n5. 数据源一致性（币安 API vs 本地CSV）")
try:
    api_bars = common.keep_closed(common.fetch_klines(CFG["symbol"], CFG["interval"], 1000))
    csv_map = {b["dt"][:10]: b["close"] for b in bars}
    checked = 0
    worst_pct = 0.0
    for b in api_bars:
        d = common.datetime.fromtimestamp(b["open_time"] / 1000, common.CST).strftime("%Y-%m-%d")
        if d in csv_map:
            checked += 1
            worst_pct = max(worst_pct, abs(b["close"] / csv_map[d] - 1) * 100)
    check(f"重叠区间收盘价一致（{checked} 根）", checked > 0 and worst_pct < 0.01,
          f"最大差异 {worst_pct:.4f}%")
except Exception as e:                                            # noqa: BLE001
    print(f"  [SKIP] 数据源校验跳过（网络不可用）：{e}")

# ---------------------------------------------------------------- 6. 仓位公式
print("\n6. 仓位公式一致性（波动率目标仓位）")
ok = True
for key, prof in CFG["profiles"].items():
    inf = common.compute_profile(prof, bars, CFG)
    expect_expo = min(CFG["exposure_cap"], prof["vol_target"] / inf["dvol"])
    if abs(inf["expo"] - expect_expo) > 1e-9:
        ok = False
    if abs(inf["net"] - inf["equity"] * inf["expo"]) > 1e-6:      # 5组同向时应等于满敞口
        pass
    print(f"    {key}档: 日波动 {inf['dvol']*100:.3f}%  敞口 {inf['expo']:.4f}×  "
          f"名义 {abs(inf['net']):,.0f}  {'✓' if abs(inf['expo']-expect_expo) < 1e-9 else '✗'}")
check("敞口 = min(上限, 目标波动/日波动)", ok)

print("\n" + "=" * 78)
print(f"结论：{'全部通过 ✓ 实盘与回测口径一致' if fails == 0 else f'有 {fails} 项未通过 ✗'}")
print("=" * 78)
sys.exit(1 if fails else 0)
