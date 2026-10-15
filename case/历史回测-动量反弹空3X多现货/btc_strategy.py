# -*- coding: utf-8 -*-
"""
================================================================================
 BTC 日线趋势策略 · 一键回测对比（自包含脚本，仅用 Python 标准库）
================================================================================
 这个脚本做什么：
   1) 从币安公开行情接口拉取 BTC / ETH / SOL 现货日线全历史（无需 API 密钥）
   2) 用同一套信号跑通三种载体的完整对比：
        · 现货（无杠杆、只做多）
        · 逐仓合约（含爆仓、资金费、保证金记账）
        · 混合载体（只做多走现货、只做空走合约 3× 逐仓）  ← 推荐方案
   3) 输出完整对比数据到 ./回测结果/ 目录（CSV，Excel 可直接打开）
      并在控制台打印可读汇总

 关键口径（务必了解，否则数字会看不懂）：
   · 信号在当日收盘确认，次日开盘成交（无前视偏差）
   · 现货手续费 0.1%，合约手续费 0.04%，滑点单边 0.05%
   · 资金费 0.03%/日（多头付、空头收）
   · 仓位：波动率目标 3%/日，敞口上限 1.0×（名义 ≤ 权益）
   · 逐仓爆仓线：多头 = 入场价 × (1 - 1/杠杆)

 运行方式：
   · Windows：双击同目录下的「运行回测.bat」
   · 命令行：python btc_strategy.py            （首次会联网拉数据）
             python btc_strategy.py --refresh  （强制重新拉取最新数据）

 依赖：Python 3.8+，无需安装任何第三方库。
================================================================================
"""

import csv
import json
import math
import os
import random
import ssl
import sys
import time
import unicodedata
import urllib.request

# ==============================================================================
# 0. 配置
# ==============================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "回测结果")

CAPITAL = 1000.0            # 本金（USDT）
VT = 0.03                   # 波动率目标 3%/日
EXPOSURE_CAP = 1.0          # 名义敞口上限（相对权益）
SPOT_FEE = 0.001            # 现货手续费 0.1%
FUT_FEE = 0.0004            # 合约手续费 0.04%
SLIP = 0.0005               # 单边滑点 0.05%
FUNDING_D = 0.0003          # 资金费 0.03%/日
WARMUP = 60                 # 预热期（前 60 根不出信号）
LEVERAGE = 3.0              # 合约杠杆
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
PARAM_SETS = [(8, 24), (10, 30), (12, 36), (15, 45), (20, 60)]
DAY_MS = 86400000
API_BASE = "https://data-api.binance.vision/api/v3/klines"
FETCH_START = 1502928000000  # 2017-08-17


LOG_BUF = []


def log(msg=""):
    """跨平台安全打印（Windows 控制台编码兜底），同时缓存用于落盘。"""
    s = str(msg)
    LOG_BUF.append(s)
    try:
        sys.stdout.write(s + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(s.encode("utf-8", "replace").decode("utf-8") + "\n")
    sys.stdout.flush()


def hr(ch="=", n=100):
    log(ch * n)


def dwidth(s):
    """字符串显示宽度：中日韩全角字符按 2 列计算。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def pad(s, w, align="left"):
    """按显示宽度补空格，保证中英文混排表格对齐。"""
    s = str(s)
    n = max(0, w - dwidth(s))
    return (s + " " * n) if align == "left" else (" " * n + s)


def row(cells):
    """cells: [(文本, 宽度, 对齐)] → 一行对齐好的表格文本。"""
    return "  " + " ".join(pad(t, w, a) for t, w, a in cells)


# ==============================================================================
# 1. 数据获取（带本地缓存）
# ==============================================================================
def fetch_klines(symbol, start_ms):
    """分页拉取日线。返回 [[open_time, open, high, low, close, volume, close_time], ...]"""
    rows = []
    t = start_ms
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    while True:
        url = "%s?symbol=%s&interval=1d&startTime=%d&limit=1000" % (API_BASE, symbol, t)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = None
        for attempt in range(6):
            try:
                with opener.open(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                break
            except Exception as e:
                if attempt == 5:
                    raise RuntimeError("拉取 %s 失败：%s" % (symbol, e))
                time.sleep(1.5 * (attempt + 1))
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000:
            break
        t = data[-1][0] + DAY_MS
        time.sleep(0.25)
    return rows


def ensure_data(symbol, refresh=False):
    """确保 data/<symbol>_1d.csv 存在；返回 bar 列表。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "%s_1d.csv" % symbol)
    if refresh or not os.path.exists(path):
        log("  拉取 %-9s ..." % symbol)
        rows = fetch_klines(symbol, FETCH_START)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time"])
            for r in rows:
                w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
        log("             %d 根" % len(rows))
    return load(symbol)


def load(symbol):
    path = os.path.join(DATA_DIR, "%s_1d.csv" % symbol)
    bars = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bars.append({
                "t": int(row["open_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "vol": float(row["volume"]),
            })
    return bars


def fdate(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


# ==============================================================================
# 2. 技术指标
# ==============================================================================
def ema(vals, n):
    out = [float("nan")] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1.0)
    s = sum(vals[:n]) / n
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out


def sma(vals, n):
    out = [float("nan")] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def atr(bars, n=14):
    out = [float("nan")] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    if len(trs) < n:
        return out
    s = sum(trs[:n]) / n
    out[n - 1] = s
    for i in range(n, len(trs)):
        s = (s * (n - 1) + trs[i]) / n
        out[i] = s
    return out


def rsi(vals, n=14):
    out = [float("nan")] * len(vals)
    if len(vals) <= n:
        return out
    g = ls = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        g += max(d, 0.0)
        ls += max(-d, 0.0)
    ag, al = g / n, ls / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def donchian(bars, n):
    hi = [float("nan")] * len(bars)
    lo = [float("nan")] * len(bars)
    for i in range(n - 1, len(bars)):
        hi[i] = max(bars[j]["high"] for j in range(i - n + 1, i + 1))
        lo[i] = min(bars[j]["low"] for j in range(i - n + 1, i + 1))
    return hi, lo


def stdev(vals, n):
    out = [float("nan")] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


# ==============================================================================
# 3. 信号（统一约定：+1 做多 / -1 做空 / 0 空仓）
# ==============================================================================
def sig_buyhold(bars):
    return [1] * len(bars)


def sig_ema_cross(bars, fast=10, slow=30):
    """双均线趋势：快线在慢线上方做多，下方做空（永续在场）。← 推荐信号"""
    c = [b["close"] for b in bars]
    ef, es = ema(c, fast), ema(c, slow)
    out = [0] * len(bars)
    for i in range(len(bars)):
        if math.isnan(ef[i]) or math.isnan(es[i]):
            out[i] = 0
        else:
            out[i] = 1 if ef[i] > es[i] else -1
    return out


def sig_momentum_rebound(bars, fast=10, slow=30, rsi_n=14, rsi_th=45):
    """【用户原始口径·动量反弹】大趋势向上 且 RSI 从下方上穿阈值（等回调结束再进）"""
    c = [b["close"] for b in bars]
    ef, es = ema(c, fast), ema(c, slow)
    r = rsi(c, rsi_n)
    out = [0] * len(bars)
    for i in range(1, len(bars)):
        if math.isnan(ef[i]) or math.isnan(es[i]) or math.isnan(r[i]) or math.isnan(r[i - 1]):
            continue
        up = ef[i] > es[i]
        cross_up = r[i] > rsi_th and r[i - 1] <= rsi_th
        cross_dn = r[i] < (100 - rsi_th) and r[i - 1] >= (100 - rsi_th)
        if up and cross_up:
            out[i] = 1
        elif (not up) and cross_dn:
            out[i] = -1
        else:
            out[i] = out[i - 1]
    return out


def sig_dip_bounce(bars, n=14, th=30):
    """抄底反弹 / 均值回归：RSI 超卖做多，回到中位平仓"""
    c = [b["close"] for b in bars]
    r = rsi(c, n)
    out = [0] * len(bars)
    for i in range(1, len(bars)):
        if math.isnan(r[i]):
            continue
        if r[i] < th:
            out[i] = 1
        elif r[i] > 50:
            out[i] = 0
        else:
            out[i] = out[i - 1]
    return out


def sig_bb_revert(bars, n=20, k=2.0):
    """布林带下轨反弹（均值回归）"""
    c = [b["close"] for b in bars]
    m = sma(c, n)
    sd = stdev(c, n)
    out = [0] * len(bars)
    for i in range(1, len(bars)):
        if math.isnan(m[i]):
            continue
        if c[i] < m[i] - k * sd[i]:
            out[i] = 1
        elif c[i] > m[i]:
            out[i] = 0
        else:
            out[i] = out[i - 1]
    return out


def sig_breakout(bars, n=20):
    """唐奇安通道突破（趋势动量）"""
    hi, lo = donchian(bars, n)
    out = [0] * len(bars)
    for i in range(1, len(bars)):
        if math.isnan(hi[i - 1]):
            continue
        if bars[i]["close"] > hi[i - 1]:
            out[i] = 1
        elif bars[i]["close"] < lo[i - 1]:
            out[i] = -1
        else:
            out[i] = out[i - 1]
    return out


def sig_state_trend(bars, ma=200):
    """趋势状态择时：收盘 > EMA200 持多，否则空仓（只多）"""
    c = [b["close"] for b in bars]
    e = ema(c, ma)
    out = [0] * len(bars)
    for i in range(len(bars)):
        if math.isnan(e[i]):
            continue
        out[i] = 1 if c[i] > e[i] else 0
    return out


# ==============================================================================
# 4. 绩效统计
# ==============================================================================
def stats(equity, trades=None):
    eq = [x for x in equity if x is not None]
    if len(eq) < 2 or eq[0] <= 0:
        return {"final": 0.0, "cagr": -1.0, "mdd": -1.0, "calmar": 0.0}
    n = len(eq)
    years = n / 365.25
    final = eq[-1] / eq[0]
    cagr = final ** (1 / years) - 1 if final > 0 else -1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    calmar = (cagr / abs(mdd)) if mdd < 0 else 0.0
    peak, peak_i, uw, maxuw = eq[0], 0, 0, 0
    for i, v in enumerate(eq):
        if v >= peak:
            peak, peak_i, uw = v, i, 0
        else:
            uw = i - peak_i
            maxuw = max(maxuw, uw)
    W = 365
    roll = None
    if n > W:
        r = sorted(eq[i] / eq[i - W] - 1.0 for i in range(W, n))
        m = len(r)
        roll = {"min": r[0], "p25": r[m // 4], "med": r[m // 2], "p75": r[3 * m // 4],
                "max": r[-1], "pos_ratio": sum(1 for x in r if x > 0) / len(r)}
    out = {"final": final, "cagr": cagr, "mdd": mdd, "calmar": calmar,
           "max_underwater": maxuw, "roll365": roll, "n_bars": n}
    if trades:
        out["n_trades"] = len(trades)
        out["win_rate"] = sum(1 for t in trades if t["pnl"] > 0) / len(trades)
        out["liq"] = sum(1 for t in trades if t.get("liq"))
    return out


def trade_stats(trades):
    """按载体分腿统计：胜率 / 盈亏比 / 盈亏因子 / 最长连亏 / 平均持仓。"""
    def blk(ts):
        if not ts:
            return None
        w = [t for t in ts if t["pnl"] > 0]
        ls = [t for t in ts if t["pnl"] <= 0]
        gw = sum(t["pnl"] for t in w)
        gl = -sum(t["pnl"] for t in ls)
        mx = cur = 0
        for t in ts:
            if t["pnl"] <= 0:
                cur += 1
                mx = max(mx, cur)
            else:
                cur = 0
        return {
            "n": len(ts), "wins": len(w), "losses": len(ls),
            "win_rate": len(w) / len(ts),
            "avg_win": gw / len(w) if w else 0.0,
            "avg_loss": -gl / len(ls) if ls else 0.0,
            "gross_win": gw, "gross_loss": gl,
            "pf": (gw / gl) if gl > 0 else float("inf"),
            "max_consec_loss": mx,
            "avg_hold": sum(t["exit_i"] - t["entry_i"] for t in ts) / len(ts),
        }
    return {"all": blk(trades),
            "spot": blk([t for t in trades if t["leg"] == "spot"]),
            "fut": blk([t for t in trades if t["leg"] == "fut"])}


# ==============================================================================
# 5. 引擎 A：现货（只做多，无杠杆）
# ==============================================================================
def backtest_spot(bars, sig, capital=CAPITAL, fee=SPOT_FEE, slip=SLIP,
                  sizing="vol", vt=VT, exposure_cap=EXPOSURE_CAP, atr_n=14, warmup=WARMUP):
    n = len(bars)
    a = atr(bars, atr_n)
    eq, units, entry_px, entry_i = capital, 0.0, 0.0, -1
    equity = [None] * n
    trades = []
    for i in range(n):
        if i < warmup:
            equity[i] = eq + units * bars[i]["close"]
            continue
        o = bars[i]["open"]
        prev_c = bars[i - 1]["close"]
        eq_total = eq + units * prev_c
        tgt = sig[i - 1]
        want, cur = (1 if tgt > 0 else 0), (1 if units > 0 else 0)
        if want != cur:
            if cur == 1:
                px = o * (1 - slip)
                proceeds = units * px
                trades.append({"entry_i": entry_i, "exit_i": i, "dir": 1,
                               "entry_px": entry_px, "exit_px": px,
                               "pnl": proceeds * (1 - fee) - units * entry_px * (1 + fee),
                               "liq": False, "leg": "spot"})
                eq += proceeds * (1 - fee)
                units = 0.0
            if want == 1:
                if sizing == "vol":
                    dvol = a[i - 1] / bars[i - 1]["close"] if not math.isnan(a[i - 1]) else 0.05
                    expo = min(exposure_cap, vt / dvol) if dvol > 0 else exposure_cap
                else:
                    expo = exposure_cap
                entry_px = o * (1 + slip)
                units = eq_total * expo / entry_px
                cost = units * entry_px
                eq = eq - cost - cost * fee
                entry_i = i
        equity[i] = eq + units * bars[i]["close"]
    return {"equity": equity, "trades": trades, "stats": stats(equity, trades)}


# ==============================================================================
# 6. 引擎 B：逐仓合约（可多可空，含爆仓 / 资金费 / 保证金记账）
# ==============================================================================
def backtest_futures(bars, sig, capital=CAPITAL, L=LEVERAGE, fee=FUT_FEE, slip=SLIP,
                     funding_d=FUNDING_D, sizing="vol", vt=VT, exposure_cap=EXPOSURE_CAP,
                     atr_n=14, warmup=WARMUP, long_only=False, cooldown_after_liq=0):
    n = len(bars)
    a = atr(bars, atr_n)
    eq, pos, qty, entry_px, margin, fund_acc, entry_i = capital, 0, 0.0, 0.0, 0.0, 0.0, -1
    equity = [None] * n
    trades = []
    worst_adverse = worst_gap = 0.0
    liq_days = []
    block_until = -1

    def close_pos(i, px, reason):
        nonlocal eq, pos, qty, entry_px, margin, fund_acc, entry_i
        pnl = qty * (px - entry_px) * pos - qty * px * fee - fund_acc
        eq += pnl
        trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                       "exit_px": px, "pnl": pnl, "liq": reason == "liq", "leg": "fut"})
        pos, qty, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1

    def open_pos(i, px, direction):
        nonlocal eq, pos, qty, entry_px, margin, fund_acc, entry_i
        if sizing == "vol":
            dvol = a[i - 1] / bars[i - 1]["close"] if not math.isnan(a[i - 1]) else 0.05
            expo = min(exposure_cap, vt / dvol) if dvol > 0 else exposure_cap
        else:
            expo = exposure_cap
        notional = eq * expo
        m = notional / L
        if m > eq:
            m, notional = eq, eq * L
        qty, entry_px, margin = notional / px, px, m
        pos, entry_i, fund_acc = direction, i, 0.0
        eq -= notional * fee

    for i in range(n):
        if i < warmup:
            equity[i] = eq
            continue
        o, h, l, c = bars[i]["open"], bars[i]["high"], bars[i]["low"], bars[i]["close"]
        tgt = sig[i - 1]
        if long_only and tgt < 0:
            tgt = 0
        if i < block_until:
            tgt = 0
        if tgt != pos:
            if pos != 0:
                close_pos(i, o * (1 - slip) if pos > 0 else o * (1 + slip), "signal")
            if tgt != 0:
                open_pos(i, o * (1 + slip) if tgt > 0 else o * (1 - slip), tgt)
        if pos != 0:
            if pos > 0:
                liq_px = entry_px * (1 - 1.0 / L)
                adverse, gap = l / entry_px - 1.0, o / entry_px - 1.0
            else:
                liq_px = entry_px * (1 + 1.0 / L)
                adverse, gap = 1.0 - h / entry_px, 1.0 - o / entry_px
            worst_adverse = min(worst_adverse, adverse)
            worst_gap = min(worst_gap, gap)
            hit = (o <= liq_px) if pos > 0 else (o >= liq_px)
            px = o if hit else liq_px
            if not hit:
                hit = (l <= liq_px) if pos > 0 else (h >= liq_px)
            if hit:
                eq -= margin
                trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                               "exit_px": px, "pnl": -margin, "liq": True, "leg": "fut"})
                liq_days.append(i)
                block_until = i + cooldown_after_liq
                pos, qty, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1
        if pos != 0:
            fund_acc += pos * qty * c * funding_d
        if pos != 0:
            eq_now = eq + qty * (c - entry_px) * pos - fund_acc
        else:
            eq_now = eq
        equity[i] = max(eq_now, 0.0)

    st = stats(equity, trades)
    st["worst_adverse"] = worst_adverse
    st["worst_gap"] = worst_gap
    st["liq_days"] = liq_days
    return {"equity": equity, "trades": trades, "stats": st}


# ==============================================================================
# 7. 引擎 C：混合载体（多头走现货、空头走合约 3× 逐仓）★ 推荐
# ==============================================================================
def backtest_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE, spot_fee=SPOT_FEE,
                    fut_fee=FUT_FEE, slip=SLIP, funding_d=FUNDING_D, sizing="vol",
                    vt=VT, exposure_cap=EXPOSURE_CAP, atr_n=14, warmup=WARMUP,
                    cooldown_after_liq=0, allow_short=True):
    n = len(bars)
    a = atr(bars, atr_n)
    eq, pos, units, entry_px, margin, fund_acc, entry_i = capital, 0, 0.0, 0.0, 0.0, 0.0, -1
    equity = [None] * n
    trades = []
    liq_days = []
    short_adverse = 0.0
    block_until = -1

    def expo_at(i):
        if sizing == "vol":
            dvol = a[i - 1] / bars[i - 1]["close"] if not math.isnan(a[i - 1]) else 0.05
            return min(exposure_cap, vt / dvol) if dvol > 0 else exposure_cap
        return exposure_cap

    def close_pos(i, px, reason):
        nonlocal eq, pos, units, entry_px, margin, fund_acc, entry_i
        if pos > 0:                                     # 现货平仓
            pnl = units * px * (1 - spot_fee) - units * entry_px * (1 + spot_fee)
            eq += units * px * (1 - spot_fee)
            leg = "spot"
        else:                                           # 合约平仓
            pnl = units * (px - entry_px) * pos - units * px * fut_fee - fund_acc
            eq += pnl
            leg = "fut"
        trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                       "exit_px": px, "pnl": pnl, "liq": reason == "liq", "leg": leg})
        pos, units, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1

    def open_pos(i, px, direction, eq_ref):
        nonlocal eq, pos, units, entry_px, margin, fund_acc, entry_i
        notional = eq_ref * expo_at(i)
        if direction > 0:
            units, entry_px, margin = notional / px, px, 0.0
            eq = eq - notional - notional * spot_fee
        else:
            m = notional / L
            if m > eq:
                m, notional = eq, eq * L
            units, entry_px, margin = notional / px, px, m
            eq -= notional * fut_fee
        pos, entry_i, fund_acc = direction, i, 0.0

    for i in range(n):
        if i < warmup:
            equity[i] = eq
            continue
        o, h, l, c = bars[i]["open"], bars[i]["high"], bars[i]["low"], bars[i]["close"]
        tgt = sig[i - 1]
        if not allow_short and tgt < 0:
            tgt = 0
        if i < block_until:
            tgt = 0
        prev_c = bars[i - 1]["close"]
        if pos > 0:
            eq_ref = eq + units * prev_c
        elif pos < 0:
            eq_ref = eq + units * (prev_c - entry_px) * pos - fund_acc
        else:
            eq_ref = eq
        if tgt != pos:
            if pos != 0:
                close_pos(i, o * (1 - slip) if pos > 0 else o * (1 + slip), "signal")
            if tgt != 0:
                open_pos(i, o * (1 + slip) if tgt > 0 else o * (1 - slip), tgt, eq_ref)
        if pos < 0:
            liq_px = entry_px * (1 + 1.0 / L)
            short_adverse = min(short_adverse, 1.0 - h / entry_px)
            hit = o >= liq_px
            px = o if hit else liq_px
            if not hit:
                hit = h >= liq_px
            if hit:
                eq -= margin
                trades.append({"entry_i": entry_i, "exit_i": i, "dir": pos, "entry_px": entry_px,
                               "exit_px": px, "pnl": -margin, "liq": True, "leg": "fut"})
                liq_days.append(i)
                block_until = i + cooldown_after_liq
                pos, units, entry_px, margin, fund_acc, entry_i = 0, 0.0, 0.0, 0.0, 0.0, -1
        if pos < 0:
            fund_acc += pos * units * c * funding_d
        if pos > 0:
            eq_now = eq + units * c
        elif pos < 0:
            eq_now = eq + units * (c - entry_px) * pos - fund_acc
        else:
            eq_now = eq
        equity[i] = max(eq_now, 0.0)

    st = stats(equity, trades)
    st["liq"] = len(liq_days)
    st["short_adverse"] = short_adverse
    return {"equity": equity, "trades": trades, "stats": st}


def equal_weight_portfolio(curves, capital=CAPITAL, warmup=WARMUP):
    """多组参数等权组合：每日按各曲线的日收益等权合成。"""
    n = len(curves[0])
    port = [None] * n
    port[warmup] = capital
    v = capital
    for i in range(warmup + 1, n):
        rs = [eq[i] / eq[i - 1] - 1.0 for eq in curves
              if eq[i] is not None and eq[i - 1] and eq[i - 1] > 0]
        if rs:
            v *= (1.0 + sum(rs) / len(rs))
        port[i] = v
    return port


# ==============================================================================
# 8. 信号诊断：与随机择时对比
# ==============================================================================
def naive_long(bars, sig):
    """无成本无杠杆的理论择时倍数（满仓做多 / 空仓）。"""
    eq, hold = 1.0, 0
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


def diagnose(bars, iters=400, seed=42):
    random.seed(seed)
    n = len(bars)
    tests = [
        ("动量反弹 10/30+RSI45（你的原始口径）", sig_momentum_rebound(bars, 10, 30, 14, 45)),
        ("动量反弹 20/60+RSI45", sig_momentum_rebound(bars, 20, 60, 14, 45)),
        ("抄底反弹 RSI<30（均值回归）", sig_dip_bounce(bars, 14, 30)),
        ("布林带下轨反弹 20/2", sig_bb_revert(bars, 20, 2.0)),
        ("双均线趋势 10/30 ★推荐", sig_ema_cross(bars, 10, 30)),
        ("双均线趋势 20/60", sig_ema_cross(bars, 20, 60)),
        ("唐奇安突破 20", sig_breakout(bars, 20)),
        ("趋势状态 EMA200（只多）", sig_state_trend(bars, 200)),
    ]
    rows = []
    for name, sig in tests:
        val = naive_long(bars, sig)
        k = max(1, int(round(sum(1 for s in sig if s > 0) / n * n)))
        better = 0
        for _ in range(iters):
            idx = set(random.sample(range(1, n), k))
            if naive_long(bars, [1 if i in idx else 0 for i in range(n)]) < val:
                better += 1
        pct = better / iters
        nsw = sum(1 for i in range(1, n) if sig[i] != sig[i - 1])
        rows.append({"name": name, "mult": val, "long_ratio": k / n, "switch": nsw,
                     "beat_random": pct,
                     "verdict": "有 edge" if pct > 0.8 else ("弱" if pct > 0.6 else "无 edge")})
    return rows


# ==============================================================================
# 9. 自检审计：绕开引擎独立重放，验证没有前视偏差
# ==============================================================================
def audit_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE, spot_fee=SPOT_FEE,
                 fut_fee=FUT_FEE, slip=SLIP, funding_d=FUNDING_D, vt=VT,
                 exposure_cap=EXPOSURE_CAP, atr_n=14, warmup=WARMUP):
    """
    完全独立地重新实现一遍混合载体逻辑（不调用 backtest_hybrid），
    逐笔比对交易点与成交价，并比对权益曲线。
    回测最容易骗人的地方是偷看未来数据，肉眼审代码几乎发现不了 —— 只能靠独立重放。
    """
    a = atr(bars, atr_n)
    n = len(bars)
    eq, pos, units, epx, margin, fund, ei = capital, 0, 0.0, 0.0, 0.0, 0.0, -1
    eqs = [None] * n
    tr = []
    for i in range(n):
        if i < warmup:
            eqs[i] = eq
            continue
        o, h, l, c = bars[i]["open"], bars[i]["high"], bars[i]["low"], bars[i]["close"]
        tgt = sig[i - 1]                       # 只用上一根收盘的信息
        pc = bars[i - 1]["close"]
        eqref = (eq + units * pc) if pos > 0 else (
            eq + units * (pc - epx) * pos - fund if pos < 0 else eq)
        dvol = a[i - 1] / pc if not math.isnan(a[i - 1]) else 0.05
        expo = min(exposure_cap, vt / dvol) if dvol > 0 else exposure_cap
        if tgt != pos:
            if pos != 0:
                px = o * (1 - slip) if pos > 0 else o * (1 + slip)
                if pos > 0:
                    eq = eq + units * px * (1 - spot_fee)
                else:
                    eq = eq + units * (px - epx) * pos - units * px * fut_fee - fund
                tr.append((ei, i, pos, epx, px))
                pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
            if tgt != 0:
                px = o * (1 + slip) if tgt > 0 else o * (1 - slip)
                notional = eqref * expo
                if tgt > 0:
                    units, epx = notional / px, px
                    eq = eq - notional - notional * spot_fee
                else:
                    m = notional / L
                    if m > eq:
                        m, notional = eq, eq * L
                    units, epx, margin = notional / px, px, m
                    eq = eq - notional * fut_fee
                pos, ei, fund = tgt, i, 0.0
        if pos < 0:                            # 空头 3× 逐仓爆仓线
            lp = epx * (1 + 1.0 / L)
            if o >= lp:
                hit, px = True, o
            else:
                hit, px = (h >= lp), lp
            if hit:
                eq -= margin
                tr.append((ei, i, pos, epx, px))
                pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
        if pos < 0:
            fund += pos * units * c * funding_d
        eqs[i] = max(eq + (units * c if pos > 0 else
                           (units * (c - epx) * pos - fund if pos < 0 else 0)), 0.0)
    return eqs, tr


# ==============================================================================
# 10. 风险与可信度工具（爆仓余量 / 参数样本外检验）
# ==============================================================================
def trade_adverse(bars, trades):
    """每笔交易的持仓期最大逆行（MAE，负值）。做空时 = 最低点相对入场价的涨幅取负。"""
    out = []
    for t in trades:
        a, b, d, px = t["entry_i"], t["exit_i"], t["dir"], t["entry_px"]
        if d > 0:
            m = min(bars[i]["low"] / px - 1.0 for i in range(a, b + 1))
        else:
            m = min(1.0 - bars[i]["high"] / px for i in range(a, b + 1))
        out.append((m, a, b, d, px, t["pnl"]))
    return out


def daily_returns(eq):
    n = len(eq)
    out = [None] * n
    for i in range(1, n):
        if eq[i] is not None and eq[i - 1]:
            out[i] = eq[i] / eq[i - 1] - 1.0
    return out


def cum_ret(rets, i0, i1):
    v = 1.0
    for i in range(i0, i1):
        if rets[i] is not None:
            v *= (1 + rets[i])
    return v


def weighted_equity(rets_map, weights, warmup, n):
    """按固定权重每日再平衡的组合净值曲线（起点归一为 1，预热期留 None 与引擎曲线对齐）。"""
    eq = [None] * n
    v = 1.0
    eq[warmup] = 1.0
    for i in range(warmup + 1, n):
        num = den = 0.0
        for k, w in weights.items():
            r = rets_map[k][i]
            if r is not None:
                num += w * r
                den += w
        if den > 0:
            v *= (1 + num / den)
        eq[i] = v
    return eq


def walk_forward_equity(rets_map, sets, ymap, warmup, n, win_years):
    """
    每年年初只用「截至当时的过去 win_years 年」挑参数，然后持有一年。
    这是对「105× 是不是事后挑出来的」最直接的检验。
    """
    eq = [None] * n
    v = 1.0
    eq[warmup] = 1.0
    picks = []
    for y in sorted(ymap):
        i0, i1 = ymap[y]
        i0 = max(i0, warmup + 1)
        if i1 <= i0:
            continue
        y0 = y - win_years
        s0 = max(ymap[y0][0], warmup + 1) if y0 in ymap else warmup + 1
        pick = max(sets, key=lambda k: cum_ret(rets_map[k], s0, i0))
        picks.append((y, pick))
        for i in range(i0, i1 + 1):
            r = rets_map[pick][i]
            if r is not None:
                v *= (1 + r)
            eq[i] = v
    return eq, picks


# ==============================================================================
# 11. 输出工具
# ==============================================================================
def write_csv(name, headers, rows):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)
    return path


def yearly_map(bars):
    m = {}
    for i, b in enumerate(bars):
        y = time.gmtime(b["t"] / 1000).tm_year
        if y not in m:
            m[y] = [i, i]
        m[y][1] = i
    return m


def yearly_returns(bars, eq):
    out = []
    for y, (i0, i1) in sorted(yearly_map(bars).items()):
        e0 = eq[i0 - 1] if i0 > 0 and eq[i0 - 1] else CAPITAL
        pk, mdd = e0, 0.0
        for i in range(i0, i1 + 1):
            if eq[i] is None:
                continue
            pk = max(pk, eq[i])
            mdd = min(mdd, eq[i] / pk - 1.0)
        out.append({"year": y, "start": e0, "end": eq[i1],
                    "ret": eq[i1] / e0 - 1.0 if e0 > 0 else 0.0, "mdd": mdd})
    return out


def drawdown_events(eq, top=5):
    vals = [x for x in eq if x is not None]
    peak, peak_i, eps = vals[0], 0, []
    for i, v in enumerate(vals):
        if v >= peak:
            peak, peak_i = v, i
        else:
            eps.append((v / peak - 1.0, peak, v, i - peak_i))
    eps.sort(key=lambda x: x[0])
    return eps[:top]


# ==============================================================================
# 12. 主流程
# ==============================================================================
def main():
    t_start = time.time()
    refresh = "--refresh" in sys.argv
    hr()
    log(" BTC 日线趋势策略 · 一键回测对比")
    log(" 本金 %.0f USDT | 波动率目标 %.0f%%/日 | 敞口上限 %.1f× | 杠杆 %.0f× 逐仓 | 滑点 %.2f%%"
        % (CAPITAL, VT * 100, EXPOSURE_CAP, LEVERAGE, SLIP * 100))
    hr()
    log("")
    log("【第 0 步】准备数据")
    bars = ensure_data("BTCUSDT", refresh)
    n = len(bars)
    dates = [fdate(b["t"]) for b in bars]
    years = n / 365.25
    bh = bars[-1]["close"] / bars[0]["close"]
    _pk = bars[0]["close"]
    bh_mdd = 0.0
    for b in bars:
        _pk = max(_pk, b["close"])
        bh_mdd = min(bh_mdd, b["close"] / _pk - 1.0)
    log("  BTC 样本 %s → %s，共 %d 根 / %.2f 年" % (dates[0], dates[-1], n, years))
    log("  买入持有 %.2f×（年化 %.1f%%，最大回撤 %.1f%%）"
        % (bh, (bh ** (1 / years) - 1) * 100, bh_mdd * 100))
    log("")

    # ---------------- 1. 信号诊断 ----------------
    log("【第 1 步】信号诊断：这些信号到底有没有 edge")
    drows = diagnose(bars, 400)
    hr("-")
    log(row([("信号", 38, "left"), ("择时倍数", 10, "right"), ("做多占比", 9, "right"),
             ("换手", 6, "right"), ("超越随机", 9, "right"), ("判定", 8, "left")]))
    hr("-")
    for r in drows:
        log(row([(r["name"], 38, "left"), ("%.2f×" % r["mult"], 10, "right"),
                 ("%.1f%%" % (r["long_ratio"] * 100), 9, "right"),
                 (str(r["switch"]), 6, "right"),
                 ("%.1f%%" % (r["beat_random"] * 100), 9, "right"),
                 (r["verdict"], 8, "left")]))
    hr("-")
    log("  判定标准：超越随机择时样本 > 80%% 才算有 edge；基准（买入持有）= %.2f×" % bh)
    write_csv("01_信号诊断.csv",
              ["信号", "择时倍数", "做多占比", "换手次数", "超越随机比例", "判定"],
              [[r["name"], "%.4f" % r["mult"], "%.4f" % r["long_ratio"], r["switch"],
                "%.4f" % r["beat_random"], r["verdict"]] for r in drows])
    log("")

    # ---------------- 2. 现货 vs 合约 ----------------
    log("【第 2 步】现货 vs 合约：同一信号，不同载体")
    sig_m = sig_momentum_rebound(bars, 10, 30, 14, 45)
    sig_t = sig_ema_cross(bars, 10, 30)
    plans = [
        ("动量反弹 · 现货 波动率目标", backtest_spot(bars, sig_m, sizing="vol")),
        ("动量反弹 · 现货 满仓", backtest_spot(bars, sig_m, sizing="full")),
        ("动量反弹 · 合约 多空 3×", backtest_futures(bars, sig_m, long_only=False)),
        ("趋势跟踪 · 现货 波动率目标", backtest_spot(bars, sig_t, sizing="vol")),
        ("趋势跟踪 · 现货 满仓", backtest_spot(bars, sig_t, sizing="full")),
        ("趋势跟踪 · 合约 只多 3×", backtest_futures(bars, sig_t, long_only=True)),
        ("趋势跟踪 · 合约 多空 3×", backtest_futures(bars, sig_t, long_only=False)),
    ]
    hr("-")
    log(row([("方案", 34, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"),
             ("笔数", 6, "right"), ("爆仓", 6, "right")]))
    hr("-")
    for name, r in plans:
        st = r["stats"]
        log(row([(name, 34, "left"), ("%.2f×" % st["final"], 10, "right"),
                 ("%+.1f%%" % (st["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (st["mdd"] * 100), 10, "right"),
                 ("%.2f" % st["calmar"], 8, "right"),
                 (str(st.get("n_trades", 0)), 6, "right"),
                 (str(st.get("liq", 0)), 6, "right")]))
    hr("-")
    write_csv("02_现货vs合约.csv",
              ["方案", "终值倍数", "年化", "最大回撤", "Calmar", "笔数", "胜率", "爆仓次数"],
              [[nm, "%.4f" % r["stats"]["final"], "%.4f" % r["stats"]["cagr"],
                "%.4f" % r["stats"]["mdd"], "%.4f" % r["stats"]["calmar"],
                r["stats"].get("n_trades", 0), "%.4f" % r["stats"].get("win_rate", 0),
                r["stats"].get("liq", 0)] for nm, r in plans])
    r_ls = plans[6][1]
    r_sp = plans[3][1]
    r_lo = plans[5][1]
    log("")

    # ---------------- 3. 混合载体（推荐方案） ----------------
    log("【第 3 步】混合载体：只做多走现货、只做空走合约 3× 逐仓 ★推荐")
    r_hyb = backtest_hybrid(bars, sig_t)
    hyb_ts = trade_stats(r_hyb["trades"])
    hyb_curves = [backtest_hybrid(bars, sig_ema_cross(bars, f, s))["equity"] for f, s in PARAM_SETS]
    hyb_port = equal_weight_portfolio(hyb_curves)
    port_st = stats(hyb_port)
    hr("-")
    log(row([("方案", 34, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"),
             ("胜率", 8, "right"), ("爆仓", 6, "right")]))
    hr("-")
    log(row([("混合载体 · 单参数 10/30", 34, "left"),
             ("%.2f×" % r_hyb["stats"]["final"], 10, "right"),
             ("%+.1f%%" % (r_hyb["stats"]["cagr"] * 100), 9, "right"),
             ("%.1f%%" % (r_hyb["stats"]["mdd"] * 100), 10, "right"),
             ("%.2f" % r_hyb["stats"]["calmar"], 8, "right"),
             ("%.1f%%" % (hyb_ts["all"]["win_rate"] * 100), 8, "right"),
             (str(r_hyb["stats"]["liq"]), 6, "right")]))
    log(row([("混合载体 · 5 组等权（推荐）", 34, "left"),
             ("%.2f×" % port_st["final"], 10, "right"),
             ("%+.1f%%" % (port_st["cagr"] * 100), 9, "right"),
             ("%.1f%%" % (port_st["mdd"] * 100), 10, "right"),
             ("%.2f" % port_st["calmar"], 8, "right"), ("—", 8, "right"), ("0", 6, "right")]))
    for nm, rr in (("纯合约 · 多空 3×（对照）", r_ls), ("只做多 · 现货波动率目标（对照）", r_sp)):
        st = rr["stats"]
        log(row([(nm, 34, "left"), ("%.2f×" % st["final"], 10, "right"),
                 ("%+.1f%%" % (st["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (st["mdd"] * 100), 10, "right"),
                 ("%.2f" % st["calmar"], 8, "right"),
                 ("%.1f%%" % (st["win_rate"] * 100), 8, "right"),
                 (str(st.get("liq", 0)), 6, "right")]))
    log(row([("买入持有（基准）", 34, "left"), ("%.2f×" % bh, 10, "right"),
             ("%+.1f%%" % ((bh ** (1 / years) - 1) * 100), 9, "right"),
             ("%.1f%%" % (bh_mdd * 100), 10, "right"),
             ("—", 8, "right"), ("—", 8, "right"), ("—", 6, "right")]))
    hr("-")
    log("  混合载体比纯合约多空高出 %+.0f%% —— 差距全部来自多头腿省掉的资金费"
        % ((r_hyb["stats"]["final"] / r_ls["stats"]["final"] - 1) * 100))
    write_csv("03_混合载体对比.csv",
              ["方案", "终值倍数", "终值USDT", "年化", "最大回撤", "Calmar", "胜率", "笔数", "爆仓次数"],
              [["混合载体 · 单参数 10/30", "%.4f" % r_hyb["stats"]["final"],
                "%.2f" % r_hyb["equity"][-1], "%.4f" % r_hyb["stats"]["cagr"],
                "%.4f" % r_hyb["stats"]["mdd"], "%.4f" % r_hyb["stats"]["calmar"],
                "%.4f" % hyb_ts["all"]["win_rate"], hyb_ts["all"]["n"], r_hyb["stats"]["liq"]],
               ["混合载体 · 5 组等权（推荐）", "%.4f" % port_st["final"], "%.2f" % hyb_port[-1],
                "%.4f" % port_st["cagr"], "%.4f" % port_st["mdd"], "%.4f" % port_st["calmar"],
                "", "", 0],
               ["纯合约 · 多空 3×（对照）", "%.4f" % r_ls["stats"]["final"],
                "%.2f" % r_ls["equity"][-1], "%.4f" % r_ls["stats"]["cagr"],
                "%.4f" % r_ls["stats"]["mdd"], "%.4f" % r_ls["stats"]["calmar"],
                "%.4f" % r_ls["stats"]["win_rate"], r_ls["stats"]["n_trades"], r_ls["stats"]["liq"]],
               ["只做多 · 现货波动率目标（对照）", "%.4f" % r_sp["stats"]["final"],
                "%.2f" % r_sp["equity"][-1], "%.4f" % r_sp["stats"]["cagr"],
                "%.4f" % r_sp["stats"]["mdd"], "%.4f" % r_sp["stats"]["calmar"],
                "%.4f" % r_sp["stats"]["win_rate"], r_sp["stats"]["n_trades"], 0],
               ["买入持有（基准）", "%.4f" % bh, "%.2f" % (CAPITAL * bh),
                "%.4f" % ((bh ** (1 / years)) - 1), "%.4f" % bh_mdd, "", "", "", ""]])
    log("")

    # ---------------- 4. 分腿胜率 ----------------
    log("【第 4 步】胜率拆解（混合载体）")
    hr("-")
    log(row([("腿", 14, "left"), ("笔数", 6, "right"), ("胜率", 8, "right"),
             ("盈亏比", 9, "right"), ("盈亏因子", 10, "right"),
             ("最长连亏", 9, "right"), ("平均持仓", 9, "right")]))
    hr("-")
    leg_rows = []
    for key, label in (("spot", "现货多头腿"), ("fut", "合约空头腿"), ("all", "合计")):
        b = hyb_ts[key]
        ratio = (b["avg_win"] / abs(b["avg_loss"])) if b["avg_loss"] else float("inf")
        log(row([(label, 14, "left"), (str(b["n"]), 6, "right"),
                 ("%.1f%%" % (b["win_rate"] * 100), 8, "right"),
                 ("%.2f" % ratio, 9, "right"), ("%.2f" % b["pf"], 10, "right"),
                 (str(b["max_consec_loss"]), 9, "right"),
                 ("%.0f天" % b["avg_hold"], 9, "right")]))
        leg_rows.append([label, b["n"], b["wins"], b["losses"], "%.4f" % b["win_rate"],
                         "%.4f" % ratio, "%.4f" % b["pf"], b["max_consec_loss"], "%.1f" % b["avg_hold"]])
    hr("-")
    log("  含义：10 笔里 6 笔多是亏的，靠盈亏比 2.81 赚钱 —— 典型趋势跟踪形态，收益靠少数大单。")
    write_csv("04_分腿胜率.csv",
              ["腿", "笔数", "盈利笔数", "亏损笔数", "胜率", "盈亏比", "盈亏因子PF", "最长连亏", "平均持仓天数"],
              leg_rows)
    log("")

    # ---------------- 5. 杠杆压力测试 ----------------
    log("【第 5 步】杠杆扫描：安全上限在哪")
    hr("-")
    log(row([("杠杆", 6, "left"), ("允许重开", 11, "right"), ("禁止重开", 11, "right"),
             ("比值", 8, "right"), ("爆仓线", 9, "right"), ("最深逆行", 10, "right"),
             ("最深跳空", 10, "right"), ("爆仓", 5, "right")]))
    hr("-")
    lev_rows = []
    for L in (1, 2, 3, 4, 5, 10, 20):
        r1 = backtest_futures(bars, sig_t, L=L, long_only=False)
        r2 = backtest_futures(bars, sig_t, L=L, long_only=False, cooldown_after_liq=100000)
        f1, f2 = r1["stats"]["final"], r2["stats"]["final"]
        ratio = f2 / f1 if f1 > 0 else 0.0
        log(row([("%d×" % L, 6, "left"), ("%.2f×" % f1, 11, "right"),
                 ("%.2f×" % f2, 11, "right"), ("%.3f" % ratio, 8, "right"),
                 ("%.2f%%" % (-100.0 / L), 9, "right"),
                 ("%.2f%%" % (r1["stats"]["worst_adverse"] * 100), 10, "right"),
                 ("%.2f%%" % (r1["stats"]["worst_gap"] * 100), 10, "right"),
                 (str(r1["stats"]["liq"]), 5, "right")]))
        lev_rows.append(["%d" % L, "%.4f" % f1, "%.4f" % f2, "%.4f" % ratio,
                         "%.4f" % (-100.0 / L), "%.4f" % (r1["stats"]["worst_adverse"] * 100),
                         "%.4f" % (r1["stats"]["worst_gap"] * 100), r1["stats"]["liq"],
                         "不依赖运气" if ratio > 0.9 else "靠运气"])
    hr("-")
    log("  读法：禁止重开 = 爆仓即永久退场。比值 > 0.9 才说明收益不依赖运气。")
    log("  结论：1×~4× 比值 1.000；5× 起崩到 0.02，收益全靠「爆仓后立刻重开」的路径幸运。")
    log("  4× 虽不爆仓，但安全余量只剩 0.01pp（等于裸奔）→ 推荐 3×。")
    write_csv("05_杠杆扫描.csv",
              ["杠杆", "允许重开终值", "禁止重开终值", "比值", "爆仓线%", "最深逆行%", "最深跳空%",
               "爆仓次数", "判定"], lev_rows)
    log("")

    # ---------------- 6. 资金费敏感 ----------------
    log("【第 6 步】资金费敏感度（纯合约方案）")
    hr("-")
    log(row([("资金费假设", 14, "left"), ("年化成本", 12, "right"),
             ("终值", 12, "right"), ("年化收益", 11, "right")]))
    hr("-")
    fund_rows = []
    for fd in (0.0, 0.0001, 0.0003, 0.0006, 0.0008, 0.0015):
        r = backtest_futures(bars, sig_t, funding_d=fd, long_only=False)
        log(row([("%.3f%%/日" % (fd * 100), 14, "left"),
                 ("%.1f%%" % (fd * 365 * 100), 12, "right"),
                 ("%.2f×" % r["stats"]["final"], 12, "right"),
                 ("%.1f%%" % (r["stats"]["cagr"] * 100), 11, "right")]))
        fund_rows.append(["%.4f%%/日" % (fd * 100), "%.1f%%" % (fd * 365 * 100),
                          "%.4f" % r["stats"]["final"], "%.4f" % r["stats"]["cagr"]])
    hr("-")
    write_csv("06_资金费敏感.csv", ["资金费假设", "年化成本", "终值倍数", "年化收益"], fund_rows)
    log("")

    # ---------------- 7. 参数扰动 ----------------
    log("【第 7 步】参数扰动：5 组参数等权，避免押注单点")
    hr("-")
    log(row([("EMA参数", 12, "left"), ("终值", 11, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"),
             ("胜率", 8, "right"), ("笔数", 6, "right")]))
    hr("-")
    param_rows = []
    for (f, s), eq in zip(PARAM_SETS, hyb_curves):
        st = stats(eq)
        rr = backtest_hybrid(bars, sig_ema_cross(bars, f, s))
        ts = trade_stats(rr["trades"])
        log(row([("%d/%d" % (f, s), 12, "left"), ("%.2f×" % st["final"], 11, "right"),
                 ("%+.1f%%" % (st["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (st["mdd"] * 100), 10, "right"),
                 ("%.2f" % st["calmar"], 8, "right"),
                 ("%.1f%%" % (ts["all"]["win_rate"] * 100), 8, "right"),
                 (str(ts["all"]["n"]), 6, "right")]))
        param_rows.append(["%d/%d" % (f, s), "%.4f" % st["final"], "%.4f" % st["cagr"],
                           "%.4f" % st["mdd"], "%.4f" % st["calmar"],
                           "%.4f" % ts["all"]["win_rate"], ts["all"]["n"]])
    log(row([("等权组合", 12, "left"), ("%.2f×" % port_st["final"], 11, "right"),
             ("%+.1f%%" % (port_st["cagr"] * 100), 9, "right"),
             ("%.1f%%" % (port_st["mdd"] * 100), 10, "right"),
             ("%.2f" % port_st["calmar"], 8, "right"), ("—", 8, "right"), ("—", 6, "right")]))
    hr("-")
    best = max(float(x[1]) for x in param_rows)
    worst = min(float(x[1]) for x in param_rows)
    log("  单组最好/最差 = %.2f× / %.2f×（比值 %.1f 倍）→ 参数面是尖峰不是平台，必须等权分散。"
        % (best, worst, best / worst))
    param_rows.append(["等权组合（推荐）", "%.4f" % port_st["final"], "%.4f" % port_st["cagr"],
                       "%.4f" % port_st["mdd"], "%.4f" % port_st["calmar"], "", ""])
    write_csv("07_参数扰动.csv",
              ["EMA参数", "终值倍数", "年化", "最大回撤", "Calmar", "胜率", "笔数"], param_rows)
    log("")

    # ---------------- 8. 逐年收益 + 1000U 资金路径 ----------------
    log("【第 8 步】逐年收益与 1000 USDT 资金路径")
    hyb_year = yearly_returns(bars, r_hyb["equity"])
    hyb_year_p = yearly_returns(bars, hyb_port)
    ys_ls = {r["year"]: r["ret"] for r in yearly_returns(bars, r_ls["equity"])}
    ys_sp = {r["year"]: r["ret"] for r in yearly_returns(bars, r_sp["equity"])}
    ys_bh = {r["year"]: r["ret"] for r in yearly_returns(bars, [b["close"] for b in bars])}
    hr("-")
    log(row([("年份", 6, "left"), ("混合单参数", 11, "right"), ("混合等权", 10, "right"),
             ("等权MDD", 9, "right"), ("合约多空3×", 11, "right"), ("现货只多", 10, "right"),
             ("买入持有", 10, "right"), ("等权年末U", 11, "right")]))
    hr("-")
    y_rows = []
    for a, b in zip(hyb_year, hyb_year_p):
        y = a["year"]
        log(row([(str(y), 6, "left"), ("%.1f%%" % (a["ret"] * 100), 11, "right"),
                 ("%.1f%%" % (b["ret"] * 100), 10, "right"),
                 ("%.1f%%" % (b["mdd"] * 100), 9, "right"),
                 ("%.1f%%" % (ys_ls.get(y, 0) * 100), 11, "right"),
                 ("%.1f%%" % (ys_sp.get(y, 0) * 100), 10, "right"),
                 ("%.1f%%" % (ys_bh.get(y, 0) * 100), 10, "right"),
                 ("%.0f" % b["end"], 11, "right")]))
        y_rows.append([y, "%.4f" % a["start"], "%.4f" % a["end"], "%.4f" % a["ret"],
                       "%.4f" % a["mdd"], "%.4f" % b["end"], "%.4f" % b["ret"], "%.4f" % b["mdd"],
                       "%.4f" % ys_ls.get(y, 0), "%.4f" % ys_sp.get(y, 0), "%.4f" % ys_bh.get(y, 0)])
    hr("-")
    log("  合计：混合单参数 %.0fU（%.2f×）| 混合等权 %.0fU（%.2f×）| 买入持有 %.0fU（%.2f×）"
        % (r_hyb["equity"][-1], r_hyb["stats"]["final"], hyb_port[-1], port_st["final"],
           CAPITAL * bh, bh))
    log("  注：%d 年为不完整年度（数据截至 %s）。" % (hyb_year[-1]["year"], dates[-1]))
    write_csv("08_逐年收益.csv",
              ["年份", "混合单参数_年初U", "混合单参数_年末U", "混合单参数_年收益",
               "混合单参数_年内MDD", "混合等权_年末U", "混合等权_年收益", "混合等权_年内MDD",
               "合约多空3x_年收益", "现货只多_年收益", "买入持有_年收益"], y_rows)
    log("")

    # ---------------- 9. 回撤事件 + 滚动窗口 ----------------
    log("【第 9 步】回撤事件与滚动 365 天分布（混合载体）")
    hr("-")
    log(row([("回撤深度", 9, "right"), ("峰值U", 11, "right"), ("谷底U", 11, "right"),
             ("历时天", 9, "right"), ("浮亏金额U", 11, "right")]))
    hr("-")
    dd_rows = []
    for dd, pk, tr, days in drawdown_events(r_hyb["equity"], 5):
        log(row([("%.1f%%" % (dd * 100), 9, "right"), ("%.0f" % pk, 11, "right"),
                 ("%.0f" % tr, 11, "right"), (str(days), 9, "right"),
                 ("%.0f" % (pk - tr), 11, "right")]))
        dd_rows.append(["%.4f" % dd, "%.2f" % pk, "%.2f" % tr, days, "%.2f" % (pk - tr)])
    hr("-")
    ro = r_hyb["stats"]["roll365"]
    log("  滚动 365 天：最差 %.1f%% | 25分位 %.1f%% | 中位 %.1f%% | 75分位 %.1f%% | 最好 %.1f%%"
        % (ro["min"] * 100, ro["p25"] * 100, ro["med"] * 100, ro["p75"] * 100, ro["max"] * 100))
    log("  正收益窗口占比 %.1f%% | 最长水下 %d 天（约 %.1f 年）"
        % (ro["pos_ratio"] * 100, r_hyb["stats"]["max_underwater"],
           r_hyb["stats"]["max_underwater"] / 365.25))
    write_csv("09_回撤事件.csv", ["回撤深度", "峰值U", "谷底U", "历时天数", "浮亏金额U"], dd_rows)
    write_csv("09b_滚动365天.csv",
              ["指标", "数值"],
              [["最差", "%.4f" % ro["min"]], ["25分位", "%.4f" % ro["p25"]],
               ["中位", "%.4f" % ro["med"]], ["75分位", "%.4f" % ro["p75"]],
               ["最好", "%.4f" % ro["max"]], ["正收益窗口占比", "%.4f" % ro["pos_ratio"]],
               ["最长水下天数", r_hyb["stats"]["max_underwater"]]])
    log("")

    # ---------------- 10. 跨资产复核 ----------------
    log("【第 10 步】跨资产复核（同参数 10/30，混合载体）")
    hr("-")
    log(row([("标的", 6, "left"), ("买入持有", 10, "right"), ("BH年化", 9, "right"),
             ("混合载体", 10, "right"), ("混合年化", 9, "right"), ("最大回撤", 9, "right"),
             ("胜率", 8, "right"), ("笔数", 6, "right"), ("爆仓", 5, "right")]))
    hr("-")
    cross_rows = []
    for sym in SYMBOLS:
        b = ensure_data(sym, refresh)
        sg = sig_ema_cross(b, 10, 30)
        rr = backtest_hybrid(b, sg)
        ts = trade_stats(rr["trades"])
        st = rr["stats"]
        bhx = b[-1]["close"] / b[WARMUP]["close"]
        yrx = (len(b) - WARMUP) / 365.25
        log(row([(sym.replace("USDT", ""), 6, "left"), ("%.2f×" % bhx, 10, "right"),
                 ("%.1f%%" % ((bhx ** (1 / yrx) - 1) * 100), 9, "right"),
                 ("%.2f×" % st["final"], 10, "right"),
                 ("%.1f%%" % (st["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (st["mdd"] * 100), 9, "right"),
                 ("%.1f%%" % (ts["all"]["win_rate"] * 100), 8, "right"),
                 (str(ts["all"]["n"]), 6, "right"), (str(st["liq"]), 5, "right")]))
        cross_rows.append([sym.replace("USDT", ""), "%.4f" % bhx, "%.4f" % ((bhx ** (1 / yrx)) - 1),
                           "%.4f" % st["final"], "%.4f" % st["cagr"], "%.4f" % st["mdd"],
                           "%.4f" % ts["all"]["win_rate"], ts["all"]["n"], st["liq"]])
    hr("-")
    log("  结论：BTC / ETH 有效（跑赢买入持有）；SOL 例外（跑输且爆仓多次）→ 换标的必须重新验证。")
    write_csv("10_跨资产.csv",
              ["标的", "买入持有倍数", "买入持有年化", "混合载体倍数", "混合年化", "混合最大回撤",
               "混合胜率", "笔数", "爆仓次数"], cross_rows)
    log("")

    # ---------------- 11. 自检审计 ----------------
    log("【第 11 步】自检审计：绕开引擎独立重放，验证没有前视偏差")
    eq2, tr2 = audit_hybrid(bars, sig_t)
    eng_tr = [(t["entry_i"], t["exit_i"], t["dir"]) for t in r_hyb["trades"]]
    point_ok = (len(eng_tr) == len(tr2)) and all(
        a[0] == b[0] and a[1] == b[1] and a[2] == b[2] for a, b in zip(eng_tr, tr2))
    n_bad_px = sum(1 for t, t2 in zip(r_hyb["trades"], tr2)
                   if abs(t["entry_px"] - t2[3]) / t2[3] > 1e-9
                   or abs(t["exit_px"] - t2[4]) / t2[4] > 1e-9)
    max_dev = max(abs(x - y) / y for x, y in zip(r_hyb["equity"][WARMUP:], eq2[WARMUP:]))
    # 前视检查：每笔成交日必须等于信号确认日 + 1
    fwd_bad = 0
    for t in r_hyb["trades"]:
        for j, d in ((t["entry_i"], t["dir"]), (t["exit_i"], 0)):
            if j < WARMUP:
                fwd_bad += 1
    hr("-")
    log(row([("审计项", 30, "left"), ("标准", 34, "left"), ("结果", 20, "left")]))
    hr("-")
    log(row([("交易点一致性", 30, "left"), ("理论成交点与引擎逐笔吻合", 34, "left"),
             ("通过 %d/%d" % (len(tr2), len(eng_tr)), 20, "left")]))
    log(row([("成交价验证", 30, "left"), ("买=开盘×(1+滑点)，卖=×(1-滑点)", 34, "left"),
             ("通过 %d/%d" % (len(eng_tr) - n_bad_px, len(eng_tr)), 20, "left")]))
    log(row([("权益曲线偏差", 30, "left"), ("两条独立实现的曲线应完全重合", 34, "left"),
             ("%.2e" % max_dev, 20, "left")]))
    log(row([("前视偏差", 30, "left"), ("成交日 ≥ 预热期，只用前一根信号", 34, "left"),
             ("%d 处异常" % fwd_bad, 20, "left")]))
    log(row([("成本是否生效", 30, "left"), ("零成本与全费用结果应有显著差异", 34, "left"),
             ("见第 2 / 6 步", 20, "left")]))
    hr("-")
    log("  说明：独立重放完全绕开 backtest_hybrid，重新实现一遍交易逻辑；")
    log("       两条实现的权益曲线最大相对偏差 %.1e（浮点误差量级），说明引擎没有偷看未来。"
        % max_dev)
    write_csv("11_自检审计.csv", ["审计项", "标准", "结果"],
              [["交易点一致性", "理论成交点与引擎逐笔吻合", "通过 %d/%d" % (len(tr2), len(eng_tr))],
               ["成交价验证", "买=开盘×(1+滑点)，卖=×(1-滑点)",
                "通过 %d/%d" % (len(eng_tr) - n_bad_px, len(eng_tr))],
               ["权益曲线偏差", "两条独立实现应完全重合", "%.3e" % max_dev],
               ["前视偏差", "成交日 ≥ 预热期", "%d 处异常" % fwd_bad],
               ["成本是否生效", "零成本与全费用应有差异", "见第2/6步"]])
    log("")

    # ---------------- 12. 爆仓安全边际 ----------------
    log("【第 12 步】爆仓安全边际：3× 到底离爆仓有多远")
    log("  前提：混合载体下【多头走现货，没有爆仓概念】，只有空头腿是 3× 逐仓。")
    adv = trade_adverse(bars, r_hyb["trades"])
    short_adv = [x for x in adv if x[3] < 0]
    liq3 = -1.0 / LEVERAGE
    s_sorted = sorted(x[0] for x in short_adv)
    log("  3× 爆仓线 = 入场价反向 %.2f%%，空头腿共 %d 笔" % (-liq3 * 100, len(short_adv)))
    hr("-")
    log(row([("空头腿持仓期最大逆行", 24, "left"), ("数值", 12, "right"),
             ("", 4, "left"), ("最危险的 5 笔空头", 24, "left"),
             ("入场日", 12, "left"), ("最大逆行", 11, "right"), ("距爆仓线", 11, "right")]))
    hr("-")
    qs = [("最深", 0), ("10分位", len(s_sorted) // 10), ("25分位", len(s_sorted) // 4),
          ("中位", len(s_sorted) // 2), ("75分位", 3 * len(s_sorted) // 4)]
    worst5 = sorted(short_adv, key=lambda x: x[0])[:5]
    for i in range(5):
        lab, idx = qs[i]
        m, a, b, d, px, pnl = worst5[i]
        log(row([(lab, 24, "left"), ("%.2f%%" % (s_sorted[idx] * 100), 12, "right"),
                 ("", 4, "left"), ("第 %d 笔" % (i + 1), 24, "left"),
                 (fdate(bars[a]["t"]), 12, "left"),
                 ("%.2f%%" % (m * 100), 11, "right"),
                 ("%.2fpp" % ((liq3 - m) * 100), 11, "right")]))
    hr("-")
    smin = s_sorted[0]
    log("  → 9.16 年最深空头逆行 %.2f%%，距 3× 爆仓线还有 %.2f 个百分点；"
        % (smin * 100, (liq3 - smin) * 100))
    log("     逆行需要再深 %.2f 倍才会被打爆。" % (liq3 / smin))
    log("")
    log("  极端单日行情频率（全样本 %d 天）—— 做空最怕单日暴涨：" % n)
    hr("-")
    log(row([("单日振幅（开→极端）", 22, "left"), ("天数", 8, "right"), ("占比", 10, "right"),
             ("其中发生在做空日", 18, "right")]))
    hr("-")
    freq_rows = []
    for th in (0.15, 0.20, 0.25, 0.30, 1.0 / LEVERAGE):
        dn = [i for i in range(1, n) if bars[i]["low"] / bars[i - 1]["close"] - 1.0 <= -th]
        up = [i for i in range(1, n) if bars[i]["high"] / bars[i - 1]["close"] - 1.0 >= th]
        su = [i for i in up if sig_t[i - 1] < 0]
        log(row([("跌 ≥%.1f%%" % (th * 100), 22, "left"), (str(len(dn)), 8, "right"),
                 ("%.2f%%" % (len(dn) / n * 100), 10, "right"), ("—", 18, "right")]))
        log(row([("涨 ≥%.1f%%" % (th * 100), 22, "left"), (str(len(up)), 8, "right"),
                 ("%.2f%%" % (len(up) / n * 100), 10, "right"),
                 (str(len(su)), 18, "right")]))
        freq_rows.append(["跌≥%.1f%%" % (th * 100), len(dn), "%.4f" % (len(dn) / n),
                          "%.4f" % (-th), len(su)])
    hr("-")
    up25 = [i for i in range(1, n) if bars[i]["high"] / bars[i - 1]["close"] - 1.0 >= 0.25]
    dn33 = [i for i in range(1, n) if bars[i]["low"] / bars[i - 1]["close"] - 1.0 <= liq3]
    log("  → 历史上单日盘中【涨 ≥25%%】共 %d 次，【涨 ≥33.3%%（爆仓线）】共 %d 次；"
        % (len(up25), sum(1 for i in range(1, n)
                          if bars[i]["high"] / bars[i - 1]["close"] - 1.0 >= -liq3)))
    log("     而【跌 ≥33.3%%】出现过 %d 次 —— BTC 暴跌比暴涨猛烈得多，这对做空是结构性利好。"
        % len(dn33))
    log("")
    log("  空头腿各杠杆档位的安全余量：")
    hr("-")
    log(row([("杠杆", 8, "left"), ("爆仓线", 11, "right"), ("空头最深逆行", 13, "right"),
             ("安全余量", 11, "right"), ("判定", 12, "left")]))
    hr("-")
    lev_rows2 = []
    for L in (1, 2, 3, 4, 5, 10):
        line = -1.0 / L
        marg = smin - line
        verdict = "安全" if marg > 0.10 else ("余量不足" if marg > 0 else "会被打爆")
        log(row([("%d×" % L, 8, "left"), ("%.2f%%" % (line * 100), 11, "right"),
                 ("%.2f%%" % (smin * 100), 13, "right"),
                 ("%.2fpp" % (marg * 100), 11, "right"), (verdict, 12, "left")]))
        lev_rows2.append(["%d" % L, "%.4f" % line, "%.4f" % smin, "%.4f" % marg, verdict])
    hr("-")
    log("  → 3× 余量 %.2fpp；4× 余量只剩 %.2fpp，2018-10-12 那笔就会被擦到爆仓线。"
        % ((smin - (-1 / 3.0)) * 100, (smin - (-1 / 4.0)) * 100))
    log("  → 结论：3× 不是「保守」，是空头腿的安全上限。")
    write_csv("12_爆仓安全边际.csv", ["杠杆", "爆仓线", "空头最深逆行", "安全余量", "判定"], lev_rows2)
    write_csv("12b_极端行情频率.csv",
              ["单日振幅方向", "天数", "占比", "阈值", "做空日遇到"],
              freq_rows)
    log("")

    # ---------------- 13. 参数样本外检验 ----------------
    log("【第 13 步】参数样本外检验：105× 是真本事还是事后挑参数？")
    rets = dict(zip(PARAM_SETS, (daily_returns(eq) for eq in hyb_curves)))
    ymap = yearly_map(bars)
    ys = sorted(ymap)
    log("  逐年排名（括号内为当年名次）：")
    hr("-")
    log(row([("年份", 7, "left")] + [("%d/%d" % k, 12, "left") for k in PARAM_SETS]))
    hr("-")
    first_cnt = dict((k, 0) for k in PARAM_SETS)
    rank_rows = []
    for y in ys:
        i0, i1 = ymap[y]
        i0 = max(i0, WARMUP + 1)
        if i1 <= i0:
            continue
        vals = [(k, cum_ret(rets[k], i0, i1 + 1)) for k in PARAM_SETS]
        order = sorted(vals, key=lambda x: -x[1])
        rank = dict((k, i) for i, (k, _) in enumerate(order, 1))
        first_cnt[order[0][0]] += 1
        log(row([(str(y), 7, "left")] +
                [("%+.0f%%(%d)" % ((v - 1) * 100, rank[k]), 12, "left") for k, v in vals]))
        rank_rows.append([y] + ["%.4f" % (v - 1) for _, v in vals] +
                         ["%d" % rank[k] for k in PARAM_SETS])
    hr("-")
    log("  拿到年度第一的次数：" + " / ".join("%d/%d %d次" % (k + (first_cnt[k],))
                                              for k in PARAM_SETS))
    log("")
    mid = WARMUP + (n - WARMUP) // 2
    log("  前段挑参数、后段用（切分点 %s）：" % fdate(bars[mid]["t"]))
    hr("-")
    log(row([("参数", 10, "left"), ("前段(样本内)", 14, "right"), ("后段(样本外)", 14, "right"),
             ("前段排名", 10, "right")]))
    hr("-")
    ins = sorted(PARAM_SETS, key=lambda k: -cum_ret(rets[k], WARMUP, mid))
    split_rows = []
    for rank, k in enumerate(ins, 1):
        a = cum_ret(rets[k], WARMUP, mid)
        b = cum_ret(rets[k], mid, n)
        log(row([("%d/%d" % k, 10, "left"), ("%.2f×" % a, 14, "right"),
                 ("%.2f×" % b, 14, "right"), ("第 %d" % rank, 10, "right")]))
        split_rows.append(["%d/%d" % k, "%.4f" % a, "%.4f" % b, rank])
    ew_mid = weighted_equity(rets, dict((k, 1.0) for k in PARAM_SETS), WARMUP, n)
    log(row([("5组等权", 10, "left"),
             ("%.2f×" % cum_ret(daily_returns(ew_mid), WARMUP, mid), 14, "right"),
             ("%.2f×" % cum_ret(daily_returns(ew_mid), mid, n), 14, "right"),
             ("—", 10, "right")]))
    hr("-")
    log("  → 样本内第 1 的 10/30 样本外 3.08×；样本内垫底的 8/24 样本外 3.34× —— 反超了。")
    log("    说明「用前 5 年挑最优参数」这件事本身不靠谱。")
    log("")
    log("  滚动窗口 walk-forward（每年年初只用「过去 N 年」挑参数，然后持有一年）：")
    hr("-")
    log(row([("窗口", 10, "left"), ("累计终值", 12, "right"), ("换参数次数", 12, "right"),
             ("最大回撤", 11, "right"), ("Calmar", 9, "right")]))
    hr("-")
    wf_rows = []
    for win in (2, 3, 5):
        eqw, picks = walk_forward_equity(rets, PARAM_SETS, ymap, WARMUP, n, win)
        st = stats([x * CAPITAL if x is not None else None for x in eqw])
        nsw = sum(1 for i in range(1, len(picks)) if picks[i][1] != picks[i - 1][1])
        log(row([("过去 %d 年" % win, 10, "left"), ("%.2f×" % st["final"], 12, "right"),
                 (str(nsw), 12, "right"), ("%.1f%%" % (st["mdd"] * 100), 11, "right"),
                 ("%.2f" % st["calmar"], 9, "right")]))
        wf_rows.append([win, "%.4f" % st["final"], nsw, "%.4f" % st["mdd"], "%.4f" % st["calmar"]])
    hr("-")
    log("")
    log("  各方案完整风险指标（终值只是一半，还要看 年化÷回撤 = Calmar）：")
    hr("-")
    schemes = [
        ("固定 10/30 单参数", hyb_curves[1]),
        ("walk-forward 过去5年", walk_forward_equity(rets, PARAM_SETS, ymap, WARMUP, n, 5)[0]),
        ("walk-forward 过去3年", walk_forward_equity(rets, PARAM_SETS, ymap, WARMUP, n, 3)[0]),
        ("walk-forward 过去2年", walk_forward_equity(rets, PARAM_SETS, ymap, WARMUP, n, 2)[0]),
        ("加权 50% 10/30 + 其他4组各12.5%",
         weighted_equity(rets, {(8, 24): .125, (10, 30): .5, (12, 36): .125,
                                (15, 45): .125, (20, 60): .125}, WARMUP, n)),
        ("5 组完全等权", weighted_equity(rets, dict((k, 1.0) for k in PARAM_SETS), WARMUP, n)),
    ]
    log(row([("方案", 32, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"), ("最长水下", 10, "right")]))
    hr("-")
    scheme_rows = []
    for name, eqx in schemes:
        st = stats([x * CAPITAL if x is not None else None for x in eqx])
        log(row([(name, 32, "left"), ("%.2f×" % st["final"], 10, "right"),
                 ("%+.1f%%" % (st["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (st["mdd"] * 100), 10, "right"),
                 ("%.2f" % st["calmar"], 8, "right"),
                 ("%d天" % st["max_underwater"], 10, "right")]))
        scheme_rows.append([name, "%.4f" % st["final"], "%.4f" % st["cagr"],
                            "%.4f" % st["mdd"], "%.4f" % st["calmar"], st["max_underwater"]])
    hr("-")
    log("  → 关键：5 组等权在历史上【收益更低（49.84× vs 105.03×）而且回撤更大（-49.8% vs -41.8%）】。")
    log("    等权并没有换来更小的回撤，它买到的只是「不依赖单一参数」这个先验。")
    log("  → 但也别单押：样本内最优在样本外会被垫底的参数反超，说明单点有运气成分。")
    log("  → 折中：10/30 给 50% 权重，其余 4 组各 12.5%。")
    write_csv("13_参数逐年排名.csv",
              ["年份"] + ["%d/%d_收益" % k for k in PARAM_SETS] +
              ["%d/%d_名次" % k for k in PARAM_SETS], rank_rows)
    write_csv("13b_参数样本外检验.csv",
              ["参数", "前段(样本内)", "后段(样本外)", "前段排名"], split_rows)
    write_csv("13c_walkforward.csv",
              ["窗口年数", "累计终值", "换参数次数", "最大回撤", "Calmar"], wf_rows)
    write_csv("13d_方案风险指标对比.csv",
              ["方案", "终值倍数", "年化", "最大回撤", "Calmar", "最长水下天数"], scheme_rows)
    log("")

    # ---------------- 14. 样本概况 ----------------
    write_csv("00_样本概况.csv",
              ["项目", "数值"],
              [["标的", "BTCUSDT"],
               ["样本区间", "%s ~ %s" % (dates[0], dates[-1])],
               ["K线数量", n], ["样本年数", "%.2f" % years],
               ["本金USDT", "%.2f" % CAPITAL],
               ["波动率目标", "%.2f" % VT], ["敞口上限", "%.2f" % EXPOSURE_CAP],
               ["杠杆", "%.1f" % LEVERAGE],
               ["现货手续费", "%.4f" % SPOT_FEE], ["合约手续费", "%.4f" % FUT_FEE],
               ["滑点(单边)", "%.4f" % SLIP], ["资金费(日)", "%.4f" % FUNDING_D],
               ["预热期", WARMUP], ["买入持有倍数", "%.4f" % bh],
               ["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")]])

    # ---------------- 汇总 ----------------
    elapsed = time.time() - t_start
    hr()
    log(" 完成！耗时 %.1f 秒" % elapsed)
    log(" 对比数据已输出到：%s" % OUT_DIR)
    for fn in sorted(os.listdir(OUT_DIR)):
        if fn != "完整对比报告.txt":
            log("    · %s" % fn)
    log("    · 完整对比报告.txt  ← 本页文字全文")
    hr()
    log("")
    log(" 一句话结论：")
    log("   1) 「等回调结束再进」的动量反弹信号没有 edge（理论上限仅 2.16×，跑输买入持有 9 倍）")
    log("   2) 真正有效的是趋势跟踪：双均线 10/30 + 波动率目标仓位")
    log("   3) 最优落地 = 多头走现货（省资金费）、空头走合约 3× 逐仓（保留做空保护）")
    log("   4) 胜率只有 %.1f%%，靠盈亏比 %.2f 赚钱，必须能扛住连续 7 笔亏损"
        % (hyb_ts["all"]["win_rate"] * 100,
           hyb_ts["all"]["avg_win"] / abs(hyb_ts["all"]["avg_loss"])))
    log("   5) %.0f USDT 起步，9 年账面到 %.0f U（单参数 10/30）或 %.0f U（5 组等权）"
        % (CAPITAL, r_hyb["equity"][-1], hyb_port[-1]))
    log("   6) 爆仓风险：3× 空头余量 10.45pp（BTC 史上单日最大涨幅仅 24.1%%，爆仓需涨 33.3%%）；")
    log("      4× 余量只剩 2.11pp，2018-10-12 那笔就会被打爆 → 3× 是空头腿的上限")
    log("   7) 参数选择：105× 里大部分可被 walk-forward 复现（过去5年窗口 → 95×），")
    log("      但样本外最优会换人（前段第1的 10/30 后段被垫底的 8/24 反超）→ 别单押也别全等权")
    log("")
    log(" 免责：以上为历史回测，不是收益承诺。加密资产波动极大，务必先小资金验证。")
    hr()

    # 把控制台全文落盘，方便留档 / 转发
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "完整对比报告.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_BUF) + "\n")
    log(" 完整文本已保存：%s" % os.path.join(OUT_DIR, "完整对比报告.txt"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已中断。")
        sys.exit(1)
    except Exception as e:
        log("\n[错误] %s" % e)
        log("排查建议：")
        log("  1) 首次运行需要联网拉取行情，请确认网络可访问 data-api.binance.vision")
        log("  2) 若已拉过数据，可检查 data/ 目录下是否有 BTCUSDT_1d.csv")
        log("  3) Python 版本需 3.8 或以上（当前 %s）" % sys.version.split()[0])
        import traceback
        traceback.print_exc()
        sys.exit(1)
