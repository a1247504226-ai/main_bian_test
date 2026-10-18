# -*- coding: utf-8 -*-
"""
BTC 日线趋势策略 · 每日检测（邮件推送 + 可选币安下单）
============================================================

只跟踪**一套**策略（没有备选方案，也没有第二套参数）：

    双均线 EMA 10/30 + SMA50 只约束多头 + 波动率目标仓位
    载体：多头走现货（不付资金费）、空头走合约 1× 逐仓

趋势过滤规则（strategy.regime_ma，默认 50，设 0 关闭）：
    收盘价 > SMA(50) 才允许做多；收盘价在均线下方时把做多信号置 0，**空头照做**。
    只约束多头是因为 BTC 的暴跌段几乎都在长期均线下方，
    挡掉「均线下方抄底做多」能明显削回撤（2022 年 -33.0% → -19.6%），
    而空头本来就是靠下跌赚钱，不该被均线挡住（双向过滤反而更差）。

空头腿杠杆（binance.leverage，默认 1）：
    名义仓位 = 权益 × 敞口，与杠杆无关 → 杠杆只决定保证金占用与爆仓线，不影响收益。
    1× 逐仓：保证金占权益 100%，价格反弹 +100% 才爆仓（基本等于不爆）；
    2× 逐仓：保证金占权益 50%，价格反弹 +50% 就爆仓。
    历史空头最深逆行 +22.89%，1× 安全余量 77.11pp，2× 只剩 27.11pp。
    注：1× 逐仓会占满 100% 保证金，所以实盘下单会自动留 1% 缓冲（见 build_order_plan）。

每天在日线收盘后跑一次，输出：最新已收盘 K 线、当前信号与目标持仓、
**持仓成本价 / 现价 / 浮动盈亏**、是否需要动作、空头腿距爆仓线的安全边际，
并把结果以「手机可直接阅读的表格邮件」推给你。

邮件有两种形态：
    · 仓位有变化 → 顶部红色横幅「仓位变更提醒」，逐条列出要做的动作
    · 仓位没变化 → 顶部绿色提示「仓位不变」，正常汇报当前持仓与盈亏

用法
----
    python btc_monitor.py                 检测 + 按配置发邮件
    python btc_monitor.py --no-mail       只检测，不发邮件
    python btc_monitor.py --refresh       强制重新拉取最新行情
    python btc_monitor.py --live          允许真实下单（还需配置 enabled=true）
    python btc_monitor.py --json out.json 额外把结果写成 JSON
    python btc_monitor.py --dump-html p.html  把邮件正文另存为 HTML（本地预览用，不发邮件）
    python btc_monitor.py --reset-state   清空本地状态（重新建立基准）
    python btc_monitor.py --force-mail    同一根日线已发过邮件时，仍然再发一封

口径说明
--------
· 邮件/日志的主口径是【从现在起算】：本金就是 base_capital_usdt（默认 1000 U），
  账户现值 = 本金 + 当前浮盈。历史回测的已平仓盈亏**不计入**（那是回测数据，不是实盘）。
  仓位也按 base 算：名义 = base × 敞口。
· 同时给一行【回测全程参考】：若从 2017-08 就用 base 一路跟着做，
  固定本金（每笔按 base 开仓）终值约 9,836 U（9.8 倍），
  滚仓（每笔按当时权益开仓、利润滚入下一笔）终值约 142,941 U（142.9 倍）。
· 两者收益率完全相同，滚仓只是把绝对金额放大约 14.5 倍。不要混用。

安全设计
--------
· 下单默认「关闭 + 演练」：monitor_config.json 里 binance.enabled 默认 false，
  且必须命令行显式加 --live 才可能真实发单。任何一步不满足，都只打印计划不下单。
· 密钥缺失、网络失败、余额不足 → 一律降级为「只报告不下单」，绝不盲目下单。
· 持仓成本价按回测口径（信号翻转次日开盘成交）从历史推演，不依赖任何外部接口。
· 同一根已收盘日线**只发一封邮件**：state 里记 last_mailed_bar，重复运行自动跳过。
  这样即便计划任务 / 手动双击 / 批处理重试叠加触发，你也不会收到多封重复邮件
  （确实想强发时用 --force-mail）。
"""

import hashlib
import hmac
import json
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btc_strategy as E                      # 单一事实来源：数据 / 信号 / 引擎 / 统计

CONFIG_PATH = os.path.join(HERE, "monitor_config.json")
STATE_PATH = os.path.join(HERE, "monitor_state.json")
LOG_PATH = os.path.join(HERE, "monitor_log.txt")

DEFAULT_CFG = {
    "email": {
        "enabled": True,
        "sender": "", "password": "", "to": [],
        "server": "smtp.163.com", "port": 465, "use_ssl": True,
        "max_retries": 3,
        "strategy_label": "BTC动量策略 · SMA50过滤多头 1×做空",
    },
    "binance": {
        "enabled": False, "dry_run": True, "use_testnet": True,
        "api_key": "", "api_secret": "",
        "spot_base": "https://api.binance.com",
        "futures_base": "https://fapi.binance.com",
        "testnet_spot_base": "https://testnet.binance.vision",
        "testnet_futures_base": "https://testnet.binancefuture.com",
        "symbol": "BTCUSDT", "leverage": 1, "margin_type": "ISOLATED",
        "min_notional_usdt": 20.0,
    },
    "strategy": {
        "symbol": "BTCUSDT", "vt": 0.03, "exposure_cap": 1.0, "atr_n": 14,
        "regime_ma": 50,          # 只约束多头：收盘价 > SMA(regime_ma) 才允许做多；0 = 关闭
        "warmup": 60, "cache_max_age_hours": 1,
        "base_capital_usdt": 1000.0,
        "primary_set": [10, 30],  # 唯一的参数组：双均线 EMA 10/30
        # True = 当前这笔不计入账户（用户没参与），等下一次仓位变化再按本金开始计算
        "participate_from_next_trade": False,
        "always_mail": True, "mail_only_on_change": False,
    },
}

# 邮件配色（涨红跌绿，A 股/国内习惯）
C_UP = "#c0392b"      # 红 = 盈利
C_DOWN = "#1e8449"    # 绿 = 亏损
C_MUTED = "#66788a"

MARGIN_BUFFER = 0.01  # 空头腿保证金缓冲：1× 逐仓会占满 100% 保证金，留 1% 否则交易所拒单


def short_size_factor(lev):
    """
    空头腿下单量系数：把名义敞口压到「保证金 + 手续费一定开得出来」的范围内。

    1× 逐仓需要 100% 保证金（名义 = 权益），再加上手续费就超过账户余额，
    交易所会以「保证金不足」直接拒单 —— 所以必须缩量留缓冲。
    杠杆 ≥ 2× 时保证金占用 ≤ 50%，系数就是 1.0（不缩量）。
    """
    return min(1.0, float(lev) * (1.0 - MARGIN_BUFFER))


# ==============================================================================
# 0. 工具
# ==============================================================================
def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        return DEFAULT_CFG
    with open(CONFIG_PATH, encoding="utf-8") as f:
        user = json.load(f)
    return deep_merge(DEFAULT_CFG, user)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 1, "positions": {}, "last_run": None, "last_bar": None,
            "last_mailed_bar": None, "last_mail_at": None, "alerts": []}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


LOG_BUF = []


def log(msg=""):
    LOG_BUF.append(str(msg))
    try:
        print(msg)
    except UnicodeEncodeError:
        print(str(msg).encode("utf-8", "replace").decode("utf-8", "replace"))


def fdate(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def fdt(ms=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000 if ms else None))


def pct(v, d=1):
    return ("%+." + str(d) + "f%%") % (v * 100)


def mult(v):
    return "%.2f×" % v


def money(v, d=2):
    """带千分位与正负号：+1,234.56 / -98.70"""
    return format(v, "+,." + str(d) + "f")


def sig_text(v):
    return {1: "做多", -1: "做空", 0: "空仓"}.get(v, "空仓")


def sig_color(v):
    return C_UP if v > 0 else (C_DOWN if v < 0 else C_MUTED)


def regime_long(bars, sig, ma):
    """
    只约束多头的趋势过滤：收盘价在 SMA(ma) 下方时把做多信号置 0，做空信号原样保留。
    ma 为 0 / None / <=1 时不过滤（原样返回）。

    只约束多头的原因：BTC 的暴跌段几乎都发生在长期均线下方，
    挡掉「均线下方抄底做多」能明显削回撤（2022 年 -33.0% → -19.6%）；
    而空头本来就靠下跌赚钱，不该被均线挡住 —— 实测「只约束空头」或「双向过滤」都更差。
    """
    if not ma or int(ma) <= 1:
        return list(sig)
    c = [b["close"] for b in bars]
    m = E.sma(c, int(ma))
    out = list(sig)
    for i in range(len(bars)):
        if m[i] != m[i] or (sig[i] > 0 and c[i] <= m[i]):    # NaN 自检
            out[i] = 0
    return out


# ==============================================================================
# 1. 数据：只取「已收盘」的日线
# ==============================================================================
def closed_bars(bars, now_ms=None):
    """
    丢掉最后一根尚未收盘的 K 线。
    币安日线以 UTC 00:00 为起点，收盘时间 = open_time + 1 天。
    这样信号只建立在真正收盘的 K 线上，避免用未走完的当日行情做决策。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return [b for b in bars if b["t"] + E.DAY_MS <= now_ms]


def load_bars(symbol, force=False, max_age_h=1.0):
    """
    取行情。缓存超过 max_age_h 小时就重新拉，避免每日任务读到旧数据。
    网络失败时回退到本地缓存（有总比没有好），但会明确提示。
    """
    path = os.path.join(E.DATA_DIR, "%s_1d.csv" % symbol)
    age = None
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 3600.0
        if not force and age < max_age_h:
            log("      使用本地缓存（%.2f 小时前更新，阈值 %.1f 小时）" % (age, max_age_h))
            return E.load(symbol)
    try:
        return E.ensure_data(symbol, refresh=True)
    except Exception as e:
        if os.path.exists(path):
            log("      ⚠ 拉取失败（%s），回退到 %.2f 小时前的本地缓存" % (e, age or -1))
            return E.load(symbol)
        raise


# ==============================================================================
# 2. 方案检测
# ==============================================================================
def exposure_at(bars, cfg, i):
    """第 i 根 K 线执行时的敞口倍数 = min(上限, 波动率目标 / 前一日波动率)。"""
    st = cfg["strategy"]
    a = E.atr(bars, st["atr_n"])
    j = max(i - 1, 0)
    dvol = a[j] / bars[j]["close"] if a[j] == a[j] else 0.05    # NaN 自检
    return (min(st["exposure_cap"], st["vt"] / dvol) if dvol > 0 else st["exposure_cap"]), dvol


def derive_position(bars, sig, cfg, tag, pending=False):
    """
    从信号历史推演出「当前这一笔持仓」的明细，同时给两套口径：

    【滚仓口径 · 主口径】起点 base_capital_usdt（默认 1000 U），
        每笔名义 = 当时账户权益 × 敞口 → 赚了自动放大下一笔，亏了自动缩小。
        等价于「把利润全部滚进下一笔仓位」，也就是复利。

    【固定本金口径 · 对照】每笔都按固定的 base_capital_usdt 重新起算，不复利。
        pnl_fixed = pnl_engine × base / equity[entry_i - 1]
        （敞口倍数在分子分母同时出现，自动约掉，缩放是精确的。）

    两套口径的交易规则完全相同，唯一区别是「赚到的钱要不要立刻滚进下一笔」。

    口径与回测一致：信号在第 i 根收盘确认 → 第 i+1 根开盘成交（含滑点）。
    因此成本价 = 翻转次根的开盘价，而不是今天的收盘价。
    """
    st = cfg["strategy"]
    base = float(st.get("base_capital_usdt", 1000.0))
    lev = float(cfg["binance"]["leverage"])
    sig = regime_long(bars, sig, st.get("regime_ma", 0))   # 幂等：调用方已过滤也无妨
    res = E.backtest_hybrid(bars, sig, E.CAPITAL, L=lev,
                            exposure_cap=st["exposure_cap"], vt=st["vt"],
                            atr_n=st["atr_n"], warmup=st["warmup"])
    eq = res["equity"]
    n = len(bars)
    cur_sig = sig[-1]
    cur_px = bars[-1]["close"]
    comp_scale = (base / E.CAPITAL) if E.CAPITAL else 1.0

    # ---- 历史已平仓交易 ----
    realized, closed_n, wins = 0.0, 0, 0
    gross_win, gross_loss = 0.0, 0.0
    recent = []
    for t in res["trades"]:
        j = t["entry_i"] - 1
        e_ref = eq[j] if (0 <= j < n and eq[j]) else E.CAPITAL
        scale = base / e_ref if e_ref else 0.0
        pnl = t["pnl"] * scale
        realized += pnl
        closed_n += 1
        if t["pnl"] > 0:                    # 胜率/盈亏比用引擎原始金额，避免被复利缩放放大
            wins += 1
            gross_win += t["pnl"]
        else:
            gross_loss += -t["pnl"]
        recent.append({
            "entry_date": fdate(bars[t["entry_i"]]["t"]),
            "exit_date": fdate(bars[t["exit_i"]]["t"]),
            "dir": t["dir"], "entry_px": t["entry_px"], "exit_px": t["exit_px"],
            "pnl": pnl, "comp_pnl": t["pnl"] * comp_scale,
            "liq": bool(t.get("liq")), "leg": t.get("leg"),
        })
    win_rate = (wins / closed_n) if closed_n else 0.0
    losses = closed_n - wins
    pl_ratio = ((gross_win / wins) / (gross_loss / losses)) if (wins and losses and gross_loss) else 0.0

    # ---- 滚仓口径的账户权益（引擎原生净值，已 mark-to-market 到最新收盘）----
    eq_last = None
    for v in reversed(eq):
        if v:
            eq_last = v
            break
    comp_equity = (eq_last or E.CAPITAL) * comp_scale

    out = {
        "tag": tag, "dir": cur_sig, "cur_px": cur_px, "base": base,
        "entry_date": None, "entry_px": None, "qty": 0.0, "notional": 0.0,
        "mkt_value": 0.0, "upnl": 0.0, "upnl_pct": 0.0, "hold_days": 0, "expo": 0.0,
        "leverage": lev, "realized": realized, "closed_n": closed_n,
        "win_rate": win_rate, "pl_ratio": pl_ratio,
        "mdd": res["stats"].get("mdd", 0.0),
        "uw": res["stats"].get("max_underwater", 0),
        "adverse": res["stats"].get("short_adverse", 0.0),
        "equity": base + realized,
        # 主口径：从现在起算（历史已平仓不计入，账户 = 本金 + 当前浮盈）
        # pending=True（本轮尚未参与）时，当前这笔的浮盈不计入账户，账户就是本金
        "pending": bool(pending),
        "now_equity": base, "now_mult": 1.0,
        # 回测全程参考
        "comp_realized": comp_equity - base, "comp_equity": comp_equity,
        "comp_qty": 0.0, "comp_notional": 0.0, "comp_mkt_value": 0.0,
        "comp_upnl": 0.0, "comp_mult": (comp_equity / base) if base else 0.0,
        "leg": ("现货" if cur_sig > 0 else
                ("合约 %d× 逐仓" % int(lev) if cur_sig < 0 else "—")),
        "recent": recent[-3:],
    }

    if cur_sig == 0:
        return out

    # ---- 当前持仓：找本轮起点（最后一个信号翻转处）----
    i = n - 1
    while i > 0 and sig[i - 1] == cur_sig:
        i -= 1
    entry_i = min(i + 1, n - 1)

    expo, _ = exposure_at(bars, cfg, entry_i)
    entry_px = bars[entry_i]["open"]
    entry_px = entry_px * (1 + E.SLIP) if cur_sig > 0 else entry_px * (1 - E.SLIP)

    days = n - 1 - entry_i

    def _upnl(q, s):
        """净浮盈：现在平掉能拿到多少（扣手续费 / 资金费）"""
        if s > 0:
            return q * cur_px * (1 - E.SPOT_FEE) - q * entry_px * (1 + E.SPOT_FEE)
        fg = q * cur_px * E.FUNDING_D * days          # 空头收资金费
        return q * (entry_px - cur_px) - q * cur_px * E.FUT_FEE + fg

    # 固定本金口径
    notional = base * expo
    qty = notional / entry_px if entry_px > 0 else 0.0
    upnl = _upnl(qty, cur_sig)

    # 滚仓口径：这笔是在入场时按「当时的账户权益 × 敞口」开的
    j = entry_i - 1
    eq_ref = eq[j] if (0 <= j < n and eq[j]) else E.CAPITAL
    comp_notional = eq_ref * expo * comp_scale
    comp_qty = comp_notional / entry_px if entry_px > 0 else 0.0
    comp_upnl = _upnl(comp_qty, cur_sig)

    out.update({
        "entry_date": fdate(bars[entry_i]["t"]), "entry_px": entry_px,
        "qty": qty, "notional": notional, "expo": expo,
        "mkt_value": qty * cur_px, "upnl": upnl,
        "upnl_pct": (upnl / notional) if notional > 0 else 0.0,
        "hold_days": days,
        "equity": base + realized + upnl,
        # 主口径：从现在起算，历史已平仓不计入。
        # pending=True → 这笔不算你的（你没参与），账户就还是本金，倍数 1.00×。
        "now_equity": base if pending else (base + upnl),
        "now_mult": 1.0 if pending else ((base + upnl) / base if base else 0.0),
        # 滚仓口径
        "comp_qty": comp_qty, "comp_notional": comp_notional,
        "comp_mkt_value": comp_qty * cur_px, "comp_upnl": comp_upnl,
    })
    # 已实现用反推，保证「本金 + 已实现 + 浮盈 = 账户现值」三行能精确对上
    # （引擎的净值已含开仓手续费，而浮盈是「现在平掉能拿到多少」，两者差一个平仓成本）
    out["comp_realized"] = comp_equity - base - comp_upnl
    return out


def short_risk(bars, sig, cfg):
    """
    空头腿风控：L× 逐仓的爆仓线在 入场价 ×(1+1/L)。
    用本轮空头区间的最高价估算最大逆行与安全边际。
    """
    lev = float(cfg["binance"]["leverage"])
    sig = regime_long(bars, sig, cfg["strategy"].get("regime_ma", 0))
    line = 1.0 / lev
    if sig[-1] >= 0:
        return None
    i = len(bars) - 1
    while i > 0 and sig[i - 1] < 0:
        i -= 1
    entry_i = min(i + 1, len(bars) - 1)
    entry_px = bars[entry_i]["open"]
    peak = max(b["high"] for b in bars[entry_i:])
    adverse = 1.0 - peak / entry_px
    return {"entry_date": fdate(bars[entry_i]["t"]), "entry_px": entry_px,
            "peak": peak, "adverse": adverse, "margin": adverse + line,
            "liq_px": entry_px * (1 + line), "lev": lev}


# ==============================================================================
# 3. 币安下单（默认关闭 + 演练）
# ==============================================================================
class Binance:
    """极简币安 REST 客户端（只用标准库，HMAC-SHA256 签名）。"""

    def __init__(self, cfg):
        b = cfg["binance"]
        testnet = bool(b["use_testnet"])
        self.spot = b["testnet_spot_base"] if testnet else b["spot_base"]
        self.fut = b["testnet_futures_base"] if testnet else b["futures_base"]
        self.key = (b["api_key"] or "").strip()
        self.secret = (b["api_secret"] or "").strip()
        self.testnet = testnet
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def has_keys(self):
        return bool(self.key and self.secret)

    def _request(self, method, base, path, params=None, signed=False, timeout=20):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            qs = qs + "&signature=" + sig
        else:
            qs = urllib.parse.urlencode(params)
        url = base + path + ("?" + qs if qs else "")
        req = urllib.request.Request(url, method=method)
        req.add_header("X-MBX-APIKEY", self.key)
        req.add_header("User-Agent", "btc-monitor/1.0")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError("HTTP %d %s -> %s" % (e.code, path, body))

    # ---- 只读 ----
    def spot_balance(self, asset="BTC"):
        acc = self._request("GET", self.spot, "/api/v3/account", signed=True)
        for b in acc.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"]) + float(b["locked"])
        return 0.0

    def usdt_balance(self):
        acc = self._request("GET", self.spot, "/api/v3/account", signed=True)
        for b in acc.get("balances", []):
            if b["asset"] == "USDT":
                return float(b["free"])
        return 0.0

    def futures_position(self, symbol):
        data = self._request("GET", self.fut, "/fapi/v1/positionRisk",
                             {"symbol": symbol}, signed=True)
        for p in data:
            amt = float(p.get("positionAmt", 0))
            if abs(amt) > 0:
                return {"amt": amt, "entry": float(p.get("entryPrice", 0)),
                        "upnl": float(p.get("unRealizedProfit", 0)),
                        "lev": p.get("leverage")}
        return {"amt": 0.0, "entry": 0.0, "upnl": 0.0, "lev": None}

    # ---- 写操作 ----
    def set_leverage(self, symbol, lev):
        return self._request("POST", self.fut, "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": int(lev)}, signed=True)

    def set_margin_type(self, symbol, mt):
        try:
            return self._request("POST", self.fut, "/fapi/v1/marginType",
                                 {"symbol": symbol, "marginType": mt}, signed=True)
        except RuntimeError as e:
            if "-4046" in str(e):        # 已经是逐仓，无需修改
                return {"msg": "already " + mt}
            raise

    def spot_market_order(self, symbol, side, qty):
        return self._request("POST", self.spot, "/api/v3/order",
                             {"symbol": symbol, "side": side, "type": "MARKET",
                              "quantity": ("%.6f" % qty).rstrip("0").rstrip(".")},
                             signed=True)

    def futures_market_order(self, symbol, side, qty, reduce_only=False):
        p = {"symbol": symbol, "side": side, "type": "MARKET",
             "quantity": ("%.3f" % qty).rstrip("0").rstrip(".")}
        if reduce_only:
            p["reduceOnly"] = "true"
        return self._request("POST", self.fut, "/fapi/v1/order", p, signed=True)


def build_order_plan(pos, cfg, binance, price):
    """根据目标持仓生成「要做什么」的计划（不执行）。"""
    b = cfg["binance"]
    symbol = b["symbol"]
    base = symbol.replace("USDT", "")
    actions, notes = [], []
    sig = pos["dir"]

    # 尚未开始参与：当前这笔不计入账户，也不需要任何操作
    if pos.get("pending"):
        actions.append("本轮不参与：当前这笔（%s 开的%s）你没有实际持仓，不计入账户。"
                       % (pos.get("entry_date") or "—", sig_text(sig) if sig else "空仓"))
        actions.append("等信号翻转后，按本金 %.0f USDT × 敞口 %.2f× 进场。"
                       % (pos["base"], pos["expo"] or 0.0))
        if not binance.has_keys():
            notes.append("未配置币安 API Key → 跳过仓位核对，只给目标方向。")
        return actions, notes

    if not binance.has_keys():
        notes.append("未配置币安 API Key → 跳过仓位核对，只给目标方向。")
        actions.append("目标：%s（%s）" % (sig_text(sig), pos["leg"]))
        if sig != 0:
            actions.append("参考成本价 %.2f，当前 %.2f，浮动盈亏 %s USDT（%s）"
                           % (pos["entry_px"], pos["cur_px"], money(pos["upnl"]),
                              pct(pos["upnl_pct"])))
            actions.append("应持 %.6f BTC（名义 %.2f USDT，敞口 %.2f×）"
                           % (pos["qty"], pos["notional"], pos["expo"]))
        return actions, notes

    try:
        spot_qty = binance.spot_balance(base)
        usdt = binance.usdt_balance()
        fut = binance.futures_position(symbol)
    except Exception as e:
        notes.append("读取账户失败：%s → 跳过下单" % e)
        return actions, notes

    equity = usdt + spot_qty * price + fut["upnl"]
    target_qty = equity * pos["expo"] / price if pos["expo"] else 0.0
    notes.append("账户权益 ≈ %.2f USDT（USDT %.2f + 现货 %.6f %s ≈ %.2f + 合约浮盈 %+.2f）"
                 % (equity, usdt, spot_qty, base, spot_qty * price, fut["upnl"]))
    if pos["expo"]:
        notes.append("目标名义敞口 %.2f USDT（敞口倍数 %.2f×）" % (equity * pos["expo"], pos["expo"]))

    if fut["amt"] != 0 and sig >= 0:
        actions.append("平合约空头：市价买入 %.3f %s（reduceOnly）" % (abs(fut["amt"]), symbol))
    if spot_qty > 0 and sig <= 0:
        actions.append("卖出现货多头：市价卖出 %.6f %s" % (spot_qty, base))

    if sig > 0:
        need = target_qty - spot_qty
        if need * price >= b["min_notional_usdt"]:
            actions.append("现货买入：市价买入 %.6f %s（≈ %.2f USDT）" % (need, base, need * price))
        elif need < 0 and -need * price >= b["min_notional_usdt"]:
            actions.append("现货减仓：市价卖出 %.6f %s" % (-need, base))
        else:
            actions.append("现货仓位已接近目标，无需操作")
    elif sig < 0:
        sf = short_size_factor(b["leverage"])
        short_qty = target_qty * sf
        if fut["amt"] == 0:
            actions.append("设置 %s 为逐仓 %d×" % (symbol, b["leverage"]))
            actions.append("开合约空头：市价卖出 %.3f %s（逐仓 %d×）"
                           % (short_qty, symbol, b["leverage"]))
            if sf < 1.0:
                actions.append("（已按 %.1f%% 缩量留保证金缓冲，否则 1× 逐仓会因保证金不足被拒单）"
                               % (sf * 100))
        else:
            actions.append("合约空头已存在（%.3f），无需加仓" % fut["amt"])
    else:
        if not actions:
            actions.append("已是空仓，无需操作")

    return actions, notes


def execute_plan(pos, cfg, binance, price, live):
    """真正下单。只有 enabled=true 且命令行给了 --live 才会走到这里。"""
    b = cfg["binance"]
    symbol = b["symbol"]
    base = symbol.replace("USDT", "")
    sig = pos["dir"]
    done = []

    if not (b["enabled"] and live):
        return ["未启用真实下单（需配置 enabled=true 且命令行加 --live）"]
    if not binance.has_keys():
        return ["缺少 API Key，取消下单"]

    try:
        spot_qty = binance.spot_balance(base)
        usdt = binance.usdt_balance()
        fut = binance.futures_position(symbol)
        equity = usdt + spot_qty * price + fut["upnl"]
        target_qty = equity * pos["expo"] / price if pos["expo"] else 0.0

        if fut["amt"] != 0 and sig >= 0:
            binance.futures_market_order(symbol, "BUY", abs(fut["amt"]), reduce_only=True)
            done.append("已平合约空头 %.3f" % abs(fut["amt"]))
            fut = binance.futures_position(symbol)

        if spot_qty > 0 and sig <= 0:
            binance.spot_market_order(symbol, "SELL", spot_qty)
            done.append("已卖出现货 %.6f" % spot_qty)
            spot_qty = 0.0

        if sig > 0:
            need = target_qty - spot_qty
            if need * price >= b["min_notional_usdt"]:
                r = binance.spot_market_order(symbol, "BUY", need)
                done.append("已现货买入 %.6f（订单 %s）" % (need, r.get("orderId")))
            elif need < 0 and -need * price >= b["min_notional_usdt"]:
                r = binance.spot_market_order(symbol, "SELL", -need)
                done.append("已现货卖出 %.6f（订单 %s）" % (-need, r.get("orderId")))
        elif sig < 0 and fut["amt"] == 0:
            binance.set_margin_type(symbol, b["margin_type"])
            binance.set_leverage(symbol, b["leverage"])
            r = binance.futures_market_order(symbol, "SELL",
                                             target_qty * short_size_factor(b["leverage"]))
            done.append("已开空 %.3f（订单 %s）"
                        % (target_qty * short_size_factor(b["leverage"]), r.get("orderId")))
    except Exception as e:
        done.append("下单过程中出错，已停止：%s" % e)

    return done or ["无需操作"]


# ==============================================================================
# 4. 邮件（手机优先的表格版）
# ==============================================================================
def _money_cell(v):
    c = C_UP if v > 0 else (C_DOWN if v < 0 else C_MUTED)
    return '<span style="color:%s;font-weight:700">%s</span>' % (c, money(v))


def _pct_cell(v):
    c = C_UP if v > 0 else (C_DOWN if v < 0 else C_MUTED)
    return '<span style="color:%s;font-weight:700">%s</span>' % (c, pct(v))


def build_mail_html(res):
    cfg = res["cfg"]
    sym = cfg["strategy"]["symbol"]
    pos = res["position"]            # 唯一方案的当前持仓
    changes = res["changes"]         # 是否发生仓位变化（0 或 1 条）
    pending = bool(pos.get("pending"))   # True = 这笔用户没参与，不计入账户
    rma = cfg["strategy"].get("regime_ma", 0)
    regime_txt = ("SMA%d 只约束多头（收盘价在均线下方时不开多、空头照做）" % int(rma)
                  if rma else "无长期均线过滤")

    def pos_row(p, label):
        if p["dir"] == 0:
            return ('<tr>'
                    '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;font-size:13px">%s</td>'
                    '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;text-align:center;'
                    'font-size:13px;color:%s">空仓</td>'
                    '<td colspan="3" style="padding:11px 4px;border-bottom:1px solid #eef1f5;'
                    'text-align:center;color:#8a9aab;font-size:12.5px">当前无持仓</td></tr>'
                    % (label, C_MUTED))
        c = sig_color(p["dir"])
        return ('<tr>'
                '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;font-size:13px">%s</td>'
                '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;text-align:center;'
                'font-size:13px;font-weight:700;color:%s">%s</td>'
                '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;text-align:right;'
                'font-size:12.5px">%.2f</td>'
                '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;text-align:right;'
                'font-size:12.5px">%.2f</td>'
                '<td style="padding:11px 4px;border-bottom:1px solid #eef1f5;text-align:right;'
                'font-size:12.5px;white-space:nowrap">%s<br><span style="font-size:11.5px">%s</span></td>'
                '</tr>'
                % (label, c, sig_text(p["dir"]), p["entry_px"], p["cur_px"],
                   _money_cell(p["upnl"]), _pct_cell(p["upnl_pct"])))

    # ---------- 顶部横幅：未参与 / 变更 / 不变 ----------
    if pending:
        banner = ('<div class="banner" style="background:#eef3f8;border-left:5px solid #2471a3;'
                  'border-radius:0 10px 10px 0;padding:13px 16px;margin-bottom:14px">'
                  '<div style="color:#1b4f72;font-size:15px;font-weight:700">尚未开始参与 · 本轮无需操作</div>'
                  '<div style="color:#33607f;font-size:12.5px;margin-top:6px;line-height:1.65">'
                  '当前这笔（%s 开的%s）你还没有实际持仓，<b>不计入账户</b> —— '
                  '账户现值就是本金 <b>%.0f USDT</b>。等信号翻转后按本金 %.0f USDT × 敞口进场，'
                  '从那一笔开始计入。</div></div>'
                  % (pos.get("entry_date") or "—",
                     sig_text(pos["dir"]) if pos["dir"] else "空仓",
                     pos["base"], pos["base"]))
    elif changes:
        detail = "；".join(changes)
        banner = ('<div class="banner" style="background:%s;border-radius:10px;padding:15px 16px;'
                  'margin-bottom:14px">'
                  '<div style="color:#fff;font-size:17px;font-weight:800;letter-spacing:.3px">'
                  '【仓位变更提醒】请及时操作</div>'
                  '<div style="color:#ffe6e2;font-size:13px;margin-top:7px;line-height:1.65">%s</div></div>'
                  % (C_UP, detail))
    else:
        hold = "%s %s" % (pos["tag"], sig_text(pos["dir"]))
        banner = ('<div class="banner" style="background:#eef7f0;border-left:5px solid %s;'
                  'border-radius:0 10px 10px 0;padding:13px 16px;margin-bottom:14px">'
                  '<div style="color:#14532d;font-size:15px;font-weight:700">仓位不变，无需操作</div>'
                  '<div style="color:#3d6b4f;font-size:12.5px;margin-top:6px;line-height:1.65">%s</div></div>'
                  % (C_DOWN, hold))

    # ---------- 持仓明细卡 ----------
    def detail_card(p, label, extra):
        if p["dir"] == 0:
            return ('<div style="background:#fff;border-radius:12px;padding:14px 16px;margin-top:12px">'
                    '<div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:9px">%s</div>'
                    '<table role="presentation" style="width:100%%;border-collapse:collapse;font-size:13.5px">'
                    '<tr><td style="padding:4px 0;color:%s;width:46%%">本金</td>'
                    '<td style="padding:4px 0">%.0f USDT（空仓中，全额在手）</td></tr>'
                    '<tr><td style="padding:4px 0;color:%s">账户现值<span style="font-size:11.5px">（现在起算）</span></td>'
                    '<td style="padding:4px 0;font-weight:700">%.2f USDT（%.2f 倍）</td></tr>'
                    '<tr><td style="padding:4px 0;color:%s">↳ 回测全程参考</td>'
                    '<td style="padding:4px 0;color:#8a9aab;font-size:12.5px">'
                    '滚仓 %.0f USDT（%.1f 倍）· 固定本金 %.0f USDT（%.1f 倍）</td></tr>'
                    '%s</table></div>'
                    % (label, C_MUTED, p["base"], C_MUTED,
                       p["now_equity"], p["now_mult"],
                       C_MUTED, p["comp_equity"], p["comp_mult"],
                       p["equity"], p["equity"] / p["base"] if p["base"] else 0.0,
                       extra))
        c = sig_color(p["dir"])
        # 「本金 + 浮盈」那一行的小字：未参与时浮盈不计入，措辞要跟着变
        eq_sub = ("本金 %s（这笔未参与，浮盈不计入）" % format(p["base"], ",.0f")
                  if p.get("pending") else
                  "本金 %s + 浮盈 %s" % (format(p["base"], ",.0f"), money(p["upnl"])))
        return ('<div style="background:#fff;border-radius:12px;padding:14px 16px;margin-top:12px">'
                '<div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:9px">'
                '%s <span style="font-weight:400;color:%s;font-size:12.5px">· %s</span></div>'
                '<table role="presentation" style="width:100%%;border-collapse:collapse;font-size:13.5px">'
                '<tr><td style="padding:4px 0;color:%s;width:46%%">成本价（入场价）</td>'
                '<td style="padding:4px 0;font-weight:700">%.2f USDT</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">最新收盘价</td>'
                '<td style="padding:4px 0">%.2f USDT</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">持仓数量</td>'
                '<td style="padding:4px 0">%.6f %s</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">入场名义（本金×敞口）</td>'
                '<td style="padding:4px 0">%.2f USDT（%.0f × %.2f× 仓位比例）</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">当前市值</td>'
                '<td style="padding:4px 0">%.2f USDT</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">浮动盈亏</td>'
                '<td style="padding:4px 0">%s USDT（%s）</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">已持有</td>'
                '<td style="padding:4px 0">%d 天（自 %s）</td></tr>'
                '<tr><td style="padding:4px 0;color:%s">账户现值<span style="font-size:11.5px">（现在起算）</span></td>'
                '<td style="padding:4px 0;font-weight:700">%.2f USDT（%.2f 倍）'
                '<div style="font-size:11px;font-weight:400;color:#8a9aab;margin-top:3px;line-height:1.5">'
                '%s</div></td></tr>'
                '<tr><td style="padding:4px 0;color:%s">↳ 回测全程参考</td>'
                '<td style="padding:4px 0;color:#8a9aab;font-size:12.5px">'
                '滚仓 %.0f USDT（%.1f 倍）· 固定本金 %.0f USDT（%.1f 倍）</td></tr>'
                '%s</table></div>'
                % (label, c, p["leg"], C_MUTED, p["entry_px"], C_MUTED, p["cur_px"], C_MUTED,
                   p["qty"], sym.replace("USDT", ""), C_MUTED,
                   p["notional"], p["base"], p["expo"], C_MUTED, p["mkt_value"], C_MUTED,
                   _money_cell(p["upnl"]), _pct_cell(p["upnl_pct"]), C_MUTED,
                   p["hold_days"], p["entry_date"], C_MUTED,
                   p["now_equity"], p["now_mult"],
                   eq_sub,
                   C_MUTED, p["comp_equity"], p["comp_mult"],
                   p["equity"], p["equity"] / p["base"] if p["base"] else 0.0,
                   extra))

    extra = ""
    if pos.get("pending"):
        extra += ('<tr><td style="padding:4px 0;color:%s">参与状态</td>'
                  '<td style="padding:4px 0;color:#1b4f72"><b>尚未参与</b>'
                  '<div style="font-size:11px;font-weight:400;color:#8a9aab;margin-top:3px;line-height:1.5">'
                  '这笔你没有实际持仓，上面的浮盈只是信号推演，不计入账户；从下一笔开始计入</div></td></tr>'
                  % C_MUTED)
    if pos["dir"] < 0 and res["short_risk"]:
        r = res["short_risk"]
        extra = ('<tr><td style="padding:4px 0;color:%s">爆仓价 / 安全余量</td>'
                 '<td style="padding:4px 0">%.2f USDT / <b>%.2f pp</b></td></tr>'
                 % (C_MUTED, r["liq_px"], r["margin"] * 100))

    # ---------- 最近平仓 ----------
    def recent_table(p, title):
        rec = p["recent"][-3:]
        if not rec:
            return ""
        rows = ""
        for t in rec:
            rows += ('<tr><td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px">'
                     '%s → %s<div style="color:#8a9aab;font-size:11.5px">%.2f → %.2f</div></td>'
                     '<td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px;'
                     'text-align:center;color:%s">%s</td>'
                     '<td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px;'
                     'text-align:right;white-space:nowrap">%s</td></tr>'
                     % (t["entry_date"], t["exit_date"], t["entry_px"], t["exit_px"],
                        sig_color(t["dir"]), sig_text(t["dir"]), _money_cell(t["pnl"])))
        return ('<div style="background:#fff;border-radius:12px;padding:14px 16px;margin-top:12px">'
                '<div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:8px">%s'
                '<span style="font-weight:400;font-size:12px;color:#8a9aab">%s</span></div>'
                '<table role="presentation" style="width:100%%;border-collapse:collapse;'
                'table-layout:fixed">'
                '<colgroup><col style="width:52%%"><col style="width:20%%"><col style="width:28%%"></colgroup>'
                '<tr><th style="padding:6px 4px;text-align:left;font-size:12px;color:%s">持仓期 / 成本→出场</th>'
                '<th style="padding:6px 4px;text-align:center;font-size:12px;color:%s">方向</th>'
                '<th style="padding:6px 4px;text-align:right;font-size:12px;color:%s">盈亏(U)</th></tr>'
                '%s</table></div>'
                % (title, "（回测历史，不计入上方账户）", C_MUTED, C_MUTED, C_MUTED, rows))

    recent = recent_table(pos, "最近 3 笔已平仓")

    # ---------- 下单计划 ----------
    orders_html = ""
    if res["orders"]:
        li = "".join('<li style="margin:5px 0">%s</li>' % a for a in res["orders"])
        note = "".join('<div style="color:#8a9aab;font-size:12.5px;margin-top:4px">%s</div>' % n
                       for n in res["notes"])
        mode = ("<b style='color:%s'>真实下单已开启</b>" % C_UP if res["live_enabled"]
                else "<b>演练模式（不会真实下单）</b>")
        orders_html = ('<div style="background:#fff;border-radius:12px;padding:14px 16px;margin-top:12px;'
                       'font-size:13.5px;color:#3d566e">'
                       '<div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:8px">'
                       '执行计划</div>%s<ul style="margin:8px 0 8px 18px;padding:0">%s</ul>%s</div>'
                       % (mode, li, note))

    warn = ""
    if res["warnings"]:
        warn = ('<div style="background:#fef9f0;border-left:4px solid #d68910;padding:11px 15px;'
                'border-radius:0 8px 8px 0;font-size:13px;color:#7a5410;margin-top:12px">'
                + "<br>".join(res["warnings"]) + "</div>")

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#f2f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;-webkit-text-size-adjust:100%%}
.card{background:#fff;border-radius:12px;padding:15px 16px;margin-top:12px}
table{width:100%%;border-collapse:collapse;table-layout:fixed}
.banner{word-break:break-word;overflow-wrap:anywhere}
.tbl th{background:#eef3f8;padding:9px 4px;font-size:12px;color:#34495e;font-weight:700;white-space:nowrap}
.tbl td{font-size:13px}
@media only screen and (max-width:480px){
  .wrap{padding:12px 10px 30px !important}
  .card{padding:13px 11px !important}
  .tbl th{font-size:11px !important;padding:8px 2px !important}
  .tbl td{font-size:12px !important;padding:9px 2px !important}
}
</style></head>
<body>
<div class="wrap" style="max-width:680px;margin:0 auto;padding:20px 14px 40px">

%(banner)s

<div style="background:#fff;border-radius:12px;padding:16px 18px">
  <div style="font-size:18px;font-weight:800;color:#1c2833">BTC 日线趋势策略 · 每日检测</div>
  <div style="color:%(muted)s;font-size:12.5px;margin-top:6px;line-height:1.6">
    标的 %(sym)s · 数据截至 <b>%(bar)s</b> 收盘（%(close).2f USDT）<br>
    <b>本金 %(base).0f USDT</b>（从现在起算，历史回测盈亏不计入）· 当前 <b>%(now_mult).2f 倍</b><br>
    %(rule)s · 生成时间 %(gen)s</div>
</div>

<div class="card">
  <div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:10px">当前持仓</div>
  <table class="tbl" role="presentation">
    <colgroup><col style="width:21%%"><col style="width:13%%"><col style="width:22%%">
      <col style="width:22%%"><col style="width:22%%"></colgroup>
    <tr><th style="text-align:left">策略</th><th style="text-align:center">持仓</th>
        <th style="text-align:right">成本价</th><th style="text-align:right">现价</th>
        <th style="text-align:right">浮动盈亏</th></tr>
    %(row)s
  </table>
  <div style="color:#8a9aab;font-size:11.5px;margin-top:9px;line-height:1.65">
    成本价 = 信号翻转后次日开盘成交价（含滑点）；名义 = %(base).0f USDT × 敞口比例；
    收益率 = 浮盈 ÷ 入场名义。盈亏均已扣手续费 / 资金费。金额单位 USDT。
  </div>
</div>

%(detail)s
%(recent)s
%(orders)s
%(warn)s

<div style="color:#8a9aab;font-size:11.5px;margin-top:20px;line-height:1.75">
  <b>关于「账户现值」：</b><br>
  · <b>本邮件采用「从现在起算」</b>：本金就是 <b>%(base).0f USDT</b>，
  账户现值 = %(base).0f 本金 + 当前浮盈，当前 <b>%(now).2f USDT（%(now_mult).2f 倍）</b>。
  历史回测里那 %(closed_n)d 笔已平仓盈亏<b>不计入</b>这个数字 —— 那是回测数据，不是你的实盘。
  仓位也按 %(base).0f U 算：名义 = %(base).0f × 敞口 %(expo).2f× = <b>%(notional).2f USDT</b>。<br>
  %(pending_note)s
  · <b>「回测全程参考」</b>（仅供了解这套策略长期跑下来什么水平）：
  若从 2017-08 就用 %(base).0f U 一路跟着做 ——
  <b>固定本金</b>（每笔都按 %(base).0f U 开仓）终值 %(fix).0f USDT（%(fix_mult).1f 倍）；
  <b>滚仓</b>（每笔按当时账户权益开仓、利润滚入下一笔）终值 %(cmp).0f USDT（%(cmp_mult).1f 倍）。
  两者收益率完全相同，滚仓只是把绝对金额放大了 %(mult_ratio).1f 倍。<br>
  · <b>往后的滚仓</b>：如果你要滚仓，就每笔按「当时账户权益 × 敞口 %(expo).2f×」下单，
  赚了自动放大下一笔。滚仓收益高得多，但回撤与爆仓风险同步放大，中途要能扛住 -50%% 回撤才拿得住，
  亏损期也会加速缩水。<b>建议先按固定 %(base).0f U 跑熟再考虑滚仓。</b><br><br>
  仓位规则：波动率目标 3%%/日，敞口上限 %(expocap).2f×；%(regime_txt)s；不设固定止盈止损，靠信号反转出场。<br>
  历史参考：2017-08 至今胜率 %(win_rate).0f%%，盈亏比 %(pl_ratio).1f，最大回撤 %(mdd_txt)s，最长水下 %(uw_txt)s。<br>
  空头腿固定 %(lev)d× 逐仓（价格逆行 +%(liqline).1f%% 即爆仓；历史最深逆行 %(adv_txt)s）；多头腿不上杠杆。<br>
  <b>本邮件是回测规则的机械信号，不构成投资建议。</b>加密资产波动极大，请自行判断并控制风险。
</div>

</div></body></html>""" % {
        "banner": banner, "muted": C_MUTED, "sym": sym,
        "bar": res["bar_date"], "close": res["close"], "gen": fdt(),
        "pending_note": (
            "<b>⚠ 当前这笔你还没有参与</b>（%s 开的%s），所以它的浮盈<b>不计入</b>账户现值，"
            "账户就是本金 <b>%.0f USDT</b>；等信号翻转后按本金进场，从那一笔开始计入。<br>"
            % (pos.get("entry_date") or "—",
               sig_text(pos["dir"]) if pos["dir"] else "空仓", pos["base"])
            if pending else ""),
        "base": pos["base"],
        "now": pos["now_equity"],
        "now_mult": pos["now_mult"],
        "notional": pos["notional"],
        "fix": pos["equity"],
        "fix_mult": (pos["equity"] / pos["base"]) if pos["base"] else 0.0,
        "cmp": pos["comp_equity"],
        "cmp_mult": pos["comp_mult"],
        "mult_ratio": (pos["comp_equity"] / pos["equity"]) if pos["equity"] else 0.0,
        "expo": pos["expo"],
        "expocap": cfg["strategy"]["exposure_cap"],
        "lev": int(float(cfg["binance"]["leverage"])),
        "liqline": 100.0 / float(cfg["binance"]["leverage"]),
        "regime_txt": regime_txt,
        "rule": "多头走现货 · 空头走合约 %d× 逐仓 · %s"
                % (int(float(cfg["binance"]["leverage"])), regime_txt),
        "closed_n": pos["closed_n"],
        "win_rate": pos["win_rate"] * 100.0,
        "pl_ratio": pos["pl_ratio"],
        "mdd_txt": "%.1f%%" % (pos["mdd"] * 100),
        "uw_txt": "%d 天" % pos["uw"],
        "adv_txt": "%.1f%%" % (pos["adverse"] * 100),
        "row": pos_row(pos, "当前信号（未参与）" if pending else "当前策略"),
        "detail": detail_card(pos, "混合载体 · 双均线 10/30", extra),
        "orders": orders_html, "warn": warn,
        "recent": recent,
    }


def send_mail(cfg, subject, html):
    em = cfg["email"]
    if not em["enabled"]:
        return False, "邮件未启用（email.enabled = false）"
    if not em["sender"] or not em["password"] or not em["to"]:
        return False, "邮件配置不完整（缺少 sender / password / to）"

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    label = em.get("strategy_label") or "BTC动量策略 · SMA50过滤多头 1×做空"
    msg["From"] = formataddr((str(Header(label, "utf-8")), em["sender"]))
    msg["To"] = ", ".join(em["to"])
    msg["Date"] = formatdate(localtime=True)

    last_err = None
    for attempt in range(1, int(em["max_retries"]) + 1):
        delivered = False
        try:
            ctx = ssl.create_default_context()
            if em["use_ssl"]:
                # local_hostname 必须给：本机主机名含中文会让 EHLO 以 ascii 编码失败
                s = smtplib.SMTP_SSL(em["server"], int(em["port"]), timeout=25,
                                     local_hostname="localhost", context=ctx)
            else:
                s = smtplib.SMTP(em["server"], int(em["port"]), timeout=25,
                                 local_hostname="localhost")
                s.starttls(context=ctx)
            s.login(em["sender"], em["password"])
            s.sendmail(em["sender"], em["to"], msg.as_string())
            delivered = True
            try:
                s.quit()
            except Exception:
                # 信已经交出去了，收尾断开失败不能算失败，更不能触发重发
                pass
            return True, "已发送至 " + ", ".join(em["to"])
        except Exception as e:
            if delivered:
                # sendmail 成功之后才出错（多半是服务器断连）→ 只补一句提示，绝不重发
                return True, "已发送至 " + ", ".join(em["to"]) + "（收尾断连，不影响收信）"
            last_err = "%s: %s" % (type(e).__name__, e)
            log("  邮件第 %d 次尝试失败：%s" % (attempt, last_err))
            if attempt < int(em["max_retries"]):
                time.sleep(3 * attempt)
    return False, "发送失败（已重试 %d 次）：%s" % (em["max_retries"], last_err)


def lock_dir():
    """本机公用的发信锁目录：同一台机器上所有策略副本都往这里写锁。"""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or tempfile.gettempdir())
    d = os.path.join(base, "BTC_Strategy")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return tempfile.gettempdir()


def claim_mail_slot(bar_date):
    """抢同一根日线的发信权（原子创建锁文件）。

    返回 (True, 锁文件路径, "") 表示抢到了，可以发。
    返回 (False, 路径, 占用者说明) 表示本机已经有一份策略副本发过这封邮件 ——
    这是「同一台机器上两份文件夹 / 两个计划任务同时跑」造成重复邮件的兜底。
    """
    path = os.path.join(lock_dir(), "sent_%s.lock" % bar_date)
    payload = "%s | %s | pid=%d" % (fdt(), os.path.abspath(__file__), os.getpid())
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                owner = f.read().strip()
        except Exception:
            owner = "（锁文件无法读取）"
        return False, path, owner
    except Exception:
        return True, None, ""          # 锁不可用时不阻断发信
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    # 顺手清掉 30 天前的旧锁，别让目录越堆越多
    try:
        cut = time.time() - 30 * 86400
        for fn in os.listdir(lock_dir()):
            fp = os.path.join(lock_dir(), fn)
            if fn.startswith("sent_") and os.path.getmtime(fp) < cut:
                os.remove(fp)
    except Exception:
        pass
    return True, path, ""


def release_mail_slot(path):
    """发信失败时把锁还回去，让下一次运行还能补发。"""
    if not path:
        return
    try:
        os.remove(path)
    except Exception:
        pass


def _ps(script):
    """跑一段 PowerShell，按 UTF-8 取回输出（避免中文乱码）。"""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
           "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=300)
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        return "[PowerShell 调用失败] %s: %s" % (type(e).__name__, e)


def doctor():
    """诊断「一次运行却收到两封邮件」：查计划任务、查策略副本、查发信锁。"""
    log("=" * 100)
    log(" 重复邮件诊断")
    log(" 时间：%s" % fdt())
    log(" 机器：%s    用户：%s" % (os.environ.get("COMPUTERNAME", "?"),
                                   os.environ.get("USERNAME", "?")))
    log(" 本目录：%s" % HERE)
    log("=" * 100)

    cfg = load_config()
    em = cfg["email"]
    st = cfg["strategy"]
    log("")
    log("【0】当前配置")
    log("      收件人：%s" % ", ".join(em["to"]))
    log("      always_mail=%s · mail_only_on_change=%s · 重试次数=%s"
        % (st.get("always_mail"), st.get("mail_only_on_change"), em.get("max_retries")))

    # ---------------------------------------------------------------- 计划任务
    log("")
    log("【1】本机计划任务（只看跑本策略的）")
    out = _ps(
        "$rows=@();"
        "Get-ScheduledTask | ForEach-Object {"
        "  $t=$_; $a=@(); foreach($x in $t.Actions){ $a += ($x.Execute + ' ' + $x.Arguments) };"
        "  $line=($a -join ' | ');"
        "  if($line -match '(?i)btc|silent'){"
        "    $i=Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath;"
        "    $rows += ('  * {0}{1}   状态={2}   上次运行={3}   返回码=0x{4:X}' -f $t.TaskPath,$t.TaskName,$t.State,$i.LastRunTime,$i.LastTaskResult);"
        "    $rows += ('      命令：' + $line)"
        "  }"
        "};"
        "if($rows.Count -eq 0){'  （没找到相关计划任务）'} else {$rows}"
    )
    for line in out.splitlines():
        log(line)
    n_task = sum(1 for ln in out.splitlines() if ln.strip().startswith("* "))

    # ---------------------------------------------------------------- 策略副本
    log("")
    log("【2】本机上的策略副本（找 btc_monitor.py）")
    out2 = _ps(
        "$roots=@($env:USERPROFILE,'D:\\','E:\\');"
        "foreach($r in $roots){ if(Test-Path $r){"
        "  Get-ChildItem -LiteralPath $r -Filter 'btc_monitor.py' -Recurse -File -Depth 8 -ErrorAction SilentlyContinue | ForEach-Object {"
        "    $d=$_.DirectoryName; $lg=Join-Path $d 'monitor_log.txt'; $last='(无日志)';"
        "    if(Test-Path $lg){"
        "      $m=Select-String -LiteralPath $lg -Pattern '20\\d\\d-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d' -AllMatches -ErrorAction SilentlyContinue | Select-Object -Last 1;"
        "      if($m){$last=$m.Matches[0].Value}"
        "    };"
        "    '  * ' + $d + '    最近一次运行=' + $last"
        "  }"
        "}}"
    )
    copies = []
    for line in out2.splitlines():
        if line.strip().startswith("* "):
            copies.append(line)
            log(line)
    if not copies:
        log("  （在用户目录下没找到其他副本；本目录之外的路径请手动确认）")
    n_copy = len(copies)

    # ---------------------------------------------------------------- 发信锁
    log("")
    log("【3】本机发信锁（%s）" % lock_dir())
    try:
        locks = sorted(fn for fn in os.listdir(lock_dir()) if fn.startswith("sent_"))
    except Exception:
        locks = []
    if not locks:
        log("      （还没有锁记录）")
    for fn in locks[-7:]:
        try:
            with open(os.path.join(lock_dir(), fn), "r", encoding="utf-8",
                      errors="replace") as f:
                body = f.read().strip()
        except Exception:
            body = "（读不到）"
        log("      %s  ←  %s" % (fn, body))

    # ---------------------------------------------------------------- 结论
    log("")
    log("【4】结论")
    if n_task > 1:
        log("      ✘ 本机有 %d 个计划任务在跑本策略 —— 每个任务都会发一封邮件，这就是重复的来源。" % n_task)
        log("        只留一个：任务计划程序里删掉多余的，或用「卸载定时任务.bat」删完再重新装一次。")
    elif n_task == 1:
        log("      · 本机只有 1 个计划任务，看起来正常。")
    if n_copy > 1:
        log("      ✘ 本机有 %d 份策略副本 —— 如果它们各自都装了计划任务，就会各发一封。" % n_copy)
        log("        留一份最新的，其余删掉（或至少删掉它们的计划任务）。")
    if n_task <= 1 and n_copy <= 1:
        log("      · 本机只有一份副本、一个任务。若仍然收到两封，第二封来自另一台机器")
        log("        （那台机器上的计划任务也要卸掉），或者来自邮箱侧的自动转发/分类。")
    log("")
    log("      已加机器级发信锁：同一根日线在这台机器上只允许发一封，")
    log("      即使两份副本同时跑也不会重复。锁目录见【3】。")
    log("=" * 100)


# ==============================================================================
# 5. 主流程
# ==============================================================================
def main():
    argv = sys.argv[1:]
    no_mail = "--no-mail" in argv
    live = "--live" in argv
    refresh = "--refresh" in argv
    reset = "--reset-state" in argv
    force_mail = "--force-mail" in argv
    json_out = None
    if "--json" in argv:
        try:
            json_out = argv[argv.index("--json") + 1]
        except IndexError:
            json_out = os.path.join(HERE, "monitor_result.json")
    html_out = None
    if "--dump-html" in argv:
        try:
            html_out = argv[argv.index("--dump-html") + 1]
        except IndexError:
            html_out = os.path.join(HERE, "monitor_mail_preview.html")

    cfg = load_config()
    st = cfg["strategy"]
    warnings = []

    if reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        log("已清空本地状态 monitor_state.json")

    state = load_state()
    # 兼容旧版 state：以前存的是 {"positions": {"a": ..., "b": ...}}，现在只留一套
    prev_pos = state.get("position") or (state.get("positions") or {}).get("a") or {}

    # 「从下一笔开始参与」：配置打开且还没开始 → 当前这笔不算你的，账户就是本金
    want_pending = bool(st.get("participate_from_next_trade", False))
    participating = bool(state.get("participating", not want_pending))
    pending = want_pending and not participating

    log("=" * 100)
    log(" BTC 日线趋势策略 · 每日检测")
    log(" 唯一方案：双均线 EMA 10/30 + SMA50 只约束多头 + 空头 %d× 逐仓"
        % int(float(cfg["binance"]["leverage"])))
    log("=" * 100)
    log("")

    # ---- 数据 ----
    log("[1/6] 拉取行情（只用当日已收盘的 K 线）")
    raw = load_bars(st["symbol"], force=refresh,
                    max_age_h=float(st.get("cache_max_age_hours", 1)))
    bars = closed_bars(raw)
    log("      原始 %d 根，剔除未收盘 %d 根，参与计算 %d 根"
        % (len(raw), len(raw) - len(bars), len(bars)))
    log("      最新已收盘：%s  收盘 %.2f USDT" % (fdate(bars[-1]["t"]), bars[-1]["close"]))
    log("")

    # ---- 信号 ----
    log("[2/6] 计算当前信号")
    rma = st.get("regime_ma", 0)
    fast, slow = st["primary_set"]
    sig = regime_long(bars, E.sig_ema_cross(bars, fast, slow), rma)
    log("      参数：双均线 EMA %d/%d" % (fast, slow))
    if rma:
        log("      趋势过滤：SMA%d 只约束多头（收盘价在均线下方时不开多、空头照做）" % int(rma))
    log("")

    # ---- 持仓与盈亏 ----
    log("[3/6] 推演当前持仓（成本价 / 现价 / 浮动盈亏）")
    pos = derive_position(bars, sig, cfg, "当前策略", pending=pending)
    _lev = float(cfg["binance"]["leverage"])
    log("      配置：多头现货 · 空头合约 %d× 逐仓（爆仓线 %+.1f%%）· %s"
        % (int(_lev), 100.0 / _lev,
           ("SMA%d 只约束多头" % int(rma)) if rma else "无均线过滤"))
    if pending:
        log("      参与状态：尚未开始 —— 当前这笔（%s 开的%s）你没有实际持仓，不计入账户"
            % (pos["entry_date"] or "—", sig_text(pos["dir"]) if pos["dir"] else "空仓"))
    log("      %s" % ("-" * 92))
    log("      " + E.row([("方案", 18, "left"), ("持仓", 6, "center"), ("成本价", 12, "right"),
                          ("现价", 12, "right"), ("浮动盈亏", 14, "right"),
                          ("收益率", 10, "right"), ("持有", 8, "right")]))
    log("      " + "-" * 92)
    if pos["dir"] == 0:
        log("      " + E.row([(pos["tag"], 18, "left"), ("空仓", 6, "center"),
                              ("-", 12, "right"), ("%.2f" % pos["cur_px"], 12, "right"),
                              ("-", 14, "right"), ("-", 10, "right"), ("-", 8, "right")]))
    else:
        log("      " + E.row([(pos["tag"], 18, "left"), (sig_text(pos["dir"]), 6, "center"),
                              ("%.2f" % pos["entry_px"], 12, "right"),
                              ("%.2f" % pos["cur_px"], 12, "right"),
                              (money(pos["upnl"]) + " U", 14, "right"),
                              (pct(pos["upnl_pct"]), 10, "right"),
                              ("%d 天" % pos["hold_days"], 8, "right")]))
    log("      " + "-" * 92)
    if pos["dir"] != 0:
        log("      %s：数量 %.6f BTC · 入场名义 %.2f U（敞口 %.2f×）· 市值 %.2f U"
            % (pos["tag"], pos["qty"], pos["notional"], pos["expo"], pos["mkt_value"]))
    if pos["pending"]:
        log("      %s：账户现值 %.2f U（%.2f 倍）—— 本金 %.0f U，当前这笔未参与，浮盈 %s 不计入"
            % (pos["tag"], pos["now_equity"], pos["now_mult"], pos["base"], money(pos["upnl"])))
    else:
        log("      %s：本金 %.0f + 浮盈 %s = 账户现值 %.2f U（%.2f 倍）"
            % (pos["tag"], pos["base"], money(pos["upnl"]), pos["now_equity"], pos["now_mult"]))
    log("      %s：回测全程参考 滚仓 %.0f U（%.1f 倍）· 固定本金 %.0f U（%.1f 倍）"
        % (pos["tag"], pos["comp_equity"], pos["comp_mult"],
           pos["equity"], pos["equity"] / pos["base"] if pos["base"] else 0.0))
    log("      %s：回测 %d 笔 · 胜率 %.0f%% · 盈亏比 %.1f · 最大回撤 %.1f%% · 最长水下 %d 天"
        % (pos["tag"], pos["closed_n"], pos["win_rate"] * 100, pos["pl_ratio"],
           pos["mdd"] * 100, pos["uw"]))
    log("")

    # ---- 变更检测 ----
    log("[4/6] 仓位变更检测")
    changes = []
    old = prev_pos.get("sig")
    if old is None:                           # 首次运行：与上一根 K 线比
        old = sig[-2] if len(sig) >= 2 else 0
    if pos["dir"] != old:
        changes.append("%s → %s" % (sig_text(old), sig_text(pos["dir"])))
        log("      ⚠ %s → %s" % (sig_text(old), sig_text(pos["dir"])))
    else:
        log("      · 维持 %s" % sig_text(pos["dir"]))
    if not changes:
        log("      仓位无变化")
    log("")

    # 一旦出现「新的仓位」（方向翻转且非空仓），就是用户进场的第一笔：
    # 从这一刻起开始按本金计入账户（此前那笔不算）。
    if pending and pos["dir"] != 0 and pos["dir"] != old:
        participating, pending = True, False
        pos["pending"] = False
        pos["now_equity"] = pos["base"] + pos["upnl"]
        pos["now_mult"] = pos["now_equity"] / pos["base"] if pos["base"] else 1.0
        log("      ▶ 从这笔开始参与：本金 %.0f USDT 已计入账户。" % pos["base"])
        log("")

    # ---- 风控 ----
    log("[5/6] 风控检查")
    sr = short_risk(bars, sig, cfg)
    if sr:
        log("      空头腿：入场 %s @ %.2f，期间最高 %.2f，最大逆行 %s，爆仓价 %.2f"
            % (sr["entry_date"], sr["entry_px"], sr["peak"], pct(sr["adverse"]), sr["liq_px"]))
        log("      距 %d× 爆仓线还有 %.2f pp%s"
            % (int(sr["lev"]), sr["margin"] * 100,
               "  ← 余量不足！" if sr["margin"] < 0.05 else ""))
        if sr["margin"] < 0.05:
            warnings.append("空头腿距爆仓线仅 %.2f pp，请检查杠杆是否超过 %d×。"
                            % (sr["margin"] * 100, int(sr["lev"])))
    else:
        log("      当前非空头状态，无需爆仓检查")
    log("")

    # ---- 下单计划 ----
    bnc = Binance(cfg)
    ref_price = bars[-1]["close"]
    orders, notes = build_order_plan(pos, cfg, bnc, ref_price)
    live_enabled = bool(cfg["binance"]["enabled"] and live)
    if live_enabled:
        log("[6/6] 真实下单已开启，执行中 ...")
        done = execute_plan(pos, cfg, bnc, ref_price, live)
        for d in done:
            log("      ✔ " + d)
        orders = orders + ["执行结果：" + d for d in done]
    else:
        log("[6/6] 执行计划（演练模式，不会真实下单）")
        for a in orders:
            log("      · " + a)
        for nt in notes:
            log("      " + nt)
    log("")

    res = {
        "cfg": cfg, "bars": bars, "bar_date": fdate(bars[-1]["t"]),
        "close": bars[-1]["close"], "position": pos, "changes": changes,
        "short_risk": sr, "orders": orders, "notes": notes,
        "warnings": warnings, "live_enabled": live_enabled,
    }

    # ---- 邮件 ----
    label = cfg["email"].get("strategy_label") or "BTC动量策略 · SMA50过滤多头 1×做空"
    if changes:
        subject = "[%s] %s 【仓位变更】%s" % (label, res["bar_date"], "；".join(changes))
    elif pending:
        subject = "[%s] %s 尚未参与 · 当前信号%s" % (
            label, res["bar_date"], sig_text(pos["dir"]))
    else:
        subject = "[%s] %s 持有%s · 仓位不变" % (label, res["bar_date"], sig_text(pos["dir"]))

    log("      标题：%s" % subject)

    mail_html = build_mail_html(res)
    if html_out:
        with open(html_out, "w", encoding="utf-8") as f:
            f.write(mail_html)
        log("      邮件正文已另存：%s" % html_out)

    # 同一根已收盘日线只发一封，两道闸：
    #   ① 本文件夹的 monitor_state.json（last_mailed_bar）
    #   ② 本机共用的锁文件 %LOCALAPPDATA%\BTC_Strategy\sent_<日期>.lock
    #      同一台机器上就算有两份策略副本、两个计划任务同时跑，也只有一份能发出去
    mailed_bar = state.get("last_mailed_bar")
    dup = (mailed_bar == res["bar_date"]) and not force_mail

    if no_mail:
        ok, info = False, "已跳过（--no-mail）"
    elif st["mail_only_on_change"] and not changes:
        ok, info = False, "已跳过（信号未变）"
    elif dup:
        ok, info = False, ("已跳过（%s 这根日线已经发过邮件，防重复；要强发请加 --force-mail）"
                           % res["bar_date"])
        log(("  ✔ " if ok else "  ✘ ") + info)
        log("  上次发信时间：%s" % (state.get("last_mail_at") or "—"))
    else:
        got, lock_path, owner = (True, None, "")
        if not force_mail:
            got, lock_path, owner = claim_mail_slot(res["bar_date"])
        if not got:
            ok, info = False, ("已跳过（%s 这根日线本机已有一份策略副本发过信）"
                               % res["bar_date"])
            log("  ✘ " + info)
            log("  占用者：%s" % owner)
            log("  想只留一份：删掉多余的文件夹，或删掉多余的计划任务。")
        else:
            log("正在发送邮件 ...")
            ok, info = send_mail(cfg, subject, mail_html)
            log(("  ✔ " if ok else "  ✘ ") + info)
            if ok:
                state["last_mailed_bar"] = res["bar_date"]
                state["last_mail_at"] = fdt()
            else:
                release_mail_slot(lock_path)

    # ---- 状态落盘 ----
    state["position"] = {"sig": pos["dir"], "entry_date": pos["entry_date"],
                         "entry_px": pos["entry_px"], "qty": pos["qty"]}
    state["participating"] = participating
    state.pop("positions", None)          # 清掉旧版的双方案结构
    state["last_run"] = fdt()
    state["last_bar"] = res["bar_date"]
    if changes:
        state.setdefault("alerts", []).append({"at": fdt(), "changes": changes})
        state["alerts"] = state["alerts"][-50:]
    save_state(state)

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 100 + "\n")
            f.write("运行时间：%s\n" % fdt())
            f.write("\n".join(LOG_BUF) + "\n")
    except Exception:
        pass

    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump({"run_at": fdt(), "bar_date": res["bar_date"], "close": res["close"],
                       "position": pos, "changes": changes,
                       "short_risk": sr, "orders": orders, "warnings": warnings,
                       "mail_ok": ok, "mail_info": info}, f, ensure_ascii=False, indent=2)
        log("结果已写入 %s" % json_out)

    log("")
    log("=" * 100)
    return 0


if __name__ == "__main__":
    if "--doctor" in sys.argv[1:]:
        # 重复邮件诊断：只读，不发信、不改状态
        try:
            doctor()
        except Exception:
            log(traceback.format_exc())
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 100 + "\n")
                f.write("诊断运行时间：%s\n" % fdt())
                f.write("\n".join(LOG_BUF) + "\n")
        except Exception:
            pass
        sys.exit(0)

    try:
        sys.exit(main())
    except Exception:
        err = traceback.format_exc()
        log("运行出错：")
        log(err)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("\n[ERROR] %s\n%s\n" % (fdt(), err))
        except Exception:
            pass
        sys.exit(1)
