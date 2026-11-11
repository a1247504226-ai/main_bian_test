#!/usr/bin/env bash
# 运行状态速查：cron / 停用开关 / 最近日志 / 交易所持仓
set -u
cd "$(dirname "$0")" || exit 1

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
fi

echo "=== 定时任务 ==="
if crontab -l 2>/dev/null | grep -q "# BTC_STRATEGY"; then
    crontab -l 2>/dev/null | grep "# BTC_STRATEGY"
else
    echo "(未注册定时任务)"
fi

echo
echo "=== 停用开关 ==="
if [ -f STOP.flag ]; then
    echo "STOP.flag 存在 -> 已停跑（删除该文件恢复）"
else
    echo "无（正常运行）"
fi

echo
echo "=== 最近 5 行日志 ==="
tail -5 monitor.log 2>/dev/null || echo "(无 monitor.log)"

echo
echo "=== 策略与交易所 ==="
if [ -n "$PY" ]; then
    "$PY" trade.py status 2>&1 | tail -12
else
    echo "(未找到 Python)"
fi
