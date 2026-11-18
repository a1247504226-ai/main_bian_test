#!/usr/bin/env bash
# 一键安装（Linux）：检查环境 -> 注册 cron -> 立即试跑一次
set -u
cd "$(dirname "$0")" || exit 1

TASK_TAG="# BTC_STRATEGY"
FAIL=0

echo "============================================================"
echo "  BTC 趋势策略 · Linux 一键安装"
echo "============================================================"
echo

# ---------------------------------------------------------------- 1. 系统
if [ "$(uname -s)" != "Linux" ]; then
    echo "[错误] 本脚本用于 Linux。Windows 请双击 install.bat"
    exit 1
fi
echo "[1/6] 系统：$(uname -s) $(uname -m)"

# ---------------------------------------------------------------- 2. Python
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        V=$("$c" -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])' 2>/dev/null || echo 0)
        if [ "$V" -ge 308 ]; then
            PY="$c"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "[错误] 需要 Python 3.8 或更高版本"
    echo "       Debian/Ubuntu: sudo apt install -y python3"
    echo "       CentOS/RHEL  : sudo yum install -y python3"
    exit 1
fi
echo "[2/6] Python：$("$PY" --version 2>&1)  ($(command -v "$PY"))"

# ---------------------------------------------------------------- 3. curl
if ! command -v curl >/dev/null 2>&1; then
    echo "[错误] 未找到 curl"
    echo "       币安域名会解析到多个 CDN IP，没有 curl 会频繁握手超时。"
    echo "       Debian/Ubuntu: sudo apt install -y curl"
    echo "       CentOS/RHEL  : sudo yum install -y curl"
    exit 1
fi
echo "[3/6] curl：$(curl --version 2>/dev/null | head -1)"

# ---------------------------------------------------------------- 4. 配置
if [ ! -f config.json ]; then
    echo "[错误] 当前目录缺少 config.json"
    exit 1
fi
echo "[4/6] config.json 已就绪"

# 检查 exchange 配置（只提示，不阻断）
if "$PY" -c "import json;c=json.load(open('config.json',encoding='utf-8'));e=c.get('exchange',{});print(e.get('testnet'))" 2>/dev/null | grep -q True; then
    echo "       下单：测试网（testnet=true）"
else
    echo "       下单：未启用或主网 —— 请确认 config.json 的 exchange 段"
fi

# ---------------------------------------------------------------- 5. 权限
chmod +x run_with_retry.sh stop.sh status.sh 2>/dev/null || true
echo "[5/6] 脚本执行权限已设置"

# ---------------------------------------------------------------- 6. cron
CRON_LINE="2 * * * * cd $(pwd) && PATH=/usr/local/bin:/usr/bin:/bin ./run_with_retry.sh >> cron.log 2>&1 ${TASK_TAG}"
if ( crontab -l 2>/dev/null | grep -v "${TASK_TAG}" ; echo "${CRON_LINE}" ) | crontab - 2>/dev/null; then
    echo "[6/6] cron 已注册（每小时 :02）"
    crontab -l 2>/dev/null | grep "${TASK_TAG}"
else
    echo "[警告] cron 注册失败。可手动执行："
    echo "       crontab -e"
    echo "       加入：${CRON_LINE}"
    FAIL=1
fi

echo
echo "------------------------------------------------------------"
echo "  立即试跑一次（会真实拉行情，可能发邮件）"
echo "------------------------------------------------------------"
if ./run_with_retry.sh; then
    echo "[OK] 运行成功"
    tail -3 monitor.log 2>/dev/null
else
    echo "[失败] 请查看 monitor.log"
    tail -10 monitor.log 2>/dev/null
    FAIL=1
fi

echo
echo "============================================================"
if [ "$FAIL" -eq 0 ]; then
    echo "  安装完成 ✓"
else
    echo "  安装完成，但有警告（见上）"
fi
echo "============================================================"
echo "常用命令："
echo "  查看策略与交易所状态 : $PY trade.py status"
echo "  让持仓对齐策略信号   : $PY trade.py sync"
echo "  下单自测             : $PY test_order.py long 150 2"
echo "  查看运行状态         : ./status.sh"
echo "  停止定时任务         : ./stop.sh"
echo "  临时停跑（不删 cron）: touch STOP.flag   恢复：rm STOP.flag"
echo
exit "$FAIL"
