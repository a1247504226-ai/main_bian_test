# -*- coding: utf-8 -*-
"""事件驱动日线回测引擎（美股 ETF / 个股，只多 + 可选做空）。

严格约定：
- 信号在 bar i 的收盘确认，bar i+1 的开盘成交（无前视）
- 止损为「挂单型」，日内触及即成交（跳空优先按开盘价），单独标注
- 权益曲线逐 bar 记录 cash + shares * close
"""
import numpy as np


class Result(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _stats(eq, dates):
    eq = np.asarray(eq, float)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    n = len(eq)
    yrs = n / 252.0
    tot = eq[-1] / eq[0]
    cagr = tot ** (1 / yrs) - 1 if yrs > 0 else 0.0
    mdd = float(dd.min())
    # 最长水下
    underwater = 0
    longest = 0
    for v in dd:
        if v < -1e-12:
            underwater += 1
            longest = max(longest, underwater)
        else:
            underwater = 0
    rets = np.divide(eq[1:], eq[:-1], out=np.ones(len(eq) - 1),
                     where=eq[:-1] > 1e-12) - 1
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    calmar = float(cagr / abs(mdd)) if mdd < -1e-9 else 0.0
    return dict(total=tot, cagr=cagr, mdd=mdd, calmar=calmar, sharpe=sharpe,
                longest_dd=int(longest), final=float(eq[-1]))


def backtest(df, sig, fee=0.0, slip=0.0005, atr_stop=None, atr_n=14,
             vol_target=None, vol_cap=1.0, allow_short=False, cooldown=0,
             init=10000.0, exec_price="open", rebal=0, fixed_expo=None):
    """
    df: 含 open/high/low/close 的 DataFrame（已复权）
    sig: 目标仓位数组，取值 {0,1}（allow_short 时可为 {-1,0,1}），在 bar i 收盘确认
    atr_stop: None 或 ATR 倍数（挂单止损，日内触及成交）
    vol_target: None 或目标日波动（如 0.03），按 ATR/price 反推敞口
    vol_cap: 敞口上限
    rebal: 每 N 根强制再平衡（0 = 只在信号变化时调仓）
    fixed_expo: 固定敞口倍数（如 0.5），优先于 vol_target
    """
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    dates = df["date"].values

    # ATR 用 bar i 之前的信息（i-1 的 ATR），避免前视
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = np.zeros(len(c))
    a = tr[0]
    for i in range(len(c)):
        a = (a * (atr_n - 1) + tr[i]) / atr_n
        atr[i] = a
    atr_prev = np.roll(atr, 1); atr_prev[0] = atr[0]

    sig = np.asarray(sig, float)
    cash = init
    shares = 0.0
    entry_px = None
    entry_i = None
    stop_px = None
    side = 0
    eq_curve = np.zeros(len(c))
    eq_curve[0] = init
    trades = []
    last_exit_i = -10**9
    exposure = np.zeros(len(c))

    for i in range(len(c)):
        # ---------- 1. 开盘：执行上一根收盘确认的目标 ----------
        if i > 0:
            target = sig[i - 1]
            if not allow_short:
                target = max(0.0, target)
            # 计算目标股数
            if target == 0:
                tgt_sh = 0.0
            else:
                base = cash + shares * o[i]
                expo = abs(target)
                if fixed_expo is not None:
                    expo = fixed_expo
                elif vol_target is not None:
                    dvol = atr_prev[i] / o[i] if o[i] > 0 else 0.0
                    expo = min(vol_cap, vol_target / dvol) if dvol > 1e-9 else vol_cap
                tgt_sh = base * expo / o[i]
                tgt_sh *= np.sign(target)
            cur = shares
            # 冷却期（仅对「空仓→建仓」生效）
            want_flat = abs(tgt_sh) < 1e-12
            if (not want_flat) and abs(cur) < 1e-12 and (i - last_exit_i) <= cooldown:
                tgt_sh = 0.0
            # 反向或清仓：先平
            if cur * tgt_sh <= 0 and abs(cur) > 1e-12:
                px = o[i] * (1 - slip * np.sign(cur))
                cash += cur * px * (1 - fee)
                trades.append(dict(entry_i=entry_i, exit_i=i, dir=int(np.sign(cur)),
                                   entry_px=entry_px, exit_px=px,
                                   ret=float((px / entry_px - 1) * np.sign(cur)),
                                   reason="信号反转"))
                shares, entry_px, entry_i, stop_px, side = 0.0, None, None, None, 0
                last_exit_i = i
            # 建/加仓
            if abs(tgt_sh) > 1e-12 and abs(shares) < 1e-12:
                px = o[i] * (1 + slip * np.sign(tgt_sh))
                cost = abs(tgt_sh) * px * (1 + fee)
                if cost > cash:                       # 满仓保护
                    tgt_sh = np.sign(tgt_sh) * cash / (px * (1 + fee))
                    cost = abs(tgt_sh) * px * (1 + fee)
                cash -= cost
                shares = tgt_sh
                entry_px, entry_i, side = px, i, int(np.sign(tgt_sh))
                stop_px = None
                if atr_stop:
                    stop_px = px - side * atr_stop * atr_prev[i]
            # 同向再平衡（rebal>0 时定期执行；也用于 fixed_expo 的持续维持）
            elif rebal > 0 and i % rebal == 0 and abs(shares) > 1e-12 and shares * tgt_sh > 0:
                delta = tgt_sh - shares
                if abs(delta) > max(1e-9, 1e-3 * abs(shares)):
                    px = o[i] * (1 + slip * np.sign(delta))
                    if delta > 0:
                        cost = delta * px * (1 + fee)
                        if cost > cash:
                            delta = cash / (px * (1 + fee))
                        cash -= delta * px * (1 + fee)
                    else:
                        cash += (-delta) * px * (1 - fee)
                    shares += delta

        # ---------- 2. 日内：挂单止损（跳空优先） ----------
        if abs(shares) > 1e-12 and stop_px is not None:
            hit = False
            if side > 0:
                if o[i] <= stop_px:
                    px, hit = o[i] * (1 - slip), True
                elif l[i] <= stop_px:
                    px, hit = stop_px * (1 - slip), True
            else:
                if o[i] >= stop_px:
                    px, hit = o[i] * (1 + slip), True
                elif h[i] >= stop_px:
                    px, hit = stop_px * (1 + slip), True
            if hit:
                cash += shares * px * (1 - fee)
                trades.append(dict(entry_i=entry_i, exit_i=i, dir=side,
                                   entry_px=entry_px, exit_px=px,
                                   ret=float((px / entry_px - 1) * side),
                                   reason="止损"))
                shares, entry_px, entry_i, stop_px, side = 0.0, None, None, None, 0
                last_exit_i = i

        eq_curve[i] = cash + shares * c[i]
        # 破产保护（只做多时不会触发；做空遇到暴涨会）
        if eq_curve[i] <= 0:
            eq_curve[i] = 0.0
            if abs(shares) > 1e-12:
                trades.append(dict(entry_i=entry_i, exit_i=i, dir=side,
                                   entry_px=entry_px, exit_px=0.0, ret=-1.0, reason="爆仓"))
            cash, shares, entry_px, entry_i, stop_px, side = 0.0, 0.0, None, None, None, 0
            eq_curve[i:] = 0.0
            exposure[i:] = 0.0
            break
        exposure[i] = abs(shares * c[i]) / eq_curve[i] if eq_curve[i] > 0 else 0

    # 期末强制平仓（用于统计）
    if abs(shares) > 1e-12:
        px = c[-1] * (1 - slip * np.sign(shares))
        cash += shares * px * (1 - fee)
        trades.append(dict(entry_i=entry_i, exit_i=len(c) - 1, dir=side,
                           entry_px=entry_px, exit_px=px,
                           ret=float((px / entry_px - 1) * side),
                           reason="期末平仓"))
        eq_curve[-1] = cash

    st = _stats(eq_curve, dates)
    st["trades"] = trades
    st["n"] = len(trades)
    wins = [t for t in trades if t["ret"] > 0]
    st["win"] = len(wins) / len(trades) if trades else 0.0
    st["avg_expo"] = float(exposure[1:].mean()) if len(exposure) > 1 else 0.0
    st["equity"] = eq_curve
    st["exposure"] = exposure
    st["in_market"] = float((exposure > 0.01).mean())
    return Result(st)


def summarize(r, name=""):
    return (f"{name:<26} {r.total:9.2f}x  年化{r.cagr*100:7.2f}%  回撤{r.mdd*100:7.1f}%  "
            f"Calmar{r.calmar:5.2f}  Sharpe{r.sharpe:5.2f}  笔数{r.n:4d}  胜率{r.win*100:4.1f}%  "
            f"在场{r.in_market*100:4.1f}%  均敞口{r.avg_expo:.2f}")
