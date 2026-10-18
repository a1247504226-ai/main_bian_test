# -*- coding: utf-8 -*-
"""候选池筛选（东财后复权数据版）。

每个标的算三件事：
  A. d_calmar —— 用最终策略（MA250 + 波动率目标 2.5%）相对买入持有的 Calmar 提升
  B. mc       —— 蒙特卡洛分位：保持同样的在场比例和翻仓次数，随机生成 400 条状态序列，
                 看真实信号排第几（>0.9 = 信号确有方向预测力）
  C. 基础统计 —— 年化波动、在场比例、翻仓次数
"""
import os
import numpy as np
import pandas as pd
from engine import backtest, _stats
from em import load, EM, ma_sig, COST

MIN_BARS = 2500
VT = 0.025
N_PATH = 400


def mc_test(df, sig, n_path=N_PATH, seed=11):
    """相位置换检验（比「随机翻仓」更严格）。

    把信号相对收益序列做随机圆周平移：在场比例、翻仓次数、持仓段长度分布全部保持不变，
    只破坏「信号时点」与「行情时点」的对应关系。
    若真实信号分位 ≈ 50%，说明它的择时能力等同于随机相位，即没有方向预测力。
    """
    c = df["close"].values.astype(float)
    logr = np.log(c[1:] / c[:-1])
    sig = np.asarray(sig, float)
    pos = np.roll(sig, 1); pos[0] = 0.0
    pos = pos[1:]                                   # 与 logr 对齐
    real = float(np.dot(logr, pos))
    flips = int((sig[1:] != sig[:-1]).sum())
    n = len(pos)
    if flips < 4 or pos.sum() < 20:
        return None, flips
    rng = np.random.default_rng(seed)
    offs = rng.integers(1, n, size=n_path)
    sims = np.array([np.dot(logr, np.roll(pos, int(o))) for o in offs])
    return float((sims < real).mean()), flips


def scan_one(sym):
    df = load(sym)
    if len(df) < MIN_BARS:
        return None
    # 数据体检：单日涨跌 >60% 一律视为复权因子断裂（真实大市值个股几乎不可能），剔除
    r_all = df["close"].pct_change()
    if r_all.abs().max() > 0.60:
        bad = df.loc[r_all.abs().idxmax(), "date"]
        raise ValueError(f"复权因子断裂 {pd.Timestamp(bad).date()} ({r_all.abs().max()*100:.0f}%)")
    sig = ma_sig(df, 250)
    bh = backtest(df, np.ones(len(df)), **COST)
    tr = backtest(df, sig, vol_target=VT, vol_cap=1.0, **COST)
    q, flips = mc_test(df, sig)
    r = df["close"].pct_change().dropna()
    return dict(sym=sym, n=len(df), start=str(df.date.iloc[0].date()), years=len(df) / 252,
                vol=float(r.std() * np.sqrt(252)),
                bh_total=bh.total, bh_cagr=bh.cagr, bh_mdd=bh.mdd, bh_calmar=bh.calmar,
                tr_total=tr.total, tr_cagr=tr.cagr, tr_mdd=tr.mdd, tr_calmar=tr.calmar,
                tr_sharpe=tr.sharpe, bh_sharpe=bh.sharpe,
                d_calmar=tr.calmar - bh.calmar, d_cagr=tr.cagr - bh.cagr,
                mc=q, flips=flips, in_market=tr.in_market, expo=tr.avg_expo)


def main():
    idx = pd.read_csv(os.path.join(EM, "_index.csv"))
    rows = []
    for i, sym in enumerate(idx.sym, 1):
        try:
            r = scan_one(sym)
            if r:
                rows.append(r)
            print(f"\r  {i}/{len(idx)} {sym:6}", end="", flush=True)
        except Exception as e:
            print(f"\n  {sym} 失败 {type(e).__name__} {e}")
    print()
    t = pd.DataFrame(rows).merge(idx[["sym", "cn"]], on="sym", how="left")
    t = t.sort_values("d_calmar", ascending=False)
    t.to_csv(os.path.join(EM, "_scan.csv"), index=False)

    print(f"\n可回测 {len(t)} 个（≥{MIN_BARS} 根）")
    print(f"Calmar 提升为正：{int((t.d_calmar > 0).sum())} 个")
    print(f"MC 中位数 {t.mc.median()*100:.0f}%，MC>90% 的 {int((t.mc > 0.9).sum())} 个")
    print(f"年化波动 vs d_calmar 相关 {t.vol.corr(t.d_calmar):+.3f}，vs MC {t.vol.corr(t.mc):+.3f}")

    print("\n=== Calmar 提升前 15 ===")
    for _, r in t.head(15).iterrows():
        print(f"{r.sym:6}{str(r.cn)[:8]:<9}{r.years:5.1f}年  vol{r.vol*100:4.0f}%  "
              f"BH {r.bh_cagr*100:5.1f}%/{r.bh_mdd*100:6.1f}%/Cal{r.bh_calmar:5.2f}  "
              f"TR {r.tr_cagr*100:5.1f}%/{r.tr_mdd*100:6.1f}%/Cal{r.tr_calmar:5.2f}  "
              f"Δ{r.d_calmar:+5.2f}  MC{r.mc*100:3.0f}%")
    print("\n=== Calmar 提升最差 12 ===")
    for _, r in t.tail(12).iterrows():
        print(f"{r.sym:6}{str(r.cn)[:8]:<9}{r.years:5.1f}年  vol{r.vol*100:4.0f}%  "
              f"BH {r.bh_cagr*100:5.1f}%/{r.bh_mdd*100:6.1f}%/Cal{r.bh_calmar:5.2f}  "
              f"TR {r.tr_cagr*100:5.1f}%/{r.tr_mdd*100:6.1f}%/Cal{r.tr_calmar:5.2f}  "
              f"Δ{r.d_calmar:+5.2f}  MC{r.mc*100:3.0f}%")
    return t


if __name__ == "__main__":
    main()
