#!/usr/bin/env bash
# 停止定时任务（只删 cron，不删任何文件）
set -u
cd "$(dirname "$0")" || exit 1

TASK_TAG="# BTC_STRATEGY"

if ! crontab -l 2>/dev/null | grep -q "${TASK_TAG}"; then
    echo "没有找到 BTC 定时任务（可能已经停过）"
    exit 0
fi

echo "即将从 crontab 移除："
crontab -l 2>/dev/null | grep "${TASK_TAG}"
echo

( crontab -l 2>/dev/null | grep -v "${TASK_TAG}" ) | crontab -
if crontab -l 2>/dev/null | grep -q "${TASK_TAG}"; then
    echo "[失败] 移除未成功，请手动执行 crontab -e"
    exit 1
fi

echo "[完成] 定时监测已停止"
echo "       恢复运行：./install.sh"
echo "       只想临时停一轮：touch STOP.flag（恢复：rm STOP.flag）"
