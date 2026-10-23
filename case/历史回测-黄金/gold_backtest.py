# -*- coding: utf-8 -*-
"""
黄金 AU 策略回测 / 风险档位表
复用 gold_strategy.py 的同一套信号逻辑，确保"回测=实盘"一致。

运行：python gold_backtest.py
输出：控制台风险档位表 + output/equity_curve.png（若安装了 matplotlib）
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from gold_strategy import load_config, fetch_gold_daily, simulate, equity_curve, metrics_from_eq

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, 'output')
os.makedirs(OUT_DIR, exist_ok=True)

def run(cfg):
    df, src = fetch_gold_daily(cfg)
    st = simulate(df, cfg)
    eqs = equity_curve(df, st, cfg['cost'])
    return df, st, eqs, metrics_from_eq(eqs), src

def main():
    cfg = load_config()
    print('黄金 AU 策略回测（鱼身趋势 + 波动率目标 + 吊灯止损）')
    print(f"参数: MA{cfg['ma_slow']}/{cfg['ma_fast']}  fail_bars={cfg['fail_bars']}  "
          f"吊灯={cfg['trail_atr']}ATR  ADX={cfg['adx_min']}")
    df, st, eqs, m, src = run(cfg)
    print(f"数据: {pd.Timestamp(df['date'].iloc[0]).date()} -> {pd.Timestamp(df['date'].iloc[-1]).date()}  "
          f"({len(df)} 根, 源:{src})")
    print('\n===== 当前配置（target_vol=%.2f）=====' % cfg['target_vol'])
    print(f"年化 {m['cagr']*100:6.1f}% | 总收益 {m['total']*100:8.1f}% | 最大回撤 {m['maxdd']*100:6.1f}% | 夏普 {m['sharpe']:.2f}")

    print('\n===== 风险档位表（调 target_vol 即可改变复利/回撤）=====')
    print(f"{'target_vol':>10} {'年化CAGR':>9} {'最大回撤':>9} {'夏普':>6} {'总收益':>10}")
    rows = []
    for tv in [0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]:
        c = dict(cfg); c['target_vol'] = tv
        _, _, e, mm, _ = run(c)
        print(f"{tv:10.2f} {mm['cagr']*100:8.1f}% {mm['maxdd']*100:8.1f}% "
              f"{mm['sharpe']:6.2f} {mm['total']*100:9.1f}%")
        rows.append(dict(target_vol=tv, cagr=mm['cagr'], maxdd=mm['maxdd'],
                         sharpe=mm['sharpe'], total=mm['total']))
    pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, 'risk_dial.csv'), index=False, encoding='utf-8-sig')

    # 画权益曲线（当前配置）
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        dates = pd.to_datetime(df['date'])
        fig, ax = plt.subplots(figsize=(10, 4.2))
        ax.plot(dates, eqs / 1e4, color='#c0392b', lw=1.6, label='Strategy equity')
        ax.set_title(f"Gold AU trend strategy  (target_vol={cfg['target_vol']:.2f}, "
                     f"CAGR={m['cagr']*100:.1f}%, MaxDD={m['maxdd']*100:.1f}%)")
        ax.set_ylabel('Equity (10k RMB)'); ax.grid(alpha=0.3); ax.legend()
        p = os.path.join(OUT_DIR, 'equity_curve.png')
        fig.savefig(p, dpi=120, bbox_inches='tight'); plt.close(fig)
        print(f"\n权益曲线已保存: {p}")
    except Exception as e:
        print(f"\n(跳过绘图: {e})")

if __name__ == '__main__':
    main()
