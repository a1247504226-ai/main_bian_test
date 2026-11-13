# -*- coding: utf-8 -*-
"""实盘下单入口（默认币安测试网）。

命令
----
    python trade.py status                 # 看策略应有状态 + 交易所实际持仓
    python trade.py sync                   # 让交易所持仓对齐策略信号（自动开/平/反手）
    python trade.py open long 150          # 手动开多，名义 150U
    python trade.py close                  # 手动全平

安全
----
* 只读 config.json 的 exchange 段；testnet=True 时**不可能**连主网。
* exchange.enabled=False 时任何下单命令都会被拒绝。
* sync 是幂等的：已经是目标状态就不会重复下单。
"""

import argparse
import datetime
import json
import os
import sys
import time

import live_monitor as L
from exchange import BinanceError, BinanceFutures

LOG_PATH = os.path.join(L.HERE, "trade.log")
LOCK_PATH = os.path.join(L.HERE, ".order.lock")


class OrderLock:
    """跨进程下单互斥锁（Windows / Linux 通用，纯标准库）。

    [坑47] cron 每分钟/每小时触发，用户又可能同时手动执行 trade.py。
    两边都查到「空仓」→ 各开一单 = 双开仓位。
    用 O_CREAT|O_EXCL 建排他锁文件实现互斥；TTL 兜住进程崩溃后的残留锁。
    """

    LOCK_TTL = 600          # 10 分钟；正常下单远小于此

    def __init__(self, path=None):
        self.path = path or LOCK_PATH
        self.held = False

    def _clear_stale(self):
        try:
            if os.path.exists(self.path) and \
                    time.time() - os.path.getmtime(self.path) > self.LOCK_TTL:
                os.remove(self.path)
        except OSError:
            pass

    def acquire(self):
        self._clear_stale()
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"pid={os.getpid()} at={time.time():.0f}\n".encode())
                finally:
                    os.close(fd)
                self.held = True
                return
            except FileExistsError:
                if attempt == 0:
                    time.sleep(1.0)
                    self._clear_stale()
                    continue
        raise RuntimeError(
            "另一个下单进程正在运行（或残留 .order.lock），本次跳过以避免双开。"
            "确认无其他进程后可删除 live/.order.lock")

    def release(self):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.held = False


def log(msg, console=True):
    """写订单日志：文件必写，控制台按需显示。

    [坑31] 时间戳固定用 UTC+8，与邮件、monitor.log 保持一致；
    Linux 服务器是 UTC 时也不会出现"日志时间比邮件时间差 8 小时"。
    """
    line = f"[{datetime.datetime.now(L.CST):%Y-%m-%d %H:%M:%S}] {msg}"
    if console:
        print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                                # noqa: BLE001
        pass


def build_client(cfg, allow_disabled=False):
    """创建交易所客户端。

    [坑41] `exchange.enabled=false` 只应拦住「下单」，不应拦住「查看」——
    否则用户想停下单确认仓位是否安全时，反而什么都看不到。
    status 命令传 allow_disabled=True。
    """
    e = cfg.get("exchange") or {}
    if not e.get("enabled") and not allow_disabled:
        sys.exit("[拒绝] config.json 里 exchange.enabled = false，下单已禁用"
                 "（查看持仓可用 trade.py status）")
    if not e.get("api_key") or not e.get("api_secret"):
        sys.exit("[拒绝] exchange.api_key / api_secret 未配置")
    ex = BinanceFutures(
        e["api_key"], e["api_secret"],
        testnet=bool(e.get("testnet", True)),
        allow_mainnet=bool(e.get("allow_mainnet", False)),
        log_file=LOG_PATH,
    )
    ex.min_interval = float(e.get("min_interval_sec", 1.0))
    return ex, e


def _fmt_pos(p):
    if p["side"] == "flat":
        return "空仓"
    return f"{p['side']} {p['qty']} @ {p['entry']:,.2f}（{p['leverage']}x, {p['margin_type']}）"


def cmd_status(ex, e, cfg):
    symbol = cfg.get("symbol", "BTCUSDT")
    print("=== 策略侧 ===")
    try:
        snap = L.snapshot(cfg)
        print(f"  当前价      {snap['price']:,.2f}")
        print(f"  4h EMA50/200 {snap['ema_fast']:,.0f} / {snap['ema_slow']:,.0f}")
        print(f"  应有状态    {snap['want']}")
        st = L.load_state()
        cd = int(st.get("cooldown_until", 0) or 0)
        now_ms = int(time.time() * 1000)
        if now_ms < cd:
            print(f"  冷却中      还剩 {(cd - now_ms) / 3600000:.1f} 小时")
        else:
            print("  冷却        无")
        flag = os.path.exists(os.path.join(L.HERE, "STOP.flag"))
        print(f"  停用开关    {'STOP.flag 存在（已停用，sync 会被拒绝）' if flag else '无'}")
    except Exception as ex_:
        print(f"  取行情失败：{ex_}")
    print("=== 交易所侧 ===")
    print(f"  网络        {'测试网' if ex.testnet else '⚠ 主网'}")
    print(f"  时间偏移    {ex._offset_ms:+d} ms")
    # [坑43] 交易所查询也要兜异常：限流时直接崩栈，用户看不到任何有用信息
    try:
        print(f"  持仓        {_fmt_pos(ex.position(symbol))}")
        b = ex.balance()
        print(f"  余额        {b['balance']:,.2f} USDT（可用 {b['available']:,.2f}）")
    except BinanceError as err:
        print(f"  查询失败    {err}")
        if err.code == -1003:
            print("              → IP 被限流，稍后再试或换网络（测试网对共享 IP 敏感）")


def cmd_sync(ex, e, cfg):
    symbol = cfg.get("symbol", "BTCUSDT")
    # [坑39] 杠杆在 config 里有两处：顶层 leverage（邮件/回测口径）和
    # exchange.leverage（下单口径）。只改一处会造成「邮件说 2x、实际下 3x」。
    top_lev = float(cfg.get("leverage", 2.0))
    lev = float(e.get("leverage", top_lev))
    if abs(lev - top_lev) > 1e-9:
        log(f"[警告] 杠杆配置不一致：config.leverage={top_lev:g}x，"
            f"exchange.leverage={lev:g}x —— 按下单段 {lev:g}x 执行")
    notional = float(e.get("notional_usdt", 150))
    margin_type = e.get("margin_type", "CROSSED")

    snap = L.snapshot(cfg)

    # [坑38] 行情新鲜度检查：数据源异常时可能返回陈旧 K 线，
    # 按几天前的信号下单 = 下错单。1h K 线正常在 1~2 小时内，
    # 超过 3 小时直接拒绝（宁可不下单）。
    bar_t = int(snap.get("bar_t") or 0)
    if bar_t:
        age_h = (time.time() * 1000 - bar_t) / 3600000
        if age_h > 3.0:
            sys.exit(f"[拒绝] 行情数据过旧：最后一根 K 线在 {age_h:.1f} 小时前，"
                     f"不下单。请检查网络或行情源")

    want = snap["want"]
    pos = ex.position(symbol)
    age_txt = (f" | K线距今 {(time.time() * 1000 - bar_t) / 3600000:.1f}h"
               if bar_t else "")
    log(f"信号 {want} | 交易所 {_fmt_pos(pos)} | 价格 {snap['price']:,.2f}{age_txt}")

    # [坑18] 停用开关：只拦「开新仓」，平仓永远放行——
    # 停用状态下更应该允许减仓，否则想退出都退不掉。
    stopped = os.path.exists(os.path.join(L.HERE, "STOP.flag"))

    if want == "flat":
        if pos["side"] == "flat":
            log("无需操作：已是空仓")
            return
        log(f"平仓：策略要求空仓（当前 {pos['side']} {pos['qty']}）")
        ex.close_position(symbol)
        log(f"完成：{_fmt_pos(ex.position(symbol))}")
        return

    if stopped:
        sys.exit("[拒绝] STOP.flag 存在（本机已停用），不开新仓。"
                 "如需平仓用 trade.py close；要恢复开仓请删除 live/STOP.flag")

    if pos["side"] == want:
        log(f"无需操作：已持{want}，与策略一致")
        return

    # [坑19] 冷却期只在「要开新仓」时生效（平仓不受限，否则止损失败会卡死）
    st = L.load_state()
    cd = int(st.get("cooldown_until", 0) or 0)
    now_ms = int(time.time() * 1000)
    if now_ms < cd:
        log(f"暂不开仓：策略冷却还剩 {(cd - now_ms) / 3600000:.1f} 小时"
            f"（强制开仓用 trade.py open {want}）")
        return

    if pos["side"] != "flat":
        log(f"反手：{pos['side']} → {want}，先平仓")
        ex.close_position(symbol)
        pos = ex.position(symbol)
        if pos["side"] != "flat":
            log("[警告] 平仓后仍有持仓，中止开仓以免反向叠加")
            return

    log(f"开{want}：名义 {notional}U / {lev:g}x")
    order = ex.open_position(symbol, want, notional, lev, margin_type=margin_type)
    log(f"完成：{_fmt_pos(ex.position(symbol))}  "
        f"成交 {order.get('executedQty')} @ {float(order.get('avgPrice') or 0):,.2f}")


def cmd_open(ex, e, cfg, direction, notional, lev):
    symbol = cfg.get("symbol", "BTCUSDT")
    pos = ex.position(symbol)
    if pos["side"] != "flat":
        sys.exit(f"[拒绝] 已有持仓 {_fmt_pos(pos)}，请先 close 或 sync")
    log(f"手动开{direction}：名义 {notional}U / {lev:g}x")
    order = ex.open_position(symbol, direction, notional, lev,
                             margin_type=e.get("margin_type", "CROSSED"))
    log(f"完成：{_fmt_pos(ex.position(symbol))}  "
        f"成交 {order.get('executedQty')} @ {float(order.get('avgPrice') or 0):,.2f}")


def cmd_close(ex, e, cfg):
    symbol = cfg.get("symbol", "BTCUSDT")
    log("手动平仓")
    order = ex.close_position(symbol)
    if order is None:
        log("本来就没有持仓")
        return
    log(f"完成：{_fmt_pos(ex.position(symbol))}  "
        f"成交 {order.get('executedQty')} @ {float(order.get('avgPrice') or 0):,.2f}")


def main():
    ap = argparse.ArgumentParser(description="BTC 策略实盘下单（默认测试网）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看策略与交易所状态")
    sub.add_parser("sync", help="让持仓对齐策略信号")
    p_open = sub.add_parser("open", help="手动开仓")
    p_open.add_argument("direction", choices=["long", "short"])
    p_open.add_argument("notional", nargs="?", type=float, default=None)
    p_open.add_argument("--lev", type=float, default=None)
    sub.add_parser("close", help="手动平仓")
    args = ap.parse_args()

    cfg = L.load_cfg()

    trading = args.cmd != "status"        # status 只是查看，不写运行分隔线
    if trading:
        log(f"===== trade.py {args.cmd} 开始 =====", console=False)

    # [坑47] 下单类命令全程持锁：cron 与手动同时跑时，后一个直接跳过，
    # 绝不允许两个进程各自查到「空仓」然后各开一单。
    lock = OrderLock() if trading else None
    t0 = time.time()
    rc = 0
    try:
        if lock:
            lock.acquire()

        # [坑40] 停用时拒绝开新仓（放在「开始」之后，日志能留痕）。
        # 用 sys.exit：SystemExit 不被下面的 except 捕获，但 finally 仍会释放锁。
        if args.cmd == "open" and os.path.exists(os.path.join(L.HERE, "STOP.flag")):
            log("[拒绝] STOP.flag 存在（本机已停用），不开仓")
            sys.exit("[拒绝] STOP.flag 存在（本机已停用）。要开仓请先删除 live/STOP.flag")

        ex, e = build_client(cfg, allow_disabled=(args.cmd == "status"))
        if args.cmd == "status":
            cmd_status(ex, e, cfg)
        elif args.cmd == "sync":
            cmd_sync(ex, e, cfg)
        elif args.cmd == "open":
            cmd_open(ex, e, cfg, args.direction,
                     args.notional or float(e.get("notional_usdt", 150)),
                     args.lev or float(e.get("leverage", 2.0)))
        elif args.cmd == "close":
            cmd_close(ex, e, cfg)
    except RuntimeError as err:               # 锁未获取 / 其他并发保护
        log(f"[跳过] {err}")
        rc = 2
    except BinanceError as err:
        log(f"[错误] {err}")
        if err.code == -1003:
            log("  → IP 被限流：带封禁时间的等解封；显示『权重耗尽』的约 1 分钟后重试。")
            log("    测试网对共享出口 IP 敏感，换手机热点可绕开。")
        rc = 1
    finally:
        if lock:
            lock.release()
        if trading:
            log(f"===== trade.py {args.cmd} 结束（{time.time() - t0:.1f}s）=====",
                console=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
