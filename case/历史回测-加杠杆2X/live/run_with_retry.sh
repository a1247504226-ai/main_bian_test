#!/usr/bin/env bash
# cron 实际执行的入口：失败自动重试 3 次（间隔 30 秒）
set -u
cd "$(dirname "$0")" || exit 1

# 停用开关：存在 STOP.flag 时直接跳过（删掉该文件即恢复）
if [ -f STOP.flag ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已停用：检测到 STOP.flag，跳过本轮" >> monitor.log
    exit 0
fi

# 选择 Python：优先 .venv，其次 python3 / python
PYEXE=""
if [ -x ".venv/bin/python" ]; then
    PYEXE=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYEXE="python3"
elif command -v python >/dev/null 2>&1; then
    PYEXE="python"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 未找到 Python，请先运行 ./install.sh" >> monitor.log
    exit 1
fi

MAXTRY=3
N=0
while [ "$N" -lt "$MAXTRY" ]; do
    N=$((N + 1))
    if "$PYEXE" live_monitor.py --once; then
        [ "$N" -gt 1 ] && echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 $N 次尝试后成功" >> monitor.log
        exit 0
    fi
    if [ "$N" -lt "$MAXTRY" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 $N 次运行失败，30 秒后重试..." >> monitor.log
        sleep 30
    fi
done
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已重试 $MAXTRY 次仍失败，放弃本轮" >> monitor.log
exit 1
