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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "signal_state.json")
LOG_DIR = os.path.join(HERE, "logs")

API_BASE = "https://data-api.binance.vision/api/v3/klines"
CST = timezone(timedelta(hours=8))          # 北京时间
MMR = 0.005                                  # 维持保证金率（与回测一致）


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
def compute_profile(profile, bars, cfg):
    """算出某档位（A/B/C）在最后一根已收盘K线上的目标仓位。"""
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
    equity = float(profile["equity"])
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
        "px": px, "atr": a, "dvol": dvol, "expo": expo, "L": L,
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


# ============================================================ 邮件
def send_email(email_cfg, subject, body, log):
    retries = int(email_cfg.get("max_retries", 3))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

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
def _arrow(d):
    return "多 ▲" if d > 0 else "空 ▼"


def _side_txt(net):
    return "多" if net > 0 else ("空" if net < 0 else "空仓")


def format_body(infos, title, note="", levels=None, actions=None, prev_map=None):
    """infos: compute_profile 结果列表；levels: {key: 风控位}；actions: [手动操作说明]。"""
    now = datetime.now(CST)
    ref = infos[0]
    levels = levels or {}
    L = []
    L.append(f"【BTC 趋势信号】{title}")
    L.append("=" * 50)
    L.append(f"触发时间：{now:%Y-%m-%d %H:%M}（北京时间）")
    L.append(f"数据K线：{ref['bar_dt']:%Y-%m-%d} 日线（已收盘）")
    L.append(f"当前价格：{ref['px']:,.0f} USDT")
    L.append(f"ATR14：{ref['atr']:,.0f}　日波动率：{ref['dvol']*100:.2f}%")
    L.append("=" * 50)
    if note:
        L.append(note)
        L.append("")

    # ---------- 需要你手动执行的操作（最显眼的位置） ----------
    if actions:
        L.append("★ 需要你手动执行的操作")
        L.append("-" * 50)
        for line in actions:
            L.append(f"  {line}")
        L.append("")

    # ---------- 各档位详情 ----------
    for inf in infos:
        k = inf.get("key")
        lv = levels.get(k, {})
        L.append(f"▌{inf['name']}（目标日波动 {inf['vt']*100:.0f}%）")
        L.append(f"  方向：{_arrow(inf['net'])}　净敞口：{inf['net_expo']:.2f}×"
                 f"　名义：{abs(inf['net']):,.0f} USDT")
        if abs(inf["net"]) > 1e-9:
            L.append(f"  数量：{inf['qty']:.6f} BTC　占用保证金：{inf['margin']:,.0f} USDT"
                     f"　杠杆：{inf['L']:.0f}× 逐仓")
        else:
            L.append("  5 组参数方向对冲，净仓位为 0（空仓等待）")
        det = "　".join(f"{sv['fast']}/{sv['slow']}→{'多' if sv['dir'] > 0 else '空'}"
                        for sv in inf["sleeves"])
        L.append(f"  参数明细：{det}")

        # ---- 风控位 ----
        if lv:
            L.append(f"  ── 风控位（仅供提醒）")
            if lv.get("stop"):
                dist = (lv["stop"] / inf["px"] - 1) * 100
                L.append(f"     止损提醒位：{lv['stop']:,.0f}（{lv['stop_kind']}，"
                         f"距现价 {dist:+.1f}%）")
            else:
                L.append(f"     止损提醒位：不设（{lv.get('stop_kind', '')}）")
            L.append(f"     爆仓价：{lv['liq']:,.0f}（距现价 {lv['liq']/inf['px']*100-100:+.1f}%，"
                     f"这是 3× 逐仓的硬底）")
            if lv.get("tp"):
                L.append(f"     止盈提醒位：{lv['tp']:,.0f}"
                         f"（距现价 {(lv['tp']/inf['px']-1)*100:+.1f}%）")
            else:
                L.append("     止盈提醒位：不设（靠方向反转离场，让利润奔跑）")
        L.append("")

    # ---------- 止盈止损机制说明 ----------
    L.append("-" * 50)
    L.append("【止盈止损是怎么工作的】")
    L.append("  本策略的止盈止损是「信号触发」，不是「价格触发」：")
    L.append("   · 止损 = 方向反转。EMA快线下穿慢线时，邮件会告诉你平仓/反手，")
    L.append("            这就是策略的止损动作，不需要另外挂止损单。")
    L.append("   · 止盈 = 同样靠方向反转。趋势走完才离场，让利润奔跑——")
    L.append("            回测证明固定止盈会砍掉贡献全部收益的那几笔大单。")
    L.append("   · 硬底 = 3× 逐仓的爆仓价（见上方风控位），距现价约 -33%。")
    L.append("")
    L.append("  如果你想要「价格触发」的提醒（到价就给你发信、由你手动操作），")
    L.append("  运行 alert.py 即可，它按 config.json 的 risk 段判断是否发信。")
    L.append("")
    L.append("执行提示：")
    L.append("  · 本信号在K线收盘后确认，请于下一根K线开盘时调仓")
    L.append("  · 仓位按「目标日波动 ÷ 当前日波动」反推，波动变大时会自动缩仓")
    L.append("  · 若 5 组参数方向不一致，净仓位会变小——这是正常的，不要手动放大")
    L.append("")
    L.append("风险提示：本邮件由量化脚本自动生成，仅为研究参考，不构成投资建议。")
    L.append(f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(L)


def format_alert_body(infos, levels, fired, px, log):
    """价格触发的风控提醒邮件。"""
    now = datetime.now(CST)
    ref = infos[0]
    L = []
    L.append("【BTC 风控提醒】" + "、".join(fired))
    L.append("=" * 50)
    L.append(f"触发时间：{now:%Y-%m-%d %H:%M}（北京时间）")
    L.append(f"当前价格：{px:,.0f} USDT（实时）")
    L.append(f"日线收盘参考：{ref['px']:,.0f}　日波动率：{ref['dvol']*100:.2f}%")
    L.append("=" * 50)
    L.append("⚠ 这是价格触发的风控提醒，不是策略信号。")
    L.append("  策略本身不设价格止损，是否执行由你决定。")
    L.append("")
    for inf in infos:
        k = inf.get("key")
        lv = levels.get(k, {})
        if not lv:
            continue
        L.append(f"▌{inf['name']}　当前方向：{_side_txt(inf['net'])}"
                 f"　持仓 {inf['qty']:.6f} BTC")
        if lv.get("stop"):
            L.append(f"     止损提醒位：{lv['stop']:,.0f}"
                     f"　现价距此 {(lv['stop']/px-1)*100:+.1f}%")
        L.append(f"     爆仓价：{lv['liq']:,.0f}　现价距爆仓 {(lv['liq']/px-1)*100:+.1f}%")
        if lv.get("tp"):
            L.append(f"     止盈提醒位：{lv['tp']:,.0f}"
                     f"　现价距此 {(lv['tp']/px-1)*100:+.1f}%")
        L.append("")
    L.append("-" * 50)
    L.append("回测提醒：加固定止损会让长期收益大幅下降（实测 BTC 上吊灯 3×ATR 把")
    L.append("114.89× 打到 2.29×），且最优止损距离在跨资产复核时失效（SOL 上从")
    L.append("0.64× 掉到 0.16×）。所以本提醒的定位是「极端行情保险」，不是日常操作。")
    L.append("")
    L.append("风险提示：本邮件由量化脚本自动生成，不构成投资建议。")
    L.append(f"生成时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(L)



def format_test_body(cfg, bars):
    now = datetime.now(CST)
    last = bars[-1]
    L = [
        "【信号推送 · 测试邮件】",
        "=" * 46,
        "这是一封测试邮件，用于验证邮件配置是否正确。",
        "收到本邮件说明 SMTP 通道畅通，策略脚本可以正常推送信号。",
        "=" * 46,
        f"发送时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）",
        f"数据源：币安现货公开接口（data-api.binance.vision）",
        f"交易对：{cfg['symbol']}　周期：{cfg['interval']}",
        f"最新已收盘K线：{datetime.fromtimestamp(last['open_time']/1000, CST):%Y-%m-%d}"
        f"　收盘价 {last['close']:,.0f} USDT",
        f"已获取历史K线：{len(bars)} 根",
        "=" * 46,
        "接下来脚本会在【方向发生变化】时自动给你发信号邮件。",
        "平时没有变化就不发信，避免噪音。",
    ]
    return "\n".join(L)


def _peak_since(bars, entry_open_ms, is_long):
    """开仓以来的最高价（多头）/ 最低价（空头），用于吊灯移动止损。"""
    sub = [b for b in bars if b["open_time"] >= entry_open_ms]
    if not sub:
        return None
    return max(b["high"] for b in sub) if is_long else min(b["low"] for b in sub)


def _build_actions(infos, changed, first_run):
    """把「方向变化」翻译成明确的手动操作清单。"""
    out = []
    changed_keys = {i["key"] for _, i in changed}
    first_keys = {i["key"] for i in first_run}
    for inf in infos:
        k = inf["key"]
        if k in changed_keys:
            prev = next(p for p, i in changed if i["key"] == k)
            old_side = _side_txt(prev.get("net_expo", 0) or 0)
            old_qty = float(prev.get("qty") or 0)
            new_side = _side_txt(inf["net"])
            parts = []
            if old_qty > 0:
                parts.append(f"平掉{old_side}头 {old_qty:.6f} BTC")
            else:
                parts.append("原为空仓")
            if abs(inf["net"]) > 1e-9:
                parts.append(f"开{new_side}头 {inf['qty']:.6f} BTC")
            else:
                parts.append("本次不建仓")
            out.append(f"{k}档（{inf['name']}）：" + " → ".join(parts))
        elif k in first_keys:
            if abs(inf["net"]) > 1e-9:
                out.append(f"{k}档（{inf['name']}）：开{_side_txt(inf['net'])}头 "
                           f"{inf['qty']:.6f} BTC（首次建仓）")
            else:
                out.append(f"{k}档（{inf['name']}）：本次空仓")
    return out


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


# ============================================================ 主流程
def run(keys, force=False, dry_run=False, test=False, log=None):
    log = log or setup_logger("live")
    cfg = load_config()

    log.info("启动：profiles=%s force=%s dry_run=%s test=%s", keys, force, dry_run, test)

    bars = keep_closed(fetch_klines(cfg["symbol"], cfg["interval"],
                                    cfg.get("kline_limit", 1000),
                                    int(cfg["email"].get("max_retries", 3)), log))
    if len(bars) < 300:
        raise RuntimeError(f"已收盘K线只有 {len(bars)} 根，不足以计算 EMA60，请检查数据源")
    staleness = (datetime.now(timezone.utc) - datetime.fromtimestamp(bars[-1]["close_time"] / 1000,
                                                                    timezone.utc)).days
    log.info("数据就绪：%d 根，最新已收盘 %s，滞后 %d 天",
             len(bars), datetime.fromtimestamp(bars[-1]["open_time"] / 1000, CST).date(), staleness)
    if staleness > int(cfg.get("max_bar_staleness_days", 3)):
        log.warning("最新K线滞后 %d 天，数据源可能异常", staleness)

    # --- 测试邮件
    if test:
        subject = "【BTC 信号】测试邮件 · 邮件通道验证"
        body = format_test_body(cfg, bars)
        if dry_run:
            log.info("[dry-run] 不实际发送。正文如下：\n%s", body)
            return 0
        send_email(cfg["email"], subject, body, log)
        log.info("测试邮件已发送至 %s", cfg["email"]["to"])
        return 0

    # --- 计算各档位
    infos = []
    for k in keys:
        inf = compute_profile(cfg["profiles"][k], bars, cfg)
        inf["key"] = k
        infos.append(inf)
        log.info("%s: 方向=%s 净敞口=%.3f 名义=%.0f 参数明细=%s",
                 inf["name"], inf["side"], inf["net_expo"], abs(inf["net"]), inf["dirs"])

    state = load_state()
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
        else:
            entry_px = prev.get("entry_px") or inf["px"]
            pk = _peak_since(bars, prev.get("entry_bar") or inf["bar_open_ms"], is_long)
            peak_px = pk if pk else (prev.get("peak_px") or inf["px"])
        levels[k] = compute_risk_levels(inf, cfg, entry_px, peak_px)
        lv = levels[k]
        log.info("%s: 止损提醒位=%s 爆仓价=%.0f", inf["name"],
                 f"{lv['stop']:,.0f}" if lv["stop"] else "不设", lv["liq"])

    actions = _build_actions(infos, changed, first_run)

    if dry_run:
        log.info("[dry-run] 变化情况：首次=%s 变化=%s",
                 [i["key"] for i in first_run], [i["key"] for _, i in changed])
        print("\n" + format_body(infos, "（dry-run，未发送）", levels=levels, actions=actions))
        return 0

    # --- 无变化：不发信，但要把持仓极值（吊灯用）更新到状态里
    if not changed and not first_run and not force:
        log.info("无方向变化，跳过推送")
        for inf in infos:
            state[inf["key"]] = _state_entry(inf, levels[inf["key"]], state.get(inf["key"]))
        save_state(state)
        return 0

    if changed:
        parts = []
        for prev, inf in changed:
            parts.append(f"{inf['key']}档 {_side_txt(prev.get('net_expo', 0) or 0)}"
                         f"→{_side_txt(inf['net'])}")
        title = "方向变化：" + "、".join(parts)
        note = ""
    elif force:
        title = "手动触发（强制推送）"
        note = "本次为手动强制推送，不代表方向发生变化。"
    else:
        title = "初始化 · 当前持仓状态"
        note = ("这是本脚本的首次运行，仅用于告知当前状态，不代表发生了交易信号。\n"
                "此后只在方向发生变化时才会推送。")

    if first_run:
        note += "\n首次运行档位：" + "、".join(i["key"] for i in first_run)

    subject = f"【BTC 信号】{title}"
    body = format_body(infos, title, note.strip(), levels=levels, actions=actions)

    send_email(cfg["email"], subject, body, log)
    log.info("邮件已发送：%s", subject)

    for inf in infos:
        k = inf["key"]
        rec = _state_entry(inf, levels[k], state.get(k))
        rec["last_sent"] = datetime.now(CST).isoformat()
        state[k] = rec
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


def _collect_levels(infos, cfg, state, bars):
    levels = {}
    for inf in infos:
        k = inf["key"]
        prev = state.get(k) or {}
        is_long = inf["net"] > 0
        entry_px = prev.get("entry_px") or inf["px"]
        pk = _peak_since(bars, prev.get("entry_bar") or inf["bar_open_ms"], is_long)
        peak_px = pk if pk else (prev.get("peak_px") or inf["px"])
        levels[k] = compute_risk_levels(inf, cfg, entry_px, peak_px)
    return levels


def check_alerts(keys, dry_run=False, force=False, log=None):
    """价格触发的风控提醒。只发提醒，不改变策略状态，也不会自动下单。"""
    log = log or setup_logger("alert")
    cfg = load_config()
    risk = cfg.get("risk", {}) or {}
    if risk.get("stop_mode", "none") == "none" and not float(risk.get("take_profit_pct", 0) or 0):
        log.info("config.json 里 risk.stop_mode=none 且未设止盈，没有价格提醒可做")
        return 0

    bars = keep_closed(fetch_klines(cfg["symbol"], cfg["interval"],
                                    cfg.get("kline_limit", 1000), 3, log))
    px = fetch_price(cfg["symbol"], 3, log)
    log.info("实时价 %.2f", px)

    infos = []
    for k in keys:
        inf = compute_profile(cfg["profiles"][k], bars, cfg)
        inf["key"] = k
        infos.append(inf)

    state = load_state()
    levels = _collect_levels(infos, cfg, state, bars)

    warn_pct = float(risk.get("liq_warn_pct", 0.10))
    fired = []
    for inf in infos:
        k, lv = inf["key"], levels[inf["key"]]
        is_long = lv["is_long"]
        if abs(lv["liq"] / px - 1.0) <= warn_pct:
            fired.append(f"{k}档接近爆仓")
        if lv.get("stop"):
            if (is_long and px <= lv["stop"]) or ((not is_long) and px >= lv["stop"]):
                fired.append(f"{k}档触及止损位")
        if lv.get("tp"):
            if (is_long and px >= lv["tp"]) or ((not is_long) and px <= lv["tp"]):
                fired.append(f"{k}档触及止盈位")

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

    subject = "【BTC 风控提醒】" + "、".join(new_fired or fired or ["强制测试"])
    body = format_alert_body(infos, levels, new_fired or fired or ["强制测试"], px, log)

    if dry_run:
        log.info("[dry-run] 不发送。触发=%s", new_fired or fired)
        print("\n" + body)
        return 0

    send_email(cfg["email"], subject, body, log)
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
                            force="--force" in argv, log=log)
    except Exception as e:                                       # noqa: BLE001
        log.error("风控提醒运行失败：%s", e, exc_info=True)
        return 1


def main(argv, keys):
    force = "--force" in argv
    dry = "--dry-run" in argv
    test = "--test" in argv
    tag = "live_" + "".join(keys)
    log = setup_logger(tag)
    try:
        return run(keys, force=force, dry_run=dry, test=test, log=log)
    except Exception as e:                                       # noqa: BLE001
        log.error("运行失败：%s", e, exc_info=True)
        return 1
