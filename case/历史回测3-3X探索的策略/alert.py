# -*- coding: utf-8 -*-
"""alert.py — 价格触发的风控提醒（止盈/止损/爆仓预警）

定位：**只发提醒，不改变策略、不自动下单**，由你手动操作。
策略本身的止盈止损是「信号触发」的（见每日信号邮件），本脚本是额外的价格保险。

用法：
    python alert.py              # 检查是否触发，触发则发信（每天同类提醒只发一次）
    python alert.py --dry-run    # 只看是否触发，不发信
    python alert.py --force      # 强制发一封（测试用）

建议比信号脚本跑得更勤，例如每小时一次（见 README 的定时任务部分）。

触发条件（在 config.json 的 risk 段配置）：
    stop_mode      trail_atr | fixed_pct | none   —— 止损提醒方式
    trail_atr      移动止损的 ATR 倍数（默认 5.0）
    fixed_stop_pct 固定止损百分比（stop_mode=fixed_pct 时生效）
    take_profit_pct 止盈百分比，0 = 不设
    liq_warn_pct   距爆仓价小于该比例时预警（默认 0.10 = 10%）
    max_alerts_per_day 每天最多发几封，防刷屏
"""
import sys
import common

if __name__ == "__main__":
    sys.exit(common.main_alert(sys.argv[1:], ["A", "B", "C"]))
