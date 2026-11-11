#!/usr/bin/env python3
"""
下单程序 CLI 入口

常用命令：
  python main.py check                          # 检查配置 / 连通性 / 白名单
  python main.py balance                        # 查余额
  python main.py positions                      # 查持仓
  python main.py open --symbol BTCUSDT --side long --entry 81000 --tp 83000 --sl 80500
  python main.py close --symbol BTCUSDT
  python main.py batch signals.json             # 批量执行信号文件
  python main.py serve                          # 常驻运行（心跳 + 监控）

安全默认：不加 --live 就是 dry-run，只打印不下单。
"""
import argparse
import json
import logging
import os
import signal as os_signal
import sys
import time

from binance_futures import BinanceError, BinanceFutures
from config import Config, load_config
from executor import Executor, Signal
from mailer import BalanceWatcher, Mailer
from notify import TelegramNotifier
from risk import RiskLimits, RiskManager
import health

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(cfg: Config, verbose: bool = False):
    os.makedirs(cfg.log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(cfg.log_dir, "tradebot.log"),
                                encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build(cfg: Config):
    """组装客户端 / 风控 / 执行器"""
    client = BinanceFutures(
        api_key=cfg.active_api_key,
        api_secret=cfg.active_api_secret,
        testnet=cfg.testnet,
    )
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
    mailer = Mailer(cfg.smtp_server, cfg.smtp_port, cfg.smtp_user,
                    cfg.smtp_password, cfg.smtp_to, cfg.smtp_max_retries)
    watcher = BalanceWatcher(
        mailer, threshold=cfg.balance_alert_threshold,
        step=cfg.balance_alert_step,
        state_file=os.path.join(cfg.state_dir, "balance_alert.json"))
    executor = Executor(client, risk, dry_run=cfg.dry_run, notify=notifier,
                        state_dir=cfg.state_dir, balance_watcher=watcher, mailer=mailer)

    # 主动同步服务器时间：本机时钟偏差 >recvWindow 会导致 -1021 签名失败
    if cfg.active_api_key:
        try:
            client._sync_time()
        except Exception as e:
            logging.getLogger(__name__).warning("时间同步失败（可忽略，后续会自动重试）: %s", e)

    return client, risk, executor, notifier


# ----------------------------------------------------------------------
def cmd_check(cfg: Config, client: BinanceFutures, notifier):
    print("=" * 60)
    print("配置检查")
    print("=" * 60)
    print(f"  环境:        {cfg.env_name}")
    print(f"  地址:        {cfg.base_url}")
    print(f"  模式:        {'DRY-RUN 只打印' if cfg.dry_run else 'LIVE 真实下单'}")
    print(f"  API Key:     {'已配置 ' + cfg.active_api_key[:8] + '...' if cfg.active_api_key else '未配置'}")
    print(f"  Telegram:    {'已配置' if notifier.enabled else '未配置'}")
    print(f"  杠杆:        {cfg.leverage}x (上限 {cfg.max_leverage}x)")
    print(f"  单笔保证金:  {cfg.margin_pct*100:.0f}% 权益 → 名义 {cfg.margin_pct*cfg.leverage*100:.0f}%")
    print(f"  兜底保护:    SL {cfg.provisional_sl_pct}% / TP {cfg.provisional_tp_pct}%")
    print(f"  最大持仓数:  {cfg.max_open_positions} 个")
    print(f"  总保证金上限:{cfg.max_total_margin_pct*100:.0f}%")
    print(f"  日亏熔断:    {cfg.daily_loss_limit_pct*100:.0f}%")
    print(f"  交易对白名单: {cfg.allowed_symbols or '不限制'}")

    print("\n连通性:")
    print(f"  API ping:    {'OK' if client.ping() else 'FAIL'}")

    if not cfg.active_api_key:
        print("\n未配置 API Key，跳过账户检查")
        return

    try:
        client._sync_time()
        bal = client.get_balance()
        print(f"  账户余额:    {bal['total']:.2f} USDT (可用 {bal['available']:.2f})")
        pos = client.get_positions()
        print(f"  当前持仓:    {len(pos)} 个")
        for p in pos:
            print(f"    {p['symbol']} {p['side']} {p['amount']} @ {p['entry']:.6g} "
                  f"盈亏 {p['unrealized']:+.2f}U")
    except BinanceError as e:
        print(f"  账户查询失败: {e}")
        if e.code in (-2015, -1022):
            print("  → 大概率是 IP 不在白名单，或 API Key 权限/签名有问题")
    print()


def cmd_open(cfg: Config, executor: Executor, args):
    sig = Signal(
        symbol=args.symbol,
        direction=args.side,
        entry=args.entry,
        tp1=args.tp,
        sl=args.sl,
        leverage=args.leverage if args.leverage is not None else cfg.leverage,
        margin_pct=args.margin_pct if args.margin_pct is not None else cfg.margin_pct,
        position_pct=args.pct if args.pct is not None else 0.10,
        signal_id=args.id or "",
        provisional_sl_pct=(args.provisional_sl if args.provisional_sl is not None
                            else cfg.provisional_sl_pct),
        provisional_tp_pct=(args.provisional_tp if args.provisional_tp is not None
                            else cfg.provisional_tp_pct),
    )
    r = executor.open(sig)
    print(json.dumps(r.__dict__, ensure_ascii=False, indent=2))
    if r.provisional:
        print("\n提示：当前只有兜底保护，收到真实止盈止损后执行：")
        print(f"  python main.py amend --symbol {r.symbol} --tp <止盈价> --sl <止损价>")
    return 0 if r.ok else 1


def cmd_amend(cfg: Config, executor: Executor, args):
    r = executor.amend_protection(args.symbol, tp_price=args.tp, sl_price=args.sl)
    print(json.dumps(r.__dict__, ensure_ascii=False, indent=2))
    return 0 if r.ok else 1


def cmd_pending(cfg: Config, executor: Executor, args):
    print(executor.pending_report(stale_minutes=args.stale_minutes))
    return 0


def cmd_close(cfg: Config, executor: Executor, args):
    r = executor.close(args.symbol, reason=args.reason)
    print(json.dumps(r.__dict__, ensure_ascii=False, indent=2))
    return 0 if r.ok else 1


def cmd_balance(cfg: Config, client: BinanceFutures):
    bal = client.get_balance()
    print(f"总权益: {bal['total']:.2f} USDT")
    print(f"可用:   {bal['available']:.2f} USDT")
    print(f"未实现: {bal['unrealized']:+.2f} USDT")


def cmd_positions(cfg: Config, client: BinanceFutures):
    pos = client.get_positions()
    if not pos:
        print("无持仓")
        return
    print(f"{'合约':12s} {'方向':6s} {'数量':>14s} {'开仓价':>14s} {'标记价':>14s} {'盈亏':>12s}")
    for p in pos:
        print(f"{p['symbol']:12s} {p['side']:6s} {p['amount']:>14.6g} "
              f"{p['entry']:>14.6g} {p['mark']:>14.6g} {p['unrealized']:>+12.2f}")


def cmd_batch(cfg: Config, executor: Executor, args):
    """批量执行信号文件，格式见 README"""
    with open(args.file, "r", encoding="utf-8") as f:
        items = json.load(f)
    if isinstance(items, dict):
        items = [items]

    ok = fail = 0
    for it in items:
        # 单个信号可以覆盖杠杆/仓位，否则用全局配置
        sig = Signal(
            symbol=it["symbol"],
            direction=it.get("direction", it.get("side", "long")),
            entry=it.get("entry"),
            tp1=it.get("tp1", it.get("tp")),
            sl=it.get("sl"),
            leverage=it.get("leverage", cfg.leverage),
            margin_pct=it.get("margin_pct", cfg.margin_pct),
            signal_id=it.get("signal_id", ""),
            provisional_sl_pct=it.get("provisional_sl_pct", cfg.provisional_sl_pct),
            provisional_tp_pct=it.get("provisional_tp_pct", cfg.provisional_tp_pct),
        )
        r = executor.open(sig)
        if r.ok:
            ok += 1
        else:
            fail += 1
        tag = "OK(兜底)" if r.provisional else ("OK" if r.ok else f"FAIL: {r.error}")
        print(f"  {sig.symbol:12s} {sig.direction:5s} -> {tag}")
        time.sleep(0.5)  # 避免触发限频
    print(f"\n完成: {ok} 成功, {fail} 失败")
    pending = executor.pending.all()
    if pending:
        print(f"\n有 {len(pending)} 个仓位只有兜底保护，等真实止盈止损到了执行：")
        for sym in pending:
            print(f"  python main.py amend --symbol {sym} --tp <止盈价> --sl <止损价>")
    return 0 if fail == 0 else 1


def cmd_serve(cfg: Config, client: BinanceFutures, risk: RiskManager, notifier):
    """常驻模式：心跳 + 定期健康检查"""
    health.start(cfg.health_port)
    notifier.send(
        f"🤖 交易机器人已启动\n"
        f"环境: {'TESTNET' if cfg.testnet else '主网'} | "
        f"模式: {'DRY-RUN' if cfg.dry_run else 'LIVE'}\n"
        f"{risk.summary()}"
    )

    running = {"v": True}

    def _stop(signum, frame):
        running["v"] = False
        notifier.send("🛑 收到停止信号，正在退出")
        print("\n正在退出...")

    os_signal.signal(os_signal.SIGINT, _stop)
    os_signal.signal(os_signal.SIGTERM, _stop)

    last_check = 0
    while running["v"]:
        now = time.time()
        health.beat(risk=risk.summary())
        # 每 60 秒做一次连通性自检
        if now - last_check > 60:
            ok = client.ping()
            if not ok:
                notifier.send("⚠️ API 连通性异常，策略可能无法下单")
            health.beat(api_ok=ok)
            last_check = now
        time.sleep(5)

    print("已退出")


# ----------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="币安合约下单程序（两阶段保护 + 滚仓）")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--live", action="store_true",
                   help="真实下单（不加此参数为 dry-run）")
    p.add_argument("--testnet", action="store_true",
                   help="用币安测试网（默认，虚拟资金）")
    p.add_argument("--mainnet", action="store_true",
                   help="切币安主网（真实资金，需配主网 API Key）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="检查配置与连通性")

    sub.add_parser("balance", help="查余额")
    sub.add_parser("positions", help="查持仓")

    po = sub.add_parser("open", help="开仓（先挂 7% 兜底止盈止损，之后 amend）")
    po.add_argument("--symbol", required=True)
    po.add_argument("--side", required=True, choices=["long", "short"])
    po.add_argument("--entry", type=float, default=None)
    po.add_argument("--tp", type=float, default=None,
                    help="止盈价（可省略，省略则走兜底模式）")
    po.add_argument("--sl", type=float, default=None,
                    help="止损价（可省略，省略则走兜底模式）")
    po.add_argument("--leverage", type=int, default=None,
                    help="杠杆，默认取 config.yaml 的 leverage")
    po.add_argument("--margin-pct", type=float, default=None,
                    help="保证金占权益比例，默认取 config.yaml 的 margin_pct")
    po.add_argument("--pct", type=float, default=None,
                    help="名义占权益比例（旧口径，给 margin-pct 时忽略）")
    po.add_argument("--id", default="", help="信号唯一ID，用于幂等")
    po.add_argument("--provisional-sl", type=float, default=None,
                    help="兜底止损百分比，默认取 config.yaml（7）")
    po.add_argument("--provisional-tp", type=float, default=None,
                    help="兜底止盈百分比，默认取 config.yaml（7）")

    pa = sub.add_parser("amend", help="收到真实止盈止损后改单")
    pa.add_argument("--symbol", required=True)
    pa.add_argument("--tp", type=float, default=None, help="止盈价")
    pa.add_argument("--sl", type=float, default=None, help="止损价")

    pp = sub.add_parser("pending", help="查看还在等真实止盈止损的仓位")
    pp.add_argument("--stale-minutes", type=int, default=10)

    pc = sub.add_parser("close", help="平仓")
    pc.add_argument("--symbol", required=True)
    pc.add_argument("--reason", default="manual")

    pb = sub.add_parser("batch", help="批量执行信号文件")
    pb.add_argument("file")

    sub.add_parser("serve", help="常驻运行")

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

    if not cfg.testnet:
        print(f"⚠️  主网模式（{cfg.base_url}）：真实资金")
        if not cfg.mainnet_api_key:
            print(f"❌ {cfg.missing_key_hint()}")
            sys.exit(1)

    client, risk, executor, notifier = build(cfg)

    if args.cmd == "check":
        cmd_check(cfg, client, notifier)
    elif args.cmd == "balance":
        cmd_balance(cfg, client)
    elif args.cmd == "positions":
        cmd_positions(cfg, client)
    elif args.cmd == "open":
        sys.exit(cmd_open(cfg, executor, args))
    elif args.cmd == "amend":
        sys.exit(cmd_amend(cfg, executor, args))
    elif args.cmd == "pending":
        sys.exit(cmd_pending(cfg, executor, args))
    elif args.cmd == "close":
        sys.exit(cmd_close(cfg, executor, args))
    elif args.cmd == "batch":
        sys.exit(cmd_batch(cfg, executor, args))
    elif args.cmd == "serve":
        cmd_serve(cfg, client, risk, notifier)


if __name__ == "__main__":
    main()
