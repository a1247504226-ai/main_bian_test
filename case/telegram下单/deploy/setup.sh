#!/usr/bin/env bash
# 币安 TG 跟单机器人 —— Linux VPS 一键部署
#
# 用法：
#   sudo bash deploy/setup.sh
#
# 做的事：
#   1. 装系统依赖（python3-venv / ufw / chrony / logrotate）
#   2. 建 venv 并装 Python 依赖
#   3. 检查 .env.bot（没有就从模板复制并提示填写）
#   4. 配防火墙（只放行 SSH + 心跳端口）
#   5. 装 systemd 服务 + 日志轮转
#
# 不做的事：
#   · 不会自动启动服务（Telethon 首次登录要输验证码，必须手动跑一次）
#   · 不会修改你的 SSH 配置

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE="tradebot"

echo "=================================================="
echo "  币安 TG 跟单机器人 · 部署"
echo "=================================================="
echo "  项目目录: $APP_DIR"
echo "  运行用户: $RUN_USER"
echo

if [ "$EUID" -ne 0 ]; then
    echo "[错误] 需要 root 权限：sudo bash deploy/setup.sh"
    exit 1
fi

# 中文/非 ASCII 路径会让 systemd 的 WorkingDirectory 出问题
# 用 LC_ALL=C + 字节范围判断，比 grep -P 兼容性好（部分系统没编译 PCRE）
if printf '%s' "$APP_DIR" | LC_ALL=C grep -q '[^ -~]'; then
    echo "[警告] 路径含非 ASCII 字符，systemd 可能无法正确工作"
    echo "       建议把项目移到 /home/$RUN_USER/bot 再运行本脚本"
    read -r -p "继续？(y/N) " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

echo "==> 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ufw fail2ban chrony logrotate

echo "==> 启用时间同步（签名对时间敏感，偏差 >5s 会 -1021）"
systemctl enable --now chrony >/dev/null 2>&1 || true

echo "==> 创建虚拟环境并安装依赖"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> 准备目录"
mkdir -p "$APP_DIR/state" "$APP_DIR/logs"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

# .env.bot
if [ ! -f "$APP_DIR/.env.bot" ]; then
    echo "==> 未找到 .env.bot，从模板创建"
    cp "$APP_DIR/.env.bot.example" "$APP_DIR/.env.bot"
    chown "$RUN_USER:$RUN_USER" "$APP_DIR/.env.bot"
    chmod 600 "$APP_DIR/.env.bot"
    echo
    echo "=================================================="
    echo "  请先编辑 .env.bot 填入密钥，然后重新运行本脚本"
    echo "=================================================="
    echo "  vi $APP_DIR/.env.bot"
    echo
    echo "  必填：BINANCE_API_KEY / BINANCE_API_SECRET"
    echo "        TG_API_ID / TG_API_HASH / TG_PHONE / TG_CHANNEL"
    echo "  可选：SMTP_* （余额邮件提醒）"
    exit 0
fi
chmod 600 "$APP_DIR/.env.bot"

echo "==> 配置防火墙（只放行 SSH 和心跳端口）"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 8080/tcp >/dev/null
ufw --force enable >/dev/null

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

echo "==> 配置日志轮转"
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

echo
echo "=================================================="
echo "  部署完成"
echo "=================================================="
echo
echo "下一步（重要）："
echo
echo "  1. 先手动登录 Telegram（需要输手机验证码，systemd 里没法交互）"
echo "     cd $APP_DIR"
echo "     sudo -u $RUN_USER ./venv/bin/python trader.py"
echo "     （看到「监听已启动」后 Ctrl+C 退出）"
echo
echo "  2. 启动服务"
echo "     sudo systemctl start $SERVICE"
echo
echo "  3. 看运行日志"
echo "     sudo journalctl -u $SERVICE -f"
echo
echo "  4. 健康检查"
echo "     curl -s localhost:8080/health"
echo
echo "建议："
echo "  · 编辑 /etc/ssh/sshd_config 设 PasswordAuthentication no"
echo "  · 把 ufw 的 22 端口限制成你的固定 IP：ufw allow from <你的IP> to any port 22"
echo "=================================================="
