# -*- coding: utf-8 -*-
"""
BTC 日线趋势策略 · 每日检测（邮件推送 + 可选币安下单）
============================================================

检测两个方案（口径完全一致，均为「多头走现货、空头走合约 3× 逐仓」）：

    方案① 混合载体 · 单参数 10/30
    方案③ 混合载体 · walk-forward 过去 5 年自动选参

每天在日线收盘后跑一次，输出：最新已收盘 K 线、两个方案的信号与目标持仓、
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

口径说明
--------
· 邮件/日志的主口径是【从现在起算】：本金就是 base_capital_usdt（默认 1000 U），
  账户现值 = 本金 + 当前浮盈。历史回测的已平仓盈亏**不计入**（那是回测数据，不是实盘）。
  仓位也按 base 算：名义 = base × 敞口。
· 同时给一行【回测全程参考】：若从 2017-08 就用 base 一路跟着做，
  固定本金（每笔按 base 开仓）终值约 9,386 U（9.4 倍），
  滚仓（每笔按当时权益开仓、利润滚入下一笔）终值约 104,689 U（104.7 倍）。
· 两者收益率完全相同，滚仓只是把绝对金额放大约 11 倍。不要混用。

安全设计
--------
· 下单默认「关闭 + 演练」：monitor_config.json 里 binance.enabled 默认 false，
  且必须命令行显式加 --live 才可能真实发单。任何一步不满足，都只打印计划不下单。
· 密钥缺失、网络失败、余额不足 → 一律降级为「只报告不下单」，绝不盲目下单。
· 持仓成本价按回测口径（信号翻转次日开盘成交）从历史推演，不依赖任何外部接口。
"""

import hashlib
import hmac
import json
import os
import smtplib
import ssl
import sys
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
        "strategy_label": "BTC动量策略 · 现货多 3×做空",
    },
    "binance": {
        "enabled": False, "dry_run": True, "use_testnet": True,
        "api_key": "", "api_secret": "",
        "spot_base": "https://api.binance.com",
        "futures_base": "https://fapi.binance.com",
        "testnet_spot_base": "https://testnet.binance.vision",
        "testnet_futures_base": "https://testnet.binancefuture.com",
        "symbol": "BTCUSDT", "leverage": 3, "margin_type": "ISOLATED",
        "min_notional_usdt": 20.0,
    },
    "strategy": {
        "symbol": "BTCUSDT", "vt": 0.03, "exposure_cap": 1.0, "atr_n": 14,
        "warmup": 60, "walk_forward_years": 5, "cache_max_age_hours": 1,
        "base_capital_usdt": 1000.0,
        "param_sets": [[8, 24], [10, 30], [12, 36], [15, 45], [20, 60]],
        "primary_set": [10, 30],
        "always_mail": True, "mail_only_on_change": False,
    },
}

# 邮件配色（涨红跌绿，A 股/国内习惯）
C_UP = "#c0392b"      # 红 = 盈利
C_DOWN = "#1e8449"    # 绿 = 亏损
C_MUTED = "#66788a"


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
    return {"version": 1, "positions": {}, "last_run": None, "last_bar": None, "alerts": []}


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
def build_curves(bars, sets, cfg):
    """对每组参数跑一遍混合载体回测，返回 {参数组: 净值曲线}。"""
    st = cfg["strategy"]
    curves = {}
    for k in sets:
        sg = E.sig_ema_cross(bars, k[0], k[1])
        curves[k] = E.backtest_hybrid(
            bars, sg, E.CAPITAL, L=float(cfg["binance"]["leverage"]),
            exposure_cap=st["exposure_cap"], vt=st["vt"],
            atr_n=st["atr_n"], warmup=st["warmup"],
        )["equity"]
    return curves


def walk_forward_picks(bars, sets, cfg):
    """
    方案③ 的核心：每年年初只用「截至当时的过去 N 年」数据挑参数，持有一年。
    返回 picks = [(年份, 参数组), ...]，最后一项就是今年在用的参数组。
    """
    warmup = cfg["strategy"]["warmup"]
    curves = build_curves(bars, sets, cfg)
    rets = dict((k, E.daily_returns(v)) for k, v in curves.items())
    ymap = E.yearly_map(bars)
    _, picks = E.walk_forward_equity(
        rets, sets, ymap, warmup, len(bars), cfg["strategy"]["walk_forward_years"])
    return picks, curves, rets


def exposure_at(bars, cfg, i):
    """第 i 根 K 线执行时的敞口倍数 = min(上限, 波动率目标 / 前一日波动率)。"""
    st = cfg["strategy"]
    a = E.atr(bars, st["atr_n"])
    j = max(i - 1, 0)
    dvol = a[j] / bars[j]["close"] if a[j] == a[j] else 0.05    # NaN 自检
    return (min(st["exposure_cap"], st["vt"] / dvol) if dvol > 0 else st["exposure_cap"]), dvol


def derive_position(bars, sig, cfg, tag):
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
    res = E.backtest_hybrid(bars, sig, E.CAPITAL, L=lev,
                            exposure_cap=st["exposure_cap"], vt=st["vt"],
                            atr_n=st["atr_n"], warmup=st["warmup"])
    eq = res["equity"]
    n = len(bars)
    cur_sig = sig[-1]
    cur_px = bars[-1]["close"]
    comp_scale = (base / E.CAPITAL) if E.CAPITAL else 1.0

    # ---- 历史已平仓交易 ----
    realized, closed_n = 0.0, 0
    recent = []
    for t in res["trades"]:
        j = t["entry_i"] - 1
        e_ref = eq[j] if (0 <= j < n and eq[j]) else E.CAPITAL
        scale = base / e_ref if e_ref else 0.0
        pnl = t["pnl"] * scale
        realized += pnl
        closed_n += 1
        recent.append({
            "entry_date": fdate(bars[t["entry_i"]]["t"]),
            "exit_date": fdate(bars[t["exit_i"]]["t"]),
            "dir": t["dir"], "entry_px": t["entry_px"], "exit_px": t["exit_px"],
            "pnl": pnl, "comp_pnl": t["pnl"] * comp_scale,
            "liq": bool(t.get("liq")), "leg": t.get("leg"),
        })

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
        "equity": base + realized,
        # 主口径：从现在起算（历史已平仓不计入，账户 = 本金 + 当前浮盈）
        "now_equity": base, "now_mult": 1.0,
        # 回测全程参考
        "comp_realized": comp_equity - base, "comp_equity": comp_equity,
        "comp_qty": 0.0, "comp_notional": 0.0, "comp_mkt_value": 0.0,
        "comp_upnl": 0.0, "comp_mult": (comp_equity / base) if base else 0.0,
        "leg": "现货" if cur_sig > 0 else ("合约 3× 逐仓" if cur_sig < 0 else "—"),
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
        # 主口径：从现在起算，历史已平仓不计入
        "now_equity": base + upnl, "now_mult": (base + upnl) / base if base else 0.0,
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
    空头腿风控：3× 逐仓的爆仓线在 入场价 ×(1+1/L)。
    用本轮空头区间的最高价估算最大逆行与安全边际。
    """
    lev = float(cfg["binance"]["leverage"])
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
        if fut["amt"] == 0:
            actions.append("设置 %s 为逐仓 %d×" % (symbol, b["leverage"]))
            actions.append("开合约空头：市价卖出 %.3f %s（逐仓 %d×）"
                           % (target_qty, symbol, b["leverage"]))
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
            r = binance.futures_market_order(symbol, "SELL", target_qty)
            done.append("已开空 %.3f（订单 %s）" % (target_qty, r.get("orderId")))
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
    pos = res["positions"]           # [方案①, 方案③]
    changes = res["changes"]         # 发生变化的方案名列表

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

    # ---------- 顶部横幅：变更 / 不变 ----------
    if changes:
        detail = "；".join(changes)
        banner = ('<div class="banner" style="background:%s;border-radius:10px;padding:15px 16px;'
                  'margin-bottom:14px">'
                  '<div style="color:#fff;font-size:17px;font-weight:800;letter-spacing:.3px">'
                  '【仓位变更提醒】请及时操作</div>'
                  '<div style="color:#ffe6e2;font-size:13px;margin-top:7px;line-height:1.65">%s</div></div>'
                  % (C_UP, detail))
    else:
        hold = "、".join("%s %s" % (p["tag"], sig_text(p["dir"])) for p in pos)
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
                '本金 %s + 浮盈 %s</div></td></tr>'
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
                   format(p["base"], ",.0f"), money(p["upnl"]),
                   C_MUTED, p["comp_equity"], p["comp_mult"],
                   p["equity"], p["equity"] / p["base"] if p["base"] else 0.0,
                   extra))

    extra_a = ""
    if pos[0]["dir"] < 0 and res["short_risk"]:
        r = res["short_risk"]
        extra_a = ('<tr><td style="padding:4px 0;color:%s">爆仓价 / 安全余量</td>'
                   '<td style="padding:4px 0">%.2f USDT / <b>%.2f pp</b></td></tr>'
                   % (C_MUTED, r["liq_px"], r["margin"] * 100))

    extra_b = ('<tr><td style="padding:4px 0;color:%s">当前选用参数</td>'
               '<td style="padding:4px 0">%d/%d（walk-forward 自动选择）</td></tr>'
               % (C_MUTED, res["pick"][0], res["pick"][1]))

    # ---------- 最近平仓（两个方案各一张）----------
    ymap = dict(res["picks"])          # {年份: (快, 慢)}，方案③ 用它标出每笔交易当时用的参数

    def recent_table(p, title, show_param=False):
        rec = p["recent"][-3:]
        if not rec:
            return ""
        rows = ""
        for t in rec:
            sub = ""
            if show_param:
                k = ymap.get(int(t["entry_date"][:4]))
                if k:
                    sub = ('<div style="color:#8a9aab;font-size:11px;margin-top:2px;font-weight:400">'
                           '%d/%d</div>' % (k[0], k[1]))
            rows += ('<tr><td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px">'
                     '%s → %s<div style="color:#8a9aab;font-size:11.5px">%.2f → %.2f</div></td>'
                     '<td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px;'
                     'text-align:center;color:%s">%s%s</td>'
                     '<td style="padding:8px 4px;border-bottom:1px solid #eef1f5;font-size:12.5px;'
                     'text-align:right;white-space:nowrap">%s</td></tr>'
                     % (t["entry_date"], t["exit_date"], t["entry_px"], t["exit_px"],
                        sig_color(t["dir"]), sig_text(t["dir"]), sub, _money_cell(t["pnl"])))
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

    recent_a = recent_table(pos[0], "方案① 最近 3 笔已平仓")
    recent_b = recent_table(pos[1], "方案③ 最近 3 笔已平仓", show_param=True)

    # ---------- 方案③ 选参记录 ----------
    picks_html = ""
    if res["picks"]:
        rows = "".join(
            '<tr><td style="padding:7px 6px;border-bottom:1px solid #eef1f5;font-size:13px">%d 年</td>'
            '<td style="padding:7px 6px;border-bottom:1px solid #eef1f5;font-size:13px;text-align:center;'
            '%s">%d/%d</td></tr>'
            % (y, 'font-weight:700;color:%s' % C_UP if (y, k) == res["picks"][-1] else "",
               k[0], k[1]) for y, k in res["picks"][-6:])
        picks_html = ('<div style="background:#fff;border-radius:12px;padding:14px 16px;margin-top:12px">'
                      '<div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:8px">'
                      '方案③ 最近 6 年自动选参'
                      '<span style="font-weight:400;font-size:12px;color:#8a9aab">'
                      '（每年只用此前数据挑，上面「最近 3 笔」的方向列即当年参数）</span></div>'
                      '<table role="presentation" style="width:100%%;border-collapse:collapse">'
                      '<tr><th style="padding:6px;text-align:left;font-size:12.5px;color:%s">年份</th>'
                      '<th style="padding:6px;text-align:center;font-size:12.5px;color:%s">选中参数</th></tr>'
                      '%s</table></div>' % (C_MUTED, C_MUTED, rows))

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
    多头走现货 · 空头走合约 3× 逐仓 · 生成时间 %(gen)s</div>
</div>

<div class="card">
  <div style="font-size:14px;font-weight:700;color:#1c2833;margin-bottom:10px">持仓总览</div>
  <table class="tbl" role="presentation">
    <colgroup><col style="width:21%%"><col style="width:13%%"><col style="width:22%%">
      <col style="width:22%%"><col style="width:22%%"></colgroup>
    <tr><th style="text-align:left">方案</th><th style="text-align:center">持仓</th>
        <th style="text-align:right">成本价</th><th style="text-align:right">现价</th>
        <th style="text-align:right">浮动盈亏</th></tr>
    %(row_a)s
    %(row_b)s
  </table>
  <div style="color:#8a9aab;font-size:11.5px;margin-top:9px;line-height:1.65">
    <b>①</b> = 单参数 10/30 &nbsp;·&nbsp; <b>③</b> = walk-forward 过去 5 年（今年选中 %(pick)s）<br>
    成本价 = 信号翻转后次日开盘成交价（含滑点）；名义 = %(base).0f USDT × 敞口比例；
    收益率 = 浮盈 ÷ 入场名义。盈亏均已扣手续费 / 资金费。金额单位 USDT。
  </div>
</div>

%(detail_a)s
%(recent_a)s
%(detail_b)s
%(recent_b)s
%(picks)s
%(orders)s
%(warn)s

<div style="color:#8a9aab;font-size:11.5px;margin-top:20px;line-height:1.75">
  <b>关于「账户现值」：</b><br>
  · <b>本邮件采用「从现在起算」</b>：本金就是 <b>%(base).0f USDT</b>，
  账户现值 = %(base).0f 本金 + 当前浮盈，当前 <b>%(now).2f USDT（%(now_mult).2f 倍）</b>。
  历史回测里那 92 笔已平仓盈亏**不计入**这个数字 —— 那是回测数据，不是你的实盘。
  仓位也按 %(base).0f U 算：名义 = %(base).0f × 敞口 %(expo).2f× = <b>%(notional).2f USDT</b>。<br>
  · <b>「回测全程参考」</b>（仅供了解这套策略长期跑下来什么水平）：
  若从 2017-08 就用 %(base).0f U 一路跟着做 ——
  <b>固定本金</b>（每笔都按 %(base).0f U 开仓）终值 %(fix).0f USDT（%(fix_mult).1f 倍）；
  <b>滚仓</b>（每笔按当时账户权益开仓、利润滚入下一笔）终值 %(cmp).0f USDT（%(cmp_mult).1f 倍）。
  两者收益率完全相同，滚仓只是把绝对金额放大了 %(mult_ratio).1f 倍。<br>
  · <b>往后的滚仓</b>：如果你要滚仓，就每笔按「当时账户权益 × 敞口 %(expo).2f×」下单，
  赚了自动放大下一笔。滚仓收益高得多，但回撤与爆仓风险同步放大，中途要能扛住 -50%% 回撤才拿得住，
  亏损期也会加速缩水。<b>建议先按固定 %(base).0f U 跑熟再考虑滚仓。</b><br><br>
  仓位规则：波动率目标 3%%/日，敞口上限 1.0×；不设固定止盈止损，靠信号反转出场。<br>
  历史参考：2017-08 至今胜率约 37%%，盈亏比 2.8，最大回撤 -40%%~-50%%，最长水下 1~2 年，会连续亏 7 笔。<br>
  空头腿固定 3× 逐仓（爆仓线 -33.3%%，历史最深逆行 -22.9%%）；多头腿不上杠杆。<br>
  <b>本邮件是回测规则的机械信号，不构成投资建议。</b>加密资产波动极大，请自行判断并控制风险。
</div>

</div></body></html>""" % {
        "banner": banner, "muted": C_MUTED, "sym": sym,
        "bar": res["bar_date"], "close": res["close"], "gen": fdt(),
        "base": pos[0]["base"],
        "now": pos[0]["now_equity"],
        "now_mult": pos[0]["now_mult"],
        "notional": pos[0]["notional"],
        "fix": pos[0]["equity"],
        "fix_mult": (pos[0]["equity"] / pos[0]["base"]) if pos[0]["base"] else 0.0,
        "cmp": pos[0]["comp_equity"],
        "cmp_mult": pos[0]["comp_mult"],
        "mult_ratio": (pos[0]["comp_equity"] / pos[0]["equity"]) if pos[0]["equity"] else 0.0,
        "expo": pos[0]["expo"],
        "row_a": pos_row(pos[0], "① 10/30"),
        "row_b": pos_row(pos[1], "③ WF5年"),
        "pick": "%d/%d" % res["pick"],
        "detail_a": detail_card(pos[0], "方案① 混合载体 · 单参数 10/30", extra_a),
        "detail_b": detail_card(pos[1], "方案③ 混合载体 · walk-forward 5 年", extra_b),
        "orders": orders_html, "warn": warn,
        "recent_a": recent_a, "recent_b": recent_b, "picks": picks_html,
    }


def send_mail(cfg, subject, html):
    em = cfg["email"]
    if not em["enabled"]:
        return False, "邮件未启用（email.enabled = false）"
    if not em["sender"] or not em["password"] or not em["to"]:
        return False, "邮件配置不完整（缺少 sender / password / to）"

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    label = em.get("strategy_label") or "BTC动量策略 · 现货多 3×做空"
    msg["From"] = formataddr((str(Header(label, "utf-8")), em["sender"]))
    msg["To"] = ", ".join(em["to"])
    msg["Date"] = formatdate(localtime=True)

    last_err = None
    for attempt in range(1, int(em["max_retries"]) + 1):
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
            s.quit()
            return True, "已发送至 " + ", ".join(em["to"])
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            log("  邮件第 %d 次尝试失败：%s" % (attempt, last_err))
            if attempt < int(em["max_retries"]):
                time.sleep(3 * attempt)
    return False, "发送失败（已重试 %d 次）：%s" % (em["max_retries"], last_err)


# ==============================================================================
# 5. 主流程
# ==============================================================================
def main():
    argv = sys.argv[1:]
    no_mail = "--no-mail" in argv
    live = "--live" in argv
    refresh = "--refresh" in argv
    reset = "--reset-state" in argv
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
    prev_pos = state.get("positions", {})

    log("=" * 100)
    log(" BTC 日线趋势策略 · 每日检测")
    log(" 方案① 混合载体 · 单参数 10/30    |    方案③ 混合载体 · walk-forward 过去 5 年")
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
    log("[2/6] 计算两个方案的当前信号")
    sig_a = E.sig_ema_cross(bars, *st["primary_set"])
    sets = [tuple(x) for x in st["param_sets"]]
    picks, curves, rets = walk_forward_picks(bars, sets, cfg)
    cur_pick = picks[-1][1]
    sig_b = E.sig_ema_cross(bars, *cur_pick)
    log("      方案③ 今年选中的参数：%d/%d" % cur_pick)
    log("")

    # ---- 持仓与盈亏 ----
    log("[3/6] 推演当前持仓（成本价 / 现价 / 浮动盈亏）")
    pos_a = derive_position(bars, sig_a, cfg, "① 单参数 10/30")
    pos_b = derive_position(bars, sig_b, cfg, "③ WF 5年 %d/%d" % cur_pick)
    log("      %s" % ("-" * 92))
    log("      " + E.row([("方案", 18, "left"), ("持仓", 6, "center"), ("成本价", 12, "right"),
                          ("现价", 12, "right"), ("浮动盈亏", 14, "right"),
                          ("收益率", 10, "right"), ("持有", 8, "right")]))
    log("      " + "-" * 92)
    for p in (pos_a, pos_b):
        if p["dir"] == 0:
            log("      " + E.row([(p["tag"], 18, "left"), ("空仓", 6, "center"),
                                  ("-", 12, "right"), ("%.2f" % p["cur_px"], 12, "right"),
                                  ("-", 14, "right"), ("-", 10, "right"), ("-", 8, "right")]))
        else:
            log("      " + E.row([(p["tag"], 18, "left"), (sig_text(p["dir"]), 6, "center"),
                                  ("%.2f" % p["entry_px"], 12, "right"),
                                  ("%.2f" % p["cur_px"], 12, "right"),
                                  (money(p["upnl"]) + " U", 14, "right"),
                                  (pct(p["upnl_pct"]), 10, "right"),
                                  ("%d 天" % p["hold_days"], 8, "right")]))
    log("      " + "-" * 92)
    for p in (pos_a, pos_b):
        if p["dir"] != 0:
            log("      %s：数量 %.6f BTC · 入场名义 %.2f U（敞口 %.2f×）· 市值 %.2f U"
                % (p["tag"], p["qty"], p["notional"], p["expo"], p["mkt_value"]))
        log("      %s：本金 %.0f + 浮盈 %s = 账户现值 %.2f U（%.2f 倍）"
            % (p["tag"], p["base"], money(p["upnl"]), p["now_equity"], p["now_mult"]))
        log("      %s：回测全程参考 滚仓 %.0f U（%.1f 倍）· 固定本金 %.0f U（%.1f 倍）"
            % (p["tag"], p["comp_equity"], p["comp_mult"],
               p["equity"], p["equity"] / p["base"] if p["base"] else 0.0))
    log("")

    # ---- 变更检测 ----
    log("[4/6] 仓位变更检测")
    changes = []
    for key, p, ptag, sg in (("a", pos_a, "方案① 单参数 10/30", sig_a),
                             ("b", pos_b, "方案③ walk-forward 5 年", sig_b)):
        old = prev_pos.get(key, {}).get("sig")
        if old is None:                       # 首次运行：与上一根 K 线比
            old = sg[-2] if len(sg) >= 2 else 0
        if p["dir"] != old:
            changes.append("%s：%s → %s" % (ptag, sig_text(old), sig_text(p["dir"])))
            log("      ⚠ %s：%s → %s" % (ptag, sig_text(old), sig_text(p["dir"])))
        else:
            log("      · %s：维持 %s" % (ptag, sig_text(p["dir"])))
    if not changes:
        log("      两个方案仓位均无变化")
    log("")

    # ---- 风控 ----
    log("[5/6] 风控检查")
    sr = short_risk(bars, sig_a, cfg)
    if sr:
        log("      空头腿：入场 %s @ %.2f，期间最高 %.2f，最大逆行 %s，爆仓价 %.2f"
            % (sr["entry_date"], sr["entry_px"], sr["peak"], pct(sr["adverse"]), sr["liq_px"]))
        log("      距 3× 爆仓线还有 %.2f pp%s"
            % (sr["margin"] * 100, "  ← 余量不足！" if sr["margin"] < 0.05 else ""))
        if sr["margin"] < 0.05:
            warnings.append("空头腿距爆仓线仅 %.2f pp，请检查杠杆是否超过 3×。" % (sr["margin"] * 100))
    else:
        log("      当前非空头状态，无需爆仓检查")
    if pos_a["dir"] != pos_b["dir"]:
        warnings.append("两个方案信号不一致（① %s / ③ %s），建议以方案③ 为准或对半分仓。"
                        % (sig_text(pos_a["dir"]), sig_text(pos_b["dir"])))
    log("")

    # ---- 下单计划 ----
    bnc = Binance(cfg)
    ref_price = bars[-1]["close"]
    orders, notes = build_order_plan(pos_b, cfg, bnc, ref_price)
    live_enabled = bool(cfg["binance"]["enabled"] and live)
    if live_enabled:
        log("[6/6] 真实下单已开启，执行中 ...")
        done = execute_plan(pos_b, cfg, bnc, ref_price, live)
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
        "close": bars[-1]["close"], "pick": cur_pick, "picks": picks,
        "positions": [pos_a, pos_b], "changes": changes,
        "short_risk": sr, "orders": orders, "notes": notes,
        "warnings": warnings, "live_enabled": live_enabled,
    }

    # ---- 邮件 ----
    label = cfg["email"].get("strategy_label") or "BTC动量策略 · 现货多 3×做空"
    if changes:
        subject = "[%s] %s 【仓位变更】%s" % (
            label, res["bar_date"],
            "；".join(c.split("：", 1)[1] for c in changes))
    else:
        hold = "、".join(dict.fromkeys(sig_text(p["dir"]) for p in (pos_a, pos_b)))
        subject = "[%s] %s 持有%s · 仓位不变" % (label, res["bar_date"], hold)

    log("      标题：%s" % subject)

    mail_html = build_mail_html(res)
    if html_out:
        with open(html_out, "w", encoding="utf-8") as f:
            f.write(mail_html)
        log("      邮件正文已另存：%s" % html_out)

    if no_mail:
        ok, info = False, "已跳过（--no-mail）"
    elif st["mail_only_on_change"] and not changes:
        ok, info = False, "已跳过（信号未变）"
    else:
        log("正在发送邮件 ...")
        ok, info = send_mail(cfg, subject, mail_html)
        log(("  ✔ " if ok else "  ✘ ") + info)

    # ---- 状态落盘 ----
    state["positions"] = {
        "a": {"sig": pos_a["dir"], "entry_date": pos_a["entry_date"],
              "entry_px": pos_a["entry_px"], "qty": pos_a["qty"]},
        "b": {"sig": pos_b["dir"], "entry_date": pos_b["entry_date"],
              "entry_px": pos_b["entry_px"], "qty": pos_b["qty"]},
    }
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
                       "pick": cur_pick, "positions": [pos_a, pos_b], "changes": changes,
                       "short_risk": sr, "orders": orders, "warnings": warnings,
                       "mail_ok": ok, "mail_info": info}, f, ensure_ascii=False, indent=2)
        log("结果已写入 %s" % json_out)

    log("")
    log("=" * 100)
    return 0


if __name__ == "__main__":
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
