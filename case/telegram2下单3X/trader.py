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
        self.stats = {"msg": 0, "open": 0, "amend": 0, "trail": 0, "close": 0,
                      "ignore": 0, "skip": 0, "fail": 0}
        self._warned: set = set()   # 已告警过的 symbol，避免重复刷屏
        self._unmanaged_warned: set = set()  # 有仓无 pending 的告警去重（重启遗留裸仓）
        # 同币种开仓冷却：TG 重连重放、消息重复、编辑再解析都可能重复触发开仓
        self._recent_opens: Dict[str, float] = {}
        self.open_cooldown = 30.0
        # 回放专用：虚拟时钟偏移（按消息间隔推进，避免把几天消息压缩成几秒）
        self._vclock_offset = 0.0
        # 回放专用：到 TP3 时模拟交易所止盈单成交、释放仓位槽
        # （实盘由交易所执行 TP 单，这里没有交易所，需要手动模拟）
        self.sim_close = False
        # 最近一条开仓信号的币种和时间（无论是否真的开仓成功），
        # 用来判断「无币种的止盈止损」到底属于哪个仓
        self._last_open_sym: str = ""
        self._last_open_ts: float = 0.0
        # 当前消息的「发出到处理」秒数（None = 不知道，回放/CLI 场景）
        self._msg_age: Optional[float] = None
        # 消息必须串行处理：Telethon 默认并发投递，
        # 两个信号同时到达会各自读到「开仓前」的持仓快照，双双通过风控 → 超限开仓
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        """当前时间。回放时叠加虚拟偏移，让「30 秒冷却 / 90 秒 TTL」按消息间隔生效，
        而不是把几天消息压缩到几秒内跑完导致冷却误触发。"""
        return time.time() + self._vclock_offset

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

    def _price_fits(self, info: Dict, ev) -> bool:
        """价位是否与这个仓位的入场价匹配（方向 + 距离都合理）。

        方向校验：多单止损必须在 entry 下方、止盈在上方；空单反之。
        价位明显在错误一侧说明是别的币的价位，绝不能挂过去。
        """
        try:
            entry = float(info.get("entry") or 0)
        except (TypeError, ValueError):
            return False
        if entry <= 0 or (ev.sl is None and ev.tp is None):
            return False
        direction = (info.get("direction") or "").lower()
        if ev.sl is not None:
            d = abs(ev.sl - entry) / entry
            if not (0.005 < d <= 0.25):
                return False
            # 方向：多单 SL 应低于 entry，空单 SL 应高于 entry
            if direction == "long" and ev.sl >= entry:
                return False
            if direction == "short" and ev.sl <= entry:
                return False
        if ev.tp is not None:
            d = abs(ev.tp - entry) / entry
            if not (0.005 < d <= 0.60):
                return False
            if direction == "long" and ev.tp <= entry:
                return False
            if direction == "short" and ev.tp >= entry:
                return False
        return True

    def _match_by_price(self, pending: Dict[str, Dict], ev) -> str:
        """无币种的止盈止损：用价位合理性反推归属

        只有当「唯一一个」仓位的入场价能让这组价位落在合理区间时才采用；
        0 个或 2 个以上都合理 → 返回空（不猜）。

        例：BTCUSDT 入场 80000，收到 SL=1.5 → 距入场 99.99%，超出 25% 上限，
            自动排除；若另一个仓位在 1.4 附近，则唯一匹配它。
        """
        ok = [sym for sym, info in pending.items() if self._price_fits(info, ev)]
        if len(ok) == 1:
            log.info("止盈止损无币种，按价位反推归属 %s（唯一合理候选）", ok[0])
            return ok[0]
        if len(ok) > 1:
            log.info("止盈止损无币种，%d 个仓位的价位都合理 %s，无法区分，跳过",
                     len(ok), ok)
        return ""

    def _match_recent_open(self, pending: Dict[str, Dict], ev) -> str:
        """新频道兼容：多仓时优先试「最近开仓」的币，但必须价位方向校验通过。

        新频道「砸鍋賣鐵去炒幣」92% 的价位簇紧跟在对应市价信号之后发出，
        所以「最近成功开仓」是最强归属线索。但仍用 _price_fits 做方向+距离
        校验：若价位明显不匹配（是别的币的），返回空让其它逻辑处理，绝不猜错。
        """
        sym = self._last_open_sym
        if not sym or sym not in pending:
            return ""
        info = pending[sym]
        if not self._price_fits(info, ev):
            log.info("最近开仓 %s 的价位校验不通过，不按时间归属", sym)
            return ""
        log.info("止盈止损无币种，按最近开仓归属 %s（价位方向校验通过）", sym)
        return sym

    # ---------------- 消息处理 ----------------
    async def on_message(self, text: str, msg_id: int, from_edit: bool = False,
                         msg_dt=None):
        async with self._lock:      # 串行化，杜绝并发下单
            self.stats["msg"] += 1
            # 消息发出时间 → 「断线补发的旧信号」判断依据（见 _handle_open）。
            # msg_dt 是 Telethon 给的 UTC datetime；回放/CLI 没有，保持 None。
            self._msg_age = None
            if msg_dt is not None:
                try:
                    from datetime import datetime, timezone
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    self._msg_age = (datetime.now(timezone.utc) - msg_dt).total_seconds()
                except Exception:
                    self._msg_age = None
            ev = sp.parse(text)

            if ev.kind == sp.OPEN:
                # 归属标记只在「开仓真正成功」时更新（在 _handle_open 里做）。
                # 不能在这里无条件记：一条被风控拒/保证金不足/排除词跳过的开仓信号
                # 若也写进来，会把真正仓位的无币种止盈止损顶掉——
                # 实盘日志实测：A 开仓成功 → B 开仓失败 → A 的止盈止损被判给 B 而跳过。
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

        # 旧信号不追：TG 断线重连会补发停机期间的消息，频道喊单价可能已经
        # 偏离现价几个百分点，照着市价开 = 一开仓就浮亏（严重时直接打穿兜底止损）。
        # 编辑事件不查（编辑往往是对旧消息的修正，不是新喊单）。
        max_age = getattr(self.cfg, "max_signal_age_sec", 0) or 0
        if (not from_edit and max_age > 0 and self._msg_age is not None
                and self._msg_age > max_age):
            log.warning("跳过 %s %s：信号已发出 %.0f 秒（上限 %d 秒），"
                        "价格可能已远离，不追单",
                        sym, ev.direction, self._msg_age, max_age)
            self.stats["ignore"] += 1
            return

        if self.cfg.allowed_symbols and sym not in self.cfg.allowed_symbols:
            log.info("跳过 %s（不在白名单）", sym)
            self.stats["skip"] += 1
            return

        # 轻仓信号优先豁免：含 light_keywords（如「轻仓」）的票不跳过，
        # 改用小仓位尝试。轻仓消息常同时带「补仓」（预留补仓位），
        # 所以必须先判轻仓，否则会被 skip_keywords 里的「补仓」误跳。
        light_words = self.cfg.light_keywords or []
        is_light = any(k in ev.raw for k in light_words)

        # 含「补仓」等非轻仓排除词的票跳过：需要人工判断补仓时机，自动化跟不了
        if not is_light and self.cfg.skip_keywords:
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
            now = self._now()
            last = self._recent_opens.get(sym, 0.0)
            if now - last < self.open_cooldown:
                log.info("跳过 %s %s：%.0f 秒前刚开过仓（防重复下单）",
                         sym, ev.direction, now - last)
                self.stats["ignore"] += 1
                return

        log.info("📥 开仓信号 %s %s%s%s", sym, ev.direction,
                 "（来自消息编辑）" if from_edit else "",
                 " 🟡轻仓小仓位" if is_light else "")
        # 轻仓单：用 light_* 覆盖杠杆/保证金，其余（止盈止损、入场位、tp_all、
        # 兜底百分比）完全照常。常规单仍用 config 的 leverage/margin_pct。
        use_lev = self.cfg.light_leverage if is_light else self.cfg.leverage
        use_margin = self.cfg.light_margin_pct if is_light else self.cfg.margin_pct
        if is_light:
            log.info("%s 轻仓模式：%dx 杠杆 / %.0f%% 保证金（常规 %dx / %.0f%%）",
                     sym, use_lev, use_margin * 100,
                     self.cfg.leverage, self.cfg.margin_pct * 100)
        sig = Signal(
            symbol=sym,
            direction=ev.direction,
            entry=ev.entry,
            tp1=ev.tp,
            sl=ev.sl,
            tp_all=list(ev.tp_all or []),
            leverage=use_lev,
            margin_pct=use_margin,
            signal_id=f"tg-{int(time.time())}",
            provisional_sl_pct=self.cfg.provisional_sl_pct,
            provisional_tp_pct=self.cfg.provisional_tp_pct,
        )
        # 执行器是同步的（requests），丢到线程池避免阻塞事件循环
        r = await asyncio.to_thread(self.ex.open, sig)
        if r.ok:
            self.stats["open"] += 1
            self._recent_opens[sym] = self._now()   # 只有开成功才进冷却
            # 归属标记只在真正开仓成功时更新：后续无币种的止盈止损靠它确认归属。
            # 失败/跳过的信号不写，否则会把已有仓位的止盈止损顶掉（实盘 bug）。
            self._last_open_sym = sym
            self._last_open_ts = self._now()
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
                # 多个待更新仓位：先试「最近开仓」的币（新频道价位簇紧跟开仓发出，
                # 92% 场景适用），但必须过价位方向校验；不通过再按价位全局反推。
                target = self._match_recent_open(pending, ev)
                if not target:
                    # 只有当唯一一个仓位的入场价能让这组价位落在合理区间时才采用。
                    # 这比 FIFO 猜安全得多（BTC@80000 收到 SL=1.5 会被排除）
                    target = self._match_by_price(pending, ev)
            elif target:
                # 单一候选：仍用「最近一条开仓信号」交叉验证。
                # 如果最近的开仓信号不是这个 pending 的币（说明中间有被跳过/失败的信号），
                # 先用价位做二次确认：价位方向/距离都对得上才敢挂，
                # 对不上就跳过 —— 猜错会把 A 币的止损挂到 B 币上
                price_ok = False
                if self._last_open_sym and self._last_open_sym != target:
                    price_ok = self._price_fits(pending.get(target) or {}, ev)
                    if not price_ok:
                        log.warning(
                            "止盈止损无币种，最近开仓信号是 %s，但待更新仓位是 %s，"
                            "价位也对不上，无法确认归属，跳过（请手动 amend --symbol %s）",
                            self._last_open_sym, target, target)
                        self.ex._critical(
                            f"⚠️ {target} 收到无币种的止盈止损\n"
                            f"   但最近一条信号是 {self._last_open_sym}，价位也对不上\n"
                            f"   已跳过。手动执行：python main.py amend "
                            f"--symbol {target} --tp <价> --sl <价>",
                            key=f"tpsl_owner_{target}")
                        self.stats["ignore"] += 1
                        return
                    log.info("%s 与最近开仓 %s 不同，但价位方向/距离校验通过，采用",
                             target, self._last_open_sym)
                # 时间衰减只在「价位也没法确认」时才生效：
                # 新频道会把同一批信号的价位簇延迟发出（人工整理），
                # 价位已对上却被 300 秒规则挡掉属于漏单，不是防错。
                if not price_ok:
                    gap = self._now() - self._last_open_ts
                    if self._last_open_ts > 0 and gap > 300:
                        log.warning("止盈止损无币种，距最近开仓信号已 %.0f 秒，"
                                    "无法确认归属，跳过", gap)
                        self.stats["ignore"] += 1
                        return
            if not target and len(pending) > 1:
                log.warning("有 %d 个待更新仓位且消息未带币种名，价位也无法唯一匹配，"
                            "已跳过。请手动执行：python main.py amend --symbol <币种> "
                            "--tp <价> --sl <价>", len(pending))
                self.ex._critical(
                    f"⚠️ 收到无币种止盈止损，但有 {len(pending)} 个待更新仓位，"
                    f"价位无法唯一匹配，已跳过\n"
                    f"   请手动执行：python main.py amend --symbol <币种> --tp <价> --sl <价>",
                    key="tpsl_ambiguous")
                self.stats["ignore"] += 1
                return
        if not target:
            log.info("收到止盈止损但无待更新仓位，忽略：tp=%s sl=%s", ev.tp, ev.sl)
            self.stats["ignore"] += 1
            return

        done = self.buf.feed(ev, target, self._now())
        if not done:
            # 频道经常把止损单独发一条（「止損：0.173」），压根不给止盈。
            # 只缓存等另一半的话永远等不到 —— 止损会一直停在兜底位置
            # （历史 1 万条里有 48 条这种单发止损）。这里把缺的一边用
            # 「当前已挂的价位」补齐后立即改单，等价于只动这一边。
            done = self._fill_one_sided(ev, target)
        if not done:
            log.info("已缓存 %s 的部分止盈止损（tp=%s sl=%s），等另一半",
                     target, ev.tp, ev.sl)
            self.stats["skip"] += 1     # 只收到一半，缓存等另一半（非最终动作）
            return

        log.info("🔧 止盈止损齐了 %s → TP %s / SL %s%s", target,
                 done["tp"], done["sl"], "（单边更新）" if done.get("one_sided") else "")
        try:
            r = await asyncio.to_thread(
                self.ex.amend_protection, target,
                done["tp"], done["sl"], done.get("tp_all") or [],
                done.get("relaxed", False))
        except Exception as e:
            # amend 内部抛异常（网络/币安错误）时，这条止盈止损不能丢：
            # 放回缓存让巡检重试，否则仓位会一直停在旧保护上
            log.exception("%s 止盈止损改单异常: %s", target, e)
            self.stats["fail"] += 1
            self.buf.restore(target, done["tp"], done["sl"], done.get("tp_all"),
                             relaxed=done.get("relaxed", False))
            self.ex._critical(
                f"⚠️ {target} 止盈止损改单异常：{e}\n"
                f"   已放回重试队列，当前仍是旧保护",
                key=f"amend_err_{target}")
            return
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
                self.ex._critical(
                    f"⚠️ {target} 止盈止损改单被拒\n   {r.error}\n"
                    f"   当前仍是兜底保护，请手动检查",
                    key=f"amend_reject_{target}")
            else:
                # 临时失败：放回缓存由巡检重试，否则频道不重发就永远卡在兜底止损上
                self.buf.restore(target, done["tp"], done["sl"],
                                 done.get("tp_all"),
                                 relaxed=done.get("relaxed", False))

    def _fill_one_sided(self, ev: sp.ParsedEvent, target: str):
        """只收到止盈或只收到止损时，用「当前已挂的价位」补齐另一边

        频道的「止損：0.173」是独立一条消息，没有配套止盈。
        如果傻等另一半，这条止损永远不会生效（仓位一直用兜底止损）。
        补齐后走正常 amend 流程：撤旧单 → 重挂 SL+TP，没给的那边保持原值。

        返回 None 表示不处理（两边都缺、或价位与现值相同），交回缓存等另一半。
        """
        if (ev.tp is None) == (ev.sl is None):
            return None                      # 两边都有 / 两边都没有 → 不归这里管
        info = self.ex.pending.get(target) or {}
        if not info:
            return None
        cur_tp = info.get("tp") or info.get("provisional_tp")
        cur_sl = info.get("sl") or info.get("provisional_sl")
        new_tp = ev.tp if ev.tp is not None else cur_tp
        new_sl = ev.sl if ev.sl is not None else cur_sl
        if not (new_tp and new_sl):
            return None

        # 与现值完全相同 → 不必白烧一次撤单+重挂
        if ev.sl is not None and cur_sl and abs(ev.sl - float(cur_sl)) < 1e-12:
            log.info("%s 单发止损 %.8g 与当前一致，跳过改单", target, ev.sl)
            return None
        if ev.tp is not None and cur_tp and abs(ev.tp - float(cur_tp)) < 1e-12:
            log.info("%s 单发止盈 %.8g 与当前一致，跳过改单", target, ev.tp)
            return None

        # 止损已移到盈利区（多单在入场上方 / 空单在下方）时，
        # 严格校验「止损必须在入场价外侧」会误拒 → 走 relaxed（移动止损）通道
        entry = float(info.get("entry") or 0)
        direction = (info.get("direction") or "").lower()
        relaxed = bool(ev.sl is not None and entry > 0 and (
            (direction == "long" and ev.sl > entry) or
            (direction == "short" and ev.sl < entry)))

        self.buf.drop(target)                # 清掉可能残留的半条，避免和新价位拼错
        return {"tp": new_tp, "sl": new_sl,
                "tp_all": info.get("tp_all") or list(ev.tp_all or []),
                "relaxed": relaxed, "one_sided": True}

    async def _handle_tp_hit(self, ev: sp.ParsedEvent):
        """到达 TP1/TP2 → 把止损提到该目标外侧 0.5%，锁定利润后继续博 TP3

        这就是回测算出来最优的策略 C2：到 TP1 后不再裸奔，
        回落到 TP1 附近就止盈出场，继续涨就吃到 TP3。
        """
        sym = ev.symbol
        info = self.ex.pending.get(sym)
        if not info or info.get("status") != "protected":
            log.debug("TP%d 到达但 %s 无受保护仓位，忽略", ev.level, sym)
            self.stats["skip"] += 1
            return

        tp_all = info.get("tp_all") or []
        if not tp_all or ev.level > len(tp_all):
            log.warning("%s 收到 TP%d 通知但目标价不全 %s，忽略",
                        sym, ev.level, tp_all)
            self.ex._critical(
                f"⚠️ {sym} 收到 TP{ev.level} 通知，但目标价清单不全 {tp_all or '空'}\n"
                f"   本次移动止损已跳过，请手动确认止损位置",
                key=f"trail_notarget_{sym}")
            self.stats["skip"] += 1
            return

        target = tp_all[ev.level - 1]
        tp3 = tp_all[-1]
        direction = info.get("direction", "")
        cur_sl = info.get("sl")
        entry = info.get("entry")

        # 回放模拟：到最后一个目标（TP3）时，交易所的止盈单会成交平仓、释放仓位槽。
        # 回放没有交易所，这里手动模拟，否则仓位永远占着槽 → 后续信号全被保证金拒绝。
        if self.sim_close and ev.level >= len(tp_all):
            try:
                self.ex.dry.remove(sym)
            except Exception:
                pass
            self.ex.pending.remove(sym)
            self.stats["close"] += 1
            log.info("🎯 %s 到达 TP%d（最后目标），模拟止盈平仓，释放仓位槽", sym, ev.level)
            return

        # 止损移到目标价「外侧」0.5%：空单在上方，多单在下方
        if direction == "short":
            new_sl = target * 1.005
            if cur_sl and new_sl >= cur_sl:
                log.info("%s TP%d 到达，新止损 %.8g 不优于当前 %.8g，保持不动",
                         sym, ev.level, new_sl, cur_sl)
                self.stats["skip"] += 1
                return
        else:
            new_sl = target * 0.995
            if cur_sl and new_sl <= cur_sl:
                log.info("%s TP%d 到达，新止损 %.8g 不优于当前 %.8g，保持不动",
                         sym, ev.level, new_sl, cur_sl)
                self.stats["skip"] += 1
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
                self.ex._critical(
                    f"⚠️ {sym} TP{ev.level} 价位异常\n"
                    f"   目标价 {target:.6g}，入场 {entry:.6g}\n"
                    f"   算出的止损 {new_sl:.6g} 超出 ±50%，已跳过\n"
                    f"   当前仍是旧止损 {cur_sl}",
                    key=f"trail_bad_{sym}")
                self.stats["ignore"] += 1
                return

        # 现价预检：TP 通知到达时价格可能尚未跌到目标、或已反弹穿越移损位。
        # 此时新止损会落在现价的错误一侧，币安必报 -2021 Order would immediately
        # trigger，而 amend 已经撤掉旧单 → 只能回滚 → 白烧权重且留裸仓窗口。
        # 真实现价不在目标一侧时，移损没有意义，直接跳过、保留现有保护。
        # dry-run/回放没有真实价格波动，跳过此检查（否则会误伤回放统计）。
        if not self.ex.dry_run:
            try:
                mark_now = self.ex.c.get_mark_price(sym)
                wrong_side = ((direction == "short" and new_sl <= mark_now) or
                              (direction != "short" and new_sl >= mark_now))
                if mark_now > 0 and wrong_side:
                    log.info("%s TP%d 新止损 %.8g 在现价 %.8g 的错误一侧，"
                             "价格未到目标/已反弹，跳过移损（保留当前止损 %.8g）",
                             sym, ev.level, new_sl, mark_now, cur_sl)
                    self.ex._critical(
                        f"⚠️ {sym} TP{ev.level} 移动止损已跳过\n"
                        f"   新止损 {new_sl:.8g} 落在现价 {mark_now:.8g} 的错误一侧，"
                        f"说明价格未到目标或已反弹\n"
                        f"   当前止损 {cur_sl} 保持不变，仓位仍有保护",
                        key=f"trail_side_{sym}")
                    self.stats["skip"] += 1
                    return
            except Exception as e:
                log.warning("%s TP%d 取现价失败，跳过本次移损: %s", sym, ev.level, e)
                self.stats["skip"] += 1
                return

        log.info("🎯 %s 到达 TP%d(%.8g)，止损上移到 %.8g，继续持有博 TP3(%.8g)",
                 sym, ev.level, target, new_sl, tp3)
        try:
            r = await asyncio.to_thread(
                self.ex.amend_protection, sym, tp3, new_sl, tp_all, True)
        except Exception as e:
            # 移动止损抛异常也要重试，否则止损永远停在旧位置
            log.exception("%s TP%d 移动止损异常: %s", sym, ev.level, e)
            self.stats["fail"] += 1
            self.buf.restore(sym, tp3, new_sl, tp_all, relaxed=True)
            self.ex._critical(
                f"⚠️ {sym} TP{ev.level} 移动止损异常：{e}\n"
                f"   已放回重试队列，当前止损仍在旧位置",
                key=f"trail_err_{sym}")
            return
        if r.ok:
            self.stats["trail"] += 1
        else:
            self.stats["fail"] += 1
            # 移动止损失败必须重试：否则止损永远停在旧位置，风险敞口一直扩大
            log.warning("%s TP%d 移动止损失败（%s），放入缓存等重试",
                        sym, ev.level, r.error)
            self.buf.restore(sym, tp3, new_sl, tp_all, relaxed=True)

    async def _handle_close(self, ev: sp.ParsedEvent):
        # 方向对不上就不平：频道的旧仓离场通知会滞后发出，
        # 若这期间我们已按新信号反手开了仓，平掉的就是好仓（误平仓）
        if ev.direction:
            cur = self._current_side(ev.symbol)
            if cur and cur != ev.direction:
                log.info("%s 离场信号方向 %s 与当前持仓 %s 不符，跳过（防误平新仓）",
                         ev.symbol, ev.direction.upper(), cur.upper())
                self.stats["skip"] += 1
                return
        log.info("📤 离场信号 %s%s", ev.symbol,
                 f"（{ev.direction.upper()}）" if ev.direction else "")
        r = await asyncio.to_thread(self.ex.close, ev.symbol, "tg-signal")
        if r.ok:
            self.stats["close"] += 1
        elif r.error and "无持仓" in r.error:
            # 仓位可能已被交易所条件单平掉，属正常情况
            log.info("%s 离场信号但无持仓（可能已被止盈止损单平掉）", ev.symbol)
            self.stats["skip"] += 1
        else:
            self.stats["fail"] += 1
            log.error("离场失败 %s: %s", ev.symbol, r.error)
            # 平仓失败 = 仓位可能还在，必须邮件告警
            self.ex._critical(
                f"❌ 离场失败 {ev.symbol}: {r.error}\n   仓位可能仍在，请手动检查",
                key=f"close_{ev.symbol}")

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
                        self.ex._critical(
                            f"⚠️ {sym} 止盈止损只收到一半（tp={part['tp']} sl={part['sl']}），"
                            f"超过 {self.cfg.tpsl_ttl_sec} 秒未补齐，请手动检查",
                            key=f"tpsl_half_{sym}")
                # 长时间未更新的仓位（每个 symbol 只告警一次，避免每 15 秒刷屏）
                for sym in self.ex.pending.stale(10):
                    if sym not in self._warned:
                        self._warned.add(sym)
                        self.ex._critical(
                            f"⚠️ {sym} 开仓后超过 10 分钟仍未收到真实止盈止损，"
                            f"当前只有兜底保护，请检查频道消息",
                            key=f"tpsl_stale_{sym}")
                # 重试之前失败的 amend（最多 3 次，超过就告警放弃）
                # 每轮最多处理 3 个符号：amend 一次 ≈ 14 权重，
                # 几十个符号同时重试会把 2400/分钟 打爆（418 的来源）
                _ready = list(self.buf.ready().items())
                if len(_ready) > 3:
                    log.warning("待重试改单 %d 个，本轮只处理 3 个（控制 API 权重）",
                                len(_ready))
                # 必须持锁：amend 是「撤旧保护单→挂新保护单」的非原子操作。
                # 以前巡检不持锁，与 on_message 的 TPSL 改单/TP移损在线程池并发，
                # 两个 cancel_all→挂单 交错会互相撤掉对方的止损 → 裸仓。
                async with self._lock:
                    for sym, data in _ready[:3]:
                        if sym not in self.ex.pending.all():
                            self.buf.drop(sym)
                            continue
                        if data.get("retry", 0) > 3:
                            self.buf.drop(sym)
                            self.ex._critical(
                                f"❌ {sym} 止盈止损改单连续失败 3 次，"
                                f"当前仍是兜底保护，请手动执行 amend",
                                key=f"amend_retry_{sym}")
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
                        all_pos = self.ex.c.get_positions()
                        live = {p["symbol"] for p in all_pos}
                        # 清理已恢复的：仓位进入 pending 或已平掉 → 移出去重集合，
                        # 这样同一币以后再出现无管理仓时还能告警。
                        self._unmanaged_warned &= (live - active)
                        # 反向对账：交易所有仓、pending 却没有（重启/崩溃窗口导致）。
                        # 这种仓没有任何程序管理，可能连保护单都没有 = 裸仓。
                        # 不擅自补挂（价位无法可靠推断），但必须告警让人介入。
                        for p in all_pos:
                            sym = p["symbol"]
                            if sym in active or sym in self._unmanaged_warned:
                                continue
                            self._unmanaged_warned.add(sym)
                            self.ex._critical(
                                f"🚨 {sym} 交易所有持仓但本地无 pending（重启/崩溃遗留）\n"
                                f"   {p['side']} {abs(p['amount'])} @ {p['entry']}\n"
                                f"   仓位无程序管理，请手动检查保护单或平仓："
                                f"python main.py positions",
                                key=f"unmanaged_{sym}")
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
                            # 多进程防重复记账：如果磁盘 pending 已被别的进程删掉
                            # （如用户 CLI 手动 close 平了这仓），说明那笔已经记过账，
                            # 这里绝不能再用 income 补记，否则连亏翻倍、熔断误触发。
                            if not self.ex.pending.on_disk(sym):
                                log.info("%s 的 pending 已被其它进程处理，跳过巡检补记", sym)
                                self.ex.pending.remove(sym)   # 同步掉内存残留
                                continue
                            log.info("仓位已了结（开仓 %.0f 秒后查无持仓），清理 pending: %s",
                                     age, sym)
                            # 关键：交易所止盈/止损单平掉的仓位不走 Executor.close，
                            # 以前只删 pending 不记盈亏 → 连亏/日亏熔断在这条
                            # 主要离场路径上完全失效，亏损会持续放大。
                            # 用币安 income（已实现盈亏+手续费+资金费）补记这笔净盈亏。
                            try:
                                created_ms = int(datetime.fromisoformat(
                                    info["created_at"]).timestamp() * 1000) - 5000
                            except Exception:
                                created_ms = int((time.time() - max(age, 0)) * 1000) - 5000
                            pnl = self.ex.c.get_realized_pnl(sym, created_ms)
                            bal = self.ex.c.get_balance()
                            # 只有查到真实流水才记（查不到时记 0 会污染胜率/连亏统计）
                            if pnl != 0.0:
                                self.ex.risk.record_trade(pnl, bal["total"])
                                self.ex._alert(
                                    f"📊 {sym} 被交易所止盈/止损平掉，已补记盈亏 "
                                    f"{pnl:+.2f}U（风控熔断统计已更新）")
                            else:
                                log.warning("%s 已了结但未查到已实现盈亏流水，"
                                            "本笔不记入熔断统计", sym)
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
    from tg_listener import SessionInvalidError, TgListener, check_ready

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
            print("   ⚠️ 如果它是另一个频道的登录态，千万不要复用 —— 同一个 auth key")
            print("      在两处使用，Telegram 会判定 AuthKeyDuplicated 并永久作废该 key")
            print(f"   → 请先跑：python tg_login.py（生成独立的 {cfg.tg_session}.session）")
        else:
            print("   目录里没有任何 .session 文件")
            print(f"   → 先跑一次：python tg_login.py（生成 {cfg.tg_session}.session）")
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
    except SessionInvalidError as e:
        # 不打印长栈：用户只需要知道「session 废了 + 怎么修」
        print()
        print("=" * 64)
        print("❌  Telegram 登录态失效，程序已停止（这段时间不会收到信号，也不会下单）")
        print("=" * 64)
        for line in str(e).splitlines():
            print("  " + line)
        print("=" * 64)
        raise SystemExit(2)
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
    print(f"完成：处理 {trader.stats['msg']} 条 → 开仓 {trader.stats['open']}，"
          f"改单 {trader.stats['amend']}，移动止损 {trader.stats['trail']}，"
          f"平仓 {trader.stats['close']}，忽略 {trader.stats['ignore']}，"
          f"跳过 {trader.stats['skip']}，失败 {trader.stats['fail']}")
    print("\n" + trader.ex.pending_report())
    await client.disconnect()


async def run_replay(cfg: Config, trader: Trader, path: str,
                     limit: Optional[int], delay: float, msg_interval: float = 60.0):
    """回放历史消息，验证解析 + 下单逻辑（不下真单，dry_run 有效）

    msg_interval: 虚拟时钟每条消息推进的秒数（默认 60）。
      回放文件没有时间戳，用固定间隔模拟「消息一条接一条到达」，
      避免把几天消息压缩到几秒、导致 30 秒冷却误触发。
    """
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

    # 离线回放的虚拟权益是 BinanceFutures._off_equity（默认 500U），
    # 不是 Executor.demo_equity（那个只在查询余额抛错时才用）
    _eq = getattr(trader.ex.c, "_off_equity", None) or trader.ex.demo_equity
    # 开启回放专用模拟：虚拟时钟 + TP3 自动平仓
    trader.sim_close = True
    trader._vclock_offset = 0.0
    span_min = len(blocks) * msg_interval / 60
    print(f"回放 {len(blocks)} 条消息（dry_run={cfg.dry_run}，"
          f"虚拟权益 {_eq:.0f}U，最多 {cfg.max_open_positions} 仓，"
          f"每仓 {cfg.margin_pct*100:.0f}% 保证金）")
    print(f"虚拟时钟：每条消息间隔 {msg_interval:.0f}s，"
          f"200 条约跨 {span_min:.0f} 分钟（避免冷却误触发）")
    print("模拟平仓：仓位到 TP3 视为止盈成交、释放仓位槽\n")
    msg_id = 0
    for b in blocks:
        msg_id += 1
        trader._vclock_offset += msg_interval     # 虚拟时钟前进
        ev = sp.parse(b)
        if ev.kind != sp.IGNORE:
            print(f"[{msg_id:03d}] {ev.kind:7s} {ev.symbol or '-':12s} "
                  f"{ev.direction or '-':6s} tp={ev.tp} sl={ev.sl}")
        await trader.on_message(b, msg_id)
        await asyncio.sleep(delay)

    print(f"\n完成：处理 {trader.stats['msg']} 条 → 开仓 {trader.stats['open']}，"
          f"改单 {trader.stats['amend']}，移动止损 {trader.stats['trail']}，"
          f"平仓 {trader.stats['close']}，忽略 {trader.stats['ignore']}，"
          f"跳过 {trader.stats['skip']}，失败 {trader.stats['fail']}")
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
    p.add_argument("--msg-interval", type=float, default=60.0,
                   help="虚拟时钟：每条消息推进的秒数（默认 60，避免冷却误触发）")
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
    # 告警通道状态：主网无人值守时邮件是最后一道防线，必须启动时就可见。
    # 以前漏配 SMTP 时 _critical 会静默跳过邮件，用户以为有告警其实一封都收不到。
    mail_ok = bool(cfg.smtp_server and cfg.smtp_user
                   and cfg.smtp_password and cfg.smtp_to)
    tg_ok = bool(cfg.tg_token and cfg.tg_chat_id)
    print(f"  告警:   邮件{'✅已配' if mail_ok else '❌未配置(收不到告警!)'} / "
          f"TG{'✅已配' if tg_ok else '❌未配置'}")
    print("=" * 64)

    if not cfg.testnet:
        print("\n⚠️  主网模式：所有下单都是真实资金，请确认配置无误")
        if not mail_ok:
            print("⚠️  警告：告警邮件未配置！裸仓/平仓失败时你不会收到任何邮件，"
                  "请补配 .env.bot 的 SMTP_*")
        if not cfg.has_active_keys:
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
            asyncio.run(run_replay(cfg, trader, args.replay, args.limit, args.delay,
                                   args.msg_interval))
        else:
            asyncio.run(run_live(cfg, trader))
    except KeyboardInterrupt:
        print("\n已退出")
        notifier.send("🛑 跟单程序已停止")


if __name__ == "__main__":
    main()
