# -*- coding: utf-8 -*-
"""
回归测试：止损「跳空」必须用「正在形成那根 K 线」的开盘价，不能用已收盘那根。
场景：持有多单，入场 100，止损 95。
  - 刚收盘那根：open=99, low=98（未触及止损）
  - 正在形成那根：open=90（跳空跌破 95！）, low=89
旧代码会把 last_closed_open(=99) 当作跳空判定 -> 成交价=止损价 95（少算亏损）
新代码会同时检查 forming open(=90) -> 成交价=90（正确）
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_monitor as L

H1 = 3600_000
H4 = 4 * H1

# 1h 三根：older / 刚收盘(未破止损) / 正在形成(跳空破止损)
b_older = {"t": 0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 100.0, "v": 1.0}
b_closed = {"t": H1, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "v": 1.0}
b_form = {"t": 2 * H1, "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "v": 1.0}

# 4h：一段上涨，保证 want=long（不会触发趋势失效离场）
ft_bars = []
for i in range(250):
    c = 100.0 + i
    ft_bars.append({"t": i * H4, "open": c - 1, "high": c + 1,
                    "low": c - 2, "close": c, "v": 1.0})


def provider(symbol, interval, limit, closed_only=True):
    if interval == "4h":
        return ft_bars[-limit:]
    closed = [b_older, b_closed]
    if closed_only:
        return closed[-limit:]
    return (closed + [b_form])[-limit:]


def price_provider(symbol):
    return b_form["close"]


cfg = L.load_cfg()
cfg = dict(cfg, email={"enabled": False}, leverage=2.0,
           stop_atr=8.0, fee=0.0005, slip=0.0003, cooldown_bars=24,
           initial_equity=1000.0)

state = {"mode": "long", "entry_price": 100.0, "entry_time": 0,
         "stop_price": 95.0, "cooldown_until": 0, "equity": 1000.0,
         "trades": 0, "last_email": 0, "last_bar_t": 0, "last_filter_bar": 0,
         "pending": None, "pending_bar": 0, "pending_atr": 0.0,
         "last_run": 0, "last_price": 100.0}

L.load_state = lambda: dict(state)
saved = []
L.save_state = lambda s: saved.append(dict(s))

s, snap, events = L.step(cfg, provider=provider, price_provider=price_provider)

stops = [e for e in events if e[0] == "STOP"]
assert stops, f"应当触发止损，但 events={events}"
fill = stops[0][2]
print(f"止损成交价 = {fill}")
print(f"旧代码会成交在 = 95.0（用已收盘那根 open=99 误判无跳空）")
assert abs(fill - 90.0) < 1e-9, f"失败：成交价应为 90（跳空开盘），实际 {fill}"
print("PASS: 止损跳空使用了「正在形成那根」的开盘价 90，而非误用上一根。")
