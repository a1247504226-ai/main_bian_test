#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 MACD-X 策略 —— 加密货币趋势跟随（自包含单文件，纯标准库，可直接运行）
================================================================================

【策略规则】
  入场（两条同时满足，次日开盘买入）
      ① 收盘价 > EMA(200)          —— 长期趋势向上
      ② MACD 快线 > 0              —— 中期动能向上

  出场（满足其一，次日开盘卖出）
      ① 收盘价 < EMA(200)  或  MACD 快线 < 0
      ② 黑天鹅硬止损：跌破入场价 -35%（9 年回测从未触发，纯保险）

  仓位：满仓进出（实盘建议单标的 50%~70%）

【为什么不做常规止盈止损】（这是本策略最反直觉、也是最重要的地方）
  回测证据：同一入场信号，只改出场方式，平均单笔收益——
      止损 30%、不止盈 .................. +56.4%
      止损 20% + 止盈 30% ................ +3.6%
  加上止盈，收益砍掉 94%。原因是加密资产收益极度右偏：
  少数几笔 +300%~400% 的交易贡献几乎全部利润，止盈砍掉的正是这几笔。
  「泡沫/超买」信号在加密市场是【动量延续】信号而非反转信号——
  实测泡沫信号出现后 90 天平均还要涨 38%（同期全样本 14.6%）。

【成本假设】
  手续费 0.1%/单边 + 滑点 0.05%/单边（币安现货 taker 保守估计）

【用法】
  python macdx_strategy.py                          # 回测默认 5 个标的
  python macdx_strategy.py --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT
  python macdx_strategy.py --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT --signal  # 每日监控这 5 个
  python macdx_strategy.py --symbol BTCUSDT --interval 4h
  python macdx_strategy.py --symbol BTCUSDT --signal     # 只看当前该不该持仓 + 邮件推送
  python macdx_strategy.py --symbols BTCUSDT --no-fetch  # 用本地 data/ 缓存
  python macdx_strategy.py --test-mail                  # 发一封测试邮件验证配置
  python macdx_strategy.py --symbol BTCUSDT --signal --no-mail   # 只看信号不发信
  python macdx_strategy.py --symbol BTCUSDT --signal --no-proxy  # 强制直连
  python macdx_strategy.py --symbol BTCUSDT --signal --proxy http://127.0.0.1:7890

【网络与代理】
  代理在文件顶部 PROXY 里改，默认 127.0.0.1:7897（Clash / Mihomo 默认端口）。
  抓取时【代理 / 直连交替重试】，并记住上次走通的线路优先使用：
  代理挂了就一路直连，不会每个分页都白等一次超时（实测 76s → 29s）。
  退避为指数 + 随机抖动。整轮下载失败会重试 RETRY['outer'] 轮，
  仍失败则用 data/ 下的本地缓存兜底，照样算信号、照样推送
  （邮件里会标注数据滞后天数）——不会因网络问题静默失效。
  最后有 RETRY['budget'] 总时间预算兜底，网络再烂也不会卡死定时任务。

【定时运行（Windows）】
  直接调用同目录的 run_signal.bat，它已配好日志、Python 路径探测和 30 天日志轮转。
  注册每天 08:01 执行（管理员 CMD 跑一次）：
      schtasks /Create /TN "MACD-X-Daily" /TR "<本目录>\run_signal.bat" /SC DAILY /ST 08:01 /F

【日志（--log-file）】
  --log-file PATH  把本次全部输出同时写进该文件。
  ★ 日志一律由 Python 以 UTF-8 + BOM 写出，不要在 bat 里用 ">>" 重定向。
    原因：中文 Windows 的 %date% 会展开成「2026/09/30 周三」，那是 GBK 字节；
    而 Python 输出是 UTF-8。两种编码混进同一个文件，用记事本打开会一半乱码。
    交给 Python 写，编码就只有一种；带 BOM 是为了让记事本正确识别成 UTF-8。
  日志开头会记录脚本目录/工作目录/Python 路径/完整命令行，
  换目录或换解释器时一眼就能看出来（排查"我到底跑的是哪一份"）。

【邮件推送】
  只在【信号状态发生变化】时发信：空仓→持有 = 买入提醒，持有→空仓 = 离场提醒。
  状态不变不发信（不会每天打扰）。状态存在 signal_state.json，首次运行只记录不发。
  日线在 UTC 00:00 收盘（= 北京时间 08:00），所以【北京时间每天 08:01】跑一次：
      python macdx_strategy.py --symbol BTCUSDT --signal --capital 1000 --frac 0.7
  Windows 直接双击同目录的 run_signal.bat（已配好定时任务命令），Linux 用 cron。

  依赖：无（只用 Python 标准库）。Python 3.9+
================================================================================
"""
from __future__ import annotations
import argparse
import calendar
import csv
import json
import math
import os
import random
import smtplib
import ssl
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText

# Windows 控制台默认 GBK，遇到 ✓ ✗ 等字符会崩。强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_STDOUT0, _STDERR0 = sys.stdout, sys.stderr   # 原始流，供日志分流用
_LOG_FP = None


class _Tee:
    """把输出同时写到控制台和日志文件。"""

    def __init__(self, stream, fp):
        self._stream, self._fp = stream, fp

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._fp.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        for s in (self._stream, self._fp):
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return "utf-8"

    def reconfigure(self, **kw):   # 兼容再次调用
        pass


def start_logging(path: str) -> None:
    """
    开启日志分流。日志一律由 Python 以 **UTF-8（带 BOM）** 写出。

    为什么要这样：如果让 .bat 用 `>>` 重定向，bat 那边的 `%date%` 在中文 Windows
    上会展开成「2026/09/30 周三」——含中文，是 GBK 字节；而 Python 输出是 UTF-8。
    两种编码混进同一个文件，用记事本（GBK）打开时 Python 那部分全是乱码。
    把日志完全交给 Python 写，编码就只有一个，彻底解决。
    带 BOM 是为了让 Windows 记事本能正确识别成 UTF-8。
    """
    global _LOG_FP
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    is_new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    fp = open(path, "a", encoding="utf-8", newline="")
    if is_new:
        fp.write("\ufeff")
    _LOG_FP = fp
    sys.stdout = _Tee(_STDOUT0, fp)
    sys.stderr = _Tee(_STDERR0, fp)


def _log_header(argv: list) -> None:
    print("=" * 78)
    print(f"MACD-X 运行日志    {now_cn()}（北京时间）")
    print(f"脚本目录 : {os.path.dirname(os.path.abspath(__file__))}")
    print(f"工作目录 : {os.getcwd()}")
    print(f"Python   : {sys.executable}")
    print(f"版本     : {sys.version.split()[0]}  平台 {sys.platform}")
    print(f"命令行   : {' '.join(argv)}")
    print("=" * 78)


def _log_footer(rc: int) -> None:
    if _LOG_FP is None:
        return
    try:
        print("-" * 78)
        print(f"运行结束    {now_cn()}    退出码 {rc}")
        print("")
        _LOG_FP.flush()
        _LOG_FP.close()
    except Exception:
        pass


def _prescan_log_file(argv: list) -> str | None:
    """在 argparse 之前先找出 --log-file，这样连参数错误也能记进日志。"""
    for i, a in enumerate(argv):
        if a == "--log-file" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--log-file="):
            return a.split("=", 1)[1]
    return None

# ─────────────────────────── 时区配置 ───────────────────────────
# 你原稿用的是 pytz.timezone('Asia/Shanghai')。本脚本坚持【零依赖】，
# 所以改用 Python 3.9+ 自带的 zoneinfo，功能完全等价、不用装 pytz。
# Windows 若没装 tzdata，自动降级成固定 UTC+8 —— 中国没有夏令时，结果一模一样。
try:
    from zoneinfo import ZoneInfo
    tz_shanghai = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    tz_shanghai = timezone(timedelta(hours=8), "Asia/Shanghai")

TZ_SHANGHAI = tz_shanghai  # 别名，两种写法都能用


def now_cn(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """当前北京时间字符串。"""
    return datetime.now(tz_shanghai).strftime(fmt)

# ─────────────────────────── 可调参数 ───────────────────────────
PARAMS = {
    # 指标
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "trend_ema": 200,      # 长期趋势均线（100=更激进，200=更稳健）
    "atr_n": 14,
    # 出场
    "exit_confirm": 1,     # 跌破后需连续 N 根确认（1=立即离场；3 在 BTC 上更优但在 ETH 上更差，故取 1）
    "hard_sl": 0.35,       # 黑天鹅硬止损（占入场价），0=关闭
    # 成本
    "fee": 0.001,          # 手续费/单边
    "slip": 0.0005,        # 滑点/单边
    # 资金
    "init_capital": 10000.0,
    "capital_frac": 1.0,   # 单次投入比例（实盘建议 0.5~0.7）
}

DATA_API = "https://data-api.binance.vision/api/v3/klines"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ─────────────────────────── 邮件推送配置 ───────────────────────────
# 仅在「信号状态发生变化」时推送（空仓→持有 = 买入，持有→空仓 = 离场），
# 状态不变时不发信，避免每天重复打扰。
EMAIL_CONFIG = {
    "from": "a1247504226@163.com",
    "to": "1247504226@qq.com",
    "password": "XKCINXNMOMMDCAFI",     # 163 邮箱的 SMTP 授权码（不是登录密码）
    "server": "smtp.163.com",
    "port": 465,                        # 465 = SSL；25 = 普通；587 = STARTTLS
    "max_retries": 3,
}
# 状态记录文件（用来判断信号是否发生变化）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_state.json")

# ─────────────────────────── 代理配置 ───────────────────────────
# 注意：HTTPS_PROXY 这里写 http:// 而不是 https://。
#   Clash / Mihomo 的入站端口是【明文 HTTP】，对 https 目标走 CONNECT 隧道。
#   写 https:// 在 urllib 里恰好也能用（它只取 host:port，仍走 CONNECT），
#   但那是巧合；写 http:// 才是规范写法，requests / pip / curl 也都能认。
PROXY = {
    "enabled": True,
    "http": "http://127.0.0.1:7897",
    "https": "http://127.0.0.1:7897",
}

# 网络重试：代理软件常被系统休眠/切节点打断，单次失败很正常
RETRY = {
    "per_page": 6,      # 单页请求最大尝试次数
    "outer": 3,         # 整轮下载失败后的整体重试轮数
    "timeout": 20,      # 单次请求超时（秒）
    "budget": 120.0,    # 【总时间预算】单个标的联网最多花多少秒，超了就走缓存兜底。
                        # 定时任务靠它保证不会卡死：网络再烂也最多等这么久。
}


def apply_proxy(enabled: bool | None = None,
                http: str | None = None, https: str | None = None) -> None:
    """把代理写进环境变量（脚本内 urllib 走独立 opener，环境变量主要给外部工具看）。"""
    global PROXY
    if enabled is not None:
        PROXY["enabled"] = enabled
    if http:
        PROXY["http"] = http
    if https:
        PROXY["https"] = https
    if PROXY["enabled"]:
        os.environ["HTTP_PROXY"] = PROXY["http"]
        os.environ["HTTPS_PROXY"] = PROXY["https"]
        os.environ["http_proxy"] = PROXY["http"]
        os.environ["https_proxy"] = PROXY["https"]
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)


_OPENERS: dict = {}


def _opener(use_proxy: bool):
    """获取 opener。True=走代理，False=强制直连（连环境变量里的代理也忽略）。"""
    key = "proxy" if use_proxy else "direct"
    if key not in _OPENERS:
        if use_proxy:
            handler = urllib.request.ProxyHandler(
                {"http": PROXY["http"], "https": PROXY["https"]})
        else:
            handler = urllib.request.ProxyHandler({})  # 空 dict = 显式禁用代理
        _OPENERS[key] = urllib.request.build_opener(handler)
    return _OPENERS[key]


def http_get(url: str, timeout: float = 20.0, use_proxy: bool = True) -> bytes:
    """带代理切换的 GET。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _opener(use_proxy).open(req, timeout=timeout) as r:
        return r.read()


# 记住上次走通的线路，下次优先用它。
# 代理软件时开时关，如果每次都从代理试起，代理没开时每页都要白等一次超时，
# 三个标的四个分页就是十几次无用重试。记住线路后，代理挂了就一路直连，快得多。
_ROUTE = {"prefer": "proxy"}


def _route_order() -> list[bool]:
    """返回线路尝试顺序（True=代理，False=直连）。"""
    if not PROXY["enabled"]:
        return [False]
    return [True, False] if _ROUTE["prefer"] == "proxy" else [False, True]


def send_email(subject: str, body: str, cfg: dict | None = None) -> bool:
    """发送邮件。失败时按 max_retries 重试，全部失败返回 False（不影响主流程）。"""
    cfg = dict(EMAIL_CONFIG, **(cfg or {}))
    # 允许用环境变量覆盖密码，避免明文硬编码
    pwd = os.environ.get("MACDX_MAIL_PASSWORD") or cfg["password"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    last = ""
    for attempt in range(int(cfg.get("max_retries", 3))):
        try:
            ctx = ssl.create_default_context()
            # 关键：显式指定纯 ASCII 的 local_hostname。
            # smtplib 发 EHLO 时会带上本机主机名，若计算机名含中文会直接
            # UnicodeEncodeError（'ascii' codec can't encode ...），连不上服务器。
            lh = "MACDX-Client"
            if int(cfg["port"]) == 465:
                with smtplib.SMTP_SSL(cfg["server"], int(cfg["port"]),
                                      local_hostname=lh, timeout=30, context=ctx) as s:
                    s.login(cfg["from"], pwd)
                    s.sendmail(cfg["from"], [cfg["to"]], msg.as_string())
            else:
                with smtplib.SMTP(cfg["server"], int(cfg["port"]),
                                  local_hostname=lh, timeout=30) as s:
                    s.ehlo()
                    s.starttls(context=ctx)
                    s.ehlo()
                    s.login(cfg["from"], pwd)
                    s.sendmail(cfg["from"], [cfg["to"]], msg.as_string())
            return True
        except Exception as e:
            last = str(e)
            if attempt < int(cfg.get("max_retries", 3)) - 1:
                time.sleep(3)
    print(f"  [邮件] 发送失败（已重试 {cfg.get('max_retries', 3)} 次）：{last[:120]}")
    return False


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(key: str, hold: bool, date: str) -> None:
    st = _load_state()
    st[key] = {"hold": hold, "date": date, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [警告] 状态文件写入失败：{e}")


def notify_if_changed(symbol: str, interval: str, sg: dict,
                      capital: float = 1000.0, frac: float = 0.7,
                      src: str = "api", stale: float = 0.0) -> None:
    """信号状态发生变化才发邮件。首次运行只记录不发，避免误报。"""
    key = f"{symbol}_{interval}"
    st = _load_state()
    prev = st.get(key, {}).get("hold")
    now = bool(sg["should_hold"])

    if prev is None:
        _save_state(key, now, sg["date"])
        print(f"  [邮件] 首次运行，已记录 {symbol} 状态为【{'持有' if now else '空仓'}】；"
              f"下次状态变化时才推送。")
        return

    if prev == now:
        _save_state(key, now, sg["date"])
        print(f"  [邮件] {symbol} 状态未变化（{'持有' if now else '空仓'}），不推送。")
        return

    # ── 状态变化，构造邮件 ──
    action = "买入" if now else "卖出"
    p = PARAMS
    fill = sg["close"] * (1 + p["slip"])
    invest = capital * frac
    qty = invest * (1 - p["fee"]) / fill if fill > 0 else 0.0

    if now:
        subject = f"[MACD-X] {symbol} 现货买入信号 · {sg['date'][:10]}"
        detail = (f"★ 建议动作：买入（市价单）\n"
                  f"  参考本金：{capital:,.0f} U\n"
                  f"  建议投入：{invest:,.0f} U（{frac*100:.0f}% 仓位，留 {(1-frac)*100:.0f}% 现金）\n"
                  f"  成交价参考：{fill:,.2f}（含 {p['slip']*100:.3f}% 滑点）\n"
                  f"  买入数量：约 {qty:.6f} {symbol.replace('USDT','')}\n"
                  f"  手续费：约 {invest*p['fee']:.2f} U")
    else:
        subject = f"[MACD-X] {symbol} 现货离场信号 · {sg['date'][:10]}"
        detail = (f"★ 建议动作：卖出（市价单）\n"
                  f"  卖出：全部持仓\n"
                  f"  成交价参考：{sg['close']*(1-p['slip']):,.2f}（含滑点）\n"
                  f"  卖出后持有 USDT 等待下一次信号")

    mark = lambda b: "在均线上方 ✓" if b else "在均线下方 ✗"
    warn = ""
    if stale > 2:
        warn = (f"\n⚠ 注意：本次数据来自本地缓存（联网失败），最后一根 K 线"
                f"距今 {stale:.1f} 天，信号可能不是最新的，请人工核对后再下单。\n")

    body = f"""MACD-X 策略信号提醒
{'='*44}

标的：{symbol}   周期：{interval}
信号日期：{sg['date']}（日线收盘 UTC 00:00 / 北京时间 08:00）
状态变化：{'空仓 → 持有' if now else '持有 → 空仓'}
推送时间：{now_cn()}（北京时间）
数据来源：{src}
{warn}
── 行情数据 ──
收盘价    : {sg['close']:,.4f}
EMA(200)  : {sg['ema_trend']:,.4f}   {mark(sg['price_above_ema'])}
MACD 快线 : {sg['macd']:+.4f}   {'在零轴上方 ✓' if sg['macd_above_zero'] else '在零轴下方 ✗'}
ATR(14)   : {sg['atr']:,.4f}

{detail}

── 操作提示 ──
1. 请在收盘后尽快下单（策略按「次日开盘价成交」回测）
2. 不要设止盈、不要设止损、不要挂条件单
3. 本策略无杠杆（现货），不要上杠杆
4. 胜率约 37%，连续亏损 3~4 笔属正常，请按规则执行

── 下次提醒 ──
本邮件只在信号状态变化时发送，状态不变不会打扰。
{'='*44}
本邮件由 macdx_strategy.py 自动生成，仅供研究，不构成投资建议。
"""
    ok = send_email(subject, body)
    if ok:
        print(f"  [邮件] 已发送【{action}】提醒到 {EMAIL_CONFIG['to']}")
        _save_state(key, now, sg["date"])
    else:
        print(f"  [邮件] 发送失败，状态未更新（下次运行会重试推送）")


# ══════════════════════════ 1. 数据获取 ══════════════════════════
def fetch_klines(symbol: str, interval: str = "1d", start_ms: int | None = None,
                 verbose: bool = True, deadline: float | None = None) -> list[dict]:
    """
    从币安公开 API 分页拉取 K 线。

    【抗代理抖动的抓取策略】
      每一页最多重试 RETRY['per_page'] 次，并且【代理 / 直连交替】尝试：
      第 1 次走代理，第 2 次直连，第 3 次代理……
      这样无论「代理挂了」还是「直连被墙」，都能自动绕过去，不需要人工干预。
      退避用指数 + 随机抖动，避免多个线程同时重连打爆本地代理。
      deadline 是绝对时间戳（time.time() 口径），到点立刻放弃 —— 定时任务靠它保证不卡死。
    """
    if start_ms is None:
        start_ms = calendar.timegm(time.strptime("2017-01-01", "%Y-%m-%d")) * 1000
    end_ms = int(time.time() * 1000)
    rows, cur = [], start_ms

    while cur < end_ms:
        url = (f"{DATA_API}?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        batch, last_err = None, ""
        order = _route_order()
        for attempt in range(int(RETRY["per_page"])):
            # 先试上次走通的线路，失败再换另一条，然后交替；--no-proxy 时永远直连
            use_proxy = order[attempt % len(order)]
            try:
                batch = json.loads(http_get(url, RETRY["timeout"], use_proxy).decode())
                _ROUTE["prefer"] = "proxy" if use_proxy else "direct"
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < int(RETRY["per_page"]) - 1:
                    wait = min(2 * (2 ** attempt), 20) + random.uniform(0, 1.5)
                    if deadline and time.time() + wait > deadline:
                        raise RuntimeError(
                            f"{symbol} 时间预算用尽（{RETRY['budget']:.0f}s），"
                            f"停止重试：{last_err[:80]}")
                    if verbose:
                        print(f"    [{symbol}] 第 {attempt+1} 次失败"
                              f"（{'代理' if use_proxy else '直连'}）：{last_err[:70]}"
                              f" → {wait:.1f}s 后重试", flush=True)
                    time.sleep(wait)
        if batch is None:
            raise RuntimeError(f"{symbol} 该页下载失败（已试 "
                               f"{RETRY['per_page']} 次）：{last_err[:100]}")
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if verbose:
            ds = time.strftime("%Y-%m-%d", time.gmtime(batch[-1][0] / 1000))
            print(f"    {symbol} {interval}: {len(rows)} 根, 最新 {ds}", flush=True)
        if len(batch) < 1000:
            break
        time.sleep(0.25)

    seen, out = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append({
            "dt": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r[0] / 1000)),
            "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]), "v": float(r[5]),
        })
    out.sort(key=lambda x: x["dt"])
    return out


def save_csv(bars: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b["dt"], b["o"], b["h"], b["l"], b["c"], b["v"]])


def load_csv(path: str) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"dt": r["datetime"], "o": float(r["open"]), "h": float(r["high"]),
                        "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])})
    return out


def load_bars(symbol: str, interval: str, no_fetch: bool = False,
              verbose: bool = True) -> tuple[list[dict], str, float]:
    """
    取数据：优先联网抓，抓不到就用本地缓存兜底。

    返回 (bars, 来源, 数据陈旧天数)。
    来源: 'api' | 'cache' | 'cache(兜底)'
    「不影响推送」的关键就在这：网络再怎么抽风，只要有历史缓存就照样算信号、照样发邮件，
    最多在邮件里标注一下数据可能过期。
    """
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")
    cache: list[dict] = []
    if os.path.exists(path):
        try:
            cache = load_csv(path)
        except Exception as e:
            print(f"  [警告] 本地缓存 {path} 读取失败：{e}")

    if no_fetch:
        return cache, "cache", _stale_days(cache)

    last_err = ""
    deadline = time.time() + float(RETRY["budget"])   # 总时间预算，到点走缓存
    for rnd in range(int(RETRY["outer"])):
        if time.time() >= deadline:
            print(f"  [超时] {symbol} 已达联网时间预算 "
                  f"{RETRY['budget']:.0f}s，改用本地缓存。")
            break
        try:
            if verbose:
                print(f"  下载 {symbol} {interval} ...（第 {rnd+1}/{RETRY['outer']} 轮）")
            bars = fetch_klines(symbol, interval, verbose=verbose, deadline=deadline)
            if bars:
                try:
                    save_csv(bars, path)
                except Exception as e:
                    print(f"  [警告] 缓存写入失败：{e}")
                return bars, "api", _stale_days(bars)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if verbose:
                print(f"  [警告] {symbol} 第 {rnd+1} 轮下载失败：{last_err[:100]}")
            w = 5 * (rnd + 1) + random.uniform(0, 2)
            # 睡完还在预算内才值得重试，否则直接跳出走缓存
            if rnd < int(RETRY["outer"]) - 1 and time.time() + w < deadline:
                if verbose:
                    print(f"          {w:.1f}s 后整轮重试 ...", flush=True)
                time.sleep(w)

    if cache:
        print(f"  [降级] {symbol} 联网失败，改用本地缓存（最后一根 "
              f"{cache[-1]['dt']}）。信号可能不是最新的。")
        return cache, "cache(兜底)", _stale_days(cache)
    raise RuntimeError(f"{symbol} 联网下载失败且无本地缓存：{last_err[:120]}")


def _stale_days(bars: list[dict]) -> float:
    """最后一根 K 线距离现在多少天（UTC 口径）。"""
    if not bars:
        return 999.0
    try:
        t = calendar.timegm(time.strptime(bars[-1]["dt"], "%Y-%m-%d %H:%M:%S"))
        return (time.time() - t) / 86400.0
    except Exception:
        return 999.0


# ══════════════════════════ 2. 指标 ══════════════════════════
def ema(vals: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out = [float("nan")] * len(vals)
    if len(vals) < n:
        return out
    prev = sum(vals[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(closes: list[float], fast=12, slow=26, signal=9):
    """返回 (macd线, 信号线, 柱)。信号线的 EMA 从 macd 有效值起算，避免 NaN 污染。"""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [ef[i] - es[i] if not (math.isnan(ef[i]) or math.isnan(es[i])) else float("nan")
            for i in range(len(closes))]
    sig = [float("nan")] * len(closes)
    k = 2.0 / (signal + 1)
    buf, prev, started = [], None, False
    for i, v in enumerate(line):
        if math.isnan(v):
            continue
        if not started:
            buf.append(v)
            if len(buf) == signal:
                prev = sum(buf) / signal
                sig[i] = prev
                started = True
        else:
            prev = v * k + prev * (1 - k)
            sig[i] = prev
    hist = [line[i] - sig[i] for i in range(len(closes))]
    return line, sig, hist


def atr(bars: list[dict], n: int = 14) -> list[float]:
    N = len(bars)
    tr = [float("nan")] * N
    for i in range(1, N):
        pc = bars[i - 1]["c"]
        tr[i] = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc))
    out = [float("nan")] * N
    if N < n + 1:
        return out
    out[n] = sum(tr[1:n + 1]) / n
    for i in range(n + 1, N):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def compute_indicators(bars: list[dict], p: dict) -> dict:
    closes = [b["c"] for b in bars]
    line, sig, hist = macd(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    return {
        "close": closes,
        "macd": line, "signal": sig, "hist": hist,
        "ema_trend": ema(closes, p["trend_ema"]),
        "atr": atr(bars, p["atr_n"]),
    }


# ══════════════════════════ 3. 信号 ══════════════════════════
def regime_on(ind: dict, i: int) -> bool:
    """是否处于「应持有」状态：收盘 > EMA(200) 且 MACD 快线 > 0。"""
    c = ind["close"][i]
    e = ind["ema_trend"][i]
    m = ind["macd"][i]
    if math.isnan(e) or math.isnan(m):
        return False
    return c > e and m > 0


def interval_seconds(interval: str) -> int:
    """把 '1d'/'4h'/'1h'/'15m' 转成秒。"""
    unit = interval[-1]
    n = int(interval[:-1] or 1)
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 86400)


def last_bar_closed(bars: list[dict], interval: str) -> bool:
    """
    最新一根 K 线是否已经收盘。

    实盘必须区分：日线的 '2026-09-29 00:00:00' 这根要到 UTC 次日 00:00 才定盘，
    白天跑脚本时它的 close 只是当前价，收盘前还会变。用它发信号 = 误报。
    """
    try:
        # dt 是 UTC 时间字符串，必须用 timegm（mktime 会按本地时区解释，会错 8 小时）
        st_utc = calendar.timegm(time.strptime(bars[-1]["dt"], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return True
    return time.time() >= st_utc + interval_seconds(interval)


def current_signal(bars: list[dict], p: dict | None = None) -> dict:
    """返回最新一根 K 线的策略状态，用于实盘判断。"""
    p = dict(PARAMS, **(p or {}))
    ind = compute_indicators(bars, p)
    i = len(bars) - 1
    on = regime_on(ind, i)
    c, e, m = ind["close"][i], ind["ema_trend"][i], ind["macd"][i]
    return {
        "date": bars[i]["dt"],
        "close": c,
        "ema_trend": e,
        "macd": m,
        "atr": ind["atr"][i],
        "price_above_ema": (not math.isnan(e)) and c > e,
        "macd_above_zero": (not math.isnan(m)) and m > 0,
        "action": "持有 / 买入" if on else "空仓 / 离场",
        "should_hold": on,
    }


# ══════════════════════════ 4. 回测引擎 ══════════════════════════
def backtest(bars: list[dict], p: dict | None = None, verbose: bool = False) -> dict:
    """
    事件驱动回测。

    【无前视偏差的执行约定】
      · 趋势信号在第 i 根【收盘】确认 → 第 i+1 根【开盘】成交（入场与离场都是）
      · 唯一例外：黑天鹅硬止损是【挂单】，价格日内触及即成交 —— 这是真实可执行的
    """
    p = dict(PARAMS, **(p or {}))
    ind = compute_indicators(bars, p)
    n = len(bars)
    fee, slip = p["fee"], p["slip"]

    cash = p["init_capital"]
    pos = None
    pending_entry = None   # 待入场：次日开盘执行
    pending_exit = None    # 待离场：次日开盘执行（收盘确认的趋势信号）
    off_count = 0
    trades, equity, dates = [], [], []

    def _sell(i, px_raw, reason, bar_dt):
        """按给定价格平仓并记账。"""
        nonlocal cash, pos, pending_exit, off_count
        px = px_raw * (1 - slip)
        proceeds = pos["shares"] * px * (1 - fee)
        cash += proceeds
        trades.append({
            "entry_dt": pos["entry_dt"], "exit_dt": bar_dt,
            "entry": pos["entry"], "exit": px,
            "ret": px / pos["entry"] - 1,
            "pnl": proceeds - pos["cost"],
            "bars_held": i - pos["entry_i"],
            "reason": reason,
            "mfe": pos["peak"] / pos["entry"] - 1,
        })
        pos, pending_exit, off_count = None, None, 0

    for i in range(n):
        b = bars[i]
        o_, h_, l_, c_ = b["o"], b["h"], b["l"], b["c"]
        et = ind["ema_trend"][i]
        macd_i = ind["macd"][i]

        if pos is not None:
            pos["peak"] = max(pos["peak"], h_)

        # ── A. 执行前一根收盘确认的离场（次日开盘成交）──
        if pos is not None and pending_exit is not None and i == pending_exit:
            _sell(i, o_, "趋势破坏", b["dt"])

        # ── B. 硬止损（挂单，日内触及即成交；跳空优先）──
        if pos is not None and p["hard_sl"] > 0:
            stop = pos["entry"] * (1 - p["hard_sl"])
            if o_ <= stop:
                _sell(i, o_, "硬止损(跳空)", b["dt"])
            elif l_ <= stop:
                _sell(i, stop, "硬止损", b["dt"])

        # ── C. 执行前一根收盘确认的入场（次日开盘成交）──
        if pos is None and pending_entry is not None and i == pending_entry:
            px = o_ * (1 + slip)
            invest = cash * p["capital_frac"]
            shares = invest * (1 - fee) / px
            if shares > 0:
                pos = {"entry": px, "shares": shares, "cost": invest,
                       "entry_i": i, "entry_dt": b["dt"], "peak": max(o_, c_)}
                cash -= invest
            pending_entry = None
        elif pending_entry is not None and i > pending_entry:
            pending_entry = None

        # ── D. 收盘后判定信号 ──
        if pos is not None:
            if pending_exit is None:
                off = ((not math.isnan(et)) and c_ < et) or \
                      ((not math.isnan(macd_i)) and macd_i < 0)
                if off:
                    off_count += 1
                    if off_count >= p["exit_confirm"]:
                        pending_exit = i + 1
                else:
                    off_count = 0
        else:
            if pending_entry is None and i < n - 1 and regime_on(ind, i):
                pending_entry = i + 1

        equity.append(cash + (pos["shares"] * c_ if pos else 0.0))
        dates.append(b["dt"])

    # 期末强制平仓
    if pos is not None:
        px = bars[-1]["c"] * (1 - slip)
        proceeds = pos["shares"] * px * (1 - fee)
        cash += proceeds
        trades.append({"entry_dt": pos["entry_dt"], "exit_dt": bars[-1]["dt"],
                       "entry": pos["entry"], "exit": px, "ret": px / pos["entry"] - 1,
                       "pnl": proceeds - pos["cost"], "bars_held": n - 1 - pos["entry_i"],
                       "reason": "期末持仓", "mfe": pos["peak"] / pos["entry"] - 1})
        equity[-1] = cash

    return {"equity": equity, "dates": dates, "trades": trades, "params": p, "ind": ind}


def buy_hold(bars: list[dict], p: dict | None = None) -> dict:
    """买入持有基准（同样扣成本）。"""
    p = dict(PARAMS, **(p or {}))
    px0 = bars[0]["o"] * (1 + p["slip"])
    shares = p["init_capital"] * (1 - p["fee"]) / px0
    return {"equity": [shares * b["c"] for b in bars],
            "dates": [b["dt"] for b in bars], "trades": []}


# ══════════════════════════ 5. 绩效统计 ══════════════════════════
def max_drawdown(eq: list[float]) -> float:
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    return mdd


def stats(res: dict, bars_per_year: float = 365.0) -> dict:
    eq, tr = res["equity"], res["trades"]
    if not eq:
        return {}
    years = len(eq) / bars_per_year
    total = eq[-1] / eq[0] - 1
    cagr = (eq[-1] / eq[0]) ** (1 / max(years, 1e-9)) - 1
    mdd = max_drawdown(eq)

    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)

    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    sharpe = 0.0
    if rets:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(len(rets) - 1, 1))
        if sd > 0:
            sharpe = mu / sd * math.sqrt(bars_per_year)

    # 最大连续亏损
    cur = mx = 0
    for t in tr:
        cur = cur + 1 if t["pnl"] <= 0 else 0
        mx = max(mx, cur)

    return {
        "final_equity": eq[-1], "total_return": total, "cagr": cagr,
        "max_drawdown": mdd, "calmar": cagr / mdd if mdd > 0 else 0.0,
        "sharpe": sharpe, "n_trades": len(tr),
        "winrate": len(wins) / len(tr) if tr else 0.0,
        "profit_factor": gp / gl if gl > 0 else float("inf"),
        "avg_hold": sum(t["bars_held"] for t in tr) / len(tr) if tr else 0.0,
        "exposure": min(sum(t["bars_held"] for t in tr) / len(eq), 1.0),
        "max_consec_loss": mx,
        "exit_reasons": {r: sum(1 for t in tr if t["reason"] == r)
                         for r in {t["reason"] for t in tr}},
    }


def yearly(res: dict) -> dict:
    eq, dates = res["equity"], res["dates"]
    out, prev, start = {}, None, eq[0]
    for i, d in enumerate(dates):
        y = d[:4]
        if prev is None:
            prev = y
        if y != prev:
            out[prev] = eq[i - 1] / start - 1
            prev, start = y, eq[i - 1]
    if prev:
        out[prev] = eq[-1] / start - 1
    return out


# ══════════════════════════ 6. 输出 ══════════════════════════
def fmt_pct(v: float, sign: bool = True) -> str:
    return f"{v*100:+.1f}%" if sign else f"{v*100:.1f}%"


def print_result(name: str, bars: list[dict], p: dict) -> dict:
    res = backtest(bars, p)
    s = stats(res)
    bh = buy_hold(bars, p)
    sb = stats(bh)

    print(f"\n{'='*78}")
    print(f" {name}   {bars[0]['dt'][:10]} → {bars[-1]['dt'][:10]}  "
          f"({len(bars)/365:.1f} 年, {len(bars)} 根)")
    print(f"{'='*78}")
    print(f"  {'':22}{'总收益':>12}{'年化':>10}{'最大回撤':>11}{'Calmar':>9}{'夏普':>8}{'笔数':>7}{'胜率':>8}")
    print(f"  {'MACD-X 策略':22}{s['total_return']*100:>11,.0f}%{s['cagr']*100:>9.1f}%"
          f"{s['max_drawdown']*100:>10.1f}%{s['calmar']:>9.2f}{s['sharpe']:>8.2f}"
          f"{s['n_trades']:>7}{s['winrate']*100:>7.1f}%")
    print(f"  {'买入持有':22}{sb['total_return']*100:>11,.0f}%{sb['cagr']*100:>9.1f}%"
          f"{sb['max_drawdown']*100:>10.1f}%{sb['calmar']:>9.2f}{sb['sharpe']:>8.2f}{0:>7}{'-':>8}")
    print(f"  ── 策略年化 / 持有年化 = {s['cagr']/sb['cagr']:.2f}x    "
          f"策略回撤 / 持有回撤 = {s['max_drawdown']/sb['max_drawdown']:.2f}x")
    print(f"  ── 盈亏比 {s['profit_factor']:.2f}   平均持仓 {s['avg_hold']:.0f} 根   "
          f"持仓时间占比 {s['exposure']*100:.1f}%   最大连亏 {s['max_consec_loss']} 笔")
    print(f"  ── 出场原因: {s['exit_reasons']}")
    yr = yearly(res)
    print("  ── 年度: " + "  ".join(f"{k}:{v*100:+.0f}%" for k, v in sorted(yr.items()) if k != "2017"))
    return {"stats": s, "bh": sb, "yearly": yr, "res": res}


def main():
    ap = argparse.ArgumentParser(description="MACD-X 加密货币趋势跟随策略")
    ap.add_argument("--symbols", nargs="*",
                    default=["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"])
    ap.add_argument("--symbol", default=None, help="只跑单个标的")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--signal", action="store_true", help="只输出当前信号，不回测")
    ap.add_argument("--no-fetch", action="store_true", help="使用本地 data/ 缓存，不联网")
    ap.add_argument("--trend-ema", type=int, default=None, help="覆盖趋势均线周期")
    ap.add_argument("--confirm", type=int, default=None, help="覆盖离场确认根数")
    ap.add_argument("--no-mail", action="store_true", help="本次不发送邮件")
    ap.add_argument("--include-today", action="store_true",
                    help="把尚未收盘的最新一根 K 线也算进信号（默认剔除，避免盘中误报）")
    ap.add_argument("--no-proxy", action="store_true", help="本次不走代理（强制直连）")
    ap.add_argument("--proxy", default=None,
                    help="覆盖代理地址，例如 http://127.0.0.1:7897")
    ap.add_argument("--retries", type=int, default=None,
                    help="覆盖每页最大重试次数（默认 6）")
    ap.add_argument("--test-mail", action="store_true", help="发送一封测试邮件后退出")
    ap.add_argument("--capital", type=float, default=1000.0,
                    help="参考本金（仅用于邮件里计算建议下单数量），默认 1000")
    ap.add_argument("--frac", type=float, default=0.7,
                    help="建议仓位比例（邮件用），默认 0.7")
    ap.add_argument("--log-file", default=None,
                    help="把本次输出同时写入该文件（UTF-8 带 BOM），定时任务用")
    args = ap.parse_args()

    if args.log_file and _LOG_FP is None:   # __main__ 里可能已经开过了
        start_logging(args.log_file)
        _log_header(sys.argv)

    # ── 代理与重试设置（必须在任何网络请求之前）──
    if args.proxy:
        apply_proxy(True, http=args.proxy, https=args.proxy)
    elif args.no_proxy:
        apply_proxy(False)
    else:
        apply_proxy(PROXY["enabled"])
    if args.retries:
        RETRY["per_page"] = max(1, args.retries)

    if args.signal:
        print(f"\n[MACD-X] {now_cn()}（北京时间）  代理: "
              f"{'开 ' + PROXY['http'] if PROXY['enabled'] else '关'}   "
              f"重试: {RETRY['per_page']} 次/页")

    if args.test_mail:
        ok = send_email("[MACD-X] 邮件推送测试",
                        "如果你收到这封邮件，说明 MACD-X 的邮件推送配置正常。\n"
                        f"发件：{EMAIL_CONFIG['from']}\n"
                        f"收件：{EMAIL_CONFIG['to']}\n"
                        f"服务器：{EMAIL_CONFIG['server']}:{EMAIL_CONFIG['port']}\n\n"
                        "之后只会在「买入 / 离场」信号出现时收到邮件。")
        print("测试邮件发送成功 ✓" if ok else "测试邮件发送失败 ✗（请检查授权码与服务器端口）")
        return

    p = dict(PARAMS)
    if args.trend_ema:
        p["trend_ema"] = args.trend_ema
    if args.confirm is not None:
        p["exit_confirm"] = args.confirm

    symbols = [args.symbol] if args.symbol else args.symbols
    bpy = 365.0 if args.interval.endswith("d") else 365.0 * 6  # 日线/4h 的年化因子

    for sym in symbols:
        try:
            bars, src, stale = load_bars(sym, args.interval, args.no_fetch)
        except Exception as e:
            print(f"  [跳过] {sym}: {e}")
            continue
        if len(bars) < p["trend_ema"] + 50:
            print(f"  [跳过] {sym} 数据不足（{len(bars)} 根）")
            continue

        if args.signal:
            sig_bars = bars
            if not args.include_today and not last_bar_closed(sig_bars, args.interval):
                sig_bars = sig_bars[:-1]
                print(f"\n  [注意] 最新一根 {args.interval} K 线尚未收盘，"
                      f"已自动剔除，信号基于上一根已收盘 K 线"
                      f"（{sig_bars[-1]['dt']}）。用 --include-today 可强制包含。")
            if len(sig_bars) < p["trend_ema"] + 50:
                print(f"  [跳过] {sym} 已收盘数据不足（{len(sig_bars)} 根）")
                continue
            sg = current_signal(sig_bars, p)
            print(f"\n【{sym} 当前信号】  数据源: {src}" +
                  (f"  ⚠ 数据已滞后 {stale:.1f} 天" if stale > 2 else ""))
            print(f"  最新 K 线      : {sg['date']}")
            print(f"  收盘价         : ${sg['close']:,.4f}")
            print(f"  EMA({p['trend_ema']})     : ${sg['ema_trend']:,.4f}   "
                  f"价格{'在均线上方 ✓' if sg['price_above_ema'] else '在均线下方 ✗'}")
            print(f"  MACD 快线      : {sg['macd']:+.4f}   "
                  f"{'在零轴上方 ✓' if sg['macd_above_zero'] else '在零轴下方 ✗'}")
            print(f"  ATR(14)        : ${sg['atr']:,.4f}")
            print(f"  →  策略动作     : 【{sg['action']}】")
            if not args.no_mail:
                notify_if_changed(sym, args.interval, sg,
                                  capital=args.capital, frac=args.frac,
                                  src=src, stale=stale)
        else:
            print_result(sym, bars, p)

    if not args.signal:
        print(f"\n{'='*78}")
        print(" 说明：策略为趋势状态跟随，年均约 4 笔交易，不需要盯盘。")
        print(" 历史回测不代表未来收益，本脚本仅供研究，不构成投资建议。")
        print(f"{'='*78}\n")


if __name__ == "__main__":
    _rc = 0
    # 先把 --log-file 捞出来，这样连 argparse 报错都能记进日志
    _lf = _prescan_log_file(sys.argv)
    if _lf and _LOG_FP is None:
        try:
            start_logging(_lf)
            _log_header(sys.argv)
        except Exception as e:
            print(f"[警告] 日志文件打开失败：{e}")
    try:
        main()
    except SystemExit as e:
        _rc = e.code if isinstance(e.code, int) else 0
        _log_footer(_rc)
        raise
    except BaseException:
        traceback.print_exc()
        _rc = 1
        _log_footer(_rc)
        raise
    _log_footer(_rc)
