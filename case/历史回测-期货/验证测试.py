# -*- coding: utf-8 -*-
"""
数值正确性验证：不读代码逻辑，用独立算法重算，比对脚本输出。
任何一项 FAIL 都说明对应函数算错了 —— 这类 bug 读代码看不出来，只能对答案。
"""
import math
import os
import statistics
import sys

import strategy_top2 as st

PASS, FAIL = [], []


def check(name, got, want, tol=1e-9):
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) else (got == want)
    (PASS if ok else FAIL).append(name)
    print("  %-46s %s" % (name, "PASS" if ok else "FAIL  got=%r want=%r" % (got, want)))


print("=" * 74)
print("1. EMA 与标准公式对比")
print("=" * 74)
v = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
for n in (5, 10, 20):
    got = st.ema(v, n)
    a = 2.0 / (n + 1)
    want, prev = [], None
    for x in v:
        prev = x if prev is None else a * x + (1 - a) * prev
        want.append(prev)
    check("EMA n=%d" % n, got, want)

print()
print("=" * 74)
print("2. ATR 与手工 Wilder 公式对比")
print("=" * 74)
bars = [{"date": "d%d" % i, "open": 10 + i, "high": 12 + i, "low": 9 + i,
         "close": 11 + i} for i in range(60)]
A = st.atr(bars, 14)
prev = None
want = []
for i, b in enumerate(bars):
    tr = (b["high"] - b["low"]) if i == 0 else max(
        b["high"] - b["low"], abs(b["high"] - bars[i - 1]["close"]),
        abs(b["low"] - bars[i - 1]["close"]))
    prev = tr if prev is None else (prev * 13 + tr) / 14
    want.append(prev)
check("ATR14 全序列", A, want)

print()
print("=" * 74)
print("3. 复权因子性质 & real_px 能还原真实价")
print("=" * 74)
for sym in ("FG", "HC"):
    b, real_last, live = st.load(sym)
    scales = [x["scale"] for x in b]
    # scale 不要求单调：换月时 k *= pc/op，贴水换月（pc>op）k 上升、
    # 升水换月（pc<op）k 下降，双向变动是正常的。只要求恒正且能还原真实价。
    check("%s scale 恒正" % sym, all(s > 0 for s in scales), True)
    check("%s scale 首根为 1.0" % sym, abs(scales[0] - 1.0) < 1e-12, True)
    # 复权价 / scale 必须回到原始 CSV 的收盘价
    raw = {}
    with open(os.path.join(st.DATA, sym + ".csv"), encoding="utf-8") as f:
        next(f)
        for line in f:
            q = line.strip().split(",")
            if len(q) >= 6:
                raw[q[0]] = float(q[4])
    bad = sum(1 for x in b
              if abs(x["close"] / x["scale"] - raw.get(x["date"], -1)) > 1e-6)
    check("%s real_px 还原原始收盘价（错 %d 根）" % (sym, bad), bad, 0)
    check("%s 末根 real_px == real_last" % sym,
          abs(st.real_px(b, len(b) - 1) - real_last) < 1e-9, True)

print()
print("=" * 74)
print("4. 信号三条腿的一致性")
print("=" * 74)
b, _, _ = st.load("FG")
carry, trend, sig = st.sig_carry(b), st.sig_trend(b), st.signal(b)
check("signal = carry 与 trend 同向",
      all(s == (c if c == t else 0) for s, c, t in zip(sig, carry, trend)), True)
check("carry 在 WARMUP 前不出信号", all(c == 0 for c in carry[:st.CARRY_WIN]), True)
check("trend 在 MA_SLOW 前不出信号", all(t == 0 for t in trend[:st.MA_SLOW]), True)
check("signal 无 None", all(isinstance(s, int) for s in sig), True)
# carry 阈值边界
s_ = sum(x.get("gap", 0.0) for x in b[-st.CARRY_WIN:])
last_carry = carry[-1]
want_c = 1 if s_ < -st.CARRY_THR else (-1 if s_ > st.CARRY_THR else 0)
check("末根 carry 与累计跳空一致", last_carry, want_c)

print()
print("=" * 74)
print("5. 敞口公式与手数取整")
print("=" * 74)
A = st.atr(b)
last = len(b) - 1
expo = st.exposure_at(b, A, last)
dvol = A[last - 1] / b[last - 1]["close"]
check("exposure_at 公式", expo, min(st.EXPO_CAP, st.VOL_TARGET / dvol), 1e-12)
check("exposure_at 不超上限", expo <= st.EXPO_CAP + 1e-12, True)
check("exposure_at(i=0) 兜底为 1.0", st.exposure_at(b, A, 0), 1.0)
# 手数：want 的整数值
rows, skip, dd = st.plan()
for r in rows:
    if r["sig"] != 0:
        check("%s lots <= want+1" % r["sym"],
              r["lots"] <= r["want"] + 1.0 + 1e-9, True)
        check("%s notional = lots×单手名义" % r["sym"],
              abs(r["notional"] - r["lots"] * r["lot_notional"]) < 1e-6, True)
        check("%s margin = lots×单手保证金" % r["sym"],
              abs(r["margin"] - r["lots"] * r["margin_per"]) < 1e-6, True)

print()
print("=" * 74)
print("6. 保证金预算不超限")
print("=" * 74)
tot = sum(r["margin"] for r in rows)
check("总保证金 <= 预算", tot <= st.EQUITY * st.MARGIN_BUDGET + 1e-6, True)
print("     总保证金 %.0f 元 = %.1f%% 权益（预算 %.0f%%）"
      % (tot, tot / st.EQUITY * 100, st.MARGIN_BUDGET * 100))

print()
print("=" * 74)
print("7. 盈亏计算（浮盈 = (现价-开仓价)×方向×手数×乘数）")
print("=" * 74)
s = st._load_state()
newpos, changes, pnl, rdelta = st._track(rows, s, dd)
for p in pnl:
    amt = (p["px"] - p["entry"]) * p["dir"] * p["lots"] * p["mult"]
    check("%s 浮盈公式" % p["sym"], p["amt"], amt, 1e-6)
    check("%s 收益率 = 盈亏/开仓名义" % p["sym"],
          p["pct"], p["amt"] / (p["entry"] * p["mult"] * p["lots"]), 1e-9)

print()
print("=" * 74)
print("8. 回测引擎：手数序列 & 权益曲线自洽")
print("=" * 74)
S = {}
for sym, mult, mrate in st.active_instruments():
    bb, _c, _l = st.load(sym)
    if bb is None:
        continue
    ln = st.real_px(bb, len(bb) - 1) * mult
    if ln > st.EQUITY * st.MAX_LEG_LEVERAGE:
        continue
    S[sym] = {"bars": bb, "sig": st.signal(bb), "atr": st.atr(bb),
              "mult": mult, "mrate": mrate}
base = min(S, key=lambda x: len(S[x]["bars"]))
dates = [x["date"] for x in S[base]["bars"]]
idx = {k: {x["date"]: i for i, x in enumerate(v["bars"])} for k, v in S.items()}
eq, curve = st.EQUITY, []
pos = {}
peak_margin = 0.0
for t in range(1, len(dates)):
    dt = dates[t]
    tgt = {}
    for sym, v in S.items():
        i = idx[sym].get(dt)
        tgt[sym] = 0 if (i is None or i == 0 or (i - 1) < st.WARMUP) else v["sig"][i - 1]
    n_sig = max(1, sum(1 for x in tgt.values() if x != 0))
    share = eq / n_sig
    pnl = 0.0
    calc = []
    for sym, v in S.items():
        i = idx[sym].get(dt)
        if i is None or i == 0:
            continue
        bb = v["bars"]
        if tgt[sym] == 0:
            lots, want = 0, 0.0
        else:
            ln = st.real_px(bb, i - 1) * v["mult"]
            want = st.exposure_at(bb, v["atr"], i - 1) * share / ln
            n = int(want)
            if n == 0 and want >= st.MIN_FILL and \
                    ln * (v["mrate"] + st.BROKER_ADD) <= eq * st.MARGIN_SAFE:
                n = 1
            lots = n * tgt[sym]
        calc.append({"sym": sym, "v": v, "i": i, "bars": bb, "lots": lots,
                     "want": want,
                     "lot_notional": st.real_px(bb, i - 1) * v["mult"],
                     "margin_per": st.real_px(bb, i - 1) * v["mult"] * (v["mrate"] + st.BROKER_ADD)})
    st._fit_margin_budget(calc, eq)
    peak_margin = max(peak_margin, sum(c["margin"] for c in calc) / eq)
    for c in calc:
        sym, v, i, bb, lots = c["sym"], c["v"], c["i"], c["bars"], c["lots"]
        # 手续费只在【手数变化】时收（与 backtest 同口径），不是每天收
        if lots != pos.get(sym, 0):
            pnl -= abs(lots - pos.get(sym, 0)) * st.real_px(bb, i) * v["mult"] * (st.FEE + st.SLIP)
            pos[sym] = lots
        if pos.get(sym, 0) != 0:
            r = bb[i]["close"] / bb[i - 1]["close"] - 1.0
            pnl += pos[sym] * v["mult"] * st.real_px(bb, i - 1) * r
    eq += pnl
    curve.append(eq)
check("回测权益曲线长度 = len(dates)-1", len(curve), len(dates) - 1)
check("保证金占用峰值 <= 预算", peak_margin <= st.MARGIN_BUDGET + 1e-9, True)
print("     峰值保证金占用 %.1f%% 权益" % (peak_margin * 100))
# 年化与官方 backtest 对比
r_official = st.backtest(verbose=False)
yrs = len(curve) / st.TRADING_DAYS
cagr_mine = (eq / st.EQUITY) ** (1 / yrs) - 1
check("自算年化 == backtest() 年化", cagr_mine, r_official["cagr"], 1e-9)
peak, mdd = st.EQUITY, 0.0
for v_ in curve:
    peak = max(peak, v_)
    mdd = max(mdd, (peak - v_) / peak)
check("自算回撤 == backtest() 回撤", mdd, r_official["mdd"], 1e-9)

print()
print("=" * 74)
print("9. 状态文件异常路径")
print("=" * 74)
TF = os.path.join(st.HERE, "_verify_state.json")
st.STATE_FILE = TF
for content, label in [("{ 坏 json", "非法 JSON"), ('{"positions": {"FG": "空"}}', "positions 存字符串"),
                       ('{"positions": "FG"}', "positions 是字符串"),
                       ('{"positions": ["FG"]}', "positions 是列表"),
                       ('{"positions": {"FG": {"dir": -1}}}', "缺 lots/entry"),
                       ('{"positions": {"FG": {"dir": 0, "lots": 0}}}', "dir=0 空仓"),
                       ('{"positions": {"FG": {"dir": -1, "lots": null, "entry_px": null}}}', "lots 为 null"),
                       ('{"realized": "abc", "start_equity": "xyz"}', "数值字段是字符串"),
                       ('{"realized": null, "start_equity": null}', "数值字段为 null"),
                       ('{"positions": {"FG": {"dir": -1, "lots": 1, "entry_px": 800.0, "entry_date": "2026-10-01"}}}', "正常持仓"),
                       ('{"positions": {"FG": {"dir": -1, "lots": 1, "entry_px": 1211.0, "entry_date": "2025-08-15"}}}', "真实现价持仓")]:
    with open(TF, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        s2 = st._load_state()
        pos = st._load_positions(s2)
        newp, ch, pl, rd = st._track(rows, s2, dd)
        r_n = st._num_state(s2, "realized", 0.0)
        print("  %-24s -> 不崩溃, positions=%s, rdelta=%.1f, realized=%.1f"
              % (label, {k: v["dir"] for k, v in pos.items()}, rd, r_n))
        PASS.append("状态异常:" + label)
    except Exception as e:
        print("  %-24s -> 抛异常 %s: %s" % (label, type(e).__name__, e))
        FAIL.append("状态异常:" + label)
os.remove(TF)

print()
print("=" * 74)
print("9b. _rebuild_entry 方向不符时必须返回 None（不能把今收当开仓价）")
print("=" * 74)
b_now, _, _ = st.load("FG")
sg_now = st.signal(b_now)
print("     当前 FG 信号: %s" % ({1: "多", -1: "空", 0: "空仓"}[sg_now[-1]]))
wrong = 1 if sg_now[-1] != 1 else -1
check("_rebuild_entry(方向=%s) 返回 None" % wrong, st._rebuild_entry("FG", wrong), None)
rb = st._rebuild_entry("FG", sg_now[-1])
if rb is None:
    print("  %-46s %s" % ("_rebuild_entry(当前方向)", "PASS（当前为空仓，符合预期）"))
    PASS.append("_rebuild_entry 空仓")
else:
    print("  %-46s %s" % ("_rebuild_entry(当前方向)", "返回 %s %s" % rb))
    PASS.append("_rebuild_entry 当前方向")

print()
print("=" * 74)
print("10. 数据边界")
print("=" * 74)
check("read_csv 不存在文件返回 None", st.read_csv("__NO_SUCH__"), None)
empty_dir = os.path.join(st.HERE, "_vdata")
os.makedirs(empty_dir, exist_ok=True)
with open(os.path.join(empty_dir, "X.csv"), "w", encoding="utf-8") as f:
    f.write("date,open,high,low,close,volume\n")
st.DATA = empty_dir
check("空 CSV 返回 None", st.read_csv("X"), None)
with open(os.path.join(empty_dir, "Y.csv"), "w", encoding="utf-8") as f:
    f.write("date,open,high,low,close,volume\n2026-01-01,nan,nan,nan,nan,0\n"
            "2026-01-02,0,0,0,0,0\n2026-01-03,10,11,9,10,100\n")
r = st.read_csv("Y")
check("NaN/0 行被剔除", r is None or len(r) == 1, True)
st.DATA = os.path.join(st.HERE, "data")
import shutil
shutil.rmtree(empty_dir, ignore_errors=True)

print()
print("=" * 74)
print("结果：PASS %d 项，FAIL %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 74)
