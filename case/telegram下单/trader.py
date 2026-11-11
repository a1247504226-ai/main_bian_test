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
from typing import Optional

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
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(cfg.log_dir, "trader.log"),
                                encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def build(cfg: Config):
    client = BinanceFutures(cfg.active_api_key, cfg.active_api_secret, testnet=cfg.testnet)
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
    if cfg.active_api_key:
        try:
            client._sync_time()
        except Exception as e:
            log.warning("时间同步失败（后续会自动重试）: %s", e)
    return client, risk, executor, notifier


class Trader:
    def __init__(self, cfg: Config, executor: Executor, notifier):
        self.cfg = cfg
        self.ex = executor
        self.notify = notifier
        self.buf = sp.TPSLBuffer(ttl_sec=cfg.tpsl_ttl_sec)
        self.stats = {"open": 0, "amend": 0, "trail": 0, "ignore": 0, "fail": 0}
        self._warned: set = set()   # 已告警过的 symbol，避免重复刷屏
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

    # ---------------- 消息处理 ----------------
    async def on_message(self, text: str, msg_id: int):
        async with self._lock:      # 串行化，杜绝并发下单
            ev = sp.parse(text)

            if ev.kind == sp.OPEN:
                await self._handle_open(ev)
            elif ev.kind == sp.TPSL:
                await self._handle_tpsl(ev)
            elif ev.kind == sp.TP_HIT:
                await self._handle_tp_hit(ev)
            elif ev.kind == sp.CLOSE:
                await self._handle_close(ev)
            else:
                self.stats["ignore"] += 1
                log.debug("忽略：%s", text[:50])

    async def _handle_open(self, ev: sp.ParsedEvent):
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

        log.info("📥 开仓信号 %s %s", sym, ev.direction)
        sig = Signal(
            symbol=sym,
            direction=ev.direction,
            entry=ev.entry,
            tp1=ev.tp,
            sl=ev.sl,
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
            # 失败时把数据放回缓存，housekeeping 会定时重试，
            # 否则频道不重发就永远卡在兜底止损上
            self.buf.restore(target, done["tp"], done["sl"], done.get("tp_all"))

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

        log.info("🎯 %s 到达 TP%d(%.8g)，止损上移到 %.8g，继续持有博 TP3(%.8g)",
                 sym, ev.level, target, new_sl, tp3)
        r = await asyncio.to_thread(
            self.ex.amend_protection, sym, tp3, new_sl, tp_all, True)
        if r.ok:
            self.stats["trail"] += 1
        else:
            self.stats["fail"] += 1

    async def _handle_close(self, ev: sp.ParsedEvent):
        log.info("📤 离场信号 %s", ev.symbol)
        await asyncio.to_thread(self.ex.close, ev.symbol, "tg-signal")

    # ---------------- 定时巡检 ----------------
    async def housekeeping(self):
        while True:
            await asyncio.sleep(15)
            # 整个循环体包在 try 里：一次异常不能把巡检任务搞死
            try:
                now = time.time()
                # 缓存超时的止盈止损
                for sym, part in self.buf.flush_expired(now):
                    if sym not in self._warned:
                        self._warned.add(sym)
                        self.notify(
                            f"⚠️ {sym} 止盈止损只收到一半（tp={part['tp']} sl={part['sl']}），"
                            f"超过 {self.cfg.tpsl_ttl_sec} 秒未补齐，请手动检查")
                # 长时间未更新的仓位（每个 symbol 只告警一次，避免每 15 秒刷屏）
                for sym in self.ex.pending.stale(10):
                    if sym not in self._warned:
                        self._warned.add(sym)
                        self.notify(f"⚠️ {sym} 开仓后超过 10 分钟仍未收到真实止盈止损，"
                                    f"当前只有兜底保护，请检查频道消息")
                # 重试之前失败的 amend（最多 3 次，超过就告警放弃）
                for sym, data in self.buf.ready().items():
                    if sym not in self.ex.pending.all():
                        self.buf.drop(sym)
                        continue
                    if data.get("retry", 0) > 3:
                        self.buf.drop(sym)
                        self.notify(f"❌ {sym} 止盈止损改单连续失败 3 次，"
                                    f"当前仍是兜底保护，请手动执行 amend")
                        continue
                    log.info("重试 amend %s (第 %d 次)", sym, data.get("retry", 0))
                    r = await asyncio.to_thread(
                        self.ex.amend_protection, sym, data["tp"], data["sl"])
                    if r.ok:
                        self.buf.drop(sym)
                        self.stats["amend"] += 1
                    else:
                        self.buf.restore(sym, data["tp"], data["sl"])

                # 已解决的移出告警集合
                active = set(self.ex.pending.all())
                self._warned &= active

                # 仓位已经被止损/止盈单平掉的，pending 记录要清掉，
                # 否则会一直误报「超过 10 分钟未更新」
                if not self.ex.dry_run:
                    try:
                        live = {p["symbol"] for p in self.ex.c.get_positions()}
                        for sym in list(active):
                            if sym not in live:
                                log.info("仓位已了结，清理 pending: %s", sym)
                                self.ex.pending.remove(sym)
                    except Exception:
                        pass

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

    listener = TgListener(
        api_id=cfg.tg_api_id,
        api_hash=cfg.tg_api_hash,
        phone=cfg.tg_phone,
        channel=cfg.tg_channel,
        session_path=cfg.tg_session,
        dedup_file=os.path.join(cfg.state_dir, "seen_msgs.json"),
    )
    health.start(cfg.health_port)
    # 保存引用：create_task 的返回值若没人持有，可能被 GC 回收导致巡检停止
    hk_task = asyncio.create_task(trader.housekeeping())
    try:
        await listener.run(trader.on_message)
    finally:
        hk_task.cancel()


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
          f"忽略 {trader.stats['ignore']}，失败 {trader.stats['fail']}")
    print("\n" + trader.ex.pending_report())


# ----------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="TG 频道自动跟单")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--live", action="store_true", help="真实下单")
    p.add_argument("--replay", metavar="FILE", help="回放历史消息文件")
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

    client, risk, executor, notifier = build(cfg)
    trader = Trader(cfg, executor, notifier)

    try:
        if args.replay:
            asyncio.run(run_replay(cfg, trader, args.replay, args.limit, args.delay))
        else:
            asyncio.run(run_live(cfg, trader))
    except KeyboardInterrupt:
        print("\n已退出")
        notifier.send("🛑 跟单程序已停止")


if __name__ == "__main__":
    main()
