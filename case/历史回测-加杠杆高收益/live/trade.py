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
import json
import os
import sys
import time

import live_monitor as L
from exchange import BinanceError, BinanceFutures


def build_client(cfg):
    e = cfg.get("exchange") or {}
    if not e.get("enabled"):
        sys.exit("[拒绝] config.json 里 exchange.enabled = false，下单已禁用")
    if not e.get("api_key") or not e.get("api_secret"):
        sys.exit("[拒绝] exchange.api_key / api_secret 未配置")
    ex = BinanceFutures(
        e["api_key"], e["api_secret"],
        testnet=bool(e.get("testnet", True)),
        allow_mainnet=bool(e.get("allow_mainnet", False)),
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
    print(f"  持仓        {_fmt_pos(ex.position(symbol))}")
    b = ex.balance()
    print(f"  余额        {b['balance']:,.2f} USDT（可用 {b['available']:,.2f}）")


def cmd_sync(ex, e, cfg):
    symbol = cfg.get("symbol", "BTCUSDT")
    lev = float(e.get("leverage", 2.0))
    notional = float(e.get("notional_usdt", 150))
    margin_type = e.get("margin_type", "CROSSED")

    snap = L.snapshot(cfg)
    want = snap["want"]
    pos = ex.position(symbol)
    print(f"策略应有: {want} | 交易所实际: {_fmt_pos(pos)}")

    # [坑18] 停用开关：只拦「开新仓」，平仓永远放行——
    # 停用状态下更应该允许减仓，否则想退出都退不掉。
    stopped = os.path.exists(os.path.join(L.HERE, "STOP.flag"))

    if want == "flat":
        if pos["side"] == "flat":
            print("→ 已经是空仓，无需操作")
            return
        print("→ 策略要求空仓，平掉现有持仓")
        ex.close_position(symbol)
        print("完成：", _fmt_pos(ex.position(symbol)))
        return

    if stopped:
        sys.exit("[拒绝] STOP.flag 存在（本机已停用），不开新仓。"
                 "如需平仓用 trade.py close；要恢复开仓请删除 live/STOP.flag")

    if pos["side"] == want:
        print(f"→ 已持{want}，与策略一致，无需操作")
        return

    # [坑19] 冷却期只在「要开新仓」时生效（平仓不受限，否则止损失败会卡死）
    st = L.load_state()
    cd = int(st.get("cooldown_until", 0) or 0)
    now_ms = int(time.time() * 1000)
    if now_ms < cd:
        remain_h = (cd - now_ms) / 3600000
        print(f"[冷却中] 策略止损后冷却还剩 {remain_h:.1f} 小时，暂不开新仓")
        print(f"         如需强制开仓：python trade.py open {want}")
        return

    if pos["side"] != "flat":
        print(f"→ 需要反手（{pos['side']} → {want}），先平仓")
        ex.close_position(symbol)
        pos = ex.position(symbol)
        if pos["side"] != "flat":
            print("[警告] 平仓后仍有持仓，中止开仓以免反向叠加")
            return

    print(f"→ 开{want}，名义 {notional}U / {lev:g}x")
    order = ex.open_position(symbol, want, notional, lev, margin_type=margin_type)
    print(f"  成交 {order.get('executedQty')} @ {order.get('avgPrice')}，状态 {order.get('status')}")
    print("完成：", _fmt_pos(ex.position(symbol)))


def cmd_open(ex, e, cfg, direction, notional, lev):
    symbol = cfg.get("symbol", "BTCUSDT")
    pos = ex.position(symbol)
    if pos["side"] != "flat":
        sys.exit(f"[拒绝] 已有持仓 {_fmt_pos(pos)}，请先 close 或 sync")
    order = ex.open_position(symbol, direction, notional, lev,
                             margin_type=e.get("margin_type", "CROSSED"))
    print(f"开{direction}成交 {order.get('executedQty')} @ {order.get('avgPrice')}")
    print("当前持仓：", _fmt_pos(ex.position(symbol)))


def cmd_close(ex, e, cfg):
    symbol = cfg.get("symbol", "BTCUSDT")
    order = ex.close_position(symbol)
    if order is None:
        print("本来就没有持仓")
        return
    print(f"平仓成交 {order.get('executedQty')} @ {order.get('avgPrice')}")
    print("当前持仓：", _fmt_pos(ex.position(symbol)))


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

    # open 是纯开仓命令：停用时直接拒绝，连交易所都不用连
    if args.cmd == "open" and os.path.exists(os.path.join(L.HERE, "STOP.flag")):
        sys.exit("[拒绝] STOP.flag 存在（本机已停用）。要开仓请先删除 live/STOP.flag")

    try:
        ex, e = build_client(cfg)
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
    except BinanceError as err:
        print(f"[交易所错误] {err}")
        if err.code == -1003:
            print("  → IP 被限流。测试网对共享出口 IP 很敏感，等解封或换网络后再试。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
