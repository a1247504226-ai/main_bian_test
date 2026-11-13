#!/usr/bin/env python3
"""
TG 频道自动跟单主程序

链路：Telegram 消息 → 解析 → 开仓（挂 7% 兜底）→ 收到止盈止损 → amend

用法：
  python trader.py                        # 实时监听（需要 api_id/api_hash）
  python trader.py --replay 文件.txt       # 回放历史消息，验证解析和下单逻辑
  python trader.py --replay 文件.txt --limit 100 --delay 0.2

安全：
  默认 dry_run，只打印不下单。确认无误后在 config.yaml 改 dry_run: false
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, Optional

import health
import signal_parser as sp
from binance_futures import BinanceFutures
from config import Config, load_config
from executor import Executor, Signal
from mailer import BalanceWatcher, Mailer
from notify import TelegramNotifier
from risk import RiskLimits, RiskManager

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
log = logging.getLogger("trader")


def setup_logging(cfg: Config, verbose=False):
    os.makedirs(cfg.log_dir, exist_ok=True)
    logfile = os.path.join(cfg.log_dir, "trader.log")

    # 每天 0 点切一个新文件，保留 14 天，自动压缩
    from logging.handlers import TimedRotatingFileHandler
    fh = TimedRotatingFileHandler(
        logfile, when="midnight", backupCount=14, encoding="utf-8")
    fh.suffix = "%Y%m%d"
    fh.setFormatter(logging.Formatter(LOG_FMT))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(LOG_FMT))

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[sh, fh],
        force=True,
    )

    # 降噪：这些第三方库的警告对交易没意义，只在出错时才要
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("telethon").setLevel(logging.ERROR)      # 屏蔽断线重连的 WARNING
    logging.getLogger("telethon.network").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.ERROR)


def build(cfg: Config, offline: bool = False):
    client = BinanceFutures(cfg.active_api_key, cfg.active_api_secret,
                            testnet=cfg.testnet, offline=offline)
    limits = RiskLimits(
        max_leverage=cfg.max_leverage,
        max_position_pct=cfg.max_position_pct,
        max_margin_pct=cfg.max_margin_pct,
        max_total_margin_pct=cfg.max_total_margin_pct,
        max_open_positions=cfg.max_open_positions,
        max_symbol_exposure_pct=cfg.max_symbol_exposure_pct,
        daily_loss_limit_pct=cfg.daily_loss_limit_pct,
        max_consecutive_losses=cfg.max_consecutive_losses,
        min_order_notional=cfg.min_order_notional,
        allowed_symbols=cfg.allowed_symbols,
    )
    risk = RiskManager(limits, os.path.join(cfg.state_dir, "daily_stats.json"))
    notifier = TelegramNotifier(cfg.tg_token, cfg.tg_chat_id)

    # 邮件通知：余额跨档提醒
    mailer = Mailer(
        server=cfg.smtp_server, port=cfg.smtp_port, user=cfg.smtp_user,
        password=cfg.smtp_password, to=cfg.smtp_to,
        max_retries=cfg.smtp_max_retries,
    )
    watcher = BalanceWatcher(
        mailer,
        threshold=cfg.balance_alert_threshold,
        step=cfg.balance_alert_step,
        state_file=os.path.join(cfg.state_dir, "balance_alert.json"),
    )

    executor = Executor(client, risk, dry_run=cfg.dry_run, notify=notifier,
                        state_dir=cfg.state_dir, balance_watcher=watcher, mailer=mailer)
    # 时间同步改成「懒同步」：第一次真正发签名请求时才校准，
    # 启动时不再主动连币安。没有信号时程序完全不碰交易所
    return client, risk, executor, notifier


class Trader:
    def __init__(self, cfg: Config, executor: Executor, notifier):
        self.cfg = cfg
        self.ex = executor
        self.notify = notifier
        self.buf = sp.TPSLBuffer(ttl_sec=cfg.tpsl_ttl_sec)
        self.stats = {"open": 0, "amend": 0, "trail": 0, "close": 0,
                      "ignore": 0, "fail": 0}
        self._warned: set = set()   # 已告警过的 symbol，避免重复刷屏
        # 同币种开仓冷却：TG 重连重放、消息重复、编辑再解析都可能重复触发开仓
        self._recent_opens: Dict[str, float] = {}
        self.open_cooldown = 30.0
        # 最近一条开仓信号的币种和时间（无论是否真的开仓成功），
        # 用来判断「无币种的止盈止损」到底属于哪个仓
        self._last_open_sym: str = ""
        self._last_open_ts: float = 0.0
        # 消息必须串行处理：Telethon 默认并发投递，
        # 两个信号同时到达会各自读到「开仓前」的持仓快照，双双通过风控 → 超限开仓
        self._lock = asyncio.Lock()

    # ---------------- 匹配待更新仓位 ----------------
    def _pick_pending(self) -> str:
        """止盈止损消息没带币种名时，只在「恰好一个」待更新仓位时才敢匹配

        多个 pending 时按 FIFO 猜，可能把止损挂到错的币上 ——
        宁可漏掉这条让你手动 amend，也不能改错仓位。
        """
        pending = self.ex.pending.provisional()
        if len(pending) == 1:
            return next(iter(pending))
        if len(pending) > 1:
            log.warning(
                "有 %d 个待更新仓位且消息未带币种名，无法确定归属，已跳过。"
                "请手动执行：python main.py amend --symbol <币种> --tp <价> --sl <价>",
                len(pending))
        return ""

    def _match_by_price(self, pending: Dict[str, Dict], ev) -> str:
        """无币种的止盈止损：用价位合理性反推归属

        只有当「唯一一个」仓位的入场价能让这组价位落在合理区间时才采用；
        0 个或 2 个以上都合理 → 返回空（不猜）。

        例：BTCUSDT 入场 80000，收到 SL=1.5 → 距入场 99.99%，超出 25% 上限，
            自动排除；若另一个仓位在 1.4 附近，则唯一匹配它。
        """
        if ev.sl is None and ev.tp is None:
            return ""
        ok = []
        for sym, info in pending.items():
            try:
                entry = float(info.get("entry") or 0)
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue
            good = True
            if ev.sl is not None:
                d = abs(ev.sl - entry) / entry
                if not (0.005 < d <= 0.25):
                    good = False
            if ev.tp is not None and good:
                d = abs(ev.tp - entry) / entry
                if not (0.005 < d <= 0.60):
                    good = False
            if good:
                ok.append(sym)
        if len(ok) == 1:
            log.info("止盈止损无币种，按价位反推归属 %s（唯一合理候选）", ok[0])
            return ok[0]
        if len(ok) > 1:
            log.info("止盈止损无币种，%d 个仓位的价位都合理 %s，无法区分，跳过",
                     len(ok), ok)
        return ""

    # ---------------- 消息处理 ----------------
    async def on_message(self, text: str, msg_id: int, from_edit: bool = False):
        async with self._lock:      # 串行化，杜绝并发下单
            ev = sp.parse(text)

            if ev.kind == sp.OPEN:
                # 先记录最近一条开仓信号（哪怕后面被排除词/风控跳过），
                # 后面无币种的止盈止损要靠它确认归属
                self._last_open_sym = ev.symbol
                self._last_open_ts = time.time()
                await self._handle_open(ev, from_edit=from_edit)
            elif ev.kind == sp.TPSL:
                await self._handle_tpsl(ev)
            elif ev.kind == sp.TP_HIT:
                await self._handle_tp_hit(ev)
            elif ev.kind == sp.CLOSE:
                await self._handle_close(ev)
            else:
                self.stats["ignore"] += 1
                log.debug("忽略：%s", text[:50])

    def _current_side(self, sym: str) -> str:
        """当前该币持仓方向（long/short），无持仓返回空字符串"""
        try:
            if self.ex.dry_run:
                for p in self.ex.dry.all():
                    if p.get("symbol") == sym:
                        return (p.get("side") or "").lower()
                return ""
            poss = self.ex.c.get_positions(sym)
            if not poss:
                return ""
            return (poss[0].get("side") or "").lower()
        except Exception as e:
            log.debug("查询 %s 持仓失败: %s", sym, e)
            return ""

    async def _handle_open(self, ev: sp.ParsedEvent, from_edit: bool = False):
        sym = ev.symbol
        if self.cfg.allowed_symbols and sym not in self.cfg.allowed_symbols:
            log.info("跳过 %s（不在白名单）", sym)
            return

        # 含「轻仓」「补仓」的票跳过：这类信号需要人工判断补仓时机和仓位大小，
        # 自动化跟不了，硬跟容易在补仓环节失控
        if self.cfg.skip_keywords:
            hit = [k for k in self.cfg.skip_keywords if k in ev.raw]
            if hit:
                log.info("跳过 %s（信号含排除词 %s）：%s",
                         sym, hit, ev.raw[:60].replace("\n", " "))
                self.stats["ignore"] += 1
                return

        # 挂单（限价）信号：频道给的「分批进场：3.13-3.08」是挂单区间，
        # 市价开仓会开在当前价、可能远离区间，默认跳过
        if ev.is_limit and self.cfg.skip_limit_orders:
            log.info("跳过 %s（挂单信号，市价开会偏离进场区间）：%s",
                     sym, ev.raw[:60].replace("\n", " "))
            self.stats["ignore"] += 1
            return

        cur = self._current_side(sym)

        # 同一条消息被编辑成同方向 = 频道只是补了备注/改了标点，绝不能重复开仓
        if from_edit and cur and cur == ev.direction:
            log.info("编辑后仍是同向开仓 %s %s，忽略（防重复下单）", sym, ev.direction)
            self.stats["ignore"] += 1
            return

        # 方向冲突处理
        if cur and ev.direction and cur != ev.direction:
            if from_edit:
                # 编辑成反方向 = 频道明确修正，直接反手（不受 allow_reverse 限制）
                log.info("🔄 信号被编辑，方向反转 %s：%s → %s，执行反手",
                         sym, cur.upper(), ev.direction.upper())
                if self.notify:
                    self.notify(f"🔄 {sym} 信号被编辑\n"
                                f"   {cur.upper()} → {ev.direction.upper()}，正在反手")
                cr = await asyncio.to_thread(self.ex.close, sym, "edit-reverse")
                if not cr.ok:
                    log.error("编辑反手平仓失败 %s: %s", sym, cr.error)
                    self.stats["fail"] += 1
                    return
            elif not self.cfg.allow_reverse:
                msg = (f"⚠️ {sym} 反向信号已跳过\n"
                       f"   当前持有 {cur.upper()}，新信号 {ev.direction.upper()}\n"
                       f"   要自动反手，把 config.yaml 的 allow_reverse 改成 true")
                log.warning(msg.replace("\n", " | "))
                if self.notify:
                    self.notify(msg)
                self.stats["ignore"] += 1
                return
            else:
                log.info("🔄 %s 反向信号：先平 %s 再开 %s", sym, cur, ev.direction)
                cr = await asyncio.to_thread(self.ex.close, sym, "reverse")
                if not cr.ok:
                    log.error("反手平仓失败 %s: %s", sym, cr.error)
                    self.stats["fail"] += 1
                    return

        # 同一币种 30 秒内只开一次：
        # TG 断线重连会重放最近的 update，同一条信号可能被投递两次。
        # 编辑来的信号不检查冷却（它是同一条消息的修正，不是重复投递）
        if not from_edit:
            now = time.time()
            last = self._recent_opens.get(sym, 0.0)
            if now - last < self.open_cooldown:
                log.info("跳过 %s %s：%.0f 秒前刚开过仓（防重复下单）",
                         sym, ev.direction, now - last)
                self.stats["ignore"] += 1
                return

        log.info("📥 开仓信号 %s %s%s", sym, ev.direction,
                 "（来自消息编辑）" if from_edit else "")
        sig = Signal(
            symbol=sym,
            direction=ev.direction,
            entry=ev.entry,
            tp1=ev.tp,
            sl=ev.sl,
            tp_all=list(ev.tp_all or []),
            leverage=self.cfg.leverage,
            margin_pct=self.cfg.margin_pct,
            signal_id=f"tg-{int(time.time())}",
            provisional_sl_pct=self.cfg.provisional_sl_pct,
            provisional_tp_pct=self.cfg.provisional_tp_pct,
        )
        # 执行器是同步的（requests），丢到线程池避免阻塞事件循环
        r = await asyncio.to_thread(self.ex.open, sig)
        if r.ok:
            self.stats["open"] += 1
            self._recent_opens[sym] = time.time()   # 只有开成功才进冷却
        else:
            self.stats["fail"] += 1
            log.error("开仓失败 %s %s: %s", sym, ev.direction, r.error)

    async def _handle_tpsl(self, ev: sp.ParsedEvent):
        # 消息里带了币种名就精确匹配；没带才按 FIFO 分配给最早的待更新仓位
        pending = self.ex.pending.all()
        if ev.symbol:
            if ev.symbol not in pending:
                # 明确指定了币种但没这个待更新仓位 → 忽略，
                # 绝不能退化成 FIFO 去改别的仓（会把止损挂到错的币上）
                log.info("止盈止损指定 %s 但没有待更新仓位，忽略", ev.symbol)
                self.stats["ignore"] += 1
                return
            target = ev.symbol
        else:
            target = self._pick_pending()
            if not target:
                # 多个待更新仓位时，用「价位是否合理」反推归属：
                # 只有当唯一一个仓位的入场价能让这组价位落在合理区间时才采用。
                # 这比 FIFO 猜安全得多（BTC@80000 收到 SL=1.5 会被排除）
                target = self._match_by_price(pending, ev)
            elif target:
                # 单一候选：仍用「最近一条开仓信号」交叉验证。
                # 如果最近的开仓信号不是这个 pending 的币（说明中间有被跳过/失败的信号），
                # 绝不能猜 —— 猜错会把 A 币的止损挂到 B 币上
                if self._last_open_sym and self._last_open_sym != target:
                    log.warning(
                        "止盈止损无币种，最近开仓信号是 %s，但待更新仓位是 %s，"
                        "无法确认归属，跳过（请手动 amend --symbol %s）",
                        self._last_open_sym, target, target)
                    if self.notify:
                        self.notify(
                            f"⚠️ {target} 收到无币种的止盈止损\n"
                            f"   但最近一条信号是 {self._last_open_sym}，无法确认归属\n"
                            f"   已跳过。手动执行：python main.py amend "
                            f"--symbol {target} --tp <价> --sl <价>")
                    self.stats["ignore"] += 1
                    return
                gap = time.time() - self._last_open_ts
                if self._last_open_ts > 0 and gap > 300:
                    log.warning("止盈止损无币种，距最近开仓信号已 %.0f 秒，"
                                "无法确认归属，跳过", gap)
                    self.stats["ignore"] += 1
                    return
            if not target and len(pending) > 1:
                log.warning("有 %d 个待更新仓位且消息未带币种名，价位也无法唯一匹配，"
                            "已跳过。请手动执行：python main.py amend --symbol <币种> "
                            "--tp <价> --sl <价>", len(pending))
                self.stats["ignore"] += 1
                return
        if not target:
            log.info("收到止盈止损但无待更新仓位，忽略：tp=%s sl=%s", ev.tp, ev.sl)
            self.stats["ignore"] += 1
            return

        done = self.buf.feed(ev, target, time.time())
        if not done:
            log.info("已缓存 %s 的部分止盈止损（tp=%s sl=%s），等另一半",
                     target, ev.tp, ev.sl)
            return

        log.info("🔧 止盈止损齐了 %s → TP %s / SL %s", target, done["tp"], done["sl"])
        r = await asyncio.to_thread(
            self.ex.amend_protection, target,
            done["tp"], done["sl"], done.get("tp_all") or [])
        if r.ok:
            self.stats["amend"] += 1
        else:
            self.stats["fail"] += 1
            # 只有「临时性」失败才重试（网络抖动、限频、撤单未确认）。
            # 永久性失败（无持仓、价位不合理、币种不存在）重试 4 次也只是白烧权重：
            # 每次 amend ≈ 14 权重，几十个这种符号同时重试就能把 2400/分钟打爆
            permanent = any(k in (r.error or "") for k in (
                "无持仓", "不合理", "不存在", "超出", "太近", "太远",
                "写错", "未成交", "取整", "缺少止损价"))
            if permanent:
                log.warning("%s 止盈止损改单失败（不可重试）：%s，已放弃",
                            target, r.error)
                if self.notify:
                    self.notify(f"⚠️ {target} 止盈止损改单被拒\n   {r.error}\n"
                                f"   当前仍是兜底保护，请手动检查")
            else:
                # 临时失败：放回缓存由巡检重试，否则频道不重发就永远卡在兜底止损上
                self.buf.restore(target, done["tp"], done["sl"],
                                 done.get("tp_all"))

    async def _handle_tp_hit(self, ev: sp.ParsedEvent):
        """到达 TP1/TP2 → 把止损提到该目标外侧 0.5%，锁定利润后继续博 TP3

        这就是回测算出来最优的策略 C2：到 TP1 后不再裸奔，
        回落到 TP1 附近就止盈出场，继续涨就吃到 TP3。
        """
        sym = ev.symbol
        info = self.ex.pending.get(sym)
        if not info or info.get("status") != "protected":
            log.debug("TP%d 到达但 %s 无受保护仓位，忽略", ev.level, sym)
            return

        tp_all = info.get("tp_all") or []
        if not tp_all or ev.level > len(tp_all):
            log.warning("%s 收到 TP%d 通知但目标价不全 %s，忽略",
                        sym, ev.level, tp_all)
            return

        target = tp_all[ev.level - 1]
        tp3 = tp_all[-1]
        direction = info.get("direction", "")
        cur_sl = info.get("sl")
        entry = info.get("entry")

        # 止损移到目标价「外侧」0.5%：空单在上方，多单在下方
        if direction == "short":
            new_sl = target * 1.005
            if cur_sl and new_sl >= cur_sl:
                log.info("%s TP%d 到达，新止损 %.8g 不优于当前 %.8g，保持不动",
                         sym, ev.level, new_sl, cur_sl)
                return
        else:
            new_sl = target * 0.995
            if cur_sl and new_sl <= cur_sl:
                log.info("%s TP%d 到达，新止损 %.8g 不优于当前 %.8g，保持不动",
                         sym, ev.level, new_sl, cur_sl)
                return

        # 边界校验：止盈价位写错（比如 1-2-3 实际应是 100-200-300）时，
        # 算出的止损会跑到入场价另一侧极远处，挂上去等于没有止损
        if entry and entry > 0:
            bad = (direction == "short" and new_sl < entry * 0.5) or \
                  (direction != "short" and new_sl > entry * 1.5)
            if bad:
                log.warning("%s TP%d 价位异常：目标 %.6g，入场 %.6g，"
                            "算出的止损 %.6g 超出 ±50%% 边界，跳过移动止损",
                            sym, ev.level, target, entry, new_sl)
                if self.notify:
                    self.notify(f"⚠️ {sym} TP{ev.level} 价位异常\n"
                                f"   目标价 {target:.6g}，入场 {entry:.6g}\n"
                                f"   算出的止损 {new_sl:.6g} 超出 ±50%，已跳过\n"
                                f"   当前仍是旧止损 {cur_sl}")
                self.stats["ignore"] += 1
                return

        log.info("🎯 %s 到达 TP%d(%.8g)，止损上移到 %.8g，继续持有博 TP3(%.8g)",
                 sym, ev.level, target, new_sl, tp3)
        r = await asyncio.to_thread(
            self.ex.amend_protection, sym, tp3, new_sl, tp_all, True)
        if r.ok:
            self.stats["trail"] += 1
        else:
            self.stats["fail"] += 1
            # 移动止损失败必须重试：否则止损永远停在旧位置，风险敞口一直扩大
            log.warning("%s TP%d 移动止损失败（%s），放入缓存等重试",
                        sym, ev.level, r.error)
            self.buf.restore(sym, tp3, new_sl, tp_all, relaxed=True)

    async def _handle_close(self, ev: sp.ParsedEvent):
        log.info("📤 离场信号 %s", ev.symbol)
        r = await asyncio.to_thread(self.ex.close, ev.symbol, "tg-signal")
        if r.ok:
            self.stats["close"] += 1
        elif r.error and "无持仓" in r.error:
            # 仓位可能已被交易所条件单平掉，属正常情况
            log.info("%s 离场信号但无持仓（可能已被止盈止损单平掉）", ev.symbol)
        else:
            self.stats["fail"] += 1
            log.error("离场失败 %s: %s", ev.symbol, r.error)

    # ---------------- 定时巡检 ----------------
    async def housekeeping(self):
        # 空闲时（没有待管理仓位、没有待重试改单）一个币安请求都不发。
        # 本地的超时检查、告警去重都不需要网络
        last_pos_check = 0.0
        idle_logged = False
        pos_check_warned = False
        while True:
            await asyncio.sleep(15)
            # 整个循环体包在 try 里：一次异常不能把巡检任务搞死
            try:
                now = time.time()
                api_calls = 0
                # 缓存超时的止盈止损
                for sym, part in self.buf.flush_expired(now):
                    if sym not in self._warned:
                        self._warned.add(sym)
                        if self.notify:
                            self.notify(
                                f"⚠️ {sym} 止盈止损只收到一半（tp={part['tp']} sl={part['sl']}），"
                                f"超过 {self.cfg.tpsl_ttl_sec} 秒未补齐，请手动检查")
                # 长时间未更新的仓位（每个 symbol 只告警一次，避免每 15 秒刷屏）
                for sym in self.ex.pending.stale(10):
                    if sym not in self._warned:
                        self._warned.add(sym)
                        if self.notify:
                            self.notify(f"⚠️ {sym} 开仓后超过 10 分钟仍未收到真实止盈止损，"
                                        f"当前只有兜底保护，请检查频道消息")
                # 重试之前失败的 amend（最多 3 次，超过就告警放弃）
                # 每轮最多处理 3 个符号：amend 一次 ≈ 14 权重，
                # 几十个符号同时重试会把 2400/分钟 打爆（418 的来源）
                _ready = list(self.buf.ready().items())
                if len(_ready) > 3:
                    log.warning("待重试改单 %d 个，本轮只处理 3 个（控制 API 权重）",
                                len(_ready))
                for sym, data in _ready[:3]:
                    if sym not in self.ex.pending.all():
                        self.buf.drop(sym)
                        continue
                    if data.get("retry", 0) > 3:
                        self.buf.drop(sym)
                        if self.notify:
                            self.notify(f"❌ {sym} 止盈止损改单连续失败 3 次，"
                                        f"当前仍是兜底保护，请手动执行 amend")
                        continue
                    log.info("重试 amend %s (第 %d 次)", sym, data.get("retry", 0))
                    # 必须带 tp_all 和 relaxed：
                    #  - 不带 tp_all 会丢掉 TP1/TP2 目标价，后续无法移动止损
                    #  - 不带 relaxed 时「移动止损」会被严格价位校验拒掉
                    relaxed = bool(data.get("relaxed"))
                    r = await asyncio.to_thread(
                        self.ex.amend_protection, sym, data["tp"], data["sl"],
                        data.get("tp_all") or [], relaxed)
                    api_calls += 1
                    if r.ok:
                        self.buf.drop(sym)
                        self.stats["amend"] += 1
                    else:
                        self.buf.restore(sym, data["tp"], data["sl"],
                                         data.get("tp_all"), relaxed)

                # 已解决的移出告警集合
                active = set(self.ex.pending.all())
                self._warned &= active

                # 仓位已经被止损/止盈单平掉的，pending 记录要清掉，
                # 否则会一直误报「超过 10 分钟未更新」。
                # 关键：只在「有待管理仓位」且距上次检查够久时才连币安，
                # 空闲时（active 为空）完全不发请求 —— 以前这里每 15 秒
                # 无条件查一次持仓，是「没信号也一直连币安」的根源
                if (not self.ex.dry_run and active
                        and now - last_pos_check >= self.cfg.pos_check_interval):
                    last_pos_check = now
                    api_calls += 1
                    try:
                        live = {p["symbol"] for p in self.ex.c.get_positions()}
                        for sym in list(active):
                            if sym in live:
                                continue
                            # 刚开的仓可能因 API 延迟查不到，等 2 分钟再判断，
                            # 否则会把还在的仓误判成「已了结」
                            info = self.ex.pending.get(sym) or {}
                            try:
                                age = (datetime.now() - datetime.fromisoformat(
                                    info.get("created_at", ""))).total_seconds()
                            except Exception:
                                age = 999
                            if age < 120:
                                continue
                            log.info("仓位已了结（开仓 %.0f 秒后查无持仓），清理 pending: %s",
                                     age, sym)
                            self.ex.pending.remove(sym)
                    except Exception as e:
                        # 连接类错误限流：连不上时不要每轮刷一条
                        if not pos_check_warned:
                            pos_check_warned = True
                            log.warning("巡检查询持仓失败（%s），稍后重试", e)
                else:
                    pos_check_warned = False

                # 让用户能从日志确认「空闲时没有连币安」
                if api_calls == 0:
                    if not idle_logged:
                        idle_logged = True
                        log.info("空闲巡检：无待管理仓位/改单，本轮零币安请求"
                                 "（只在有信号时才连币安）")
                else:
                    idle_logged = False
                    log.debug("巡检本轮发出 %d 次币安请求", api_calls)

                health.beat(stats=str(self.stats),
                            pending=len(self.ex.pending.all()))
            except Exception as e:
                log.exception("巡检异常（不影响主流程）: %s", e)


# ----------------------------------------------------------------------
async def run_live(cfg: Config, trader: Trader):
    from tg_listener import TgListener, check_ready

    problem = check_ready(cfg.tg_api_id, cfg.tg_api_hash, cfg.tg_channel)
    if problem:
        print(f"❌ {problem}")
        sys.exit(1)

    # session 文件必须真实存在
    sess_file = cfg.tg_session if cfg.tg_session.endswith(".session") else cfg.tg_session + ".session"
    if not os.path.exists(sess_file):
        found = sorted(f for f in os.listdir(".") if f.endswith(".session"))
        print(f"❌ 找不到 session 文件：{sess_file}")
        print(f"   当前目录：{os.getcwd()}")
        if found:
            print(f"   目录里已有的 session：{found}")
            print(f"   → 把 .env.bot 里的 TG_SESSION 改成 {found[0][:-len('.session')]}")
        else:
            print("   目录里没有任何 .session 文件")
        sys.exit(1)

    listener = TgListener(
        api_id=cfg.tg_api_id,
        api_hash=cfg.tg_api_hash,
        phone=cfg.tg_phone,
        channel=cfg.tg_channel,
        session_path=cfg.tg_session,
        dedup_file=os.path.join(cfg.state_dir, "seen_msgs.json"),
        proxy_url=cfg.tg_proxy,
    )
    # 启动时打印现有持仓（可选，默认关闭以保持启动零请求）
    if cfg.print_positions_on_start:
        try:
            poss = trader.ex.c.get_positions()
            if poss:
                print(f"当前持仓 {len(poss)} 个：")
                for p_ in poss:
                    print(f"  {p_['symbol']:12s} {p_['side']:5s} {abs(p_['amount'])} "
                          f"入场 {p_['entry']} 标记 {p_['mark']} "
                          f"浮盈 {p_['unrealized']:+.2f}U")
            else:
                print("当前无持仓")
            print()
        except Exception as e:
            # 启动连不上币安不影响监听；真正下单时会重试
            log.debug("启动查询持仓失败: %s", e)
    else:
        print("启动零请求模式：无信号时不连币安（有信号下单时才连）")
        print()

    health.start(cfg.health_port)
    # 保存引用：create_task 的返回值若没人持有，可能被 GC 回收导致巡检停止
    hk_task = asyncio.create_task(trader.housekeeping())
    try:
        await listener.run(trader.on_message)
    finally:
        hk_task.cancel()


async def run_fetch(cfg: Config, trader: Trader, limit: int):
    """直接从 TG 拉历史消息，解析并走一遍下单逻辑（dry-run）

    不用手动导出 txt，也能验证 session 是否可用、解析规则对不对。
    """
    from telethon import TelegramClient
    from tg_listener import build_proxy, check_ready, resolve_channel

    problem = check_ready(cfg.tg_api_id, cfg.tg_api_hash, cfg.tg_channel)
    if problem:
        print(f"❌ {problem}")
        sys.exit(1)

    # 清空上次的虚拟持仓，避免影响风控
    trader.ex.dry.clear()
    for s_ in list(trader.ex.pending.all()):
        trader.ex.pending.remove(s_)
    trader.ex.strict_price_check = False

    # 先确认 session 文件真的存在，否则 Telethon 会静默新建空 session
    sess_file = cfg.tg_session if cfg.tg_session.endswith(".session") else cfg.tg_session + ".session"
    if not os.path.exists(sess_file):
        found = sorted(f for f in os.listdir(".") if f.endswith(".session"))
        print(f"❌ 找不到 session 文件：{sess_file}")
        print(f"   当前目录：{os.getcwd()}")
        if found:
            print(f"   目录里已有的 session：{found}")
            print(f"   → 把 .env.bot 里的 TG_SESSION 改成 {found[0][:-len('.session')]}")
        else:
            print("   目录里没有任何 .session 文件，请先把 session 放进来")
        sys.exit(1)
    print(f"session 文件：{sess_file} ({os.path.getsize(sess_file)} 字节)")

    proxy = build_proxy(cfg.tg_proxy)
    if proxy:
        print(f"代理：{proxy['proxy_type']}://{proxy['addr']}:{proxy['port']}")
    client = TelegramClient(
        cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash,
        proxy=proxy, connection_retries=5, retry_delay=3, timeout=20,
    )
    print("连接 Telegram...")
    await client.connect()
    if await client.is_user_authorized():
        print("session 有效，无需验证码")
    else:
        if not cfg.tg_phone:
            print()
            print("❌ session 未授权，且没配 TG_PHONE，无法登录")
            print(f"   当前工作目录：{os.getcwd()}")
            print(f"   1) 确认 {cfg.tg_session}.session 就在这个目录里")
            print("   2) 或在 .env.bot 填 TG_PHONE=+86... 后重跑（会要求输一次验证码）")
            await client.disconnect()
            sys.exit(1)
        print("session 无效，用手机号重新登录（需要输验证码）...")
        await client.start(phone=cfg.tg_phone)

    me = await client.get_me()
    print(f"已登录：{me.first_name} (@{me.username or '-'})")

    entity = await client.get_entity(resolve_channel(cfg.tg_channel))
    title = getattr(entity, "title", cfg.tg_channel)
    print(f"频道：{title}")

    msgs = await client.get_messages(entity, limit=limit)
    print(f"拉到 {len(msgs)} 条消息，按时间正序解析（dry_run={cfg.dry_run}）\n")

    ok = ign = 0
    for m in reversed(msgs):
        text = (m.message or "").strip()
        if not text:
            continue
        ev = sp.parse(text)
        if ev.kind != sp.IGNORE:
            print(f"[{m.id:>6}] {ev.kind:7s} {ev.symbol or '-':12s} "
                  f"{ev.direction or '-':6s} tp={ev.tp} sl={ev.sl}")
            ok += 1
        else:
            ign += 1
        await trader.on_message(text, m.id)

    print(f"\n解析出信号 {ok} 条，忽略 {ign} 条")
    print(f"完成：开仓 {trader.stats['open']}，改单 {trader.stats['amend']}，"
          f"移动止损 {trader.stats['trail']}，平仓 {trader.stats['close']}，"
          f"忽略 {trader.stats['ignore']}，失败 {trader.stats['fail']}")
    print("\n" + trader.ex.pending_report())
    await client.disconnect()


async def run_replay(cfg: Config, trader: Trader, path: str,
                     limit: Optional[int], delay: float):
    """回放历史消息，验证解析 + 下单逻辑（不下真单，dry_run 有效）"""
    # 每次回放从干净状态开始，避免上次的虚拟持仓影响风控
    trader.ex.dry.clear()
    for s in list(trader.ex.pending.all()):
        trader.ex.pending.remove(s)
    # 历史价位与当前价必然不匹配，回放时关掉价位校验
    trader.ex.strict_price_check = False

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    blocks = [b.strip() for b in text.split("=" * 70) if b.strip()]
    blocks = list(reversed(blocks))          # 原文件是倒序，转成时间正序
    if limit:
        blocks = blocks[:limit]

    print(f"回放 {len(blocks)} 条消息（dry_run={cfg.dry_run}）\n")
    msg_id = 0
    for b in blocks:
        msg_id += 1
        ev = sp.parse(b)
        if ev.kind != sp.IGNORE:
            print(f"[{msg_id:03d}] {ev.kind:7s} {ev.symbol or '-':12s} "
                  f"{ev.direction or '-':6s} tp={ev.tp} sl={ev.sl}")
        await trader.on_message(b, msg_id)
        await asyncio.sleep(delay)

    print(f"\n完成：开仓 {trader.stats['open']}，改单 {trader.stats['amend']}，"
          f"移动止损 {trader.stats['trail']}，平仓 {trader.stats['close']}，"
          f"忽略 {trader.stats['ignore']}，失败 {trader.stats['fail']}")
    print("\n" + trader.ex.pending_report())


# ----------------------------------------------------------------------
def main():
    # 开机自启时工作目录是 C:\Windows\System32，必须切回脚本所在目录
    # 否则找不到 config.yaml / .env.bot
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    p = argparse.ArgumentParser(description="TG 频道自动跟单")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--live", action="store_true", help="真实下单")
    p.add_argument("--replay", metavar="FILE", help="回放本地历史消息文件")
    p.add_argument("--fetch", type=int, metavar="N", default=None,
                   help="直接从 TG 拉最近 N 条消息并解析（dry-run 验证用）")
    p.add_argument("--limit", type=int, default=None, help="回放条数")
    p.add_argument("--delay", type=float, default=0.1, help="回放间隔秒")
    p.add_argument("--testnet", action="store_true",
                   help="用币安测试网（默认，虚拟资金）")
    p.add_argument("--mainnet", action="store_true",
                   help="切币安主网（真实资金，需配主网 API Key）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.testnet and args.mainnet:
        print("❌ --testnet 和 --mainnet 不能同时指定")
        sys.exit(1)

    cfg = load_config(args.config)
    if args.mainnet:
        cfg.testnet = False
    elif args.testnet:
        cfg.testnet = True
    if args.live:
        cfg.dry_run = False
    # --fetch 拉的是历史消息，按几小时前的信号在「当前价」开仓毫无意义，
    # 而且会真的把钱亏掉 —— 强制 dry-run（放在横幅之前，避免显示 LIVE 误导）
    if args.fetch and not cfg.dry_run:
        print("⚠️  --fetch 强制使用 DRY-RUN（历史信号不能用来真下单）")
        cfg.dry_run = True
    setup_logging(cfg, args.verbose)

    print("=" * 64)
    print(f"  环境:   {cfg.env_name}")
    print(f"  地址:   {cfg.base_url}")
    print(f"  模式:   {'DRY-RUN 只打印' if cfg.dry_run else 'LIVE 真实下单'}")
    key = cfg.active_api_key
    print(f"  API Key: {key[:8] + '...' + key[-4:] if key else '未配置'}")
    print(f"  杠杆:   {cfg.leverage}x | 每仓保证金 {cfg.margin_pct*100:.0f}% "
          f"→ 名义 {cfg.margin_pct*cfg.leverage*100:.0f}%")
    print(f"  兜底:   SL {cfg.provisional_sl_pct}% / TP {cfg.provisional_tp_pct}%")
    print("=" * 64)

    if not cfg.testnet:
        print("\n⚠️  主网模式：所有下单都是真实资金，请确认配置无误")
        if not cfg.mainnet_api_key:
            print(f"❌ {cfg.missing_key_hint()}")
            sys.exit(1)
        if cfg.dry_run:
            print("   （当前是 DRY-RUN，只打印不会真下单）")
    print()

    # 回放强制离线：不连币安，避免反复测试把 IP 权重打爆（418 封禁）
    offline = bool(args.replay)
    client, risk, executor, notifier = build(cfg, offline=offline)
    trader = Trader(cfg, executor, notifier)

    try:
        if args.fetch:
            asyncio.run(run_fetch(cfg, trader, args.fetch))
        elif args.replay:
            asyncio.run(run_replay(cfg, trader, args.replay, args.limit, args.delay))
        else:
            asyncio.run(run_live(cfg, trader))
    except KeyboardInterrupt:
        print("\n已退出")
        notifier.send("🛑 跟单程序已停止")


if __name__ == "__main__":
    main()
