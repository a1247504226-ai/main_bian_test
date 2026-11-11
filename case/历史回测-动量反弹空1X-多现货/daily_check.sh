#!/usr/bin/env bash
# 手动跑一次每日检测（等价于 Windows 的「每日策略检测.bat」）
# 参数原样传给 btc_monitor.py，例如：
#   bash daily_check.sh --no-mail     只检测不发邮件
#   bash daily_check.sh --live        允许真实下单（还需 config 里 enabled=true）
#   bash daily_check.sh --refresh     强制重新下载行情
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "[ERROR] 找不到 python3，请先安装"
    exit 1
fi
exec "$PY" btc_monitor.py "$@"
