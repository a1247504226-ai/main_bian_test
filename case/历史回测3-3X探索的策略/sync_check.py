# -*- coding: utf-8 -*-
"""
sync_check.py — 上线前对账与自检（只读，绝不下单）

用途：在你把 API 白名单加好、准备开启自动下单之前，先跑这个脚本，
      它会告诉你「交易所现在的状态」和「脚本打算做什么」是否一致。

检查项：
  1. 网络连通性（ping）与 API 签名是否正常
  2. 服务器时间校准（本地时差，避免 -1021）
  3. 账户 USDT 余额（钱包 / 可用 / 未实现盈亏）
  4. 当前 BTCUSDT 持仓（数量、开仓价、标记价、强平价、逐仓保证金、杠杆）
  5. 交易所当前杠杆 / 保证金模式 是否与 config 一致
  6. 目标仓位 vs 实际持仓的差异（复用 trade.py 的同一套公式）
     → 明确指出「若不 dry_run 会执行什么动作」
  7. 精度参数（qty_step / min_qty / min_notional）与实际所需数量的对比
  8. config.trade.enabled / dry_run 当前开关状态

用法：
  python sync_check.py           # 完整只读对账
  python sync_check.py --quick   # 跳过 K 线拉取，只查账户与持仓

【安全】本脚本不含任何下单调用（POST /fapi/v1/order 不会出现）。
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common as C                                          # noqa: E402
import trade as T                                           # noqa: E402
from binance import BinanceFutures, BinanceError            # noqa: E402

OK = "[OK]  "
BAD = "[!!]  "
WARN = "[??]  "


def _hr(title=""):
    line = "=" * 66
    return f"\n{line}\n  {title}\n{line}" if title else line


def _fmt(x, n=2):
    try:
        return f"{float(x):,.{n}f}"
    except Exception:                                       # noqa: BLE001
        return str(x)


def main(argv):
    quick = "--quick" in argv
    out = []
    problems = []            # 致命问题（阻止上线）
    notices = []             # 提示（不阻止，但需知晓）

    def say(s=""):
        out.append(s)
        print(s)

    say(_hr("币安合约对账自检 · 只读模式"))
    say(f"运行时间：{datetime.now(C.CST):%Y-%m-%d %H:%M:%S}（北京时间）")

    # ---------- 配置 ----------
    cfg = C.load_config()
    tr = cfg.get("trade") or {}
    symbol = cfg["symbol"]
    profile_key = tr.get("profile", "B")
    L_trade = int(tr.get("leverage", cfg.get("leverage", 3)))
    ms = C.master_switch(cfg)
    enabled = ms["real_ok"]
    dry_cfg = bool(tr.get("dry_run", True))
    margin_type = tr.get("margin_type", "ISOLATED")

    say(_hr("0. 总开关状态（config.json 顶部 _master_switch）"))
    say(f"  mode                          = {ms['mode']}")
    say(f"  i_understand_real_money       = {bool((cfg.get('_master_switch') or {}).get('i_understand_real_money', False))}")
    say(f"  → 综合结论：{ms['reason']}")
    say(f"  {'⚠ 允许真实下单' if enabled else '✓ 当前不会真实下单（只对账/只推送）'}")
    say(f"  trade.dry_run    = {dry_cfg}   "
        f"{'→ 预演模式' if dry_cfg else '→ 真实提交'}")
    say(f"  trade.profile    = {profile_key}")
    say(f"  trade.leverage   = {L_trade}×   保证金模式 = {margin_type}")
    say(f"  config.leverage  = {cfg.get('leverage')}×  （回测/信号用的杠杆）")
    if float(cfg.get("leverage", 0)) != float(L_trade):
        notices.append(f"config.leverage={cfg.get('leverage')} 与 trade.leverage={L_trade} "
                       f"不一致。信号仓位按 config.leverage 计算，下单杠杆按 trade.leverage，"
                       f"建议两者保持一致以免语义混乱。")
    if ms["live"] and dry_cfg:
        notices.append("总开关已是 live，但 trade.dry_run 仍为 true：仍不会真实下单。"
                       "要真上线需把 trade.dry_run 改成 false。")

    api_key = (tr.get("apiKey") or "").strip()
    api_secret = (tr.get("apiSecret") or "").strip()
    if (not api_key) or (not api_secret) or ("在这里填" in api_key):
        say(_hr("1. 密钥"))
        say(BAD + "config.trade 里的 apiKey / apiSecret 还是占位符。")
        say("      请填入真实密钥（或用环境变量 BINANCE_API_KEY / BINANCE_API_SECRET）。")
        say("      未填密钥时无法继续对账。")
        _dump(out)
        return 2

    # ---------- 连接与签名 ----------
    say(_hr("1. 网络与签名"))
    api = BinanceFutures(api_key, api_secret, int(tr.get("recvWindow", 5000)),
                         C.setup_logger("sync_check"))
    try:
        api.ping()
        say(OK + "ping 成功，可达币安合约 API。")
    except Exception as e:                                  # noqa: BLE001
        say(BAD + f"ping 失败：{e}")
        say("      排查：网络 / 代理 / 白名单是否已加。")
        _dump(out)
        return 2

    off = api.sync_time(force=True)
    say(f"{OK} 服务器时间已校准，本地偏移 {off} ms"
        + ("（正常）" if abs(off) < 1000 else "（偏大，注意系统时钟）"))
    if abs(off) >= 1000:
        notices.append(f"本地时间与服务器相差 {off} ms，建议开启系统自动同步时间。")

    # 用一个签名接口验证密钥/签名是否正确
    try:
        acc = api.account()
        say(OK + "API 签名验证通过（已成功读取账户）。")
    except BinanceError as e:
        say(BAD + f"签名/权限错误：[{e.code}] {e.msg}")
        if e.code in (-2015, -2014):
            say("      排查：API Key 是否绑定了『合约』权限；是否已把服务器出口 IP 加入白名单；"
                "签名是否用了 Secret。")
        _dump(out)
        return 2

    # ---------- 余额 ----------
    say(_hr("2. 账户余额"))
    bal = api.usdt_balance()
    say(f"  钱包余额（walletBalance）  : {_fmt(bal['wallet'])} USDT")
    say(f"  可用余额（availableBalance）: {_fmt(bal['available'])} USDT")
    say(f"  未实现盈亏（unrealizedProfit）: {_fmt(bal['unrealized'])} USDT")
    # 顺带看 acc 里的保证金占用
    try:
        say(f"  已用保证金（totalPositionInitialMargin）: "
            f"{_fmt(acc.get('totalPositionInitialMargin', 0))} USDT")
        say(f"  维持保证金（totalMaintMargin）: "
            f"{_fmt(acc.get('totalMaintMargin', 0))} USDT")
    except Exception:                                       # noqa: BLE001
        pass
    if bal["wallet"] <= 0:
        problems.append("USDT 钱包余额为 0，无法开仓。请先划转到 U 本位合约账户。")

    # ---------- 持仓 ----------
    say(_hr("3. 当前持仓"))
    pos = api.position(symbol)
    if pos is None:
        say(BAD + f"查不到 {symbol} 持仓信息。")
        _dump(out)
        return 2
    cur_qty = float(pos["positionAmt"])
    say(f"  方向        : {pos['side']}")
    say(f"  数量        : {cur_qty:+.4f} BTC")
    say(f"  开仓均价    : {_fmt(pos['entryPrice'])}")
    say(f"  标记价      : {_fmt(pos['markPrice'])}")
    say(f"  未实现盈亏  : {_fmt(pos['unRealizedProfit'])} USDT")
    say(f"  强平价      : {_fmt(pos['liquidationPrice'])}")
    say(f"  逐仓保证金  : {_fmt(pos['isolatedMargin'])} USDT")
    say(f"  持仓杠杆    : {_fmt(pos['leverage'], 0)}×")

    # ---------- 杠杆 / 保证金模式核对 ----------
    say(_hr("4. 杠杆与保证金模式核对"))
    ex_L = float(pos["leverage"] or 0)
    say(f"  交易所杠杆 {_fmt(ex_L, 0)}×  vs  config.trade.leverage {L_trade}×"
        + ("   " + OK + "一致" if abs(ex_L - L_trade) < 1e-6
           else "   " + WARN + "不一致（开仓时脚本会自动设置为 "
                f"{L_trade}×，但当前持仓仍是 {ex_L:g}×）"))
    if cur_qty and abs(ex_L - L_trade) >= 1e-6:
        notices.append(f"当前有持仓({cur_qty:+.4f}) 且交易所杠杆 {ex_L:g}× ≠ 目标 {L_trade}×。"
                       f"改动杠杆会改变已持仓的强平价，请谨慎。")
    # 保证金模式（从持仓接口的 positionSide / isolate 字段间接判断；用 account 更稳）
    try:
        funds = api._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        mt = None
        for p in funds:
            if p["symbol"] == symbol:
                mt = "ISOLATED" if p.get("isolated") else "CROSSED"
        if mt:
            say(f"  交易所保证金模式 {mt} vs 目标 {margin_type}   "
                + (OK + "一致" if mt == margin_type
                   else WARN + "不一致（开仓时脚本会自动切换；切换前需确保无持仓/挂单）"))
            if mt != margin_type:
                notices.append(f"保证金模式为 {mt}，目标 {margin_type}。"
                               f"币安在『有持仓或有挂单』时拒绝切换保证金模式。")
    except Exception as e:                                  # noqa: BLE001
        say(WARN + f"保证金模式读取失败：{e}")

    # 挂单（本策略应为 0，有挂单说明有人工单）
    try:
        oo = api.open_orders(symbol)
        say(f"  当前挂单数  : {len(oo)}" + ("（正常，本策略不挂单）" if not oo
                                          else "  ← 注意：存在挂单，请确认是否人工单"))
        if oo:
            notices.append(f"{symbol} 存在 {len(oo)} 笔挂单。本策略只用市价单，"
                           f"挂单可能来自人工操作，反手/切换保证金模式前需先处理。")
    except Exception as e:                                  # noqa: BLE001
        say(WARN + f"挂单查询失败：{e}")

    # ---------- 精度 ----------
    say(_hr("5. 交易精度参数"))
    try:
        f = api.symbol_filters(symbol)
        say(f"  数量步长 qty_step      : {f['qty_step']}")
        say(f"  最小数量 min_qty       : {f['min_qty']}")
        say(f"  最小名义 min_notional  : {f['min_notional']} USDT")
        say(f"  数量精度 / 价格精度    : {f['qty_precision']} / {f['price_precision']}")
        # 与 config 里手填的比较
        cfg_step = float(tr.get("qty_step", 0) or 0)
        if cfg_step and abs(cfg_step - f["qty_step"]) > 1e-12:
            notices.append(f"config.trade.qty_step={cfg_step} 与交易所实际 {f['qty_step']} 不同；"
                           f"脚本下单时以交易所实际值为准（自动获取）。")
    except Exception as e:                                  # noqa: BLE001
        f = None
        say(WARN + f"精度参数读取失败：{e}")

    # ---------- 目标 vs 实际 ----------
    say(_hr("6. 目标仓位 vs 实际持仓"))
    if quick:
        say("  --quick 模式，跳过信号计算。")
    else:
        try:
            bars = C.keep_closed(C.fetch_klines(symbol, cfg["interval"],
                                                cfg.get("kline_limit", 1000),
                                                int(cfg["email"].get("max_retries", 3)),
                                                C.setup_logger("sync_check")))
            if len(bars) < 300:
                say(WARN + f"K线只有 {len(bars)} 根，不足 300，无法可靠计算信号。")
            else:
                inf = C.compute_profile(cfg["profiles"][profile_key], bars, cfg)
                inf["key"] = profile_key
                bar_dt = inf["bar_dt"].strftime("%Y-%m-%d")
                say(f"  信号K线日期 : {bar_dt}（已收盘）")
                say(f"  当前价格     : {_fmt(inf['px'])}")
                say(f"  日内波动率   : {inf['dvol'] * 100:.2f}%")
                say(f"  信号方向     : {inf['side']}   净敞口 {inf['net_expo']:.3f}×")
                say(f"  五组参数方向 : {inf['dirs']}  "
                    f"(8/24, 10/30, 12/36, 15/45, 20/60)")
                say(f"  信号下的名义 : {_fmt(inf['net'], 0)} USDT（按配置 equity "
                    f"{_fmt(inf['equity'], 0)} 计）")

                eq_ref = bal["wallet"] if bal["wallet"] > 0 else inf["equity"]
                tgt_qty, tgt_notional = T.target_position(
                    inf, equity_override=eq_ref,
                    notional_cap=tr.get("max_order_notional"))
                say(f"  按真实余额 {_fmt(eq_ref)} 折算目标持仓：")
                say(f"      目标数量 : {tgt_qty:+.4f} BTC")
                say(f"      目标名义 : {_fmt(tgt_notional, 0)} USDT")
                delta = tgt_qty - cur_qty
                say(f"      差异     : {delta:+.4f} BTC（名义 {_fmt(delta * inf['px'], 0)} USDT）")

                action = T._decide(cur_qty, tgt_qty)
                say("")
                say(f"  【若不 dry_run，脚本将执行】→ {action['label']}：{action['desc']}")

                # 阈值判断（与 trade.py 一致）
                if f:
                    aligned = api.align_qty(abs(delta), symbol)
                    thr = float(tr.get("rebalance_threshold", 0.05))
                    rel = abs(delta) / max(abs(tgt_qty), 1e-9) if tgt_qty else \
                        (1.0 if delta else 0.0)
                    if aligned < f["min_qty"] or rel < thr:
                        say(f"  → 但差异经阈值判断为『无需调仓』"
                            f"（对齐后 {aligned} < {f['min_qty']}，或相对偏差 "
                            f"{rel * 100:.1f}% < {thr * 100:.1f}%）")
                    else:
                        # 反手/开仓的名义是否超出上限
                        notional_need = abs(delta) * inf["px"]
                        cap = float(tr.get("max_order_notional", 0) or 0)
                        if cap and notional_need > cap:
                            problems.append(f"需要下单的名义 {notional_need:,.0f} U 超过 "
                                            f"max_order_notional={cap:,.0f} U，真实下单会被拒绝。")
        except Exception as e:                              # noqa: BLE001
            say(WARN + f"信号计算失败：{e}")
            notices.append(f"信号计算失败：{e}")

    # ---------- 幂等文件状态 ----------
    say(_hr("7. 幂等状态文件"))
    it = T.load_intent()
    if not it:
        say(f"  {os.path.basename(T.INTENT_PATH)} 不存在（尚未下过单，正常）。")
    else:
        done = it.get("done")
        say(f"  最近意图 key : {it.get('key')}")
        say(f"  是否完成     : {done}   时间 {it.get('at')}")
        if not done:
            problems.append("存在『未完成』的下单意图（可能上次下单中断）。"
                            "上线前请人工核对持仓，确认无误后再 --force 继续。")

    # ---------- 风控提示 ----------
    say(_hr("8. 风控提示（来自 config.risk，仅提醒）"))
    risk = cfg.get("risk", {}) or {}
    say(f"  止损模式   : {risk.get('stop_mode', 'none')}"
        + (f"  吊灯 {risk.get('trail_atr')}×ATR"
           if risk.get("stop_mode") == "trail_atr" else ""))
    say(f"  爆仓提醒距 : {float(risk.get('liq_warn_pct', 0.10)) * 100:.0f}%")
    say(f"  每日最多提醒: {risk.get('max_alerts_per_day', 3)} 次")
    say("  ※ 这些只是『提醒』，脚本不会自动挂止损单。真实止损需你在币安 App 手动设置，")
    say("    或改由 trade.py 按信号反手（本策略实测 9 年 124 笔 100% 靠信号平仓）。")

    # ---------- 结论 ----------
    say(_hr("结论"))
    if problems:
        say(BAD + f"发现 {len(problems)} 个【必须解决】的问题：")
        for i, p in enumerate(problems, 1):
            say(f"     {i}. {p}")
    else:
        say(OK + "未发现阻止上线的问题。")
    if notices:
        say("")
        say(WARN + f"{len(notices)} 条提示（不阻止上线，但请知晓）：")
        for i, n in enumerate(notices, 1):
            say(f"     {i}. {n}")

    say("")
    say("上线步骤建议：")
    say("  1) 在币安把『合约交易』API 权限打开，并把本机出口 IP 加入白名单")
    say("  2) 反复运行  python sync_check.py  直到无 [!!] 问题")
    say("  3) python trade.py --dry-run   预演一次，检查邮件里的动作是否符合预期")
    say("  4) 确认无误后：config.json 里 trade.enabled=true 且 trade.dry_run=false")
    say("  5) python trade.py            首次真实执行（建议先小额）")
    say("")

    _dump(out)
    return 1 if problems else 0


def _dump(lines):
    """把本次自检结果落盘，便于留档。"""
    try:
        path = os.path.join(HERE, "logs", "sync_check.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n\n########## {datetime.now(C.CST):%Y-%m-%d %H:%M:%S} ##########\n")
            fh.write("\n".join(lines))
    except Exception:                                       # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
