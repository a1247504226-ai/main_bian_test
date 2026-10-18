# -*- coding: utf-8 -*-
"""美股趋势策略 · 每日开盘前邮件提醒

流程：
  1. 判断美东今天是不是交易日（周末 / NYSE 节假日跳过）
  2. 从东方财富拉 5 只标的的后复权日线（全量覆盖缓存，避免复权因子接缝累积）
  3. 算 MA200 信号、距均线偏离、ATR
  4. 和上次发信的状态对比，得出「今天开盘要做什么」
  5. 发 HTML 邮件

用法：
  python daily_signal.py              # 正常跑（不在发送窗口内则跳过）
  python daily_signal.py --force      # 无视交易日/时间窗口，强制发
  python daily_signal.py --dry-run    # 只打印，不发邮件、不写 state
"""
import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
CACHE = os.path.join(HERE, "cache")
LOGDIR = os.path.join(HERE, "logs")
STATE_PATH = os.path.join(HERE, "state.json")

SINA = "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK"
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}
UA_EM = dict(UA, Referer="https://quote.eastmoney.com/")
# 东财 secid：105 纳斯达克 / 106 纽交所 / 107 美交所
SECID = {
    "AAPL": "105.AAPL", "MSFT": "105.MSFT", "NVDA": "105.NVDA",
    "AMZN": "105.AMZN", "NFLX": "105.NFLX",
}
KEEP_BARS = 900          # 缓存保留的根数（MA200 需要 200，留足缓冲）
CN = {
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "AMZN": "亚马逊", "NFLX": "奈飞",
}

log = logging.getLogger("signal")


# ────────────────────────── 日志 ──────────────────────────
def setup_log(verbose=False):
    os.makedirs(LOGDIR, exist_ok=True)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(
        os.path.join(LOGDIR, datetime.now().strftime("%Y-%m") + ".log"), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)


# ────────────────────── 美东时间 / 交易日历 ──────────────────────
def _nth_weekday(y, m, wd, n):
    """某年某月第 n 个星期 wd（0=周一）。"""
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(y, m, wd):
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _easter(y):
    """Anonymous Gregorian algorithm。"""
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31
    da = (h + l - 7 * m + 114) % 31 + 1
    return date(y, mo, da)


def _obs(d):
    """固定日期假日：遇周六提前到周五，遇周日推后到周一。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(y):
    hs = {
        _obs(date(y, 1, 1)),                       # 元旦
        _nth_weekday(y, 1, 0, 3),                  # 马丁路德金日
        _nth_weekday(y, 2, 0, 3),                  # 总统日
        _easter(y) - timedelta(days=2),            # 耶稣受难日
        _last_weekday(y, 5, 0),                    # 阵亡将士纪念日
        _obs(date(y, 7, 4)),                       # 独立日
        _nth_weekday(y, 9, 0, 1),                  # 劳动节
        _nth_weekday(y, 11, 3, 4),                 # 感恩节
        _obs(date(y, 12, 25)),                     # 圣诞
    }
    if y >= 2022:
        hs.add(_obs(date(y, 6, 19)))               # Juneteenth
    return hs


def is_us_trading_day(d):
    if d.weekday() >= 5:
        return False
    # 次年 1/1 落在周六时，今年 12/31 周五休市，所以要一并看下一年
    for y in (d.year, d.year + 1):
        if d in nyse_holidays(y):
            return False
    return True


def us_eastern_now():
    utc = datetime.now(timezone.utc)
    y = utc.year

    def _utc_of_local_2am(d):     # 美东 02:00 对应的 UTC（EST 用 07:00 近似）
        return datetime(d.year, d.month, d.day, 7, tzinfo=timezone.utc)

    dst = _utc_of_local_2am(_nth_weekday(y, 3, 6, 2)) <= utc < \
          _utc_of_local_2am(_nth_weekday(y, 11, 6, 1))
    return utc.astimezone(timezone(timedelta(hours=-4 if dst else -5))), dst


# ────────────────────────── 数据 ──────────────────────────
def fix_splits(d):
    """把不复权序列按拆股修正成前复权（不处理分红，分红对 MA200 影响 <1%）。

    判据用「开盘价 / 前一日收盘」而不是「收盘价 / 前一日收盘」：
    拆股是开盘就生效的，跳空比例精确等于 1/k；真实暴跌是盘中跌的，开盘不跳。
    这样 2:1、3:1 这种小比例也不会和真实腰斩混淆。
    """
    c = d["close"].values.astype(float)
    o = d["open"].values.astype(float)
    n = len(c)
    f = np.ones(n)
    hits = []
    for i in range(1, n):
        if c[i - 1] <= 0 or o[i] <= 0:
            continue
        gap = o[i] / c[i - 1]
        if gap > 0.9:
            continue
        k = 1.0 / gap
        kk = int(round(k))
        if kk < 2 or abs(k - kk) / kk > 0.035:
            continue
        r = c[i] / c[i - 1]
        if r > 0.9 or abs(1 / r - kk) / kk > 0.05:
            continue
        f[:i] *= kk
        hits.append((str(d["date"].iloc[i].date()), kk))
    out = d.copy()
    for col in ["open", "high", "low", "close"]:
        out[col] = d[col] / f
    out["volume"] = d["volume"] * f
    if hits:
        log.info("  拆股修正 %s", ", ".join(f"{dt} {k}:1" for dt, k in hits[-4:]))
    return out


def fetch_sina(sym, retries=4):
    """新浪美股日线。返回前复权序列，最后一根的 close 就是真实价格。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(SINA, params={"symbol": sym}, headers=UA, timeout=30)
            j = r.json()
            if not isinstance(j, list) or not j:
                raise RuntimeError(f"空数据 {str(j)[:100]}")
            df = pd.DataFrame([{
                "date": x["d"], "open": float(x["o"]), "high": float(x["h"]),
                "low": float(x["l"]), "close": float(x["c"]), "volume": float(x["v"]),
            } for x in j])
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df = df[df["close"] > 0].reset_index(drop=True)
            return fix_splits(df)
        except Exception as e:
            last = e
            if i < retries - 1:
                wait = 2 ** i * 2
                log.warning("%s 新浪第 %d 次失败（%s），%ds 后重试", sym, i + 1, e, wait)
                time.sleep(wait)
    raise RuntimeError(f"{sym}: 新浪拉取失败 {last}")


def _em_kline(sym, fqt):
    sid = SECID.get(sym)
    if not sid:
        r = requests.get("https://searchapi.eastmoney.com/api/suggest/get",
                         params={"input": sym, "type": "14",
                                 "token": "D43BF722C8E33BDC906FB84D85E326E8",
                                 "count": "10"}, headers=UA_EM, timeout=20)
        for it in r.json()["QuotationCodeTable"]["Data"]:
            if it.get("Code", "").upper() == sym.upper():
                sid = f'{it["MktNum"]}.{sym}'
                break
    if not sid:
        raise RuntimeError(f"{sym}: 找不到 secid")
    r = requests.get(KLINE, headers=UA_EM, timeout=25, params={
        "secid": sid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101",
        "fqt": fqt, "beg": "19900101", "end": "20500101", "lmt": "100000",
    })
    kl = (r.json().get("data") or {}).get("klines")
    if not kl:
        raise RuntimeError("东财空数据")
    df = pd.DataFrame([ln.split(",") for ln in kl],
                      columns=["date", "open", "close", "high", "low", "volume", "amount"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_em(sym, retries=2):
    """东财后复权日线（备用源）。整体缩放到「最新价 = 真实价」，与新浪口径对齐。"""
    last = None
    for i in range(retries):
        try:
            adj = _em_kline(sym, "2")
            raw = _em_kline(sym, "0")
            scale = float(raw["close"].iloc[-1]) / float(adj["close"].iloc[-1])
            for col in ["open", "high", "low", "close"]:
                adj[col] = adj[col] * scale
            adj["volume"] = adj["volume"] / scale
            return adj.reset_index(drop=True)
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(3)
    raise RuntimeError(f"{sym}: 东财拉取失败 {last}")


def update_cache(syms):
    """全量重拉并覆盖缓存。

    每次只保留最近 KEEP_BARS 根，且同一个源一次拉全 —— 绝不把两个源的数据拼在一起，
    否则后复权/前复权的口径差会在 MA200 窗口里留下接缝。
    """
    os.makedirs(CACHE, exist_ok=True)
    out = {}
    for s in syms:
        df, used = None, None
        for name, fn in (("新浪", fetch_sina), ("东财", fetch_em)):
            try:
                df, used = fn(s), name
                break
            except Exception as e:
                log.warning("%s 用%s失败：%s", s, name, str(e)[:110])
        if df is None:
            p = os.path.join(CACHE, f"{s}.csv")
            if not os.path.exists(p):
                raise RuntimeError(f"{s}: 两个数据源都失败，且没有本地缓存")
            log.warning("%s 两个源都失败，回退本地缓存（数据可能不是最新）", s)
            df, used = pd.read_csv(p, parse_dates=["date"]), "缓存"
        df = df.tail(KEEP_BARS).reset_index(drop=True)
        df.to_csv(os.path.join(CACHE, f"{s}.csv"), index=False, encoding="utf-8")
        out[s] = dict(adj=df, raw_close=float(df["close"].iloc[-1]),
                      raw_date=df["date"].iloc[-1])
        log.info("  %s [%s] %d 根，最新 %s 收盘 %.2f", s, used, len(df),
                 df["date"].iloc[-1].date(), out[s]["raw_close"])
    return out


def atr_ratio(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1] / c.iloc[-1])


# ────────────────────────── 信号 ──────────────────────────
def compute(data, ma_n, capital, syms):
    rows = []
    for s in syms:
        d = data[s]["adj"]
        c = d["close"]
        ma = float(c.rolling(ma_n).mean().iloc[-1])
        last = float(c.iloc[-1])
        dev = last / ma - 1
        rows.append(dict(
            sym=s, cn=CN.get(s, s),
            date=d["date"].iloc[-1].strftime("%Y-%m-%d"),
            close=data[s]["raw_close"], ma=data[s]["raw_close"] / (1 + dev),
            dev=dev, atr=atr_ratio(d), hold=dev > 0,
            amount=capital / len(syms) if dev > 0 else 0.0,
        ))
    return rows


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(rows, asof):
    json.dump({"asof": asof, "hold": {r["sym"]: r["hold"] for r in rows},
               "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
              open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ────────────────────────── 邮件 ──────────────────────────
def build_mail(rows, prev, cfg, asof, trade_date):
    cap = cfg["strategy"]["capital"]
    per = cap / len(rows)
    n_hold = sum(1 for r in rows if r["hold"])
    expo = n_hold / len(rows)

    actions = []
    for r in rows:
        was = prev.get(r["sym"])
        if was is None:
            r["act"] = "持有" if r["hold"] else "空仓"
        elif r["hold"] and not was:
            r["act"] = "买入"
            actions.append(("buy", r))
        elif was and not r["hold"]:
            r["act"] = "卖出"
            actions.append(("sell", r))
        else:
            r["act"] = "继续持有" if r["hold"] else "保持空仓"

    if actions:
        parts = []
        for kind, r in actions:
            if kind == "buy":
                parts.append(f'{r["sym"]}（{r["cn"]}）开盘买入 {per:,.0f}U')
            else:
                parts.append(f'{r["sym"]}（{r["cn"]}）开盘清仓')
        headline = "今天有调仓：" + "；".join(parts)
        hcolor = "#c0392b" if any(k == "buy" for k, _ in actions) else "#1e8449"
    else:
        headline = f"无需操作：{n_hold} 只继续持有，{len(rows)-n_hold} 只保持空仓"
        hcolor = "#111827"

    crit = [r for r in rows if r["hold"] and 0 < r["dev"] < 0.05] + \
           [r for r in rows if not r["hold"] and r["dev"] > -0.05]
    crit_txt = "、".join(f'{r["sym"]} 距均线仅 {abs(r["dev"])*100:.1f}%' for r in crit)

    # ---- 表格行 ----
    trs = ""
    for r in rows:
        hold = r["hold"]
        sig_col = "#c0392b" if hold else "#1e8449"
        sig_txt = "持有" if hold else "空仓"
        act = r["act"]
        act_col = "#c0392b" if act in ("买入", "继续持有", "持有") else "#1e8449"
        if act in ("买入", "卖出"):
            act_col, weight = "#b45309", "700"
        else:
            weight = "400"
        amt = (f'−{per:,.0f}U' if r["act"] == "卖出"
               else f'{r["amount"]:,.0f}U' if hold else "—")
        trs += (
            f'<tr>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;font-weight:600">{r["sym"]}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;color:#6b7280">{r["cn"]}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:right">{r["close"]:,.2f}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:right;color:#6b7280">{r["ma"]:,.2f}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:right;'
            f'color:{sig_col};font-weight:600">{r["dev"]*100:+.1f}%</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:center;'
            f'color:{sig_col};font-weight:600">{sig_txt}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:center;'
            f'color:{act_col};font-weight:{weight}">{act}</td>'
            f'<td style="padding:9px 10px;border-bottom:1px solid #eef0f3;text-align:right;color:#374151">{amt}</td>'
            f'</tr>'
        )

    subject = f'[美股趋势] {trade_date} 开盘前 · 持有 {n_hold}/{len(rows)} · 仓位 {expo*100:.0f}%'
    if actions:
        subject = f'[美股趋势] {trade_date} 开盘前 · ⚠ 有调仓 {len(actions)} 笔'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f5f7">
<div style="max-width:640px;margin:0 auto;padding:20px 16px;font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;color:#111827">

  <div style="font-size:13px;color:#6b7280;margin-bottom:6px">美股趋势策略 · 每日信号</div>
  <div style="font-size:21px;font-weight:700;letter-spacing:-.3px;margin-bottom:2px">
    {trade_date} 开盘前</div>
  <div style="font-size:13px;color:#6b7280;margin-bottom:16px">
    信号基于 {asof} 收盘 · 组合 {len(rows)} 只等权 · 总资金 {cap:,.0f}U</div>

  <div style="background:#fff;border-left:4px solid {hcolor};border-radius:6px;padding:14px 16px;margin-bottom:18px">
    <div style="font-size:15px;font-weight:600;color:{hcolor};line-height:1.5">{headline}</div>
    <div style="font-size:13px;color:#4b5563;margin-top:6px">
      当前仓位 <b>{expo*100:.0f}%</b>（{n_hold} 只 × {per:,.0f}U）
      {'· 现金 ' + f'{(1-expo)*cap:,.0f}U' if expo < 1 else '· 满仓'}
    </div>
  </div>

  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;font-size:13.5px">
    <thead><tr style="background:#f9fafb">
      <th style="padding:9px 10px;text-align:left;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">代码</th>
      <th style="padding:9px 10px;text-align:left;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">名称</th>
      <th style="padding:9px 10px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">收盘</th>
      <th style="padding:9px 10px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">MA{cfg['strategy']['ma']}</th>
      <th style="padding:9px 10px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">距均线</th>
      <th style="padding:9px 10px;text-align:center;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">信号</th>
      <th style="padding:9px 10px;text-align:center;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">动作</th>
      <th style="padding:9px 10px;text-align:right;font-weight:600;color:#6b7280;font-size:12px;border-bottom:1px solid #eef0f3">金额</th>
    </tr></thead>
    <tbody>{trs}</tbody>
  </table>

  {f'<div style="background:#fffbeb;border-radius:6px;padding:12px 14px;margin-top:16px;font-size:13px;color:#92400e"><b>临界持仓</b>：{crit_txt}。这些标的离均线不足 5%，随时可能翻空。</div>' if crit_txt else ''}

  <div style="background:#fff;border-radius:6px;padding:14px 16px;margin-top:16px;font-size:12.5px;color:#4b5563;line-height:1.75">
    <b style="color:#111827">规则</b><br>
    每个交易日收盘后看收盘价，站上 {cfg['strategy']['ma']} 日均线 → 次日开盘持有；跌破 → 次日开盘清空。<br>
    不做止损、不看财报、不判断大盘。每只等权 {per:,.0f}U。<br><br>
    <b style="color:#111827">回测（2006-01 ~ 2026-10，20.8 年）</b><br>
    趋势过滤 214.5 倍 / 年化 29.5% / 最大回撤 −34.5% / Calmar 0.86<br>
    等权买入 223.6 倍 / 年化 29.8% / 最大回撤 −62.3% / Calmar 0.48
  </div>

  <div style="font-size:11.5px;color:#9ca3af;margin-top:16px;line-height:1.7">
    自动发送 · {datetime.now().strftime('%Y-%m-%d %H:%M')}（北京时间）<br>
    回测结果不代表未来收益，本邮件仅为策略信号提醒，不构成投资建议。
  </div>
</div>
</body></html>"""

    text = f"""{trade_date} 开盘前 · 美股趋势信号
信号基于 {asof} 收盘

{headline}
当前仓位 {expo*100:.0f}%（{n_hold}/{len(rows)} 只，每只 {per:,.0f}U）

代码   名称    收盘      MA{cfg['strategy']['ma']}    距均线    信号   动作
""" + "\n".join(
        f'{r["sym"]:<6} {r["cn"]:<4} {r["close"]:>8.2f} {r["ma"]:>8.2f} {r["dev"]*100:>+8.1f}%  '
        f'{"持有" if r["hold"] else "空仓"}  {r["act"]}'
        for r in rows) + f"""

规则：收盘站上 {cfg['strategy']['ma']} 日均线则次日开盘持有，跌破则清空。
回测 2006-01~2026-10：趋势 214.5x / Calmar 0.86，买入持有 223.6x / Calmar 0.48。
不构成投资建议。
"""
    return subject, html, text


def send_mail(subject, html, text, cfg):
    m = cfg["mail"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("美股趋势信号", m["from"]), charset="utf-8")
    msg["To"] = m["to"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text, subtype="plain", charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")

    n = max(1, int(m.get("max_retries", 3)))
    last = None
    for i in range(n):
        try:
            # local_hostname 必须显式给：本机计算机名含中文时，
            # EHLO 命令会带中文主机名，smtplib 用 ascii 编码直接抛 UnicodeEncodeError。
            with smtplib.SMTP_SSL(m["server"], int(m["port"]), timeout=30,
                                  local_hostname="localhost") as s:
                s.login(m["from"], m["password"])
                s.send_message(msg)
            log.info("邮件已发送 → %s", m["to"])
            return True
        except Exception as e:
            last = e
            log.warning("发送第 %d/%d 次失败：%s", i + 1, n, e)
            if i < n - 1:
                time.sleep(3 * (i + 1))
    log.error("邮件发送失败（已重试 %d 次）：%s", n, last)
    return False


# ────────────────────────── 主流程 ──────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="无视交易日与时间窗口")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发信")
    ap.add_argument("--no-fetch", action="store_true", help="用缓存数据，不联网拉新")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_log(args.verbose)
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    syms = cfg["strategy"]["symbols"]

    now_et, dst = us_eastern_now()
    today_et = now_et.date()
    log.info("北京时间 %s ／ 美东 %s（%s）",
             datetime.now().strftime("%Y-%m-%d %H:%M"), now_et.strftime("%Y-%m-%d %H:%M"),
             "夏令时" if dst else "冬令时")

    # 1. 交易日
    if not args.force:
        if cfg["schedule"].get("skip_us_holiday", True) and not is_us_trading_day(today_et):
            log.info("美东 %s 不是交易日（周末或 NYSE 休市），跳过。", today_et)
            return 0
        # 2. 时间窗口
        win = cfg["schedule"].get("send_window", ["05:00", "09:30"])
        hhmm = now_et.strftime("%H:%M")
        if not (win[0] <= hhmm <= win[1]):
            log.info("当前美东 %s 不在发送窗口 %s~%s 内，跳过。用 --force 强制发送。",
                     hhmm, win[0], win[1])
            return 0

    # 3. 数据
    if args.no_fetch and all(os.path.exists(os.path.join(CACHE, f"{s}.csv")) for s in syms):
        log.info("使用本地缓存（--no-fetch）")
        data = {}
        for s in syms:
            d = pd.read_csv(os.path.join(CACHE, f"{s}.csv"), parse_dates=["date"])
            data[s] = dict(adj=d, raw_close=float(d["close"].iloc[-1]),
                           raw_date=d["date"].iloc[-1])
    else:
        log.info("拉取数据：%s", ", ".join(syms))
        data = update_cache(syms)

    # 4. 信号
    rows = compute(data, cfg["strategy"]["ma"], cfg["strategy"]["capital"], syms)
    asof = rows[0]["date"]
    if rows[0]["date"] != rows[-1]["date"]:
        log.warning("各标的最新日期不一致：%s", {r["sym"]: r["date"] for r in rows})

    # 5. 对比上次
    st = load_state()
    prev = st.get("hold", {})
    if not args.force and st.get("asof") == asof:
        log.info("信号基准日 %s 与上次已发送的一致（没有新收盘数据），跳过。", asof)
        return 0
    trade_date = today_et.strftime("%m-%d")
    subject, html, text = build_mail(rows, prev, cfg, asof, trade_date)

    print("\n" + "=" * 62)
    print(f" {subject}")
    print("=" * 62)
    print(text)
    print("=" * 62)

    if args.dry_run:
        log.info("--dry-run：不发送、不写 state")
        return 0

    ok = send_mail(subject, html, text, cfg)
    if ok:
        save_state(rows, asof)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
