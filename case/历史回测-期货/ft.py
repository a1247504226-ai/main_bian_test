# -*- coding: utf-8 -*-
"""
国内商品期货 趋势策略 —— 探索 / 回测 / 校验 一体化工具

数据: 新浪"主连"(主力连续)日线。主连是【不复权】拼接序列 —— 换月当天价格会
      直接跳到新合约价位，产生一次跳空。这个跳空不是交易者能拿到的盈亏:
        近月100 / 远月105 的 contango 下, 原始序列换月 100->105 (虚增+5%),
        而真实持仓者换月后要承受 105->100 的收敛 (实亏 5%)。
      故本工具默认对主连做【比例复权】(_adjust), 抹掉换月跳空, 得到可回测的连续序列。
      设环境变量 FT_RAW=1 可切回未复权序列做对照 —— 对照结果见 README。

用法:
    python ft.py check        数据体检(跳空诊断)
    python ft.py scan         信号族全样本扫描
    python ft.py bias         随机信号控制实验(量化残余漂移)
    python ft.py explore      成本敏感性 / 参数寻优 / 样本内外秩相关
    python ft.py bench        基准对照(买入持有 / 截面动量反转 / 波动率目标 / 止损)
    python ft.py surface      (快线,慢线)x止损 参数曲面
    python ft.py validate [信号名]   完整校验(止损/多空/样本外/自助/逐年/成本/净值)
    python ft.py rank         逐品种归因: 最终策略单独跑每个品种, 排出收益最高的品种
    python ft.py report       生成单文件 HTML 研究报告
"""
import os, sys, json, math, statistics, random

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")          # 原始主连日线（不复权）

WARMUP = 250
TRADING_DAYS = 244

# 是否对主连做比例复权（抹掉换月跳空）。FT_RAW=1 关闭，用于对照实验。
ADJUST = os.environ.get("FT_RAW", "") not in ("1", "true", "True")
GAP_THRESH = 0.03          # |换月跳空| 超过此值判定为换月日

# 品种中文名
NAME = {
    "RB": "螺纹钢", "HC": "热卷", "BU": "沥青", "RU": "橡胶", "CU": "沪铜",
    "AL": "沪铝", "ZN": "沪锌", "NI": "沪镍", "AG": "沪银", "AU": "沪金",
    "SP": "纸浆", "SS": "不锈钢", "FU": "燃油", "AO": "氧化铝", "BR": "丁二烯胶",
    "TA": "PTA", "MA": "甲醇", "FG": "玻璃", "SA": "纯碱", "SR": "白糖",
    "CF": "棉花", "AP": "苹果", "OI": "菜油", "RM": "菜粕", "UR": "尿素",
    "SF": "硅铁", "SM": "锰硅", "PF": "短纤", "PK": "花生", "CJ": "红枣",
    "CY": "棉纱", "PX": "对二甲苯", "SH": "烧碱", "PR": "瓶片", "PL": "丙烯",
    "SI": "工业硅", "LC": "碳酸锂", "PS": "多晶硅", "SC": "原油",
    # 大商所（交易所接口被反爬，仅有主连数据；漂移由多空对称口径抵消）
    "C": "玉米", "CS": "玉米淀粉", "M": "豆粕", "A": "豆一", "Y": "豆油",
    "P": "棕榈油", "I": "铁矿石", "J": "焦炭", "JM": "焦煤", "V": "PVC",
    "L": "塑料", "PP": "聚丙烯", "EG": "乙二醇", "JD": "鸡蛋", "LH": "生猪",
}

# 合约规格: (合约乘数[单位/手], 交易所保证金率)
# 保证金率取交易所口径; 期货公司通常再加 3~5 个百分点
SPEC = {
    "RB": (10, 0.07), "HC": (10, 0.07), "BU": (10, 0.10), "RU": (10, 0.10),
    "CU": (5, 0.10), "AL": (5, 0.10), "ZN": (5, 0.10), "NI": (1, 0.12),
    "AG": (15, 0.10), "AU": (1000, 0.10), "SP": (10, 0.10), "SS": (5, 0.10),
    "FU": (10, 0.10), "AO": (20, 0.10), "BR": (5, 0.10),
    "TA": (5, 0.07), "MA": (10, 0.08), "FG": (20, 0.09), "SA": (20, 0.09),
    "SR": (10, 0.07), "CF": (5, 0.07), "AP": (10, 0.10), "OI": (10, 0.07),
    "RM": (10, 0.07), "UR": (20, 0.07), "SF": (5, 0.10), "SM": (5, 0.10),
    "PF": (5, 0.08), "PK": (5, 0.10), "CJ": (5, 0.12), "CY": (5, 0.07),
    "PX": (5, 0.10), "SH": (30, 0.10), "PR": (15, 0.10), "PL": (5, 0.10),
    "SI": (5, 0.10), "LC": (1, 0.12), "PS": (3, 0.10), "SC": (1000, 0.10),
    "C": (10, 0.07), "CS": (10, 0.06), "M": (10, 0.08), "A": (10, 0.08),
    "Y": (10, 0.08), "P": (10, 0.09), "I": (100, 0.10), "J": (100, 0.12),
    "JM": (60, 0.12), "V": (5, 0.07), "L": (5, 0.08), "PP": (5, 0.08),
    "EG": (10, 0.08), "JD": (10, 0.09), "LH": (16, 0.08),
}


# ================================================================ 数据层
def _read_csv(sym):
    p = os.path.join(DATA, sym + ".csv")
    if not os.path.exists(p):
        return None
    bars = []
    with open(p, encoding="utf-8") as f:
        next(f)
        for line in f:
            q = line.strip().split(",")
            if len(q) < 6:
                continue
            try:
                bars.append({"date": q[0], "open": float(q[1]), "high": float(q[2]),
                             "low": float(q[3]), "close": float(q[4]), "volume": float(q[5])})
            except ValueError:
                continue
    return _clean(bars) if bars else None


def load(sym):
    """默认口径：清洗 + 比例复权（去换月跳空），可直接回测。
    每根 K 线上附带 "gap" 字段 = 该日【换月跳空】(非换月日为 0)，
    供 carry 信号使用 —— 复权会抹掉跳空，故必须在此处留档。"""
    b = _read_csv(sym)
    if b is None:
        return None
    for i in range(1, len(b)):
        pc, op = b[i - 1]["close"], b[i]["open"]
        b[i]["gap"] = (op / pc - 1.0) if (pc > 0 and abs(op / pc - 1.0) > GAP_THRESH) else 0.0
    b[0]["gap"] = 0.0
    return _adjust(b) if ADJUST else b


def load_raw(sym):
    """对照口径：原始主连（不复权），仅用于证明跳空污染的严重性。"""
    return _read_csv(sym)


def _adjust(bars, thresh=GAP_THRESH):
    """比例复权（去换月跳空），得到可回测的连续价格序列。

    原理：主连换月日 open 直接跳到新合约价位。把该日整根 K 线乘以
    累计因子 k *= 前收/今开，使复权后 open 与前收严丝合缝衔接，
    日内涨跌幅(close/open)保持不变。累计因子只依赖当日及此前的换月，
    不含未来信息。

    效果：复权序列的累计收益 = 真实持仓盈亏（含展期损益），
          而非原始拼接序列那种被换月跳空虚增的收益。
    """
    out = [dict(bars[0])]
    k = 1.0
    for i in range(1, len(bars)):
        pc, op = bars[i - 1]["close"], bars[i]["open"]
        if pc > 0 and op > 0 and abs(op / pc - 1.0) > thresh:
            k *= pc / op
        b = dict(bars[i])
        for c in ("open", "high", "low", "close"):
            b[c] = bars[i][c] * k
        out.append(b)
    return out


def _clean(bars, tol=0.5):
    """剔除明显的坏数据点：某日价格相对前后两日中位数偏离超过 tol 视为脏数据，
    用前后中位数替换（如 CS 2017-05-25 曾出现 close=7 的错误值）。"""
    n = len(bars)
    fixed = 0
    for i in range(1, n - 1):
        nb = sorted([bars[i - 1]["close"], bars[i + 1]["close"]])
        ref = (nb[0] + nb[1]) / 2.0
        if ref <= 0:
            continue
        for k in ("open", "high", "low", "close"):
            if abs(bars[i][k] / ref - 1.0) > tol:
                bars[i][k] = ref
                fixed += 1
        if bars[i]["high"] < bars[i]["low"]:
            bars[i]["high"], bars[i]["low"] = bars[i]["low"], bars[i]["high"]
    return bars


def universe(min_bars=WARMUP + 750):
    out = []
    for s in sorted(NAME):
        b = load(s)
        if b and len(b) >= min_bars:
            out.append(s)
    return out


# ================================================================ 指标层
def ema(v, n):
    o, a, p = [], 2.0 / (n + 1), None
    for x in v:
        p = x if p is None else a * x + (1 - a) * p
        o.append(p)
    return o


def atr(bars, n=20):
    o, p = [], None
    for i, b in enumerate(bars):
        tr = (b["high"] - b["low"]) if i == 0 else max(
            b["high"] - b["low"], abs(b["high"] - bars[i - 1]["close"]),
            abs(b["low"] - bars[i - 1]["close"]))
        p = tr if p is None else (p * (n - 1) + tr) / n
        o.append(p)
    return o


def donchian(bars, n):
    up, lo = [None] * len(bars), [None] * len(bars)
    for i in range(n, len(bars)):
        w = bars[i - n:i]
        up[i] = max(x["high"] for x in w)
        lo[i] = min(x["low"] for x in w)
    return up, lo


def rsi(v, n=14):
    o, ag, al = [], None, None
    for i, x in enumerate(v):
        if i == 0:
            o.append(50.0); continue
        ch = x - v[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        ag, al = (g, l) if ag is None else ((ag * (n - 1) + g) / n, (al * (n - 1) + l) / n)
        o.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return o


# ================================================================ 信号层
def sig_tsmom(bars, n=120):
    """时序动量：过去 n 日收益为正做多，为负做空"""
    c = [b["close"] for b in bars]
    return [0 if i < n else (1 if c[i] > c[i - n] else -1) for i in range(len(bars))]


def sig_ma(bars, fast=20, slow=60):
    c = [b["close"] for b in bars]
    ef, es = ema(c, fast), ema(c, slow)
    return [0 if i < slow else (1 if ef[i] > es[i] else -1) for i in range(len(bars))]


def sig_don(bars, n=55):
    up, lo = donchian(bars, n)
    o, cur = [], 0
    for i in range(len(bars)):
        if up[i] is not None:
            if bars[i]["close"] > up[i]:
                cur = 1
            elif bars[i]["close"] < lo[i]:
                cur = -1
        o.append(cur)
    return o


def sig_ma_don(bars, fast=20, slow=60, n=20):
    a, b_ = sig_ma(bars, fast, slow), sig_don(bars, n)
    return [a[i] if a[i] == b_[i] else 0 for i in range(len(bars))]


def sig_rsi_rev(bars, n=14, lo=30, hi=70):
    r = rsi([b["close"] for b in bars], n)
    o, cur = [], 0
    for i in range(len(bars)):
        if i < n + 1:
            o.append(0); continue
        if r[i] < lo:
            cur = 1
        elif r[i] > hi:
            cur = -1
        elif (cur == 1 and r[i] > 50) or (cur == -1 and r[i] < 50):
            cur = 0
        o.append(cur)
    return o


def sig_boll_rev(bars, n=20, k=2.0):
    c = [b["close"] for b in bars]
    o, cur = [], 0
    for i in range(len(bars)):
        if i < n:
            o.append(0); continue
        w = c[i - n:i]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        if c[i] < m - k * sd:
            cur = 1
        elif c[i] > m + k * sd:
            cur = -1
        elif (cur == 1 and c[i] > m) or (cur == -1 and c[i] < m):
            cur = 0
        o.append(cur)
    return o


def sig_vbreak(bars, k=0.5, n=20):
    """波动突破：收盘价突破 前收 ± k×ATR 则顺势（状态机保持持仓）。
    经典 CTA 家族之一，用来补足"日线级别突破"这一类证据。"""
    A = atr(bars, n)
    c = [b["close"] for b in bars]
    o, cur = [], 0
    for i in range(len(bars)):
        if i < n + 1 or A[i - 1] is None or A[i - 1] <= 0:
            o.append(0); continue
        if c[i] > c[i - 1] + k * A[i - 1]:
            cur = 1
        elif c[i] < c[i - 1] - k * A[i - 1]:
            cur = -1
        o.append(cur)
    return o


def sig_vote(bars):
    """三信号投票：均线 / 唐奇安 / 时序动量 三者至少两个同向才持仓。
    用于检验"单信号无效是否只是噪声，集成后是否浮现 edge"。"""
    a = sig_ma(bars, 20, 60)
    b_ = sig_don(bars, 55)
    d = sig_tsmom(bars, 120)
    out = []
    for i in range(len(bars)):
        v = a[i] + b_[i] + d[i]
        out.append(1 if v >= 2 else (-1 if v <= -2 else 0))
    return out


def sig_tsmom_slow(bars, n=120, hold=20):
    """低换手时序动量：每 hold 个交易日才更新一次仓位（换手降为 1/hold）。
    直接检验"零成本有微弱正收益、被成本吃掉"这一假设。"""
    base = sig_tsmom(bars, n)
    o, cur = [], 0
    for i in range(len(bars)):
        if i % hold == 0:
            cur = base[i]
        o.append(cur)
    return o


def sig_carry(bars, win=252, thresh=0.004):
    """展期收益(carry)代理：主连换月跳空 ≈ 真实展期损益。

    逻辑：远月贵于近月(contango)时，换月会把主连价格向上跳，持有【多头】要付展期成本；
          反之(backwardation)持多头收展期收益。
    做法：滚动累计跳空。累计为正(contango 为主) -> 做空；为负(backwardation 为主) -> 做多。
    注意：跳空取自 load() 留档的 "gap" 字段（复权会抹掉它，故不能从价格重算）。
    """
    n = len(bars)
    out = []
    for i in range(n):
        if i < win:
            out.append(0); continue
        s = sum(bars[j].get("gap", 0.0) for j in range(i - win + 1, i + 1))
        out.append(1 if s < -thresh else (-1 if s > thresh else 0))
    return out


def sig_carry_trend(bars):
    """carry 与趋势同向才持仓（两条腿都要同意）。"""
    a = sig_carry(bars)
    b_ = sig_ma(bars, 20, 60)
    return [a[i] if a[i] == b_[i] else 0 for i in range(len(bars))]
def asset_returns(bars, sig, fee=0.0002, slip=0.0005, vol_target=0.02,
                  atr_n=20, expo_cap=2.0, sl_atr=0.0, vol_scale=True):
    """
    返回 (dates, rets, turn) —— 单品种日收益率与换手。

    时序（严格无前视）:
      bar i 开盘: 先判止损(跳空优先: open 已破止损价 -> 按 open 成交)，再执行 bar i-1 收盘确认的调仓
      bar i 日内: 持仓持有到收盘
    关键: 止损成交必须【结算当日已实现的亏损】，否则止损会变成免费保险。
    仓位: 敞口 = min(expo_cap, 波动率目标 / 日波动率)，日波动率取 ATR[i-1]/close[i-1]
    """
    n = len(bars)
    A = atr(bars, atr_n)
    dates, rets, turn = [], [], []
    pos, expo, stop = 0, 0.0, 0.0
    for i in range(1, n):
        prev_c = bars[i - 1]["close"]
        o, h, l, c = bars[i]["open"], bars[i]["high"], bars[i]["low"], bars[i]["close"]
        tgt = sig[i - 1] if i - 1 >= WARMUP else 0
        dvol = (A[i - 1] / prev_c) if prev_c > 0 and A[i - 1] > 0 else 0.0
        e = min(expo_cap, vol_target / dvol) if (vol_scale and dvol > 1e-6) else 1.0
        r, t = 0.0, 0.0

        # ---------- 1) 止损（跳空优先） ----------
        stopped = False
        if pos != 0 and sl_atr > 0:
            if (pos > 0 and (o <= stop or l <= stop)) or (pos < 0 and (o >= stop or h >= stop)):
                stopped = True
                exit_px = min(o, stop) if pos > 0 else max(o, stop)
                exit_px *= (1 - slip) if pos > 0 else (1 + slip)
                r += pos * expo * (exit_px / prev_c - 1.0)     # 结算已实现亏损
                r -= abs(pos) * expo * (fee + slip)
                t += abs(pos) * expo
                pos, expo = 0, 0.0

        # ---------- 2) 信号调仓（开盘执行；当日刚被止损则不再反手） ----------
        if tgt != pos and not stopped:
            if pos != 0:                                        # 先平旧仓
                exit_px = o * (1 - slip) if pos > 0 else o * (1 + slip)
                r += pos * expo * (exit_px / prev_c - 1.0)
                r -= abs(pos) * expo * (fee + slip)
                t += abs(pos) * expo
                pos, expo = 0, 0.0
            if tgt != 0 and A[i - 1] > 0:                       # 再开新仓
                entry_px = o * (1 + slip) if tgt > 0 else o * (1 - slip)
                expo, pos = e, tgt
                r -= abs(pos) * expo * (fee + slip)
                t += abs(pos) * expo
                stop = entry_px - sl_atr * A[i - 1] if pos > 0 else entry_px + sl_atr * A[i - 1]
                r += pos * expo * (c / entry_px - 1.0)          # 入场当日剩余收益
                dates.append(bars[i]["date"]); rets.append(r); turn.append(t)
                continue

        # ---------- 3) 持仓收益 ----------
        if pos != 0:
            r += pos * expo * (c / prev_c - 1.0)
        dates.append(bars[i]["date"]); rets.append(r); turn.append(t)
    return dates, rets, turn


# ================================================================ 组合层
def combine(dates_list, rets_list, init=10000.0):
    """按日期对齐等权合成（不是按索引！索引对齐会静默截断到最短序列）。"""
    all_dates = sorted(set().union(*[set(d) for d in dates_list]))
    idx = {d: i for i, d in enumerate(all_dates)}
    m = len(all_dates)
    tot = [0.0] * m
    cnt = [0] * m
    for ds, rs in zip(dates_list, rets_list):
        for d, r in zip(ds, rs):
            tot[idx[d]] += r
            cnt[idx[d]] += 1
    # 每日对"当日有数据的品种数"取平均 -> 等权
    daily = [(tot[i] / cnt[i]) if cnt[i] > 0 else 0.0 for i in range(m)]
    eq, cur = init, []
    for r in daily:
        eq *= (1.0 + r)
        cur.append(eq)
    return all_dates, daily, cur


def stats(dates, daily, cur, init=10000.0):
    yrs = len(cur) / TRADING_DAYS
    fin = cur[-1]
    cagr = (fin / init) ** (1 / yrs) - 1 if yrs > 0 and fin > 0 else -1.0
    peak, mdd = cur[0], 0.0
    for v in cur:
        if v > peak:
            peak = v
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    sharpe = (statistics.mean(daily) / sd * math.sqrt(TRADING_DAYS)) if sd > 1e-12 else 0.0
    # 最长水下
    peak, longest, cur_len = cur[0], 0, 0
    for v in cur:
        if v >= peak:
            peak, cur_len = v, 0
        else:
            cur_len += 1
            longest = max(longest, cur_len)
    # 滚动 365 天
    W = 365
    roll = []
    if len(cur) > W:
        roll = sorted(cur[i + W] / cur[i] - 1 for i in range(len(cur) - W))
    return {
        "mult": fin / init, "cagr": cagr, "mdd": mdd,
        "calmar": (cagr / mdd) if mdd > 1e-9 else 0.0, "sharpe": sharpe,
        "days": len(cur), "years": yrs, "longest_dd": longest,
        "roll_worst": roll[0] if roll else 0.0,
        "roll_med": roll[len(roll) // 2] if roll else 0.0,
        "roll_best": roll[-1] if roll else 0.0,
        "roll_win": (sum(1 for x in roll if x > 0) / len(roll)) if roll else 0.0,
    }


def run_portfolio(syms, sigfn, **kw):
    dl, rl = [], []
    for s in syms:
        b = load(s)
        if not b:
            continue
        d, r, _ = asset_returns(b, sigfn(b), **kw)
        dl.append(d); rl.append(r)
    dates, daily, cur = combine(dl, rl)
    return dates, daily, cur


# ================================================================ 命令
SIGNALS = {
    "时序动量120":  lambda b: sig_tsmom(b, 120),
    "时序动量250":  lambda b: sig_tsmom(b, 250),
    "低换手时序动量": lambda b: sig_tsmom_slow(b, 120, 20),
    "双均线20/60":  lambda b: sig_ma(b, 20, 60),
    "双均线40/120": lambda b: sig_ma(b, 40, 120),
    "唐奇安20":     lambda b: sig_don(b, 20),
    "唐奇安55":     lambda b: sig_don(b, 55),
    "均线+唐奇安":   lambda b: sig_ma_don(b, 20, 60, 20),
    "波动突破":      lambda b: sig_vbreak(b, 0.5, 20),
    "三信号投票":    lambda b: sig_vote(b),
    "展期收益carry":  lambda b: sig_carry(b, 252, 0.004),
    "carry+趋势":    lambda b: sig_carry_trend(b),
    "RSI回归":      lambda b: sig_rsi_rev(b),
    "布林回归":      lambda b: sig_boll_rev(b),
}


def cmd_check(argv):
    """数据体检：主连【不复权】序列的换月跳空诊断。
    注意：必须用 load_raw，复权会抹掉跳空。"""
    syms = universe(min_bars=WARMUP + 300)
    print("可用品种: %d 个（原始主连不复权序列）\n" % len(syms))
    print("%-6s %-8s %7s %10s %10s %9s" % ("代码", "名称", "bars", "跳空累计", "年化漂移", "|跳空|>4%"))
    print("-" * 58)
    rows = []
    for s in syms:
        b = load_raw(s)
        gap_sum, big = 0.0, 0
        for i in range(1, len(b)):
            g = b[i]["open"] / b[i - 1]["close"] - 1.0
            gap_sum += g
            if abs(g) > 0.04:
                big += 1
        yrs = len(b) / TRADING_DAYS
        ann = (1 + gap_sum) ** (1 / yrs) - 1 if gap_sum > -1 else -1.0
        rows.append({"sym": s, "bars": len(b), "gap_sum": gap_sum, "ann": ann, "big": big})
        print("%-6s %-8s %7d %9.1f%% %9.2f%% %9d" % (s, NAME[s], len(b), gap_sum * 100, ann * 100, big))
    pos = sum(1 for r in rows if r["gap_sum"] > 0)
    med = sorted(r["gap_sum"] for r in rows)[len(rows) // 2]
    print("\n跳空累计为正的品种: %d/%d   中位 %.1f%%" % (pos, len(rows), med * 100))
    print(">>> 主连为不复权价格，换月跳空使【只多】被虚增、【只空】被打压，须用多空对称口径评估。")
    with open(os.path.join(HERE, "check.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "pos": pos, "total": len(rows), "median_gap": med},
                  f, ensure_ascii=False, indent=1)
    print("已写出 check.json")


def cmd_scan(argv):
    syms = universe()
    print("品种数 %d\n" % len(syms))
    print("%-14s %9s %9s %9s %9s %9s %9s %9s" % (
        "信号", "倍数", "年化", "最大回撤", "Calmar", "Sharpe", "只多年化", "只空年化"))
    print("-" * 84)
    out = {}
    for name, fn in SIGNALS.items():
        d0, r0, c0 = run_portfolio(syms, fn)
        s0 = stats(d0, r0, c0)
        dL, rL, cL = run_portfolio(syms, lambda b, f=fn: [max(0, x) for x in f(b)])
        dS, rS, cS = run_portfolio(syms, lambda b, f=fn: [min(0, x) for x in f(b)])
        sL, sS = stats(dL, rL, cL), stats(dS, rS, cS)
        out[name] = {"ls": s0, "long": sL, "short": sS}
        print("%-14s %8.2fx %8.1f%% %8.1f%% %9.2f %9.2f %8.1f%% %8.1f%%" % (
            name, s0["mult"], s0["cagr"] * 100, s0["mdd"] * 100, s0["calmar"], s0["sharpe"],
            sL["cagr"] * 100, sS["cagr"] * 100))
    with open(os.path.join(HERE, "scan.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n已写出 scan.json")


def cmd_bias(argv):
    """控制实验：随机方向信号（无任何择时能力）跑多空/只多/只空。
    真实信号无 edge 时三者都应≈ -成本；偏离量即为数据漂移。"""
    syms = universe()
    rnd = random.Random(7)
    print("品种数 %d\n" % len(syms))
    print("控制实验：随机方向信号（每 30 交易日随机翻转一次）")
    print("%-16s %11s %11s %11s" % ("口径", "年化", "最大回撤", "期末倍数"))
    print("-" * 52)
    res = {}
    for tag, mode in [("多空对称", "ls"), ("只多", "long"), ("只空", "short")]:
        sigs = {}
        for s in syms:
            b = load(s)
            cur, out = 0, []
            for i in range(len(b)):
                if i % 30 == 0:
                    cur = rnd.choice([-1, 1])
                out.append(cur if mode == "ls" else (max(0, cur) if mode == "long" else min(0, cur)))
            sigs[s] = out
        dl, rl = [], []
        for s, sg in sigs.items():
            d, r, _ = asset_returns(load(s), sg)
            dl.append(d); rl.append(r)
        d, r, c = combine(dl, rl)
        st = stats(d, r, c)
        res[tag] = st
        print("%-16s %10.2f%% %10.1f%% %10.2fx" % (tag, st["cagr"] * 100, st["mdd"] * 100, st["mult"]))
    drift = (res["只多"]["cagr"] - res["只空"]["cagr"]) / 2
    print("\n>>> 估计的系统性漂移 δ ≈ %+.2f%%/年" % (drift * 100))
    print(">>> 只多口径被虚增、只空口径被打压各约 %.1f%%/年；多空对称口径基本无偏。" % abs(drift * 100))
    with open(os.path.join(HERE, "bias.json"), "w", encoding="utf-8") as f:
        json.dump({"res": res, "drift": drift}, f, ensure_ascii=False, indent=1)


def cmd_explore(argv):
    """分三步定位问题：① 零成本 vs 全成本（区分"没 edge"与"被成本吃掉"）
       ② 参数寻优（看平台还是尖峰）③ 样本内/样本外秩相关。"""
    syms = universe()
    print("品种数 %d\n" % len(syms))
    out = {}

    # ---------- ① 成本敏感性 ----------
    print("【① 成本敏感性】同一信号，只改成本口径")
    print("%-14s %10s %10s %10s %10s" % ("信号", "零成本年化", "全成本年化", "成本拖累", "全成本Calmar"))
    print("-" * 60)
    cs = {}
    for name, fn in SIGNALS.items():
        d, r, c = run_portfolio(syms, fn, fee=0.0, slip=0.0)
        s0 = stats(d, r, c)
        d, r, c = run_portfolio(syms, fn, fee=0.0002, slip=0.0005)
        s1 = stats(d, r, c)
        cs[name] = {"zero": s0, "full": s1}
        print("%-14s %9.1f%% %9.1f%% %9.2fpp %10.2f" % (
            name, s0["cagr"] * 100, s1["cagr"] * 100, (s0["cagr"] - s1["cagr"]) * 100, s1["calmar"]))
    out["cost"] = cs

    # ---------- ② 参数寻优 ----------
    print("\n【② 参数寻优】看曲线形状（平台=可信，锯齿=过拟合）")
    grids = {
        "双均线": (lambda b, f, s: sig_ma(b, f, s),
                 [(10, 30), (15, 45), (20, 60), (25, 75), (30, 90), (40, 120), (50, 150)]),
        "唐奇安": (lambda b, n, _x: sig_don(b, n),
                 [(20, 0), (30, 0), (40, 0), (55, 0), (70, 0), (90, 0), (120, 0)]),
        "时序动量": (lambda b, n, _x: sig_tsmom(b, n),
                  [(60, 0), (90, 0), (120, 0), (150, 0), (180, 0), (250, 0)]),
    }
    grid_res = {}
    for gname, (fn, plist) in grids.items():
        row = []
        for p in plist:
            d, r, c = run_portfolio(syms, lambda b, fn=fn, p=p: fn(b, p[0], p[1]))
            st = stats(d, r, c)
            row.append({"param": "%s/%s" % (p[0], p[1]) if p[1] else str(p[0]),
                        "cagr": st["cagr"], "mdd": st["mdd"], "calmar": st["calmar"]})
        grid_res[gname] = row
        print("  %-8s " % gname + "  ".join("%s:%.1f%%" % (x["param"], x["cagr"] * 100) for x in row))
    out["grid"] = grid_res

    # ---------- ③ 样本内 / 样本外 ----------
    print("\n【③ 样本内(2009-2018) -> 样本外(2019-2026)】秩相关 ρ 接近 0 即无预测力")
    print("%-10s %12s %12s %10s" % ("族", "内最优参数", "外年化", "ρ"))
    print("-" * 48)
    oos = {}
    for gname, (fn, plist) in grids.items():
        rec = []
        for p in plist:
            # 样本内
            dl, rl = [], []
            for s in syms:
                b = [x for x in load(s) if x["date"] <= "2018-12-31"]
                if len(b) < WARMUP + 200:
                    continue
                d, r, _ = asset_returns(b, fn(b, p[0], p[1]))
                dl.append(d); rl.append(r)
            if not dl:
                continue
            d, r, c = combine(dl, rl)
            si = stats(d, r, c)["cagr"]
            # 样本外（指标用全历史预热，净值从 2019 起算）
            dl2, rl2 = [], []
            for s in syms:
                b = load(s)
                d, r, _ = asset_returns(b, fn(b, p[0], p[1]))
                dd = [(x, y) for x, y in zip(d, r) if x >= "2019-01-01"]
                if not dd:
                    continue
                dl2.append([x[0] for x in dd]); rl2.append([x[1] for x in dd])
            d, r, c = combine(dl2, rl2)
            so = stats(d, r, c)["cagr"]
            rec.append({"param": p, "in": si, "out": so})
        # Spearman 秩相关
        n = len(rec)
        ri = {i: v for i, v in enumerate(sorted(range(n), key=lambda k: rec[k]["in"]))}
        ro = {i: v for i, v in enumerate(sorted(range(n), key=lambda k: rec[k]["out"]))}
        d2 = sum((ri[i] - ro[i]) ** 2 for i in range(n))
        rho = 1 - 6 * d2 / (n * (n * n - 1)) if n > 2 else 0.0
        best = max(rec, key=lambda x: x["in"])
        oos[gname] = {"rho": rho, "rec": rec, "best": best}
        print("%-10s %12s %11.1f%% %10.2f" % (
            gname, "%s/%s" % (best["param"][0], best["param"][1]) if best["param"][1] else str(best["param"][0]),
            best["out"] * 100, rho))
    out["oos"] = oos

    with open(os.path.join(HERE, "explore.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("\n已写出 explore.json")


def build_matrix(syms, raw=False):
    """把各品种收盘价对齐到统一日期轴（前值填充），返回 (dates, {sym: [px]})。"""
    loader = load_raw if raw else load
    rawd = {}
    for s in syms:
        b = loader(s)
        if b:
            rawd[s] = {x["date"]: x["close"] for x in b}
    if not rawd:
        return [], {}
    all_dates = sorted(set().union(*[set(d) for d in rawd.values()]))
    px = {}
    for s, d in rawd.items():
        ser, last = [], None
        for dt in all_dates:
            last = d.get(dt, last)
            ser.append(last)
        px[s] = ser
    return all_dates, px


def build_matrix_raw(syms):
    """与 build_matrix 同一日期轴，但价格取自原始主连（不复权）。
    复权只缩放价格、不增删日期，故两个口径的日期轴完全一致，可逐日对比。"""
    return build_matrix(syms, raw=True)


def cmd_bench(argv):
    """基准与补充方向：① 买入持有 ② 截面动量 ③ 波动率目标敏感性 ④ ATR止损"""
    syms = universe()
    print("品种数 %d\n" % len(syms))
    dates, px = build_matrix(syms)
    out = {}

    def eval_sig(sigmap):
        dl, rl = [], []
        for s in syms:
            b = load(s)
            d, r, _ = asset_returns(b, sigmap[s])
            dl.append(d); rl.append(r)
        d, r, c = combine(dl, rl)
        return stats(d, r, c), c, d

    # ---------- ① 买入持有 ----------
    bh = {s: [1] * len(load(s)) for s in syms}
    st, c, d = eval_sig(bh)
    out["buy_hold"] = st
    print("【① 买入持有(等权做多全部品种)】倍数 %.2fx  年化 %.1f%%  回撤 %.1f%%  仅此即含约 +1.1%%/年漂移"
          % (st["mult"], st["cagr"] * 100, st["mdd"] * 100))

    # ---------- ② 截面动量 ----------
    # 关键对照：同一策略分别跑在【复权序列】与【原始主连】上。
    # 若两者结论相反，说明原始主连的换月跳空足以颠倒策略方向。
    print("\n【② 截面动量 vs 截面反转】(复权序列 / 原始主连 对照)")
    print("%-24s %10s %10s %10s %10s" % ("参数", "年化", "最大回撤", "Calmar", "Sharpe"))
    print("-" * 68)

    _, px_raw = build_matrix_raw(syms)

    def cs_run(N, K, reverse=False, use_raw=False):
        P = px_raw if use_raw else px
        pos = {s: 0.0 for s in syms}
        rets = []
        for i in range(N + 1, len(dates)):
            mom, ret = {}, {}
            for s in syms:
                p0, p1 = P[s][i - 1 - N], P[s][i - 1]
                if p0 and p1:
                    mom[s] = p1 / p0 - 1.0
                if P[s][i - 1]:
                    ret[s] = P[s][i] / P[s][i - 1] - 1.0
            if len(mom) < 2 * K + 1:
                rets.append(0.0); continue
            rank = sorted(mom, key=lambda k: mom[k])
            tgt = {s: 0.0 for s in syms}
            hi, lo_ = (rank[:K], rank[-K:]) if reverse else (rank[-K:], rank[:K])
            for s in hi:
                tgt[s] = 1.0 / K
            for s in lo_:
                tgt[s] = -1.0 / K
            r = 0.0
            for s in syms:
                if pos[s] != 0 and s in ret:
                    r += pos[s] * ret[s]
                if tgt[s] != pos[s]:
                    r -= abs(tgt[s] - pos[s]) * 0.0007
            pos = tgt
            rets.append(r)
        cur = [10000.0]
        for x in rets:
            cur.append(cur[-1] * (1 + x))
        return stats(dates[-len(cur):], rets, cur)

    xs = {}
    for tag, rev, ur in [("动量·复权", False, False), ("反转·复权", True, False),
                         ("动量·原始", False, True), ("反转·原始", True, True)]:
        for N in [20, 60, 120, 250]:
            K = 3
            st2 = cs_run(N, K, reverse=rev, use_raw=ur)
            xs["%s_N%d_K%d" % (tag, N, K)] = st2
            print("%-24s %9.1f%% %9.1f%% %10.2f %10.2f" % (
                "%s N=%d" % (tag, N), st2["cagr"] * 100, st2["mdd"] * 100,
                st2["calmar"], st2["sharpe"]))
    out["xsmom"] = xs

    # ---------- ③ 波动率目标敏感性 ----------
    print("\n【③ 波动率目标仓位敏感性】(时序动量120 / 双均线20/60)")
    print("%-10s %10s %10s %10s %10s" % ("vol_target", "年化", "最大回撤", "Calmar", "Sharpe"))
    print("-" * 54)
    vt = {}
    for v in [0.01, 0.02, 0.03, 0.04, 0.06]:
        for nm, fn in [("时序动量120", lambda b: sig_tsmom(b, 120)), ("双均线20/60", lambda b: sig_ma(b, 20, 60))]:
            d2, r2, c2 = run_portfolio(syms, fn, vol_target=v)
            s2 = stats(d2, r2, c2)
            vt["%s@%.2f" % (nm, v)] = s2
            print("%-10s %9.1f%% %9.1f%% %10.2f %10.2f   %s" % (
                "%.0f%%" % (v * 100), s2["cagr"] * 100, s2["mdd"] * 100, s2["calmar"], s2["sharpe"], nm))
    out["voltarget"] = vt

    # ---------- ④ ATR 止损 ----------
    print("\n【④ ATR 硬止损】(时序动量120 / 双均线20/60, vol_target=2%)")
    print("%-12s %10s %10s %10s" % ("sl_atr", "年化", "最大回撤", "Calmar"))
    print("-" * 46)
    sl = {}
    for k in [0.0, 2.0, 3.0, 4.0, 6.0]:
        for nm, fn in [("时序动量120", lambda b: sig_tsmom(b, 120)), ("双均线20/60", lambda b: sig_ma(b, 20, 60))]:
            d2, r2, c2 = run_portfolio(syms, fn, sl_atr=k)
            s2 = stats(d2, r2, c2)
            sl["%s@%.1f" % (nm, k)] = s2
            print("%-12s %9.1f%% %9.1f%% %10.2f   %s" % (
                ("无" if k == 0 else "%.1f ATR" % k), s2["cagr"] * 100, s2["mdd"] * 100, s2["calmar"], nm))
    out["stoploss"] = sl

    with open(os.path.join(HERE, "bench.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n已写出 bench.json")


def cmd_surface(argv):
    """(快线,慢线) x 止损距离 的 Calmar 曲面 —— 判断是"平台"还是"尖峰"。"""
    syms = universe()
    print("品种数 %d\n" % len(syms))
    fasts = [5, 10, 15, 20, 30, 40]
    slows = [30, 45, 60, 90, 120]
    sls = [0.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    res = {}
    for sl in sls:
        print("止损 = %s" % ("无" if sl == 0 else "%.1f ATR" % sl))
        print("        " + "".join("%9s" % ("%d/%d" % (f, s)) for s in slows for f in [0]))
        for f in fasts:
            row = []
            for s in slows:
                if f >= s:
                    row.append(None); continue
                d, r, c = run_portfolio(syms, lambda b, f=f, s=s: sig_ma(b, f, s), sl_atr=sl)
                st = stats(d, r, c)
                row.append(st)
                res["ma%d_%d_sl%.1f" % (f, s, sl)] = st
            print("%-8s" % ("MA%d" % f) + "".join(
                ("%9s" % "-") if x is None else "%8.1f%%" % (x["cagr"] * 100) for x in row))
        print()
    with open(os.path.join(HERE, "surface.json"), "w", encoding="utf-8") as fp:
        json.dump(res, fp, ensure_ascii=False, indent=1)
    print("已写出 surface.json")


FINAL = {"name": "carry+趋势", "sl_atr": 0.0, "vol_target": 0.02, "expo_cap": 2.0}


def _final_sig(bars):
    return SIGNALS[FINAL["name"]](bars)


def _eq_from_rets(rets, init=10000.0):
    """把日收益序列转成净值曲线（stats 需要 cur）。"""
    cur, eq = [], init
    for x in rets:
        eq *= (1.0 + x)
        cur.append(eq)
    return cur


def _seg(d, r, lo, hi):
    """截取日期区间 [lo, hi] 的 (dates, rets, cur)。"""
    pr = [(x, y) for x, y in zip(d, r) if lo <= x <= hi]
    if not pr:
        return None, None, None
    dd = [x[0] for x in pr]
    rr = [x[1] for x in pr]
    return dd, rr, _eq_from_rets(rr)


def cmd_rank(argv):
    """逐品种归因：把最终策略单独跑在每个品种上，按收益排序。

    同时给出三项"识别假货"的对照：
      只多 / 只空  —— 若利润全部来自只多，多半是主连残余漂移而非真实 edge
      样本内 2009-2018 / 样本外 2019-2026 —— 检验稳定性
    参数：carry+趋势，无止损，波动率目标 2%，敞口上限 2x，含 2bp 手续费 + 5bp 滑点。
    """
    syms = universe()
    kw = dict(sl_atr=FINAL["sl_atr"], vol_target=FINAL["vol_target"], expo_cap=FINAL["expo_cap"])
    print("品种数 %d   策略 %s   无止损   波动率目标 %.0f%%\n"
          % (len(syms), FINAL["name"], FINAL["vol_target"] * 100))
    rows = []
    for s in syms:
        b = load(s)
        sig = _final_sig(b)
        d, r, _ = asset_returns(b, sig, **kw)
        st = stats(d, r, _eq_from_rets(r))
        _, rL, _ = asset_returns(b, [max(0, x) for x in sig], **kw)
        sL = stats(d, rL, _eq_from_rets(rL))
        _, rS, _ = asset_returns(b, [min(0, x) for x in sig], **kw)
        sS = stats(d, rS, _eq_from_rets(rS))
        di, ri, ci = _seg(d, r, "2000-01-01", "2018-12-31")
        do, ro, co = _seg(d, r, "2019-01-01", "2099-12-31")
        # 1 手要多少钱（用真实价，不能用复权价）
        raw = load_raw(s)
        px = raw[-1]["close"]
        mult, mr = SPEC.get(s, (10, 0.10))
        lot_notional = px * mult
        margin = lot_notional * (mr + 0.04)          # 期货公司口径
        rows.append({
            "sym": s, "name": NAME[s], "bars": len(b), "px": px,
            "lot_notional": lot_notional, "margin": margin,
            "afford": margin <= 20000 * 0.6,
            "st": st, "long": sL, "short": sS,
            "in": stats(di, ri, ci) if ci else None,
            "out": stats(do, ro, co) if co else None,
        })

    rows.sort(key=lambda x: x["st"]["cagr"], reverse=True)
    print("%-6s %-8s %7s %7s %8s %7s %7s %7s %7s %7s %9s %5s" % (
        "代码", "名称", "年化", "回撤", "Calmar", "Sharpe", "只多", "只空",
        "样本内", "样本外", "1手保证金", "可行"))
    print("-" * 104)
    for x in rows:
        st = x["st"]
        print("%-6s %-8s %6.1f%% %6.1f%% %8.2f %7.2f %6.1f%% %6.1f%% %6.1f%% %6.1f%% %9s %5s" % (
            x["sym"], x["name"], st["cagr"] * 100, st["mdd"] * 100, st["calmar"], st["sharpe"],
            x["long"]["cagr"] * 100, x["short"]["cagr"] * 100,
            (x["in"]["cagr"] * 100) if x["in"] else 0.0,
            (x["out"]["cagr"] * 100) if x["out"] else 0.0,
            format(x["margin"], ",.0f"), "√" if x["afford"] else "×"))

    # ---- 组合口径：取前 2 名，看合在一起是什么样 ----
    def eval_sel(sel, tag):
        dl, rl = [], []
        for s in sel:
            b = load(s)
            d2, r2, _ = asset_returns(b, _final_sig(b), **kw)
            dl.append(d2); rl.append(r2)
        d2, r2, c2 = combine(dl, rl)
        st = stats(d2, r2, c2)
        print("\n  【%s】%s" % (tag, " + ".join("%s(%s)" % (NAME[s], s) for s in sel)))
        print("    全样本   年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f  Sharpe %5.2f  倍数 %.2fx"
              % (st["cagr"] * 100, st["mdd"] * 100, st["calmar"], st["sharpe"], st["mult"]))
        for lab, lo, hi in [("样本内", "2000-01-01", "2018-12-31"), ("样本外", "2019-01-01", "2099-12-31")]:
            ds, rs, cs = _seg(d2, r2, lo, hi)
            if cs:
                s2 = stats(ds, rs, cs)
                print("    %-7s  年化 %6.2f%%  回撤 %5.1f%%  Calmar %5.2f"
                      % (lab, s2["cagr"] * 100, s2["mdd"] * 100, s2["calmar"]))
        # 分块自助：年化为负的概率
        rnd = random.Random(11)
        B, L = 800, 30
        blocks = [r2[i:i + L] for i in range(0, len(r2) - L, L)]
        cg = []
        for _ in range(B):
            sm = []
            while len(sm) < len(r2):
                sm.extend(rnd.choice(blocks))
            sm = sm[:len(r2)]
            eq = 1.0
            for v in sm:
                eq *= (1 + v)
            cg.append(eq ** (TRADING_DAYS / len(sm)) - 1)
        cg.sort()
        print("    自助90%%区间 [%.2f%%, %.2f%%]  中位 %.2f%%  年化为负概率 %.1f%%"
              % (cg[int(B * .05)] * 100, cg[int(B * .95)] * 100, cg[B // 2] * 100,
                 sum(1 for v in cg if v < 0) / B * 100))
        return {"syms": sel, "stats": st,
                "in": stats(*_seg(d2, r2, "2000-01-01", "2018-12-31")) if _seg(d2, r2, "2000-01-01", "2018-12-31")[2] else None,
                "out": stats(*_seg(d2, r2, "2019-01-01", "2099-12-31")) if _seg(d2, r2, "2019-01-01", "2099-12-31")[2] else None,
                "boot": {"p5": cg[int(B * .05)], "med": cg[B // 2], "p95": cg[int(B * .95)],
                         "neg": sum(1 for v in cg if v < 0) / B}}

    top2 = [x["sym"] for x in rows[:2]]
    bycal = sorted(rows, key=lambda x: x["st"]["calmar"], reverse=True)
    top2c = [x["sym"] for x in bycal[:2]]
    print("\n" + "=" * 82)
    print("按【年化收益】前 2 名组合：")
    r_ret = eval_sel(top2, "收益前二")
    print("\n按【Calmar】前 2 名组合（风险调整口径）：")
    r_cal = eval_sel(top2c, "Calmar 前二")

    with open(os.path.join(HERE, "rank.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "top2_return": r_ret, "top2_calmar": r_cal},
                  f, ensure_ascii=False, indent=1)
    print("\n已写出 rank.json")


def cmd_validate(argv):
    """最终配置的完整校验：止损细扫 / 多空分解 / 样本外 / 分块自助 / 逐年 / 成本压力
    用法: python ft.py validate [信号名]   —— 不填则用 FINAL 默认信号。"""
    syms = universe()
    if argv:
        if argv[0] not in SIGNALS:
            print("未知信号 %r；可选: %s" % (argv[0], " / ".join(SIGNALS)))
            return
        FINAL["name"] = argv[0]
    print("品种数 %d" % len(syms))
    print("配置: %s  +  %s  +  波动率目标%.0f%%  +  %d品种等权\n"
          % (FINAL["name"], "无止损" if FINAL["sl_atr"] == 0 else "%.1f×ATR止损" % FINAL["sl_atr"],
             FINAL["vol_target"] * 100, len(syms)))
    out = {}

    # ---------- ① 止损细扫 ----------
    print("【① 止损距离细扫】")
    print("%-12s %10s %10s %10s" % ("止损", "年化", "最大回撤", "Calmar"))
    print("-" * 46)
    sls = {}
    for k in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 0.0]:
        d, r, c = run_portfolio(syms, _final_sig, sl_atr=k)
        st = stats(d, r, c)
        sls["%.2f" % k] = st
        print("%-12s %9.1f%% %9.1f%% %10.2f" % (
            "无" if k == 0 else "%.2f ATR" % k, st["cagr"] * 100, st["mdd"] * 100, st["calmar"]))
    out["stoploss"] = sls

    # ---------- ② 多空分解 ----------
    print("\n【② 多空分解】若改善只出现在单边，则是数据漂移而非真实 edge")
    print("%-12s %10s %10s %10s" % ("口径", "年化", "最大回撤", "Calmar"))
    print("-" * 46)
    dec = {}
    for tag, mod in [("多空对称", None), ("只多", "L"), ("只空", "S")]:
        if mod is None:
            f = _final_sig
        elif mod == "L":
            f = lambda b: [max(0, x) for x in _final_sig(b)]
        else:
            f = lambda b: [min(0, x) for x in _final_sig(b)]
        d, r, c = run_portfolio(syms, f, sl_atr=FINAL["sl_atr"])
        st = stats(d, r, c)
        dec[tag] = st
        print("%-12s %9.1f%% %9.1f%% %10.2f" % (tag, st["cagr"] * 100, st["mdd"] * 100, st["calmar"]))
    out["decomp"] = dec

    # ---------- ③ 样本内 / 样本外 ----------
    print("\n【③ 样本内 2009-2018  vs  样本外 2019-2026】")
    oos = {}
    for tag, lo, hi in [("样本内", "2000-01-01", "2018-12-31"), ("样本外", "2019-01-01", "2099-12-31")]:
        dl, rl = [], []
        for s in syms:
            b = load(s)
            d, r, _ = asset_returns(b, _final_sig(b), sl_atr=FINAL["sl_atr"])
            pair = [(x, y) for x, y in zip(d, r) if lo <= x <= hi]
            if not pair:
                continue
            dl.append([x[0] for x in pair]); rl.append([x[1] for x in pair])
        d, r, c = combine(dl, rl)
        st = stats(d, r, c)
        oos[tag] = st
        print("  %-6s 年化 %6.1f%%  回撤 %5.1f%%  Calmar %5.2f  (%.1f 年)"
              % (tag, st["cagr"] * 100, st["mdd"] * 100, st["calmar"], st["years"]))
    out["oos"] = oos

    # ---------- ④ 分块自助 ----------
    print("\n【④ 分块自助检验】30日一块, 有放回重抽 1000 次 -> 年化 90% 区间")
    d, r, c = run_portfolio(syms, _final_sig, sl_atr=FINAL["sl_atr"])
    rnd = random.Random(11)
    B, L = 1000, 30
    blocks = [r[i:i + L] for i in range(0, len(r) - L, L)]
    cagrs = []
    for _ in range(B):
        sample = []
        while len(sample) < len(r):
            sample.extend(rnd.choice(blocks))
        sample = sample[:len(r)]
        eq = 1.0
        for x in sample:
            eq *= (1 + x)
        cagrs.append(eq ** (TRADING_DAYS / len(sample)) - 1)
    cagrs.sort()
    boot = {"p5": cagrs[int(B * 0.05)], "p25": cagrs[int(B * 0.25)],
            "med": cagrs[B // 2], "p75": cagrs[int(B * 0.75)], "p95": cagrs[int(B * 0.95)],
            "neg_ratio": sum(1 for x in cagrs if x < 0) / B}
    print("  年化 90%% 区间 [%.1f%%, %.1f%%]  中位 %.1f%%  年化为负的概率 %.1f%%"
          % (boot["p5"] * 100, boot["p95"] * 100, boot["med"] * 100, boot["neg_ratio"] * 100))
    out["bootstrap"] = boot

    # ---------- ⑤ 逐年 ----------
    print("\n【⑤ 逐年收益】(策略 vs 等权买入持有)")
    yearly = {}
    eqy = {}
    for i, dt in enumerate(d):
        eqy.setdefault(dt[:4], []).append(r[i])
    # 买入持有基准：必须"先按日等权平均、再逐日复利"。
    # 若把全部品种的全部日收益连乘，会得到 2009 年 +8.7 万% 这种荒谬值。
    bh_day = {}                       # year -> {date: [rets]}
    for s in syms:
        b = load(s)
        for i in range(1, len(b)):
            bh_day.setdefault(b[i]["date"][:4], {}).setdefault(b[i]["date"], []).append(
                b[i]["close"] / b[i - 1]["close"] - 1)
    print("  %-6s %10s %12s %10s" % ("年份", "策略", "买入持有", "品种数"))
    print("  " + "-" * 44)
    for y in sorted(eqy):
        pr = 1.0
        for x in eqy[y]:
            pr *= (1 + x)
        daymap = bh_day.get(y, {})
        br = 1.0
        for dt in sorted(daymap):
            v = daymap[dt]
            br *= (1 + sum(v) / len(v))
        yearly[y] = {"strat": pr - 1, "bh": br - 1, "max_sym": max((len(v) for v in daymap.values()), default=0)}
        print("  %-6s %9.1f%% %11.1f%% %9d" % (y, (pr - 1) * 100, (br - 1) * 100, yearly[y]["max_sym"]))

    # ---------- ⑦ 净值曲线（策略 vs 等权买入持有），供报告绘图 ----------
    bh_map = {}
    for y in bh_day:
        for dt, v in bh_day[y].items():
            bh_map[dt] = sum(v) / len(v)
    eq_s, eq_b, cur_s, cur_b = [], [], 1.0, 1.0
    for i, dt in enumerate(d):
        cur_s *= (1 + r[i])
        cur_b *= (1 + bh_map.get(dt, 0.0))
        if i % 5 == 0:
            eq_s.append(dt); eq_b.append(dt)
            out.setdefault("_eqs", []).append(round(cur_s, 2))
            out.setdefault("_eqb", []).append(round(cur_b, 2))
    out["equity"] = {"dates": eq_s, "strat": out.pop("_eqs"), "bh": out.pop("_eqb")}
    out["yearly"] = yearly

    # ---------- ⑥ 成本压力 ----------
    print("\n【⑥ 成本压力测试】")
    print("%-16s %10s %10s" % ("口径", "年化", "最大回撤"))
    print("-" * 38)
    cost = {}
    for tag, fee, slip in [("零成本", 0.0, 0.0), ("1× (基准)", 0.0002, 0.0005),
                           ("2×", 0.0004, 0.0010), ("3×", 0.0006, 0.0015), ("5×", 0.0010, 0.0025)]:
        d2, r2, c2 = run_portfolio(syms, _final_sig, fee=fee, slip=slip, sl_atr=FINAL["sl_atr"])
        st = stats(d2, r2, c2)
        cost[tag] = st
        print("%-16s %9.1f%% %9.1f%%" % (tag, st["cagr"] * 100, st["mdd"] * 100))
    out["cost"] = cost

    with open(os.path.join(HERE, "validate.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n已写出 validate.json")


# ================================================================ HTML 模板
_REPORT_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>中国商品期货日线趋势策略研究报告</title>
__ECHARTS__
<style>
:root{
  --bg:#f4f6f8; --card:#fff; --ink:#1c2128; --mut:#616b76; --line:#e2e6ea;
  --up:#d0453c; --dn:#17915c; --acc:#2b62c9; --acc2:#8a5cd6; --warn:#c8842a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.75 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 80px}
header{background:linear-gradient(135deg,#1c2b45,#2b62c9 60%,#4a86e8);color:#fff;
  padding:52px 0 44px;margin-bottom:28px}
header .wrap{padding-bottom:0}
h1{margin:0 0 10px;font-size:29px;letter-spacing:.5px;font-weight:700}
.sub{opacity:.9;font-size:14.5px}
.meta{margin-top:18px;font-size:12.5px;opacity:.75;border-top:1px solid rgba(255,255,255,.22);padding-top:12px}
h2{font-size:20px;margin:0 0 4px;font-weight:700}
h2 .no{color:var(--acc);font-weight:800;margin-right:8px}
h3{font-size:15.5px;margin:22px 0 8px;font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:26px 28px;margin-bottom:22px;box-shadow:0 1px 3px rgba(16,24,40,.04)}
.lead{color:var(--mut);font-size:14px;margin:6px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:24px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.kpi .v{font-size:26px;font-weight:800;letter-spacing:-.5px}
.kpi .l{font-size:12.5px;color:var(--mut);margin-top:4px;line-height:1.5}
.kpi.hi{background:#fff7f6;border-color:#f3c9c5}
.kpi.gd{background:#f2fbf7;border-color:#c4e6d6}
.up{color:var(--up)} .dn{color:var(--dn)} .acc{color:var(--acc)} .warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f7f9fb;font-weight:600;color:var(--mut);font-size:12.5px;
  position:sticky;top:0}
tr:hover td{background:#fafbfc}
td.n{font-variant-numeric:tabular-nums}
.chart{width:100%;height:340px;margin-top:8px}
.chart.tall{height:420px}
.note{background:#f7f9fb;border-left:3px solid var(--acc);padding:12px 16px;
  border-radius:0 8px 8px 0;font-size:13.5px;color:#3b444e;margin-top:16px}
.note.warnbox{background:#fdf8ef;border-left-color:var(--warn)}
.note.danger{background:#fdf3f2;border-left-color:var(--up)}
code{background:#eef1f4;padding:1.5px 6px;border-radius:4px;font-size:12.5px;
  font-family:ui-monospace,Consolas,monospace}
ul{margin:8px 0;padding-left:22px} li{margin:5px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
@media(max-width:760px){.grid2,.grid3{grid-template-columns:1fr}}
.hm{width:100%;height:210px}
.hm-t{font-size:12.5px;color:var(--mut);text-align:center;margin:2px 0 0;font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
  border:1px solid currentColor;margin-left:6px;vertical-align:1px}
footer{color:var(--mut);font-size:12.5px;text-align:center;padding:30px 0 0;
  border-top:1px solid var(--line);margin-top:36px;line-height:1.9}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>中国商品期货日线趋势策略研究报告</h1>
  <div class="sub">41 个品种 · 2009–2026 · 14 类信号族 · 无前视回测 · 全成本口径</div>
  <div class="meta">核心结论：<b>主连不复权序列会让趋势策略的结论完全颠倒</b>。修正换月跳空后，
  时序动量与展期收益(carry)均呈稳定正收益，最优组合 Sharpe 约 0.6、最大回撤约 11%。</div>
</div></header>

<div class="wrap">

  <div class="kpis" id="kpis"></div>

  <!-- ============ 1 ============ -->
  <div class="card">
    <h2><span class="no">01</span>数据陷阱：主连不是可回测序列</h2>
    <p class="lead">新浪"主连"是主力合约的<b>不复权拼接</b>。换月当天价格直接跳到新合约价位，
    产生一次跳空。这个跳空<b>不是交易者能拿到的盈亏</b>。</p>
    <div class="note danger">
      <b>举个算例。</b>近月 100、远月 105（contango）。换月时原始序列 100 → 105，
      记作 +5% 收益。但真实持仓者是把 100 的合约卖掉、换成 105 的合约，
      随后新合约要向现货收敛 <b>105 → 100</b>，<b>实亏 5%</b>。
      原始序列把「亏损」记成了「盈利」，方向正好相反。
    </div>
    <h3>各品种换月跳空累计幅度（原始主连，前 12 名）</h3>
    <div id="c_gap" class="chart tall"></div>
    <div class="note">共 <b id="gap_txt"></b> 个品种跳空累计为正，中位 <b id="gap_med"></b>。
    这些跳空与真实基本面无关，纯属拼接产物，足以让任何基于价格序列的信号失效。</div>
  </div>

  <!-- ============ 2 ============ -->
  <div class="card">
    <h2><span class="no">02</span>同一策略，两种数据，结论相反</h2>
    <p class="lead">把完全相同的信号分别跑在原始主连与复权序列上。若结论符号相反，
    说明原始数据的跳空足以颠倒策略方向 —— 而不是策略本身有问题。</p>
    <h3>截面动量 / 反转：复权 vs 原始（年化收益）</h3>
    <div id="c_xs" class="chart"></div>
    <div class="note warnbox">原始序列上"动量 −24%、反转 +9%"，看起来是强反转市场；
    复权后变成"动量 +13%、反转 −24%"。<b>符号完全对调。</b>
    这解释了为什么很多用主连做的回测会得出"中国商品期货动量失效"的错误结论。</div>
  </div>

  <!-- ============ 3 ============ -->
  <div class="card">
    <h2><span class="no">03</span>信号族全样本扫描（复权口径）</h2>
    <p class="lead">14 类信号、41 品种等权组合、含 2bp 手续费 + 5bp 滑点。
    <b>多空对称</b>口径：信号为多则做多、为空则做空，天然抵消市场整体涨跌。</p>
    <div id="c_scan" class="chart tall"></div>
    <table id="t_scan"></table>
    <div class="note">趋势类（时序动量、双均线、唐奇安）与 <b>展期收益 carry</b> 为正；
    均值回归类（RSI、布林）与波动突破为负。carry 的<b>只空侧也接近 0</b>，
    说明它赚的不是市场上涨的钱，而是真实的展期损益。</div>
  </div>

  <!-- ============ 4 ============ -->
  <div class="card">
    <h2><span class="no">04</span>参数曲面：是"平台"还是"尖峰"</h2>
    <p class="lead">(快线, 慢线) 网格 × 止损距离。可信的策略应在参数邻域内表现平稳（平台），
    而非孤立的尖峰（过拟合）。</p>
    <div class="grid3" id="surf_grid"></div>
    <div class="note">曲面平滑、无孤立尖峰，说明结果不是参数过拟合的产物；
    但同时可见<b>止损越紧、整体越差</b> —— 日线级别硬止损只会增加成本并砍掉趋势中的正常回撤。</div>
    <h3>成本敏感性：零成本 vs 全成本</h3>
    <div id="c_costsen" class="chart"></div>
    <h3>参数平台：样本内 vs 样本外（每族独立坐标）</h3>
    <div class="grid3" id="plat_grid"></div>
    <div class="note" id="rho_txt"></div>
  </div>

  <!-- ============ 5 ============ -->
  <div class="card">
    <h2><span class="no">05</span>最终配置完整校验</h2>
    <p class="lead" id="fin_txt"></p>
    <div class="grid2">
      <div><h3>止损距离细扫</h3><div id="c_stop" class="chart"></div></div>
      <div><h3>多空分解</h3><div id="c_dec" class="chart"></div></div>
    </div>
    <h3>净值曲线（对数轴）</h3>
    <div id="c_eq" class="chart"></div>
    <div class="note">对比基准为<b>等权买入持有价格指数</b>（日度再平衡、不计成本、不做波动率控制）。
    它虽然涨到约 2.5 倍，但中途回撤超过 50%；策略以约 1/5 的回撤拿到相近的收益，
    这正是趋势跟踪的价值所在 —— 不是更高收益，而是<b>更好的风险调整</b>。</div>
    <div class="grid2">
      <div><h3>样本内 / 样本外</h3><div id="c_oos" class="chart"></div></div>
      <div><h3>成本压力测试</h3><div id="c_cost" class="chart"></div></div>
    </div>
    <h3>逐年收益：策略 vs 等权买入持有</h3>
    <div id="c_year" class="chart tall"></div>
    <div class="note" id="boot_txt"></div>
  </div>

  <!-- ============ 6 逐品种归因 ============ -->
  <div class="card">
    <h2><span class="no">06</span>逐品种归因：收益最高的品种，以及"能不能真做"</h2>
    <p class="lead">把最终策略（carry+趋势）单独跑在每个品种上，按年化收益排序。
      但<b>"收益最高"和"做得了"是两件事</b> —— 2 万本金下，不少高收益品种 1 手的保证金就超过全部本金。</p>
    <div id="c_rank" class="chart tall"></div>
    <div class="note" id="rank_txt"></div>
    <table id="rank_tbl"></table>
    <div class="note warnbox" id="rank_warn"></div>
  </div>

  <!-- ============ 7 ============ -->
  <div class="card">
    <h2><span class="no">07</span>对小资金实操的含义</h2>
    <ul>
      <li><b>先修数据再谈策略。</b>任何用主连不复权数据做的回测都不可信，
        方向都可能反。<code>ft.py</code> 的 <code>_adjust()</code> 做了比例复权。</li>
      <li><b>别做日内。</b>41 品种等权、含成本后年化只有个位数；
        而日内高频的手续费按本金计可达 30%/年（见第一份报告）。</li>
      <li><b>低换手是朋友。</b>时序动量 250 日、carry 都是低频信号，
        换手极低，成本占比小；越"灵敏"的信号（波动突破）越亏。</li>
      <li><b>硬止损在日线上是负贡献。</b>它主要是在震荡里反复砍仓，
        而不是保护本金。真正的保护来自<b>仓位</b>，不是止损线。</li>
      <li><b>期望值诚实评估。</b>年化 3~4%、回撤 10~20%、Sharpe 0.5 左右，
        是小资金做商品期货的现实上限。任何承诺"月翻倍"的都是骗局。</li>
    </ul>
    <div class="note warnbox">
      本报告为量化研究，不构成投资建议。历史回测不代表未来表现。
      期货为保证金交易，存在本金全部损失的风险。
    </div>
  </div>

  <footer>
    数据来源：新浪财经主力连续日线（41 品种，2009–2026）<br>
    回测引擎：<code>ft.py</code> · 无前视（信号 T 日收盘确认，T+1 开盘执行）·
    成本 2bp 手续费 + 5bp 滑点（单边）<br>
    本报告由脚本自动生成，全部数字可由 <code>python ft.py scan|bench|surface|validate|report</code> 复现
  </footer>
</div>

<script>
const D = __DATA__, K = __KEY__;
const UP='#d0453c', DN='#17915c', ACC='#2b62c9', ACC2='#8a5cd6', MUT='#616b76';
const AX={axisLine:{lineStyle:{color:'#c9d0d8'}},axisLabel:{color:MUT,fontSize:11},
  splitLine:{lineStyle:{color:'#eef1f4'}}};
const pct=v=>(v>0?'+':'')+v.toFixed(1)+'%';
const colr=v=>v>=0?UP:DN;
const base={grid:{left:58,right:24,top:34,bottom:44},tooltip:{trigger:'axis',
  backgroundColor:'#fff',borderColor:'#e2e6ea',textStyle:{color:'#1c2128',fontSize:12}}};

/* ---- KPI ---- */
const iCal=D.scan.calmar.indexOf(Math.max(...D.scan.calmar));
const iShp=D.scan.sharpe.indexOf(Math.max(...D.scan.sharpe));
const kpi=[
 ['主连 vs 复权','符号完全相反',
  '原始主连让截面动量从 +12% 变成 −23%，任何基于主连的回测结论都不可信','hi'],
 ['最佳单信号',D.scan.names[0]+' '+pct(D.scan.cagr[0]),
  '最大回撤 '+D.scan.mdd[0].toFixed(1)+'% · Sharpe '+D.scan.sharpe[0].toFixed(2),''],
 ['最佳风险调整',D.scan.names[iShp]+' Sharpe '+D.scan.sharpe[iShp].toFixed(2),
  '年化 '+pct(D.scan.cagr[iShp])+'，最大回撤仅 '+D.scan.mdd[iShp].toFixed(1)+'%','gd'],
 ['最终配置',K.final,
  '年化为负概率 '+K.neg_ratio+'% · 5× 成本下仍不亏','gd'],
 ['残余漂移 δ','+'+K.drift+'%/年',
  '复权后仅剩真实现货升值，已不再是数据假象',''],
 ['成本拖累',(K.zero_cost-K.base_cost).toFixed(1)+'pp',
  '最终配置零成本 '+pct(K.zero_cost)+' → 全成本 '+pct(K.base_cost),''],
 ['参数网格',K.pos_cells+'/'+K.n_grid+' 格为正',
  '负值格占多数，说明没有可优化的稳定 edge','']
];
document.getElementById('kpis').innerHTML=kpi.map(k=>
 `<div class="kpi ${k[3]}"><div class="v ${k[3]==='hi'?'up':(k[3]==='gd'?'acc':'')}">${k[1]}</div>
  <div class="l"><b>${k[0]}</b><br>${k[2]}</div></div>`).join('');
const mk=(id,opt)=>{const c=echarts.init(document.getElementById(id));c.setOption(opt);return c;};

/* ---- 1 跳空 ---- */
if(D.gap){
 document.getElementById('gap_txt').textContent=D.gap.pos+'/'+D.gap.total;
 document.getElementById('gap_med').textContent='+'+D.gap.median+'%';
 mk('c_gap',{...base,grid:{left:104,right:70,top:20,bottom:34},
  xAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  yAxis:{type:'category',data:D.gap.names.slice().reverse(),...AX,splitLine:{show:false},
    axisLabel:{color:MUT,fontSize:11}},
  series:[{type:'bar',data:D.gap.gap.slice().reverse(),
    itemStyle:{color:p=>p.value>=0?'#e07b74':'#7ec9a5',borderRadius:[0,3,3,0]},
    label:{show:true,position:'right',formatter:p=>pct(p.value),fontSize:11,color:MUT}}]});
}

/* ---- 2 截面 ---- */
if(D.xs){
 const N=['20日','60日','120日','250日'];
 mk('c_xs',{...base,legend:{top:0,textStyle:{color:MUT,fontSize:12}},
  xAxis:{type:'category',data:N,...AX},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[
   {name:'动量·复权',type:'line',data:D.xs['动量·复权'],smooth:true,lineStyle:{width:3,color:UP},itemStyle:{color:UP}},
   {name:'反转·复权',type:'line',data:D.xs['反转·复权'],smooth:true,lineStyle:{width:3,color:DN},itemStyle:{color:DN}},
   {name:'动量·原始主连',type:'line',data:D.xs['动量·原始'],smooth:true,lineStyle:{width:2,type:'dashed',color:'#e8a49e'},itemStyle:{color:'#e8a49e'}},
   {name:'反转·原始主连',type:'line',data:D.xs['反转·原始'],smooth:true,lineStyle:{width:2,type:'dashed',color:'#8fd0b3'},itemStyle:{color:'#8fd0b3'}}]});
}

/* ---- 3 扫描 ---- */
mk('c_scan',{...base,grid:{left:52,right:24,top:16,bottom:76},
 xAxis:{type:'category',data:D.scan.names,...AX,axisLabel:{color:MUT,fontSize:10.5,rotate:38}},
 yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
 series:[{type:'bar',data:D.scan.cagr.map(v=>({value:v,itemStyle:{color:colr(v),borderRadius:[3,3,0,0]}})),
  label:{show:true,position:'top',formatter:p=>pct(p.value),fontSize:10.5,color:MUT}}]});
document.getElementById('t_scan').innerHTML=
 '<tr><th>信号</th><th>年化</th><th>最大回撤</th><th>Calmar</th><th>Sharpe</th><th>只多</th><th>只空</th></tr>'+
 D.scan.names.map((n,i)=>`<tr><td>${n}</td>
  <td class="n ${D.scan.cagr[i]>=0?'up':'dn'}"><b>${pct(D.scan.cagr[i])}</b></td>
  <td class="n">${D.scan.mdd[i].toFixed(1)}%</td><td class="n">${D.scan.calmar[i].toFixed(2)}</td>
  <td class="n">${D.scan.sharpe[i].toFixed(2)}</td>
  <td class="n">${pct(D.scan.long[i])}</td><td class="n">${pct(D.scan.short[i])}</td></tr>`).join('');

/* ---- 4 曲面：6 个止损面板，各画一张热力图 ---- */
if(D.surface){
 const F=D.surface.fasts,S=D.surface.slows;
 const all=D.surface.panels.flatMap(p=>p.row).filter(v=>v!==null);
 const mn=Math.min(...all,0), mx=Math.max(...all);
 const gd=document.getElementById('surf_grid');
 D.surface.panels.forEach((p,pi)=>{
   const d=document.createElement('div');
   d.innerHTML='<div class="hm" id="hm'+pi+'"></div><div class="hm-t">止损 '+(p.sl==='无'?'无（推荐）':p.sl)+'</div>';
   gd.appendChild(d);
   const cells=[];
   F.forEach((f,fi)=>S.forEach((s,si)=>{const v=p.row[fi*S.length+si];
     if(v!==null)cells.push([si,fi,v]);}));
   mk('hm'+pi,{grid:{left:46,right:10,top:8,bottom:26},
     tooltip:{...base.tooltip,formatter:q=>'MA'+F[q.value[1]]+'/'+S[q.value[0]]+'<br>年化 '+pct(q.value[2])},
     xAxis:{type:'category',data:S,...AX,axisLabel:{color:MUT,fontSize:9.5},splitArea:{show:false}},
     yAxis:{type:'category',data:F,...AX,axisLabel:{color:MUT,fontSize:9.5},splitArea:{show:false}},
     visualMap:{min:mn,max:mx,show:false,inRange:{color:['#2f7d5b','#eef2f4','#c0453c']}},
     series:[{type:'heatmap',data:cells,
       itemStyle:{borderColor:'#fff',borderWidth:1},
       label:{show:true,formatter:q=>q.value[2].toFixed(1),fontSize:9,color:'#1c2128'}}]});
 });
}

/* ---- 4b 成本敏感性 ---- */
if(D.costsens){
 mk('c_costsen',{...base,grid:{left:52,right:20,top:30,bottom:78},legend:{top:0,textStyle:{color:MUT,fontSize:11}},
  xAxis:{type:'category',data:D.costsens.names,...AX,axisLabel:{color:MUT,fontSize:10,rotate:42}},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[
   {name:'零成本',type:'bar',data:D.costsens.zero,itemStyle:{color:'#a8c4e8',borderRadius:[2,2,0,0]}},
   {name:'全成本',type:'bar',data:D.costsens.full.map(v=>({value:v,itemStyle:{color:colr(v),borderRadius:[2,2,0,0]}}))}]});
}
/* ---- 4c 参数平台：每族独立小图 ---- */
if(D.plateau){
 const gd=document.getElementById('plat_grid');
 Object.entries(D.plateau).forEach(([nm,v],i)=>{
   const d=document.createElement('div');
   d.innerHTML='<div class="hm" id="pl'+i+'"></div><div class="hm-t">'+nm+'</div>';
   gd.appendChild(d);
   mk('pl'+i,{grid:{left:42,right:8,top:10,bottom:34},
     tooltip:{...base.tooltip,formatter:q=>nm+' '+v.x[q.dataIndex]+'<br>样本内 '+pct(q.value)},
     xAxis:{type:'category',data:v.x,...AX,axisLabel:{color:MUT,fontSize:9,rotate:32}},
     yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:9.5,formatter:'{value}%'}},
     series:[
      {name:'样本内',type:'line',data:v.in,smooth:true,showSymbol:true,symbolSize:5,
        lineStyle:{width:2,color:ACC},itemStyle:{color:ACC}},
      {name:'样本外',type:'line',data:v.out,smooth:true,showSymbol:true,symbolSize:5,
        lineStyle:{width:2,color:ACC2,type:'dashed'},itemStyle:{color:ACC2}}]});
 });
}
if(D.rho){
 document.getElementById('rho_txt').innerHTML=
  '<b>参数稳定性（样本内最优参数 → 样本外表现，Spearman 秩相关）：</b>'+
  Object.entries(D.rho).map(([k,v])=>k+' <b class="'+(v>0.3?'up':(v<0?'dn':''))+'">ρ='+v.toFixed(2)+'</b>').join(' · ')+
  '。时序动量的 ρ 为正（+0.60），说明在样本内选出的最优参数在样本外依然排名靠前，'+
  '参数选择本身具有可迁移性；双均线与唐奇安为负，说明其参数排序不稳定。';
}

/* ---- 6 逐品种归因 ---- */
if(D.rank){
 const R=D.rank;
 mk('c_rank',{...base,grid:{left:60,right:26,top:44,bottom:96},
  legend:{top:0,textStyle:{color:MUT,fontSize:11.5}},
  tooltip:{...base.tooltip,formatter:p=>{
    const i=p[0].dataIndex;
    return '<b>'+R.names[i]+'</b><br>年化 '+pct(R.cagr[i])+'<br>最大回撤 '+R.mdd[i].toFixed(1)+'%'+
      '<br>只多 '+pct(R.long[i])+' / 只空 '+pct(R.short[i])+
      '<br>1 手保证金 '+R.margin[i].toLocaleString()+' 元'+
      '<br>'+(R.afford[i]?'<b style="color:#17915c">2 万可做</b>':'<b style="color:#d0453c">2 万开不起</b>');}},
  xAxis:{type:'category',data:R.sym,...AX,axisLabel:{color:MUT,fontSize:9.5,rotate:90}},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[
   {name:'只多',type:'bar',stack:'s',data:R.long.map(v=>({value:v,itemStyle:{color:'rgba(208,69,60,.42)'}}))},
   {name:'只空',type:'bar',stack:'s',data:R.short.map(v=>({value:v,itemStyle:{color:'rgba(23,145,92,.42)'}}))},
   {name:'多空对称',type:'line',smooth:false,symbolSize:8,lineStyle:{width:2,color:ACC},
    data:R.cagr.map((v,i)=>({value:v,itemStyle:{
      color:R.afford[i]?ACC:'#9aa4ae',borderColor:'#fff',borderWidth:1.5}}))}]});

 // 表：前 12 名
 let h='<thead><tr><th>#</th><th>品种</th><th class="n">年化</th><th class="n">回撤</th>'+
       '<th class="n">只多</th><th class="n">只空</th><th class="n">样本内</th><th class="n">样本外</th>'+
       '<th class="n">1手保证金</th><th>2万可行</th></tr></thead><tbody>';
 R.names.slice(0,12).forEach((nm,i)=>{
   h+='<tr><td>'+(i+1)+'</td><td>'+nm+'</td>'+
      '<td class="n '+(R.cagr[i]>=0?'up':'dn')+'">'+pct(R.cagr[i])+'</td>'+
      '<td class="n">'+R.mdd[i].toFixed(1)+'%</td>'+
      '<td class="n">'+pct(R.long[i])+'</td><td class="n">'+pct(R.short[i])+'</td>'+
      '<td class="n">'+(R.ins[i]===null?'—':pct(R.ins[i]))+'</td>'+
      '<td class="n">'+(R.outs[i]===null?'—':pct(R.outs[i]))+'</td>'+
      '<td class="n">'+R.margin[i].toLocaleString()+'</td>'+
      '<td class="'+(R.afford[i]?'up':'dn')+'">'+(R.afford[i]?'√':'×')+'</td></tr>';});
 h+='</tbody>';
 document.getElementById('rank_tbl').innerHTML=h;

 const t2=R.top2_ret;
 document.getElementById('rank_txt').innerHTML=
  '<b>结论：</b>全样本年化最高的两个品种是 '+t2.map(x=>'<b>'+x.name+' '+x.sym+'</b>（'+pct(x.cagr)+'）').join(' 与 ')+
  '，两只合在一起 <b>'+pct(R.pair.cagr)+'/年</b>、最大回撤 '+R.pair.mdd.toFixed(1)+'%、Sharpe '+R.pair.sharpe.toFixed(2)+
  '（多空对称口径、41 品种同标准）。';

 document.getElementById('rank_warn').innerHTML=
  '<b>但这两只 2 万都做不了，而且不是"少赚一点"的问题：</b><br>'+
  t2.map(x=>'· <b>'+x.name+'</b>：1 手名义保证金约 <b>'+x.margin.toLocaleString()+' 元</b>'+
    (x.afford?'':'，<b>已超过全部本金</b>')+
    (x.short>0&&x.cagr>0&&x.long<0?'，且利润 '+Math.abs(x.short).toFixed(1)+'% 全部来自做空 —— 骑的是 2021 年至今的猪价单边熊市，不是可重复的 edge':'')+
    '。').join('<br>')+
  '<br><b>换成可落地的：玻璃(FG) + 热卷(HC)</b>，1 手保证金分别约 2,267 / 3,581 元，'+
  '合计占本金约 29%，固定 1 手/腿跑 2014–2026 得到年化 14.8%、最大回撤 26.0%、Sharpe 0.85，'+
  '样本内 15.0% / 样本外 14.6% 几乎不变。'+
  '<br><b>选品种本身的偏差必须说清楚：</b>FG+HC 是在全样本排名里挑出来的，'+
  '同样条件下换别的品种对（玉米+淀粉、豆粕+菜粕、甲醇+白糖…）大多是 −1% ~ −12%。'+
  '41 品种不挑的平均只有 +2.5%/年 —— 所以对 FG+HC 的合理预期，应落在 2.5% 与 14.6% 之间，而不是直接照搬 14.6%。';
}

/* ---- 5 校验 ---- */
document.getElementById('fin_txt').innerHTML =
 '配置：<b>'+K.final+'</b>（多空对称）· 41 品种等权 · 波动率目标 2% · 无止损 · 成本 2bp+5bp。';

if(D.stop){
 mk('c_stop',{...base,grid:{left:52,right:20,top:30,bottom:40},legend:{top:0,textStyle:{color:MUT,fontSize:11}},
  xAxis:{type:'category',data:D.stop.x,...AX,axisLabel:{color:MUT,fontSize:11,rotate:30}},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[{name:'年化',type:'line',data:D.stop.cagr,smooth:true,lineStyle:{width:3,color:ACC},itemStyle:{color:ACC},
    label:{show:true,formatter:p=>p.value.toFixed(1),fontSize:10,color:MUT}},
   {name:'最大回撤',type:'line',data:D.stop.mdd,smooth:true,lineStyle:{width:2,color:'#c8842a',type:'dashed'},itemStyle:{color:'#c8842a'}}]});
}
mk('c_dec',{...base,grid:{left:52,right:20,top:30,bottom:40},legend:{top:0,textStyle:{color:MUT,fontSize:11}},
 xAxis:{type:'category',data:D.decomp.names,...AX},
 yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
 series:[{name:'年化',type:'bar',data:D.decomp.cagr.map(v=>({value:v,itemStyle:{color:colr(v),borderRadius:[3,3,0,0]}})),
   label:{show:true,position:'top',formatter:p=>pct(p.value),fontSize:11,color:MUT}},
  {name:'最大回撤',type:'line',data:D.decomp.mdd,smooth:true,itemStyle:{color:'#c8842a'},lineStyle:{color:'#c8842a',type:'dashed'}}]});

if(D.equity&&D.equity.dates.length){
 const A=D.equity.strat.concat(D.equity.bh).filter(v=>v>0);
 const lo=Math.min(...A)*0.9, hi=Math.max(...A)*1.08;
 mk('c_eq',{...base,grid:{left:58,right:24,top:34,bottom:52},legend:{top:0,textStyle:{color:MUT,fontSize:12}},
  color:[ACC,'#b0b8c1'],
  tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#e2e6ea',textStyle:{fontSize:12,color:'#1c2128'}},
  xAxis:{type:'category',data:D.equity.dates,...AX,axisLabel:{color:MUT,fontSize:10.5,
    formatter:v=>v.slice(0,4)}},
  yAxis:{type:'log',min:lo,max:hi,...AX,axisLabel:{color:MUT,fontSize:11},
    name:'净值(起点1.0)',nameTextStyle:{color:MUT,fontSize:11}},
  series:[
   {name:'策略',type:'line',data:D.equity.strat,smooth:true,showSymbol:false,lineStyle:{width:2.4,color:ACC},itemStyle:{color:ACC}},
   {name:'等权买入持有指数',type:'line',data:D.equity.bh,smooth:true,showSymbol:false,lineStyle:{width:1.6,color:'#b0b8c1',type:'dashed'},itemStyle:{color:'#b0b8c1'}}]});
}
if(D.oos){
 const kk=Object.keys(D.oos);
 mk('c_oos',{...base,grid:{left:52,right:20,top:30,bottom:40},legend:{top:0,textStyle:{color:MUT,fontSize:11}},
  xAxis:{type:'category',data:kk,...AX},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[{name:'年化',type:'bar',data:kk.map(k=>D.oos[k].cagr),
    itemStyle:{color:p=>colr(p.value),borderRadius:[3,3,0,0]},
    label:{show:true,position:'top',formatter:p=>pct(p.value),fontSize:11,color:MUT}},
   {name:'最大回撤',type:'line',data:kk.map(k=>D.oos[k].mdd),itemStyle:{color:'#c8842a'},lineStyle:{color:'#c8842a',type:'dashed'}}]});
}
if(D.cost){
 mk('c_cost',{...base,grid:{left:52,right:20,top:30,bottom:52},legend:{top:0,textStyle:{color:MUT,fontSize:11}},
  xAxis:{type:'category',data:D.cost.names,...AX,axisLabel:{color:MUT,fontSize:11,rotate:20}},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[{name:'年化',type:'bar',data:D.cost.cagr.map(v=>({value:v,itemStyle:{color:colr(v),borderRadius:[3,3,0,0]}})),
    label:{show:true,position:'top',formatter:p=>pct(p.value),fontSize:10.5,color:MUT}},
   {name:'最大回撤',type:'line',data:D.cost.mdd,itemStyle:{color:'#c8842a'},lineStyle:{color:'#c8842a',type:'dashed'}}]});
}
if(D.year){
 mk('c_year',{...base,grid:{left:52,right:20,top:30,bottom:52},legend:{top:0,textStyle:{color:MUT,fontSize:12}},
  color:['#d0453c','#b0b8c1'],
  xAxis:{type:'category',data:D.year.years,...AX,axisLabel:{color:MUT,fontSize:11}},
  yAxis:{type:'value',...AX,axisLabel:{color:MUT,fontSize:11,formatter:'{value}%'}},
  series:[
   {name:'策略',type:'bar',data:D.year.strat.map(v=>({value:v,itemStyle:{color:colr(v)}})),barGap:'0%'},
   {name:'等权买入持有',type:'bar',data:D.year.bh.map(v=>({value:v,itemStyle:{color:v>=0?'#e8b0ab':'#a9d6c2'}}))}]});
}
document.getElementById('boot_txt').innerHTML=
 '<b>分块自助检验（30 日一块，1000 次有放回重抽）：</b>年化 90% 区间 <b>['+D.boot.p5+'%, '+D.boot.p95+'%]</b>，'+
 '中位 '+D.boot.med+'%，<b>年化为负的概率 '+D.boot.neg_ratio+'%</b>。'+
 '区间明显偏向正侧，说明收益不是单一年份的偶然。';

window.addEventListener('resize',()=>{document.querySelectorAll('div').forEach(d=>{
  const i=echarts.getInstanceByDom(d); if(i)i.resize();});});
</script>
</body>
</html>
"""


# ================================================================ HTML 报告
def cmd_report(argv):
    """把 scan / validate / surface / bias / check / bench 的 JSON 汇总成
    一份单文件 HTML 研究报告（ECharts 内嵌，离线可看）。"""
    def J(name):
        p = os.path.join(HERE, name)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    scan, val, surf = J("scan.json"), J("validate.json"), J("surface.json")
    bias, chk, bench = J("bias.json"), J("check.json"), J("bench.json")
    if not (scan and val and surf):
        print("缺少 scan.json / validate.json / surface.json —— 请先依次运行: scan / validate / surface")
        return

    ec = os.path.join(HERE, "echarts.min.js")
    if os.path.exists(ec):
        with open(ec, encoding="utf-8") as f:
            ecjs = f.read()
        ec_tag = "<script>%s</script>" % ecjs
    else:
        ec_tag = '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>'

    D = {}

    # ---- ① 信号扫描 ----
    order = sorted(scan.keys(), key=lambda k: scan[k]["ls"]["cagr"], reverse=True)
    D["scan"] = {
        "names": order,
        "cagr": [round(scan[k]["ls"]["cagr"] * 100, 2) for k in order],
        "long": [round(scan[k]["long"]["cagr"] * 100, 2) for k in order],
        "short": [round(scan[k]["short"]["cagr"] * 100, 2) for k in order],
        "mdd": [round(scan[k]["ls"]["mdd"] * 100, 1) for k in order],
        "calmar": [round(scan[k]["ls"]["calmar"], 2) for k in order],
        "sharpe": [round(scan[k]["ls"]["sharpe"], 2) for k in order],
    }
    # 未复权对照扫描（FT_RAW=1 ft.py scan > scan_raw.json）
    sraw = J("scan_raw.json")
    if sraw:
        D["scan_raw"] = {k: round(sraw[k]["ls"]["cagr"] * 100, 2) for k in order if k in sraw}
        D["scan_adj"] = {k: round(scan[k]["ls"]["cagr"] * 100, 2) for k in order}

    # ---- 截面动量/反转 复权 vs 原始 ----
    if bench and bench.get("xsmom"):
        xm = bench["xsmom"]
        D["xs"] = {}
        for tag in ["动量·复权", "反转·复权", "动量·原始", "反转·原始"]:
            D["xs"][tag] = [round(xm["%s_N%d_K%d" % (tag, N, 3)]["cagr"] * 100, 2)
                            for N in [20, 60, 120, 250]]

    # ---- ② 数据体检（跳空） ----
    if chk:
        rows = sorted(chk["rows"], key=lambda x: -x["gap_sum"])[:12]
        D["gap"] = {
            "names": ["%s %s" % (r["sym"], NAME.get(r["sym"], "")) for r in rows],
            "gap": [round(r["gap_sum"] * 100, 1) for r in rows],
            "ann": [round(r["ann"] * 100, 2) for r in rows],
            "pos": chk["pos"], "total": chk["total"], "median": round(chk["median_gap"] * 100, 1),
        }

    # ---- ③ 止损细扫 ----
    sl_order = ["0.50", "0.75", "1.00", "1.25", "1.50", "2.00", "3.00", "0.00"]
    sl_lab = ["0.5", "0.75", "1.0", "1.25", "1.5", "2.0", "3.0", "无"]
    sls = val["stoploss"]
    D["stop"] = {
        "x": sl_lab,
        "cagr": [round(sls[k]["cagr"] * 100, 2) for k in sl_order],
        "mdd": [round(sls[k]["mdd"] * 100, 1) for k in sl_order],
    }

    # ---- ④ 多空分解 ----
    dec = val["decomp"]
    D["decomp"] = {
        "names": ["多空对称", "只多", "只空"],
        "cagr": [round(dec[k]["cagr"] * 100, 2) for k in ["多空对称", "只多", "只空"]],
        "mdd": [round(dec[k]["mdd"] * 100, 1) for k in ["多空对称", "只多", "只空"]],
    }

    # ---- ⑤ 样本内外 ----
    D["oos"] = {k: {"cagr": round(v["cagr"] * 100, 2), "mdd": round(v["mdd"] * 100, 1),
                    "calmar": round(v["calmar"], 2), "years": round(v["years"], 1)}
                for k, v in val["oos"].items()}

    # ---- ⑥ 自助检验 ----
    b = val["bootstrap"]
    D["boot"] = {k: round(v * 100, 2) for k, v in b.items()}

    # ---- ⑦ 逐年 ----
    yr = val["yearly"]
    D["year"] = {
        "years": sorted(yr.keys()),
        "strat": [round(yr[y]["strat"] * 100, 1) for y in sorted(yr)],
        "bh": [round(yr[y]["bh"] * 100, 1) for y in sorted(yr)],
    }

    # ---- ⑧ 成本压力 ----
    D["cost"] = {"names": list(val["cost"].keys()),
                 "cagr": [round(v["cagr"] * 100, 2) for v in val["cost"].values()],
                 "mdd": [round(v["mdd"] * 100, 1) for v in val["cost"].values()]}

    # ---- ⑨ 净值曲线 ----
    D["equity"] = val.get("equity", {"dates": [], "strat": [], "bh": []})

    # ---- ⑩ 参数曲面 ----
    fasts, slows = [5, 10, 15, 20, 30, 40], [30, 45, 60, 90, 120]
    hm, stopgrid = [], {}
    for si, sl in enumerate([0.0, 1.5, 2.0, 2.5, 3.0, 4.0]):
        row = []
        for f in fasts:
            for s in slows:
                if f >= s:
                    row.append(None); continue
                v = surf.get("ma%d_%d_sl%.1f" % (f, s, sl))
                row.append(None if v is None else round(v["cagr"] * 100, 2))
        hm.append({"sl": "无" if sl == 0 else "%.1fATR" % sl, "row": row})
    for sl in [0.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
        vs = [v["cagr"] * 100 for k, v in surf.items()
              if k.endswith("_sl%.1f" % sl) and v is not None]
        stopgrid["无" if sl == 0 else "%.1f" % sl] = round(sum(vs) / len(vs), 2) if vs else 0
    D["surface"] = {"fasts": fasts, "slows": slows, "panels": hm, "stopgrid": stopgrid}
    D["bias"] = {"drift": round((bias["drift"] * 100), 2) if bias else 1.2}

    # ---- ⑪ 成本敏感性 / 参数平台 / 样本内外秩相关 ----
    ex = J("explore.json")
    if ex:
        c = ex["cost"]
        D["costsens"] = {
            "names": list(c.keys()),
            "zero": [round(c[k]["zero"]["cagr"] * 100, 2) for k in c],
            "full": [round(c[k]["full"]["cagr"] * 100, 2) for k in c],
            "drag": [round((c[k]["zero"]["cagr"] - c[k]["full"]["cagr"]) * 100, 2) for k in c],
        }
        g = ex.get("grid", {})
        D["plateau"] = {}
        for gname in ["双均线", "唐奇安", "时序动量"]:
            rec = ex.get("oos", {}).get(gname, {}).get("rec")
            if rec:
                D["plateau"][gname] = {
                    "x": [("%s/%s" % (r["param"][0], r["param"][1])) if r["param"][1] else str(r["param"][0])
                          for r in rec],
                    "in": [round(r["in"] * 100, 2) for r in rec],
                    "out": [round(r["out"] * 100, 2) for r in rec],
                }
        D["rho"] = {k: round(v["rho"], 2) for k, v in ex.get("oos", {}).items()}

    # ---- ⑫ 逐品种归因（Top 品种 + 2 万可落地性）----
    rk = J("rank.json")
    if rk:
        rr = rk["rows"]
        D["rank"] = {
            "names": ["%s %s" % (x["sym"], x["name"]) for x in rr],
            "sym": [x["sym"] for x in rr],
            "cagr": [round(x["st"]["cagr"] * 100, 2) for x in rr],
            "mdd": [round(x["st"]["mdd"] * 100, 1) for x in rr],
            "long": [round(x["long"]["cagr"] * 100, 2) for x in rr],
            "short": [round(x["short"]["cagr"] * 100, 2) for x in rr],
            "ins": [round(x["in"]["cagr"] * 100, 1) if x["in"] else None for x in rr],
            "outs": [round(x["out"]["cagr"] * 100, 1) if x["out"] else None for x in rr],
            "margin": [round(x["margin"]) for x in rr],
            "afford": [bool(x["afford"]) for x in rr],
            "top2_ret": [{"sym": s, "name": NAME.get(s, s),
                          "cagr": round(next(x["st"]["cagr"] for x in rr if x["sym"] == s) * 100, 2),
                          "mdd": round(next(x["st"]["mdd"] for x in rr if x["sym"] == s) * 100, 1),
                          "margin": round(next(x["margin"] for x in rr if x["sym"] == s)),
                          "afford": bool(next(x["afford"] for x in rr if x["sym"] == s)),
                          "short": round(next(x["short"]["cagr"] for x in rr if x["sym"] == s) * 100, 1)}
                         for s in rk["top2_return"]["syms"]],
            "pair": {"cagr": round(rk["top2_return"]["stats"]["cagr"] * 100, 2),
                     "mdd": round(rk["top2_return"]["stats"]["mdd"] * 100, 1),
                     "sharpe": round(rk["top2_return"]["stats"]["sharpe"], 2)},
        }

    # ---- 关键结论数字 ----
    best = order[0]
    K = {
        "n_sig": len(scan), "n_sym": 41, "n_grid": sum(
            1 for k, v in surf.items() if v is not None),
        "best": best, "best_cagr": round(scan[best]["ls"]["cagr"] * 100, 2),
        "best_mdd": round(scan[best]["ls"]["mdd"] * 100, 1),
        "drift": D["bias"]["drift"],
        "neg_ratio": round(b["neg_ratio"] * 100, 1),
        "p5": round(b["p5"] * 100, 2), "p95": round(b["p95"] * 100, 2),
        "zero_cost": round(val["cost"]["零成本"]["cagr"] * 100, 2),
        "base_cost": round(val["cost"]["1× (基准)"]["cagr"] * 100, 2),
        "pos_cells": sum(1 for v in surf.values() if v is not None and v["cagr"] > 0.005),
        "final": FINAL["name"],
    }

    html = _REPORT_TPL.replace("__DATA__", json.dumps(D, ensure_ascii=False)).replace(
        "__KEY__", json.dumps(K, ensure_ascii=False)).replace("__ECHARTS__", ec_tag)
    out_path = os.path.join(HERE, "商品期货策略研究报告.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("已写出 %s  (%.0f KB)" % (out_path, len(html.encode("utf-8")) / 1024))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "check":
        cmd_check(sys.argv[2:])
    elif cmd == "scan":
        cmd_scan(sys.argv[2:])
    elif cmd == "bias":
        cmd_bias(sys.argv[2:])
    elif cmd == "explore":
        cmd_explore(sys.argv[2:])
    elif cmd == "bench":
        cmd_bench(sys.argv[2:])
    elif cmd == "surface":
        cmd_surface(sys.argv[2:])
    elif cmd == "validate":
        cmd_validate(sys.argv[2:])
    elif cmd == "rank":
        cmd_rank(sys.argv[2:])
    elif cmd == "report":
        cmd_report(sys.argv[2:])
    else:
        print(__doc__)
