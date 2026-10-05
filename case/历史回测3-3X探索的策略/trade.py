# -*- coding: utf-8 -*-
"""
trade.py — 自动下单入口（对接币安 U 本位合约）

流程：
  1. 拉已收盘K线 → 算信号 → 算目标仓位（与回测同一套公式）
  2. 查交易所实际持仓、余额、杠杆
  3. 目标 vs 实际比对 → 决定动作（开仓 / 平仓 / 反手 / 不动）
  4. 风控校验（数量、名义、幂等）→ 下单
  5. 发邮件通知（含成交明细）

【幂等保护】同一「目标方向 + 交易日」只下单一次。
意图文件在下单前写入，成功后写完成标记；崩溃重启不会重复下单。

用法：
  python trade.py --check      # 只读对账，不下单（推荐首次）
  python trade.py --dry-run    # 打印将执行的下单，不提交
  python trade.py              # 真实下单（需 config.trade.enabled=true）
  python trade.py --force      # 忽略幂等，强制执行一次
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common as C                                          # noqa: E402
from binance import BinanceFutures, BinanceError            # noqa: E402

INTENT_PATH = os.path.join(HERE, "trade_intent.json")


# ============================================================ 幂等
def load_intent():
    if not os.path.exists(INTENT_PATH):
        return {}
    try:
        with open(INTENT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                       # noqa: BLE001
        return {}


def save_intent(d):
    tmp = INTENT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INTENT_PATH)


def intent_key(symbol, bar_dt, target_side):
    """幂等键：交易对 + K线日期 + 目标方向。"""
    return f"{symbol}|{bar_dt}|{target_side}"


# ============================================================ 目标仓位
def target_position(inf, equity_override=None, notional_cap=None):
    """
    由信号信息算出目标持仓（有符号数量）。
    inf: compute_profile 的返回
    返回 (target_qty_signed, target_notional_signed, 说明)
    """
    px = inf["px"]
    net = inf["net"]                       # 有符号名义（基于配置里的 equity）
    eq_cfg = float(inf["equity"])

    if equity_override:
        # 用交易所真实余额等比缩放，避免配置与实际账户不一致
        scale = float(equity_override) / eq_cfg if eq_cfg else 1.0
        net = net * scale

    if notional_cap:
        net = max(-float(notional_cap), min(float(notional_cap), net))

    qty = net / px
    return qty, net


# ============================================================ 主流程
def run(argv, log=None):
    log = log or C.setup_logger("trade")
    cfg = C.load_config()
    tr = cfg.get("trade") or {}

    check_only = "--check" in argv
    # --dry-run 命令行参数可强制预演；否则读配置
    dry = ("--dry-run" in argv) or bool(tr.get("dry_run", True))
    force = "--force" in argv

    # ---------- 总开关（config.json 顶部 _master_switch）----------
    ms = C.master_switch(cfg)
    enabled = ms["real_ok"]
    real = (not check_only) and (not dry)          # 本次是否真要下单
    log.info("总开关：mode=%s  real_ok=%s  —— %s", ms["mode"], ms["real_ok"], ms["reason"])
    if not ms["live"]:
        log.warning("当前为【观察模式】：本次只算目标仓位，不会提交任何订单。")
        log.warning("想开始实盘：把 config.json 顶部 _master_switch.mode 改成 \"live\"，"
                    "并把 i_understand_real_money 改成 true。")
    if real and not enabled:
        log.error("拒绝真实下单：%s", ms["reason"])
        log.error("请检查 config.json 顶部 _master_switch 的两个字段"
                  "（mode 必须为 \"live\"，i_understand_real_money 必须为 true）。")
        return 2

    symbol = cfg["symbol"]
    profile_key = tr.get("profile", "B")
    if profile_key not in cfg["profiles"]:
        log.error("config.trade.profile = %s 不存在", profile_key)
        return 2
    profile = cfg["profiles"][profile_key]
    L = int(tr.get("leverage", cfg.get("leverage", 3)))

    log.info("=" * 62)
    log.info("自动交易启动  profile=%s(%s)  杠杆=%d×  总开关=%s  真正下单=%s  check=%s",
             profile_key, profile["name"], L, ms["mode"],
             ("是" if real else "否"), check_only)

    # ---------- ① 信号
    bars = C.keep_closed(C.fetch_klines(symbol, cfg["interval"],
                                        cfg.get("kline_limit", 1000),
                                        int(cfg["email"].get("max_retries", 3)), log))
    if len(bars) < 300:
        log.error("K线不足 %d 根，无法计算 EMA60", len(bars))
        return 2
    inf = C.compute_profile(profile, bars, cfg)
    inf["key"] = profile_key
    bar_dt = inf["bar_dt"].strftime("%Y-%m-%d")
    log.info("信号：方向=%s 净敞口=%.3f 参数明细=%s 价格=%.2f 日波动=%.2f%%",
             inf["side"], inf["net_expo"], inf["dirs"], inf["px"], inf["dvol"] * 100)

    # ---------- ② 交易所状态
    if (not tr.get("apiKey")) or ("在这里填" in str(tr.get("apiKey"))) \
            or (not tr.get("apiSecret")):
        log.error("-" * 62)
        log.error("尚未配置币安 API 密钥，无法连接交易所。")
        log.error("请编辑 config.json 的 trade.apiKey / trade.apiSecret，")
        log.error("或设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET。")
        log.error("配置后可先跑：python sync_check.py  做只读对账。")
        log.error("-" * 62)
        log.info("（本次已成功算出信号：方向=%s 净敞口=%.3f 价格=%.2f 日波动=%.2f%%）",
                 inf["side"], inf["net_expo"], inf["px"], inf["dvol"] * 100)
        return 2

    api = BinanceFutures(tr.get("apiKey"), tr.get("apiSecret"),
                         int(tr.get("recvWindow", 5000)), log)
    api.sync_time()

    dual = api.dual_side_position()
    if dual:
        log.error("-" * 62)
        log.error("账户当前是【双向持仓】模式，本策略只支持【单向持仓】。")
        log.error("双向持仓下同一交易对会返回多空两条持仓记录，下单也会因为缺少")
        log.error("positionSide 而失败。请先改成单向持仓再跑。")
        log.error("币安 App：合约 -> 右上角设置 -> 持仓模式 -> 单向持仓")
        log.error("-" * 62)
        return 2
    if dual is None:
        log.warning("未能确认持仓模式（接口异常），继续执行；"
                    "若下单报错请检查账户是否为单向持仓。")

    pos = api.position(symbol)
    bal = api.usdt_balance()
    if pos is None:
        log.error("查不到 %s 持仓信息", symbol)
        return 2
    log.info("交易所持仓：%s  数量=%+.3f  entry=%.2f  未实现盈亏=%+.2f  杠杆=%s×",
             pos["side"], pos["positionAmt"], pos["entryPrice"],
             pos["unRealizedProfit"], pos["leverage"])
    log.info("账户：钱包余额 %.2f U，可用 %.2f U", bal["wallet"], bal["available"])

    # ---------- ③ 算目标与差异
    # 权益口径必须与回测一致：回测里 eq = 已实现余额 + 该仓位浮动盈亏。
    # 只用 walletBalance 会在浮亏时把仓位算得偏大（比回测更激进）。
    wallet = bal["wallet"]
    upnl = float(pos.get("unRealizedProfit") or 0)
    eq_mtm = wallet + upnl
    if eq_mtm > 0:
        eq_ref = eq_mtm
    elif wallet > 0:
        eq_ref = wallet
    else:
        eq_ref = inf["equity"]
    log.info("权益口径：钱包 %.2f U %+.2f（浮动）= %.2f U（回测同口径）",
             wallet, upnl, eq_ref)
    tgt_qty, tgt_notional = target_position(inf, equity_override=eq_ref,
                                            notional_cap=tr.get("max_order_notional"))
    cur_qty = float(pos["positionAmt"])
    delta = tgt_qty - cur_qty
    px = inf["px"]
    delta_notional = delta * px

    side_txt = "多" if tgt_qty > 0 else ("空" if tgt_qty < 0 else "空仓")
    log.info("目标：%s  数量=%+.3f  名义=%+.0f U", side_txt, tgt_qty, tgt_notional)
    log.info("差异：%+.3f BTC  名义 %+.0f U", delta, delta_notional)

    f = api.symbol_filters(symbol)
    aligned = api.align_qty(abs(delta), symbol)
    thr = float(tr.get("rebalance_threshold", 0.05))
    rel = abs(delta) / max(abs(tgt_qty), 1e-9) if tgt_qty else (1.0 if delta else 0.0)

    log.info("精度：步长 %s  minQty %s  minNotional %s U",
             f["qty_step"], f["min_qty"], f["min_notional"])

    if aligned < f["min_qty"] or rel < thr:
        log.info("差异过小（对齐后 %s < %s，或相对偏差 %.1f%% < %.1f%%），无需调仓",
                 aligned, f["min_qty"], rel * 100, thr * 100)
        if not check_only:
            C.send_email(cfg["email"], f"【BTC交易{C.SUBJECT_SUFFIX}】无需调仓 · {bar_dt}",
                         _noop_body(inf, pos, tgt_qty, eq_ref, L), log)
        return 0

    action = _decide(cur_qty, tgt_qty)
    log.info("判定动作：%s", action["desc"])

    if check_only:
        log.info("[check] 只读模式，不执行下单。若要预演请加 --dry-run。")
        print("\n" + _plan_body(inf, pos, tgt_qty, eq_ref, L, action, f, api))
        return 0

    # ---------- ④ 幂等
    key = intent_key(symbol, bar_dt, side_txt)
    it = load_intent()
    if it.get("key") == key and it.get("done") and not force:
        log.info("幂等命中：%s 已执行过（%s），跳过", key, it.get("at"))
        return 0
    if it.get("key") == key and not it.get("done") and not force:
        log.warning("发现未完成的意图（可能上次下单中断）：%s。"
                    "请先跑 --check 人工核对持仓，确认无误后用 --force 继续。", key)
        return 3

    if tr.get("set_leverage_on_start", True) and not dry:
        api.set_leverage(symbol, L, tr.get("margin_type", "ISOLATED"))

    # 写入意图（下单前）
    if not dry:
        save_intent({"key": key, "done": False, "at": datetime.now(C.CST).isoformat(),
                     "target_qty": tgt_qty, "action": action["kind"]})

    # ---------- ⑤ 执行
    try:
        fills = _execute(api, symbol, cur_qty, tgt_qty, action, tr, dry, log)
    except Exception as e:                                  # noqa: BLE001
        log.error("下单失败：%s", e, exc_info=True)
        C.send_email(cfg["email"], f"【BTC交易{C.SUBJECT_SUFFIX}】❌ 下单失败 · {bar_dt}",
                     _err_body(inf, e, action, tgt_qty, cur_qty, eq_ref, L), log)
        return 1

    if not dry:
        save_intent({"key": key, "done": True, "at": datetime.now(C.CST).isoformat(),
                     "target_qty": tgt_qty, "action": action["kind"],
                     "fills": [x.get("desc", "") for x in fills]})

    # ---------- ⑥ 通知
    subject = f"【BTC交易{C.SUBJECT_SUFFIX}】{'[预演] ' if dry else ''}{action['label']} · {bar_dt}"
    body = _done_body(inf, pos, tgt_qty, eq_ref, L, action, fills, f, dry, api)
    if dry:
        log.info("[DRY-RUN] 不发送邮件，正文：\n%s", body)
    else:
        C.send_email(cfg["email"], subject, body, log)
        log.info("邮件已发送：%s", subject)
    return 0


def _decide(cur, tgt):
    """判断该做什么动作。"""
    if abs(cur) < 1e-9 and abs(tgt) < 1e-9:
        return {"kind": "none", "label": "无需操作", "desc": "目标与当前均为空仓"}
    if abs(tgt) < 1e-9:
        return {"kind": "close", "label": "平仓",
                "desc": f"目标空仓，平掉当前 {abs(cur):.3f} BTC"}
    if abs(cur) < 1e-9:
        return {"kind": "open", "label": "开仓",
                "desc": f"从空仓开到 {abs(tgt):.3f} BTC"}
    if (cur > 0) != (tgt > 0):
        return {"kind": "flip", "label": "反手",
                "desc": f"方向反转：平 {abs(cur):.3f} 后开 {abs(tgt):.3f}"}
    return {"kind": "adjust", "label": "调仓",
            "desc": f"同向调整：{abs(cur):.3f} → {abs(tgt):.3f}"}


def _execute(api, symbol, cur, tgt, action, tr, dry, log):
    """执行下单。反手时先平后开（reduce_first）。"""
    fills = []
    kind = action["kind"]
    if kind == "none":
        return fills

    if kind == "close":
        side = "SELL" if cur > 0 else "BUY"
        r = api.market_order(symbol, side, abs(cur), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"平仓 {side} {abs(cur):.3f}", "resp": r})
        return fills

    if kind == "open":
        side = "BUY" if tgt > 0 else "SELL"
        r = api.market_order(symbol, side, abs(tgt), dry_run=dry)
        fills.append({"desc": f"开仓 {side} {abs(tgt):.3f}", "resp": r})
        return fills

    if kind == "flip":
        if not tr.get("allow_flip", True):
            log.warning("config.trade.allow_flip=false，只平不反手")
            side = "SELL" if cur > 0 else "BUY"
            r = api.market_order(symbol, side, abs(cur), reduce_only=True, dry_run=dry)
            fills.append({"desc": f"平仓（不反手）{side} {abs(cur):.3f}", "resp": r})
            return fills
        # 先平（reduceOnly 保证不会反向开仓）
        side_close = "SELL" if cur > 0 else "BUY"
        r1 = api.market_order(symbol, side_close, abs(cur), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"平旧仓 {side_close} {abs(cur):.3f}", "resp": r1})
        # 再开新方向
        side_open = "BUY" if tgt > 0 else "SELL"
        r2 = api.market_order(symbol, side_open, abs(tgt), dry_run=dry)
        fills.append({"desc": f"开新仓 {side_open} {abs(tgt):.3f}", "resp": r2})
        return fills

    # adjust：同向增减，用差额下单
    delta = abs(tgt) - abs(cur)
    if abs(delta) < 1e-12:
        return fills
    if delta > 0:
        side = "BUY" if tgt > 0 else "SELL"
        r = api.market_order(symbol, side, delta, dry_run=dry)
        fills.append({"desc": f"加仓 {side} {delta:.3f}", "resp": r})
    else:
        side = "SELL" if tgt > 0 else "BUY"
        r = api.market_order(symbol, side, abs(delta), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"减仓 {side} {abs(delta):.3f}", "resp": r})
    return fills


# ============================================================ 邮件正文
def _common_head(inf, pos, tgt_qty, eq_ref, L):
    return (
        f"数据K线：{inf['bar_dt']:%Y-%m-%d}（已收盘）\n"
        f"价格：{inf['px']:,.2f} USDT   日波动率：{inf['dvol'] * 100:.2f}%\n"
        f"信号方向：{inf['side']}   净敞口：{inf['net_expo']:.3f}×   参数：{inf['dirs']}\n"
        f"账户钱包余额：{eq_ref:,.2f} U   杠杆：{L}× 逐仓\n"
        f"当前持仓：{pos['side']} {pos['positionAmt']:+.4f} BTC"
        + (f"  开仓价 {pos['entryPrice']:,.2f}" if pos["positionAmt"] else "")
        + "\n"
        f"目标持仓：{'多' if tgt_qty > 0 else ('空' if tgt_qty < 0 else '空仓')} "
        f"{tgt_qty:+.4f} BTC\n"
    )


def _noop_body(inf, pos, tgt_qty, eq_ref, L):
    return (_common_head(inf, pos, tgt_qty, eq_ref, L)
            + "\n本次差异小于调仓阈值，未做任何操作。\n"
            + "\n" + _footer())


def _plan_body(inf, pos, tgt_qty, eq_ref, L, action, f, api):
    return (_common_head(inf, pos, tgt_qty, eq_ref, L)
            + f"\n【只读取证】判定动作：{action['label']} —— {action['desc']}\n"
            + f"数量步长 {f['qty_step']}，最小数量 {f['min_qty']}，"
              f"最小名义 {f['min_notional']} U\n"
            + "\n" + _footer())


def _done_body(inf, pos, tgt_qty, eq_ref, L, action, fills, f, dry, api):
    s = _common_head(inf, pos, tgt_qty, eq_ref, L)
    s += f"\n动作：{action['label']} —— {action['desc']}\n"
    if dry:
        s += "\n[DRY-RUN 预演，未真实提交] 计划成交：\n"
    else:
        s += "\n实际成交：\n"
    for x in fills:
        s += f"  · {x.get('desc', '')}\n"
        r = x.get("resp") or {}
        if r.get("avgPrice"):
            s += f"      成交价 {r['avgPrice']}  数量 {r.get('executedQty')}  " \
                 f"状态 {r.get('status')}\n"
    s += "\n" + _footer()
    return s


def _err_body(inf, e, action, tgt_qty, cur, eq_ref, L):
    return (_common_head(inf, {"side": "?", "positionAmt": cur, "entryPrice": 0},
                         tgt_qty, eq_ref, L)
            + f"\n动作：{action['label']} —— {action['desc']}\n"
            + f"\n❌ 下单失败：{e}\n\n请立即登录币安人工核对持仓！\n"
            + "\n" + _footer())


def _footer():
    return ("风险提示：本邮件由量化脚本自动生成，仅为研究参考，不构成投资建议。\n"
            f"生成时间：{datetime.now(C.CST):%Y-%m-%d %H:%M:%S}（北京时间）")


# ============================================================ 入口
def main(argv):
    try:
        return run(argv)
    except BinanceError as e:
        log = C.setup_logger("trade")
        log.error("币安接口错误：[%s] %s", e.code, e.msg)
        if e.code in (-2015, -2014):
            log.error("排查：API Key 是否开通『合约』权限？服务器出口 IP 是否已加入白名单？")
        elif e.code == -2019:
            log.error("排查：账户可用保证金不足，请降低下单名义或追加资金。")
        elif e.code == -1021:
            log.error("排查：本地时间与服务器偏差过大，请开启系统自动同步时间。")
        log.error("本次未完成下单，请人工登录币安核对持仓！")
        return 1
    except ValueError as e:
        C.setup_logger("trade").error("配置错误：%s", e)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:                                  # noqa: BLE001
        C.setup_logger("trade").error("运行失败：%s", repr(e))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
