# -*- coding: utf-8 -*-
"""
trade.py — 自动下单入口（对接币安 U 本位合约）

【两种运行模式，由 config.json 决定】
  run_mode = "single"  → 只跑 config.symbol（默认 BTCUSDT），全部本金押它
  run_mode = "multi"   → 跑 config.symbols 全部（默认四币等权：BTC/ETH/BNB/SOL）
                         资金 = 账户总权益 ÷ 币数（动态再平衡）

命令行可临时覆盖（不改配置文件）：
  --single                 强制只跑 config.symbol
  --multi                  强制跑 config.symbols 全部
  --symbols BTCUSDT,SOLUSDT  指定一组交易对

流程：
  1. 拉已收盘K线 → 算信号 → 算每个币的目标仓位（与回测同一套公式）
  2. 查交易所实际持仓、账户总权益、杠杆
  3. 目标 vs 实际比对 → 决定动作（开仓 / 平仓 / 反手 / 不动）
  4. 风控校验（数量、名义、幂等）→ 下单（逐币独立）
  5. 发一封合并邮件（同时列出「四币等权」和「BTC 单跑参照」两个视角）

【幂等保护】同一「交易对 + 目标方向 + 交易日」只下单一次。
意图文件在下单前写入，成功后写完成标记；崩溃重启不会重复下单。
每个交易对独立幂等，互不影响。

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
    """意图文件：{ "BTCUSDT|2026-10-05|多": {...}, ... }

    兼容旧版单条格式 {"key": ..., "done": ...}，会自动转成新格式。
    """
    if not os.path.exists(INTENT_PATH):
        return {}
    try:
        with open(INTENT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:                                       # noqa: BLE001
        return {}
    if not isinstance(d, dict):
        return {}
    if "key" in d:                                          # 旧格式
        k = d.get("key")
        return {k: d} if k else {}
    return d


def save_intent(d):
    tmp = INTENT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INTENT_PATH)


def prune_intent(d, keep=120):
    """只保留最近 keep 条，避免文件无限增长。"""
    if len(d) <= keep:
        return d
    items = sorted(d.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True)
    return dict(items[:keep])


def intent_key(symbol, bar_dt, target_side):
    """幂等键：交易对 + K线日期 + 目标方向。"""
    return f"{symbol}|{bar_dt}|{target_side}"


# ============================================================ 目标仓位
def _notional_cap(tr, equity):
    """名义上限（有符号，取绝对值）。

    config.trade.max_order_notional：
      > 0  → 用这个绝对值（老行为，适合固定本金）
      = 0  → 自动跟随：配额 × 3.15
             （3× 杠杆 + 5 组参数全同向时理论最大是 3× 配额，
              留一点余量只用来挡住异常大单，不干预正常调仓）
    """
    v = float(tr.get("max_order_notional") or 0)
    return v if v > 0 else float(equity) * 3.15


def target_position(inf, equity_override=None, notional_cap=None):
    """
    由信号信息算出目标持仓（有符号数量）。
    inf: compute_profile 的返回
    返回 (target_qty_signed, target_notional_signed)
    """
    px = inf["px"]
    net = inf["net"]                       # 有符号名义（基于 inf 里的 equity）
    eq_cfg = float(inf["equity"])

    if equity_override:
        # 用交易所真实权益等比缩放，避免配置与实际账户不一致
        scale = float(equity_override) / eq_cfg if eq_cfg else 1.0
        net = net * scale

    if notional_cap:
        net = max(-float(notional_cap), min(float(notional_cap), net))

    qty = net / px
    return qty, net


def _side_name(q):
    return "多" if q > 1e-9 else ("空" if q < -1e-9 else "空仓")


# ============================================================ 单个交易对：算目标与差异
def _process_symbol(api, cfg, tr, sym, inf, pos, quota, L, log):
    """算出一个交易对的目标仓位与差异，判断是否需要调仓。

    不执行下单，只产出结果字典（供 check / 执行 / 邮件共用）。
    """
    coin = C.coin_name(sym)
    cur = float(pos["positionAmt"]) if pos else 0.0
    px = inf["px"]
    cap = _notional_cap(tr, quota)
    tgt_qty, tgt_notional = target_position(inf, equity_override=quota, notional_cap=cap)
    delta = tgt_qty - cur
    f = api.symbol_filters(sym)
    aligned = api.align_qty(abs(delta), sym)
    thr = float(tr.get("rebalance_threshold", 0.05))
    if abs(tgt_qty) > 1e-9:
        rel = abs(delta) / abs(tgt_qty)
    else:
        rel = 1.0 if abs(delta) > 1e-9 else 0.0

    r = {
        "symbol": sym, "coin": coin, "inf": inf, "pos": pos,
        "cur": cur, "tgt_qty": tgt_qty, "tgt_notional": tgt_notional,
        "delta": delta, "aligned": aligned, "rel": rel, "f": f,
        "quota": quota, "cap": cap,
        "side_txt": _side_name(tgt_qty),
        "margin": abs(tgt_notional) / L if L else 0.0,
        "action": None, "status": "noop", "note": "", "fills": [], "error": None,
    }

    if abs(cur) < 1e-12 and abs(tgt_qty) < 1e-12:
        r["note"] = "当前与目标均为空仓"
        return r

    if aligned < f["min_qty"] or rel < thr:
        r["note"] = (f"差异过小（对齐后 {aligned:g} < {f['min_qty']:g}，"
                     f"或相对偏差 {rel * 100:.1f}% < {thr * 100:.1f}%）")
        return r

    # 名义门槛预检：只挡「加仓/开仓」，减仓（reduceOnly）交给交易所判定，
    # 避免本地误拦掉本该能平的仓。
    reduces = abs(tgt_qty) < abs(cur) and (tgt_qty * cur >= 0)
    if (not reduces) and aligned * px < f["min_notional"]:
        r["status"] = "too_small"
        r["note"] = (f"本次名义 {aligned * px:,.1f} U < 交易所最小名义 "
                     f"{f['min_notional']:,.0f} U，会被拒单")
        return r

    r["action"] = _decide(cur, tgt_qty)
    r["status"] = "planned"
    return r


def _decide(cur, tgt):
    """判断该做什么动作。"""
    if abs(cur) < 1e-9 and abs(tgt) < 1e-9:
        return {"kind": "none", "label": "无需操作", "desc": "目标与当前均为空仓"}
    if abs(tgt) < 1e-9:
        return {"kind": "close", "label": "平仓",
                "desc": f"目标空仓，平掉当前 {abs(cur):.6f}"}
    if abs(cur) < 1e-9:
        return {"kind": "open", "label": "开仓",
                "desc": f"从空仓开到 {abs(tgt):.6f}"}
    if (cur > 0) != (tgt > 0):
        return {"kind": "flip", "label": "反手",
                "desc": f"方向反转：平 {abs(cur):.6f} 后开 {abs(tgt):.6f}"}
    return {"kind": "adjust", "label": "调仓",
            "desc": f"同向调整：{abs(cur):.6f} → {abs(tgt):.6f}"}


def _execute(api, symbol, cur, tgt, action, tr, dry, log):
    """执行下单。反手时先平后开（reduce_first）。"""
    fills = []
    kind = action["kind"]
    if kind == "none":
        return fills

    if kind == "close":
        side = "SELL" if cur > 0 else "BUY"
        r = api.market_order(symbol, side, abs(cur), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"平仓 {side} {abs(cur):.6f}", "resp": r})
        return fills

    if kind == "open":
        side = "BUY" if tgt > 0 else "SELL"
        r = api.market_order(symbol, side, abs(tgt), dry_run=dry)
        fills.append({"desc": f"开仓 {side} {abs(tgt):.6f}", "resp": r})
        return fills

    if kind == "flip":
        if not tr.get("allow_flip", True):
            log.warning("config.trade.allow_flip=false，只平不反手")
            side = "SELL" if cur > 0 else "BUY"
            r = api.market_order(symbol, side, abs(cur), reduce_only=True, dry_run=dry)
            fills.append({"desc": f"平仓（不反手）{side} {abs(cur):.6f}", "resp": r})
            return fills
        # 先平（reduceOnly 保证不会反向开仓）
        side_close = "SELL" if cur > 0 else "BUY"
        r1 = api.market_order(symbol, side_close, abs(cur), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"平旧仓 {side_close} {abs(cur):.6f}", "resp": r1})
        # 再开新方向
        side_open = "BUY" if tgt > 0 else "SELL"
        r2 = api.market_order(symbol, side_open, abs(tgt), dry_run=dry)
        fills.append({"desc": f"开新仓 {side_open} {abs(tgt):.6f}", "resp": r2})
        return fills

    # adjust：同向增减，用差额下单
    delta = abs(tgt) - abs(cur)
    if abs(delta) < 1e-12:
        return fills
    if delta > 0:
        side = "BUY" if tgt > 0 else "SELL"
        r = api.market_order(symbol, side, delta, dry_run=dry)
        fills.append({"desc": f"加仓 {side} {delta:.6f}", "resp": r})
    else:
        side = "SELL" if tgt > 0 else "BUY"
        r = api.market_order(symbol, side, abs(delta), reduce_only=True, dry_run=dry)
        fills.append({"desc": f"减仓 {side} {abs(delta):.6f}", "resp": r})
    return fills


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

    # ---------- 交易对：仅 BTC / 四币合一 ----------
    syms = C.symbols_of(cfg, override=C._symbols_from_argv(argv))
    multi = len(syms) >= 2
    n = len(syms)
    head = "/".join(C.coin_name(s) for s in syms)

    profile_key = tr.get("profile", "B")
    if profile_key not in cfg["profiles"]:
        log.error("config.trade.profile = %s 不存在", profile_key)
        return 2
    profile = cfg["profiles"][profile_key]
    L = int(tr.get("leverage", cfg.get("leverage", 3)))

    log.info("=" * 62)
    log.info("自动交易启动  模式=%s（%d 个交易对：%s）  profile=%s(%s)  杠杆=%d×  "
             "总开关=%s  真正下单=%s  check=%s",
             ("四币等权" if multi else "仅单标的"), n, ",".join(syms),
             profile_key, profile["name"], L, ms["mode"],
             ("是" if real else "否"), check_only)
    if multi:
        log.info("资金分配：账户总权益 ÷ %d（动态再平衡）—— 账户涨了每币配额自动等比放大", n)

    # ---------- ① 拉各交易对K线 ----------
    bars_map = {}
    for s in syms:
        b = C.keep_closed(C.fetch_klines(s, cfg["interval"], cfg.get("kline_limit", 1000),
                                         int(cfg["email"].get("max_retries", 3)), log))
        if len(b) < 300:
            log.error("%s 已收盘K线只有 %d 根，不足以计算 EMA60", s, len(b))
            return 2
        bars_map[s] = b
        log.info("%s 数据就绪：%d 根，最新已收盘 %s", s, len(b),
                 datetime.fromtimestamp(b[-1]["open_time"] / 1000, C.CST).date())

    # ---------- ② 交易所状态 ----------
    if (not tr.get("apiKey")) or ("在这里填" in str(tr.get("apiKey"))) \
            or (not tr.get("apiSecret")):
        log.error("-" * 62)
        log.error("尚未配置币安 API 密钥，无法连接交易所。")
        log.error("请编辑 config.json 的 trade.apiKey / trade.apiSecret，")
        log.error("或设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET。")
        log.error("配置后可先跑：python sync_check.py  做只读对账。")
        log.error("-" * 62)
        cfg_eq = float(profile["equity"])
        for s in syms:
            inf = C.compute_profile(profile, bars_map[s], cfg, symbol=s,
                                    equity_override=cfg_eq / n)
            log.info("（配置本金口径）%s：方向=%s 净敞口=%.3f 价格=%.2f 日波动=%.2f%%",
                     s, inf["side"], inf["net_expo"], inf["px"], inf["dvol"] * 100)
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

    # ---------- ③ 账户总权益 → 每币配额 ----------
    bal = api.usdt_balance()
    eq_total = bal["wallet"] + bal["unrealized"]
    if eq_total <= 0:
        eq_total = float(profile["equity"])
        log.warning("账户权益读到 %.2f U，改用配置本金 %.2f U 计算",
                    bal["wallet"] + bal["unrealized"], eq_total)
    quota = eq_total / n
    log.info("账户：钱包 %.2f U  未实现 %+.2f U  →  总权益 %.2f U",
             bal["wallet"], bal["unrealized"], eq_total)
    if multi:
        log.info("资金分配：总权益 %.2f U ÷ %d = 每币配额 %.2f U（与回测「动态再平衡」同口径）",
                 eq_total, n, quota)

    # ---------- ④ 逐币算目标与差异 ----------
    results = []
    for s in syms:
        inf = C.compute_profile(profile, bars_map[s], cfg, symbol=s, equity_override=quota)
        inf["key"] = s
        inf["coin"] = C.coin_name(s)
        pos = api.position(s)
        if pos is None:
            log.info("%s 交易所无持仓记录，按空仓处理", s)
        r = _process_symbol(api, cfg, tr, s, inf, pos, quota, L, log)
        results.append(r)
        log.info("%s 信号=%s(%s) 目标=%s %+.6f  当前=%+.6f  差异=%+.6f（%.1f%%）"
                 "  名义=%+.0fU 保证金=%.0fU  状态=%s %s",
                 s, inf["side"], inf["dirs"], r["side_txt"], r["tgt_qty"],
                 r["cur"], r["delta"], r["rel"] * 100, r["tgt_notional"],
                 r["margin"], r["status"], r["note"])

    # ---------- 多标的：额外算一份「BTC 单跑参照」（本金全押 BTC）----------
    btc_ref = None
    if multi:
        btc_sym = "BTCUSDT" if "BTCUSDT" in syms else syms[0]
        inf = C.compute_profile(profile, bars_map[btc_sym], cfg, symbol=btc_sym,
                                equity_override=eq_total)
        inf["key"] = btc_sym
        inf["coin"] = C.coin_name(btc_sym)
        q, nt = target_position(inf, equity_override=eq_total,
                                notional_cap=_notional_cap(tr, eq_total))
        btc_ref = {"symbol": btc_sym, "coin": C.coin_name(btc_sym), "inf": inf,
                   "tgt_qty": q, "tgt_notional": nt, "quota": eq_total,
                   "margin": abs(nt) / L if L else 0.0}
        log.info("BTC 单跑参照（本金 %.0f U 全押 BTC）：%s 数量 %+.6f 名义 %+.0fU",
                 eq_total, _side_name(q), q, nt)

    bar_dt = results[0]["inf"]["bar_dt"].strftime("%Y-%m-%d") if results else \
        datetime.now(C.CST).strftime("%Y-%m-%d")

    # ---------- ⑤ check：只读，不执行 ----------
    if check_only:
        log.info("[check] 只读模式，不执行下单。若要预演请加 --dry-run。")
        print("\n" + _body(results, btc_ref, multi, syms, eq_total, quota, L,
                           dry=True, check=True))
        return 0

    # ---------- ⑥ 逐币执行（每个交易对独立幂等）----------
    it = load_intent()
    blocked = False
    for r in results:
        if r["status"] != "planned":
            continue
        sym = r["symbol"]
        bdt = r["inf"]["bar_dt"].strftime("%Y-%m-%d")
        key = intent_key(sym, bdt, r["side_txt"])
        rec = it.get(key)
        if rec and rec.get("done") and not force:
            log.info("%s 幂等命中：%s 已执行过（%s），跳过", sym, key, rec.get("at"))
            r["status"] = "skipped"
            r["note"] = "今日该方向已下过单（幂等保护）"
            continue
        if rec and not rec.get("done") and not force:
            log.warning("%s 发现未完成的意图（可能上次下单中断）：%s。"
                        "请先跑 --check 人工核对持仓，确认无误后用 --force 继续。", sym, key)
            r["status"] = "blocked"
            r["note"] = "上次下单中断，需人工核对后用 --force"
            blocked = True
            continue

        if tr.get("set_leverage_on_start", True) and not dry:
            try:
                api.set_leverage(sym, L, tr.get("margin_type", "ISOLATED"))
            except Exception as e:                          # noqa: BLE001
                log.warning("%s 设置杠杆失败（继续）：%s", sym, e)

        # 写入意图（下单前）
        if not dry:
            it[key] = {"done": False, "at": datetime.now(C.CST).isoformat(),
                       "symbol": sym, "target_qty": r["tgt_qty"],
                       "action": r["action"]["kind"]}
            save_intent(prune_intent(it))

        try:
            fills = _execute(api, sym, r["cur"], r["tgt_qty"], r["action"], tr, dry, log)
        except Exception as e:                              # noqa: BLE001
            log.error("%s 下单失败：%s", sym, e, exc_info=True)
            r["status"] = "failed"
            r["error"] = str(e)
            continue

        r["fills"] = fills
        r["status"] = "dry" if dry else "done"
        log.info("%s 动作完成：%s —— %s", sym, r["action"]["label"],
                 "；".join(x.get("desc", "") for x in fills) or "无成交")

        if not dry:
            it[key] = {"done": True, "at": datetime.now(C.CST).isoformat(),
                       "symbol": sym, "target_qty": r["tgt_qty"],
                       "action": r["action"]["kind"],
                       "fills": [x.get("desc", "") for x in fills]}
            save_intent(prune_intent(it))

    # ---------- ⑦ 汇总 → 一封邮件 ----------
    failed = [r for r in results if r["status"] == "failed"]
    acted = [r for r in results if r["status"] in ("done", "dry")]
    if failed:
        label = "❌ 下单失败" if len(failed) == len(results) else "⚠ 部分下单失败"
    elif acted:
        label = "、".join(dict.fromkeys(r["action"]["label"] for r in acted))
    else:
        label = "无需调仓"

    subject = (f"【{head}交易{C.SUBJECT_SUFFIX}】"
               f"{'[预演] ' if dry else ''}{label} · {bar_dt}")
    body = _body(results, btc_ref, multi, syms, eq_total, quota, L, dry=dry)
    html = _body_html(results, btc_ref, multi, syms, eq_total, quota, L, dry=dry)

    if dry:
        log.info("[DRY-RUN] 不发送邮件，正文：\n%s", body)
    else:
        C.send_email(cfg["email"], subject, body, log, html=html)
        log.info("邮件已发送：%s", subject)

    if blocked:
        return 3
    if failed:
        return 1
    return 0


# ============================================================ 邮件正文（纯文本兜底）
def _body(results, btc_ref, multi, syms, eq_total, quota, L, dry=False, check=False):
    now = datetime.now(C.CST)
    ref = results[0]
    bar_dt = ref["inf"]["bar_dt"]
    t_margin = sum(r["margin"] for r in results)
    t_notional = sum(abs(r["tgt_notional"]) for r in results)

    S = []
    S.append(f"{now:%Y-%m-%d %H:%M}（北京时间）　数据 {bar_dt:%Y-%m-%d} 日线（已收盘）")
    if multi:
        S.append(f"模式：四币等权（{len(syms)} 个交易对）　账户总权益 {eq_total:,.2f} U "
                 f"÷ {len(syms)} = 每币配额 {quota:,.2f} U")
    else:
        S.append(f"模式：仅 {C.coin_name(syms[0])}　账户总权益 {eq_total:,.2f} U")
    S.append(f"杠杆：{L}× 逐仓　信号档位：{ref['inf']['name']}　"
             f"参数：{ref['inf']['dirs']}")
    if check:
        S.append("※ 这是 --check 只读取证，没有提交任何订单。")

    # ① 四币等权
    S.append("")
    S.append(f"===== ① {'四币等权 · 你实际要下的单' if multi else '本次目标仓位'}"
             f"（{ref['inf']['name']}）=====")
    S.append(f"  {'币种':<6}{'方向':<5}{'下单数量':>14}{'名义':>10}{'保证金':>10}"
             f"{'日波动':>8}  状态")
    for r in results:
        S.append(f"  {r['coin']:<6}{r['side_txt']:<5}{r['tgt_qty']:>+14.6f}"
                 f"{abs(r['tgt_notional']):>9,.0f}U{r['margin']:>9,.0f}U"
                 f"{r['inf']['dvol'] * 100:>7.1f}%  {_status_txt(r)}")
    if multi:
        S.append(f"  {'合计':<6}{'':5}{'':14}{t_notional:>9,.0f}U{t_margin:>9,.0f}U")
        S.append(f"  → 合计保证金占本金 {t_margin / eq_total * 100:.1f}%，其余为安全垫")

    # ② BTC 单跑参照
    if btc_ref:
        inf = btc_ref["inf"]
        S.append("")
        S.append(f"===== ② BTC 单跑参照（把 {eq_total:,.0f} U 全押 BTC）=====")
        S.append(f"  {C.coin_name(btc_ref['symbol'])}  {_side_name(btc_ref['tgt_qty'])}  "
                 f"数量 {btc_ref['tgt_qty']:+.6f}  名义 {abs(btc_ref['tgt_notional']):,.0f}U  "
                 f"保证金 {btc_ref['margin']:,.0f}U  日波动 {inf['dvol'] * 100:.1f}%")
        S.append("  ※ 这是对照，不要照着下单。四币方案下 BTC 只占 "
                 f"{quota:,.0f} U 配额，比单跑小 {len(syms)} 倍。")

    # ③ 本次动作
    S.append("")
    S.append("===== ③ 本次动作 =====")
    any_act = False
    for r in results:
        if r["fills"]:
            any_act = True
            S.append(f"  · {r['coin']} {r['action']['label']}："
                     f"{'；'.join(x.get('desc', '') for x in r['fills'])}")
            for x in r["fills"]:
                resp = x.get("resp") or {}
                if resp.get("avgPrice"):
                    S.append(f"      成交价 {resp['avgPrice']}　数量 "
                             f"{resp.get('executedQty')}　状态 {resp.get('status')}")
        elif r["status"] == "failed":
            any_act = True
            S.append(f"  · ❌ {r['coin']} 下单失败：{r['error']}")
        elif r["status"] in ("too_small", "blocked"):
            any_act = True
            S.append(f"  · ⚠ {r['coin']} 未下单：{r['note']}")
        else:
            S.append(f"  · {r['coin']}：{r['note'] or '无需操作'}")
    if not any_act and not check:
        S.append("  （本次所有交易对都无需调仓）")

    # ④ 机制
    S.append("")
    S.append("===== 止盈止损怎么工作 =====")
    for t in ["止损 = 方向反转（本邮件会告诉你平仓/反手），不用另外挂止损单",
              "止盈 = 同样等方向反转，让利润奔跑",
              "爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆",
              "每个币独立判断方向、独立下单，一个币爆仓不影响其他币（逐仓隔离）"]:
        S.append(f"  · {t}")
    if dry:
        S.append("")
        S.append("※ 本次为预演，未真实提交订单。")
    S.append("")
    S.append("风险提示：本邮件由量化脚本自动生成，仅为研究参考，不构成投资建议。")
    S.append(f"生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）")
    return "\n".join(S)


def _status_txt(r):
    return {"noop": "无需调仓", "planned": "待执行", "done": "已下单",
            "dry": "预演", "skipped": "幂等跳过", "blocked": "待人工核对",
            "failed": "下单失败", "too_small": "名义过小"}.get(r["status"], r["status"])


# ============================================================ 邮件正文（HTML，手机友好）
def _body_html(results, btc_ref, multi, syms, eq_total, quota, L, dry=False, check=False):
    now = datetime.now(C.CST)
    ref = results[0]
    bar_dt = ref["inf"]["bar_dt"]
    t_margin = sum(r["margin"] for r in results)
    t_notional = sum(abs(r["tgt_notional"]) for r in results)

    P = []
    P.append(C._p(f"{now:%Y-%m-%d %H:%M}（北京时间）　数据 {bar_dt:%Y-%m-%d} 日线（已收盘）",
                  12, "#9aa0a6"))
    if multi:
        P.append(C._p(f"模式：<b>四币等权</b>（{len(syms)} 个交易对）　账户总权益 "
                      f"<b>{eq_total:,.2f} U</b> ÷ {len(syms)} = 每币配额 "
                      f"<b>{quota:,.2f} U</b>"))
    else:
        P.append(C._p(f"模式：<b>仅 {C.coin_name(syms[0])}</b>　账户总权益 "
                      f"<b>{eq_total:,.2f} U</b>"))
    P.append(C._p(f"杠杆 <b>{L}× 逐仓</b>　信号档位 <b>{ref['inf']['name']}</b>　"
                  f"参数 {ref['inf']['dirs']}"))
    if check:
        P.append(C._box("只读取证", C._p("这是 --check 模式，没有提交任何订单。", 12, "#8a5a00")))

    # ① 目标仓位
    P.append(C._h("① " + ("四币等权 · 你实际要下的单" if multi else "本次目标仓位")
                  + f"（{ref['inf']['name']}）"))
    rows = []
    for r in results:
        rows.append([r["coin"], C._side_short(r["tgt_notional"]),
                     f"{r['tgt_qty']:+.6f}", f"{abs(r['tgt_notional']):,.0f} U",
                     f"{r['margin']:,.0f} U", f"{r['inf']['dvol'] * 100:.1f}%",
                     _status_html(r)])
    if multi:
        rows.append(['<b>合计</b>', '', '', f"<b>{t_notional:,.0f} U</b>",
                     f"<b>{t_margin:,.0f} U</b>", '', ''])
    P.append(C._table(["币种", "方向", "下单数量", "名义", "保证金", "日波动", "状态"], rows,
                      ["left", "center", "right", "right", "right", "right", "center"]))
    if multi:
        P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
                 f'合计保证金占本金 <b>{t_margin / eq_total * 100:.1f}%</b>（其余为安全垫）　'
                 f'每个币独立隔离，互不影响</div>')

    # ② BTC 单跑参照
    if btc_ref:
        inf = btc_ref["inf"]
        P.append(C._h("② BTC 单跑参照 · 如果把本金全押 BTC"))
        P.append(C._table(
            ["币种", "方向", "下单数量", "名义", "保证金", "日波动"],
            [[C.coin_name(btc_ref["symbol"]), C._side_short(btc_ref["tgt_notional"]),
              f"{btc_ref['tgt_qty']:+.6f}", f"{abs(btc_ref['tgt_notional']):,.0f} U",
              f"{btc_ref['margin']:,.0f} U", f"{inf['dvol'] * 100:.1f}%"]],
            ["left", "center", "right", "right", "right", "right"]))
        P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:6px;">'
                 f'※ 这是<b>对照</b>，不要照着下单。四币方案下 BTC 只占 {quota:,.0f} U 配额，'
                 f'比单跑小 {len(syms)} 倍，回撤也更小。</div>')

    # ③ 本次动作
    P.append(C._h("③ 本次动作"))
    items = []
    for r in results:
        if r["fills"]:
            items.append(f'<b>{r["coin"]}</b> {r["action"]["label"]}：'
                         + "；".join(x.get("desc", "") for x in r["fills"]))
            for x in r["fills"]:
                resp = x.get("resp") or {}
                if resp.get("avgPrice"):
                    items.append(f'　　{r["coin"]} 成交价 {resp["avgPrice"]}　数量 '
                                 f'{resp.get("executedQty")}　状态 {resp.get("status")}')
        elif r["status"] == "failed":
            items.append(f'<b>{r["coin"]}</b> <span style="color:{C.RED};">❌ 下单失败：'
                         f'{r["error"]}</span>')
        elif r["status"] in ("too_small", "blocked"):
            items.append(f'<b>{r["coin"]}</b> <span style="color:#c98a00;">⚠ 未下单：'
                         f'{r["note"]}</span>')
        else:
            items.append(f'<b>{r["coin"]}</b>：{r["note"] or "无需操作"}')
    P.append(C._ul(items))
    if dry:
        P.append(C._box("预演模式", C._p("本次为 --dry-run，未真实提交订单。", 12, "#8a5a00")))

    # ④ 机制
    P.append(C._h("止盈止损怎么工作"))
    P.append(C._ul([
        "止损 = 方向反转（本邮件会告诉你平仓/反手），不用另外挂止损单",
        "止盈 = 同样等方向反转，让利润奔跑（固定止盈会砍掉贡献收益的大单）",
        "爆仓价是硬底，3× 逐仓下与仓位大小无关，只取决于杠杆",
        "每个币独立判断方向、独立下单，一个币爆仓只亏它自己那份保证金，不影响其他币",
    ]))
    P.append(f'<div style="font-size:11px;color:#9aa0a6;margin-top:16px;">'
             f'生成时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）</div>')

    head = "/".join(C.coin_name(s) for s in syms)
    sub = ("四币等权 · 自动下单" if multi else f"{C.coin_name(syms[0])} · 自动下单")
    if check:
        sub += " · 只读取证"
    elif dry:
        sub += " · 预演"
    return C._shell(f"{head} 交易{C.SUBJECT_SUFFIX}", sub, "".join(P))


def _status_html(r):
    m = {"done": ('<span style="color:#1a7f37;">已下单</span>'),
         "dry": ('<span style="color:#c98a00;">预演</span>'),
         "skipped": ('<span style="color:#9aa0a6;">幂等跳过</span>'),
         "blocked": ('<span style="color:#c98a00;">待核对</span>'),
         "failed": ('<span style="color:#c0392b;">失败</span>'),
         "too_small": ('<span style="color:#c98a00;">名义过小</span>'),
         "planned": ('<span style="color:#9aa0a6;">待执行</span>'),
         "noop": ('<span style="color:#9aa0a6;">无需调仓</span>')}
    return m.get(r["status"], r["status"])


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
