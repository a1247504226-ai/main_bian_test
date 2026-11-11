"""
下单执行器

两阶段保护流程（对应频道「先发方向、1-2 分钟后再发止盈止损」的节奏）：

  阶段 1  收到「现价空 #XXX」→ 立即市价开仓
          同时挂 7% 止盈 + 7% 止损（兜底，双向都有保护）
  阶段 2  收到「止盈：xxx 止损：xxx」→ 撤掉兜底单，挂真实止盈止损

仓位口径（两种，二选一）：
  margin_pct   保证金占权益比例   ← 推荐，直观："这笔拿 50% 的钱做保证金"
  position_pct 名义价值占权益比例 ← 兼容旧用法
  实际名义 = margin_pct × equity × leverage

核心保证：
1. 幂等 —— 同一个信号重复调用不会重复开仓（client_order_id 由信号内容派生）
2. 任何时刻都有交易所条件单在保护仓位，不留裸仓
3. 滚仓仓位基于「账户实际权益」计算，不依赖内存变量，进程重启不跑偏
"""
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from binance_futures import BinanceError, BinanceFutures
from risk import RiskManager, RiskRejected

log = logging.getLogger(__name__)


@dataclass
class Signal:
    """一个交易信号

    tp1 / sl 都为空 → 走「兜底模式」：挂 provisional_sl_pct / provisional_tp_pct
    任意一个缺失   → 也走兜底模式（缺的那个用兜底百分比补）
    """
    symbol: str
    direction: str                      # long / short
    entry: Optional[float] = None       # 参考入场价（None = 用标记价）
    tp1: Optional[float] = None         # 止盈价（可后补）
    sl: Optional[float] = None          # 止损价（可后补）
    leverage: int = 2
    margin_pct: Optional[float] = None  # 保证金占权益（优先使用）
    position_pct: float = 0.10          # 名义占权益（margin_pct 为空时用）
    signal_id: str = ""
    # 兜底保护（双向）
    provisional_sl_pct: float = 5.0
    provisional_tp_pct: float = 7.0

    @property
    def has_real_protection(self) -> bool:
        return self.tp1 is not None and self.sl is not None


@dataclass
class OrderResult:
    ok: bool
    symbol: str
    direction: str
    qty: float = 0.0
    avg_price: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    sl_order_id: str = ""
    tp_order_id: str = ""
    error: str = ""
    dry_run: bool = False
    provisional: bool = False


def _coid(prefix: str, sig: Signal) -> str:
    """生成幂等 clientOrderId。币安要求 <36 字符，只允许 .A-Za-z0-9_-"""
    seed = f"{sig.symbol}|{sig.direction}|{sig.signal_id or sig.entry}|{sig.sl}|{sig.tp1}"
    h = hashlib.md5(seed.encode()).hexdigest()[:20]
    return f"{prefix}{h}"


class PendingStore:
    """记录「只有兜底保护、等待真实止盈止损」的仓位，进程重启后可查"""

    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                log.warning("读取 pending 状态失败: %s", e)

    def _save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, symbol: str, info: Dict[str, Any]):
        self._data[symbol.upper()] = {
            **info,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save()

    def remove(self, symbol: str):
        if self._data.pop(symbol.upper(), None) is not None:
            self._save()

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._data.get(symbol.upper())

    def all(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._data)

    def provisional(self) -> Dict[str, Dict[str, Any]]:
        """还在等真实止盈止损的（只有兜底保护）"""
        return {k: v for k, v in self._data.items()
                if v.get("status", "provisional") == "provisional"}

    def protected(self) -> Dict[str, Dict[str, Any]]:
        """已挂真实止盈止损、正在跟踪 TP1/TP2 的"""
        return {k: v for k, v in self._data.items()
                if v.get("status") == "protected"}

    def stale(self, minutes: int = 10) -> List[str]:
        out = []
        now = datetime.now()
        for sym, info in self._data.items():
            if info.get("status", "provisional") != "provisional":
                continue
            try:
                t = datetime.fromisoformat(info["created_at"])
                if (now - t).total_seconds() > minutes * 60:
                    out.append(sym)
            except Exception:
                out.append(sym)
        return out


class DryPositionStore:
    """dry-run 下的虚拟持仓，持久化到文件

    否则每次 python main.py 都是新进程，风控永远看到 0 个持仓，
    无法验证「能不能开第二个仓」。
    """

    def __init__(self, path: str):
        self.path = path
        self._data: List[Dict[str, Any]] = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                log.warning("读取 dry 持仓失败: %s", e)

    def _save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def all(self) -> List[Dict[str, Any]]:
        return list(self._data)

    def add(self, pos: Dict[str, Any]):
        self._data = [p for p in self._data if p.get("symbol") != pos.get("symbol")]
        self._data.append(pos)
        self._save()

    def remove(self, symbol: str):
        before = len(self._data)
        self._data = [p for p in self._data if p.get("symbol") != symbol.upper()]
        if len(self._data) != before:
            self._save()

    def clear(self):
        self._data = []
        self._save()


class Executor:
    def __init__(
        self,
        client: BinanceFutures,
        risk: RiskManager,
        dry_run: bool = True,
        notify=None,
        demo_equity: float = 1000.0,
        state_dir: str = "state",
        strict_price_check: bool = True,
        balance_watcher=None,
        mailer=None,
    ):
        self.c = client
        self.risk = risk
        self.dry_run = dry_run
        self.notify = notify
        self.demo_equity = demo_equity
        # replay 历史消息时关掉价位校验（历史价位与当前价必然不匹配）
        self.strict_price_check = strict_price_check
        self.balance_watcher = balance_watcher
        self.mailer = mailer
        self.pending = PendingStore(os.path.join(state_dir, "pending_protection.json"))
        self.dry = DryPositionStore(os.path.join(state_dir, "dry_positions.json"))

    # ------------------------------------------------------------------
    def _alert(self, text: str):
        log.info(text)
        if self.notify:
            try:
                self.notify(text)
            except Exception as e:
                log.warning("告警发送失败: %s", e)

    def _notify_open(self, sym, direction, qty, avg, notional, margin,
                     sl, tp, lev, provisional):
        """开仓成功后推一封邮件，手机上能第一时间看到"""
        if not self.mailer:
            return
        tag = "兜底 7%（等真实止盈止损）" if provisional else "完整止盈止损"
        body = (
            f"币种：{sym}\n"
            f"方向：{direction.upper()}\n"
            f"成交价：{avg:.6g}\n"
            f"数量：{qty}\n"
            f"名义价值：{notional:.2f} USDT\n"
            f"保证金：{margin:.2f} USDT（{lev}x）\n"
            f"止损价：{sl:.6g}\n"
            f"止盈价：{tp:.6g}\n"
            f"保护类型：{tag}\n\n"
            f"—— 交易机器人自动通知"
        )
        try:
            self.mailer.send(f"【开仓】{sym} {direction.upper()}", body)
        except Exception as e:
            log.warning("开仓邮件发送失败: %s", e)

    def _check_balance(self, context: str = ""):
        """交易后检查余额，跨档发邮件。失败不影响主流程"""
        if self.dry_run or not self.balance_watcher:
            return
        try:
            bal = self.c.get_balance()
            self.balance_watcher.check(bal["total"], context)
        except Exception as e:
            log.warning("余额检查失败: %s", e)

    # ------------------------------------------------------------------
    def _calc_size(self, equity: float, sig: Signal) -> tuple:
        """返回 (notional, margin)。margin_pct 优先于 position_pct"""
        lev = max(1, sig.leverage)
        if sig.margin_pct is not None:
            margin = equity * sig.margin_pct
            return margin * lev, margin
        notional = equity * sig.position_pct
        return notional, notional / lev

    # ------------------------------------------------------------------
    def open(self, sig: Signal) -> OrderResult:
        sym = sig.symbol.upper()
        direction = sig.direction.lower()
        side = "BUY" if direction == "long" else "SELL"
        pos_side = "LONG" if direction == "long" else "SHORT"
        provisional = not sig.has_real_protection

        try:
            # ---------- 0. 币种存在性 ----------
            # 必须在最前面拦：拼错的币种（BTCUSDCUSDT / 不存在的山寨）
            # 如果不提前拒绝，会走到后面用 fallback 价格继续算，容易出怪问题
            try:
                self.c.get_filters(sym)
            except BinanceError as e:
                return OrderResult(False, sym, direction,
                                   error=f"{sym} 不在币安 USDT 本位合约市场: {e}")

            # ---------- 1. 权益 / 仓位 ----------
            try:
                bal = self.c.get_balance()
                equity = bal["total"]
                positions = self.c.get_positions()
            except BinanceError:
                if not self.dry_run:
                    raise
                equity = self.demo_equity
                positions = []
                log.warning("dry-run 无法查询账户，使用虚拟权益 %.2fU", equity)

            # dry-run 时把之前虚拟开的仓也算进风控，才能验证多仓限制
            if self.dry_run:
                positions = positions + self.dry.all()

            self.risk.set_start_equity(equity)

            # 数量必须用「当前市价」算，不能用信号里的历史入场价：
            # 信号从发出到执行可能隔几分钟，价格早变了，用历史价算会导致实际名义偏离预算
            try:
                mark = self.c.get_mark_price(sym)
            except Exception as e:
                if sig.entry:
                    mark = sig.entry
                    log.warning("取市价失败(%s)，退回用信号入场价 %s 估算", e, mark)
                else:
                    return OrderResult(False, sym, direction,
                                       error=f"无法获取 {sym} 市价: {e}")

            ref_price = sig.entry or mark        # 仅用于日志和信号合理性校验
            notional, margin = self._calc_size(equity, sig)
            qty = self.c.round_qty(sym, notional / mark)
            est_notional = qty * mark            # 风控用这个，与实际成交口径一致

            # 数量为 0 要在风控前拦掉，否则风控会报「名义价值 0 低于最小」，
            # 提示不准确（真实原因是数量取整后低于该交易对最小下单量）
            if qty <= 0:
                return OrderResult(False, sym, direction,
                                   error=f"下单数量取整后为 0（预算名义 {notional:.2f}U，"
                                         f"价格 {mark:.6g}，低于该交易对最小下单量）")

            # ---------- 2. 风控 ----------
            self.risk.check_open(
                symbol=sym,
                notional=est_notional,
                margin=margin,
                equity=equity,
                leverage=sig.leverage,
                open_positions=positions,
            )

            # ---------- 3. 真实值方向校验 ----------
            if sig.has_real_protection and sig.entry:
                if direction == "long" and not (sig.sl < sig.entry < sig.tp1):
                    return OrderResult(False, sym, direction,
                                       error=f"多单价位不合理 entry={sig.entry} sl={sig.sl} tp={sig.tp1}")
                if direction == "short" and not (sig.tp1 < sig.entry < sig.sl):
                    return OrderResult(False, sym, direction,
                                       error=f"空单价位不合理 entry={sig.entry} sl={sig.sl} tp={sig.tp1}")

            # ---------- 4. 计算保护价位 ----------
            slp = sig.provisional_sl_pct / 100
            tpp = sig.provisional_tp_pct / 100

            def calc_prov(base: float):
                """兜底价位必须基于「实际成交价」算，否则 7% 会变成 5% 或 14%"""
                if not provisional:
                    return sig.sl, sig.tp1
                s = base * (1 + slp) if direction == "short" else base * (1 - slp)
                t = base * (1 - tpp) if direction == "short" else base * (1 + tpp)
                return s, t

            prov_sl, prov_tp = calc_prov(mark)   # dry-run 展示用，实盘会在成交后重算

            coid_open = _coid("OP", sig)
            coid_sl = _coid("PS" if provisional else "SL", sig)
            coid_tp = _coid("PT" if provisional else "TP", sig)

            # ---------- 5. dry_run ----------
            if self.dry_run:
                tag = "兜底" if provisional else "完整"
                self._alert(
                    f"[DRY-RUN][{tag}] {sym} {direction.upper()} {qty} @~{ref_price:.6g} "
                    f"名义 {qty*ref_price:.2f}U 保证金 {margin:.2f}U ({sig.leverage}x)\n"
                    f"   SL {prov_sl:.6g} | TP {prov_tp:.6g} | coid={coid_open}"
                )
                if provisional:
                    self.pending.add(sym, {
                        "direction": direction, "entry": ref_price, "qty": qty,
                        "provisional_sl": prov_sl, "provisional_tp": prov_tp,
                        "leverage": sig.leverage, "dry_run": True,
                    })
                # 记录虚拟持仓，后续 dry-run 的风控才能看到它
                self.dry.add({
                    "symbol": sym,
                    "amount": qty if direction == "long" else -qty,
                    "side": pos_side,
                    "entry": ref_price,
                    "mark": ref_price,
                    "leverage": sig.leverage,
                })
                return OrderResult(True, sym, direction, qty, ref_price,
                                   qty * ref_price, margin, dry_run=True,
                                   provisional=provisional)

            # ---------- 6. 下单 ----------
            self.c.set_margin_type(sym, isolated=True)
            self.c.set_leverage(sym, sig.leverage)

            order = self.c.market_order(sym, side, qty, coid_open)
            avg = float(order.get("avgPrice") or ref_price)
            filled = float(order.get("executedQty") or qty)
            actual_notional = avg * filled
            actual_margin = actual_notional / max(1, sig.leverage)
            log.info("开仓成功 %s %s qty=%s avg=%s", sym, side, filled, avg)

            # 用实际成交价重算兜底价位，保证 7% 就是相对成交价的 7%
            if provisional:
                prov_sl, prov_tp = calc_prov(avg)
                log.info("兜底价位按成交价 %s 重算：SL %s / TP %s", avg, prov_sl, prov_tp)

            # ---------- 7. 挂保护单（双向）----------
            sl_id = tp_id = ""
            try:
                # 价位有效性检查：避免挂上就立即触发
                mark = self.c.get_mark_price(sym)
                if direction == "short":
                    sl_ok = prov_sl > mark
                    tp_ok = prov_tp < mark
                else:
                    sl_ok = prov_sl < mark
                    tp_ok = prov_tp > mark

                if not sl_ok:
                    self._alert(
                        f"⚠️ {sym} 止损价 {prov_sl:.6g} 已在当前价 {mark:.6g} 的错误一侧，"
                        f"跳过挂单（价格已穿越止损位，请立即人工确认）"
                    )
                else:
                    r = self.c.stop_loss(sym, pos_side, prov_sl, coid_sl)
                    sl_id = str(r.get("orderId", ""))

                if not tp_ok:
                    log.info("%s 止盈价 %s 已在当前价 %s 错误一侧，跳过", sym, prov_tp, mark)
                else:
                    r = self.c.take_profit(sym, pos_side, prov_tp, coid_tp)
                    tp_id = str(r.get("orderId", ""))

                if not sl_id:
                    # 没有止损保护 = 裸仓，必须平掉
                    raise BinanceError(-1, "止损单未能挂上，拒绝留裸仓")

            except BinanceError as e:
                log.error("保护单挂单失败 %s，紧急平仓", e)
                self._alert(f"[紧急] {sym} 保护单失败 {e}，正在平仓")
                try:
                    self.c.cancel_all(sym)
                    self.c.market_order(sym, "SELL" if side == "BUY" else "BUY",
                                        filled, _coid("EM", sig), reduce_only=True)
                    self._alert(f"[已平仓] {sym} 因保护单失败已紧急平仓")
                except Exception as e2:
                    self._alert(f"[严重] {sym} 紧急平仓也失败: {e2}，请立刻手动处理！")
                return OrderResult(False, sym, direction, filled, avg,
                                   actual_notional, actual_margin,
                                   error=f"保护单失败: {e}")

            # ---------- 8. 记录 ----------
            if provisional:
                self.pending.add(sym, {
                    "status": "provisional",
                    "direction": direction, "entry": avg, "qty": filled,
                    "provisional_sl": prov_sl, "provisional_tp": prov_tp,
                    "leverage": sig.leverage,
                    "sl_order_id": sl_id, "tp_order_id": tp_id,
                })
                self._alert(
                    f"🟡 开仓 {sym} {direction.upper()} {filled} @ {avg:.6g}\n"
                    f"   名义 {actual_notional:.2f}U | 保证金 {actual_margin:.2f}U ({sig.leverage}x)\n"
                    f"   兜底 SL {prov_sl:.6g} / TP {prov_tp:.6g}（±{sig.provisional_sl_pct}%）\n"
                    f"   ⏳ 等待真实止盈止损，收到后执行 amend"
                )
            else:
                self._alert(
                    f"✅ 开仓 {sym} {direction.upper()} {filled} @ {avg:.6g}\n"
                    f"   名义 {actual_notional:.2f}U | 保证金 {actual_margin:.2f}U ({sig.leverage}x)\n"
                    f"   SL {prov_sl:.6g} (id {sl_id}) | TP {prov_tp:.6g} (id {tp_id})\n"
                    f"   {self.risk.summary()}"
                )

            # 开仓推送 + 余额跨档提醒（dry-run 不发）
            self._notify_open(sym, direction, filled, avg, actual_notional,
                              actual_margin, prov_sl, prov_tp,
                              sig.leverage, provisional)
            self._check_balance(f"开仓 {sym} {direction.upper()} {filled}")

            return OrderResult(True, sym, direction, filled, avg, actual_notional,
                               actual_margin, sl_id, tp_id, provisional=provisional)

        except RiskRejected as e:
            self._alert(f"⛔ 风控拒单 {sym} {direction}: {e}")
            return OrderResult(False, sym, direction, error=f"风控: {e}")
        except BinanceError as e:
            self._alert(f"❌ 下单失败 {sym} {direction}: {e}")
            return OrderResult(False, sym, direction, error=str(e))
        except Exception as e:
            log.exception("下单异常")
            self._alert(f"❌ 下单异常 {sym}: {e}")
            return OrderResult(False, sym, direction, error=str(e))

    # ------------------------------------------------------------------
    def amend_protection(
        self,
        symbol: str,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_all: Optional[List[float]] = None,
        relaxed: bool = False,
    ) -> OrderResult:
        """
        阶段 2：收到真实止盈止损后替换兜底单。

        只给一边时，另一边沿用现有挂单的价位（不会被误撤掉）。
        流程：查现有单 → 撤全部 → 重挂 SL/TP
        """
        sym = symbol.upper()
        try:
            positions = self.c.get_positions(sym)
            # dry-run 下真实持仓为空，回退到虚拟持仓
            if not positions and self.dry_run:
                positions = [p for p in self.dry.all() if p.get("symbol") == sym]
            if not positions:
                # 仓位可能已被条件单平掉，残留挂单要清掉
                try:
                    self.c.cancel_all(sym)
                except Exception:
                    pass
                self.pending.remove(sym)
                return OrderResult(False, sym, "", error="无持仓，无需改单")
            if len(positions) > 1:
                return OrderResult(
                    False, sym, "",
                    error=f"{sym} 检测到 {len(positions)} 个持仓（双向持仓模式），"
                          f"本程序只支持单向持仓，请先在币安切换为单向")

            p = positions[0]
            pos_side = p["side"]
            direction = pos_side.lower()
            entry = p["entry"]

            # 只给一边时，从现有挂单里取另一边的价位
            if tp_price is None or sl_price is None:
                try:
                    for o in self.c.get_open_orders(sym):
                        if o.get("type") == "TAKE_PROFIT_MARKET" and tp_price is None:
                            tp_price = float(o["stopPrice"])
                        elif o.get("type") == "STOP_MARKET" and sl_price is None:
                            sl_price = float(o["stopPrice"])
                except Exception as e:
                    log.warning("读取现有挂单失败: %s", e)

            if tp_price and sl_price and self.strict_price_check:
                if relaxed:
                    # 移动止损：止损已经进到盈利区，只要求它在止盈的正确一侧
                    if direction == "short" and not (tp_price < sl_price):
                        return OrderResult(False, sym, direction,
                                           error=f"空单移动止损不合理 tp={tp_price} sl={sl_price}")
                    if direction == "long" and not (sl_price < tp_price):
                        return OrderResult(False, sym, direction,
                                           error=f"多单移动止损不合理 tp={tp_price} sl={sl_price}")
                else:
                    # 初次挂单：止损必须在入场价外侧
                    if direction == "long" and not (sl_price < entry < tp_price):
                        return OrderResult(False, sym, direction,
                                           error=f"多单价位不合理 entry={entry} sl={sl_price} tp={tp_price}")
                    if direction == "short" and not (tp_price < entry < sl_price):
                        return OrderResult(False, sym, direction,
                                           error=f"空单价位不合理 entry={entry} sl={sl_price} tp={tp_price}")

            if not sl_price:
                return OrderResult(False, sym, direction,
                                   error="缺少止损价且无法从现有挂单读取，拒绝执行（避免裸仓）")

            if self.dry_run:
                self._alert(
                    f"[DRY-RUN] amend {sym} {direction.upper()} "
                    f"SL {sl_price} TP {tp_price or '无'}（entry {entry:.6g}）"
                )
                self.pending.add(sym, {
                    "status": "protected", "direction": direction,
                    "entry": entry, "qty": abs(p["amount"]),
                    "tp": tp_price, "sl": sl_price,
                    "tp_all": tp_all or ([tp_price] if tp_price else []),
                })
                return OrderResult(True, sym, direction, abs(p["amount"]), p["mark"],
                                   dry_run=True)

            self.c.cancel_all(sym)
            # 轮询确认撤单真的生效，固定 sleep 在慢网络下不够用
            for _ in range(10):
                try:
                    if not self.c.get_open_orders(sym):
                        break
                except Exception:
                    break
                time.sleep(0.2)

            stamp = str(int(time.time()))[-8:]
            r = self.c.stop_loss(sym, pos_side, sl_price, f"SL{stamp}")
            sl_id = str(r.get("orderId", ""))

            tp_id = ""
            if tp_price:
                r = self.c.take_profit(sym, pos_side, tp_price, f"TP{stamp}")
                tp_id = str(r.get("orderId", ""))

            # 不删记录：改成「已保护」并保存全部目标价，
            # 后面 TP1/TP2 到达时要靠它把止损提上来
            self.pending.add(sym, {
                "status": "protected",
                "direction": direction,
                "entry": entry,
                "qty": abs(p["amount"]),
                "tp": tp_price,
                "sl": sl_price,
                "tp_all": tp_all or ([tp_price] if tp_price else []),
            })

            sl_pct = abs(sl_price - entry) / entry * 100
            tp_pct = abs(tp_price - entry) / entry * 100 if tp_price else 0
            self._alert(
                f"🔄 保护单已更新 {sym} {direction.upper()}\n"
                f"   止损 {sl_price} (-{sl_pct:.2f}%) id={sl_id}\n"
                f"   止盈 {tp_price or '无'} (+{tp_pct:.2f}%) id={tp_id}"
            )
            return OrderResult(True, sym, direction, abs(p["amount"]), p["mark"],
                               sl_order_id=sl_id, tp_order_id=tp_id)

        except Exception as e:
            self._alert(f"❌ 改单失败 {sym}: {e}")
            return OrderResult(False, sym, "", error=str(e))

    # ------------------------------------------------------------------
    def close(self, symbol: str, reason: str = "manual") -> OrderResult:
        sym = symbol.upper()
        try:
            positions = self.c.get_positions(sym)
            # dry-run 下真实持仓可能为空，回退到虚拟持仓
            if not positions and self.dry_run:
                positions = [p for p in self.dry.all() if p.get("symbol") == sym]
            if not positions:
                try:
                    self.c.cancel_all(sym)
                except Exception:
                    pass
                self.pending.remove(sym)
                return OrderResult(False, sym, "", error="无持仓")
            if len(positions) > 1:
                return OrderResult(
                    False, sym, "",
                    error=f"{sym} 检测到 {len(positions)} 个持仓（双向持仓模式），"
                          f"本程序只支持单向持仓，请先在币安切换为单向")

            p = positions[0]
            close_side = "SELL" if p["side"] == "LONG" else "BUY"

            if self.dry_run:
                self._alert(f"[DRY-RUN] 平仓 {sym} {p['side']} {abs(p['amount'])} ({reason})")
                self.pending.remove(sym)
                self.dry.remove(sym)
                return OrderResult(True, sym, p["side"].lower(), abs(p["amount"]),
                                   p["mark"], dry_run=True)

            # 平仓前先记权益，平仓后用差额算真实盈亏（含手续费和滑点）
            try:
                bal_before = self.c.get_balance()["total"]
            except Exception:
                bal_before = None

            self.c.cancel_all(sym)
            seed = Signal(symbol=sym, direction="close", entry=None,
                          signal_id=f"{reason}-{int(time.time())}")
            self.c.market_order(sym, close_side, abs(p["amount"]),
                                _coid("CL", seed), reduce_only=True)

            bal = self.c.get_balance()
            if bal_before is not None:
                pnl = bal["total"] - bal_before
            else:
                pnl = p["unrealized"]
            self.risk.record_trade(pnl, bal["total"])
            self.pending.remove(sym)
            self._alert(f"🔵 平仓 {sym} {p['side']} 盈亏 {pnl:+.2f}U ({reason})\n"
                        f"   {self.risk.summary()}")
            # 平仓后余额可能跨档，检查一次
            if self.balance_watcher:
                try:
                    self.balance_watcher.check(bal["total"], f"平仓 {sym} 盈亏 {pnl:+.2f}U")
                except Exception as e:
                    log.warning("余额检查失败: %s", e)
            return OrderResult(True, sym, p["side"].lower(), abs(p["amount"]), p["mark"])

        except Exception as e:
            self._alert(f"❌ 平仓失败 {sym}: {e}")
            return OrderResult(False, sym, "", error=str(e))

    # ------------------------------------------------------------------
    def pending_report(self, stale_minutes: int = 10) -> str:
        data = self.pending.all()
        if not data:
            return "没有待更新的仓位"
        stale = set(self.pending.stale(stale_minutes))
        lines = [f"待更新保护单的仓位：{len(data)} 个"]
        for sym, info in data.items():
            flag = f"  ⚠️ 超过 {stale_minutes} 分钟未更新" if sym in stale else ""
            lines.append(
                f"  {sym} {info.get('direction','').upper()} "
                f"entry={info.get('entry')} "
                f"兜底SL={info.get('provisional_sl')} 兜底TP={info.get('provisional_tp')}{flag}"
            )
        return "\n".join(lines)
