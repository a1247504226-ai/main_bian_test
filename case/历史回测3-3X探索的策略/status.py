# -*- coding: utf-8 -*-
"""status.py — 一屏看清：开关状态 / 当前信号 / 该下多少 / 账户与持仓差异

支持两种运行模式（由 config.json 的 run_mode 决定）：
    run_mode = "single" → 只跑 config.symbol（默认 BTCUSDT），全部本金押它
    run_mode = "multi"  → 跑 config.symbols 全部（默认四币等权：BTC/ETH/BNB/SOL）
                          资金 = 账户总权益 ÷ 币数（动态再平衡）
命令行可临时覆盖：--single / --multi / --symbols BTCUSDT,SOLUSDT

这是你日常唯一需要主动运行的脚本（只读，永远不会下单）。
它把「邮件推送」和「币安账户」两条线合并到一屏，方便你决定是否切到 live。

用法：
    python status.py            # 完整：含币安账户与持仓（需要 API 密钥）
    python status.py --offline  # 离线：只看信号与开关，不连币安
"""
import sys
from datetime import datetime

import common as C


def hr(title="", w=66):
    if not title:
        return "=" * w
    pad = max(0, w - len(title) - 2)
    return "=" * 2 + " " + title + " " + "=" * pad


def main(argv):
    offline = "--offline" in argv
    cfg = C.load_config()
    tr = cfg.get("trade") or {}
    ms = C.master_switch(cfg)
    profile_key = tr.get("profile", "B")
    profile = cfg["profiles"][profile_key]

    syms = C.symbols_of(cfg, override=C._symbols_from_argv(argv))
    multi = len(syms) >= 2
    n = len(syms)
    head = "/".join(C.coin_name(s) for s in syms)
    W = 78 if multi else 66

    print(hr(f"{head} 杠杆策略 · 状态总览", W))
    print(f"运行时间：{datetime.now(C.CST):%Y-%m-%d %H:%M:%S}（北京时间）")
    print()

    # ---------- ① 总开关 ----------
    print(hr("1. 总开关", W))
    print(f"  当前模式        : {ms['mode']}")
    if ms["real_ok"]:
        print("  真实下单        : ⚠ 已放行（会自动向币安提交订单）")
    elif ms["live"]:
        print("  真实下单        : ✗ 未放行（双保险未开）")
    else:
        print("  真实下单        : ✓ 关闭（只推送信号，安全）")
    print(f"  说明            : {ms['reason']}")
    print(f"  运行模式        : {'四币等权（%d 个交易对）' % n if multi else '仅单标的'}"
          f"   run_mode={cfg.get('run_mode') or '(未填，按 symbols 长度自动判断)'}")
    print(f"  交易对          : {', '.join(syms)}")
    print(f"  只跑档位        : {profile_key}（{profile['name']}）")
    print(f"  杠杆 / 保证金   : {tr.get('leverage', 3)}× / {tr.get('margin_type', 'ISOLATED')}")
    if multi:
        print(f"  资金分配        : 账户总权益 ÷ {n}（动态再平衡，账户涨了配额自动放大）")
    print()

    # ---------- ② 信号 ----------
    cfg_eq = float(profile["equity"])
    per_eq = cfg_eq / n if multi else cfg_eq
    bars_map = {}
    infos = []
    for s in syms:
        b = C.keep_closed(C.fetch_klines(s, cfg["interval"], cfg.get("kline_limit", 1000),
                                        int(cfg["email"].get("max_retries", 3))))
        bars_map[s] = b
        inf = C.compute_profile(profile, b, cfg, symbol=s, equity_override=per_eq)
        inf["key"] = s
        inf["coin"] = C.coin_name(s)
        infos.append(inf)
    bar_dt = infos[0]["bar_dt"].strftime("%Y-%m-%d")

    print(hr("2. 当前信号", W))
    print(f"  信号日期        : {bar_dt}（已收盘）")
    if multi:
        print(f"  口径            : 按配置本金 {cfg_eq:,.0f} U ÷ {n} = 每币 {per_eq:,.0f} U 计算")
        print()
        print(f"  {'币种':<6}{'方向':<6}{'净敞口':>9}{'目标名义':>11}{'目标保证金':>12}"
              f"{'目标数量':>15}{'爆仓价':>12}{'日波动':>8}")
        for inf in infos:
            print(f"  {inf['coin']:<6}{inf['side']:<6}{inf['net_expo']:>8.3f}×"
                  f"{abs(inf['net']):>10,.0f}U{inf['margin']:>11,.0f}U"
                  f"{inf['qty']:>15.6f}{inf['liq']:>12,.0f}{inf['dvol'] * 100:>7.2f}%")
        print()
        for inf in infos:
            print(f"  {inf['coin']} 5 组方向 : {inf['dirs']}   （1=多 -1=空）")
    else:
        inf = infos[0]
        print(f"  最新价格        : {inf['px']:,.2f} USDT")
        print(f"  ATR14 / 日波动  : {inf['atr']:,.0f} / {inf['dvol'] * 100:.2f}%")
        print(f"  5 组方向        : {inf['dirs']}   （1=多 -1=空）")
        print(f"  综合方向        : {inf['side']}")
        print(f"  目标净敞口      : {inf['net_expo']:.4f}× 本金")
        print(f"  目标名义        : {abs(inf['net']):,.0f} USDT")
        print(f"  目标保证金      : {inf['margin']:,.0f} USDT"
              f"（占本金 {inf['margin'] / inf['equity'] * 100:.1f}%）")
        print(f"  目标数量        : {inf['qty']:.6f} {C.coin_name(syms[0])}")
        print(f"  爆仓价（3×逐仓）: {inf['liq']:,.0f}  "
              f"（距现价 {abs(inf['liq'] / inf['px'] - 1) * 100:.1f}%）")
    print()

    if offline:
        print("  （--offline：跳过币安账户检查）")
        return 0

    # ---------- ③ 账户与持仓 ----------
    api_key = (tr.get("apiKey") or "").strip()
    if (not api_key) or ("在这里填" in api_key):
        print(hr("3. 币安账户", W))
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
        print(hr("3. 币安账户", W))
        print(f"  ✗ 连接失败：{e}")
        return 1

    print(hr("3. 币安账户", W))
    eq_ref = cfg_eq
    try:
        bal = api.usdt_balance()
        eq_ref = bal["wallet"] + bal["unrealized"]
        if eq_ref <= 0:
            eq_ref = bal["wallet"] if bal["wallet"] > 0 else cfg_eq
        print(f"  钱包余额        : {bal['wallet']:,.2f} USDT")
        print(f"  可用余额        : {bal['available']:,.2f} USDT")
        print(f"  未实现盈亏      : {bal['unrealized']:+,.2f} USDT")
        print(f"  账户总权益      : {eq_ref:,.2f} USDT（钱包 + 未实现，与回测同口径）")
        if multi:
            print(f"  每币配额        : {eq_ref / n:,.2f} USDT（= 总权益 ÷ {n}）")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ✗ 读取余额失败：{e}")

    quota = eq_ref / n

    # ---------- ④ 持仓对照 ----------
    print(hr("4. 持仓对照", W))
    thr = float(tr.get("rebalance_threshold", 0.05))
    cap_cfg = float(tr.get("max_order_notional") or 0)
    rows = []
    for inf in infos:
        sym = inf["symbol"]
        coin = inf["coin"]
        try:
            pos = api.position(sym)
        except Exception as e:                               # noqa: BLE001
            print(f"  ✗ {sym} 读取持仓失败：{e}")
            return 1
        cur = float(pos["positionAmt"]) if pos else 0.0
        # 按真实权益重算目标
        scale = quota / float(inf["equity"]) if inf["equity"] else 1.0
        tgt_qty = inf["qty"] * scale * (1 if inf["net"] > 0 else -1)
        cap = cap_cfg if cap_cfg > 0 else quota * 3.15
        tgt_qty = max(-cap / inf["px"], min(cap / inf["px"], tgt_qty))
        delta = tgt_qty - cur
        rel = abs(delta) / abs(tgt_qty) if abs(tgt_qty) > 1e-9 else (1.0 if delta else 0.0)
        rows.append({"inf": inf, "pos": pos, "cur": cur, "tgt": tgt_qty,
                     "delta": delta, "rel": rel})

    if multi:
        print(f"  {'币种':<6}{'当前持仓':>15}{'目标持仓':>15}{'需要调整':>15}"
              f"{'偏差':>8}{'未实现盈亏':>13}")
        for r in rows:
            inf, pos = r["inf"], r["pos"]
            print(f"  {inf['coin']:<6}{r['cur']:>+15.6f}{r['tgt']:>+15.6f}"
                  f"{r['delta']:>+15.6f}{r['rel'] * 100:>7.1f}%"
                  f"{(float(pos['unRealizedProfit']) if pos else 0):>+13,.2f}")
        print()
        print(f"  {'币种':<6}{'当前方向':<9}{'目标方向':<9}{'开仓价':>12}{'标记价':>12}"
              f"{'强平价':>12}{'逐仓保证金':>12}")
        for r in rows:
            inf, pos = r["inf"], r["pos"]
            print(f"  {inf['coin']:<6}{(pos['side'] if pos else '空仓'):<9}"
                  f"{inf['side']:<9}{(float(pos['entryPrice']) if pos else 0):>12,.2f}"
                  f"{(float(pos['markPrice']) if pos else 0):>12,.2f}"
                  f"{(float(pos['liquidationPrice']) if pos else 0):>12,.2f}"
                  f"{(float(pos['isolatedMargin']) if pos else 0):>12,.2f}")
    else:
        r = rows[0]
        inf, pos = r["inf"], r["pos"]
        if abs(r["cur"]) < 1e-9:
            print("  当前持仓        : 空仓")
        else:
            print(f"  当前持仓        : {pos['side']} {abs(r['cur']):.6f} {inf['coin']}"
                  f"  开仓价 {float(pos['entryPrice']):,.2f}")
            print(f"  未实现盈亏      : {float(pos['unRealizedProfit']):+,.2f} USDT")
            print(f"  账户杠杆        : {pos.get('leverage')}×")
        print(f"  目标持仓        : {r['tgt']:+.6f} {inf['coin']}"
              f"  （按真实权益 {eq_ref:,.2f} 换算）")
        print(f"  需要调整        : {r['delta']:+.6f} {inf['coin']}"
              f"  （名义 {r['delta'] * inf['px']:+,.0f} USDT）")
    print()

    # ---------- ⑤ 结论 ----------
    print(hr("5. 结论 / 下一步", W))
    need = []
    for r in rows:
        inf = r["inf"]
        try:
            f = api.symbol_filters(inf["symbol"])
            aligned = api.align_qty(abs(r["delta"]), inf["symbol"])
            mn = f["min_notional"]
            mq = f["min_qty"]
        except Exception:                                    # noqa: BLE001
            aligned, mn, mq = abs(r["delta"]), 5.0, 0.0
        if aligned < mq or r["rel"] < thr:
            print(f"  ✓ {inf['coin']:<6} 与目标基本一致，无需调仓"
                  f"（偏差 {r['rel'] * 100:.1f}% < {thr * 100:.0f}%）")
        else:
            ntl = aligned * inf["px"]
            reduces = abs(r["tgt"]) < abs(r["cur"]) and (r["tgt"] * r["cur"] >= 0)
            if ntl < mn and not reduces:
                print(f"  ⚠ {inf['coin']:<6} 需要调仓，但本次名义 {ntl:,.1f} U < "
                      f"交易所最小名义 {mn:,.0f} U → 会被拒单")
                need.append((inf["coin"], "too_small"))
            else:
                print(f"  ⚠ {inf['coin']:<6} 存在偏差 {r['rel'] * 100:.1f}%，需要调仓"
                      f"（名义 {ntl:,.0f} U）")
                need.append((inf["coin"], "ok"))
    print()
    if not need:
        print("  → 全部无需调仓。")
    elif not ms["real_ok"]:
        print("  → 当前是观察模式：请手动登录币安按上方『目标持仓』调整，")
        print("    或把总开关切到 live 让脚本自动执行。")
    else:
        print("  → 总开关已放行：运行 run_trade.bat 执行（或已由定时任务完成）。")
    print()
    print(hr(w=W))
    print("  日志目录：logs\\")
    print("  想切换模式：编辑 config.json 顶部 _master_switch（是否真实下单）")
    print("              编辑 config.json 的 run_mode（仅 BTC / 四币合一）")
    print(hr(w=W))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                   # noqa: BLE001
        print(f"运行失败：{e!r}")
        sys.exit(1)
