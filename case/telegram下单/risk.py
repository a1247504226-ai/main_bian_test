"""
风控模块

职责：在真正下单之前，把不该发生的交易挡在门外。
风控失败一律抛 RiskRejected，调用方捕获后跳过该信号并告警。

两个维度同时管控：
  名义敞口  notional / equity       ← 决定盈亏幅度
  保证金占用 margin / equity        ← 决定能否再开仓、离强平多远
"""
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import date
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class RiskRejected(Exception):
    """风控拒单"""


@dataclass
class RiskLimits:
    max_leverage: int = 3                    # 杠杆上限
    max_position_pct: float = 1.00           # 单笔名义敞口上限（占权益）
    max_margin_pct: float = 0.50             # 单笔保证金占用上限（占权益）
    max_total_margin_pct: float = 0.95       # 全部持仓保证金合计上限
    max_open_positions: int = 2              # 最多同时持仓数
    max_symbol_exposure_pct: float = 1.00    # 单币种累计名义敞口上限
    daily_loss_limit_pct: float = 0.05       # 当日亏损熔断
    max_consecutive_losses: int = 3          # 连亏熔断
    min_order_notional: float = 5.0          # 币安合约最小名义
    allowed_symbols: Optional[List[str]] = None


@dataclass
class DailyStats:
    day: str = ""
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""


class RiskManager:
    def __init__(self, limits: RiskLimits, state_file: str = "state/daily_stats.json"):
        self.limits = limits
        self.state_file = state_file
        self.stats = self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> DailyStats:
        today = date.today().isoformat()
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                st = DailyStats(**{k: v for k, v in d.items()
                                   if k in DailyStats.__dataclass_fields__})
                if st.day != today:
                    log.info("跨日，重置日内统计")
                    return DailyStats(day=today)
                return st
            except Exception as e:
                log.warning("读取风控状态失败 %s，使用新状态", e)
        return DailyStats(day=today)

    def _save(self):
        d = os.path.dirname(self.state_file)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self.stats), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)

    # ---------------- 跨日 ----------------
    def _ensure_today(self):
        """长期运行时必须主动检查跨日，否则日亏/连亏会一直累积成永久熔断"""
        today = date.today().isoformat()
        if self.stats.day != today:
            log.info("跨日重置日内统计：%s → %s", self.stats.day or "-", today)
            self.stats = DailyStats(day=today)
            self._save()

    # ---------------- 记录 ----------------
    def record_trade(self, pnl: float, equity_after: float):
        self._ensure_today()
        self.stats.trades += 1
        self.stats.realized_pnl += pnl
        if pnl > 0:
            self.stats.wins += 1
            self.stats.consecutive_losses = 0
        elif pnl < 0:
            self.stats.losses += 1
            self.stats.consecutive_losses += 1
            self._check_halt(equity_after)
        self._save()

    def set_start_equity(self, equity: float):
        if self.stats.start_equity <= 0:
            self.stats.start_equity = equity
            self._save()

    def _check_halt(self, equity: float):
        lim = self.limits
        if self.stats.consecutive_losses >= lim.max_consecutive_losses:
            self.stats.halted = True
            self.stats.halt_reason = f"连亏 {self.stats.consecutive_losses} 笔"
        if self.stats.start_equity > 0:
            dd = (equity - self.stats.start_equity) / self.stats.start_equity
            if dd <= -lim.daily_loss_limit_pct:
                self.stats.halted = True
                self.stats.halt_reason = f"当日回撤 {dd*100:.1f}% 触及上限"
        if self.stats.halted:
            log.error("风控熔断：%s", self.stats.halt_reason)

    def resume(self):
        """人工解除熔断"""
        self.stats.halted = False
        self.stats.halt_reason = ""
        self.stats.consecutive_losses = 0
        self._save()

    # ---------------- 校验 ----------------
    def check_open(
        self,
        symbol: str,
        notional: float,
        margin: float,
        equity: float,
        leverage: int,
        open_positions: List[Dict],
    ):
        """开仓前校验。全部通过才返回，否则抛 RiskRejected"""
        self._ensure_today()
        lim = self.limits
        symbol = symbol.upper()

        if self.stats.halted:
            raise RiskRejected(f"已熔断：{self.stats.halt_reason}")

        if leverage > lim.max_leverage:
            raise RiskRejected(f"杠杆 {leverage}x 超过上限 {lim.max_leverage}x")

        if lim.allowed_symbols and symbol not in lim.allowed_symbols:
            raise RiskRejected(f"{symbol} 不在白名单内")

        if equity <= 0:
            raise RiskRejected("账户权益为 0，无法开仓")

        if notional < lim.min_order_notional:
            raise RiskRejected(
                f"名义价值 {notional:.2f}U 低于最小 {lim.min_order_notional}U")

        # 单笔名义敞口
        pct = notional / equity
        if pct > lim.max_position_pct + 1e-9:
            raise RiskRejected(
                f"单笔名义敞口 {pct*100:.1f}% 超过上限 {lim.max_position_pct*100:.0f}%")

        # 单笔保证金占用
        mpct = margin / equity
        if mpct > lim.max_margin_pct + 1e-9:
            raise RiskRejected(
                f"单笔保证金 {mpct*100:.1f}% 超过上限 {lim.max_margin_pct*100:.0f}%")

        # 持仓数量
        if len(open_positions) >= lim.max_open_positions:
            raise RiskRejected(
                f"持仓数 {len(open_positions)} 已达上限 {lim.max_open_positions}")

        if any(p["symbol"] == symbol for p in open_positions):
            raise RiskRejected(f"{symbol} 已有持仓，不重复开仓")

        # 单币种累计名义敞口（同 symbol 已在上面拦截，这里防同一标的的不同写法）
        same = sum(abs(p["amount"] * p["mark"]) for p in open_positions
                   if p["symbol"] == symbol)
        if (same + notional) / equity > lim.max_symbol_exposure_pct + 1e-9:
            raise RiskRejected(
                f"{symbol} 累计敞口超过 {lim.max_symbol_exposure_pct*100:.0f}%")

        # 总保证金占用 —— 这条最关键，防止开满仓后无法承受浮亏
        used_margin = 0.0
        for p in open_positions:
            lev = max(1, int(p.get("leverage", 1)))
            used_margin += abs(p["amount"] * p["mark"]) / lev
        total_margin_pct = (used_margin + margin) / equity
        if total_margin_pct > lim.max_total_margin_pct + 1e-9:
            raise RiskRejected(
                f"总保证金占用将达 {total_margin_pct*100:.1f}%，"
                f"超过上限 {lim.max_total_margin_pct*100:.0f}%"
                f"（已用 {used_margin/equity*100:.1f}%）")

    def summary(self) -> str:
        s = self.stats
        wr = (s.wins / s.trades * 100) if s.trades else 0.0
        return (f"今日 {s.trades} 笔 | 盈{s.wins} 亏{s.losses} | 胜率 {wr:.0f}% | "
                f"已实现 {s.realized_pnl:+.2f}U | 连亏 {s.consecutive_losses} | "
                f"{'熔断中: ' + s.halt_reason if s.halted else '正常'}")
