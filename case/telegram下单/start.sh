#!/usr/bin/env bash
# 一键启动（Linux / macOS / VPS）
# 用法：./start.sh          交互菜单
#       ./start.sh 1        直接跑测试网 dry-run
#       ./start.sh 2        测试网真实下单
#       ./start.sh 4        检查配置

set -u
cd "$(dirname "$0")"

echo "=================================================="
echo "   币安 TG 跟单机器人"
echo "=================================================="
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 找不到 python3"
    exit 1
fi

if [ ! -f ".env.bot" ]; then
    echo "[错误] 缺少 .env.bot"
    echo "       请复制 .env.bot.example 为 .env.bot 并填好密钥"
    exit 1
fi

# 优先用 venv，其次 python3，最后 python（Git Bash 里通常只有 python）
if [ -x "venv/bin/python" ]; then
    PY="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[错误] 找不到 python3 / python"
    exit 1
fi

if ! $PY -c "import requests, yaml, dotenv" >/dev/null 2>&1; then
    echo "[提示] 首次运行，正在安装依赖..."
    $PY -m pip install -q -r requirements.txt || { echo "[错误] 依赖安装失败"; exit 1; }
fi

CHOICE="${1:-}"
if [ -z "$CHOICE" ]; then
    echo "选择运行模式："
    echo
    echo "  [1] 测试网 + 只打印      最安全，验证信号解析"
    echo "  [2] 测试网 + 真实下单    虚拟资金，可放心测"
    echo "  [3] 回放历史消息         离线验证解析和下单逻辑"
    echo "  [4] 检查配置             看环境/密钥/余额"
    echo "  [5] 主网 + 真实下单      真实资金，谨慎"
    echo
    read -r -p "请输入序号 [1]: " CHOICE
    CHOICE="${CHOICE:-1}"
fi

case "$CHOICE" in
  1) echo "=== 测试网 + 只打印 ==="; $PY trader.py ;;
  2) echo "=== 测试网 + 真实下单 ==="; $PY trader.py --live ;;
  3)
     read -r -p "历史消息文件路径: " F
     [ -z "$F" ] && F="telegram_history_10000.txt"
     [ -f "$F" ] || { echo "[错误] 文件不存在: $F"; exit 1; }
     $PY trader.py --replay "$F" --limit 50 ;;
  4) $PY main.py check ;;
  5)
     echo
     echo "##################################################"
     echo "  警告：即将用【真实资金】在币安主网下单"
     echo "##################################################"
     read -r -p "输入 YES 继续: " C
     [ "$C" = "YES" ] || { echo "已取消"; exit 0; }
     $PY trader.py --mainnet --live ;;
  *) echo "无效选择"; exit 1 ;;
esac

echo
echo "=================================================="
echo "程序已退出"
echo "=================================================="
