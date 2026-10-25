# -*- coding: utf-8 -*-
"""
BTC 趋势策略实盘监测器（选项 B）
================================
规则：
  4h EMA50 > EMA200 且 收盘 > EMA200  -> 做多
  4h EMA50 < EMA200 且 收盘 < EMA200  -> 做空
  其余                                -> 空仓
风控（回测验证）：
  止损 = 8 × ATR(14)（1小时）
  被止损后冷却 24 根 1小时才允许再进场
  不设固定止盈（回测证明固定止盈在 2x 杠杆下必然归零）
  趋势失效（EMA 状态反转）-> 平仓

本脚本只做「信号监测 + 邮件推送 + 纸上权益跟踪」，不自动下单。
需要自动下单请自行接交易所 API（脚本里留了 on_signal 钩子）。

依赖：仅 Python 标准库（urllib / smtplib / json），无需 pip install。
"""
import argparse
import datetime
import io
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "monitor.log")
RECENT_PATH = os.path.join(HERE, "recent_trades.json")
CST = datetime.timezone(datetime.timedelta(hours=8))

# data-api.binance.vision 是币安官方公开行情镜像，通常比 api.binance.com 更稳，放第一位
BINANCE = ["https://data-api.binance.vision", "https://api.binance.com",
           "https://api1.binance.com", "https://api2.binance.com"]
MS = {"1h": 3600_000, "4h": 4 * 3600_000, "1d": 86400_000}


# ------------------------------------------------------------------ 工具
def log(msg):
    line = f"[{datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cfg():
    if not os.path.exists(CFG_PATH):
        raise SystemExit(f"找不到配置文件 {CFG_PATH}，请先运行 install.bat")
    with io.open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with io.open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"mode": "flat", "entry_price": 0.0, "entry_time": 0,
            "stop_price": 0.0, "cooldown_until": 0, "equity": 0.0,
            "trades": 0, "last_email": 0, "trade_log": []}


def _seed_trade_log():
    """首次运行时用 recent_trades.json（回测最近几笔）作为交易记录种子。"""
    try:
        with io.open(RECENT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _log_close(s, dir_, entry_px, entry_t, exit_px, exit_t, why, ret_pct):
    """把一笔已平仓交易记入 trade_log（保留最近 20 笔）。"""
    log = s.setdefault("trade_log", [])
    log.append({"dir": dir_, "entry_px": entry_px, "entry_t": entry_t,
                "exit_px": exit_px, "exit_t": exit_t, "why": why, "ret_pct": ret_pct})
    if len(log) > 20:
        del log[:-20]


def _action_tag(s, want):
    """标题用的动作标签：需开空/需开多/需平仓/无需操作·继续持有/无需操作·空仓观望。"""
    mode = s.get("mode", "flat")
    if mode in ("long", "short") and want == mode:
        return "无需操作·继续持有"
    if mode in ("long", "short") and want != mode:
        return "需平仓"
    if mode == "flat" and want == "long":
        return "需开多"
    if mode == "flat" and want == "short":
        return "需开空"
    return "无需操作·空仓观望"


def save_state(s):
    """原子写状态文件；失败时退化为直接写，避免计划任务并发把状态写丢。"""
    data = json.dumps(s, ensure_ascii=False, indent=1)
    tmp = STATE_PATH + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        if os.path.exists(tmp):
            os.replace(tmp, STATE_PATH)
        return
    except OSError:
        pass
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    try:
        with io.open(STATE_PATH, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError as e:
        log(f"写入 state.json 失败: {e}")


# ------------------------------------------------------------------ 行情
def http_json(url, tries=3, timeout=10, deadline=None):
    """拉取 URL 并解析 JSON。deadline 传入「绝对截止时间戳」时，超时即跳出，避免无限等待。"""
    last = None
    for k in range(tries):
        if deadline is not None and time.time() > deadline:
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(0.5 * (k + 1))
    raise last


def klines(symbol, interval, limit=400, closed_only=True, provider=None,
           _budget=35.0):
    """带镜像兜底的 K 线拉取。closed_only=True 时剔除尚未收盘的 K 线（防前视）。

    _budget：本轮拉取的总时间预算（秒）。网络不可达时整体在预算内失败退出，
    让上层重试脚本可以及时重试，而不是卡死数分钟。
    """
    if provider is not None:
        return provider(symbol, interval, limit, closed_only)
    deadline = time.time() + _budget
    last = None
    for host in BINANCE:
        if time.time() > deadline:
            break
        url = f"{host}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        try:
            d = http_json(url, tries=2, deadline=deadline)
            if isinstance(d, list) and d:
                now_ms = int(time.time() * 1000)
                bars = []
                for r in d:
                    ct = int(r[6])
                    if closed_only and ct > now_ms:      # 未收盘 -> 丢弃
                        continue
                    bars.append({"t": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                                 "low": float(r[3]), "close": float(r[4]), "v": float(r[5])})
                if bars:
                    return bars
        except Exception as e:                      # noqa: BLE001
            last = e
    raise RuntimeError(f"拉取 {symbol} {interval} K线失败: {last}")


def last_price(symbol, provider=None, _budget=20.0):
    """实时价格（用于止损判断，不用于指标计算）。"""
    if provider is not None:
        return provider(symbol)
    deadline = time.time() + _budget
    last = None
    for host in BINANCE:
        if time.time() > deadline:
            break
        try:
            d = http_json(f"{host}/api/v3/ticker/price?symbol={symbol}",
                          tries=2, deadline=deadline)
            return float(d["price"])
        except Exception as e:                      # noqa: BLE001
            last = e
    raise RuntimeError(f"拉取 {symbol} 实时价格失败: {last}")


# ------------------------------------------------------------------ 指标
def ema(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2.0 / (n + 1)
    s = sum(vals[:n]) / n
    out = [None] * (n - 1) + [s]
    for v in vals[n:]:
        s = v * k + s * (1 - k)
        out.append(s)
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + (d if d > 0 else 0.0)) / n
        al = (al * (n - 1) + (-d if d < 0 else 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def atr_wilder(bars, n=14):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = [bars[0]["high"] - bars[0]["low"]]
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - p["close"]),
                       abs(b["low"] - p["close"])))
    s = sum(trs[1:n + 1]) / n
    out[n] = s
    for i in range(n + 1, len(bars)):
        s = (s * (n - 1) + trs[i]) / n
        out[i] = s
    return out


# ------------------------------------------------------------------ 邮件
def send_email(cfg, subject, body, html=False):
    e = cfg.get("email", {})
    if not e.get("enabled"):
        log("邮件未启用，跳过推送")
        return False
    # 发件人：兼容 sender / from；收件人：兼容 数组 / 单个字符串
    sender = e.get("sender") or e.get("from") or ""
    to = e.get("to")
    if isinstance(to, (list, tuple)):
        recipients = [str(x).strip() for x in to if str(x).strip()]
    else:
        recipients = [str(to).strip()] if to else []
    pwd = os.environ.get("STRATEGY_EMAIL_PASSWORD") or e.get("password", "")
    if not pwd:
        log("未配置邮箱授权码（config.json 的 email.password 或环境变量 STRATEGY_EMAIL_PASSWORD），跳过推送")
        return False
    if not sender or not recipients:
        log("邮件收发件人配置不完整（sender / to），跳过推送")
        return False
    ctype = "text/html" if html else "text/plain"
    msg = ("From: %s\r\nTo: %s\r\nSubject: %s\r\nContent-Type: %s; "
           "charset=utf-8\r\n\r\n%s" % (sender, ", ".join(recipients), subject, ctype, body))
    server = e.get("server", "smtp.163.com")
    port = int(e.get("port", 465))
    use_ssl = bool(e.get("use_ssl", port == 465))
    tries = int(e.get("max_retries", 3))
    for k in range(tries):
        try:
            if use_ssl:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(server, port, timeout=25, context=ctx,
                                      local_hostname="localhost") as s:
                    s.login(sender, pwd)
                    s.sendmail(sender, recipients, msg.encode("utf-8"))
            else:
                with smtplib.SMTP(server, port, timeout=25, local_hostname="localhost") as s:
                    s.starttls()
                    s.login(sender, pwd)
                    s.sendmail(sender, recipients, msg.encode("utf-8"))
            log(f"邮件已发送 -> {', '.join(recipients)} : {subject}")
            return True
        except Exception as ex:                     # noqa: BLE001
            log(f"邮件发送失败({k+1}/{tries}): {ex}")
            time.sleep(2.0 * (k + 1))
    return False


def fmt(v, d=2):
    return f"{v:,.{d}f}"


def _today_action(s, want, price, snap, cfg):
    """给出一句话「今日操作」建议。"""
    mode = s.get("mode", "flat")
    st = {"long": "做多", "short": "做空", "flat": "空仓"}
    if mode in ("long", "short") and want == mode:
        return f"继续持有{st[mode]}，止损 {fmt(s.get('stop_price', 0))}"
    if mode in ("long", "short") and want != mode:
        return f"平{st[mode]}（趋势反转），转为{st.get(want, '空仓')}"
    if mode == "flat" and want in ("long", "short"):
        d = 1 if want == "long" else -1
        atr = snap.get("atr") or 0.0
        stop = price - d * float(cfg.get("stop_atr", 8.0)) * atr
        return f"开{st[want]}：入场≈{fmt(price)}，止损≈{fmt(stop)}"
    return "空仓观望，暂无信号"


def _build_email_html(now, price, snap, s, want, lev, eq, events, cfg):
    """把信号整理成 HTML 表格邮件正文。"""
    def _st(v):
        return {"long": "做多", "short": "做空", "flat": "空仓"}.get(v, v)

    td = "padding:6px 10px;border:1px solid #e3e6e8;"
    th = td + "background:#f0f3f6;font-weight:600;text-align:left;"
    tdr = td + "text-align:right;"
    summary = [
        ("时间 (CST)", datetime.datetime.fromtimestamp(now / 1000, CST).strftime("%Y-%m-%d %H:%M")),
        ("BTC 价格", f"{fmt(price)} USDT"),
        ("4h EMA50 / EMA200", f"{fmt(snap['ema_fast'])} / {fmt(snap['ema_slow'])}"),
        ("应有状态", _st(want)),
        ("当前持仓", _st(s.get("mode", "flat"))),
        ("杠杆 / 名义", f"{lev:g}x / {fmt(eq * lev)} USDT"),
        ("纸上权益（从1000U滚仓）",
         f"起始 {fmt(float(cfg.get('initial_equity', 1000)))}U → 当前 {fmt(eq)}U（{eq / float(cfg.get('initial_equity', 1000)):.2f}x）"),
    ]
    if s.get("stop_price"):
        summary.append(("止损价", f"{fmt(s['stop_price'])}（距现价 {abs(price / s['stop_price'] - 1) * 100:.2f}%）"))
    elif s.get("mode", "flat") == "flat" and want in ("long", "short") and snap.get("atr"):
        # 空仓但有信号：直接给出「若按此方向开仓」的入场价与止损价
        _d = 1 if want == "long" else -1
        _stop = price - _d * float(cfg.get("stop_atr", 8.0)) * snap["atr"]
        summary.append((f"待开仓（{_st(want)}）", f"入场≈{fmt(price)}，止损≈{fmt(_stop)}"))
    if s.get("cooldown_until", 0) > now:
        left = (s["cooldown_until"] - now) / MS[snap["st_iv"]]
        summary.append(("冷却中", f"还剩 {left:.0f} 根 {snap['st_iv']}"))
    sum_html = "".join(
        f"<tr><td style='{th}'>{k}</td><td style='{td}'>{v}</td></tr>" for k, v in summary)

    ev_rows = ""
    for kind, m, px, ret, e in events:
        if kind == "ENTER":
            act, ret_txt = "开仓", "—"
        elif kind == "STOP":
            act, ret_txt = "止损平仓", f"{ret * 100:+.2f}%"
        else:
            act, ret_txt = "趋势失效平仓", f"{ret * 100:+.2f}%"
        color = "#c0392b" if (kind == "ENTER" or ret >= 0) else "#1e8449"
        ev_rows += (f"<tr><td style='{td}'>{act}</td><td style='{td}'>{_st(m)}</td>"
                    f"<td style='{tdr}'>{fmt(px)}</td>"
                    f"<td style='{tdr};color:{color}'>{ret_txt}</td>"
                    f"<td style='{tdr}'>{fmt(e)}</td></tr>")

    # 最近 3 笔交易（trade_log 末 3 笔，倒序）
    log = s.get("trade_log") or []
    recent_html = ""
    if log:
        rrows = ""
        for t in list(reversed(log[-3:])):
            d = "多" if t.get("dir") in ("long", 1) else "空"
            rp = float(t.get("ret_pct", 0.0))
            rc = "#c0392b" if rp >= 0 else "#1e8449"
            et = datetime.datetime.fromtimestamp(t["entry_t"] / 1000, CST).strftime("%m-%d %H:%M") if t.get("entry_t") else "—"
            xt = datetime.datetime.fromtimestamp(t["exit_t"] / 1000, CST).strftime("%m-%d %H:%M") if t.get("exit_t") else "—"
            rrows += (f"<tr><td style='{td}'>{d}</td><td style='{td}'>{et}</td>"
                      f"<td style='{tdr}'>{fmt(t.get('entry_px', 0))}</td>"
                      f"<td style='{td}'>{xt}</td><td style='{tdr}'>{fmt(t.get('exit_px', 0))}</td>"
                      f"<td style='{td}'>{t.get('why', '')}</td>"
                      f"<td style='{tdr};color:{rc}'>{rp:+.2f}%</td></tr>")
        recent_html = (
            "<h4 style='margin:10px 0 6px'>最近 3 笔交易</h4>"
            "<table style='border-collapse:collapse;font-size:13px'>"
            f"<tr><th style='{th}'>方向</th><th style='{th}'>入场时间</th>"
            f"<th style='{th};text-align:right'>入场价</th>"
            f"<th style='{th}'>出场时间</th><th style='{th};text-align:right'>出场价</th>"
            f"<th style='{th}'>原因</th><th style='{th};text-align:right'>收益</th></tr>{rrows}</table>")

    return (
        "<div style=\"font-family:'Microsoft YaHei',Arial,sans-serif;color:#222;font-size:13px\">"
        f"<h3 style='margin:0 0 10px'>BTC 趋势策略 · 信号通知（{lev:g}x 合约）</h3>"
        f"<div style='background:#eef7ff;border-left:4px solid #0b5fff;padding:8px 12px;margin-bottom:12px;font-size:14px'><b>今日操作：</b>{_today_action(s, want, price, snap, cfg)}</div>"
        f"<table style='border-collapse:collapse;font-size:13px;margin-bottom:14px'>{sum_html}</table>"
        f"{recent_html}"
        "<h4 style='margin:10px 0 6px'>本次动作</h4>"
        "<table style='border-collapse:collapse;font-size:13px'>"
        f"<tr><th style='{th}'>动作</th><th style='{th}'>方向</th>"
        f"<th style='{th};text-align:right'>成交价</th>"
        f"<th style='{th};text-align:right'>本笔收益</th>"
        f"<th style='{th};text-align:right'>权益(U)</th></tr>{ev_rows}</table>"
        "<p style='color:#888;font-size:12px;margin-top:12px'>"
        "纸上权益从 1000U 起、按本机实时信号滚仓（与历史回测的 491x 无关）。"
        "仅为策略信号，不构成投资建议；实际仓位请自行决定。</p></div>")


# ------------------------------------------------------------------ 主逻辑
def snapshot(cfg, provider=None, price_provider=None):
    """取数 + 算指标 + 判定应有状态。"""
    sym = cfg.get("symbol", "BTCUSDT")
    st_iv, ft_iv = cfg.get("signal_timeframe", "1h"), cfg.get("filter_timeframe", "4h")
    # 注意：EMA(n) 的种子是前 n 根的 SMA，K 线取太少会让种子点不同、
    # 残留误差污染 EMA 值。EMA200 至少取 1000 根（残留误差约 0.03%）。
    st = klines(sym, st_iv, 300, provider=provider)
    ft = klines(sym, ft_iv, 1000, provider=provider)
    c = [b["close"] for b in ft]
    ef = ema(c, int(cfg.get("ema_fast", 50)))
    es = ema(c, int(cfg.get("ema_slow", 200)))
    a = atr_wilder(st, 14)
    last_ft, last_st = ft[-1], st[-1]
    efv, esv = ef[-1], es[-1]
    # 与回测引擎一致：引擎要求最后一根已收盘 4h K 线的下标 >= ema_slow+1，
    # 即已收盘根数 >= ema_slow+2。少一根就会提前一个 4h 周期入场。
    if efv is None or esv is None or len(ft) < int(cfg.get("ema_slow", 200)) + 2:
        raise RuntimeError("4h EMA 预热未完成，请稍后再试")
    up = (efv > esv) and (last_ft["close"] > esv)
    dn = (efv < esv) and (last_ft["close"] < esv)
    want = "long" if up else ("short" if dn else "flat")
    # 实时价格单独取（未收盘价不影响 EMA/ATR 判定，只用于止损监控）
    try:
        px = last_price(sym, price_provider)
    except Exception:                               # noqa: BLE001
        px = last_st["close"]
    # 当前这根 1h K 线（未收盘）的高低点：用于盘中止损判断，避免只按收盘价漏掉插针
    bar_hi, bar_lo = last_st["high"], last_st["low"]
    # 正在形成那根的开盘价：跳空止损要用它，不能用已收盘那根的开盘价（否则会取错 K 线）
    bar_open = last_st["open"]
    try:
        live1h = klines(sym, st_iv, 2, closed_only=False, provider=provider)
        if live1h and live1h[-1]["t"] >= last_st["t"]:
            bar_hi = max(bar_hi, live1h[-1]["high"])
            bar_lo = min(bar_lo, live1h[-1]["low"])
            bar_open = live1h[-1]["open"]
    except Exception:                               # noqa: BLE001
        pass
    return {
        "st": st, "ft": ft, "want": want, "atr": a[-1],
        "price": px, "bar_high": bar_hi, "bar_low": bar_lo,
        "ema_fast": efv, "ema_slow": esv,
        "bar_t": last_st["t"], "st_iv": st_iv,
        "last_closed_open": last_st["open"], "bar_open": bar_open,
    }


def equity_of(cfg, s):
    """纸上权益：按名义=权益×杠杆 复利跟踪。"""
    return float(s.get("equity") or cfg.get("initial_equity", 1000))


def step(cfg, force_email=False, provider=None, price_provider=None):
    s = load_state()
    if not s.get("equity"):
        s["equity"] = float(cfg.get("initial_equity", 1000))
    if not s.get("trade_log"):
        s["trade_log"] = _seed_trade_log()
    sym = cfg.get("symbol", "BTCUSDT")
    st_iv = cfg.get("signal_timeframe", "1h")

    # ---- 每日定时邮件：到达 daily_email_hour 点（默认 8 点，任务在 :02 跑）时，
    #      无论有没有信号都发一封「今日操作」；last_daily 保证一天只发一次 ----
    _now_local = datetime.datetime.now()
    _daily_hour = int(cfg.get("daily_email_hour", 8))
    _today = _now_local.strftime("%Y-%m-%d")
    is_daily = (_now_local.hour == _daily_hour) and (s.get("last_daily") != _today)

    # ---- 快速路径：没有新收盘的 1h K 线、且当前空仓/无待执行动作 -> 直接返回 ----
    # 这样即使计划任务每 1~2 分钟跑一次，也只在「小时线收盘后」才真正干活，
    # 不会浪费请求；有持仓时仍然每轮检查止损。
    if not force_email and not is_daily and provider is None:
        try:
            probe = klines(sym, st_iv, 2, provider=provider)
            last_closed = probe[-1]["t"]
        except Exception as ex:                     # noqa: BLE001
            log(f"轻量探测失败，按完整流程重试：{ex}")
            last_closed = None
        if last_closed is not None:
            have_pos = s.get("mode") in ("long", "short")
            # 有持仓 -> 必须每轮检查止损；空仓 -> 只在新的 4h 收盘后才重新判定信号
            ft_ms = MS[cfg.get("filter_timeframe", "4h")]
            new_filter_bar = (last_closed // ft_ms) != (s.get("last_filter_bar", 0) // ft_ms) \
                if s.get("last_filter_bar") else True
            need = have_pos or new_filter_bar or s.get("pending")
            if not need:
                # 跳过（非失败）：返回带标记的 snap，避免上层重试脚本误判
                return s, {"skipped": True, "want": s.get("mode", "flat"),
                           "price": s.get("last_price", 0.0),
                           "ema_fast": 0.0, "ema_slow": 0.0,
                           "bar_t": last_closed, "st_iv": st_iv,
                           "bar_high": 0.0, "bar_low": 0.0, "atr": 0.0}, []
            if have_pos or new_filter_bar:
                s["last_bar_t"] = last_closed
            if new_filter_bar:
                s["last_filter_bar"] = last_closed

    try:
        snap = snapshot(cfg, provider=provider, price_provider=price_provider)
    except Exception as ex:                         # noqa: BLE001
        # 取数失败或预热未完成：本轮跳过，保持原状态，等下一轮
        log(f"本轮跳过：{ex}")
        return s, None, []
    price, want = snap["price"], snap["want"]
    now = snap["bar_t"]
    lev = float(cfg.get("leverage", 2.0))
    cost = 2.0 * (float(cfg.get("fee", 0.0005)) + float(cfg.get("slip", 0.0003)))
    slip = float(cfg.get("slip", 0.0003))
    sl_mult = float(cfg.get("stop_atr", 8.0))
    cd_bars = int(cfg.get("cooldown_bars", 24))
    cd_ms = cd_bars * MS[snap["st_iv"]]
    mode = s.get("mode", "flat")
    eq = equity_of(cfg, s)
    events = []

    # ---- 0) 执行上一轮挂起的动作（次根开盘成交，与回测引擎一致） ----
    pend = s.get("pending")
    if pend and now > s.get("pending_bar", 0):
        px = snap["st"][-1]["open"]
        if pend == "exit":
            d = 1 if mode == "long" else -1
            ret = d * (px / s["entry_price"] - 1.0) * lev - cost * lev
            eq *= (1 + ret)
            events.append(("TREND", mode, px, ret, eq))
            _log_close(s, mode, s.get("entry_price", 0.0), s.get("entry_time", 0),
                       px, now, "趋势失效", ret * 100)
            s.update({"mode": "flat", "entry_price": 0.0, "stop_price": 0.0,
                      "cooldown_until": now + cd_ms})
            mode = "flat"
        elif pend in ("long", "short"):
            d = 1 if pend == "long" else -1
            fill = px * (1 + slip) if pend == "long" else px * (1 - slip)
            # 止损从「未加滑点的价格」起算，与引擎 stop_px = open - d*sl*ATR 一致
            s.update({"mode": pend, "entry_price": fill,
                      "stop_price": px - d * sl_mult * s.get("pending_atr", 0.0),
                      "entry_time": now})
            events.append(("ENTER", pend, fill, 0.0, eq))
            mode = pend
        s.pop("pending", None)
        s.pop("pending_bar", None)
        s.pop("pending_atr", None)

    # ---- 1) 持仓中：盘中止损（用当前 K 线高低点 + 实时价） ----
    if mode in ("long", "short"):
        d = 1 if mode == "long" else -1
        stop = s.get("stop_price", 0.0)
        adverse = snap.get("bar_low") if mode == "long" else snap.get("bar_high")
        hit_stop = (adverse <= stop) if mode == "long" else (adverse >= stop)
        hit_stop = hit_stop or ((price <= stop) if mode == "long" else (price >= stop))
        if hit_stop:
            # 跳空优先：若「刚收盘那根」或「正在形成那根」的开盘价已越过止损，
            # 按该开盘价成交（与引擎逐根 K 线判定一致）。取最坏（对持仓最不利）的成交价。
            cand = [stop]
            for ob in (snap.get("last_closed_open"), snap.get("bar_open")):
                if ob is None:
                    continue
                if (mode == "long" and ob <= stop) or (mode == "short" and ob >= stop):
                    cand.append(ob)
            fill = min(cand) if mode == "long" else max(cand)
            ret = d * (fill / s["entry_price"] - 1.0) * lev - cost * lev
            eq *= (1 + ret)
            events.append(("STOP", mode, fill, ret, eq))
            _log_close(s, mode, s.get("entry_price", 0.0), s.get("entry_time", 0),
                       fill, now, "止损", ret * 100)
            s.update({"mode": "flat", "cooldown_until": now + cd_ms,
                      "entry_price": 0.0, "stop_price": 0.0})
            mode = "flat"
        elif want != mode and not s.get("pending"):
            # 趋势失效：本轮收盘只记录，下一轮开盘才成交（与回测引擎一致）
            s["pending"] = "exit"
            s["pending_bar"] = now

    # ---- 2) 空仓：状态匹配且不在冷却 -> 挂起进场，下一轮开盘成交 ----
    if mode == "flat" and not events and not s.get("pending") \
            and want in ("long", "short") and now >= s.get("cooldown_until", 0):
        if snap["atr"] and snap["atr"] > 0:
            s["pending"] = want
            s["pending_bar"] = now
            s["pending_atr"] = snap["atr"]

    s["equity"] = eq
    s["trades"] = int(s.get("trades", 0)) + sum(1 for e in events if e[0] == "ENTER")
    s["last_run"] = int(time.time())
    s["last_price"] = price
    if is_daily:
        s["last_daily"] = _today
    save_state(s)
    # 心跳日志：每轮都写一行，便于确认计划任务确实跑过
    log(f"本轮完成：应有状态 {want}，当前持仓 {s['mode']}，价格 {fmt(price)}，"
        f"权益 {fmt(eq)} USDT" + (f"，发生 {len(events)} 个事件" if events else ""))

    # ---- 3) 推送（HTML 表格邮件；每日 8 点无论有无信号都发） ----
    if events or force_email or is_daily:
        lev_txt = f"{lev:g}x 合约"
        if is_daily:
            subj = f"BTC策略·今日操作（{lev_txt}）：{_action_tag(s, want)}"
        elif events:
            subj = f"BTC策略信号（{lev_txt}）：" + "；".join(
                {"ENTER": "开仓", "STOP": "止损", "TREND": "趋势失效平仓"}[e[0]] for e in events)
        else:
            subj = f"BTC策略信号（{lev_txt}）：状态播报"
        html_body = _build_email_html(now, price, snap, s, want, lev, eq, events, cfg)
        send_email(cfg, subj, html_body, html=True)
    return s, snap, events


def main():
    ap = argparse.ArgumentParser(description="BTC 趋势策略实盘监测器")
    ap.add_argument("--once", action="store_true", help="只跑一次后退出")
    ap.add_argument("--loop", action="store_true", help="常驻循环（配合 Windows 计划任务）")
    ap.add_argument("--interval", type=int, default=300, help="循环间隔秒，默认 300")
    ap.add_argument("--test-email", action="store_true", help="发送一封测试邮件")
    ap.add_argument("--status", action="store_true", help="只打印当前状态，不动作")
    args = ap.parse_args()

    cfg = load_cfg()

    if args.test_email:
        lev_txt = f"{float(cfg.get('leverage', 2.0)):g}x 合约"
        ok = send_email(cfg, f"BTC策略监测器 测试邮件（{lev_txt}）",
                        "如果你收到这封邮件，说明邮件推送配置正确。\n"
                        f"发送时间 {datetime.datetime.now(CST):%Y-%m-%d %H:%M:%S} CST")
        raise SystemExit(0 if ok else 1)

    if args.status:
        s, snap, _ = None, None, None
        try:
            snap = snapshot(cfg)
        except Exception as ex:                     # noqa: BLE001
            log(f"取数失败: {ex}")
            raise SystemExit(1)
        st = load_state()
        print(f"BTC 价格      {fmt(snap['price'])}")
        print(f"4h EMA50      {fmt(snap['ema_fast'])}")
        print(f"4h EMA200     {fmt(snap['ema_slow'])}")
        print(f"应有状态      {snap['want']}")
        print(f"当前持仓      {st.get('mode','flat')}")
        print(f"纸上权益      {fmt(equity_of(cfg, st))} USDT")
        raise SystemExit(0)

    if args.once:
        # 单次运行：取数失败返回退出码 2，供上层重试脚本判断
        try:
            _, snap, _ = step(cfg)
        except Exception as ex:                     # noqa: BLE001
            log(f"本轮执行出错: {ex}")
            raise SystemExit(2)
        raise SystemExit(0 if snap is not None else 2)

    if not args.loop:
        while True:
            try:
                step(cfg)
            except Exception as ex:                 # noqa: BLE001
                log(f"本轮执行出错: {ex}")
            time.sleep(max(30, args.interval))
    else:
        log("进入常驻监测循环")
        while True:
            try:
                step(cfg)
            except Exception as ex:                 # noqa: BLE001
                log(f"本轮执行出错: {ex}")
            time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
