# -*- coding: utf-8 -*-
"""
「展期收益 + 趋势」多品种策略  ——  实盘信号 + 邮件通知 + 历史回测
单文件，只依赖同目录 data/*.csv，可直接运行。

━━━ 品种分层：本金涨了自动加品种 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
默认 2 万 → 只跑 玻璃 FG + 热卷 HC（1 手保证金合计约 5,850 元）。
本金变大后，只要改 EQUITY，CAPITAL_TIERS 里够门槛的品种会自动加入；
每个品种仍受「杠杆护栏 + 波动率目标仓位」约束，不会因为品种多了就超风险。

    2 万起   玻璃 FG / 热卷 HC              ← 默认起点
    3 万起  + 螺纹钢 RB / 纯碱 SA
    6 万起  + 短纤 PF / 棉花 CF
   11 万起  + 沪镍 NI
   16 万起  + 焦煤 JM
   20 万起  + 橡胶 RU
   29 万起  + 焦炭 J                       ← 全池收益最高

   只收「只多、只空【都为正】」的品种（收益不靠市场漂移，可重复）。
   剔除的品种与理由见下方配置区注释。

━━━ 策略逻辑（三条腿同时成立才持仓）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
① 展期收益 carry —— 决定「做多还是做空」
   主连是主力合约的【不复权】拼接，换月当天价格会跳到新合约价位：
       近月 100 / 远月 105（contango 升水）→ 主连换月日跳空 +5%
   但这个 +5% 不是交易者拿到的收益 —— 真实持仓者是卖掉 100 的合约、
   换成 105 的合约，随后新合约要向现货收敛 105 → 100，【实亏 5%】。
   所以：累计跳空为正 = 该品种长期升水 = 持有【多头】在付展期成本。
       过去 252 日累计跳空 > +0.4%  → 长期 contango    → 只做空
       过去 252 日累计跳空 < -0.4%  → 长期 backwardation → 只做多
       介于两者之间                 → 空仓（没有展期收益可赚）

② 趋势确认 —— 决定「什么时候进」
   EMA20 与 EMA60 的方向必须与 carry 一致，否则空仓。
   carry 给方向，趋势给时点；两者打架就不做。

③ 波动率目标仓位 —— 决定「下多少手」
   敞口 = min(2.0, 2% / 日波动率)        日波动率 = ATR20 / 前收盘
   目标手数 = 敞口 × 账户权益 / (现价 × 合约乘数)
   波动大的品种自动少开、波动小的自动多开，让每个品种贡献相同的风险。

━━━ 执行时点（严格无前视）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   T 日收盘算出信号  →  T+1 日【开盘】调仓
   回测证明日线硬止损是负贡献（只在震荡里反复砍仓），故不设止损；
   风险控制完全交给「波动率目标仓位 + 杠杆护栏」。

━━━ 用法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   python strategy_top2.py             # 打印今天的信号 + 跑回测
   python strategy_top2.py signal      # 只打印今天的信号（不发邮件）
   python strategy_top2.py notify      # 检查信号变动，有变动就发邮件（供定时任务调用）
   python strategy_top2.py notify daily# 不论有无变动都发一封当日简报
   python strategy_top2.py backtest    # 只跑回测
   python strategy_top2.py test-mail   # 发一封测试邮件，验证邮箱配置

   数据：读取同目录 data/{代码}.csv（date,open,high,low,close,volume）。
        缺数据时自动用 akshare 拉取（需 pip install akshare）。
"""
import os
import sys
import json
import math
import time
import smtplib
import statistics
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr, formatdate

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

TRADING_DAYS = 244          # 中国商品期货年均交易日
WARMUP = 250                # 前 250 根 K 线不出信号（等指标预热）


# ══════════════════════════════════════════════════════ 配置区
EQUITY = 20000.0            # 账户权益（元）—— 本金涨了就改这里，品种会自动增加

# ══════════════════════════════════════════════════════════════════════
#  品种池：只收「只多和只空【都为正】」的品种
#
#  为什么用这个标准：主连复权后仍残留真实现货升值（δ≈+1.7%/年）。
#  一个品种若【只空为负】，说明它的收益全来自市场上涨，换个年份就没了；
#  只有【两侧都为正】，才说明 carry+趋势 在这品种上是可重复的 edge。
#
#  逐品种实测（carry+趋势，波动率目标 2%，无止损）：
#    收进池子（只空>0）：
#      J  焦炭  +14.7%  只空 +9.3%   门槛 29万  ← 样本外 −2.0%，有衰减
#      FG 玻璃  +11.4%  只空 +2.8%   门槛  2万  ← 样本外 +12.4%，最稳
#      RB 螺纹钢 +10.2%  只空 +6.3%   门槛  3万  ← 空侧最强
#      NI 沪镍  + 9.1%  只空 +0.9%   门槛 11万  ← Calmar 0.55 全场最高
#      HC 热卷  + 9.1%  只空 +0.6%   门槛  2万  ← 样本外 +11.1%
#      JM 焦煤  + 8.0%  只空 +1.8%   门槛 16万
#      SA 纯碱  + 4.9%  只空 +0.7%   门槛  3万  ← 保证金最便宜的补充
#      RU 橡胶  + 4.5%  只空 +2.0%   门槛 20万  ← 样本外 −0.1%，勉强
#      CF 棉花  + 3.5%  只空 +1.9%   门槛  6万
#      PF 短纤  + 3.5%  只空 +1.2%   门槛  6万  ← 回撤仅 13.5%，全场最低
#    剔除（只空≤0，收益靠漂移）：
#      I 铁矿石 +10.5% 只空 −0.2%   UR 尿素 +4.2% 只空 −0.1%
#      M 豆粕   + 2.5% 只空 −3.1%   C 玉米  −1.9% 只空 −2.4%
#      RM 菜粕  − 2.6% 只空 −2.4%   CS 淀粉 −2.7% 只空 −0.6%
#    单独说明：
#      LH 生猪 +14.9% 但只多 −2.8% / 只空 +18.2% —— 单边押注，
#      且只有 2021 年至今 5.7 年数据，骑的是猪价单边熊市，不进核心池。
#
#  门槛 ≈ MIN_FILL × 2 × 1手名义 / 敞口（按同时持有 2 个品种估），
#  实际能否持仓由运行时决定（信号 + MIN_FILL + 保证金预算 + 杠杆护栏）。
# ══════════════════════════════════════════════════════════════════════
CAPITAL_TIERS = [
    # 1 手名义 1.7~3.3 万，波动率敞口 0.8~2.0 —— 小资金唯一能碰的一层
    {"name": "2万起", "min_equity": 0,
     "syms": [("FG", 20, 0.09), ("HC", 10, 0.07)]},
    {"name": "3万起", "min_equity": 30000,
     "syms": [("RB", 10, 0.07), ("SA", 20, 0.09)]},
    {"name": "6万起", "min_equity": 60000,
     "syms": [("PF", 5, 0.08), ("CF", 5, 0.07)]},
    {"name": "11万起", "min_equity": 110000,
     "syms": [("NI", 1, 0.12)]},
    {"name": "16万起", "min_equity": 160000,
     "syms": [("JM", 60, 0.12)]},
    # 收益最高的两个真-edge 品种，但 1 手名义 20 万，账户不够就是十几倍杠杆
    {"name": "20万起", "min_equity": 200000,
     "syms": [("RU", 10, 0.10)]},
    {"name": "29万起", "min_equity": 290000,
     "syms": [("J", 100, 0.12)]},
]

# 手动指定品种（设成列表则忽略 CAPITAL_TIERS 自动分层）；None = 自动
INSTRUMENTS = None

LOTS_MODE = "auto"          # "auto" = 按波动率目标动态算手数（随本金缩放）
                            # "fixed" = 每腿固定 LOTS_PER_LEG 手
LOTS_PER_LEG = 1            # LOTS_MODE="fixed" 时生效

VOL_TARGET = 0.02           # 单品种日波动率目标 2%
EXPO_CAP = 2.0              # 单品种敞口上限（名义价值 / 权益）
MIN_FILL = 0.50             # 目标手数低于此值就不开仓
                            # —— 目标 0.1 手却开 1 手，实际风险是目标的 10 倍
MARGIN_BUDGET = 0.50        # 全部持仓的保证金占用上限（占权益）
MARGIN_SAFE = 0.60          # 单腿保证金超过权益此比例则不再开新仓
MAX_LEG_LEVERAGE = 3.0      # 单腿「名义/权益」上限。1 手就超过 3 倍的品种直接不做
                            # —— 例：1 手沪金名义 90 万，在 2 万账户上是 45 倍杠杆，必爆

CARRY_WIN = 252             # carry 回看窗口（约 1 年）
CARRY_THR = 0.004           # carry 阈值 0.4%
MA_FAST, MA_SLOW = 20, 60   # 趋势确认用双均线
ATR_N = 20                  # ATR 周期
GAP_THRESH = 0.03           # |跳空| 超过 3% 判定为换月（阈值 1%~3% 是平稳平台）
FEE = 0.0002                # 手续费 2bp
SLIP = 0.0005               # 滑点 5bp
BROKER_ADD = 0.04           # 期货公司保证金加收（交易所口径 +4pp）

STATE_FILE = os.path.join(HERE, "signal_state.json")   # 记录上次已通知的仓位

# ── 邮件配置（163 邮箱，SMTP over SSL 465）─────────────────────────
# ⚠️ 这是授权码不是登录密码。若要把本脚本发给别人，请先删掉这一段。
MAIL = {
    "enabled": True,
    "from": "a1247504226@163.com",
    "to": "1247504226@qq.com",
    "password": "XKCINXNMOMMDCAFI",
    "server": "smtp.163.com",
    "port": 465,
    "max_retries": 3,
}

NAME = {"FG": "玻璃", "HC": "热卷", "RB": "螺纹钢", "SA": "纯碱", "NI": "沪镍",
        "LH": "生猪", "J": "焦炭", "I": "铁矿石", "C": "玉米", "CS": "玉米淀粉",
        "RM": "菜粕", "V": "PVC", "M": "豆粕", "TA": "PTA", "MA": "甲醇", "PF": "短纤",
        "JM": "焦煤", "AL": "沪铝", "UR": "尿素", "AG": "沪银", "AU": "沪金",
        "CU": "沪铜", "Y": "豆油", "P": "棕榈油", "PP": "聚丙烯", "SM": "锰硅",
        "SF": "硅铁", "SP": "纸浆", "CF": "棉花", "AP": "苹果", "OI": "菜油",
        "PK": "花生", "CJ": "红枣", "EG": "乙二醇", "L": "塑料", "V": "PVC",
        "ZN": "沪锌", "RU": "橡胶", "BU": "沥青", "SC": "原油", "SI": "工业硅",
        "LC": "碳酸锂", "SS": "不锈钢", "FU": "燃油", "AO": "氧化铝"}


def active_instruments(equity=None):
    """按账户权益选出当前该跑的品种（分层自动启用）。"""
    if INSTRUMENTS is not None:
        return list(INSTRUMENTS)
    eq = EQUITY if equity is None else equity
    out = []
    for t in CAPITAL_TIERS:
        if eq >= t["min_equity"]:
            out.extend(t["syms"])
    return out


# ══════════════════════════════════════════════════════ 数据层
def read_csv(sym):
    path = os.path.join(DATA, sym + ".csv")
    if not os.path.exists(path):
        return None
    bars = []
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            q = line.strip().split(",")
            if len(q) < 6:
                continue
            try:
                bars.append({"date": q[0], "open": float(q[1]), "high": float(q[2]),
                             "low": float(q[3]), "close": float(q[4]),
                             "volume": float(q[5])})
            except ValueError:
                continue
    return bars or None


# 新浪接口返回中文列名，必须映射成英文
_COL_MAP = {"日期": "date", "开盘价": "open", "最高价": "high",
            "最低价": "low", "收盘价": "close", "成交量": "volume"}


LAST_FETCH_ERROR = ""


def fetch(sym):
    """用 akshare 拉新浪主连日线并覆盖本地 CSV。失败返回 None，
    失败原因写入模块级 LAST_FETCH_ERROR（供 refresh 打印）。"""
    global LAST_FETCH_ERROR
    LAST_FETCH_ERROR = ""
    try:
        import akshare as ak
    except ImportError:
        LAST_FETCH_ERROR = "未安装 akshare（pip install akshare）"
        return None
    try:
        df = ak.futures_main_sina(symbol=sym + "0")
    except Exception as e:
        LAST_FETCH_ERROR = "%s: %s" % (type(e).__name__, e)
        return None
    if df is None or len(df) == 0:
        LAST_FETCH_ERROR = "返回空表"
        return None
    df = df.rename(columns=_COL_MAP)
    cols = ["date", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in cols):
        LAST_FETCH_ERROR = "列名不符：%s" % list(df.columns)
        return None
    os.makedirs(DATA, exist_ok=True)
    df[cols].to_csv(os.path.join(DATA, sym + ".csv"), index=False)
    return read_csv(sym)


def refresh(syms=None, verbose=True):
    """重新拉取所有在用品种的日线。返回 (成功数, 失败列表)。

    定时任务必须先跑这一步，否则脚本只会读本地旧 CSV，
    数据日期不变 → notify 永远认为「数据未更新」。
    """
    syms = syms or [s[0] for s in active_instruments()]
    ok, bad = 0, []
    for s in syms:
        b = fetch(s)
        if b:
            ok += 1
            if verbose:
                print("  %-4s %-6s 更新至 %s（%d 根）" % (s, NAME.get(s, s), b[-1]["date"], len(b)))
        else:
            bad.append(s)
            if verbose:
                print("  %-4s %-6s 拉取失败，沿用本地数据 —— %s"
                      % (s, NAME.get(s, s), LAST_FETCH_ERROR))
    return ok, bad


def clean(bars, tol=0.5):
    """修坏点：某日价格相对前后两日中位数偏离 >50% 视为脏数据，用中位数替换。"""
    n = len(bars)
    for i in range(1, n - 1):
        ref = (bars[i - 1]["close"] + bars[i + 1]["close"]) / 2.0
        if ref <= 0:
            continue
        for k in ("open", "high", "low", "close"):
            if abs(bars[i][k] / ref - 1.0) > tol:
                bars[i][k] = ref
    return bars


def load(sym):
    """清洗 + 比例复权。

    返回 (adj_bars, real_last_price)。每根 adj bar 上附带：
        "gap"   —— 该日的【换月跳空】（非换月日为 0），供 carry 信号使用
        "scale" —— 复权累计因子；真实价 = 复权价 / scale
    复权的作用：把换月跳空抹掉，使价格序列的涨跌幅 = 真实持仓盈亏。
    """
    raw = read_csv(sym) or fetch(sym)
    if raw is None:
        return None, None
    raw = clean(raw)
    real_last = raw[-1]["close"]

    for i in range(1, len(raw)):
        pc, op = raw[i - 1]["close"], raw[i]["open"]
        raw[i]["gap"] = (op / pc - 1.0) if (pc > 0 and abs(op / pc - 1.0) > GAP_THRESH) else 0.0
    raw[0]["gap"] = 0.0

    out = [dict(raw[0])]
    out[0]["scale"] = 1.0
    k = 1.0
    for i in range(1, len(raw)):
        pc, op = raw[i - 1]["close"], raw[i]["open"]
        if pc > 0 and op > 0 and abs(op / pc - 1.0) > GAP_THRESH:
            k *= pc / op
        b = dict(raw[i])
        for c in ("open", "high", "low", "close"):
            b[c] = raw[i][c] * k
        b["scale"] = k
        out.append(b)
    return out, real_last


# ══════════════════════════════════════════════════════ 指标层
def ema(v, n):
    out, a, prev = [], 2.0 / (n + 1), None
    for x in v:
        prev = x if prev is None else a * x + (1 - a) * prev
        out.append(prev)
    return out


def atr(bars, n=ATR_N):
    """Wilder ATR（基于复权价，与收盘价的比值是尺度无关的）。"""
    out, prev = [], None
    for i, b in enumerate(bars):
        tr = (b["high"] - b["low"]) if i == 0 else max(
            b["high"] - b["low"], abs(b["high"] - bars[i - 1]["close"]),
            abs(b["low"] - bars[i - 1]["close"]))
        prev = tr if prev is None else (prev * (n - 1) + tr) / n
        out.append(prev)
    return out


def real_px(bars, i):
    """第 i 根 K 线的真实价（复权价 / 累计因子）。"""
    return bars[i]["close"] / bars[i]["scale"]


# ══════════════════════════════════════════════════════ 信号层
def sig_carry(bars, win=CARRY_WIN, thresh=CARRY_THR):
    """展期收益：滚动累计换月跳空的方向。累计正跳空 = 长期升水 = 做空。"""
    out = []
    for i in range(len(bars)):
        if i < win:
            out.append(0)
            continue
        s = sum(bars[j].get("gap", 0.0) for j in range(i - win + 1, i + 1))
        out.append(1 if s < -thresh else (-1 if s > thresh else 0))
    return out


def sig_trend(bars, fast=MA_FAST, slow=MA_SLOW):
    """双均线趋势：EMA20 > EMA60 → 1，否则 -1。"""
    c = [b["close"] for b in bars]
    ef, es = ema(c, fast), ema(c, slow)
    return [0 if i < slow else (1 if ef[i] > es[i] else -1) for i in range(len(bars))]


def signal(bars):
    """carry 与趋势同向才持仓（两条腿都要同意）。"""
    a, t = sig_carry(bars), sig_trend(bars)
    return [a[i] if a[i] == t[i] else 0 for i in range(len(bars))]


def exposure_at(bars, A, i):
    """bar i 的敞口 = min(cap, vol_target / 日波动率)，日波动率取 bar i-1 的 ATR/收盘。"""
    if i <= 0:
        return 1.0
    pc = bars[i - 1]["close"]
    dvol = A[i - 1] / pc if pc > 0 else 0.0
    return 1.0 if dvol < 1e-6 else min(EXPO_CAP, VOL_TARGET / dvol)


# ══════════════════════════════════════════════════════ 仓位计算
def plan(equity=None, verbose=False):
    """算出「今天收盘后」每个品种的目标仓位。返回 (rows, skipped, data_date)。

    手数分配（关键）：
      每个品种的目标名义 = 敞口 × (权益 / 同时持仓的品种数)
      —— 除以 N 是因为组合是等权平均的：N 个品种各占 1/N 资金。
         若每腿都按【全额权益】定仓，品种越多总杠杆越大，风险失控。
      然后向下取整到手；目标不足 MIN_FILL 手就不开（1 手会远超风险目标）。
    """
    eq = EQUITY if equity is None else equity
    cand, skipped, dates = [], [], []
    for sym, mult, mrate in active_instruments(eq):
        bars, px = load(sym)
        if bars is None:
            skipped.append({"sym": sym, "name": NAME.get(sym, sym), "why": "无数据"})
            continue
        dates.append(bars[-1]["date"])
        A = atr(bars)
        last = len(bars) - 1
        cur = signal(bars)[last]
        expo = exposure_at(bars, A, last)
        dvol = A[last - 1] / bars[last - 1]["close"]
        gap = sum(b.get("gap", 0.0) for b in bars[-CARRY_WIN:])
        lot_notional = px * mult
        margin_per = lot_notional * (mrate + BROKER_ADD)

        if lot_notional > eq * MAX_LEG_LEVERAGE:
            skipped.append({"sym": sym, "name": NAME.get(sym, sym), "why":
                            "1 手名义 %s 元 = %.1f 倍权益，超过 %.1f 倍杠杆上限"
                            % (format(lot_notional, ",.0f"), lot_notional / eq, MAX_LEG_LEVERAGE)})
            continue

        cand.append({
            "sym": sym, "name": NAME.get(sym, sym), "sig": cur, "px": px,
            "atr": dvol * px, "dvol": dvol, "expo": expo,
            "mult": mult, "mrate": mrate, "lot_notional": lot_notional,
            "margin_per": margin_per, "gap": gap, "date": bars[-1]["date"],
        })

    # 同时持仓数 = 有信号的品种数（至少 1）——组合按等权 1/N 分配资金
    n_sig = max(1, sum(1 for c in cand if c["sig"] != 0))
    share = eq / n_sig

    rows = []
    for c in cand:
        if c["sig"] == 0:                       # ← 空仓品种必须 0 手
            c["want"] = 0.0
            c["lots"] = 0
        elif LOTS_MODE == "fixed":
            c["want"] = c["expo"] * share / c["lot_notional"]
            c["lots"] = LOTS_PER_LEG
        else:
            c["want"] = c["expo"] * share / c["lot_notional"]
            c["lots"] = int(c["want"])
            if c["lots"] == 0 and c["want"] >= MIN_FILL and \
                    c["margin_per"] <= eq * MARGIN_SAFE:
                c["lots"] = 1                   # 不足 1 手但目标够接近，按 1 手执行
        c["notional"] = c["lots"] * c["lot_notional"]
        c["margin"] = c["lots"] * c["margin_per"]
        rows.append(c)

    # 总保证金预算：超了先砍【目标最小】的（它们被 1 手取整扭曲得最厉害）
    if LOTS_MODE != "fixed":
        budget = eq * MARGIN_BUDGET
        total = sum(r["margin"] for r in rows)
        for r in sorted([x for x in rows if x["lots"] > 0], key=lambda x: x["want"]):
            if total <= budget:
                break
            while total > budget and r["lots"] > 0:
                r["lots"] -= 1
                r["notional"] = r["lots"] * r["lot_notional"]
                r["margin"] = r["lots"] * r["margin_per"]
                total -= r["margin_per"]

    data_date = max(dates) if dates else None
    if verbose:
        for s in skipped:
            print("  跳过 %s(%s)：%s" % (s["name"], s["sym"], s["why"]))
    return rows, skipped, data_date


def dir_txt(v):
    return {1: "多", -1: "空", 0: "空仓"}[v]


# ══════════════════════════════════════════════════════ 实盘信号
def show_signal():
    print("═" * 78)
    print("  实盘信号（T 日收盘生成，T+1 日开盘执行）")
    print("  账户权益 %s 元   单品种波动率目标 %.0f%%   敞口上限 %.1fx"
          % (format(EQUITY, ",.0f"), VOL_TARGET * 100, EXPO_CAP))
    print("═" * 78)
    print()
    rows, skipped, data_date = plan(verbose=True)
    for s in skipped:
        print("  %s(%s)  %s\n" % (s["name"], s["sym"], s["why"]))

    for r in rows:
        arrow = {1: "▲ 做多", -1: "▼ 做空", 0: "— 空仓"}[r["sig"]]
        if r["gap"] > CARRY_THR:
            basis = "长期升水（contango），做空收展期收益"
        elif r["gap"] < -CARRY_THR:
            basis = "长期贴水（backwardation），做多收展期收益"
        else:
            basis = "无明显展期结构 → 空仓"
        print("  %s(%s)  %s" % (r["name"], r["sym"], arrow))
        print("     现价 %.1f   20日ATR %.1f（真实口径）   日波动 %.2f%%   波动率敞口 %.2fx"
              % (r["px"], r["atr"], r["dvol"] * 100, r["expo"]))
        print("     近 252 日累计换月跳空 %+.2f%%  →  %s" % (r["gap"] * 100, basis))
        if r["sig"] == 0:
            print("     → 目标手数 0 手（不开仓）")
        else:
            print("     → 目标手数 %d 手（理论 %.2f 手）   名义 %s 元   保证金 %s 元"
                  % (r["lots"], r["want"], format(r["notional"], ",.0f"),
                     format(r["margin"], ",.0f")))
        print()

    tn = sum(r["notional"] for r in rows)
    tm = sum(r["margin"] for r in rows)
    print("  ── 合计 ──")
    print("     名义敞口 %s 元  =  %.2f 倍权益" % (format(tn, ",.0f"), tn / EQUITY))
    print("     占用保证金 %s 元  =  %.1f%% 权益（剩余 %s 元）"
          % (format(tm, ",.0f"), tm / EQUITY * 100, format(EQUITY - tm, ",.0f")))
    if tn > 0:
        worst = tn * 0.08
        print("     极端情形（全部同时反向 8%%，接近跌停）亏损约 %s 元 = %.1f%% 权益"
              % (format(worst, ",.0f"), worst / EQUITY * 100))

    if rows:
        print("\n  ── 最近 15 个交易日的信号 ──")
        ser, dts = {}, None
        for r in rows:
            b, _ = load(r["sym"])
            ser[r["sym"]] = signal(b)
            if dts is None or len(b) < len(dts):
                dts = [x["date"] for x in b]
        print("  %-12s" % "日期" + "".join("  %-8s" % (NAME.get(s, s) + " " + s) for s in ser))
        n = min(len(v) for v in ser.values())
        for k in range(max(0, n - 15), n):
            print("  %-12s" % dts[k] + "".join(
                "  %-8s" % dir_txt(ser[s][k]) for s in ser))
    print("\n  数据截止 %s" % data_date)
    print()


# ══════════════════════════════════════════════════════ 邮件
def send_mail(subject, text, html=None, cfg=None):
    """发邮件，失败重试 cfg['max_retries'] 次。返回 (ok, msg)。"""
    cfg = cfg or MAIL
    if not cfg.get("enabled", True):
        return False, "邮件未启用（MAIL['enabled'] = False）"

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("期货信号机器人", "utf-8")), cfg["from"]))
    msg["To"] = cfg["to"]
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    rcpts = [x.strip() for x in cfg["to"].split(",") if x.strip()]
    err = None
    for attempt in range(1, int(cfg.get("max_retries", 3)) + 1):
        try:
            # local_hostname 必须显式给 ASCII 值：本机主机名可能含中文，
            # 默认值会让 EHLO 命令编码失败（UnicodeEncodeError: 'ascii' codec）。
            s = smtplib.SMTP_SSL(cfg["server"], int(cfg["port"]),
                                 local_hostname="localhost", timeout=25)
            s.login(cfg["from"], cfg["password"])
            s.sendmail(cfg["from"], rcpts, msg.as_string())
            s.quit()
            return True, "已发送（第 %d 次尝试）" % attempt
        except Exception as e:
            err = e
            if attempt < int(cfg.get("max_retries", 3)):
                time.sleep(2 * attempt)
    return False, "%s: %s" % (type(err).__name__, err)


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ── 邮件样式（全部内联：邮件客户端不认 <style> 标签）──────────────
_C_TH = ("padding:8px 5px;background:#eef1f5;border-bottom:2px solid #d5dbe3;"
         "font-size:12.5px;color:#4a5460;font-weight:700;text-align:left")
_C_TD = "padding:9px 5px;border-bottom:1px solid #eceff3;font-size:14px;color:#1c2128;word-break:break-word"
_C_NUM = _C_TD + ";text-align:right"
_COLOR = {1: "#c62828", -1: "#2e7d32", 0: "#98a2ad"}      # 红涨绿跌


def _tbl(headers, widths, rows, note_rows=None):
    """生成手机友好的 HTML 表格。

    用 table-layout:fixed + colgroup 固定列宽 —— 否则手机窄屏下表会撑破容器、
    最右一列被截掉。note_rows = {行号: 说明文字}，跨列显示在该行下方。
    """
    note_rows = note_rows or {}
    cols = "".join('<col style="width:' + w + '">' for w in widths)
    h = ('<table style="width:100%;max-width:100%;border-collapse:collapse;'
         'table-layout:fixed;margin:6px 0 2px" cellpadding="0" cellspacing="0">'
         '<colgroup>' + cols + '</colgroup>')
    h += "<tr>" + "".join('<th style="%s">%s</th>' % (_C_TH, x) for x in headers) + "</tr>"
    for i, cells in enumerate(rows):
        h += "<tr>" + "".join(cells) + "</tr>"
        if i in note_rows:
            h += ('<tr><td colspan="%d" style="padding:0 5px 10px;font-size:12px;'
                  'color:#67717c;line-height:1.6;border-bottom:1px solid #eceff3;'
                  'word-break:break-word">%s</td></tr>' % (len(headers), note_rows[i]))
    return h + "</table>"


def _build_mail(rows, changes, data_date, daily=False):
    """把变动组装成邮件标题 + 正文（HTML 表格，手机可读）。"""
    if changes:
        parts = ["%s %s" % (c["name"], c["how"]) for c in changes]
        subject = "【期货信号】%s  %s" % (data_date, " / ".join(parts))
    else:
        subject = "【期货信号】%s  无变动" % data_date

    tn = sum(r["notional"] for r in rows)
    tm = sum(r["margin"] for r in rows)
    tn_held = sum(r["notional"] for r in rows)
    worst = tn_held * 0.08

    # ── ① 本次变动表 ──
    ch_rows, ch_notes = [], {}
    for k, c in enumerate(changes):
        col = _COLOR[c["sig"]]
        ch_rows.append([
            '<td style="%s"><b>%s</b> <span style="font-size:12px;color:#8a949e">%s</span></td>'
            % (_C_TD, c["name"], c["sym"]),
            '<td style="%s;color:%s;font-weight:700">%s</td>' % (_C_TD, col, c["how"]),
            '<td style="%s">%d</td>' % (_C_NUM, c["lots"]),
            '<td style="%s">%s</td>' % (_C_NUM, format(c["px"], ",.1f")),
            '<td style="%s">%s</td>' % (_C_NUM, format(c["margin"], ",.0f")),
        ])
        ch_notes[k] = "%s　·　日波动 %.2f%%　敞口 %.2fx" % (c["basis"], c["dvol"] * 100, c["expo"])
    ch_tbl = _tbl(["品种", "操作", "手数", "现价", "保证金"],
                  ["28%", "18%", "12%", "20%", "22%"], ch_rows, ch_notes)

    # ── ② 当前持仓表（只列有信号的，再补一行空仓汇总）──
    act = [r for r in rows if r["sig"] != 0]
    idle = [r for r in rows if r["sig"] == 0]
    pos_rows = []
    for r in act:
        col = _COLOR[r["sig"]]
        pos_rows.append([
            '<td style="%s"><b>%s</b> <span style="font-size:12px;color:#8a949e">%s</span></td>'
            % (_C_TD, r["name"], r["sym"]),
            '<td style="%s;color:%s;font-weight:700">%s</td>' % (_C_TD, col, dir_txt(r["sig"])),
            '<td style="%s">%d</td>' % (_C_NUM, r["lots"]),
            '<td style="%s">%s</td>' % (_C_NUM, format(r["notional"], ",.0f")),
            '<td style="%s">%s</td>' % (_C_NUM, format(r["margin"], ",.0f")),
        ])
    if not pos_rows:
        pos_rows.append(['<td style="%s" colspan="5">当前无持仓（全部空仓）</td>' % _C_TD])
    else:
        pos_rows.append([
            '<td style="%s;font-weight:700;border-top:2px solid #d5dbe3">合计</td>' % _C_TD,
            '<td style="%s;border-top:2px solid #d5dbe3"></td>' % _C_TD,
            '<td style="%s;border-top:2px solid #d5dbe3">%d</td>' % (_C_NUM, sum(r["lots"] for r in act)),
            '<td style="%s;font-weight:700;border-top:2px solid #d5dbe3">%s</td>' % (_C_NUM, format(tn, ",.0f")),
            '<td style="%s;font-weight:700;border-top:2px solid #d5dbe3">%s</td>' % (_C_NUM, format(tm, ",.0f")),
        ])
    pos_tbl = _tbl(["品种", "方向", "手数", "名义(元)", "保证金(元)"],
                   ["28%", "14%", "12%", "22%", "24%"], pos_rows)

    # ── 组装 ──
    P = "margin:0 0 4px;font-size:15px;font-weight:700;color:#1c2128"
    S = "margin:0;font-size:13px;color:#67717c;line-height:1.7"
    h = ['<div style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
         '\'Microsoft YaHei\',sans-serif;font-size:15px;color:#1c2128;'
         'line-height:1.7;width:100%;max-width:600px;margin:0 auto;'
         'overflow-wrap:break-word;word-break:break-word">']

    h.append('<div style="background:#1c2128;color:#fff;padding:14px 16px;border-radius:8px">'
             '<div style="font-size:17px;font-weight:700">期货信号 · 展期收益 + 趋势</div>'
             '<div style="font-size:13px;opacity:.85;margin-top:6px;line-height:1.6">'
             '数据截止 %s　·　权益 %s 元<br><b>下一交易日开盘执行</b></div></div>'
             % (data_date, format(EQUITY, ",.0f")))

    if changes:
        h.append('<p style="%s;margin-top:18px">■ 本次需要操作（%d 项）</p>' % (P, len(changes)))
        h.append(ch_tbl)
    else:
        h.append('<p style="%s;margin-top:18px">■ 本次无仓位变动</p>' % P)
        h.append('<p style="%s">持仓与上次一致，无需下单。</p>' % S)

    h.append('<p style="%s;margin-top:20px">■ 当前持仓</p>' % P)
    h.append(pos_tbl)
    h.append('<p style="%s;margin-top:6px">名义敞口 %.2f 倍权益　·　'
             '保证金占权益 %.1f%%　·　剩余可用 %s 元</p>'
             % (S, tn / EQUITY, tm / EQUITY * 100, format(EQUITY - tm, ",.0f")))
    if idle:
        h.append('<p style="%s">空仓（无信号）：%s</p>'
                 % (S, "、".join("%s %s" % (r["name"], r["sym"]) for r in idle)))

    h.append('<p style="%s;margin-top:20px">■ 风险提示</p>' % P)
    h.append('<p style="%s">· 本策略不设止损，风险控制靠波动率目标仓位 + 杠杆护栏。<br>'
             '· 极端情形（全部持仓同时反向 8%%）约亏 %s 元 = %.1f%% 权益。<br>'
             '· 历史回测不代表未来表现；期货为保证金交易，存在本金全部损失的风险。</p>'
             % (S, format(worst, ",.0f"), worst / EQUITY * 100))
    h.append('<p style="%s;margin-top:16px;padding-top:10px;border-top:1px solid #eceff3">'
             '由 strategy_top2.py 自动发送</p>' % S)
    h.append("</div>")
    html = "".join(h)

    # ── 纯文本兜底（部分客户端不渲染 HTML）──
    L = ["期货信号 · 展期收益 + 趋势",
         "数据截止 %s   权益 %s 元   下一交易日开盘执行" % (data_date, format(EQUITY, ",.0f")),
         "-" * 46]
    if changes:
        L.append("本次需要操作（%d 项）" % len(changes))
        L.append("%-12s %-8s %5s %9s %9s" % ("品种", "操作", "手数", "现价", "保证金"))
        for c in changes:
            L.append("%-12s %-8s %5d %9s %9s"
                     % (c["name"], c["how"], c["lots"], format(c["px"], ",.1f"),
                        format(c["margin"], ",.0f")))
    else:
        L.append("本次无仓位变动。")
    L.append("")
    L.append("当前持仓")
    L.append("%-12s %-6s %5s %10s %10s" % ("品种", "方向", "手数", "名义", "保证金"))
    for r in act:
        L.append("%-12s %-6s %5d %10s %10s"
                 % (r["name"], dir_txt(r["sig"]), r["lots"],
                    format(r["notional"], ",.0f"), format(r["margin"], ",.0f")))
    if not act:
        L.append("（无持仓）")
    L.append("-" * 46)
    L.append("名义 %.2f 倍权益   保证金占权益 %.1f%%" % (tn / EQUITY, tm / EQUITY * 100))
    L.append("")
    L.append("风险提示：不设止损；极端情形约亏 %.1f%% 权益；历史回测不代表未来表现。"
             % (worst / EQUITY * 100))
    L.append("-- 由 strategy_top2.py 自动发送")
    return subject, "\n".join(L), html


def notify(argv=None):
    """检查信号变动，有变动（或 daily）就发邮件。

    参数： daily    —— 无变动也发当日简报
          nofetch  —— 跳过数据更新（用本地 CSV）
    """
    argv = argv or []
    daily = "daily" in argv
    if "nofetch" not in argv:
        print("更新行情数据…")
        n_ok, bad = refresh(verbose=True)
        if bad:
            print("  注意：%d 个品种拉取失败，沿用本地数据" % len(bad))
        print()
    rows, skipped, data_date = plan()
    if data_date is None:
        print("没有可用数据，放弃")
        return
    cur_pos = {r["sym"]: (r["sig"] if r["lots"] > 0 else 0) for r in rows}

    st = _load_state()
    old_pos = st.get("positions", {})
    last_date = st.get("data_date")

    if last_date == data_date and not daily:
        print("数据未更新（仍为 %s），无需通知。如需强制发送请加 daily。" % data_date)
        return

    changes = []
    for r in rows:
        new = cur_pos[r["sym"]]
        old = old_pos.get(r["sym"], 0)
        if new == old:
            continue
        if new == 0:
            how = "%s → 平仓" % dir_txt(old)
            reason = "carry 与趋势不再同向（或信号消失）"
        elif old == 0:
            how = "开%s仓" % ("多" if new > 0 else "空")
            reason = ("近 252 日累计换月跳空 %+.2f%% 为%s，且 EMA20/60 趋势同向"
                      % (r["gap"] * 100, "正（长期升水）" if r["gap"] > 0 else "负（长期贴水）"))
        else:
            how = "%s → %s（反手）" % (dir_txt(old), dir_txt(new))
            reason = "carry 与趋势同时反向"
        if r["gap"] > CARRY_THR:
            basis = "长期升水（contango），做空收展期收益"
        elif r["gap"] < -CARRY_THR:
            basis = "长期贴水（backwardation），做多收展期收益"
        else:
            basis = "无明显展期结构"
        c = dict(r)
        c.update({"how": how, "reason": reason, "basis": basis})
        changes.append(c)

    # 首次运行：把当前仓位当作"已通知"的基线
    first = not old_pos
    subject, text, html = _build_mail(rows, changes, data_date, daily)
    if first:
        subject = "【期货信号】%s  初始基线（%d 个品种）" % (data_date, len(rows))
        text = "（首次运行，以下为当前持仓基线；后续只在实际变动时通知）\n\n" + text

    if not changes and not daily:
        print("信号无变动（数据日 %s），不发邮件。" % data_date)
        _save_state({"data_date": data_date, "positions": cur_pos,
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
        return

    ok, msg = send_mail(subject, text, html)
    print("%s  →  %s" % (subject, msg))
    if ok:
        _save_state({"data_date": data_date, "positions": cur_pos,
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    else:
        print("邮件发送失败，状态未更新，下次运行会重试。")


# ══════════════════════════════════════════════════════ 回测
def backtest(verbose=True):
    """2 万实盘口径：LOTS_MODE 决定手数，信号驱动，T+1 开盘调仓。

    盈亏换算：持有 1 手的当日盈亏 = 乘数 × 真实前收 × 复权日收益。
    复权日收益已剔除换月跳空 —— 那个跳空不是你能拿到的钱。
    """
    S = {}
    for sym, mult, mrate in active_instruments():
        bars, _ = load(sym)
        if bars is None:
            continue
        lot_notional = real_px(bars, len(bars) - 1) * mult
        if lot_notional > EQUITY * MAX_LEG_LEVERAGE:
            if verbose:
                print("  跳过 %s(%s)：1 手名义 %s 元 = %.1f 倍权益，超过 %.1f 倍杠杆上限"
                      % (NAME.get(sym, sym), sym, format(lot_notional, ",.0f"),
                         lot_notional / EQUITY, MAX_LEG_LEVERAGE))
            continue
        S[sym] = {"bars": bars, "sig": signal(bars), "atr": atr(bars),
                  "mult": mult, "mrate": mrate}
    if not S:
        print("无可用数据（或全部品种都超过杠杆上限）")
        return None

    base = min(S, key=lambda s: len(S[s]["bars"]))
    dates = [b["date"] for b in S[base]["bars"]]
    idx = {s: {b["date"]: i for i, b in enumerate(v["bars"])} for s, v in S.items()}

    eq = EQUITY
    curve, daily = [], []
    pos = {s: 0 for s in S}
    trades = {s: 0 for s in S}

    for t in range(1, len(dates)):
        dt = dates[t]
        # ① 本日各品种的目标方向（用 bar i-1 收盘确认的信号）
        tgt = {}
        for sym, v in S.items():
            i = idx[sym].get(dt)
            tgt[sym] = 0 if (i is None or i == 0 or (i - 1) < WARMUP) else v["sig"][i - 1]
        # ② 同时持仓数决定每腿资金份额（等权 1/N，与 plan() 同一口径）
        n_sig = max(1, sum(1 for x in tgt.values() if x != 0))
        share = eq / n_sig

        pnl = 0.0
        for sym, v in S.items():
            i = idx[sym].get(dt)
            if i is None or i == 0:
                continue
            bars = v["bars"]
            if tgt[sym] == 0:
                lots = 0
            elif LOTS_MODE == "fixed":
                lots = LOTS_PER_LEG * tgt[sym]
            else:
                lot_notional = real_px(bars, i - 1) * v["mult"]
                want = exposure_at(bars, v["atr"], i - 1) * share / lot_notional
                n = int(want)
                if n == 0 and want >= MIN_FILL:
                    n = 1
                lots = n * tgt[sym]
            if lots != pos[sym]:
                pnl -= abs(lots - pos[sym]) * real_px(bars, i) * v["mult"] * (FEE + SLIP)
                if pos[sym] == 0 and lots != 0:
                    trades[sym] += 1
                pos[sym] = lots
            if pos[sym] != 0:
                r = bars[i]["close"] / bars[i - 1]["close"] - 1.0      # 复权日收益（已去跳空）
                pnl += pos[sym] * v["mult"] * real_px(bars, i - 1) * r
        eq += pnl
        curve.append(eq)
        prev_eq = curve[-2] if len(curve) > 1 else EQUITY
        daily.append(pnl / prev_eq if prev_eq > 0 else 0.0)
        if eq <= 0:
            if verbose:
                print("!! 权益归零，回测终止于 %s" % dt)
            break

    yrs = len(curve) / TRADING_DAYS
    cagr = (eq / EQUITY) ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else -1.0
    peak, mdd = EQUITY, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    sharpe = statistics.mean(daily) / sd * math.sqrt(TRADING_DAYS) if sd > 1e-12 else 0.0

    if verbose:
        legs = " + ".join("%s(%s)" % (NAME.get(s, s), s) for s in S)
        print("═" * 78)
        print("  回测：%s   手数模式 %s   T+1 开盘调仓" % (legs, LOTS_MODE))
        print("  成本 %.0fbp 手续费 + %.0fbp 滑点   起始权益 %s 元"
              % (FEE * 1e4, SLIP * 1e4, format(EQUITY, ",.0f")))
        print("═" * 78)
        print("  区间      %s → %s  (%.1f 年)" % (dates[0], dates[-1], yrs))
        print("  期末权益  %s 元   倍数 %.2fx   年化 %.2f%%"
              % (format(eq, ",.0f"), eq / EQUITY, cagr * 100))
        print("  最大回撤  %.1f%%   Calmar %.2f   Sharpe %.2f"
              % (mdd * 100, (cagr / mdd) if mdd > 1e-9 else 0.0, sharpe))
        print("  开仓次数  " + "   ".join("%s %d 次" % (NAME.get(s, s), n) for s, n in trades.items()))
        print()

        print("  样本内外拆分（防止用后见之明挑品种）")
        for lab, lo, hi in [("样本内 ≤2018", "0000", "2018"), ("样本外 ≥2019", "2019", "9999")]:
            seg = [i for i, dt in enumerate(dates[:len(curve)]) if lo <= dt[:4] <= hi]
            if len(seg) < 60:
                continue
            b0 = EQUITY if seg[0] == 0 else curve[seg[0] - 1]
            b1 = curve[seg[-1]]
            y = (seg[-1] - seg[0] + 1) / TRADING_DAYS
            c2 = (b1 / b0) ** (1 / y) - 1 if y > 0 and b1 > 0 else -1.0
            pk, dd = b0, 0.0
            for i in seg:
                pk = max(pk, curve[i])
                dd = max(dd, (pk - curve[i]) / pk)
            print("    %-12s 年化 %6.2f%%   区间回撤 %5.1f%%   倍数 %.2fx"
                  % (lab, c2 * 100, dd * 100, b1 / b0))
        print()

        print("  年份       年末权益      年度收益")
        print("  " + "-" * 38)
        base_eq = EQUITY
        for i, dt in enumerate(dates[:len(curve)]):
            nxt = dates[i + 1][:4] if i + 1 < len(dates[:len(curve)]) else None
            if nxt != dt[:4]:
                print("  %-10s %11s   %+8.1f%%"
                      % (dt[:4], format(curve[i], ",.0f"), (curve[i] / base_eq - 1) * 100))
                base_eq = curve[i]
        print()

        if curve:
            print("  净值曲线（横轴为全区间）")
            step = max(1, len(curve) // 64)
            pts = curve[::step]
            lo, hi = min(pts + [EQUITY]), max(pts)
            for k in range(8, -1, -1):
                lv = lo + (hi - lo) * k / 8
                print("  %9s │" % format(lv, ",.0f") + "".join("█" if v >= lv else " " for v in pts))
            print("  " + " " * 10 + "└" + "─" * len(pts))
            print()

    return {"cagr": cagr, "mdd": mdd, "mult": eq / EQUITY, "sharpe": sharpe, "curve": curve}


# ══════════════════════════════════════════════════════ 入口
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("signal", "all"):
        show_signal()
    if cmd in ("backtest", "all"):
        backtest()
    if cmd == "notify":
        notify(sys.argv[2:])
    if cmd == "refresh":
        ok, bad = refresh()
        print("\n成功 %d 个%s" % (ok, "，失败：" + " ".join(bad) if bad else ""))
    if cmd == "test-mail":
        # 发一封【真实格式】的样例，让用户直接看到以后收到长什么样
        rows, skipped, dd = plan()
        sample = []
        for r in rows:
            if r["sig"] != 0:
                c = dict(r)
                c["how"] = "开%s仓" % ("多" if r["sig"] > 0 else "空")
                c["basis"] = ("长期升水（contango），做空收展期收益" if r["gap"] > CARRY_THR
                              else "长期贴水（backwardation），做多收展期收益")
                sample.append(c)
                break
        subj, text, html = _build_mail(rows, sample, dd)
        subj = "[样例] " + subj
        ok, m = send_mail(subj, text, html)
        print("样例邮件：", m)
        if not ok:
            print("（提示：检查 MAIL 里的授权码 / 收发件人）")
