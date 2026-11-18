#!/usr/bin/env bash
# 币安 TG 跟单机器人 —— Linux VPS 一键部署
#
# 用法：
#   sudo bash deploy/setup.sh
#
# 干净 Ubuntu 20.04 / 22.04 / 24.04 均可直接运行，脚本会自己装齐依赖。

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE="tradebot"

echo "=================================================="
echo "  Binance TG Trader - Setup"
echo "=================================================="
echo "  项目目录: $APP_DIR"
echo "  运行用户: $RUN_USER"
echo

# ---------- 1. 权限 ----------
if [ "$EUID" -ne 0 ]; then
    echo "[错误] 需要 root：sudo bash deploy/setup.sh"
    exit 1
fi

# 非 ASCII 路径会让 systemd 的 WorkingDirectory 出问题
if printf '%s' "$APP_DIR" | LC_ALL=C grep -q '[^ -~]'; then
    echo "[警告] 路径含非 ASCII 字符，systemd 可能无法正确工作"
    echo "       建议移到 /home/$RUN_USER/bot"
    read -r -p "继续？(y/N) " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ---------- 2. 系统依赖 ----------
echo "==> 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || { echo "[错误] apt-get update 失败，检查网络或换源"; exit 1; }
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    ufw chrony logrotate ca-certificates \
    || { echo "[错误] 依赖安装失败"; exit 1; }

# ---------- 3. Python 版本 ----------
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "==> Python 版本: $PY_VER"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "[错误] 需要 Python 3.9+，当前 $PY_VER"
    echo "       Ubuntu 20.04 默认 3.8，需要装 3.9+ 或用 deadsnakes PPA"
    exit 1
fi

# ---------- 4. 时间同步 ----------
echo "==> 启用时间同步（签名偏差 >5s 会 -1021）"
timedatectl set-timezone Asia/Shanghai >/dev/null 2>&1 || true
systemctl enable --now chrony >/dev/null 2>&1 || true

# ---------- 5. venv + 依赖 ----------
echo "==> 创建虚拟环境"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/venv" \
        || { echo "[错误] venv 创建失败（可能需要 python3-venv 包）"; exit 1; }
fi

echo "==> 安装 Python 依赖"
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
    || { echo "[错误] 依赖安装失败"; exit 1; }

mkdir -p "$APP_DIR/state" "$APP_DIR/logs"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

# ---------- 6. 配置检查 ----------
NEED_CONFIG=0
if [ ! -f "$APP_DIR/.env.bot" ]; then
    echo "==> 未找到 .env.bot，从模板创建"
    cp "$APP_DIR/.env.bot.example" "$APP_DIR/.env.bot"
    chown "$RUN_USER:$RUN_USER" "$APP_DIR/.env.bot"
    NEED_CONFIG=1
fi
chmod 600 "$APP_DIR/.env.bot"

# session 文件（从 Windows 传过来的登录态）
if [ ! -f "$APP_DIR/telegram_monitor.session" ] && [ ! -f "$APP_DIR/tg_session.session" ]; then
    echo "[提示] 没找到 .session 文件"
    echo "       把 Windows 上的 telegram_monitor.session 传到 $APP_DIR/"
    echo "       或者填 TG_PHONE 后首次运行手动登录"
fi

# 代理提示
if grep -q "^TG_PROXY=http" "$APP_DIR/.env.bot" 2>/dev/null; then
    echo "[注意] .env.bot 配了 TG_PROXY"
    echo "       海外 VPS 直连 TG，建议改成 TG_PROXY=（清空）"
fi

if [ "$NEED_CONFIG" = "1" ]; then
    echo
    echo "=================================================="
    echo "  请先填 .env.bot，然后重新运行本脚本"
    echo "=================================================="
    echo "  vi $APP_DIR/.env.bot"
    echo
    echo "  必填: BINANCE_API_KEY / BINANCE_API_SECRET"
    echo "        TG_API_ID / TG_API_HASH / TG_CHANNEL / TG_SESSION"
    echo "  可选: SMTP_* （邮件提醒）"
    exit 0
fi

# ---------- 7. 防火墙 ----------
echo "==> 配置防火墙（放行 SSH + 心跳端口）"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 8080/tcp >/dev/null
ufw --force enable >/dev/null

# ---------- 8. systemd ----------
echo "==> 安装 systemd 服务"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Binance TG Auto Trader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment="PYTHONUNBUFFERED=1"
ExecStart=$APP_DIR/venv/bin/python trader.py
Restart=always
RestartSec=5
StartLimitBurst=50
StartLimitIntervalSec=600

NoNewPrivileges=true
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

# ---------- 9. 日志轮转 ----------
cat > "/etc/logrotate.d/${SERVICE}" <<EOF
$APP_DIR/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ---------- 10. 完成 ----------
echo
echo "=================================================="
echo "  部署完成"
echo "=================================================="
echo
echo "下一步："
echo
echo "  1. 先手动跑一次（首次要输 TG 验证码，systemd 里没法交互）"
echo "     cd $APP_DIR"
echo "     sudo -u $RUN_USER ./venv/bin/python trader.py"
echo "     看到「监听已启动」后 Ctrl+C"
echo
echo "  2. 启动服务"
echo "     sudo systemctl start $SERVICE"
echo
echo "  3. 看日志"
echo "     sudo journalctl -u $SERVICE -f"
echo
echo "  4. 健康检查"
echo "     curl -s localhost:8080/health"
echo
echo "=================================================="
