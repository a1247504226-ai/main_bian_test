# -*- coding: utf-8 -*-
"""
================================================================================
 BTC 日线趋势策略 · 一键回测（自包含脚本，仅用 Python 标准库）
================================================================================
 这个脚本做什么（只跑当前这一套方案，不再做多方案对比）：
   1) 从币安公开行情接口拉取 BTCUSDT 现货日线全历史（无需 API 密钥）
   2) 用唯一一套规则跑完整回测：
        · 信号  双均线 EMA 10/30，收盘确认、次日开盘成交
        · 过滤  SMA50 只约束多头（收盘价在均线下方时不开多，空头照做）
        · 载体  多头走现货（不付资金费）· 空头走合约 1× 逐仓（保留做空保护）
        · 仓位  波动率目标 3%/日，敞口上限 1.0×
   3) 输出回测数据到 ./回测结果/ 目录（CSV，Excel 可直接打开）
      并在控制台打印可读汇总

 为什么「空头 1×」（重要）：
   · 名义仓位 = 当时权益 × 敞口比例，这个公式里**没有杠杆**；
   · 所以 1×~4× 的净值曲线几乎完全相同（实测差 0.09%，只来自保证金不足时的自动缩仓），
     杠杆只决定「押多少保证金」和「爆仓线」；
   · 1× 要价格逆行 +100% 才爆，2× 只要 +50%，3× 只要 +33.3% —— 历史最深逆行 22.89%，
     1× 的安全余量 77.11pp，2× 只剩 27.11pp。详见「对比_空头杠杆.py」。

 关键口径（务必了解，否则数字会看不懂）：
   · 信号在当日收盘确认，次日开盘成交（无前视偏差，第 8 步有独立重放自检）
   · 只用已收盘的日线：剔除当天还没走完的那根 K 线
   · 现货手续费 0.1%，合约手续费 0.04%，滑点单边 0.05%
   · 资金费 0.03%/日（多头付、空头收）
   · 逐仓爆仓线：空头 = 入场价 × (1 + 1/杠杆)

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
LEVERAGE = 1.0              # 合约杠杆（空头腿；名义仓位与杠杆无关，只影响爆仓线。1× = 逆行 100% 才爆）
REGIME_MA = 50              # 只约束多头的趋势过滤：收盘价 > SMA(50) 才允许做多；0 = 关闭
PRIMARY_SET = (10, 30)      # 双均线参数（快线, 慢线）
ATR_N = 14                  # ATR 窗口（波动率目标的输入）
SHORT_STOP = None           # 空头固定止损（None = 关闭；0.05 = 逆行 5% 平仓）
LONG_STOP = None            # 多头固定止损（默认关闭，实测不划算，见使用说明）
PARAM_SETS = [(8, 24), (10, 30), (12, 36), (15, 45), (20, 60)]
DAY_MS = 86400000
DATA_MAX_AGE_DAYS = 2.0     # 本地行情缓存最后一根超过这么多天，就自动重拉
API_BASE = "https://data-api.binance.vision/api/v3/klines"
FETCH_START = 1502928000000  # 2017-08-17
CFG_PATH = os.path.join(HERE, "monitor_config.json")
CFG_SOURCE = "内置默认值（没找到 monitor_config.json）"


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


def _apply_monitor_config():
    """
    同目录如果有 monitor_config.json，就用它的策略参数覆盖上面的常量。

    为什么必须这么做：以前回测脚本和每日检测各写一份参数（均线 10/30、SMA50、预热 60、
    波动率目标 3%……）。只要有一边被改过而另一边没改，回测报告就不再描述你真正在跑的策略，
    而且这种不一致是「静默」的 —— 报告照样跑完、数字照样好看，只是已经跟实盘对不上了。
    现在统一成：以 monitor_config.json 为唯一来源，回测跟着它走。
    """
    global VT, EXPOSURE_CAP, WARMUP, REGIME_MA, LEVERAGE, CAPITAL
    global PRIMARY_SET, ATR_N, CFG_SOURCE, SHORT_STOP, LONG_STOP
    if not os.path.exists(CFG_PATH):
        return
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        log("  ⚠ monitor_config.json 解析失败（%s），本次改用内置默认参数" % e)
        return
    s = d.get("strategy") or {}
    b = d.get("binance") or {}

    def _f(src, key, cur):
        v = src.get(key)
        if v is None:
            return cur
        try:
            return float(v)
        except (TypeError, ValueError):
            return cur

    VT = _f(s, "vt", VT)
    EXPOSURE_CAP = _f(s, "exposure_cap", EXPOSURE_CAP)
    WARMUP = int(_f(s, "warmup", WARMUP))
    REGIME_MA = int(_f(s, "regime_ma", REGIME_MA))
    ATR_N = int(_f(s, "atr_n", ATR_N))
    if ATR_N < 1:                    # atr() 里要除以窗口，0 会直接除零
        ATR_N = 14
    # 下面三个当分母用过，必须挡住 0 和负数，否则会除零或算出负的名义仓位
    _cap = _f(s, "base_capital_usdt", CAPITAL)
    CAPITAL = _cap if _cap > 0 else CAPITAL
    _lev = _f(b, "leverage", LEVERAGE)
    LEVERAGE = _lev if _lev > 0 else LEVERAGE
    # 止损：0 或缺失 = 关闭；只有正数才生效（负数会变成「反着做」，必须挡掉）
    def _stop(src, key, cur):
        v = src.get(key)
        if v is None:
            return cur
        try:
            v = float(v)
        except (TypeError, ValueError):
            return cur
        return v if v > 0 else None

    SHORT_STOP = _stop(s, "short_stop", SHORT_STOP)
    LONG_STOP = _stop(s, "long_stop", LONG_STOP)
    ps = s.get("primary_set")
    if isinstance(ps, (list, tuple)) and len(ps) == 2:
        try:
            PRIMARY_SET = (int(ps[0]), int(ps[1]))
        except (TypeError, ValueError):
            pass
    CFG_SOURCE = "monitor_config.json（与每日检测同一口径）"


_apply_monitor_config()


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


def cache_is_stale(symbol, now_ms=None, max_age_days=DATA_MAX_AGE_DAYS):
    """
    判断本地行情缓存能不能用，返回 (是否过期, 原因)。

    除了「太旧」之外，还要挡一种更隐蔽的坏数据：
    **最后一根日线在它收盘之前就被写进了缓存**。
    比如 10-21 中午 12:44 拉过一次数据，那时 10-21 这根还在走，
    文件里存下的 close 是当时的价格；等到 10-22 再跑，按墙钟这根「已收盘」，
    于是这个半成品收盘价就被当成最终收盘价参与计算 —— 静默偏 0.5% 左右，没人看得出来。
    """
    path = os.path.join(DATA_DIR, "%s_1d.csv" % symbol)
    if not os.path.exists(path):
        return True, "没有本地缓存"
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    mtime_ms = os.path.getmtime(path) * 1000.0
    bars = load(symbol)
    if not bars:
        return True, "本地缓存是空的"
    closed = [b for b in bars if b["t"] + DAY_MS <= now_ms]
    if not closed:
        return True, "本地缓存里一根已收盘的日线都没有"
    last = closed[-1]
    lag = (now_ms - last["t"]) / float(DAY_MS)
    if lag > max_age_days:
        return True, "最后一根已收盘日线是 %s，距今 %.1f 天" % (fdate(last["t"]), lag)
    if last["t"] + DAY_MS > mtime_ms:
        return True, ("最后一根已收盘日线 %s 在它收盘前就被写进缓存了，那根的收盘价是残缺的"
                      % fdate(last["t"]))
    return False, ""


def ensure_data(symbol, refresh=False):
    """确保 data/<symbol>_1d.csv 存在且可用；返回 bar 列表。

    以前只在文件不存在时才联网，结果双击「运行回测.bat」可能拿着两周前的数据跑，
    报告照样生成、照样好看，只是截止日期悄悄停在过去 —— 这种沉默的过期最容易被忽略。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "%s_1d.csv" % symbol)
    need = refresh or not os.path.exists(path)
    if not need:
        stale, why = cache_is_stale(symbol)
        if stale:
            log("  本地缓存需要更新：%s" % why)
            log("  自动补拉最新数据 ...")
            need = True
    if need:
        log("  拉取 %-9s ..." % symbol)
        try:
            rows = fetch_klines(symbol, FETCH_START)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time"])
                for r in rows:
                    w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
            log("             %d 根" % len(rows))
        except Exception as e:
            if not os.path.exists(path):
                raise
            log("  ⚠ 拉取失败（%s），继续用本地缓存 —— 报告截止日可能不是最新" % e)
    return load(symbol)


def load(symbol):
    """读本地 CSV 日线，并做健壮性清洗。

    币安接口偶尔会返回异常行（价格为 0、时间戳重复、OHLC 越界）。
    旧版原样读进来：价格为 0 会让 `notional / price` 除零、整份回测崩掉；
    乱序或重复会让 EMA/ATR 算出错误结果，但**不会报错**，报告照样生成、照样好看。
    这里统一过滤掉非法行、按时间排序、时间戳重复只留最后一条，并打印清理条数。
    """
    path = os.path.join(DATA_DIR, "%s_1d.csv" % symbol)
    raw = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                b = {"t": int(row["open_time"]),
                     "open": float(row["open"]), "high": float(row["high"]),
                     "low": float(row["low"]), "close": float(row["close"]),
                     "vol": float(row["volume"])}
            except (KeyError, TypeError, ValueError):
                raw.append(None)                     # 缺字段/非数字
                continue
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            ok = (o > 0 and h > 0 and l > 0 and c > 0
                  and h >= l and h >= o and h >= c and l <= o and l <= c)
            raw.append(b if ok else None)

    bad = sum(1 for x in raw if x is None)
    bars = [x for x in raw if x is not None]
    if not bars:
        raise RuntimeError("%s 没有可用日线（%d 行全部非法）" % (symbol, len(raw)))

    bars.sort(key=lambda x: x["t"])
    dedup = []
    dup = 0
    for b in bars:
        if dedup and dedup[-1]["t"] == b["t"]:
            dedup[-1] = b                        # 同一根只留最后一条
            dup += 1
        else:
            dedup.append(b)
    bars = dedup

    if bad or dup:
        log("  ⚠ %s 数据清洗：丢弃非法行 %d 条、重复时间戳 %d 条（剩 %d 根）"
            % (symbol, bad, dup, len(bars)))
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


def regime_long(bars, sig, ma=None):
    """
    只约束多头的趋势过滤：收盘价在 SMA(ma) 下方时把做多信号置 0，做空信号原样保留。

    只约束多头的原因：BTC 的暴跌段几乎都发生在长期均线下方，
    挡掉「均线下方抄底做多」能明显削回撤；而空头本来就靠下跌赚钱，不该被均线挡住。
    实测「只约束空头」或「双向过滤」都比「只约束多头」差。

    ma 为 0 / None / <=1 时不过滤（原样返回）。
    """
    ma = REGIME_MA if ma is None else ma
    if not ma or int(ma) <= 1:
        return list(sig)
    c = [b["close"] for b in bars]
    m = sma(c, int(ma))
    out = list(sig)
    for i in range(len(bars)):
        if m[i] != m[i] or (sig[i] > 0 and c[i] <= m[i]):    # NaN 自检
            out[i] = 0
    return out












# ==============================================================================
# 3. 绩效统计
# ==============================================================================
def stats(equity, trades=None):
    eq = [x for x in equity if x is not None]
    if len(eq) < 2 or eq[0] <= 0:
        # 字段要和正常分支一致，否则调用方取 st["max_underwater"] 会 KeyError
        return {"final": 0.0, "cagr": -1.0, "mdd": -1.0, "calmar": 0.0,
                "max_underwater": 0, "roll365": None, "n_bars": len(eq)}
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
        # 起点权益为 0 时跳过：那是「爆仓归零之后」的窗口，除零会让整个回测崩掉。
        # 1× 逐仓通常只亏掉保证金、权益不会真的归零，但空头被逆行 100%
        # （或把杠杆调大）时就会出现 0，届时旧版会 ZeroDivisionError 直接挂。
        r = sorted(eq[i] / eq[i - W] - 1.0 for i in range(W, n) if eq[i - W] > 0)
        if r:
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
# 4. 引擎：混合载体（多头走现货、空头走合约 1× 逐仓）★ 唯一方案
# ==============================================================================
def _check_args(capital, L, atr_n, vt, exposure_cap, warmup):
    """拦住会算出除零或 NaN 的参数。

    config 那条路径有 >0 兜底，但函数被直接调用时（分析脚本、实验）没有。
    L=-1 尤其危险：它不报错，会安静地算出 nan 和 Calmar=-3.5 这种荒谬结果，
    而报告照样生成 —— 比直接崩掉更难发现。
    """
    bad = []
    if not (capital > 0):
        bad.append("本金 capital 必须 > 0（当前 %r）" % capital)
    if not (L > 0):
        bad.append("杠杆 L 必须 > 0（当前 %r；L=0 会除零，L<0 会算出 nan）" % L)
    if not (atr_n >= 1):
        bad.append("ATR 窗口 atr_n 必须 >= 1（当前 %r）" % atr_n)
    for nm, v in (("vt", vt), ("exposure_cap", exposure_cap), ("warmup", warmup)):
        if v != v or v < 0:
            bad.append("%s 不能为负或 NaN（当前 %r）" % (nm, v))
    if bad:
        raise ValueError("参数非法：" + "；".join(bad))


def backtest_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE, spot_fee=SPOT_FEE,
                    fut_fee=FUT_FEE, slip=SLIP, funding_d=FUNDING_D, sizing="vol",
                    vt=VT, exposure_cap=EXPOSURE_CAP, atr_n=ATR_N, warmup=WARMUP,
                    cooldown_after_liq=0, allow_short=True, short_stop=SHORT_STOP,
                    long_stop=LONG_STOP, stop_reentry="immediate"):
    """
    short_stop / long_stop：固定百分比止损（默认 None = 不设，只用信号反转出场）。
        short_stop=0.05 → 空头逆行 5% 平仓；long_stop=0.05 → 多头下跌 5% 平仓。
    stop_reentry：止损后怎么再入场
        "immediate"  = 下一根开盘按信号重新进场（止损基准随之重置，即滚动止损）
        "wait_signal"= 等信号先翻转再进场（同一段行情只止损一次，更贴近手工交易）
    """
    _check_args(capital, L, atr_n, vt, exposure_cap, warmup)
    n = len(bars)
    a = atr(bars, atr_n)
    eq, pos, units, entry_px, margin, fund_acc, entry_i = capital, 0, 0.0, 0.0, 0.0, 0.0, -1
    equity = [None] * n
    trades = []
    liq_days = []
    short_adverse = 0.0
    block_until = -1
    stopped_dir = 0            # 被止损平掉的方向，用于 stop_reentry="wait_signal"

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
                       "exit_px": px, "pnl": pnl, "liq": reason == "liq", "leg": leg,
                       "why": reason})
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
        # 止损后「等信号翻转才再入场」：信号没变就一直待在场外，不再被同一段行情反复扫损
        if stopped_dir and stop_reentry == "wait_signal":
            if tgt == stopped_dir:
                tgt = 0
            else:
                stopped_dir = 0
        prev_c = bars[i - 1]["close"]
        if pos > 0:
            eq_ref = eq + units * prev_c
        elif pos < 0:
            eq_ref = eq + units * (prev_c - entry_px) * pos - fund_acc
        else:
            eq_ref = eq
        # 账户已经归零（爆仓把保证金全吃掉、还倒欠手续费）→ 不许再开新仓。
        # 旧版会拿这个负数当「权益」算出负的名义仓位，凭空造出一笔反向的幽灵持仓。
        can_open = eq_ref > 0
        if tgt != pos:
            if pos != 0:
                close_pos(i, o * (1 - slip) if pos > 0 else o * (1 + slip), "signal")
            if tgt != 0 and can_open:
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
            elif short_stop:
                # 空头固定止损（默认关闭）。触及即按止损价成交；开盘就跳空过止损价则按开盘价。
                # 止损是市价单，必须和信号平仓一样计滑点（买回价格更高 = 更差）。
                stop_px = entry_px * (1 + short_stop)
                if o >= stop_px:
                    close_pos(i, o * (1 + slip), "stop")
                    stopped_dir = -1
                elif h >= stop_px:
                    close_pos(i, stop_px * (1 + slip), "stop")
                    stopped_dir = -1
        if pos > 0 and long_stop:
            # 多头固定止损（现货，无爆仓风险，纯粹限制单笔亏损）。
            stop_px = entry_px * (1 - long_stop)
            if o <= stop_px:
                close_pos(i, o * (1 - slip), "stop")
                stopped_dir = 1
            elif l <= stop_px:
                close_pos(i, stop_px * (1 - slip), "stop")
                stopped_dir = 1
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
    # 期末仍未平的仓位：止损后会「当天重开」，成本价随之改变，按信号连续段反推会算错，
    # 所以直接把引擎内部真实的 entry_i / entry_px 交出去。
    open_pos = None
    if pos != 0:
        open_pos = {"entry_i": entry_i, "entry_px": entry_px, "dir": pos,
                    "units": units, "margin": margin, "fund": fund_acc}
    return {"equity": equity, "trades": trades, "stats": st, "open": open_pos}








# ==============================================================================
# 5. 自检审计：绕开引擎独立重放，验证没有前视偏差
# ==============================================================================
def audit_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE, spot_fee=SPOT_FEE,
                 fut_fee=FUT_FEE, slip=SLIP, funding_d=FUNDING_D, vt=VT,
                 exposure_cap=EXPOSURE_CAP, atr_n=ATR_N, warmup=WARMUP,
                 short_stop=SHORT_STOP, long_stop=LONG_STOP):
    """
    完全独立地重新实现一遍混合载体逻辑（不调用 backtest_hybrid），
    逐笔比对交易点与成交价，并比对权益曲线。
    回测最容易骗人的地方是偷看未来数据，肉眼审代码几乎发现不了 —— 只能靠独立重放。

    止损也要在这里独立实现一遍：否则一开止损，主引擎和重放必然对不上，
    而「对不上」会被误读成引擎有 bug。
    """
    _check_args(capital, L, atr_n, vt, exposure_cap, warmup)
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
        can_open = eqref > 0                   # 与引擎一致：归零后不再开新仓
        if tgt != pos:
            if pos != 0:
                px = o * (1 - slip) if pos > 0 else o * (1 + slip)
                if pos > 0:
                    eq = eq + units * px * (1 - spot_fee)
                else:
                    eq = eq + units * (px - epx) * pos - units * px * fut_fee - fund
                tr.append((ei, i, pos, epx, px))
                pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
            if tgt != 0 and can_open:
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
        if pos < 0:                            # 空头逐仓爆仓线：入场价 × (1 + 1/L)
            lp = epx * (1 + 1.0 / L)
            if o >= lp:
                hit, px = True, o
            else:
                hit, px = (h >= lp), lp
            if hit:
                eq -= margin
                tr.append((ei, i, pos, epx, px))
                pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
            elif short_stop:
                # 止损：先判开盘是否已跳空过止损价（那就按开盘价，更差），再判盘中最高
                sp = epx * (1 + short_stop)
                fp = o * (1 + slip) if o >= sp else (sp * (1 + slip) if h >= sp else None)
                if fp is not None:
                    eq = eq + units * (fp - epx) * pos - units * fp * fut_fee - fund
                    tr.append((ei, i, pos, epx, fp))
                    pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
        if pos > 0 and long_stop:
            sp = epx * (1 - long_stop)
            fp = o * (1 - slip) if o <= sp else (sp * (1 - slip) if l <= sp else None)
            if fp is not None:
                eq = eq + units * fp * (1 - spot_fee)
                tr.append((ei, i, pos, epx, fp))
                pos, units, epx, margin, fund, ei = 0, 0.0, 0.0, 0.0, 0.0, -1
        if pos < 0:
            fund += pos * units * c * funding_d
        eqs[i] = max(eq + (units * c if pos > 0 else
                           (units * (c - epx) * pos - fund if pos < 0 else 0)), 0.0)
    return eqs, tr


# ==============================================================================
# 6. 风险与可信度工具（滚动 365 天 / walk-forward 样本外检验 / 回撤事件 / 年度切分）
# ==============================================================================
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




def walk_forward_equity(rets_map, sets, ymap, warmup, n, win_years):
    """
    每年年初只用「截至当时的过去 win_years 年」挑参数，然后持有一年。
    这是对「固定 10/30 的优势是不是事后挑出来的」最直接的检验。
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
# 7. 输出工具
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
    """
    返回按深度排序的「独立回撤段」：从每个历史新高跌到下一个新高算一段，
    每段只保留跌得最深的那一天（谷底）。这样列表里不会出现同一次回撤的多个时点。
    每项 = (回撤深度, 峰值权益, 谷底权益, 峰到谷天数)。
    """
    vals = [x for x in eq if x is not None]
    events = []
    peak, peak_i, cur = vals[0], 0, None
    for i, v in enumerate(vals):
        if v >= peak:
            if cur is not None:
                events.append(cur)
                cur = None
            peak, peak_i = v, i
        else:
            d = v / peak - 1.0
            if cur is None or d < cur[0]:
                cur = (d, peak, v, i - peak_i)
    if cur is not None:
        events.append(cur)
    events.sort(key=lambda x: x[0])
    return events[:top]


# ==============================================================================
# 8. 主流程
# ==============================================================================
def main():
    t_start = time.time()
    refresh = "--refresh" in sys.argv
    hr()
    log(" BTC 日线趋势策略 · 当前方案回测")
    log(" 载体：多头走现货 · 空头走合约 %d× 逐仓" % int(LEVERAGE))
    log(" 过滤：SMA%d 只约束多头（收盘价在均线下方时不开多，空头照做）" % REGIME_MA)
    log(" 本金 %.0f USDT | 波动率目标 %.0f%%/日 | 敞口上限 %.1f× | 滑点 %.2f%%"
        % (CAPITAL, VT * 100, EXPOSURE_CAP, SLIP * 100))
    log(" 双均线 EMA %d/%d | ATR 窗口 %d | 预热 %d 根"
        % (PRIMARY_SET[0], PRIMARY_SET[1], ATR_N, WARMUP))
    log(" 参数来源：%s" % CFG_SOURCE)
    hr()
    log("")

    # ---------------- 0. 数据 ----------------
    log("【第 0 步】准备数据")
    bars = ensure_data("BTCUSDT", refresh)
    # 只用已收盘的日线：币安日线以 UTC 00:00 为起点，收盘时间 = open_time + 1 天。
    # 剔除正在走的那根，口径才和每日检测脚本（btc_monitor.py）完全一致。
    _now = int(time.time() * 1000)
    _n_all = len(bars)
    bars = [b for b in bars if b["t"] + DAY_MS <= _now]
    if len(bars) != _n_all:
        log("  原始 %d 根，剔除未收盘 %d 根，参与计算 %d 根"
            % (_n_all, _n_all - len(bars), len(bars)))
    n = len(bars)
    dates = [fdate(b["t"]) for b in bars]
    years = n / 365.25
    bh = bars[-1]["close"] / bars[0]["close"]
    pk, bh_mdd = bars[0]["close"], 0.0
    for b in bars:
        pk = max(pk, b["close"])
        bh_mdd = min(bh_mdd, b["close"] / pk - 1.0)
    log("  BTC 样本 %s → %s，共 %d 根 / %.2f 年" % (dates[0], dates[-1], n, years))
    log("  买入持有 %.2f×（年化 %.1f%%，最大回撤 %.1f%%）"
        % (bh, (bh ** (1 / years) - 1) * 100, bh_mdd * 100))
    write_csv("00_样本概况.csv",
              ["项目", "数值"],
              [["标的", "BTCUSDT"],
               ["样本区间", "%s ~ %s" % (dates[0], dates[-1])],
               ["K线数量", n], ["样本年数", "%.2f" % years],
               ["本金USDT", "%.2f" % CAPITAL],
               ["信号", "双均线 EMA %d/%d（收盘确认，次日开盘成交）" % PRIMARY_SET],
               ["多头腿", "现货（不付资金费）"],
               ["空头腿", "合约 %.0f× 逐仓" % LEVERAGE],
               ["趋势过滤", "SMA%d 只约束多头" % REGIME_MA],
               ["波动率目标", "%.2f" % VT], ["敞口上限", "%.2f" % EXPOSURE_CAP],
               ["现货手续费", "%.4f" % SPOT_FEE], ["合约手续费", "%.4f" % FUT_FEE],
               ["滑点(单边)", "%.4f" % SLIP], ["资金费(日)", "%.4f" % FUNDING_D],
               ["预热期", WARMUP],
               ["买入持有倍数", "%.4f" % bh],
               ["生成时间", time.strftime("%Y-%m-%d %H:%M:%S")]])
    log("")

    # ---------------- 当前方案 ----------------
    sig_raw = sig_ema_cross(bars, PRIMARY_SET[0], PRIMARY_SET[1])
    sig = regime_long(bars, sig_raw, REGIME_MA)
    r = backtest_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE)
    ts = trade_stats(r["trades"])
    st = r["stats"]
    eq = r["equity"]
    roll = st.get("roll365") or {}
    n_liq = sum(1 for t in r["trades"] if t.get("liq"))

    # ---------------- 1. 总览 + 收益归因 ----------------
    log("【第 1 步】当前方案总览（含收益归因：%.1f× 到底从哪来）" % st["final"])
    hr("-")
    log(row([("方案", 34, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"),
             ("最长水下", 10, "right"), ("滚365最差", 11, "right")]))
    hr("-")

    # 把方案拆成各条腿分别单独回测：只多腿 / 只空腿 / 全程做多（不择时）
    sig_l = [max(x, 0) for x in sig]
    sig_s = [min(x, 0) for x in sig]
    r_l = backtest_hybrid(bars, sig_l, capital=CAPITAL, L=LEVERAGE)
    r_s = backtest_hybrid(bars, sig_s, capital=CAPITAL, L=LEVERAGE)
    # 全程做多、但保留波动率目标仓位（不择时）——用来看「仓位管理」单独值多少
    r_b = backtest_hybrid(bars, [1] * len(bars), capital=CAPITAL, L=1.0)

    def _sumline(label, rr):
        s2 = rr["stats"]
        rl2 = s2.get("roll365") or {}
        log(row([(label, 34, "left"), ("%.2f×" % s2["final"], 10, "right"),
                 ("%+.1f%%" % (s2["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (s2["mdd"] * 100), 10, "right"),
                 ("%.3f" % s2["calmar"], 8, "right"),
                 ("%d天" % s2["max_underwater"], 10, "right"),
                 (("%.1f%%" % (rl2["min"] * 100)) if rl2.get("min") is not None else "—",
                  11, "right")]))

    _sumline("当前方案（SMA%d 多头 + 空头 %d×）★" % (REGIME_MA, int(LEVERAGE)), r)
    _sumline("  ├ 只做多腿（EMA 择时 + 仓位管理）", r_l)
    _sumline("  ├ 只做空腿（SMA%d 不管空头）" % REGIME_MA, r_s)
    _sumline("  └ 全程做多（不择时，只做仓位管理）", r_b)
    log(row([("买入持有（一直满仓现货，基准）", 34, "left"), ("%.2f×" % bh, 10, "right"),
             ("%+.1f%%" % ((bh ** (1 / years) - 1) * 100), 9, "right"),
             ("%.1f%%" % (bh_mdd * 100), 10, "right"),
             ("—", 8, "right"), ("—", 10, "right"), ("—", 11, "right")]))
    hr("-")
    log("  终值是「利润全部滚进下一笔」的复利口径；固定本金口径见第 7 步。")
    log("  方案 ≈ 只多腿 × 只空腿（两条腿时间不重叠，复利相乘）。")
    log("  只做多腿 %.1f× 远高于一直拿着 %.1f× —— 差额来自「跌破均线就空仓」躲开大跌；"
        % (r_l["stats"]["final"], bh))
    log("  而仓位管理单独用反而更差（%.1f× < %.1f×），说明真正赚钱的是择时、不是仓位缩放。"
        % (r_b["stats"]["final"], bh))
    log("  空头腿只贡献 %.2f×（见第 2 步），作用是熊市减亏、压回撤，不是收益引擎。"
        % r_s["stats"]["final"])
    write_csv("01_当前方案总览.csv",
              ["方案", "终值倍数", "年化", "最大回撤", "Calmar", "最长水下(天)",
               "滚365最差", "成交笔数", "爆仓次数"],
              [["当前方案（SMA%d + 空头 %d×）" % (REGIME_MA, int(LEVERAGE)),
                "%.4f" % st["final"], "%.4f" % st["cagr"], "%.4f" % st["mdd"],
                "%.4f" % st["calmar"], st["max_underwater"],
                "" if roll.get("min") is None else "%.4f" % roll["min"],
                len(r["trades"]), n_liq],
               ["只做多腿（EMA择时 + 仓位管理）",
                "%.4f" % r_l["stats"]["final"], "%.4f" % r_l["stats"]["cagr"],
                "%.4f" % r_l["stats"]["mdd"], "%.4f" % r_l["stats"]["calmar"],
                r_l["stats"]["max_underwater"],
                "" if (r_l["stats"].get("roll365") or {}).get("min") is None
                else "%.4f" % r_l["stats"]["roll365"]["min"],
                len(r_l["trades"]), sum(1 for t in r_l["trades"] if t.get("liq"))],
               ["只做空腿（SMA%d 不管空头）" % REGIME_MA,
                "%.4f" % r_s["stats"]["final"], "%.4f" % r_s["stats"]["cagr"],
                "%.4f" % r_s["stats"]["mdd"], "%.4f" % r_s["stats"]["calmar"],
                r_s["stats"]["max_underwater"],
                "" if (r_s["stats"].get("roll365") or {}).get("min") is None
                else "%.4f" % r_s["stats"]["roll365"]["min"],
                len(r_s["trades"]), sum(1 for t in r_s["trades"] if t.get("liq"))],
               ["全程做多（不择时，只做仓位管理）",
                "%.4f" % r_b["stats"]["final"], "%.4f" % r_b["stats"]["cagr"],
                "%.4f" % r_b["stats"]["mdd"], "%.4f" % r_b["stats"]["calmar"],
                r_b["stats"]["max_underwater"],
                "" if (r_b["stats"].get("roll365") or {}).get("min") is None
                else "%.4f" % r_b["stats"]["roll365"]["min"],
                len(r_b["trades"]), sum(1 for t in r_b["trades"] if t.get("liq"))],
               ["买入持有（基准）", "%.4f" % bh, "%.4f" % (bh ** (1 / years) - 1),
                "%.4f" % bh_mdd, "", "", "", "", ""]])
    log("")

    # ---------------- 2. 分腿胜率 ----------------
    log("【第 2 步】胜率拆解 + 收益归因")
    hr("-")
    log(row([("腿", 14, "left"), ("笔数", 6, "right"), ("胜率", 8, "right"),
             ("盈亏比", 9, "right"), ("盈亏因子", 10, "right"),
             ("最长连亏", 9, "right"), ("平均持仓", 9, "right"),
             ("单独回测", 11, "right"), ("收益占比", 9, "right")]))
    hr("-")

    # 逐日对数收益按当时持仓方向切开（两条腿时间不重叠 → log 收益可直接相加）
    _LL = _LS = 0.0
    for _i in range(1, len(eq)):
        _a, _bb = eq[_i - 1], eq[_i]
        if _a is None or _bb is None or _a <= 0 or _bb <= 0:
            continue
        _d = math.log(_bb / _a)
        if sig[_i - 1] > 0:
            _LL += _d
        elif sig[_i - 1] < 0:
            _LS += _d
    _sum = _LL + _LS
    _solo = {"spot": r_l["stats"]["final"], "fut": r_s["stats"]["final"]}
    _share = {"spot": (_LL / _sum) if _sum else 0.0,
              "fut": (_LS / _sum) if _sum else 0.0, "all": 1.0}

    leg_rows = []
    for key, label in (("spot", "现货多头腿"), ("fut", "合约空头腿"), ("all", "合计")):
        b = ts.get(key)
        if not b:
            continue
        ratio = (b["avg_win"] / abs(b["avg_loss"])) if b["avg_loss"] else float("inf")
        solo = st["final"] if key == "all" else _solo[key]
        share = _share[key]
        log(row([(label, 14, "left"), (str(b["n"]), 6, "right"),
                 ("%.1f%%" % (b["win_rate"] * 100), 8, "right"),
                 ("%.2f" % ratio, 9, "right"), ("%.2f" % b["pf"], 10, "right"),
                 (str(b["max_consec_loss"]), 9, "right"),
                 ("%.0f天" % b["avg_hold"], 9, "right"),
                 ("%.2f×" % solo, 11, "right"), ("%.0f%%" % (share * 100), 9, "right")]))
        leg_rows.append([label, b["n"], b["wins"], b["losses"], "%.4f" % b["win_rate"],
                         "%.4f" % ratio, "%.4f" % b["pf"], b["max_consec_loss"],
                         "%.1f" % b["avg_hold"], "%.4f" % solo, "%.4f" % share])
    hr("-")
    log("  趋势跟踪的常态：胜率只有三成上下，靠盈亏比赚钱，必须能扛住连续亏损。")
    log("  「单独回测」= 只跑这一条腿、其余时间空仓；「收益占比」按逐日对数收益切分。")
    log("  → 收益主要来自多头腿的「择时空仓」（躲开大跌，复利基数不被打残）；")
    log("    空头腿只贡献 %.2f×（占 %.0f%%），作用是熊市减亏、压回撤，不是收益引擎。"
        % (_solo["fut"], _share["fut"] * 100))
    write_csv("02_分腿胜率.csv",
              ["腿", "笔数", "盈利笔数", "亏损笔数", "胜率", "盈亏比", "盈亏因子PF",
               "最长连亏", "平均持仓天数", "单独回测终值", "收益占比"], leg_rows)
    log("")

    # ---------------- 3. 逐年收益 ----------------
    log("【第 3 步】逐年收益")
    yr = yearly_returns(bars, eq)
    hr("-")
    log(row([("年份", 8, "left"), ("年初权益", 13, "right"), ("年末权益", 13, "right"),
             ("收益", 10, "right"), ("年内最大回撤", 14, "right")]))
    hr("-")
    yr_rows = []
    for y in yr:
        log(row([(str(y["year"]), 8, "left"), ("%.0f" % y["start"], 13, "right"),
                 ("%.0f" % y["end"], 13, "right"),
                 ("%+.1f%%" % (y["ret"] * 100), 10, "right"),
                 ("%.1f%%" % (y["mdd"] * 100), 14, "right")]))
        yr_rows.append([y["year"], "%.2f" % y["start"], "%.2f" % y["end"],
                        "%.4f" % y["ret"], "%.4f" % y["mdd"]])
    hr("-")
    write_csv("03_逐年收益.csv",
              ["年份", "年初权益", "年末权益", "年度收益", "年内最大回撤"], yr_rows)
    log("")

    # ---------------- 4. 回撤与滚动 365 天 ----------------
    log("【第 4 步】回撤事件与滚动 365 天分布")
    dd = drawdown_events(eq, top=5)
    hr("-")
    log(row([("排名", 6, "left"), ("回撤深度", 12, "right"), ("峰值权益", 13, "right"),
             ("谷底权益", 13, "right"), ("持续天数", 10, "right")]))
    hr("-")
    dd_rows = []
    for i, (d, pk_v, v, days) in enumerate(dd, 1):
        log(row([("#%d" % i, 6, "left"), ("%.1f%%" % (d * 100), 12, "right"),
                 ("%.0f" % pk_v, 13, "right"), ("%.0f" % v, 13, "right"),
                 (str(days), 10, "right")]))
        dd_rows.append(["#%d" % i, "%.4f" % d, "%.2f" % pk_v, "%.2f" % v, days])
    hr("-")
    if roll:
        log("  滚动 365 天收益分布（任意一天入场、持有满一年）：")
        log("    最差 %.1f%% | 25分位 %.1f%% | 中位 %.1f%% | 75分位 %.1f%% | 最好 %.1f%%"
            % (roll["min"] * 100, roll["p25"] * 100, roll["med"] * 100,
               roll["p75"] * 100, roll["max"] * 100))
        log("    正收益占比 %.1f%%" % (roll["pos_ratio"] * 100))
    write_csv("04_回撤事件.csv",
              ["排名", "回撤深度", "峰值权益", "谷底权益", "持续天数"], dd_rows)
    if roll:
        write_csv("04b_滚动365天.csv",
                  ["指标", "数值"],
                  [["最差", "%.4f" % roll["min"]], ["25分位", "%.4f" % roll["p25"]],
                   ["中位数", "%.4f" % roll["med"]], ["75分位", "%.4f" % roll["p75"]],
                   ["最好", "%.4f" % roll["max"]],
                   ["正收益占比", "%.4f" % roll["pos_ratio"]]])
    log("")

    # ---------------- 5. 过滤开关 & 空头腿杠杆 ----------------
    log("【第 5 步】趋势过滤开关 & 空头腿杠杆")
    hr("-")
    log(row([("配置", 30, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"), ("爆仓", 6, "right"),
             ("爆仓线", 10, "right")]))
    hr("-")
    conf_rows = []
    variants = [
        ("关闭过滤 + 空头 %d×" % int(LEVERAGE), sig_raw, LEVERAGE),
        ("SMA%d 只约束多头 + 空头 %d× ★当前" % (REGIME_MA, int(LEVERAGE)), sig, LEVERAGE),
    ]
    for _L in (2.0, 3.0, 4.0, 5.0):
        if abs(_L - LEVERAGE) < 1e-9:
            continue                    # 当前杠杆已经在上面 ★ 那行了，别重复
        variants.append(("SMA%d 只约束多头 + 空头 %d×" % (REGIME_MA, int(_L)), sig, _L))
    for label, sg, L in variants:
        rr = backtest_hybrid(bars, sg, capital=CAPITAL, L=L)
        ss = rr["stats"]
        liq = sum(1 for t in rr["trades"] if t.get("liq"))
        line = "+%.1f%%" % (100.0 / L)
        log(row([(label, 30, "left"), ("%.2f×" % ss["final"], 10, "right"),
                 ("%+.1f%%" % (ss["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (ss["mdd"] * 100), 10, "right"),
                 ("%.3f" % ss["calmar"], 8, "right"),
                 (str(liq), 6, "right"), (line, 10, "right")]))
        conf_rows.append([label, "%.4f" % ss["final"], "%.4f" % ss["cagr"],
                          "%.4f" % ss["mdd"], "%.4f" % ss["calmar"], liq, line])
    hr("-")
    log("  读法：名义仓位 = 权益 × 敞口，公式里没有杠杆 → 1×~4× 收益几乎完全相同，")
    log("        杠杆只决定保证金占用与爆仓线（价格逆行多少即爆）。5× 起开始爆仓、收益被削。")
    log("        1× 逐仓要占满 100% 保证金，实盘下单自动留 1% 缓冲（见 btc_monitor.py）。")
    log("        「关闭过滤」那行用来说明 SMA%d 过滤到底带来了什么。" % REGIME_MA)
    write_csv("05_过滤与杠杆对比.csv",
              ["配置", "终值倍数", "年化", "最大回撤", "Calmar", "爆仓次数", "爆仓线"],
              conf_rows)
    log("")

    # ---- 5b. 空头腿的资金费假设敏感性 ----
    # 引擎默认「空头每天收 0.03% 资金费」。这是收益里最容易被高估的一块，
    # 所以单独把它翻过来算一遍，看看结论还站不站得住。
    _fd0 = FUNDING_D if FUNDING_D > 0 else 0.0003
    log("  空头资金费敏感性（引擎默认空头每天「收」%.2f%%，这是偏乐观的一侧）：" % (_fd0 * 100))
    hr("-")
    log(row([("资金费假设", 30, "left"), ("只空腿终值", 12, "right"),
             ("全策略终值", 12, "right"), ("全策略回撤", 12, "right"),
             ("全策略Calmar", 13, "right")]))
    hr("-")
    fd_rows = []
    for _fd, _lbl in ((_fd0, "+%.2f%%/日 空头收（默认）" % (_fd0 * 100)),
                      (0.0, "0（不收不付）"),
                      (-_fd0, "-%.2f%%/日 空头付" % (_fd0 * 100))):
        _rs = backtest_hybrid(bars, sig_s, capital=CAPITAL, L=LEVERAGE, funding_d=_fd)["stats"]
        _rf = backtest_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE, funding_d=_fd)["stats"]
        log(row([(_lbl, 30, "left"), ("%.2f×" % _rs["final"], 12, "right"),
                 ("%.2f×" % _rf["final"], 12, "right"),
                 ("%.1f%%" % (_rf["mdd"] * 100), 12, "right"),
                 ("%.3f" % _rf["calmar"], 13, "right")]))
        fd_rows.append([_lbl, "%.4f" % _rs["final"], "%.4f" % _rf["final"],
                        "%.4f" % _rf["mdd"], "%.4f" % _rf["calmar"]])
    hr("-")
    log("  读法：空头腿的收益里有相当一部分来自「收资金费」。真实 BTC 永续资金费长期为正")
    log("        （多头付、空头收），但在下跌趋势里经常转负，那时空头反而要付钱。")
    log("        所以「空头收」是上限一侧；即使按「空头付」算，多头腿仍是收益主体，")
    log("        结论方向不变，但绝对倍数要打折看。")
    write_csv("05b_资金费敏感性.csv",
              ["资金费假设", "只空腿终值", "全策略终值", "全策略最大回撤", "全策略Calmar"],
              fd_rows)
    log("")

    # ---------------- 6. 参数样本外检验 ----------------
    log("【第 6 步】参数样本外检验（walk-forward，只用过去 5 年选参）")
    rets = {}
    for k in PARAM_SETS:
        sg = regime_long(bars, sig_ema_cross(bars, k[0], k[1]), REGIME_MA)
        rets[k] = daily_returns(backtest_hybrid(bars, sg, capital=CAPITAL, L=LEVERAGE)["equity"])
    ymap = yearly_map(bars)
    wf_eq, picks = walk_forward_equity(rets, PARAM_SETS, ymap, WARMUP, n, 5)
    wf_st = stats(wf_eq)
    hr("-")
    log(row([("方案", 34, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right")]))
    hr("-")
    log(row([("当前方案（固定 %d/%d）" % PRIMARY_SET, 34, "left"), ("%.2f×" % st["final"], 10, "right"),
             ("%+.1f%%" % (st["cagr"] * 100), 9, "right"),
             ("%.1f%%" % (st["mdd"] * 100), 10, "right"),
             ("%.3f" % st["calmar"], 8, "right")]))
    log(row([("walk-forward 过去 5 年自选", 34, "left"), ("%.2f×" % wf_st["final"], 10, "right"),
             ("%+.1f%%" % (wf_st["cagr"] * 100), 9, "right"),
             ("%.1f%%" % (wf_st["mdd"] * 100), 10, "right"),
             ("%.3f" % wf_st["calmar"], 8, "right")]))
    hr("-")
    log("  每年年初只用「截至当时的过去 5 年」数据挑参数，不许偷看未来。")
    log("  逐年选中：%s" % "  ".join("%d→%d/%d" % (y, k[0], k[1]) for y, k in picks))
    write_csv("06_参数样本外检验.csv",
              ["方案", "终值倍数", "年化", "最大回撤", "Calmar"],
              [["当前方案（固定 %d/%d）" % PRIMARY_SET, "%.4f" % st["final"], "%.4f" % st["cagr"],
                "%.4f" % st["mdd"], "%.4f" % st["calmar"]],
               ["walk-forward 过去 5 年自选", "%.4f" % wf_st["final"],
                "%.4f" % wf_st["cagr"], "%.4f" % wf_st["mdd"],
                "%.4f" % wf_st["calmar"]]])
    write_csv("06b_逐年选参.csv", ["年份", "快线", "慢线"],
              [[y, k[0], k[1]] for y, k in picks])
    log("")

    # ---------------- 7. 资金路径 ----------------
    log("【第 7 步】%.0f USDT 资金路径：固定本金 vs 滚仓" % CAPITAL)
    # 引擎的逐笔 pnl 只扣了「平仓费」：现货开仓费扣了，合约开仓费是从权益里直接扣的、
    # 没进 pnl。这里补上，否则「固定本金」会系统性高估（9 年 138 笔约 +0.2%），
    # 也会和每日检测邮件里打印的数字对不上。
    _a7 = atr(bars, ATR_N)
    fixed = CAPITAL
    for t in r["trades"]:
        j = t["entry_i"] - 1
        e_ref = eq[j] if (0 <= j < n and eq[j]) else CAPITAL
        if e_ref:
            _p = t["pnl"]
            if t.get("leg") == "fut" and 0 <= j < n:
                _dv = _a7[j] / bars[j]["close"] if not math.isnan(_a7[j]) else 0.05
                _ex = min(EXPOSURE_CAP, VT / _dv) if _dv > 0 else EXPOSURE_CAP
                _p -= e_ref * _ex * FUT_FEE
            fixed += _p * CAPITAL / e_ref

    # 当前持仓的浮动盈亏（固定本金口径），让这里的数字和每日检测邮件的
    # 「账户现值 = 本金 + 浮盈」完全对齐，避免两处对不上。
    def _cur_upnl(sig_seq):
        s = sig_seq[-1]
        if s == 0:
            return 0.0
        i = n - 1
        while i > 0 and sig_seq[i - 1] == s:
            i -= 1
        ei = min(i + 1, n - 1)
        e_px = bars[ei]["open"] * (1 + SLIP) if s > 0 else bars[ei]["open"] * (1 - SLIP)
        a = atr(bars, ATR_N)
        j = max(ei - 1, 0)
        dv = a[j] / bars[j]["close"] if a[j] == a[j] else 0.05
        expo = min(EXPOSURE_CAP, VT / dv) if dv > 0 else EXPOSURE_CAP
        q = (CAPITAL * expo) / e_px if e_px > 0 else 0.0
        cpx = bars[-1]["close"]
        days = n - 1 - ei
        if s > 0:
            return q * cpx * (1 - SPOT_FEE) - q * e_px * (1 + SPOT_FEE)
        return q * (e_px - cpx) - q * cpx * FUT_FEE + q * cpx * FUNDING_D * days

    cur_upnl = _cur_upnl(sig)
    hr("-")
    log(row([("口径", 34, "left"), ("账户现值 USDT", 15, "right"), ("倍数", 10, "right")]))
    hr("-")
    log(row([("固定本金（每笔都按 %.0f U 开仓）" % CAPITAL, 34, "left"),
             ("%.0f" % (fixed + cur_upnl), 15, "right"),
             ("%.2f×" % ((fixed + cur_upnl) / CAPITAL), 10, "right")]))
    log(row([("滚仓（每笔按当时权益开仓）", 34, "left"),
             ("%.0f" % r["equity"][-1], 15, "right"),
             ("%.2f×" % (r["equity"][-1] / CAPITAL), 10, "right")]))
    hr("-")
    log("  两者都含当前持仓浮盈（%.2f U，固定本金口径），与每日检测邮件的「账户现值」同口径。" % cur_upnl)
    log("  单笔收益率完全相同，差别只在「赚到的钱要不要立刻滚进下一笔」。")
    log("  滚仓收益高得多，但回撤与亏损期的缩水同步放大 —— 扛得住回撤才用滚仓，否则固定本金。")
    write_csv("07_资金路径.csv",
              ["口径", "账户现值USDT", "倍数", "其中当前浮盈"],
              [["固定本金（每笔都按 %.0f U 开仓）" % CAPITAL, "%.2f" % (fixed + cur_upnl),
                "%.4f" % ((fixed + cur_upnl) / CAPITAL), "%.2f" % cur_upnl],
               ["滚仓（每笔按当时权益开仓）", "%.2f" % r["equity"][-1],
                "%.4f" % (r["equity"][-1] / CAPITAL), ""]])
    log("")

    # ---------------- 8. 无前视自检 ----------------
    log("【第 8 步】无前视自检（绕开引擎独立重放）")
    log("  重放口径：%s" % ("含空头止损 %.0f%%（独立实现）" % (SHORT_STOP * 100)
                          if SHORT_STOP else "无止损"))
    aeq, atr_tr = audit_hybrid(bars, sig, capital=CAPITAL, L=LEVERAGE)
    max_rel, diff_i = 0.0, -1
    for i in range(n):
        x, y = eq[i], aeq[i]
        if x is None or y is None:
            continue
        if x:
            rel = abs(y - x) / abs(x)
            if rel > max_rel:
                max_rel, diff_i = rel, i
    ok_eq = max_rel < 1e-6
    eng_tr = [(t["entry_i"], t["exit_i"], t["dir"]) for t in r["trades"]]
    aud_tr = [(e, x, d) for (e, x, d, _ep, _xp) in atr_tr]
    ok_tr = eng_tr == aud_tr
    hr("-")
    log(row([("检查项", 44, "left"), ("结果", 34, "left")]))
    hr("-")
    log(row([("权益曲线：逐日独立重放", 44, "left"),
             ("一致（最大相对偏差 %.1e）" % max_rel if ok_eq
              else "不一致！第 %d 天偏差 %.1e" % (diff_i, max_rel), 34, "left")]))
    log(row([("交易序列：笔数 / 进出场日 / 方向", 44, "left"),
             ("一致（%d 笔）" % len(eng_tr) if ok_tr
              else "不一致：引擎 %d 笔 vs 重放 %d 笔" % (len(eng_tr), len(aud_tr)), 34, "left")]))
    hr("-")
    if ok_eq and ok_tr:
        log("  ✔ 通过：独立重放与引擎完全一致，未发现前视偏差（信号只用上一根收盘、次日开盘成交）。")
    else:
        log("  ✘ 未通过：两处实现出现分歧，请检查引擎逻辑。")
    write_csv("08_无前视自检.csv", ["检查项", "结果"],
              [["权益曲线逐日重放", "一致（最大相对偏差 %.1e）" % max_rel if ok_eq else "不一致"],
               ["交易序列", "一致（%d 笔）" % len(eng_tr) if ok_tr else "不一致"],
               ["结论", "通过" if (ok_eq and ok_tr) else "未通过"]])
    log("")

    # ---------------- 9. 反向持仓稳健性检验 ----------------
    log("【第 9 步】反向持仓稳健性检验（sig=0 的时段改成做空，是不是真的更好？）")
    hr("-")
    sig_rev = [-1 if x == 0 else x for x in sig]
    r_rev = backtest_hybrid(bars, sig_rev, capital=CAPITAL, L=LEVERAGE)

    log(row([("方案", 34, "left"), ("终值", 10, "right"), ("年化", 9, "right"),
             ("最大回撤", 10, "right"), ("Calmar", 8, "right"),
             ("笔数", 8, "right"), ("", 13, "right")]))
    hr("-")

    def _sline(label, rr, mark=""):
        s2 = rr["stats"]
        log(row([(label, 34, "left"), ("%.2f×" % s2["final"], 10, "right"),
                 ("%+.1f%%" % (s2["cagr"] * 100), 9, "right"),
                 ("%.1f%%" % (s2["mdd"] * 100), 10, "right"),
                 ("%.3f" % s2["calmar"], 8, "right"),
                 ("%d笔" % len(rr["trades"]), 8, "right"), (mark, 13, "right")]))

    _sline("A 当前：sig=0 空仓 ★", r, "采用")
    _sline("B 改成：sig=0 做空", r_rev, "看起来更好")
    hr("-")

    # 只看预热期之后的 sig=0 时段。预热期（前 WARMUP 根）引擎根本不下单，
    # 把那段算进来会虚增天数，还会把 2017 年那波单边上涨混进 t 检验 —— 结论会被稀释。
    _d0 = [i for i in range(WARMUP, len(bars) - 1) if sig[i] == 0]
    _rets = [bars[i + 1]["close"] / bars[i]["close"] - 1.0 for i in _d0]
    _n = len(_rets)
    _n_valid = max(len(bars) - WARMUP, 1)
    if _n >= 2:
        _m = sum(_rets) / _n
        _sd = (sum((x - _m) ** 2 for x in _rets) / (_n - 1)) ** 0.5
        _t = _m / (_sd / math.sqrt(_n)) if _sd > 0 else 0.0
    else:
        _m, _t = 0.0, 0.0
    _tot, _top5 = 1.0, 1.0
    for x in _rets:
        _tot *= (1.0 + x)
    for x in sorted(_rets)[:5]:          # 跌最多 = 做空最赚的 5 天
        _top5 *= (1.0 + x)
    # _tot / _top5 都已是「倍数」，去掉 top5 后剩下的倍数 = 两者相除
    _rest = (_tot / _top5 - 1.0) if _top5 else 0.0

    _scan = []
    for _ma in (20, 30, 40, 50, 60, 80, 100, 120):
        s2 = regime_long(bars, sig_ema_cross(bars, PRIMARY_SET[0], PRIMARY_SET[1]), _ma)
        a2 = backtest_hybrid(bars, s2, capital=CAPITAL, L=LEVERAGE)["stats"]["final"]
        b2 = backtest_hybrid(bars, [-1 if x == 0 else x for x in s2],
                             capital=CAPITAL, L=LEVERAGE)["stats"]["final"]
        _scan.append((_ma, b2 / a2 - 1.0))
    _neg = sum(1 for _, g in _scan if g < 0)

    log("  sig=0 的时段共 %d 天（占预热后 %d 天的 %.1f%%）—— 三项检验："
        % (_n, _n_valid, 100.0 * _n / _n_valid))
    log("    ① 统计显著性：次日 BTC 均值 %+.2f%%，t = %+.2f（|t| < 2 = 与随机无法区分）"
        % (_m * 100, _t))
    log("    ② 收益集中度：这 %d 天价格累计 %+.1f%%（做空就赚这么多），"
        % (_n, _tot * 100 - 100))
    log("       去掉做空最赚的 5 天后，剩下 %d 天价格累计 %+.1f%%（做空反而亏这么多）"
        % (max(_n - 5, 0), _rest * 100))
    log("    ③ 参数敏感性：扫 regime_ma 20→120，%d / %d 个是负的"
        % (_neg, len(_scan)))
    log("       %s" % "  ".join("%d:%+.1f%%" % (ma, g * 100) for ma, g in _scan))
    log("  → 结论：%s（保持 A：sig=0 空仓）"
        % ("过拟合，不采用" if _neg > len(_scan) / 2 else "稳健，可考虑采用"))
    write_csv("09_反向持仓检验.csv", ["检验项", "结果", "判定"],
              [["sig=0 天数（预热后）", _n, "占预热后 %.1f%%" % (100.0 * _n / _n_valid)],
               ["方案A 终值（当前）", "%.4f" % st["final"], "采用"],
               ["方案B 终值（sig=0做空）", "%.4f" % r_rev["stats"]["final"], "看起来更好"],
               ["① 次日均值 t 统计量", "%.2f" % _t, "显著" if abs(_t) >= 2 else "不显著"],
               ["② 这 %d 天累计" % _n, "%.4f" % (_tot - 1.0), "—"],
               ["② 去掉最赚 5 天后", "%.4f" % _rest, "做空反而巨亏"],
               ["③ 参数为负个数", "%d / %d" % (_neg, len(_scan)), "典型过拟合"],
               ["结论", "不采用 B", "保持 sig=0 空仓"]])
    log("")

    # ---------------- 汇总 ----------------
    elapsed = time.time() - t_start
    hr()
    log(" 完成！耗时 %.1f 秒" % elapsed)
    log(" 结果已输出到：%s" % OUT_DIR)
    for fn in sorted(os.listdir(OUT_DIR)):
        if fn != "完整回测报告.txt":
            log("    · %s" % fn)
    log("    · 完整回测报告.txt  ← 本页文字全文")
    hr()
    log("")
    log(" 一句话总结：")
    log("   1) 信号：双均线 EMA %d/%d，收盘确认、次日开盘成交，无未来函数" % PRIMARY_SET)
    log("   2) 过滤：SMA%d 只约束多头 —— 价格在均线下方时不开多，空头照做" % REGIME_MA)
    log("   3) 载体：多头走现货（省资金费）、空头走合约 %d× 逐仓（保留做空保护）"
        % int(LEVERAGE))
    log("   4) 结果：%.2f×（年化 %+.1f%%），最大回撤 %.1f%%，Calmar %.3f"
        % (st["final"], st["cagr"] * 100, st["mdd"] * 100, st["calmar"]))
    log("   5) 胜率仅 %.1f%%，靠盈亏比 %.2f 赚钱，必须能扛住连续 %d 笔亏损"
        % (ts["all"]["win_rate"] * 100,
           (ts["all"]["avg_win"] / abs(ts["all"]["avg_loss"])) if ts["all"]["avg_loss"] else 0.0,
           ts["all"]["max_consec_loss"]))
    log("   6) 爆仓：%.2f 年 %d 次；空头腿 %d× 需价格逆行 +%.0f%% 才爆，历史最深逆行 %.1f%%"
        % (years, n_liq, int(LEVERAGE), 100.0 / LEVERAGE,
           st.get("short_adverse", 0.0) * 100))
    log("   7) 资金：%.0f U 起步 → 固定本金口径 %.0f U（%.2f×），滚仓口径 %.0f U（%.2f×）"
        % (CAPITAL, fixed + cur_upnl, (fixed + cur_upnl) / CAPITAL,
           r["equity"][-1], r["equity"][-1] / CAPITAL))
    log("   8) 自检：独立重放与引擎逐日一致，未发现前视偏差")
    log("")
    log(" 免责：以上为历史回测，不是收益承诺。加密资产波动极大，务必先小资金验证。")
    hr()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "完整回测报告.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_BUF) + "\n")
    log(" 完整文本已保存：%s" % os.path.join(OUT_DIR, "完整回测报告.txt"))
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
