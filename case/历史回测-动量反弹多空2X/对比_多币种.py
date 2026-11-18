# -*- coding: utf-8 -*-
"""
多币种对比：把同一套策略（EMA10/30 + SMA50 只约束多头 + 波动率目标 3% + 多头现货/空头合约 1×）
原封不动地跑在各类币上，回答一个问题：**这个策略是不是只适合 BTC？**

为什么要分两组（关键）：
  组 A「今日头部」= 今天市值靠前的币。用它选标的 = 用后视镜选股，必然高估。
  组 B「昔日头部」= 2017-2018 年市值前 20、今天大多已经衰落甚至退市的币。
  只有把 B 组也算进来，才能知道「2017 年随机挑一个头部币跑这套策略」的真实结果。

三个口径：
  ① 各自全程      —— 每个币从自己上市第一天跑起
  ② 同窗口配对    —— 每个币从自己上市日算，BTC 也切到同一天（唯一公平的横向比较）
  ③ 分散组合      —— 等权跑 N 个币 vs 只跑 BTC，看分散能不能改善风险调整后收益

不做的事（避免自欺）：不按币种调参数、不按币种调波动率目标。
"""
import csv
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btc_strategy as M

CACHE = os.path.join(HERE, "data_多币种")
OUT = os.path.join(HERE, "多币种对比")

# 组 A：今天市值靠前、币安有足够历史的标的
GROUP_A = [
    ("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("BNBUSDT", "BNB"), ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"), ("ADAUSDT", "ADA"), ("DOGEUSDT", "DOGE"), ("TRXUSDT", "TRX"),
    ("LINKUSDT", "LINK"), ("AVAXUSDT", "AVAX"), ("DOTUSDT", "DOT"), ("LTCUSDT", "LTC"),
    ("BCHUSDT", "BCH"), ("UNIUSDT", "UNI"), ("NEARUSDT", "NEAR"), ("ATOMUSDT", "ATOM"),
    ("ETCUSDT", "ETC"), ("XLMUSDT", "XLM"), ("APTUSDT", "APT"), ("ARBUSDT", "ARB"),
    ("TONUSDT", "TON"),
]

# 组 B：2017-2018 年市值前 20、今天已大幅衰落或已退市（含 5 个已下架）
GROUP_B = [
    ("NEOUSDT", "NEO"), ("EOSUSDT", "EOS"), ("IOTAUSDT", "IOTA"), ("QTUMUSDT", "QTUM"),
    ("ONTUSDT", "ONT"), ("VETUSDT", "VET"), ("ALGOUSDT", "ALGO"), ("ZILUSDT", "ZIL"),
    ("ONEUSDT", "ONE"), ("FILUSDT", "FIL"), ("ICPUSDT", "ICP"), ("SHIBUSDT", "SHIB"),
    ("DASHUSDT", "DASH"), ("ZECUSDT", "ZEC"), ("THETAUSDT", "THETA"), ("MANAUSDT", "MANA"),
    ("SANDUSDT", "SAND"), ("AXSUSDT", "AXS"), ("OMGUSDT", "OMG*"), ("XEMUSDT", "XEM*"),
    ("BTGUSDT", "BTG*"), ("FTMUSDT", "FTM*"),
]
# 带 * 的是已经下架/停止交易的（数据止于退市日）

MIN_BARS = M.WARMUP + 120


def log(msg=""):
    print(msg)
    sys.stdout.flush()


def get_bars(sym, refresh=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_1d.csv" % sym)
    if refresh or not os.path.exists(path):
        rows = M.fetch_klines(sym, M.FETCH_START)
        if not rows:
            return []
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time"])
            for r in rows:
                w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
        time.sleep(0.15)
    bars = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            bars.append({"t": int(r["open_time"]), "open": float(r["open"]),
                         "high": float(r["high"]), "low": float(r["low"]),
                         "close": float(r["close"]), "vol": float(r["volume"])})
    now = time.time() * 1000
    return [b for b in bars if b["t"] + M.DAY_MS <= now]


def bh_equity(bars):
    units = M.CAPITAL / (bars[0]["close"] * (1 + M.SLIP))
    eq = [units * b["close"] for b in bars]
    eq[-1] = units * bars[-1]["close"] * (1 - M.SLIP)
    return eq


def summarize(bars, allow_short=True, vt=None, funding_d=None, slip=None):
    sig = M.regime_long(bars, M.sig_ema_cross(bars))
    kw = {}
    if vt is not None:
        kw["vt"] = vt
    if funding_d is not None:
        kw["funding_d"] = funding_d
    if slip is not None:
        kw["slip"] = slip
    res = M.backtest_hybrid(bars, sig, allow_short=allow_short, **kw)
    st = res["stats"]
    bst = M.stats(bh_equity(bars), None)
    a = M.atr(bars, 14)
    expo = []
    for i in range(M.WARMUP, len(bars)):
        if sig[i - 1] == 0:
            continue
        dvol = a[i - 1] / bars[i - 1]["close"] if not math.isnan(a[i - 1]) else 0.05
        _vt = M.VT if vt is None else vt
        expo.append(min(M.EXPOSURE_CAP, _vt / dvol) if dvol > 0 else M.EXPOSURE_CAP)
    return {
        "bars": len(bars), "start": M.fdate(bars[0]["t"]), "end": M.fdate(bars[-1]["t"]),
        "final": st.get("final", 0.0), "cagr": st.get("cagr", 0.0),
        "mdd": st.get("mdd", 0.0), "calmar": st.get("calmar", 0.0),
        "win": st.get("win_rate", 0.0), "liq": st.get("liq", 0),
        "bh_final": bst.get("final", 0.0), "bh_cagr": bst.get("cagr", 0.0),
        "bh_mdd": bst.get("mdd", 0.0), "bh_calmar": bst.get("calmar", 0.0),
        "avg_expo": statistics.mean(expo) if expo else 0.0,
        "long_pct": 100.0 * sum(1 for x in sig if x > 0) / len(sig),
        "short_pct": 100.0 * sum(1 for x in sig if x < 0) / len(sig),
        "trades": len(res["trades"]),
        "equity": res["equity"], "sig": sig, "trade_list": res["trades"],
    }


def r2(x, n=2):
    return round(x, n)


def load_group(group, tag):
    log("")
    log("  载入【%s】" % tag)
    data, names = {}, {}
    seen = set()
    for sym, name in group:
        if sym in seen:
            continue
        seen.add(sym)
        try:
            b = get_bars(sym)
        except Exception as e:
            log("    %-9s 失败：%s" % (sym, str(e)[:50]))
            continue
        if len(b) < MIN_BARS:
            log("    %-9s 只有 %d 根，跳过" % (sym, len(b)))
            continue
        data[sym], names[sym] = b, name
    log("    有效 %d 个" % len(data))
    return data, names


def table_group(data, names, order, title, note=""):
    log("")
    log(title)
    if note:
        log(note)
    log("")
    log("%-7s %6s %-11s %-11s %11s %8s %9s %7s %11s %8s %7s %6s" % (
        "币", "根数", "起", "止", "策略终值", "年化", "最大回撤", "Calmar",
        "买入持有", "B&H年化", "策略/BH", "爆仓"))
    log("-" * 122)
    out, res = [], {}
    for s in order:
        if s not in data:
            continue
        r = summarize(data[s])
        res[s] = r
        ratio = r["final"] / r["bh_final"] if r["bh_final"] else float("inf")
        log("%-7s %6d %-11s %-11s %10.2f× %+7.1f%% %8.1f%% %7.3f %10.2f× %+7.1f%% %6.2f× %6d" % (
            names[s], r["bars"], r["start"], r["end"], r["final"], r["cagr"] * 100,
            r["mdd"] * 100, r["calmar"], r["bh_final"], r["bh_cagr"] * 100, ratio, r["liq"]))
        out.append([names[s], r["bars"], r["start"], r["end"], r2(r["final"]),
                    r2(r["cagr"] * 100, 1), r2(r["mdd"] * 100, 1), r2(r["calmar"], 3),
                    r2(r["bh_final"]), r2(r["bh_cagr"] * 100, 1), r2(r["bh_mdd"] * 100, 1),
                    r2(r["bh_calmar"], 3), r2(ratio), r2(r["avg_expo"]),
                    r2(r["long_pct"], 1), r2(r["short_pct"], 1), r["trades"],
                    r2(r["win"] * 100, 1), r["liq"]])
    return out, res


def dist_stats(vals, label):
    vals = [v for v in vals if v is not None]
    if not vals:
        return [label, 0, "", "", "", "", ""]
    return [label, len(vals), r2(statistics.median(vals)), r2(statistics.mean(vals)),
            r2(min(vals)), r2(max(vals)), r2(100.0 * sum(1 for v in vals if v > 1) / len(vals), 1)]


def main():
    os.makedirs(OUT, exist_ok=True)
    log("=" * 122)
    log(" 多币种对比：同一套策略跑遍各类币（参数一字不改）")
    log(" 策略：EMA10/30 收盘确认 + 次日开盘成交 ｜ SMA50 只约束多头 ｜ 波动率目标 3%/日 ｜ 上限 1.0×")
    log(" 载体：多头现货 0.1% ｜ 空头合约 1× 逐仓（4bp + 资金费 3bp/日）｜ 滑点 5bp")
    log("=" * 122)

    dA, nA = load_group(GROUP_A, "组 A：今日头部（幸存者）")
    dB, nB = load_group(GROUP_B, "组 B：昔日头部（含已退市）")

    orderA, orderB = [], []
    for s, _ in GROUP_A:
        if s in dA and s not in orderA:
            orderA.append(s)
    for s, _ in GROUP_B:
        if s in dB and s not in orderB:
            orderB.append(s)

    rowsA, resA = table_group(
        dA, nA, orderA,
        "【第 1 步】组 A：今日头部（每个币从自己上市第一天起跑）",
        "  这一栏是后视镜 —— 今天还能排在前面的币，本来就是涨上来的。看结论必须结合第 2 步。")

    rowsB, resB = table_group(
        dB, nB, orderB,
        "【第 2 步】组 B：昔日头部（2017-2018 年市值前 20，今天大多已衰落）",
        "  带 * 的是已经下架/停止交易的，数据止于退市日。这一栏才是「当年随机挑一个」的真实体验。")

    # ---------------------------------------------------------------- 第 3 步
    log("")
    log("【第 3 步】两组对比：如果 2017 年随机从头部挑一个币跑这套策略")
    log("")
    log("%-26s %4s %12s %12s %10s %10s %12s" % (
        "组", "个数", "策略终值中位", "策略终值均值", "最差", "最好", "策略赚钱占比"))
    log("-" * 122)
    dist_rows = [
        dist_stats([resA[s]["final"] for s in orderA], "组 A 今日头部"),
        dist_stats([resB[s]["final"] for s in orderB], "组 B 昔日头部"),
        dist_stats([resA[s]["final"] for s in orderA if s != "BTCUSDT"], "组 A 去掉 BTC"),
        dist_stats([resB[s]["final"] for s in orderB], "组 B 全部"),
    ]
    # 合并 A+B 去重
    all_final = [resA[s]["final"] for s in orderA] + [resB[s]["final"] for s in orderB]
    dist_rows.append(dist_stats(all_final, "全部 43 个币"))
    dist_rows = [dist_rows[0], dist_rows[1], dist_rows[2], dist_rows[4]]
    for row in dist_rows:
        log("%-26s %4d %11.2f× %11.2f× %9.2f× %9.2f× %11.1f%%" % (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6]))
    log("")
    log("  注：终值是「1000 U 变成多少倍」。策略终值 > 1× 即赚钱。")

    # ---------------------------------------------------------------- 第 4 步
    log("")
    log("【第 4 步】同窗口配对：每个币从自己上市那天算，BTC 也切到同一天（唯一公平的横向比较）")
    log("  「跑赢BTC」= 本币策略终值 / 同窗口 BTC 策略终值")
    log("")
    log("%-7s %-11s %6s %11s %11s %8s %11s %9s %9s" % (
        "币", "起点", "根数", "本币策略", "同窗BTC", "跑赢BTC", "本币B&H", "本币Calmar", "BTC Calmar"))
    log("-" * 122)
    pair_rows, beats, total = [], 0, 0
    for s in orderA + orderB:
        if s == "BTCUSDT":
            continue
        pool = dA if s in dA else dB
        b = pool[s]
        start = b[0]["t"]
        btc_slice = [x for x in dA["BTCUSDT"] if x["t"] >= start]
        # BTC 只切到本币最后一天（退市币要对齐终点）
        end = b[-1]["t"]
        btc_slice = [x for x in btc_slice if x["t"] <= end]
        if len(btc_slice) < MIN_BARS:
            continue
        rb = summarize(btc_slice)
        r = (resA if s in dA else resB)[s]
        beat = r["final"] / rb["final"] if rb["final"] else 0
        total += 1
        beats += 1 if beat > 1 else 0
        tag = nA[s] if s in dA else nB[s]
        log("%-7s %-11s %6d %10.2f× %10.2f× %7.2f× %10.2f× %9.3f %9.3f" % (
            tag, r["start"], r["bars"], r["final"], rb["final"], beat,
            r["bh_final"], r["calmar"], rb["calmar"]))
        pair_rows.append([tag, "A" if s in dA else "B", r["start"], r["bars"],
                          r2(r["final"]), r2(rb["final"]), r2(beat), r2(r["bh_final"]),
                          r2(r["calmar"], 3), r2(rb["calmar"], 3),
                          r2(r["mdd"] * 100, 1), r2(rb["mdd"] * 100, 1)])
    log("")
    log("  43 个币里，同窗口跑赢 BTC 的有 %d 个（%.0f%%）" % (beats, 100.0 * beats / total if total else 0))

    # ---------------------------------------------------------------- 第 5 步
    log("")
    log("【第 5 步】分散组合：等权跑 N 个币，能不能比只跑 BTC 更好？")
    log("  做法：每个币分 1/N 资金各自独立跑，每天加总。回撤会因低相关而下降。")
    log("")
    log("%-30s %5s %-11s %11s %8s %9s %8s" % (
        "组合", "币数", "区间", "终值", "年化", "最大回撤", "Calmar"))
    log("-" * 122)
    port_rows = []

    def portfolio(syms, pool):
        starts = [pool[s][0]["t"] for s in syms]
        ends = [pool[s][-1]["t"] for s in syms]
        d0, d1 = max(starts), min(ends)
        series = []
        for s in syms:
            b = pool[s]
            r = resA[s] if s in resA else resB[s]
            m = {}
            for i, bar in enumerate(b):
                if r["equity"][i] is not None:
                    m[bar["t"]] = r["equity"][i]
            keys = sorted(k for k in m if d0 <= k <= d1)
            if len(keys) < 100:
                continue
            base = m[keys[0]]
            series.append({k: m[k] / base for k in keys})
        if not series:
            return None
        dates = sorted(set.intersection(*[set(s.keys()) for s in series])) if len(series) > 1 \
            else sorted(series[0].keys())
        eq = [statistics.mean(s[d] for s in series) for d in dates]
        st = M.stats([e * M.CAPITAL for e in eq], None)
        return {"st": st, "n": len(series), "d0": M.fdate(dates[0]), "d1": M.fdate(dates[-1])}

    longA = [s for s in orderA if dA[s][0]["t"] <= 1547596800000]  # 2019-01-16 之前上市
    combos = [
        ("只跑 BTC", ["BTCUSDT"]),
        ("BTC + ETH", ["BTCUSDT", "ETHUSDT"]),
        ("组 A 早期 %d 个等权" % len(longA), longA),
        ("组 A 早期 + 组 B 早期", [s for s in longA] +
         [s for s in orderB if dB[s][0]["t"] <= 1547596800000]),
    ]
    for label, syms in combos:
        syms = [s for s in syms if s in resA or s in resB]
        if not syms:
            continue
        p = portfolio(syms, {**dA, **dB})
        if not p:
            continue
        st = p["st"]
        log("%-30s %5d %-11s %10.2f× %+7.1f%% %8.1f%% %8.3f" % (
            label, p["n"], p["d0"] + "起", st["final"], st["cagr"] * 100,
            st["mdd"] * 100, st["calmar"]))
        port_rows.append([label, p["n"], p["d0"], p["d1"], r2(st["final"]),
                          r2(st["cagr"] * 100, 1), r2(st["mdd"] * 100, 1), r2(st["calmar"], 3)])

    # ---------------------------------------------------------------- 第 6 步
    log("")
    log("【第 6 步】为什么？把标的本身的「可交易性」量出来")
    log("  日波动 = 日收益率标准差（波动率目标按它缩仓，波动越大仓位越小）")
    log("  假信号率 = 持仓不足 5 天就反手的比例（越高说明噪声越多、越容易骗线）")
    log("")
    log("%-7s %8s %9s %9s %9s %9s %8s" % (
        "币", "日波动", "年化波动", "平均持有", "假信号率", "交易笔数", "胜率"))
    log("-" * 122)
    qual_rows = []
    for s in orderA + orderB:
        pool = dA if s in dA else dB
        b = pool[s]
        r = resA[s] if s in dA else resB[s]
        rets = [b[i]["close"] / b[i - 1]["close"] - 1 for i in range(1, len(b))]
        dvol = statistics.stdev(rets)
        holds = [t["exit_i"] - t["entry_i"] for t in r["trade_list"]]
        fake = 100.0 * sum(1 for h in holds if h < 5) / len(holds) if holds else 0
        tag = nA[s] if s in dA else nB[s]
        log("%-7s %7.2f%% %8.1f%% %8.1f天 %8.1f%% %9d %7.1f%%" % (
            tag, dvol * 100, dvol * math.sqrt(365) * 100,
            statistics.mean(holds) if holds else 0, fake, len(holds), r["win"] * 100))
        qual_rows.append([tag, "A" if s in dA else "B", r2(dvol * 100, 2),
                          r2(dvol * math.sqrt(365) * 100, 1),
                          r2(statistics.mean(holds) if holds else 0, 1), r2(fake, 1),
                          len(holds), r2(r["win"] * 100, 1), r2(r["avg_expo"])])

    # ---------------------------------------------------------------- 第 7 步
    log("")
    log("【第 7 步】给 alt 平反：前面 alt 输，是不是因为波动率目标把它们的手脚绑住了？")
    log("  BTC 日波动 3.49%，vt=3% 算出敞口 0.86×、被 1.0 截断后平均 0.70×；")
    log("  alt 日波动 5~10%，vt=3% 只算出 0.3~0.6×，等于自动只用了四成资金。")
    log("  测试 A：把每个币的 vt 按「本币波动 / BTC 波动」同比例放大 → 平均敞口拉回 0.70×")
    log("  测试 B：滑点从 5bp 提到 20bp（alt 盘口薄，这是真实成本；BTC 才是真能 5bp 成交的）")
    log("  测试 C：资金费从 3bp/日 提到 10bp/日（空头是「收」资金费，这是对 alt 有利的假设）")
    log("")
    btc_dvol = statistics.stdev([dA["BTCUSDT"][i]["close"] / dA["BTCUSDT"][i - 1]["close"] - 1
                                 for i in range(1, len(dA["BTCUSDT"]))])
    log("%-7s %7s %10s %10s %8s %9s %9s %9s %10s %9s" % (
        "币", "日波动", "原始终值", "敞口拉平后", "变化", "拉平回撤", "拉平Calmar",
        "原始Calmar", "高滑点终值", "高滑点Calmar"))
    log("-" * 122)
    fix_rows = []
    for s in orderA + orderB:
        pool = dA if s in dA else dB
        b = pool[s]
        r0 = (resA if s in dA else resB)[s]
        dv = statistics.stdev([b[i]["close"] / b[i - 1]["close"] - 1 for i in range(1, len(b))])
        vt2 = M.VT * dv / btc_dvol
        r1 = summarize(b, vt=vt2)
        r3 = summarize(b, slip=0.0020)
        tag = nA[s] if s in dA else nB[s]
        log("%-7s %6.2f%% %9.2f× %9.2f× %7.2f× %8.1f%% %9.3f %9.3f %9.2f× %9.3f" % (
            tag, dv * 100, r0["final"], r1["final"],
            r1["final"] / r0["final"] if r0["final"] else 0,
            r1["mdd"] * 100, r1["calmar"], r0["calmar"], r3["final"], r3["calmar"]))
        fix_rows.append([tag, "A" if s in dA else "B", r2(dv * 100, 2), r2(r0["final"]),
                         r2(r1["final"]), r2(r1["final"] / r0["final"] if r0["final"] else 0),
                         r2(r1["avg_expo"]), r2(r1["mdd"] * 100, 1), r2(r1["calmar"], 3),
                         r2(r0["calmar"], 3), r2(r3["final"]), r2(r3["calmar"], 3)])
    log("")
    log("  读法：拉平敞口后 alt 的终值普遍涨 1.5~3 倍 —— 说明前面确实被波动率目标绑住了。")
    log("        但收益放大的同时回撤也放大，看 Calmar 才知道是不是真的变好。")

    # ---------------------------------------------------------------- 落盘
    H1 = ["币", "根数", "起", "止", "策略终值", "年化%", "最大回撤%", "Calmar",
          "买入持有终值", "B&H年化%", "B&H回撤%", "B&H Calmar", "策略/BH",
          "平均敞口", "多头占比%", "空头占比%", "交易笔数", "胜率%", "爆仓次数"]

    def wcsv(name, headers, rows):
        with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)

    wcsv("01_今日头部_全程.csv", H1, rowsA)
    wcsv("02_昔日头部_全程.csv", H1, rowsB)
    wcsv("03_分布统计.csv",
         ["组", "币数", "终值中位", "终值均值", "最差", "最好", "赚钱占比%"], dist_rows)
    wcsv("04_同窗口配对.csv",
         ["币", "组", "起点", "根数", "本币策略", "同窗BTC", "跑赢BTC", "本币B&H",
          "本币Calmar", "BTC Calmar", "本币回撤%", "BTC回撤%"], pair_rows)
    wcsv("05_分散组合.csv", ["组合", "币数", "起", "止", "终值", "年化%", "最大回撤%", "Calmar"],
         port_rows)
    wcsv("06_标的质量.csv",
         ["币", "组", "日波动%", "年化波动%", "平均持有天数", "假信号率%", "交易笔数",
          "胜率%", "平均敞口"], qual_rows)
    wcsv("07_敞口拉平与高资金费.csv",
         ["币", "组", "日波动%", "原始终值", "敞口拉平后终值", "变化倍数",
          "拉平后平均敞口", "拉平后回撤%", "拉平后Calmar", "原始Calmar",
          "高滑点终值", "高滑点Calmar"], fix_rows)

    log("")
    log("  已写入 %s（7 个 CSV）" % OUT)


if __name__ == "__main__":
    main()
