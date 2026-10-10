# -*- coding: utf-8 -*-
"""
common.py — A/B/C 杠杆合约实盘信号推送 · 共享库

设计要点（与回测口径严格一致）：
  · 策略：5 组双均线参数集成（8/24、10/30、12/36、15/45、20/60），多空双向
  · 杠杆：3× 逐仓；仓位由「目标日波动 ÷ 当前日波动」反推（波动率目标仓位）
  · 只使用【已收盘】K线，绝不使用未完成K线（否则信号会漂移）
  · 只在【方向发生变化】时推送邮件，首次运行只发一封「初始化」说明信

运行环境：Python 3.8+，仅用标准库，无需 pip 安装任何依赖。
"""
import json
import logging
import math
import os
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "signal_state.json")
LOG_DIR = os.path.join(HERE, "logs")

API_BASE = "https://data-api.binance.vision/api/v3/klines"
CST = timezone(timedelta(hours=8))          # 北京时间
MMR = 0.005                                  # 维持保证金率（与回测一致）

# ============================================================ 邮件标题
# 所有邮件的标题后缀。想改标题，只改这一行即可（例如改成 "·5倍合约"）。
SUBJECT_SUFFIX = "·3倍合约"


# ============================================================ 日志
def setup_logger(tag="live"):
    os.makedirs(LOG_DIR, exist_ok=True)
    log = logging.getLogger(tag)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"{tag}_{datetime.now(CST):%Y%m%d}.log"),
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


# ============================================================ 配置
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 允许用环境变量覆盖密码，避免把授权码明文留在文件里
    env_pwd = os.environ.get("SIGNAL_EMAIL_PASSWORD")
    if env_pwd:
        cfg["email"]["password"] = env_pwd
    # 币安 API 密钥同样支持环境变量覆盖（更安全，推荐）
    tr = cfg.get("trade") or {}
    if os.environ.get("BINANCE_API_KEY"):
        tr["apiKey"] = os.environ["BINANCE_API_KEY"]
    if os.environ.get("BINANCE_API_SECRET"):
        tr["apiSecret"] = os.environ["BINANCE_API_SECRET"]
    if tr:
        cfg["trade"] = tr
    return cfg


def master_switch(cfg=None):
    """读取「总开关」。返回 dict(mode, live, real_ok, reason)。

    mode:  "observe" 只推送不下单（默认，观察期）
           "live"    真金白银自动下单
    兼容旧的 trade.enabled 写法：若没有 _master_switch，则退回看 trade.enabled。
    """
    cfg = cfg or load_config()
    ms = cfg.get("_master_switch") or {}
    tr = cfg.get("trade") or {}

    if ms:
        mode = str(ms.get("mode", "observe")).strip().lower()
        confirm = bool(ms.get("i_understand_real_money", False))
    else:
        # 老配置回退：以 trade.enabled 为准
        mode = "live" if tr.get("enabled") else "observe"
        confirm = True

    live = (mode == "live")
    real_ok = live and confirm
    if not live:
        reason = "总开关 mode=observe（只推送，不下单）"
    elif not confirm:
        reason = "mode=live 但 i_understand_real_money=false（双保险未放行）"
    else:
        reason = "mode=live 且已确认（真实下单已放行）"
    return {"mode": mode, "live": live, "real_ok": real_ok, "reason": reason}


# ============================================================ 数据
def fetch_klines(symbol, interval="1d", limit=1000, retries=3, log=None):
    """从币安公开镜像拉取K线。失败自动重试。"""
    url = f"{API_BASE}?symbol={symbol}&interval={interval}&limit={limit}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signal-bot/1.0"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"返回内容异常: {str(raw)[:200]}")
            bars = [{
                "open_time": int(k[0]), "close_time": int(k[6]),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            } for k in raw]
            return bars
        except Exception as e:                                   # noqa: BLE001
            last_err = e
            if log:
                log.warning("拉取K线失败（第 %d/%d 次）：%s", attempt, retries, e)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"拉取K线失败（已重试 {retries} 次）：{last_err}")


def keep_closed(bars, now_ms=None):
    """只保留【已收盘】的K线——这是实盘信号正确性的第一道防线。"""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return [b for b in bars if b["close_time"] <= now_ms]


# ============================================================ 指标（与回测逐行一致）
def ema(vals, n):
    """标准 EMA，用前 n 个值的 SMA 播种（与 TA-Lib / 回测一致）。"""
    out = [float("nan")] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1.0)
    seed = sum(vals[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(bars, n=14):
    """Wilder ATR。"""
    trs = [float("nan")] * len(bars)
    for i, b in enumerate(bars):
        if i == 0:
            trs[i] = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            trs[i] = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
    out = [float("nan")] * len(bars)
    if len(bars) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ============================================================ 信号与仓位
def compute_profile(profile, bars, cfg, symbol=None, equity_override=None):
    """算出某档位（A/B/C）在最后一根已收盘K线上的目标仓位。

    symbol          : 交易对（多标的模式下必须传，用于显示与状态键）
    equity_override : 覆盖本金。多标的模式下传「账户总权益 ÷ 币数」，
                      这样每个币的 sleeve 规模自动跟随账户（动态再平衡）。
    """
    closes = [b["close"] for b in bars]
    atr_v = atr(bars, cfg["atr_period"])
    last = bars[-1]
    px = last["close"]
    a = atr_v[-1]
    if math.isnan(a) or a <= 0:
        raise RuntimeError("ATR 计算失败，数据可能不足")
    dvol = a / px

    vt = float(profile["vol_target"])
    cap = float(cfg["exposure_cap"])
    expo = min(cap, vt / dvol) if dvol > 0 else 0.0

    L = float(cfg["leverage"])
    params = cfg["params"]
    equity = float(equity_override) if equity_override else float(profile["equity"])
    sleeve_eq = equity / len(params)

    sleeves = []
    for (f, s) in params:
        ef = ema(closes, f)[-1]
        es = ema(closes, s)[-1]
        if math.isnan(ef) or math.isnan(es):
            raise RuntimeError(f"数据不足，无法计算 EMA{f}/{s}（需要更多历史K线）")
        d = 1 if ef > es else -1
        notion = sleeve_eq * expo
        sleeves.append({"fast": f, "slow": s, "dir": d, "notional": notion,
                        "ema_fast": ef, "ema_slow": es})

    net = sum(sv["dir"] * sv["notional"] for sv in sleeves)      # 有符号名义
    gross = sum(sv["notional"] for sv in sleeves)
    net_expo = net / equity if equity else 0.0
    qty = abs(net) / px
    margin = abs(net) / L

    if net > 1e-9:
        liq = px * (1.0 - 1.0 / L + MMR)
        side = "多"
    elif net < -1e-9:
        liq = px * (1.0 + 1.0 / L - MMR)
        side = "空"
    else:
        liq, side = px, "持平"

    return {
        "key": None, "name": profile["name"], "vt": vt, "equity": equity,
        "symbol": symbol or cfg.get("symbol"), "px": px, "atr": a, "dvol": dvol,
        "expo": expo, "L": L,
        "sleeves": sleeves, "net": net, "gross": gross, "net_expo": net_expo,
        "qty": qty, "margin": margin, "liq": liq, "side": side,
        "bar_dt": datetime.fromtimestamp(last["open_time"] / 1000, CST),
        "bar_close": datetime.fromtimestamp(last["close_time"] / 1000, CST),
        "bar_open_ms": int(last["open_time"]),
        "dirs": [sv["dir"] for sv in sleeves],
    }


# ============================================================ 风控位（只提醒，不改策略）
def compute_risk_levels(inf, cfg, entry_px=None, peak_px=None):
    """算出止损/止盈/爆仓提醒位。纯提醒用途，不改变策略逻辑，也不会自动下单。"""
    risk = cfg.get("risk", {}) or {}
    a, px, L = inf["atr"], inf["px"], inf["L"]
    is_long = inf["net"] > 0
    base = float(entry_px) if entry_px else px      # 开仓基准价
    pk = float(peak_px) if peak_px else px          # 持仓期间最高/最低价（吊灯用）

    lv = {"base": base, "peak": pk, "liq": inf["liq"],
          "liq_dist": abs(inf["liq"] / px - 1.0), "stop": None,
          "stop_kind": "不设价格止损", "tp": None, "is_long": is_long}

    mode = risk.get("stop_mode", "none")
    if mode == "trail_atr":
        k = float(risk.get("trail_atr", 5.0))
        lv["stop"] = (pk - k * a) if is_long else (pk + k * a)
        lv["stop_kind"] = f"移动止损（吊灯 {k:g}×ATR）"
    elif mode == "fixed_pct":
        p = float(risk.get("fixed_stop_pct", 0.10))
        lv["stop"] = base * (1 - p) if is_long else base * (1 + p)
        lv["stop_kind"] = f"固定止损 {p * 100:g}%"

    tp = float(risk.get("take_profit_pct", 0.0) or 0.0)
    if tp > 0:
        lv["tp"] = base * (1 + tp) if is_long else base * (1 - tp)
    return lv


# ============================================================ 状态
def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                            # noqa: BLE001
        return {}


def save_state(state):
    """原子写入，避免断电/中断把状态文件写坏。"""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ============================================================ 多标的辅助
def symbols_of(cfg, override=None):
    """返回要运行的交易对列表。**这是「仅 BTC / 四币合一」的唯一开关**。

    优先级（从高到低）：
      1. override 参数（来自命令行 --symbols A,B,C）—— 临时覆盖，不动配置
      2. config 顶层 run_mode：
           "single" → 只用 config.symbol（老行为，仅 BTC）
           "multi"  → 用 config.symbols 里的全部（四币合一）
           未填/其它 → 按 symbols 长度自动判断（向后兼容老配置）
      3. config.symbols 长度 ≥ 2 → 多标的；否则单标的

    返回的列表长度 ≥ 2 即视为多标的等权模式。
    """
    if override:
        clean = [str(s).strip().upper() for s in override if str(s).strip()]
        if clean:
            return clean

    syms = cfg.get("symbols")
    clean = ([str(s).strip().upper() for s in syms if str(s).strip()]
             if isinstance(syms, list) else [])
    single = [str(cfg.get("symbol") or "BTCUSDT").strip().upper()]

    mode = str(cfg.get("run_mode") or "").strip().lower()
    if mode == "single":
        return single
    if mode == "multi":
        return clean if len(clean) >= 2 else single
    # 未指定 run_mode：按数组长度自动判断（老配置完全不受影响）
    return clean if len(clean) >= 2 else single


def is_multi(cfg):
    return len(symbols_of(cfg)) >= 2


def coin_name(symbol):
    """BTCUSDT -> BTC，用于邮件里显示。"""
    s = str(symbol).upper()
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[:-len(quote)]
    return s


def state_key(symbol, profile_key, multi):
    """状态键。多标的模式下带交易对前缀，避免不同币互相覆盖。

    单标的模式仍用原来的 "A"/"B"/"C"，老状态文件可以直接沿用。
    """
    return f"{symbol}|{profile_key}" if multi else profile_key


# ============================================================ 邮件
def send_email(email_cfg, subject, body, log, html=None):
    """发邮件。body 是纯文本（兜底），html 若给出则优先显示（手机端排版友好）。"""
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

    retries = int(email_cfg.get("max_retries", 3))
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # local_hostname 必须是纯 ASCII：若计算机名含中文，
            # smtplib 的 EHLO 会因 ascii 编码报错，且报错信息完全不指向主机名。
            with smtplib.SMTP_SSL(email_cfg["server"], int(email_cfg["port"]),
                                  local_hostname="signal-bot", timeout=30) as s:
                s.login(email_cfg["from"], email_cfg["password"])
                s.sendmail(email_cfg["from"], [email_cfg["to"]], msg.as_string())
            return True
        except Exception as e:                                   # noqa: BLE001
            last_err = e
            if log:
                log.warning("发送邮件失败（第 %d/%d 次）：%s", attempt, retries, e)
            if attempt < retries:
                time.sleep(3 * attempt)
    raise RuntimeError(f"发送邮件失败（已重试 {retries} 次）：{last_err}")


# ============================================================ 邮件正文
def _dw(s):
    """字符串的显示宽度（中日韩全角字符算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s, width, align="left"):
    """按显示宽度补空格，让中文表格也能对齐。"""
    gap = max(0, width - _dw(s))
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def _arrow(d):
    return "多 ▲" if d > 0 else "空 ▼"


def _side_txt(net):
    return "多" if net > 0 else ("空" if net < 0 else "空仓")


def format_body(infos, title, note="", levels=None, actions=None, prev_map=None,
                primary="B"):
    """infos: compute_profile 结果列表；levels: {key: 风控位}。

    actions: 结构化操作清单（见 _build_actions / _build_actions_multi），
    每条都带「保证金 + 操作比例 + 下单数量」，与 trade.py 的下单口径一致。

    primary: 重点展示哪一档（A/B/C）——就是你实际在跑的那一档。
    邮件开头直接给出「方向 / 杠杆 / 保证金 / 下单数量」四件事，
    避免 敞口、名义 这些术语让人不知道该怎么下单。
    """
    now = datetime.now(CST)
    ref = infos[0]
    levels = levels or {}
    L = []
    L.append(f"【BTC信号{SUBJECT_SUFFIX}】{title}")
    L.append("=" * 58)
    L.append(f"时间：{now:%Y-%m-%d %H:%M}（北京时间）")
    L.append(f"数据：{ref['bar_dt']:%Y-%m-%d} 日线（已收盘）")
    L.append(f"现价：{ref['px']:,.0f} USDT　日波动率：{ref['dvol']*100:.2f}%")
    L.append("=" * 58)
    if note:
        L.append(note)
        L.append("")

    # ---------- ① 最显眼：直接告诉你怎么下单 ----------
    main = None
    for inf in infos:
        if inf.get("key") == primary:
            main = inf
            break
    if main is None:
        main = infos[0]

    lv = levels.get(main.get("key"), {})
    side = "做多 ▲" if main["net"] > 0 else ("做空 ▼" if main["net"] < 0 else "空仓（不开仓）")

    L.append(f"★★ 你要做的事（{main['name']} · 本金 {main['equity']:,.0f} U）★★")
    L.append("-" * 58)
    L.append(f"  方向        {side}")
    e_px, e_day, e_hold = entry_info(lv, main)
    if e_px:
        chg, lev_pct = entry_pnl(e_px, main)
        tag = ("本次新建仓" if lv.get("entry_new")
               else f"{e_day} 建立，已持有 {e_hold} 天")
        L.append(f"  建仓价      {e_px:,.0f} USDT    ← {tag}")
        L.append(f"  当前浮动    价格 {chg:+.2f}%　保证金口径 {lev_pct:+.2f}%"
                 f"（{main['L']:.0f}×）")
    L.append(f"  杠杆        {main['L']:.0f}× 逐仓（固定不变）")
    if abs(main["net"]) > 1e-9:
        L.append(f"  保证金      {main['margin']:,.0f} USDT"
                 f"        ← 划这么多进合约账户")
        L.append(f"  下单数量    {main['qty']:.6f} BTC"
                 f"    ← 币安下单框填这个")
        L.append(f"  （名义 {abs(main['net']):,.0f} USDT = 保证金 × {main['L']:.0f}，不用你手算）")
        L.append("")
        if lv.get("stop"):
            L.append(f"  止损提醒    {lv['stop']:,.0f}"
                     f"（距现价 {(lv['stop']/main['px']-1)*100:+.1f}%）"
                     f"   ← 只是提醒位，不会自动挂单")
        else:
            L.append(f"  止损提醒    不设（靠方向反转离场）")
        L.append(f"  爆仓价      {lv.get('liq', main['liq']):,.0f}"
                 f"（距现价 {main['liq']/main['px']*100-100:+.1f}%，3× 逐仓的硬底）")
    else:
        L.append("  5 组参数方向互相抵消，净仓位为 0 —— 本次空仓，不操作。")
    L.append("")

    # ---------- ② 需要执行的操作（保证金 + 操作比例） ----------
    if actions and actions.get("items"):
        L.append("★ " + actions["title"])
        L.append("-" * 58)
        for a in actions["items"]:
            L.append("  " + _fmt_action_txt(a))
        L.append("  ※ 保证金 = 该仓位占用（数量 × 现价 ÷ 3×）"
                 "　操作比例 = 本次数量相对原仓位的变动")
        L.append("  ※ " + actions["note"])
        L.append("")

    # ---------- ③ 三档对照（同一份本金，只跑一档） ----------
    if len(infos) > 1:
        L.append(f"三档对照（同一份 {main['equity']:,.0f} U，风险档不同，只跑一档）")
        L.append("-" * 58)
        L.append("  " + _pad("档位", 12) + _pad("方向", 8)
                 + _pad("保证金", 12, "right") + _pad("下单数量BTC", 18, "right")
                 + _pad("目标日波动", 12, "right"))
        for inf in infos:
            s = "多" if inf["net"] > 0 else ("空" if inf["net"] < 0 else "空仓")
            mark = "  ← 你在跑这档" if inf.get("key") == main.get("key") else ""
            L.append("  " + _pad(inf["name"], 12) + _pad(s, 8)
                     + _pad(f"{inf['margin']:,.0f}", 12, "right")
                     + _pad(f"{inf['qty']:.6f}", 18, "right")
                     + _pad(f"{inf['vt']*100:.0f}%", 12, "right") + mark)
        L.append("")

    # ---------- ④ 机制说明（精简） ----------
    L.append("-" * 58)
    L.append("止盈止损怎么工作")
    L.append("  · 止损 = 方向反转（邮件会告诉你平仓/反手），不用另外挂止损单")
    L.append("  · 止盈 = 同样等方向反转，让利润奔跑（固定止盈会砍掉贡献收益的大单）")
    L.append("  · 爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆")
    L.append("")
    L.append("执行提示")
    L.append("  · 收盘后确认的信号，下一根K线开盘时调仓")
    L.append("  · 波动变大时会自动缩仓；5 组参数方向不一致时净仓位变小，都属正常")
    L.append("")
    L.append("风险提示：本邮件由量化脚本自动生成，仅为研究参考，不构成投资建议。")
    L.append(f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(L)


# ============================================================ 邮件正文 · HTML（手机友好）
_HTML_HEAD = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
              '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
              '</head><body style="margin:0;padding:0;background:#f2f2f5;'
              '-webkit-text-size-adjust:100%;">')
_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "'PingFang SC','Microsoft YaHei',sans-serif")
RED, GREEN, GREY, DARK = "#c0392b", "#1e8449", "#6b7280", "#15171a"


def _h(text):
    return (f'<div style="font-size:14px;font-weight:700;color:{DARK};'
            f'margin:18px 0 8px 0;">{text}</div>')


def _p(text, size=13, color=GREY):
    return f'<div style="font-size:{size}px;color:{color};line-height:1.7;">{text}</div>'


def _ul(items, size=13, color=GREY):
    lis = "".join(f'<li style="margin:4px 0;">{i}</li>' for i in items)
    return (f'<ul style="margin:6px 0 0 0;padding-left:18px;font-size:{size}px;'
            f'color:{color};line-height:1.6;">{lis}</ul>')


def _box(title, inner, color="#f5a623", bg="#fffaf0"):
    return (f'<div style="border-left:4px solid {color};background:{bg};'
            f'padding:12px 14px;border-radius:6px;margin:14px 0;">'
            f'<div style="font-size:14px;font-weight:700;color:{DARK};'
            f'margin-bottom:8px;">{title}</div>{inner}</div>')


def _kv(rows):
    """rows: [(label, value_html, note)] —— 手机上两列也不会挤。"""
    out = []
    for label, value, note in rows:
        note_html = (f'<span style="font-size:11px;font-weight:400;color:#9aa0a6;">'
                     f'&nbsp;{note}</span>' if note else '')
        out.append(
            '<tr>'
            f'<td style="padding:7px 8px 7px 0;font-size:13px;color:{GREY};'
            f'width:74px;vertical-align:middle;">{label}</td>'
            f'<td style="padding:7px 0;font-size:16px;font-weight:700;color:{DARK};'
            f'vertical-align:middle;">{value}{note_html}</td>'
            '</tr>')
    return f'<table style="width:100%;border-collapse:collapse;">{"".join(out)}</table>'


def _table(headers, rows, aligns=None):
    """真正的 HTML 表格：手机上按列自适应，不会像纯文本那样折行错位。"""
    aligns = aligns or ["left"] * len(headers)
    th = "".join(
        f'<th style="padding:8px 4px;text-align:{aligns[i]};font-size:11px;'
        f'color:{GREY};font-weight:600;border-bottom:1px solid #e6e6ea;'
        f'white-space:nowrap;">{h}</th>'
        for i, h in enumerate(headers))
    trs = []
    for r in rows:
        tds = "".join(
            f'<td style="padding:9px 4px;text-align:{aligns[i]};font-size:13px;'
            f'color:{DARK};border-bottom:1px solid #f0f0f3;'
            f'white-space:nowrap;">{c}</td>'
            for i, c in enumerate(r))
        trs.append(f'<tr>{tds}</tr>')
    return (f'<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>')


def _shell(main, sub, inner):
    return (_HTML_HEAD
            + f'<div style="max-width:560px;margin:0 auto;padding:12px;'
              f'font-family:{_FONT};">'
            + f'<div style="background:#15171a;color:#fff;padding:14px 16px;'
              f'border-radius:10px 10px 0 0;">'
            + f'<div style="font-size:17px;font-weight:700;line-height:1.3;">{main}</div>'
            + f'<div style="font-size:13px;color:#9aa0a6;margin-top:5px;">{sub}</div>'
            + '</div>'
            + '<div style="background:#fff;padding:16px;border:1px solid #e6e6ea;'
              'border-top:none;border-radius:0 0 10px 10px;">'
            + inner
            + '</div>'
            + '<div style="text-align:center;font-size:11px;color:#9aa0a6;'
              'padding:12px 8px;line-height:1.6;">'
              '本邮件由量化脚本自动生成，仅为研究参考，不构成投资建议。</div>'
            + '</div></body></html>')


def _side_html(net):
    if net > 1e-9:
        return f'<span style="color:{RED};">做多 ▲</span>'
    if net < -1e-9:
        return f'<span style="color:{GREEN};">做空 ▼</span>'
    return "空仓"


def _side_short(net):
    if net > 1e-9:
        return f'<span style="color:{RED};">多</span>'
    if net < -1e-9:
        return f'<span style="color:{GREEN};">空</span>'
    return "空仓"


def _pick(infos, primary):
    for inf in infos:
        if inf.get("key") == primary:
            return inf
    return infos[0]


def format_body_html(infos, title, note="", levels=None, actions=None, primary="B"):
    """format_body 的 HTML 版：手机上表格不会折行，数字更醒目。"""
    now = datetime.now(CST)
    ref = infos[0]
    levels = levels or {}
    main = _pick(infos, primary)
    lv = levels.get(main.get("key"), {})

    P = []
    P.append(_p(f"{now:%Y-%m-%d %H:%M}（北京时间）　数据 {ref['bar_dt']:%Y-%m-%d} 日线",
                12, "#9aa0a6"))
    P.append(_p(f"现价 <b>{ref['px']:,.0f}</b> USDT　日波动率 {ref['dvol']*100:.2f}%"))
    if note:
        P.append('<div style="margin-top:10px;padding:10px 12px;background:#f7f7f9;'
                 'border-radius:6px;font-size:12px;color:#6b7280;line-height:1.7;'
                 f'white-space:pre-line;">{note}</div>')

    # ① 你要做的事
    if abs(main["net"]) > 1e-9:
        rows = [("方向", _side_html(main["net"]), "")]
        e_px, e_day, e_hold = entry_info(lv, main)
        if e_px:
            chg, lev_pct = entry_pnl(e_px, main)
            tag = ("本次新建仓" if lv.get("entry_new")
                   else f"{e_day} 建立，已持有 {e_hold} 天")
            rows.append(("建仓价", f"{e_px:,.0f}", tag))
            color = RED if chg > 0 else (GREEN if chg < 0 else GREY)
            rows.append(("当前浮动",
                         f'<span style="color:{color};">价格 {chg:+.2f}%</span>',
                         f"保证金口径 {lev_pct:+.2f}%（{main['L']:.0f}×）"))
        rows += [
            ("杠杆", f"{main['L']:.0f}× 逐仓", "固定不变"),
            ("保证金", f"{main['margin']:,.0f} USDT", "划这么多进合约账户"),
            ("下单数量", f"{main['qty']:.6f} BTC", "币安下单框填这个"),
        ]
        P.append(_box(f"你要做的事（{main['name']} · 本金 {main['equity']:,.0f} U）",
                      _kv(rows)))
        rk = []
        if lv.get("stop"):
            rk.append(("止损提醒", f"{lv['stop']:,.0f}",
                       f"{(lv['stop']/main['px']-1)*100:+.1f}%　只是提醒，不会自动挂单"))
        else:
            rk.append(("止损提醒", "不设", "靠方向反转离场"))
        rk.append(("爆仓价", f"{lv.get('liq', main['liq']):,.0f}",
                   f"{main['liq']/main['px']*100-100:+.1f}%　3×逐仓硬底"))
        P.append(_kv(rk))
    else:
        P.append(_box("本次不操作",
                      _p("5 组参数方向互相抵消，净仓位为 0 —— 空仓等待。", 13, DARK),
                      "#9aa0a6", "#f7f7f9"))

    # ② 需要执行的操作（保证金 + 操作比例）
    if actions and actions.get("items"):
        P.append(_h(actions["title"]))
        P.append(_action_table_html(actions["items"], kind="profile"))
        P.append(_action_foot(actions))

    # ③ 三档对照表（手机友好）
    if len(infos) > 1:
        P.append(_h(f"三档对照（同一份 {main['equity']:,.0f} U，只跑一档）"))
        rows = []
        for inf in infos:
            name = inf["name"]
            if inf.get("key") == main.get("key"):
                name = f'<b>{name}</b>'
            rows.append([name, _side_short(inf["net"]),
                         f"{inf['margin']:,.0f} U", f"{inf['qty']:.6f}"])
        P.append(_table(["档位", "方向", "保证金", "数量BTC"], rows,
                        ["left", "center", "right", "right"]))
        P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
                 f'目标日波动：A {infos[0]["vt"]*100:.0f}%　'
                 f'B {infos[1]["vt"]*100:.0f}%　C {infos[2]["vt"]*100:.0f}%　'
                 f'（你跑的是 <b>{main["name"]}</b>）</div>')

    # ④ 机制说明
    P.append(_h("止盈止损怎么工作"))
    P.append(_ul([
        "止损 = 方向反转（邮件会告诉你平仓/反手），不用另外挂止损单",
        "止盈 = 同样等方向反转，让利润奔跑（固定止盈会砍掉贡献收益的大单）",
        "爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆",
    ]))
    P.append(_h("执行提示"))
    P.append(_ul([
        "收盘后确认的信号，下一根K线开盘时调仓",
        "波动变大时会自动缩仓；5 组参数方向不一致时净仓位变小，都属正常",
    ]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:16px;">'
             f'生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）</div>')

    return _shell(f"BTC信号{SUBJECT_SUFFIX}", title, "".join(P))


def format_alert_body_html(infos, levels, fired, px, primary="B"):
    """format_alert_body 的 HTML 版。"""
    now = datetime.now(CST)
    ref = infos[0]
    main = _pick(infos, primary)
    mlv = levels.get(main.get("key"), {})

    P = []
    P.append(_p(f"{now:%Y-%m-%d %H:%M}（北京时间）", 12, "#9aa0a6"))
    P.append(_p(f"实时价 <b>{px:,.0f}</b> USDT　日线收盘 {ref['px']:,.0f}"
                f"　日波动率 {ref['dvol']*100:.2f}%"))
    P.append(_box("⚠ 这是「价格触及」提醒，不是策略信号",
                  _p("策略本身不挂价格止损，是否操作由你决定。", 12, "#8a5a00"),
                  "#f5a623", "#fff8e6"))
    rows = [("方向", _side_html(main["net"]), "")]
    e_px, e_day, e_hold = entry_info(mlv, main)
    if e_px:
        chg, lev_pct = entry_pnl(e_px, main)
        color = RED if chg > 0 else (GREEN if chg < 0 else GREY)
        rows.append(("建仓价", f"{e_px:,.0f}", f"{e_day}　已持有 {e_hold} 天"))
        rows.append(("当前浮动",
                     f'<span style="color:{color};">价格 {chg:+.2f}%</span>',
                     f"保证金口径 {lev_pct:+.2f}%（{main['L']:.0f}×）"))
    rows += [
        ("杠杆", f"{main['L']:.0f}× 逐仓", ""),
        ("当前持仓", f"{main['qty']:.6f} BTC", f"保证金 {main['margin']:,.0f} U"),
    ]
    if mlv.get("stop"):
        rows.append(("止损提醒位", f"{mlv['stop']:,.0f}",
                     f"实时价距此 {(mlv['stop']/px-1)*100:+.1f}%"))
    else:
        rows.append(("止损提醒位", "不设", "靠方向反转离场"))
    rows.append(("爆仓价", f"{mlv.get('liq', main['liq']):,.0f}",
                 f"实时价距此 {(main['liq']/px-1)*100:+.1f}%"))
    P.append(_box(f"你的仓位（{main['name']} · 本金 {main['equity']:,.0f} U）", _kv(rows)))

    others = [i for i in infos if i.get("key") != main.get("key")]
    if others:
        P.append(_h("其他档位参考"))
        rows = []
        for inf in others:
            name = inf["name"]
            rows.append([name, _side_short(inf["net"]),
                         f"{inf['margin']:,.0f} U", f"{inf['qty']:.6f}"])
        P.append(_table(["档位", "方向", "保证金", "数量BTC"], rows,
                        ["left", "center", "right", "right"]))

    P.append(_h("为什么本策略不挂固定止损（重要）"))
    P.append(_ul([
        "回测实测：BTC 上吊灯 3×ATR 止损会把 114.89× 打到 2.29×",
        "且最优止损距离跨资产不一致（SOL 上 0.64× → 0.16×）",
        "所以本提醒是「极端行情保险」，不是日常操作依据",
        "日常止损靠「方向反转」——那封信号邮件会告诉你怎么做",
    ]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:16px;">'
             f'生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）</div>')
    return _shell(f"BTC风控{SUBJECT_SUFFIX}", "、".join(fired), "".join(P))


def format_test_body_html(cfg, bars):
    """format_test_body 的 HTML 版。"""
    now = datetime.now(CST)
    last = bars[-1]
    P = []
    P.append(_box("✓ 邮件通道正常",
                  _p("收到本邮件 = SMTP 配置正确，策略脚本可以正常推送。", 13, "#1e6b3a"),
                  "#1e8449", "#f0f9f4"))
    P.append(_kv([
        ("发送时间", f"{now:%Y-%m-%d %H:%M:%S}", "北京时间"),
        ("交易对", cfg["symbol"], f"{cfg['interval']}　{cfg['leverage']:.0f}× 逐仓"),
        ("最新K线", f"{datetime.fromtimestamp(last['open_time']/1000, CST):%Y-%m-%d}",
         f"收盘 {last['close']:,.0f} USDT"),
        ("历史K线", f"{len(bars)} 根", ""),
    ]))
    P.append(_h("推送规则"))
    P.append(_ul([
        "每天 08:05 自动发一封日报（就是你以后会收到的那种）",
        "方向反转时，日报会变成「方向变化：…」并给出调仓动作",
        "价格触及止损/爆仓位时，另有一封「风控提醒」",
        "想停掉日报：记事本打开 run_daily.bat，删掉末尾的 --daily",
    ]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:16px;">'
             f'数据源：币安公开接口（data-api.binance.vision）</div>')
    return _shell(f"BTC信号{SUBJECT_SUFFIX}", "测试邮件 · 邮件通道验证", "".join(P))


def format_alert_body(infos, levels, fired, px, log, primary="B"):
    """价格触发的风控提醒邮件。开头直接给出「你的仓位 + 两个关键价位」。"""
    now = datetime.now(CST)
    ref = infos[0]
    L = []
    L.append(f"【BTC风控{SUBJECT_SUFFIX}】" + "、".join(fired))
    L.append("=" * 58)
    L.append(f"触发时间：{now:%Y-%m-%d %H:%M}（北京时间）")
    L.append(f"实时价格：{px:,.0f} USDT")
    L.append(f"日线收盘：{ref['px']:,.0f} USDT　日波动率：{ref['dvol']*100:.2f}%")
    L.append("=" * 58)
    L.append("⚠ 这是「价格触及」提醒，不是策略信号。")
    L.append("  策略本身不挂价格止损，是否操作由你决定。")
    L.append("")

    main = None
    for inf in infos:
        if inf.get("key") == primary:
            main = inf
            break
    if main is None:
        main = infos[0]
    mlv = levels.get(main.get("key"), {})

    L.append(f"★★ 你的仓位（{main['name']} · 本金 {main['equity']:,.0f} U）★★")
    L.append("-" * 58)
    L.append(f"  方向        {'做多 ▲' if main['net'] > 0 else ('做空 ▼' if main['net'] < 0 else '空仓')}")
    e_px, e_day, e_hold = entry_info(mlv, main)
    if e_px:
        chg, lev_pct = entry_pnl(e_px, main)
        L.append(f"  建仓价      {e_px:,.0f} USDT    ← {e_day} 建立，已持有 {e_hold} 天")
        L.append(f"  当前浮动    价格 {chg:+.2f}%　保证金口径 {lev_pct:+.2f}%"
                 f"（{main['L']:.0f}×）")
    L.append(f"  杠杆        {main['L']:.0f}× 逐仓")
    L.append(f"  当前持仓    {main['qty']:.6f} BTC"
             f"（保证金 {main['margin']:,.0f} U）")
    if mlv.get("stop"):
        L.append(f"  止损提醒位  {mlv['stop']:,.0f}"
                 f"       ← 实时价距此 {(mlv['stop']/px-1)*100:+.1f}%")
    else:
        L.append("  止损提醒位  不设（靠方向反转离场）")
    L.append(f"  爆仓价      {mlv.get('liq', main['liq']):,.0f}"
             f"       ← 实时价距此 {(main['liq']/px-1)*100:+.1f}%")
    L.append("")

    # 其他档位（如果你同时跑多档才需要看）
    others = [i for i in infos if i.get("key") != main.get("key")]
    if others:
        L.append("其他档位参考")
        L.append("-" * 58)
        for inf in others:
            L.append(f"  {inf['name']}：{_side_txt(inf['net'])}头　"
                     f"保证金 {inf['margin']:,.0f} U　持仓 {inf['qty']:.6f} BTC")
        L.append("")

    L.append("-" * 58)
    L.append("为什么本策略不挂固定止损（重要）")
    L.append("  · 回测实测：BTC 上吊灯 3×ATR 止损会把 114.89× 打到 2.29×")
    L.append("  · 且最优止损距离跨资产不一致（SOL 上 0.64× → 0.16×）")
    L.append("  · 所以本提醒是「极端行情保险」，不是日常操作依据")
    L.append("  · 日常止损靠「方向反转」——那封信号邮件会告诉你怎么做")
    L.append("")
    L.append("风险提示：本邮件由量化脚本自动生成，不构成投资建议。")
    L.append(f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(L)



def format_test_body(cfg, bars):
    now = datetime.now(CST)
    last = bars[-1]
    L = [
        f"【BTC信号{SUBJECT_SUFFIX}】测试邮件 · 邮件通道验证",
        "=" * 58,
        "这是一封测试邮件，用来验证邮件通道是否畅通。",
        "收到本邮件 = SMTP 配置正确，策略脚本可以正常推送。",
        "=" * 58,
        f"发送时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）",
        f"交易对：{cfg['symbol']}　周期：{cfg['interval']}　杠杆：{cfg['leverage']:.0f}× 逐仓",
        f"数据源：币安公开接口（data-api.binance.vision）",
        f"最新已收盘K线：{datetime.fromtimestamp(last['open_time']/1000, CST):%Y-%m-%d}"
        f"　收盘价 {last['close']:,.0f} USDT",
        f"已获取历史K线：{len(bars)} 根",
        "=" * 58,
        "推送规则",
        "  · 每天 08:05 自动发一封日报（就是你以后会收到的那种）",
        "  · 方向反转时，日报会变成「方向变化：…」并给出调仓动作",
        "  · 价格触及止损/爆仓位时，另有一封「风控提醒」",
        "  · 想停掉日报：记事本打开 run_daily.bat，删掉末尾的 --daily",
    ]
    return "\n".join(L)


def _peak_since(bars, entry_open_ms, is_long):
    """开仓以来的最高价（多头）/ 最低价（空头），用于吊灯移动止损。"""
    sub = [b for b in bars if b["open_time"] >= entry_open_ms]
    if not sub:
        return None
    return max(b["high"] for b in sub) if is_long else min(b["low"] for b in sub)


def _migrate_state(state, syms):
    """把老版单标的状态（"A"/"B"/"C"）迁到多标的键（"BTCUSDT|A"）上。

    只在多标的模式下调用，且只在目标键不存在时复制，不会覆盖已有记录。
    返回迁移条数。旧键保留不删（切回 single 模式还能用）。
    """
    btc = "BTCUSDT" if "BTCUSDT" in syms else syms[0]
    n = 0
    for k in ("A", "B", "C"):
        old = state.get(k)
        if not isinstance(old, dict):
            continue
        nk = f"{btc}|{k}"
        if nk not in state:
            state[nk] = dict(old)
            n += 1
    return n


def entry_info(lv, inf):
    """从风控位里取出「建仓价 / 建仓日 / 持有天数」。

    空仓时返回 (None, None, 0)。
    建仓价 = 方向确认那根K线的收盘价（也就是你实际该下单的价位）。
    """
    if abs(inf["net"]) < 1e-9:
        return None, None, 0
    px = lv.get("base")
    if not px:
        return None, None, 0
    eb = lv.get("entry_bar")
    dt = datetime.fromtimestamp(eb / 1000, CST) if eb else inf["bar_dt"]
    days = (inf["bar_dt"].date() - dt.date()).days
    return float(px), dt.strftime("%Y-%m-%d"), max(0, days)


def entry_pnl(entry_px, inf):
    """返回 (价格变动%, 保证金口径收益率%)。

    保证金收益率 = 价格变动 × 杠杆 —— 因为 名义 = 保证金 × 杠杆，
    所以仓位盈亏 ÷ 保证金 = 价格变动 × 杠杆。
    """
    if not entry_px:
        return None, None
    chg = inf["px"] / float(entry_px) - 1.0
    return chg * 100.0, chg * float(inf["L"]) * 100.0


# ============================================================ 操作清单（保证金 + 操作比例）
def _action_items(infos, changed, first_run):
    """把「仓位变化」翻译成结构化操作项 —— 口径与 trade.py 的 _decide() 完全一致。

    只对「首次运行」和「方向发生变化」的标的产出条目；其余标的没有动作。

    每条包含：
      coin/name   展示用
      kind        open / close / flip / add / cut / hold
      label       中文动作名（开多仓 / 平仓 / 反手 / 加仓 / 减仓 / 不动）
      order       要在币安下的那一单，例如 "SELL 0.039535"（hold 时为 ""）
      delta       这一单的数量（绝对值）
      old_qty / new_qty
      old_margin / new_margin   保证金（U）= |数量| × 现价 ÷ 杠杆
      pct         操作比例（本次相对原仓位的变动 %）；新开 / 反手 / 空仓为 None
    """
    changed_map = {i["key"]: p for p, i in changed}
    first_keys = {i["key"] for i in first_run}
    out = []
    for inf in infos:
        k = inf["key"]
        if k not in changed_map and k not in first_keys:
            continue
        prev = changed_map.get(k) or {}
        old_qty = float(prev.get("qty") or 0)
        new_qty = float(inf["qty"])
        lev = float(inf.get("L") or 3.0)
        px = float(inf["px"])
        old_m = abs(old_qty) * px / lev
        new_m = float(inf["margin"])
        o, n = abs(old_qty), abs(new_qty)

        if o < 1e-9 and n < 1e-9:
            kind, label, order, delta = "hold", "不动", "", 0.0
        elif o < 1e-9:
            kind = "open"
            label = f"开{'多' if new_qty > 0 else '空'}仓"
            order = f"{'BUY' if new_qty > 0 else 'SELL'} {n:.6f}"
            delta = n
        elif n < 1e-9:
            kind, label = "close", "平仓"
            order = f"{'SELL' if old_qty > 0 else 'BUY'} {o:.6f}"
            delta = o
        elif (old_qty > 0) != (new_qty > 0):
            kind, label = "flip", "反手"
            close_side = "SELL" if old_qty > 0 else "BUY"
            open_side = "BUY" if new_qty > 0 else "SELL"
            order = f"先 {close_side} {o:.6f}，再 {open_side} {n:.6f}"
            delta = o + n
        elif n > o:
            kind, label = "add", "加仓"
            order = f"{'BUY' if new_qty > 0 else 'SELL'} {n - o:.6f}"
            delta = n - o
        elif n < o:
            kind, label = "cut", "减仓"
            order = f"{'SELL' if old_qty > 0 else 'BUY'} {o - n:.6f}"
            delta = o - n
        else:
            kind, label, order, delta = "hold", "不动", "", 0.0

        if kind in ("open", "flip") or o < 1e-9:
            pct = None
        else:
            pct = (n - o) / o * 100.0

        out.append({
            "key": k, "coin": inf["coin"], "name": inf.get("name") or "",
            "kind": kind, "label": label, "order": order, "delta": delta,
            "old_qty": old_qty, "new_qty": new_qty,
            "old_margin": old_m, "new_margin": new_m, "pct": pct,
            "side": _side_txt(inf["net"]),
        })
    return out


def _pct_txt(a):
    """操作比例的展示文案。"""
    if a["kind"] in ("open",):
        return "新开"
    if a["kind"] == "flip":
        return "反手"
    if a["pct"] is None:
        return "—"
    return f"{a['pct']:+.0f}%"


def _split_actions(actions):
    """兼容新结构化 dict 与老的字符串列表，返回 (items, ref_items, title, note)。"""
    if isinstance(actions, dict):
        return (actions.get("items") or [], actions.get("ref_items") or [],
                actions.get("title") or "需要执行的操作", actions.get("note") or "")
    return (actions or [], [], "需要执行的操作", "")


def _fmt_action_txt(a, with_name=True):
    """把一条操作渲染成纯文本：保证金 + 操作比例 + 下单数量。"""
    head = f"{a['coin']}（{a['name']}）" if (with_name and a.get("name")) else a["coin"]
    if a["kind"] == "hold":
        return f"{head}：不动　保证金 {a['new_margin']:,.0f} U（仓位未变，无需下单）"
    mg = f"{a['old_margin']:,.0f} → {a['new_margin']:,.0f} U"
    return (f"{head}：{a['label']}　操作比例 {_pct_txt(a)}　"
            f"保证金 {mg}　下单 {a['order']} {a['coin']}")


def _action_table_html(items, kind="coin", primary=None):
    """把操作项渲染成 HTML 表格：动作 / 保证金 / 操作比例 / 下单数量。

    kind:
      "coin"    —— 第一列「币种·档位」，例如 ETH·B（多标的、三档并列时用）
      "profile" —— 第一列只写档位名，例如 B 推荐（单标的、只跑一档时用）
      "plain"   —— 第一列只写币种（BTC 单跑参照这种单一标的用）
    primary: 用户实际在跑的那一档，加粗突出。
    """
    head = {"coin": "币种·档位", "profile": "档位", "plain": "标的"}.get(kind, "币种")
    rows = []
    for a in items:
        pk = a["key"].split("|")[-1] if "|" in a["key"] else ""
        if kind == "coin":
            first = f"{a['coin']}·{pk}" if pk else a["coin"]
        elif kind == "profile":
            first = a.get("name") or a["coin"]
        else:
            first = a["coin"]
        if primary and pk == primary:
            first = f"<b>{first}</b>"
        if a["kind"] == "hold":
            rows.append([first, "不动", f"{a['new_margin']:,.0f}", "—", "—"])
        else:
            rows.append([
                first, a["label"],
                f"{a['old_margin']:,.0f} → <b>{a['new_margin']:,.0f}</b>",
                _pct_txt(a), f"{a['order']} {a['coin']}"])
    return _table([head, "动作", "保证金(U)", "操作比例", "下单数量"], rows,
                  ["left", "left", "right", "right", "right"])


def _action_foot(actions):
    """操作表下方的口径说明 + 总开关提示。"""
    return (f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;line-height:1.7;">'
            f'保证金 = 该仓位占用（数量 × 现价 ÷ 3×）　'
            f'操作比例 = 本次数量相对原仓位的变动<br>{actions["note"]}</div>')


def _action_header(cfg):
    """按总开关决定标题与说明：live 下这些动作是脚本自动做的，不是「要你手动做」。"""
    mode = ""
    try:
        mode = str((cfg.get("_master_switch") or {}).get("mode") or "").strip().lower()
    except Exception:
        mode = ""
    if mode == "live":
        return ("需要执行的操作（脚本会自动完成）",
                "你已开启「真实下单」总开关（mode=live）：上面这些动作会由 "
                "run_trade.bat 自动执行，你不用手动操作。只想看信号不想下单，"
                "把总开关改回 observe 即可。")
    return ("需要你手动执行的操作",
            "当前总开关是 observe，脚本不会自动下单：以上动作请你在币安 App 里手动完成。")


def _build_actions(infos, changed, first_run, cfg=None):
    """单标的版操作清单（结构化）。"""
    title, note = _action_header(cfg or {})
    return {"title": title, "note": note,
            "items": _action_items(infos, changed, first_run), "ref_items": []}


def _state_entry(inf, lv, prev):
    """构造写入 signal_state.json 的一条记录。"""
    prev = prev or {}
    entry_bar = prev.get("entry_bar") or int(inf["bar_open_ms"])
    return {
        "dirs": inf["dirs"], "side": inf["side"], "net_expo": round(inf["net_expo"], 4),
        "qty": round(inf["qty"], 8), "entry_px": round(lv["base"], 2),
        "entry_bar": entry_bar, "peak_px": round(lv["peak"], 2),
        "bar_dt": inf["bar_dt"].strftime("%Y-%m-%d"),
        "last_checked": datetime.now(CST).isoformat(),
    }


# ============================================================ 多标的：操作清单与邮件
def _build_actions_multi(infos, changed, first_run, cfg=None, ref=None):
    """多标的版操作清单（结构化）——按币种列出，另带一份 BTC 单跑参照。

    ref = (ref_infos, ref_changed, ref_first)；给 None 表示不算参照那一组。
    """
    title, note = _action_header(cfg or {})
    ref_items = []
    if ref:
        r_infos, r_changed, r_first = ref
        ref_items = _action_items(r_infos, r_changed, r_first)
    return {"title": title, "note": note,
            "items": _action_items(infos, changed, first_run),
            "ref_items": ref_items}


def _multi_rows(infos, primary, levels):
    """按 primary 档抽出「每个币一行」的数据，顺序与 syms 一致。"""
    return [i for i in infos if i["pk"] == primary]


def format_multi_body(infos, title, note, levels, actions, primary, cfg, syms, btc_ref):
    """多标的合并邮件的纯文本兜底版。"""
    now = datetime.now(CST)
    ref = infos[0]
    prim = _multi_rows(infos, primary, levels)
    by_sym = {i["symbol"]: i for i in prim}
    t_margin = sum(i["margin"] for i in prim)
    t_notional = sum(abs(i["net"]) for i in prim)
    t_eq = sum(i["equity"] for i in prim) or 1.0
    act_items, ref_items, act_title, act_note = _split_actions(actions)

    L = []
    L.append(f"{now:%Y-%m-%d %H:%M}（北京时间）  数据 {ref['bar_dt']:%Y-%m-%d} 日线")
    L.append(f"模式：多标的等权（{len(syms)} 个交易对）  每个币配额 = 账户总权益 ÷ {len(syms)}")
    if note:
        L.append("")
        L.append(note)
    L.append("")
    L.append(f"===== ① 四币等权 · 你实际要下的单（{primary} 档 · 每币 "
             f"{prim[0]['equity']:,.0f} U）=====")
    for s in syms:
        inf = by_sym.get(s)
        if not inf:
            continue
        lv = levels.get(inf["key"], {})
        L.append(f"  {inf['coin']:<6}{inf['side']:<4}保证金 {inf['margin']:>7,.0f} U   "
                 f"数量 {inf['qty']:>12.6f}   名义 {abs(inf['net']):>7,.0f} U   "
                 f"爆仓价 {lv.get('liq', inf['liq']):>11,.0f}")
    L.append(f"  {'合计':<6}{'':4}保证金 {t_margin:>7,.0f} U   "
             f"{'':26}名义 {t_notional:>7,.0f} U")
    L.append(f"  → 合计保证金占本金 {t_margin / t_eq * 100:.1f}%，其余为安全垫")

    # ② 建仓价（你是什么价位进的）
    L.append("")
    L.append("===== ② 你的持仓 · 建仓价 / 止损提醒 =====")
    L.append("  " + _pad("币种", 7) + _pad("方向", 7) + _pad("建仓价", 12, "right")
             + _pad("现价", 12, "right") + _pad("止损提醒", 12, "right")
             + _pad("浮动", 11, "right") + "   建仓日")
    for s in syms:
        inf = by_sym.get(s)
        if not inf:
            continue
        lv = levels.get(inf["key"], {})
        e_px, e_day, e_hold = entry_info(lv, inf)
        stop_txt = f"{lv['stop']:,.0f}" if lv.get("stop") else "不设"
        if not e_px:
            L.append("  " + _pad(inf["coin"], 7) + _pad("空仓", 7)
                     + _pad("—", 12, "right") + _pad(f"{inf['px']:,.0f}", 12, "right")
                     + _pad(stop_txt, 12, "right") + _pad("—", 11, "right") + "   —")
            continue
        chg, lev_pct = entry_pnl(e_px, inf)
        tag = "本次新建仓" if lv.get("entry_new") else f"{e_day}（{e_hold}天）"
        L.append("  " + _pad(inf["coin"], 7) + _pad(inf["side"], 7)
                 + _pad(f"{e_px:,.0f}", 12, "right")
                 + _pad(f"{inf['px']:,.0f}", 12, "right")
                 + _pad(stop_txt, 12, "right")
                 + _pad(f"{lev_pct:+.1f}%", 11, "right") + f"   {tag}")
    L.append("  ※ 建仓价 = 方向确认那根K线的收盘价；浮动按保证金口径（价格变动 × 3）")
    L.append(f"  ※ 止损提醒 = 吊灯 {float((cfg.get('risk') or {}).get('trail_atr', 5.0)):g}×ATR "
             f"移动止损（跟着最高价往上走）—— 只是提醒位，脚本不会自动挂单或平仓")
    L.append("  ※ 爆仓价见上表 ①；策略真正的离场信号是「方向反转」")

    if btc_ref:
        lvr = levels.get(btc_ref["key"], {})
        L.append("")
        L.append(f"===== ③ BTC 单跑参照（把 {btc_ref['equity']:,.0f} U 全押 BTC）=====")
        L.append(f"  {btc_ref['side']}  保证金 {btc_ref['margin']:,.0f} U   "
                 f"数量 {btc_ref['qty']:.6f} BTC   名义 {abs(btc_ref['net']):,.0f} U   "
                 f"爆仓价 {lvr.get('liq', btc_ref['liq']):,.0f}")
        if ref_items:
            L.append("  ★ 若要按这个方案单跑，本次动作：")
            for a in ref_items:
                L.append("    " + _fmt_action_txt(a, with_name=False))
        else:
            L.append("  ★ 本次无需操作（仓位未变）")
        L.append("  ※ 这是对照，不是你要下的单。四币方案下 BTC 只占 "
                 f"{by_sym.get(btc_ref['symbol'], btc_ref)['equity']:,.0f} U 配额。")

    if act_items:
        L.append("")
        L.append(f"===== {act_title} =====")
        for a in act_items:
            line = _fmt_action_txt(a)
            pk = a["key"].split("|")[-1] if "|" in a["key"] else ""
            if pk == primary:
                line += "　← 你在跑这档"
            L.append("  · " + line)
        L.append("  ※ 保证金 = 该仓位占用（数量 × 现价 ÷ 3×）"
                 "　操作比例 = 本次数量相对原仓位的变动")
        L.append("  ※ " + act_note)

    L.append("")
    L.append("===== 三档对照（每币保证金）=====")
    hdr = "  档位    " + "".join(f"{coin_name(s):>10}" for s in syms)
    L.append(hdr)
    for k in sorted({i["pk"] for i in infos}):
        row = [i for i in infos if i["pk"] == k]
        m = {i["symbol"]: i["margin"] for i in row}
        L.append(f"  {k:<8}" + "".join(f"{m.get(s, 0):>9,.0f}U" for s in syms))
    L.append("")
    L.append("===== 止盈止损怎么工作 =====")
    for t in ["止损 = 方向反转（邮件会告诉你平仓/反手），不用另外挂止损单",
              "止盈 = 同样等方向反转，让利润奔跑",
              "爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆",
              "每个币独立判断方向，一个币爆仓不影响其他币（逐仓隔离）"]:
        L.append(f"  · {t}")
    L.append("")
    L.append(f"生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(L)


def format_multi_body_html(infos, title, note, levels, actions, primary, cfg, syms, btc_ref):
    """多标的合并邮件的 HTML 版（手机友好）。"""
    now = datetime.now(CST)
    ref = infos[0]
    prim = _multi_rows(infos, primary, levels)
    by_sym = {i["symbol"]: i for i in prim}
    t_margin = sum(i["margin"] for i in prim)
    t_notional = sum(abs(i["net"]) for i in prim)
    t_eq = sum(i["equity"] for i in prim) or 1.0
    per_eq = prim[0]["equity"] if prim else 0.0
    act_items, ref_items, act_title, act_note = _split_actions(actions)

    P = []
    P.append(_p(f"{now:%Y-%m-%d %H:%M}（北京时间）　数据 {ref['bar_dt']:%Y-%m-%d} 日线", 12, "#9aa0a6"))
    P.append(_p(f"模式：<b>多标的等权</b>（{len(syms)} 个交易对）　"
                f"每个币配额 = 账户总权益 ÷ {len(syms)}"))
    if note:
        P.append('<div style="margin-top:10px;padding:10px 12px;background:#f7f7f9;'
                 'border-radius:6px;font-size:12px;color:#6b7280;line-height:1.7;'
                 f'white-space:pre-line;">{note}</div>')

    # ① 四币等权
    P.append(_h(f"① 四币等权 · 你实际要下的单（{primary} 档 · 每币 {per_eq:,.0f} U）"))
    rows = []
    for s in syms:
        inf = by_sym.get(s)
        if not inf:
            continue
        lv = levels.get(inf["key"], {})
        rows.append([inf["coin"], _side_html(inf["net"]), f"{inf['margin']:,.0f} U",
                     f"{inf['qty']:.6f}", f"{lv.get('liq', inf['liq']):,.0f}"])
    rows.append(['<b>合计</b>', '', f"<b>{t_margin:,.0f} U</b>", '', ''])
    P.append(_table(["币种", "方向", "保证金", "下单数量", "爆仓价"], rows,
                    ["left", "center", "right", "right", "right"]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
             f'合计名义 {t_notional:,.0f} U　保证金占本金 <b>{t_margin / t_eq * 100:.1f}%</b>'
             f'（其余为安全垫）　杠杆 3× 逐仓　每个币独立隔离，互不影响</div>')

    # ② 你的持仓：建仓价 / 止损提醒
    P.append(_h("② 你的持仓 · 建仓价 / 止损提醒"))
    rows = []
    for s in syms:
        inf = by_sym.get(s)
        if not inf:
            continue
        lv = levels.get(inf["key"], {})
        e_px, e_day, e_hold = entry_info(lv, inf)
        stop_txt = f"{lv['stop']:,.0f}" if lv.get("stop") else "不设"
        if not e_px:
            rows.append([inf["coin"], "—", f"{inf['px']:,.0f}", stop_txt, "—", "空仓"])
            continue
        chg, lev_pct = entry_pnl(e_px, inf)
        color = RED if chg > 0 else (GREEN if chg < 0 else GREY)
        tag = ("<b>本次新建仓</b>" if lv.get("entry_new")
               else f"{e_day}<br><span style=\"font-size:11px;color:#9aa0a6;\">"
                    f"持有 {e_hold} 天</span>")
        rows.append([inf["coin"], f"{e_px:,.0f}", f"{inf['px']:,.0f}", stop_txt,
                     f'<span style="color:{color};">{lev_pct:+.1f}%</span>', tag])
    P.append(_table(["币种", "建仓价", "现价", "止损提醒", "浮动", "建仓日"], rows,
                    ["left", "right", "right", "right", "right", "right"]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;line-height:1.7;">'
             f'建仓价 = 方向确认那根K线的收盘价　'
             f'浮动 = 价格变动 × {infos[0]["L"]:.0f}×（保证金口径）<br>'
             f'止损提醒 = 吊灯 '
             f'{float((cfg.get("risk") or {}).get("trail_atr", 5.0)):g}×ATR 移动止损'
             f'（跟着最高价往上走）—— <b>只是提醒位，脚本不会自动挂单或平仓</b><br>'
             f'爆仓价见上表 ①　策略真正的离场信号是「方向反转」</div>')

    # ③ BTC 单跑参照
    if btc_ref:
        lvr = levels.get(btc_ref["key"], {})
        own = by_sym.get(btc_ref["symbol"])
        P.append(_h("③ BTC 单跑参照 · 如果把本金全押 BTC"))
        P.append(_table(
            ["方向", "保证金", "下单数量", "名义", "爆仓价"],
            [[_side_html(btc_ref["net"]), f"{btc_ref['margin']:,.0f} U",
              f"{btc_ref['qty']:.6f}", f"{abs(btc_ref['net']):,.0f} U",
              f"{lvr.get('liq', btc_ref['liq']):,.0f}"]],
            ["center", "right", "right", "right", "right"]))
        if ref_items:
            P.append(f'<div style="font-size:12px;font-weight:600;color:{DARK};'
                     f'margin:10px 0 4px 0;">★ 若要按这个方案单跑，本次动作</div>')
            P.append(_action_table_html(ref_items, kind="plain"))
        else:
            P.append(f'<div style="font-size:12px;color:{GREY};margin-top:8px;">'
                     f'★ 本次无需操作（仓位未变）</div>')
        P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
                 f'※ 这是<b>对照</b>，不要照着下单。四币方案下 BTC 只占 '
                 f'{own["equity"] if own else per_eq:,.0f} U 配额，'
                 f'比单跑小 {len(syms)} 倍，回撤也更小。</div>')

    # 需要执行的操作（保证金 + 操作比例）
    if act_items:
        P.append(_h(act_title))
        P.append(_action_table_html(act_items, kind="coin", primary=primary))
        P.append(_action_foot({"note": act_note}))

    # ④ 三档对照
    keys_sorted = sorted({i["pk"] for i in infos})
    if len(keys_sorted) > 1:
        P.append(_h("三档对照（每币保证金）"))
        rows = []
        for k in keys_sorted:
            row = [i for i in infos if i["pk"] == k]
            m = {i["symbol"]: i["margin"] for i in row}
            nm = row[0]["name"]
            rows.append([f'<b>{nm}</b>' if k == primary else nm]
                        + [f"{m.get(s, 0):,.0f}" for s in syms])
        P.append(_table(["档位"] + [coin_name(s) for s in syms], rows,
                        ["left"] + ["right"] * len(syms)))
        vts = {i["pk"]: i["vt"] for i in infos}
        P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
                 f'目标日波动：' + "　".join(
                     f'{k} {vts[k] * 100:.0f}%' for k in keys_sorted)
                 + f'　（你跑的是 <b>{primary}</b> 档）</div>')

    # ⑤ 机制
    P.append(_h("止盈止损怎么工作"))
    P.append(_ul([
        "止损 = 方向反转（邮件会告诉你平仓/反手），不用另外挂止损单",
        "止盈 = 同样等方向反转，让利润奔跑（固定止盈会砍掉贡献收益的大单）",
        "爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆",
        "每个币独立判断方向，一个币爆仓只亏它自己那份保证金，不影响其他币",
    ]))
    P.append(_h("执行提示"))
    P.append(_ul([
        "收盘后确认的信号，下一根K线开盘时调仓",
        "波动变大时自动缩仓；5 组参数方向不一致时净仓位变小，都属正常",
        "账户涨了会自动等比放大仓位（配额 = 总权益 ÷ 币数），不用手动改",
    ]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:16px;">'
             f'生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）</div>')

    head = "/".join(coin_name(s) for s in syms)
    return _shell(f"{head} 信号{SUBJECT_SUFFIX}", title, "".join(P))


# ============================================================ 主流程
def run(keys, force=False, dry_run=False, test=False, daily=False, log=None, symbols=None):
    log = log or setup_logger("live")
    cfg = load_config()
    syms = symbols_of(cfg, override=symbols)
    multi = len(syms) >= 2
    n_sym = len(syms)

    log.info("启动：profiles=%s symbols=%s 模式=%s force=%s dry_run=%s test=%s daily=%s",
             keys, syms, ("多标的等权" if multi else "单标的"), force, dry_run, test, daily)

    # ---------- ① 拉取各交易对K线 ----------
    bars_map = {}
    for s in syms:
        b = keep_closed(fetch_klines(s, cfg["interval"], cfg.get("kline_limit", 1000),
                                     int(cfg["email"].get("max_retries", 3)), log))
        if len(b) < 300:
            raise RuntimeError(f"{s} 已收盘K线只有 {len(b)} 根，不足以计算 EMA60，请检查数据源")
        staleness = (datetime.now(timezone.utc)
                     - datetime.fromtimestamp(b[-1]["close_time"] / 1000, timezone.utc)).days
        log.info("%s 数据就绪：%d 根，最新已收盘 %s，滞后 %d 天", s, len(b),
                 datetime.fromtimestamp(b[-1]["open_time"] / 1000, CST).date(), staleness)
        if staleness > int(cfg.get("max_bar_staleness_days", 3)):
            log.warning("%s 最新K线滞后 %d 天，数据源可能异常", s, staleness)
        bars_map[s] = b
    ref_bars = bars_map[syms[0]]
    head = ("/".join(coin_name(s) for s in syms) if multi else coin_name(syms[0]))

    # --- 测试邮件
    if test:
        subject = f"【{head}信号{SUBJECT_SUFFIX}】测试邮件 · 邮件通道验证"
        body = format_test_body(cfg, ref_bars)
        html = format_test_body_html(cfg, ref_bars)
        if dry_run:
            log.info("[dry-run] 不实际发送。正文如下：\n%s", body)
            return 0
        send_email(cfg["email"], subject, body, log, html=html)
        log.info("测试邮件已发送至 %s", cfg["email"]["to"])
        return 0

    # --- 计算各档位
    #     多标的：每个币分到「配置本金 ÷ 币数」（与实盘 trade.py 的
    #     「账户总权益 ÷ 币数」同构，所以邮件里的数字能直接对着下单）
    infos = []
    for s in syms:
        for k in keys:
            prof = cfg["profiles"][k]
            eq = (float(prof["equity"]) / n_sym) if multi else None
            inf = compute_profile(prof, bars_map[s], cfg, symbol=s, equity_override=eq)
            inf["key"] = state_key(s, k, multi)
            inf["pk"] = k
            inf["coin"] = coin_name(s)
            infos.append(inf)
            log.info("%s %s: 方向=%s 净敞口=%.3f 名义=%.0f 参数明细=%s",
                     s, inf["name"], inf["side"], inf["net_expo"], abs(inf["net"]), inf["dirs"])

    # 多标的：额外算一份「BTC 单跑参照」——把全部配置本金押在 BTC 上
    btc_ref = None
    if multi:
        btc_sym = "BTCUSDT" if "BTCUSDT" in syms else syms[0]
        prof = cfg["profiles"][keys[0]]
        btc_ref = compute_profile(prof, bars_map[btc_sym], cfg, symbol=btc_sym,
                                  equity_override=float(prof["equity"]))
        btc_ref["key"] = f"{btc_sym}|ref"
        btc_ref["pk"] = keys[0]
        btc_ref["coin"] = coin_name(btc_sym)

    state = load_state()
    # 老版本状态是单标的 {"A":..,"B":..}；切到多标的时把 BTC 那条迁过来，
    # 否则会丢掉已经跟踪了很久的建仓价（第一根K线会被当成「今天才建仓」）。
    if multi:
        migrated = _migrate_state(state, syms)
        if migrated:
            log.info("状态迁移：把旧单标的记录 %d 条迁到 %s 名下（保住建仓价）",
                     migrated, "BTCUSDT" if "BTCUSDT" in syms else syms[0])

    changed, first_run = [], []
    for inf in infos:
        prev = state.get(inf["key"])
        if prev is None:
            first_run.append(inf)
        elif prev.get("dirs") != inf["dirs"]:
            changed.append((prev, inf))

    # --- 风控位：方向变了就用当前价重置基准，否则沿用上次的入场价与持仓极值
    changed_keys = {i["key"] for _, i in changed}
    first_keys = {i["key"] for i in first_run}
    levels = {}
    for inf in infos:
        k = inf["key"]
        prev = state.get(k) or {}
        is_long = inf["net"] > 0
        if k in changed_keys or k in first_keys:
            entry_px, peak_px = inf["px"], inf["px"]
            entry_bar = int(inf["bar_open_ms"])
        else:
            entry_px = prev.get("entry_px") or inf["px"]
            entry_bar = int(prev.get("entry_bar") or inf["bar_open_ms"])
            pk = _peak_since(bars_map[inf["symbol"]], entry_bar, is_long)
            peak_px = pk if pk else (prev.get("peak_px") or inf["px"])
        lv = compute_risk_levels(inf, cfg, entry_px, peak_px)
        # 建仓信息（邮件里要显示「当时的下单价」）
        lv["entry_bar"] = entry_bar
        lv["entry_dt"] = datetime.fromtimestamp(entry_bar / 1000, CST)
        lv["entry_new"] = (k in changed_keys or k in first_keys)
        levels[k] = lv
        e_px, e_day, e_hold = entry_info(lv, inf)
        log.info("%s %s: 止损提醒位=%s 爆仓价=%.0f 建仓价=%s（%s，持有 %d 天）",
                 inf["symbol"], inf["name"],
                 f"{lv['stop']:,.0f}" if lv["stop"] else "不设", lv["liq"],
                 f"{e_px:,.2f}" if e_px else "无持仓", e_day or "-", e_hold)

    # BTC 单跑参照也走同一套状态跟踪，③ 段才能给出「本次要做什么」
    ref_group = None
    if btc_ref is not None:
        rk = btc_ref["key"]
        rprev = state.get(rk) or {}
        r_first = not rprev
        r_chg = (not r_first) and rprev.get("dirs") != btc_ref["dirs"]
        if r_first or r_chg:
            entry_px, peak_px = btc_ref["px"], btc_ref["px"]
            entry_bar = int(btc_ref["bar_open_ms"])
        else:
            entry_px = rprev.get("entry_px") or btc_ref["px"]
            entry_bar = int(rprev.get("entry_bar") or btc_ref["bar_open_ms"])
            pk = _peak_since(bars_map[btc_ref["symbol"]], entry_bar, btc_ref["net"] > 0)
            peak_px = pk if pk else (rprev.get("peak_px") or btc_ref["px"])
        lv = compute_risk_levels(btc_ref, cfg, entry_px, peak_px)
        lv["entry_bar"] = entry_bar
        lv["entry_dt"] = datetime.fromtimestamp(entry_bar / 1000, CST)
        lv["entry_new"] = (r_first or r_chg)
        levels[rk] = lv
        ref_group = ([btc_ref],
                     ([(rprev, btc_ref)] if r_chg else []),
                     ([btc_ref] if r_first else []))
        log.info("BTC 单跑参照：方向=%s 净敞口=%.3f 数量=%.6f（首次=%s 方向变化=%s）",
                 btc_ref["side"], btc_ref["net_expo"], btc_ref["qty"], r_first, r_chg)

    actions = (_build_actions_multi(infos, changed, first_run, cfg=cfg, ref=ref_group)
               if multi else _build_actions(infos, changed, first_run, cfg=cfg))

    # 邮件里重点展示哪一档：优先 email.primary_profile，其次 trade.profile，最后 B
    primary = (str((cfg.get("email") or {}).get("primary_profile") or "").strip().upper()
               or str((cfg.get("trade") or {}).get("profile") or "").strip().upper()
               or "B")
    if multi:
        if primary not in keys:
            primary = keys[0]
    elif primary not in [i.get("key") for i in infos]:
        primary = infos[0].get("key")

    if dry_run:
        log.info("[dry-run] 变化情况：首次=%s 变化=%s",
                 [i["key"] for i in first_run], [i["key"] for _, i in changed])
        if multi:
            print("\n" + format_multi_body(infos, "（dry-run，未发送）", "", levels,
                                           actions, primary, cfg, syms, btc_ref))
            html_txt = format_multi_body_html(infos, "（dry-run，未发送）", "", levels,
                                              actions, primary, cfg, syms, btc_ref)
        else:
            print("\n" + format_body(infos, "（dry-run，未发送）", levels=levels,
                                     actions=actions, primary=primary))
            html_txt = format_body_html(infos, "（dry-run，未发送）", levels=levels,
                                        actions=actions, primary=primary)
        # 顺手把 HTML 版落盘，方便在浏览器里看排版（不会发信、不会下单）
        try:
            with open("信号邮件预览.html", "w", encoding="utf-8") as f:
                f.write(html_txt)
            log.info("[dry-run] HTML 预览已写入 信号邮件预览.html（双击即可查看）")
        except Exception as e:                                   # noqa: BLE001
            log.warning("[dry-run] HTML 预览写入失败：%s", e)
        return 0

    # --- 无变化：不发信，但要把持仓极值（吊灯用）更新到状态里
    #     加了 --daily 时例外：即使没变化也发一封日报，作为「脚本活着」的回执
    #     BTC 单跑参照若首次跟踪 / 方向变化，也算有变化（否则它的「首次」会被吞掉）
    ref_pending = bool(ref_group and (ref_group[1] or ref_group[2]))
    if not changed and not first_run and not ref_pending and not force and not daily:
        log.info("无方向变化，跳过推送")
        for inf in infos:
            state[inf["key"]] = _state_entry(inf, levels[inf["key"]], state.get(inf["key"]))
        save_state(state)
        return 0

    if changed:
        parts = []
        for prev, inf in changed:
            tag = f"{inf['coin']} " if multi else f"{inf['key']}档 "
            parts.append(f"{tag}{_side_txt(prev.get('net_expo', 0) or 0)}"
                         f"→{_side_txt(inf['net'])}")
        title = "方向变化：" + "、".join(parts)
        note = ""
    elif force:
        title = "手动触发（强制推送）"
        note = "本次为手动强制推送，不代表方向发生变化。"
    elif daily:
        title = "每日日报 · 方向未变"
        note = ("今日 5 组参数方向与上一交易日完全一致，未产生新的交易信号。\n"
                "本邮件为每日例行回执，用来确认脚本仍在正常运行。")
    elif ref_pending:
        title = "BTC 单跑参照 · 状态更新"
        note = ("多标的方案本次无方向变化。下方 ③ 是「BTC 单跑参照」的仓位动作，"
                "此后它与四币一样，只在方向变化时才更新。")
    else:
        title = "初始化 · 当前持仓状态"
        note = ("这是本脚本的首次运行，仅用于告知当前状态，不代表发生了交易信号。\n"
                "此后只在方向发生变化时才会推送。")

    if first_run:
        if multi:
            note += "\n首次运行：" + "、".join(f"{i['coin']}({i['pk']}档)" for i in first_run)
        else:
            note += "\n首次运行档位：" + "、".join(i["key"] for i in first_run)

    subject = f"【{head}信号{SUBJECT_SUFFIX}】{title}"
    if multi:
        body = format_multi_body(infos, title, note.strip(), levels, actions,
                                 primary, cfg, syms, btc_ref)
        html = format_multi_body_html(infos, title, note.strip(), levels, actions,
                                      primary, cfg, syms, btc_ref)
    else:
        body = format_body(infos, title, note.strip(), levels=levels, actions=actions,
                           primary=primary)
        html = format_body_html(infos, title, note.strip(), levels=levels,
                                actions=actions, primary=primary)

    send_email(cfg["email"], subject, body, log, html=html)
    log.info("邮件已发送：%s", subject)

    for inf in infos:
        k = inf["key"]
        rec = _state_entry(inf, levels[k], state.get(k))
        rec["last_sent"] = datetime.now(CST).isoformat()
        state[k] = rec
    if btc_ref is not None:
        rk = btc_ref["key"]
        rec = _state_entry(btc_ref, levels[rk], state.get(rk))
        rec["last_sent"] = datetime.now(CST).isoformat()
        state[rk] = rec
    save_state(state)
    log.info("状态已更新 -> %s", STATE_PATH)
    return 0



# ============================================================ 价格触发的风控提醒
PRICE_API = "https://data-api.binance.vision/api/v3/ticker/price"


def fetch_price(symbol, retries=3, log=None):
    """取实时最新价（用于风控提醒；信号仍只用已收盘K线）。"""
    url = f"{PRICE_API}?symbol={symbol}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signal-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
                return float(json.loads(r.read().decode("utf-8"))["price"])
        except Exception as e:                                   # noqa: BLE001
            last_err = e
            if log:
                log.warning("取实时价失败（第 %d/%d 次）：%s", attempt, retries, e)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"取实时价失败：{last_err}")


def _collect_levels(infos, cfg, state, bars_map):
    levels = {}
    for inf in infos:
        k = inf["key"]
        prev = state.get(k) or {}
        is_long = inf["net"] > 0
        entry_px = prev.get("entry_px") or inf["px"]
        entry_bar = int(prev.get("entry_bar") or inf["bar_open_ms"])
        b = bars_map.get(inf["symbol"]) if isinstance(bars_map, dict) else bars_map
        pk = _peak_since(b, entry_bar, is_long) if b else None
        peak_px = pk if pk else (prev.get("peak_px") or inf["px"])
        lv = compute_risk_levels(inf, cfg, entry_px, peak_px)
        lv["entry_bar"] = entry_bar
        lv["entry_dt"] = datetime.fromtimestamp(entry_bar / 1000, CST)
        lv["entry_new"] = False
        levels[k] = lv
    return levels


def _alert_multi_html(infos, levels, fired, px_map, primary, syms, title):
    """多标的风控提醒邮件（HTML）。"""
    now = datetime.now(CST)
    prim = [i for i in infos if i["pk"] == primary]
    P = []
    P.append(_p(f"{now:%Y-%m-%d %H:%M}（北京时间）", 12, "#9aa0a6"))
    P.append(_box("⚠ 这是「价格触及」提醒，不是策略信号",
                  _p("策略本身不挂价格止损，是否操作由你决定。", 12, "#8a5a00")))
    if fired:
        P.append(_h("触发的提醒"))
        P.append(_ul(fired))
    P.append(_h(f"各币当前风控位（{primary} 档）"))
    rows = []
    for inf in prim:
        lv = levels.get(inf["key"], {})
        px = px_map.get(inf["symbol"], inf["px"])
        e_px, _eday, _ehold = entry_info(lv, inf)      # 注意：不能用 _h，会覆盖 HTML 辅助函数
        rows.append([inf["coin"], _side_short(inf["net"]),
                     f"{e_px:,.4g}" if e_px else "空仓", f"{px:,.4g}",
                     f"{lv['stop']:,.4g}" if lv.get("stop") else "不设",
                     f"{lv.get('liq', inf['liq']):,.4g}",
                     f"{(lv.get('liq', inf['liq']) / px - 1) * 100:+.1f}%"])
    P.append(_table(["币种", "方向", "建仓价", "现价", "止损位", "爆仓价", "距爆仓"], rows,
                    ["left", "center", "right", "right", "right", "right", "right"]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:12px;">'
             f'生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）</div>')
    head = "/".join(coin_name(s) for s in syms)
    return _shell(f"{head} 风控{SUBJECT_SUFFIX}", title, "".join(P))


def check_alerts(keys, dry_run=False, force=False, log=None, symbols=None):
    """价格触发的风控提醒。只发提醒，不改变策略状态，也不会自动下单。"""
    log = log or setup_logger("alert")
    cfg = load_config()
    syms = symbols_of(cfg, override=symbols)
    multi = len(syms) >= 2
    n_sym = len(syms)
    risk = cfg.get("risk", {}) or {}
    if risk.get("stop_mode", "none") == "none" and not float(risk.get("take_profit_pct", 0) or 0):
        log.info("config.json 里 risk.stop_mode=none 且未设止盈，没有价格提醒可做")
        return 0

    bars_map, px_map = {}, {}
    for s in syms:
        bars_map[s] = keep_closed(fetch_klines(s, cfg["interval"],
                                               cfg.get("kline_limit", 1000), 3, log))
        px_map[s] = fetch_price(s, 3, log)
        log.info("%s 实时价 %.6g", s, px_map[s])

    infos = []
    for s in syms:
        for k in keys:
            prof = cfg["profiles"][k]
            eq = (float(prof["equity"]) / n_sym) if multi else None
            inf = compute_profile(prof, bars_map[s], cfg, symbol=s, equity_override=eq)
            inf["key"] = state_key(s, k, multi)
            inf["pk"] = k
            inf["coin"] = coin_name(s)
            infos.append(inf)

    state = load_state()
    levels = _collect_levels(infos, cfg, state, bars_map)

    warn_pct = float(risk.get("liq_warn_pct", 0.10))
    fired = []
    for inf in infos:
        k, lv = inf["key"], levels[inf["key"]]
        px = px_map.get(inf["symbol"], inf["px"])
        is_long = lv["is_long"]
        tag = f"{inf['coin']}·{inf['pk']}" if multi else f"{k}档"
        if abs(lv["liq"] / px - 1.0) <= warn_pct:
            fired.append(f"{tag}接近爆仓")
        if lv.get("stop"):
            if (is_long and px <= lv["stop"]) or ((not is_long) and px >= lv["stop"]):
                fired.append(f"{tag}触及止损位")
        if lv.get("tp"):
            if (is_long and px >= lv["tp"]) or ((not is_long) and px <= lv["tp"]):
                fired.append(f"{tag}触及止盈位")

    log.info("触发检查结果：%s", fired or "无")

    today = datetime.now(CST).strftime("%Y-%m-%d")
    rec = state.get("_alerts") or {}
    if rec.get("date") != today:
        rec = {"date": today, "count": 0, "fired": []}
    new_fired = [f for f in fired if f not in rec["fired"]]

    if not new_fired and not force:
        log.info("无新的风控触发（今日已提醒：%s），跳过", rec["fired"] or "无")
        state["_alerts"] = rec
        save_state(state)
        return 0

    max_n = int(risk.get("max_alerts_per_day", 3))
    if rec["count"] >= max_n and not force:
        log.warning("今日提醒次数已达上限 %d，跳过", max_n)
        state["_alerts"] = rec
        save_state(state)
        return 0

    primary = (str((cfg.get("email") or {}).get("primary_profile") or "").strip().upper()
               or str((cfg.get("trade") or {}).get("profile") or "").strip().upper()
               or "B")
    if multi:
        if primary not in keys:
            primary = keys[0]
    elif primary not in [i.get("key") for i in infos]:
        primary = infos[0].get("key")

    head = ("/".join(coin_name(s) for s in syms) if multi else coin_name(syms[0]))
    hit = new_fired or fired or ["强制测试"]
    subject = f"【{head}风控{SUBJECT_SUFFIX}】" + "、".join(hit)

    if multi:
        body = format_multi_body(infos, "风控提醒", "触发：" + "、".join(hit),
                                 levels, [], primary, cfg, syms, None)
        html = _alert_multi_html(infos, levels, hit, px_map, primary, syms,
                                 "风控提醒 · " + "、".join(hit))
    else:
        px = px_map[syms[0]]
        body = format_alert_body(infos, levels, hit, px, log, primary=primary)
        html = format_alert_body_html(infos, levels, hit, px, primary=primary)

    if dry_run:
        log.info("[dry-run] 不发送。触发=%s", new_fired or fired)
        print("\n" + body)
        return 0

    send_email(cfg["email"], subject, body, log, html=html)
    log.info("风控提醒已发送：%s", subject)

    rec["count"] += 1
    rec["fired"] = list(dict.fromkeys(rec["fired"] + (new_fired or fired)))
    state["_alerts"] = rec
    save_state(state)
    return 0


def main_alert(argv, keys):
    log = setup_logger("alert")
    try:
        return check_alerts(keys, dry_run="--dry-run" in argv,
                            force="--force" in argv, log=log,
                            symbols=_symbols_from_argv(argv))
    except Exception as e:                                       # noqa: BLE001
        log.error("风控提醒运行失败：%s", e, exc_info=True)
        return 1


def _symbols_from_argv(argv):
    """从命令行解析交易对覆盖（不改配置文件）：
       --symbols BTCUSDT,ETHUSDT   临时指定一组交易对
       --single                    强制单标的（用 config.symbol）
       --multi                     强制多标的（用 config.symbols）
    返回列表；返回 None 表示不覆盖，按 config 的 run_mode 决定。
    """
    for i, a in enumerate(argv):
        if a == "--symbols" and i + 1 < len(argv):
            return [x.strip() for x in argv[i + 1].split(",") if x.strip()]
        if a.startswith("--symbols="):
            return [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
    if "--single" in argv:
        return [str(load_config().get("symbol") or "BTCUSDT")]
    if "--multi" in argv:
        return [str(s) for s in (load_config().get("symbols") or [])]
    return None


def main(argv, keys):
    force = "--force" in argv
    dry = "--dry-run" in argv
    test = "--test" in argv
    daily = "--daily" in argv
    symbols = _symbols_from_argv(argv)
    tag = "live_" + "".join(keys)
    log = setup_logger(tag)
    try:
        return run(keys, force=force, dry_run=dry, test=test, daily=daily, log=log,
                   symbols=symbols)
    except Exception as e:                                       # noqa: BLE001
        log.error("运行失败：%s", e, exc_info=True)
        return 1
