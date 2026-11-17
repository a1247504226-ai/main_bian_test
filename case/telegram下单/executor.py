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
from dataclasses import dataclass, field
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
    leverage: int = 3
    margin_pct: Optional[float] = None  # 保证金占权益（优先使用）
    position_pct: float = 0.10          # 名义占权益（margin_pct 为空时用）
    signal_id: str = ""
    # 开仓信号自带的全部止盈目标（TP1/TP2/TP3），后续移动止损要用
    tp_all: List[float] = field(default_factory=list)
    # 兜底保护（双向）
    provisional_sl_pct: float = 5.0
    provisional_tp_pct: float = 7.0

    def __post_init__(self):
        # 手动 CLI / batch 不给 --id 时，signal_id 会是空字符串。
        # _coid() 会退化成 "symbol|direction|None|None|None"，
        # 同一个币同方向每次开仓都拿到同一个 clientOrderId →
        # 第二笔被币安 -4116 当重复单，get_order 返回上一笔的成交，
        # 程序会误以为开仓成功并挂错数量的保护单。
        # 没显式给 id 就自动生成一个唯一 id（相当于放弃幂等保护）
        if not self.signal_id:
            self.signal_id = f"auto-{int(time.time() * 1000)}-{os.urandom(2).hex()}"

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


def _num(v) -> float:
    """严格解析币安返回的数值：'0' / '' / None / 非法值统一返回 0.0

    不能写 float(x or default)：'0' 是真值，会被当成 0.0 而不走 default，
    反过来 float(0 or qty) 又会把「没成交」误判成「全部成交」
    """
    try:
        f = float(v)
        return f if f > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


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
        # 告警邮件限流：同类失败 60 秒最多一封，避免币安抽风时刷爆邮箱
        self._mail_at: Dict[str, float] = {}

    # ------------------------------------------------------------------
    def _alert(self, text: str):
        log.info(text)
        if self.notify:
            try:
                self.notify(text)
            except Exception as e:
                log.warning("告警发送失败: %s", e)

    def _critical(self, text: str, key: str = ""):
        """关键失败（保护单/改单/平仓/撤单）：日志 + TG + 邮件。

        和 _alert 的区别：_alert 只用于「没开成仓、不影响已有仓位」的提示；
        凡是「已经有仓位、但保护或平仓出问题」的，一律走这里发邮件，
        否则机器人在无人值守时裸仓/平不掉都没人知道。

        邮件是异步入队，发送失败只记日志，不影响交易主流程；
        同类告警 60 秒限流，避免一次故障刷几十封。
        """
        log.error("%s", text.replace("\n", " | "))
        if self.notify:
            try:
                self.notify(text)
            except Exception as e:
                log.warning("告警发送失败: %s", e)
        if self.dry_run:
            return          # 模拟盘/回放不发告警邮件，避免测试时刷屏
        if not self.mailer or not getattr(self.mailer, "enabled", False):
            return
        now = time.time()
        k = key or text.splitlines()[0][:40]
        if now - self._mail_at.get(k, 0.0) < 60:
            log.debug("同类告警邮件 60 秒内已发过，跳过: %s", k)
            return
        self._mail_at[k] = now
        subject = "【交易机器人告警】" + text.splitlines()[0][:50]
        body = (
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"环境：{'DRY-RUN' if self.dry_run else 'LIVE'}\n\n"
            f"{text}\n\n"
            f"—— 交易机器人自动告警，请尽快检查仓位和挂单"
        )
        try:
            self.mailer.send(subject, body)
        except Exception as e:
            log.error("告警邮件入队失败: %s", e)

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

    def _notify_close(self, sym, side, pnl, reason, equity):
        """平仓后推邮件"""
        if not self.mailer:
            return
        body = (
            f"币种：{sym}\n"
            f"方向：{side}\n"
            f"盈亏：{pnl:+.2f} USDT\n"
            f"平仓原因：{reason}\n"
            f"当前权益：{equity:.2f} USDT\n\n"
            f"—— 交易机器人自动通知"
        )
        try:
            self.mailer.send(f"【平仓】{sym} {pnl:+.2f}U", body)
        except Exception as e:
            log.warning("平仓邮件发送失败: %s", e)

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

            # 已有持仓锁定的保证金要从可用额度里扣：
            # 否则第一仓亏一点，权益缩水但第一仓保证金已锁定，
            # 第二仓按新权益算 50% → 合计超过 100% → 永远开不了第二仓
            used_margin = 0.0
            for p_ in positions:
                lv = max(1, int(p_.get("leverage", 1)))
                used_margin += abs(p_["amount"] * p_["mark"]) / lv

            # 可用保证金：优先用币安返回的真实 availableBalance，而不是
            # equity - 估算占用。原因：
            #  1) 全仓仓位、挂单冻结、未实现盈亏都会影响真实可用额度，估算必然有偏差；
            #  2) 实测估算 1325.41U vs 真实 1324.83U，按估算下调后仍超 0.58U，
            #     币安直接 -2019 Margin is insufficient 拒单，主网同样会踩。
            # 留 1% 安全垫，避免四舍五入/手续费/未实现盈亏波动导致刚好超限。
            # dry-run 时虚拟仓位不在币安真实余额里，仍用「权益-估算占用」才能验证多仓限制
            real_avail = bal.get("available") if not self.dry_run else None
            avail = float(real_avail) if real_avail is not None else (equity - used_margin)
            avail = max(0.0, avail * 0.99)
            if avail <= 0:
                return OrderResult(False, sym, direction,
                                   error=f"可用保证金不足（权益 {equity:.2f}U，"
                                         f"可用 {avail:.2f}U，已占用估算 {used_margin:.2f}U）")
            if margin > avail:
                # 可用额度不到目标一半时直接拒绝：
                # 硬开出来的仓位会远小于预期，还可能低于最小下单量
                target_pct = sig.margin_pct if sig.margin_pct is not None else sig.position_pct
                if avail < equity * target_pct * 0.5:
                    return OrderResult(False, sym, direction,
                                       error=f"可用保证金不足（权益 {equity:.2f}U，"
                                             f"已占用 {used_margin:.2f}U，可用 {avail:.2f}U，"
                                             f"目标 {margin:.2f}U）")
                log.info("%s 保证金按可用额度下调 %.2fU → %.2fU（权益 %.2fU，"
                         "币安可用 %.2fU，已占用估算 %.2fU，含1%%安全垫）",
                         sym, margin, avail, equity,
                         float(bal.get("available") or 0), used_margin)
                margin = avail
                notional = margin * max(1, sig.leverage)

            qty = self.c.round_qty(sym, notional / mark)
            est_notional = qty * mark            # 风控用这个，与实际成交口径一致

            # 数量为 0 要在风控前拦掉，否则风控会报「名义价值 0 低于最小」，
            # 提示不准确（真实原因是数量取整后低于该交易对最小下单量）
            if qty <= 0:
                return OrderResult(False, sym, direction,
                                   error=f"下单数量取整后为 0（预算名义 {notional:.2f}U，"
                                         f"价格 {mark:.6g}，低于该交易对最小下单量）")

            # 取整会让实际名义偏离目标：高价币（BTC）stepSize 粗，偏差可能很大。
            # 权益越小偏差越大，必须提示，否则以为开了 150% 实际只有 80%
            if notional > 0 and est_notional < notional * 0.85:
                log.warning(
                    "%s 数量取整后名义 %.1fU，比目标 %.1fU 少 %.0f%%"
                    "（价格 %.6g，数量 %s）。权益越小偏差越大",
                    sym, est_notional, notional,
                    (1 - est_notional / notional) * 100, mark, qty)
                self._alert(
                    f"⚠️ {sym} 实际仓位偏小\n"
                    f"   目标名义 {notional:.1f}U → 实际 {est_notional:.1f}U"
                    f"（-{(1-est_notional/notional)*100:.0f}%）\n"
                    f"   原因：价格 {mark:.6g}，最小数量单位限制")

            # ---------- 2. 风控 ----------
            self.risk.check_open(
                symbol=sym,
                notional=est_notional,
                margin=margin,
                equity=equity,
                leverage=sig.leverage,
                open_positions=positions,
                available=real_avail,
            )

            # ---------- 3. 真实值方向 + 距离校验 ----------
            if sig.has_real_protection and sig.entry:
                if direction == "long" and not (sig.sl < sig.entry < sig.tp1):
                    return OrderResult(False, sym, direction,
                                       error=f"多单价位不合理 entry={sig.entry} sl={sig.sl} tp={sig.tp1}")
                if direction == "short" and not (sig.tp1 < sig.entry < sig.sl):
                    return OrderResult(False, sym, direction,
                                       error=f"空单价位不合理 entry={sig.entry} sl={sig.sl} tp={sig.tp1}")

            # 距离校验：信号里自带的价位也要检查是否离谱。
            # 频道笔误（止损多打一个 0）会挂出「永远不触发」的止损 = 裸奔
            # 回放时 strict_price_check=False（历史价位与当前价必然不匹配），跳过
            if not provisional and mark > 0 and self.strict_price_check:
                for label, price, hi in (("止损", sig.sl, 0.25), ("止盈", sig.tp1, 0.60)):
                    if price is None:
                        continue
                    dist = abs(price - mark) / mark
                    if dist <= 0.005:
                        return OrderResult(
                            False, sym, direction,
                            error=f"{label}价 {price:.6g} 距当前价 {mark:.6g} 仅 {dist*100:.2f}%，"
                                  f"太近会被立即触发，拒绝开仓")
                    if dist > hi:
                        return OrderResult(
                            False, sym, direction,
                            error=f"{label}价 {price:.6g} 距当前价 {mark:.6g} 为 {dist*100:.1f}%，"
                                  f"超过 {hi*100:.0f}% 上限，疑似写错，拒绝开仓")

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
            # 开仓前必须清掉该币的历史挂单：
            # 上一次仓位被止损平掉后，旧的止盈单（closePosition=true）会留在交易所，
            # 新仓位一开，它一触发就把新仓平掉 —— 这种「幽灵单」极难排查。
            # 清不掉就不开仓（宁可不下单，也不下可能被误平的单）
            try:
                self.c.cancel_all(sym)
                # cancel_all 内部会把单次撤单失败吞成 warning，必须复核真的清空了：
                # 残留的 closePosition 条件单会在新仓开出的瞬间把它平掉
                left = self.c.get_open_orders(sym, strict=True)
                if left:
                    raise BinanceError(-1, f"仍有 {len(left)} 个挂单未撤掉")
            except Exception as e:
                log.error("%s 开仓前清理历史挂单失败，拒绝开仓: %s", sym, e)
                self._alert(f"⚠️ {sym} 开仓前无法清理历史挂单，已放弃开仓\n"
                            f"   原因：{e}\n"
                            f"   请手动检查该币是否有残留条件单")
                return OrderResult(False, sym, direction,
                                   error=f"开仓前无法清理历史挂单: {e}")

            # 逐仓：每个仓位独立保证金，单仓爆仓最多亏掉该仓保证金，不牵连其他仓位
            try:
                self.c.set_margin_type(sym, isolated=True)
            except BinanceError as e:
                # -4048 有持仓 / -4047 有挂单 时币安不允许改保证金模式
                if e.code in (-4048, -4047):
                    log.error("%s 无法切换逐仓（%s）", sym, e)
                    self._alert(
                        f"⚠️ {sym} 已有持仓/挂单，无法切换成逐仓，已放弃开仓\n"
                        f"   原因：{e}\n"
                        f"   处理：先平掉该币仓位或撤掉挂单，下次开仓会自动设逐仓")
                    return OrderResult(False, sym, direction,
                                       error=f"无法切换逐仓（有持仓/挂单）: {e}")
                raise
            self.c.set_leverage(sym, sig.leverage)

            order = self.c.market_order(sym, side, qty, coid_open)

            # 成交价/成交量必须严格解析：
            # 币安偶发返回 "0" 时，用 `or` 会误判成「全部成交」，
            # 结果挂上止损止盈却根本没有仓位，或者按错误数量挂单
            filled = _num(order.get("executedQty"))
            avg = _num(order.get("avgPrice")) or ref_price

            if filled <= 0:
                log.error("%s 市价单未成交 executedQty=%r，不挂保护单",
                          sym, order.get("executedQty"))
                self._alert(f"❌ {sym} 市价单未成交（executedQty={order.get('executedQty')}），"
                            f"请手动检查")
                return OrderResult(False, sym, direction,
                                   error="市价单未成交（executedQty=0）")

            # 部分成交：实际仓位比预期少，后面挂止损必须用实际数量
            if filled < qty * 0.95:
                log.warning("%s 部分成交 %.6g / 预期 %.6g（%.0f%%）",
                            sym, filled, qty, filled / qty * 100)
                self._alert(f"⚠️ {sym} 部分成交：实际 {filled:.6g} / 预期 {qty:.6g}")

            # 滑点告警：成交价偏离标记价超过 2%，说明流动性差或价格剧烈波动
            if mark > 0 and abs(avg - mark) / mark > 0.02:
                log.warning("%s 滑点偏大：标记价 %.6g → 成交价 %.6g（%+.2f%%）",
                            sym, mark, avg, (avg - mark) / mark * 100)
                self._alert(f"⚠️ {sym} 滑点 {(avg - mark) / mark * 100:+.2f}%\n"
                            f"   标记价 {mark:.6g} → 成交价 {avg:.6g}")

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
                    self._critical(
                        f"⚠️ {sym} 止损价 {prov_sl:.6g} 已在当前价 {mark:.6g} 的错误一侧，"
                        f"跳过挂单（价格已穿越止损位，请立即人工确认）",
                        key=f"sl_side_{sym}")
                else:
                    r = self.c.stop_loss(sym, pos_side, prov_sl, coid_sl)
                    sl_id = str(r.get("orderId", ""))

                # 没有止损保护 = 裸仓，必须平掉
                if not sl_id:
                    raise BinanceError(-1, "止损单未能挂上，拒绝留裸仓")

                # 止盈失败不致命：止损已经生效，仓位仍有保护。
                # 绝不能因为止盈挂不上就把仓位平掉（白付手续费、丢单）
                if not tp_ok:
                    log.info("%s 止盈价 %s 已在当前价 %s 错误一侧，跳过", sym, prov_tp, mark)
                else:
                    try:
                        r = self.c.take_profit(sym, pos_side, prov_tp, coid_tp)
                        tp_id = str(r.get("orderId", ""))
                    except BinanceError as e:
                        log.error("%s 止盈挂单失败（止损已生效，仓位仍受保护）: %s", sym, e)
                        self._critical(f"⚠️ {sym} 止盈挂单失败：{e}\n"
                                       f"   止损已生效，仓位有保护；收到真实止盈止损后会重挂",
                                       key=f"open_tp_{sym}")

            except BinanceError as e:
                log.error("保护单挂单失败 %s，紧急平仓", e)
                self._critical(f"[紧急] {sym} 保护单失败 {e}，正在平仓",
                               key=f"protect_{sym}")
                try:
                    self.c.cancel_all(sym)
                    self.c.market_order(sym, "SELL" if side == "BUY" else "BUY",
                                        filled, _coid("EM", sig), reduce_only=True)
                    self._alert(f"[已平仓] {sym} 因保护单失败已紧急平仓")
                except Exception as e2:
                    self._critical(f"[严重] {sym} 紧急平仓也失败: {e2}，请立刻手动处理！",
                                   key=f"emerg_{sym}")
                return OrderResult(False, sym, direction, filled, avg,
                                   actual_notional, actual_margin,
                                   error=f"保护单失败: {e}")

            # ---------- 8. 记录 ----------
            # 结构化单行日志：一行一笔，方便 grep 和事后对账
            log.info(
                "开仓成交 | %s %s | 数量 %s | 成交价 %.6g | 名义 %.2fU | 保证金 %.2fU | "
                "%dx | SL %.6g | TP %.6g | 订单 %s | coid %s | 保护 %s | 信号 %s",
                sym, direction.upper(), filled, avg, actual_notional, actual_margin,
                sig.leverage, prov_sl, prov_tp, order.get("orderId"), coid_open,
                "兜底" if provisional else "完整", sig.signal_id)

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
                    f"   兜底 SL {prov_sl:.6g} (id {sl_id}) | TP {prov_tp:.6g} (id {tp_id})\n"
                    f"   订单 {order.get('orderId')} | coid {coid_open} | 信号 {sig.signal_id}\n"
                    f"   ⏳ 等待真实止盈止损，收到后执行 amend"
                )
            else:
                # 开仓信号自带真实止盈止损：也必须写 pending，否则
                # 后续 TPSL 改单 / TP1 到达移动止损 / 巡检清理全部失效
                # （频道偶尔会把方向和价位写在一条消息里）
                self.pending.add(sym, {
                    "status": "protected",
                    "direction": direction, "entry": avg, "qty": filled,
                    "tp": prov_tp, "sl": prov_sl,
                    "tp_all": list(sig.tp_all or ([prov_tp] if prov_tp else [])),
                    "leverage": sig.leverage,
                    "sl_order_id": sl_id, "tp_order_id": tp_id,
                })
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
    def _emergency_close(self, sym: str, pos_side: str, qty: float,
                         prefix: str = "EM") -> bool:
        """保护单挂不上时的紧急市价平仓。返回是否成功。"""
        try:
            self.c.cancel_all(sym)
        except Exception as e:
            # 撤单失败不能阻止平仓
            log.warning("%s 紧急平仓前撤单失败（继续平仓）: %s", sym, e)
        try:
            close_side = "SELL" if pos_side.upper() == "LONG" else "BUY"
            coid = f"{prefix}{int(time.time() * 1000) % 10 ** 10}"
            self.c.market_order(sym, close_side, qty, coid, reduce_only=True)
            self.pending.remove(sym)
            self._alert(f"[已平仓] {sym} 因保护单失败已紧急平仓")
            return True
        except Exception as e:
            self._critical(f"[严重] {sym} 紧急平仓也失败: {e}，请立刻手动处理！",
                           key=f"emerg_{sym}")
            return False

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
                    # 距离校验：频道笔误（止损多打一个 0）会挂出永远不触发的止损，
                    # 等于完全没有保护，必须在挂单前拦掉
                    for label, price, hi in (("止损", sl_price, 0.25),
                                             ("止盈", tp_price, 0.60)):
                        dist = abs(price - entry) / entry
                        if dist <= 0.005:
                            return OrderResult(
                                False, sym, direction,
                                error=f"{label}价 {price:.6g} 距入场价 {entry:.6g} "
                                      f"仅 {dist*100:.2f}%，太近会被立即触发，拒绝改单")
                        if dist > hi:
                            return OrderResult(
                                False, sym, direction,
                                error=f"{label}价 {price:.6g} 距入场价 {entry:.6g} "
                                      f"为 {dist*100:.1f}%，超过 {hi*100:.0f}% 上限，"
                                      f"疑似写错，拒绝改单")

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

            # 记下旧保护价：新单挂失败时用它回滚，避免「旧单已撤、新单没挂上」的裸仓
            old = self.pending.get(sym) or {}
            old_sl = old.get("sl") or old.get("provisional_sl")

            try:
                self.c.cancel_all(sym)
            except Exception as e:
                # cancel_all 内部已吞掉单次撤单失败，这里只兜网络类异常
                log.warning("%s 撤旧保护单异常（继续确认）: %s", sym, e)

            # 轮询确认撤单真的生效。没确认就挂新单 = 同一币两个止损单，
            # 旧单一触发平仓，新单变孤儿；下次开仓还可能被它误触发
            cancelled = False
            confirm_error = None
            try:
                # 6 次 × 0.4s = 2.4 秒，比原来 10 次 × 0.2s 少 4 个请求（省权重）
                for _ in range(6):
                    # strict：algo 查询失败必须抛，不能把「查不到」当成「已撤干净」
                    if not self.c.get_open_orders(sym, strict=True):
                        cancelled = True
                        break
                    time.sleep(0.4)
            except Exception as e:
                confirm_error = e
                log.warning("%s 撤单确认查询失败: %s", sym, e)

            if not cancelled and not confirm_error:
                # 查询成功、且确实还有挂单 → 旧保护仍在，放弃改单是安全的
                log.error("%s 撤旧保护单未确认（仍有挂单），拒绝挂新单", sym)
                self._critical(f"⚠️ {sym} 撤旧保护单未确认，已放弃本次改单\n"
                               f"   当前仍是旧保护单，请手动检查挂单",
                               key=f"cancel_{sym}")
                return OrderResult(False, sym, direction,
                                   error="撤旧保护单未确认，拒绝挂新单避免重复")

            if confirm_error:
                # 查询失败 ≠ 旧单还在：旧单可能已经被撤掉，仓位此刻可能裸奔。
                # 不能在这里直接 return（之前就是误判成「旧单还在」导致裸仓），
                # 继续往下挂新止损；万一旧单还在，最多多一张，不会双倍平仓。
                log.error("%s 撤单结果无法确认（%s），立即挂新止损兜底", sym, confirm_error)

            # 单号唯一到毫秒 + 随机后缀：同一毫秒内两次改单也不会撞 -4116
            stamp = f"{int(time.time() * 1000) % 10 ** 10}{os.urandom(1).hex()}"
            sl_id = ""
            try:
                r = self.c.stop_loss(sym, pos_side, sl_price, f"SL{stamp}")
                sl_id = str(r.get("orderId", ""))
                if not sl_id:
                    raise BinanceError(-1, "止损单未返回 orderId，视为失败")
            except Exception as e:
                # 旧单已撤、新单没挂上 = 裸仓。先用旧止损价回滚，再不行就紧急平仓
                log.error("%s 新止损挂单失败（旧单已撤），尝试回滚旧止损 %s: %s",
                          sym, old_sl, e)
                if old_sl:
                    try:
                        r2 = self.c.stop_loss(sym, pos_side, old_sl, f"SLR{stamp}")
                        sl_id = str(r2.get("orderId", ""))
                        if sl_id:
                            self._critical(
                                f"⚠️ {sym} 新止损挂单失败，已回滚到旧止损 {old_sl:.6g}\n"
                                f"   原因：{e}\n"
                                f"   本次改单未生效，请检查价位后重试",
                                key=f"amend_sl_{sym}")
                    except Exception as e2:
                        log.error("%s 回滚旧止损也失败: %s", sym, e2)
                if not sl_id:
                    log.error("%s 止损彻底挂不上，紧急平仓", sym)
                    self._critical(f"[紧急] {sym} 改单后无法挂上止损，正在平仓",
                                   key=f"amend_emerg_{sym}")
                    self._emergency_close(sym, pos_side, abs(p["amount"]), "AME")
                    return OrderResult(False, sym, direction, abs(p["amount"]), p["mark"],
                                       error=f"止损挂单失败且回滚失败: {e}")
                return OrderResult(False, sym, direction, abs(p["amount"]), p["mark"],
                                   sl_order_id=sl_id,
                                   error=f"新止损挂单失败，已回滚旧止损: {e}")

            tp_id = ""
            tp_err = ""
            if tp_price:
                try:
                    r = self.c.take_profit(sym, pos_side, tp_price, f"TP{stamp}")
                    tp_id = str(r.get("orderId", ""))
                    if not tp_id:
                        tp_err = "止盈单未返回 orderId"
                except Exception as e:
                    # 止盈挂不上不算致命：止损还在，仓位仍受保护。
                    # 不能因为止盈失败就把已经挂好的止损撤掉
                    tp_err = str(e)
                    log.error("%s 止盈挂单失败（止损已挂上）: %s", sym, tp_err)

            # 不删记录：改成「已保护」并保存全部目标价，
            # 后面 TP1/TP2 到达时要靠它把止损提上来
            # 止盈没挂上时不能记 tp，否则本地状态与交易所不一致
            self.pending.add(sym, {
                "status": "protected",
                "direction": direction,
                "entry": entry,
                "qty": abs(p["amount"]),
                "tp": tp_price if tp_id else None,
                "sl": sl_price,
                # tp_all 是「目标价清单」，和 TP 单是否挂上无关：
                # 即使这次止盈没挂上，价格到 TP1 也要把止损提上来，
                # 清空会导致后续 TP1/TP2 全被当成「目标价不全」忽略
                "tp_all": tp_all or ([tp_price] if tp_price else []),
            })

            sl_delta = (sl_price - entry) / entry * 100
            tp_delta = ((tp_price - entry) / entry * 100) if tp_price else 0.0
            sl_pct = abs(sl_delta)
            tp_pct = abs(tp_delta)
            # 结构化单行日志
            log.info(
                "改单成交 | %s %s | 入场 %.6g | 止损 %.6g (%+.2f%%) id=%s | "
                "止盈 %s (%+.2f%%) id=%s | 目标 %s",
                sym, direction.upper(), entry, sl_price,
                (sl_price - entry) / entry * 100, sl_id,
                f"{tp_price:.6g}" if tp_price else "无", tp_pct, tp_id,
                tp_all or "-")
            self._alert(
                f"🔄 保护单已更新 {sym} {direction.upper()}\n"
                f"   止损 {sl_price} ({sl_delta:+.2f}%) id={sl_id}\n"
                f"   止盈 {tp_price or '无'} ({tp_delta:+.2f}%) id={tp_id}"
            )
            if tp_err:
                self._critical(f"⚠️ {sym} 止盈未挂上（止损已生效）\n   {tp_err}\n"
                               f"   仓位仍有止损保护，巡检会重试补挂止盈",
                               key=f"amend_tp_{sym}")
                return OrderResult(False, sym, direction, abs(p["amount"]), p["mark"],
                                   sl_order_id=sl_id,
                                   error=f"止盈挂单失败: {tp_err}")
            return OrderResult(True, sym, direction, abs(p["amount"]), p["mark"],
                               sl_order_id=sl_id, tp_order_id=tp_id)

        except Exception as e:
            self._critical(f"❌ 改单失败 {sym}: {e}", key=f"amend_{sym}")
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
            # 必须 fresh：缓存里的权益可能是 1.5 秒前的，算出来的盈亏会偏
            try:
                bal_before = self.c.get_balance(fresh=True)["total"]
            except Exception:
                bal_before = None

            # 撤保护单失败不能阻止平仓：用户要的是平掉仓位，
            # 撤单失败最多留个孤儿条件单，平仓后它会因无仓位而失效
            try:
                self.c.cancel_all(sym)
            except Exception as e:
                log.warning("%s 平仓前撤单失败（继续平仓）: %s", sym, e)
            seed = Signal(symbol=sym, direction="close", entry=None,
                          signal_id=f"{reason}-{int(time.time())}")
            close_order = self.c.market_order(sym, close_side, abs(p["amount"]),
                                              _coid("CL", seed), reduce_only=True)

            # 平仓单也可能 0 成交：不确认就删 pending 会留下裸奔的仓位
            if _num(close_order.get("executedQty")) <= 0:
                log.error("%s 平仓单未成交 executedQty=%r，仓位仍在",
                          sym, close_order.get("executedQty"))
                self._critical(f"❌ {sym} 平仓单未成交，仓位仍在，请手动处理",
                               key=f"close_{sym}")
                return OrderResult(False, sym, p["side"].lower(),
                                   error="平仓单未成交（executedQty=0）")

            fill_px = _num(close_order.get("avgPrice")) or p["mark"]

            # 再确认仓位真的清零：部分成交会残留，此时不能删 pending
            left = None
            for _ in range(3):
                try:
                    # 必须强制刷新：缓存里的持仓是平仓前的，会误判「没平掉」
                    poss = self.c.get_positions(sym, fresh=True)
                    left = abs(float(poss[0]["amount"])) if poss else 0.0
                except Exception:
                    left = None
                    break
                if left < 1e-9:
                    break
                time.sleep(0.3)

            if left is None:
                # 注意：返回空持仓（left=0）是平仓成功，不会走到这里。
                # 只有持仓查询接口本身报错（网络/币安异常）才是 None —— 此时
                # 平仓单已经成交，但没法确认仓位是否真的清零，保留 pending 并告警。
                self._critical(f"⚠️ {sym} 平仓单已成交，但持仓查询接口报错，"
                               f"无法确认是否清零（已保留 pending）\n"
                               f"   请手动确认：python main.py positions",
                               key=f"close_verify_{sym}")
                return OrderResult(False, sym, p["side"].lower(),
                                   error="平仓后持仓查询失败（不是无持仓）")

            if left > 1e-9:
                log.error("%s 平仓后仍剩 %.6g（部分成交），保留 pending 记录", sym, left)
                self._critical(f"⚠️ {sym} 平仓后仍剩 {left:.6g}，未完全平掉\n"
                               f"   请手动处理：python main.py close --symbol {sym}",
                               key=f"close_left_{sym}")
                return OrderResult(False, sym, p["side"].lower(),
                                   error=f"平仓未完全成交，剩余 {left:.6g}")

            bal = self.c.get_balance()
            if bal_before is not None:
                pnl = bal["total"] - bal_before
            else:
                pnl = p["unrealized"]
            self.risk.record_trade(pnl, bal["total"])
            self.pending.remove(sym)
            # 结构化单行日志
            log.info(
                "平仓成交 | %s %s | 数量 %s | 成交价 %.6g | 入场 %.6g | 盈亏 %+.2fU | "
                "原因 %s | 订单 %s | 权益 %.2fU | %s",
                sym, p["side"], abs(p["amount"]), fill_px, p["entry"], pnl,
                reason, close_order.get("orderId"), bal["total"],
                f"{(pnl/(bal['total']-pnl)*100):+.2f}%" if bal["total"] - pnl > 0 else "-")
            self._alert(
                f"🔵 平仓 {sym} {p['side']} {abs(p['amount'])} @ {fill_px:.6g}\n"
                f"   入场 {p['entry']:.6g} | 盈亏 {pnl:+.2f}U | 原因 {reason}\n"
                f"   订单 {close_order.get('orderId')} | 权益 {bal['total']:.2f}U\n"
                f"   {self.risk.summary()}"
            )
            self._notify_close(sym, p["side"], pnl, reason, bal["total"])
            # 平仓后余额可能跨档，检查一次
            if self.balance_watcher:
                try:
                    self.balance_watcher.check(bal["total"], f"平仓 {sym} 盈亏 {pnl:+.2f}U")
                except Exception as e:
                    log.warning("余额检查失败: %s", e)
            return OrderResult(True, sym, p["side"].lower(), abs(p["amount"]), p["mark"])

        except Exception as e:
            # 平仓抛异常时仓位可能还在，必须邮件告警，不能只记日志
            self._critical(f"❌ 平仓失败 {sym}: {e}\n   仓位可能仍在，请手动检查",
                           key=f"close_{sym}")
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
