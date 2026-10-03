# -*- coding: utf-8 -*-
"""status.py — 一屏看清：开关状态 / 当前信号 / 该下多少 / 账户与持仓差异

这是你日常唯一需要主动运行的脚本（只读，永远不会下单）。
它把「邮件推送」和「币安账户」两条线合并到一屏，方便你决定是否切到 live。

用法：
    python status.py            # 完整：含币安账户与持仓（需要 API 密钥）
    python status.py --offline  # 离线：只看信号与开关，不连币安
"""
import sys
from datetime import datetime

import common as C

W = 66


def hr(title=""):
    if not title:
        return "=" * W
    pad = max(0, W - len(title) - 2)
    return "=" * 2 + " " + title + " " + "=" * pad


def main(argv):
    offline = "--offline" in argv
    cfg = C.load_config()
    tr = cfg.get("trade") or {}
    ms = C.master_switch(cfg)
    profile_key = tr.get("profile", "B")
    profile = cfg["profiles"][profile_key]

    print(hr("BTC 杠杆策略 · 状态总览"))
    print(f"运行时间：{datetime.now(C.CST):%Y-%m-%d %H:%M:%S}（北京时间）")
    print()

    # ---------- ① 总开关 ----------
    print(hr("1. 总开关"))
    print(f"  当前模式        : {ms['mode']}")
    if ms["real_ok"]:
        print("  真实下单        : ⚠ 已放行（会自动向币安提交订单）")
    elif ms["live"]:
        print("  真实下单        : ✗ 未放行（双保险未开）")
    else:
        print("  真实下单        : ✓ 关闭（只推送信号，安全）")
    print(f"  说明            : {ms['reason']}")
    print(f"  只跑档位        : {profile_key}（{profile['name']}）")
    print(f"  杠杆 / 保证金   : {tr.get('leverage', 3)}× / {tr.get('margin_type', 'ISOLATED')}")
    print()

    # ---------- ② 信号 ----------
    bars = C.keep_closed(C.fetch_klines(cfg["symbol"], cfg["interval"],
                                        cfg.get("kline_limit", 1000),
                                        int(cfg["email"].get("max_retries", 3))))
    inf = C.compute_profile(profile, bars, cfg)
    inf["key"] = profile_key
    bar_dt = inf["bar_dt"].strftime("%Y-%m-%d")
    log_note = "（已收盘）"

    print(hr("2. 当前信号"))
    print(f"  信号日期        : {bar_dt} {log_note}")
    print(f"  最新价格        : {inf['px']:,.2f} USDT")
    print(f"  ATR14 / 日波动  : {inf['atr']:,.0f} / {inf['dvol']*100:.2f}%")
    print(f"  5 组方向        : {inf['dirs']}   （1=多 -1=空）")
    print(f"  综合方向        : {inf['side']}")
    print(f"  目标净敞口      : {inf['net_expo']:.4f}× 本金")
    print(f"  目标名义        : {abs(inf['net']):,.0f} USDT")
    print(f"  目标保证金      : {inf['margin']:,.0f} USDT"
          f"（占本金 {inf['margin']/inf['equity']*100:.1f}%）")
    print(f"  目标数量        : {inf['qty']:.6f} BTC")
    print(f"  爆仓价（3×逐仓）: {inf['liq']:,.0f}  （距现价 {abs(inf['liq']/inf['px']-1)*100:.1f}%）")
    print()

    if offline:
        print("  （--offline：跳过币安账户检查）")
        return 0

    # ---------- ③ 账户与持仓 ----------
    api_key = (tr.get("apiKey") or "").strip()
    if (not api_key) or ("在这里填" in api_key):
        print(hr("3. 币安账户"))
        print("  ✗ 尚未配置 API 密钥，跳过。")
        print("    填 config.json → trade.apiKey / trade.apiSecret")
        print("    或设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET")
        return 0

    try:
        import binance as B
        api = B.BinanceFutures(tr.get("apiKey"), tr.get("apiSecret"),
                               int(tr.get("recvWindow", 5000)))
        api.sync_time()
    except Exception as e:                                   # noqa: BLE001
        print(hr("3. 币安账户"))
        print(f"  ✗ 连接失败：{e}")
        return 1

    print(hr("3. 币安账户"))
    try:
        bal = api.usdt_balance()
        print(f"  钱包余额        : {bal['wallet']:,.2f} USDT")
        print(f"  可用余额        : {bal['available']:,.2f} USDT")
        eq_ref = bal["wallet"] if bal["wallet"] > 0 else inf["equity"]
    except Exception as e:                                   # noqa: BLE001
        print(f"  ✗ 读取余额失败：{e}")
        eq_ref = inf["equity"]

    try:
        pos = api.position(cfg["symbol"])
    except Exception as e:                                   # noqa: BLE001
        print(f"  ✗ 读取持仓失败：{e}")
        return 1

    # 按真实余额重算目标
    scale = eq_ref / float(inf["equity"]) if inf["equity"] else 1.0
    tgt_qty = inf["qty"] * scale * (1 if inf["net"] > 0 else -1)
    cur_qty = float(pos["positionAmt"]) if pos else 0.0
    delta = tgt_qty - cur_qty

    print(hr("4. 持仓对照"))
    if abs(cur_qty) < 1e-9:
        print("  当前持仓        : 空仓")
    else:
        print(f"  当前持仓        : {pos['side']} {abs(cur_qty):.6f} BTC"
              f"  开仓价 {float(pos['entryPrice']):,.2f}")
        print(f"  未实现盈亏      : {float(pos['unRealizedProfit']):+,.2f} USDT")
        print(f"  账户杠杆        : {pos.get('leverage')}×")
    print(f"  目标持仓        : {tgt_qty:+.6f} BTC"
          f"  （按真实余额 {eq_ref:,.2f} 换算）")
    print(f"  需要调整        : {delta:+.6f} BTC"
          f"  （名义 {delta*inf['px']:+,.0f} USDT）")
    print()

    # ---------- ⑤ 结论 ----------
    print(hr("5. 结论 / 下一步"))
    thr = float(tr.get("rebalance_threshold", 0.05))
    rel = abs(delta) / max(abs(tgt_qty), 1e-9) if abs(tgt_qty) > 1e-9 else 0.0
    if abs(delta) * inf["px"] < float(tr.get("min_notional", 5.0)) or rel < thr:
        print("  ✓ 实际持仓与目标基本一致，无需调仓。")
    else:
        print(f"  ⚠ 存在偏差 {rel*100:.1f}%（阈值 {thr*100:.0f}%），需要调仓。")
        if not ms["real_ok"]:
            print("    当前是观察模式 → 请手动登录币安按上方『目标持仓』调整，")
            print("    或把总开关切到 live 让脚本自动执行。")
        else:
            print("    总开关已放行 → 运行 run_trade.bat 执行（或已由定时任务完成）。")
    print()
    print(hr())
    print("  日志目录：logs\\")
    print("  想切换模式：编辑 config.json 顶部 _master_switch")
    print(hr())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                   # noqa: BLE001
        print(f"运行失败：{e!r}")
        sys.exit(1)
