"""轻量冒烟测试：不联网，只验证核心函数逻辑是否可运行、止盈止损是否合理。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MODULE_PATH = Path(__file__).resolve().parent / "binance_multi_tf_monitor_5m_entry_optimized.py"
sys.path.insert(0, str(MODULE_PATH.parent))

import importlib.util

spec = importlib.util.spec_from_file_location("monitor5m", MODULE_PATH)
monitor5m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor5m)


def make_ohlcv(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=pd.Timestamp.now(tz="Asia/Shanghai"), periods=n, freq="5min", tz="Asia/Shanghai")
    close = 100 + np.cumsum(rng.normal(0, 0.35, n))
    high = close + rng.uniform(0.05, 0.6, n)
    low = close - rng.uniform(0.05, 0.6, n)
    open_ = close - rng.normal(0, 0.15, n)
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


df = make_ohlcv()
df = monitor5m.calculate_technical_indicators(df, "BTCUSDT")
assert not df.empty, "指标计算后数据不应为空"

df_valid = monitor5m.validate_market_data(df.copy(), "BTCUSDT", "5m")
assert not df_valid.empty, "5m 数据完整性校验不应被跳过"

signal_price = float(df["Close"].iloc[-2])
atr = float(df["ATR"].iloc[-2])
adx = float(df["ADX"].iloc[-2])
structure = monitor5m.get_recent_structure_levels(df, monitor5m.CONFIG["risk"]["structure_lookback_5m"])
pivot = {
    "H": signal_price * 1.03,
    "L": signal_price * 0.97,
    "C": signal_price * 1.01,
    "PP": signal_price,
    "R1": signal_price * 1.012,
    "R2": signal_price * 1.024,
    "S1": signal_price * 0.988,
    "S2": signal_price * 0.976,
}

for sig_type in (1, -1):
    plan = monitor5m.calculate_optimized_positions(
        signal_type=sig_type,
        signal_price=signal_price,
        execution_price=signal_price,
        atr=atr,
        adx=adx,
        pivot=pivot,
        structure=structure,
    )
    print("-" * 60)
    print("方向:", "LONG" if sig_type == 1 else "SHORT")
    print(f"信号价={plan.signal_price:.4f} ATR={plan.atr:.4f} ADX={plan.adx:.2f}")
    print(f"SL={plan.stop_loss:.4f} ({plan.stop_loss_pct * 100:.2f}%)")
    print(f"TP={plan.take_profit:.4f} ({plan.take_profit_pct * 100:.2f}%)")
    print(f"RR={plan.rr:.2f} 追踪止损距离={plan.trailing_stop:.4f}")

    assert plan.rr > 0, "RR 应大于 0"
    assert plan.stop_loss_pct <= monitor5m.CONFIG["risk"]["max_stop_pct"] + 1e-9, "止损应受最大止损比例约束"
    assert plan.take_profit_pct <= monitor5m.CONFIG["risk"]["max_take_profit_pct"] + 1e-9, "止盈应受最大止盈比例约束"
    if sig_type == 1:
        assert plan.stop_loss < signal_price < plan.take_profit, "多头方向错误"
    else:
        assert plan.take_profit < signal_price < plan.stop_loss, "空头方向错误"

print("-" * 60)
print("冒烟测试通过：数据校验、指标、止盈止损计算均可运行。")
