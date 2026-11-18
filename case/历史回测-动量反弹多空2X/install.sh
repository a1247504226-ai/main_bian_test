#!/usr/bin/env bash
# =============================================================================
#  BTC 动量策略 · Linux 一键安装
#
#  用法：
#     bash install.sh              # 安装每天自动检测（只发邮件）
#     bash install.sh --uninstall  # 卸载定时任务
#
#  做的事：
#     1. 检查 python3 和必要文件
#     2. 把 monitor_config.json 权限设成 600（里面有 API Key）
#     3. 按「UTC 00:02」换算成本机时区的 cron 时间并注册
#     4. 跑一次自检（不发邮件）
#
#  ⚠ 时区很关键：币安日线是 UTC 00:00 收盘。脚本会在 UTC 00:02 触发，
#    如果你的机器是 UTC 时区，cron 会写成 0:02；如果是北京时区，写成 8:02。
#    写死 8:02 在 UTC 机器上会晚 8 小时，成交价就不是开盘价了。
# =============================================================================
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$DIR/monitor_config.json"
CRON_TAG="BTC_Strategy_DailyCheck"
PY="$(command -v python3 || command -v python || true)"

say() { printf '%s\n' "$*"; }
hr()  { printf '%s\n' "================================================================================"; }

# ---------------------------------------------------------------- 卸载
if [ "${1:-}" = "--uninstall" ]; then
    hr
    say " 卸载 BTC 策略定时任务"
    hr
    if ! command -v crontab >/dev/null 2>&1; then
        say " ✘ 这台机器没有 crontab 命令"
        exit 1
    fi
    BEFORE="$(crontab -l 2>/dev/null | wc -l)"
    ( crontab -l 2>/dev/null | grep -v "$CRON_TAG" ) | crontab - || true
    AFTER="$(crontab -l 2>/dev/null | wc -l)"
    say " ✔ 已移除（crontab 行数 $BEFORE -> $AFTER）"
    say ""
    say " 手动确认：crontab -l"
    exit 0
fi

hr
say " BTC 动量策略 · Linux 安装"
say " 目录 : $DIR"
say " 时间 : $(date '+%Y-%m-%d %H:%M:%S %Z')"
hr
say ""

# ---------------------------------------------------------------- 1. python
say "[1/5] 检查 Python"
if [ -z "$PY" ]; then
    say " ✘ 找不到 python3。请先安装："
    say "     Ubuntu/Debian : sudo apt install -y python3"
    say "     CentOS/RHEL   : sudo yum install -y python3"
    say "     或参考 https://www.python.org/downloads/"
    exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?")"
say " ✔ $PY（$PYVER）"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)'; then
    say " ✘ 需要 Python 3.6 以上"
    exit 1
fi

# ---------------------------------------------------------------- 2. 文件
say ""
say "[2/5] 检查文件完整性"
MISSING=0
for f in btc_monitor.py btc_strategy.py btc_trade.py monitor_config.json; do
    if [ -f "$DIR/$f" ]; then
        say " ✔ $f"
    else
        say " ✘ 缺少 $f"
        MISSING=1
    fi
done
[ -d "$DIR/data" ] || say " ⚠ 没有 data/ 目录（首次运行会自动创建并下载行情）"
if [ "$MISSING" = "1" ]; then
    say ""
    say " ✘ 文件不全，请把整个文件夹完整拷贝过来再运行。"
    exit 1
fi

# ---------------------------------------------------------------- 3. 权限
say ""
say "[3/5] 设置文件权限"
if [ -f "$CFG" ]; then
    chmod 600 "$CFG" 2>/dev/null && say " ✔ monitor_config.json -> 600（只有你自己能读，含 API Key）" \
        || say " ⚠ 改权限失败，请手动 chmod 600 $CFG"
fi
chmod +x "$DIR"/*.sh 2>/dev/null || true
say " ✔ .sh 脚本已设为可执行"

# ---------------------------------------------------------------- 4. cron
say ""
say "[4/5] 注册定时任务"
if ! command -v crontab >/dev/null 2>&1; then
    say " ✘ 这台机器没有 crontab。请安装 cron："
    say "     Ubuntu/Debian : sudo apt install -y cron && sudo systemctl enable --now cron"
    say "     CentOS/RHEL   : sudo yum install -y cronie && sudo systemctl enable --now crond"
    exit 1
fi

# 目标时刻 = UTC 00:02（日线 00:00 UTC 收盘，留 2 分钟等交易所落库）
# 换算成本机时区，避免「Linux 是 UTC 时区却写死 8:02」导致晚 8 小时下单。
CRON_HM="$("$PY" - <<'PYEOF'
import datetime
utc = datetime.datetime.now(datetime.timezone.utc).replace(
    hour=0, minute=2, second=0, microsecond=0)
local = utc.astimezone()
print("%d %d" % (local.minute, local.hour))
PYEOF
)"
CRON_MIN="$(printf '%s' "$CRON_HM" | cut -d' ' -f1)"
CRON_HOUR="$(printf '%s' "$CRON_HM" | cut -d' ' -f2)"

if [ -z "$CRON_MIN" ] || [ -z "$CRON_HOUR" ]; then
    say " ✘ 时区换算失败，改用 8:02（北京时间假设）"
    CRON_MIN=2; CRON_HOUR=8
fi

TZ_NAME="$(date '+%Z')"
UTC_OFFSET="$(date '+%z')"
say " 本机时区 : $TZ_NAME（UTC$UTC_OFFSET）"
say " 触发时刻 : 每天 ${CRON_HOUR}:$(printf '%02d' "$CRON_MIN") 本地时间"
say "            = UTC 00:02（币安日线收盘后 2 分钟）"

CRON_LINE="$CRON_MIN $CRON_HOUR * * * cd '$DIR' && '$PY' btc_monitor.py >> '$DIR/cron.log' 2>&1  # $CRON_TAG"

# 幂等：先删掉所有调用 btc_monitor.py 的旧行，再写新的
BEFORE="$(crontab -l 2>/dev/null | wc -l)"
( crontab -l 2>/dev/null | grep -v "$CRON_TAG" ; echo "$CRON_LINE" ) | crontab -
AFTER="$(crontab -l 2>/dev/null | wc -l)"
say " ✔ 已写入 crontab（行数 $BEFORE -> $AFTER）"
say ""
say " 当前 crontab 里的策略行："
crontab -l 2>/dev/null | grep "$CRON_TAG" | sed 's/^/   /'

# ---------------------------------------------------------------- 5. 自检
say ""
say "[5/5] 自检（只读，不发邮件）"
if ( cd "$DIR" && "$PY" btc_monitor.py --no-mail >/tmp/btc_selfcheck.log 2>&1 ); then
    say " ✔ 自检通过"
    grep -E "最新已收盘|配置：|账户现值|信号" /tmp/btc_selfcheck.log 2>/dev/null | head -6 | sed 's/^/   /'
else
    RC=$?
    say " ✘ 自检失败（退出码 $RC），最后 20 行："
    tail -20 /tmp/btc_selfcheck.log 2>/dev/null | sed 's/^/   /'
    say ""
    say " 常见原因："
    say "   1) monitor_config.json 里 email.sender / password / to 没填全"
    say "   2) 这台机器连不上 data-api.binance.vision（行情源），需要代理"
    say "   3) 配置文件 JSON 语法错（多/少逗号）"
    exit 1
fi

# ---------------------------------------------------------------- 完成
say ""
hr
say " 安装完成"
hr
say ""
say " 每天 ${CRON_HOUR}:$(printf '%02d' "$CRON_MIN") 会自动检测并发邮件。"
say " 日志：$DIR/monitor_log.txt"
say " 定时输出：$DIR/cron.log"
say ""
say " 常用命令："
say "   手动检测一次   : bash daily_check.sh"
say "   手动下单(演练) : bash place_order.sh"
say "   手动下单(真实) : bash place_order.sh --live"
say "   查看定时任务   : crontab -l"
say "   看运行日志     : tail -f $DIR/monitor_log.txt"
say "   卸载定时任务   : bash install.sh --uninstall"
say ""
say " ⚠ 自动下单还需要 monitor_config.json 里："
say "      binance.enabled = true"
say "      binance.auto_live = true      <- 不开的话定时任务只发邮件、不下单"
say "      binance.auto_stop_order = true <- 不开的话加仓后止损价要自己算（会算错）"
say ""
