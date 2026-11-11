# 币安合约下单程序（两阶段保护 + 滚仓）

一个可以直接跑的下单执行器。**只负责执行，不负责产生信号**——信号从哪来（TG 频道、手工、你自己的策略）由你决定，喂给它就行。

当前默认配置：**2x 杠杆，每仓 50% 保证金（名义 100%）**，开仓先挂 **7% 止盈 + 7% 止损**兜底，收到真实止盈止损后 `amend` 替换。

## 一键运行

**Windows**：双击 `start.bat`

**Linux / VPS**：
```bash
chmod +x start.sh
./start.sh          # 交互菜单
./start.sh 2        # 直接跑测试网真实下单
```

菜单长这样：

```
选择运行模式：

  [1] 测试网 + 只打印      最安全，验证信号解析
  [2] 测试网 + 真实下单    虚拟资金，可放心测
  [3] 回放历史消息         离线验证解析和下单逻辑
  [4] 检查配置             看环境/密钥/余额
  [5] 主网 + 真实下单      真实资金，谨慎
```

脚本会自动做三件事：检查 Python、首次运行装依赖、检查 `.env.bot` 是否存在。出问题会停住不让窗口闪退。

## 核心特性

| 特性 | 说明 |
|------|------|
| **两阶段保护** | 开仓先挂宽兜底止损（默认 7%），收到真实止盈止损后再 `amend` 替换。任何时刻都不裸仓 |
| **幂等下单** | `clientOrderId` 由信号内容哈希生成。同一个信号重复执行不会重复开仓，网络超时重试也安全 |
| **条件单止盈止损** | 交易所端 `STOP_MARKET` / `TAKE_PROFIT_MARKET`，机器挂了止损依然生效 |
| **裸仓保护** | 条件单挂失败会立刻市价平仓，绝不留无保护的仓位 |
| **滚仓基于真实权益** | 每笔都重新读账户余额算仓位，不依赖内存变量，进程重启不跑偏 |
| **精度自适应** | 自动读 `exchangeInfo` 的 `stepSize` / `tickSize`，避免 `-1111` 精度错误 |
| **风控熔断** | 单笔上限、持仓数上限、日亏上限、连亏熔断 |
| **双重安全开关** | `testnet`（测试网）+ `dry_run`（只打印），默认都开着 |
| **心跳监控** | `/health` 端点供 UptimeRobot 之类的免费监控用 |

## 测试网 / 主网切换

**默认走币安测试网**（虚拟资金），不会碰到真钱。

| 方式 | 命令 | 说明 |
|------|------|------|
| 默认 | `python trader.py` | 读 `config.yaml` 的 `testnet: true` → 测试网 |
| 强制测试网 | `python trader.py --testnet` | 忽略配置，强制测试网 |
| 切主网 | `python trader.py --mainnet` | **真实资金**，需配主网 key |

启动时会打印当前环境，一眼能看出在哪个网：

```
================================================================
  环境:   测试网 TESTNET
  地址:   https://testnet.binancefuture.com
  模式:   LIVE 真实下单
  API Key: riufKUh5...u9fb
  杠杆:   2x | 每仓保证金 50% → 名义 100%
================================================================
```

### 两套密钥是分开的

测试网和主网的 API Key **不通用**，`.env.bot` 里分开放：

```bash
# 测试网（默认用这个）
BINANCE_API_KEY=...
BINANCE_API_SECRET=...

# 主网（加 --mainnet 才会用）
BINANCE_MAINNET_API_KEY=
BINANCE_MAINNET_API_SECRET=
```

程序按 `testnet` 开关自动选对应那套，不会混用。

**主网 key 没配时，`--mainnet` 会直接报错退出**，不会用测试网 key 去连主网（那样只会收到一堆 `-2015` 错误，很难排查）。

### 切主网前的检查清单

- [ ] 测试网已经跑通至少 20 笔，下单/改单/平仓都正常
- [ ] `.env.bot` 里 `BINANCE_MAINNET_API_KEY` / `BINANCE_MAINNET_API_SECRET` 已填
- [ ] 主网 API 权限只开「读取 + 合约交易」，**提现关闭**
- [ ] 主网 API 绑定了 VPS 公网 IP 白名单
- [ ] `margin_pct` 从 0.5 降到 0.1-0.2 先用小仓试
- [ ] 邮件通知能收到（余额跨档提醒）

## 止盈止损管理（三阶段）

回测 214 条真实信号得出的最优策略：

```
① 开仓       挂兜底 TP 7% / SL 5%
              ↓
② 收到真实值  止盈挂 TP3（最远目标），保存 TP1/TP2/TP3 全部价位
              ↓
③ TP1 到达    止损提到 TP1 外侧 0.5%，锁定利润，继续持有博 TP3
   TP2 到达    止损再提到 TP2 外侧 0.5%
   TP3 到达    止盈单自动成交
              ↓
④ 离场通知    市价平仓（双保险，防止条件单没触发）
```

### 为什么止盈挂 TP3 而不是 TP1

历史数据：**79% 能到 TP1，57% 能继续到 TP3**。挂 TP1 会过早落袋，挂 TP3 吃到最大利润。

### 为什么到 TP1 要把止损提上来

| 策略 | 每笔期望 |
|------|---------|
| 挂 TP3 不动 | +8.13% |
| 到 TP1 移止损到 TP1 | **+10.70%** |
| 到 TP1 直接平仓 | +6.21% |

到 TP1 后只有 72% 会继续走到 TP3，剩下 28% 会回落。把止损提上来，回落的单子也能吃 TP1 的利润，而不是吐回去变成止损。

### TP1/TP2 到达不平仓

频道发「TP1 止盈减仓」「TP2 已抵达」只是通知减仓，程序**不会平仓**，只把止损提上来。

### 只有这些词才平仓

`离场` / `平仓` / `止损了` / `打止损` / `全平` / `出局`

`止盈` / `TP1` / `TP2` **不触发平仓**。

## 两阶段保护流程

频道发信号的节奏是「先发方向，1-2 分钟后再发止盈止损」，程序按这个节奏设计：

```
① 收到「现价空 #BTCUSDT」
   ↓
   python main.py open --symbol BTCUSDT --side short --entry 83500
   ↓
   立即市价开仓 + 挂 7% 止盈 + 7% 止损兜底
   空单示例：entry 83500 → TP 77655 (-7%) / SL 89345 (+7%)
   状态记为「待更新」，写入 state/pending_protection.json

② 收到「止盈：82000 止损：85000」
   ↓
   python main.py amend --symbol BTCUSDT --tp 82000 --sl 85000
   ↓
   撤掉兜底单 → 挂真实止盈止损，状态清除
```

**为什么兜底要 7% 这么宽：** 兜底的作用是防极端行情，不是执行策略。真实止盈止损通常 2-10%，7% 大致落在中间，不会太早误触发；等真实值到了再收紧。

**兜底期间的风险：** 这 1-2 分钟里仓位只有 7% 的保护。如果价格瞬间反向 6%，兜底还没触发而真实止损本该触发了——这是这个流程无法消除的代价，只能靠控制单笔仓位来限制。

**忘了 amend 怎么办：**

```bash
python main.py pending              # 查看哪些仓位还在等更新
python main.py pending --stale-minutes 5   # 标出超 5 分钟没更新的
```

超过 10 分钟没更新的仓位会在报告里标 ⚠️。也可以把它挂进 cron 定时检查。

## 邮件通知（余额跨档提醒）

余额每跨过一个档位就发一封邮件，不会每笔交易都刷。

### 配置

填到 `.env.bot`：

```bash
SMTP_SERVER=smtp.163.com
SMTP_PORT=465
SMTP_USER=your@163.com
SMTP_PASSWORD=授权码          # 163 用授权码，不是登录密码
SMTP_TO=your@qq.com
BALANCE_ALERT_THRESHOLD=2000  # 超过这个值开始提醒
BALANCE_ALERT_STEP=2000       # 每跨一个步长发一封
```

**163 授权码怎么拿**：163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 生成授权码（16 位字母）。

### 测试

```bash
python -c "
from config import load_config
from mailer import Mailer
c = load_config('config.yaml')
m = Mailer(c.smtp_server, c.smtp_port, c.smtp_user, c.smtp_password, c.smtp_to)
print('OK' if m.send_sync('测试', '邮件通道正常') else '失败')
"
```

### 开仓推送

每次开仓成功会自动发一封邮件，内容：

```
币种：BTCUSDT
方向：SHORT
成交价：83500
数量：0.0591
名义价值：4934.85 USDT
保证金：2467.62 USDT（2x）
止损价：89345
止盈价：77655
保护类型：兜底 7%（等真实止盈止损）
```

标题是 `【开仓】BTCUSDT SHORT`，手机上能直接看到。

### 余额提醒触发逻辑

| 余额 | 是否发邮件 |
|------|-----------|
| 1500 / 1999 | 不发（低于阈值） |
| 2100 | 发「突破 2,000 U」 |
| 2200 / 3900 | 不发（还在 2000 档内） |
| 4100 | 发「突破 4,000 U」 |

记录存在 `state/balance_alert.json`，重启不会重复发同一档。

### 已知坑

**Windows 中文主机名会导致 SMTP 崩溃**（`UnicodeEncodeError: ascii codec`）。代码里已用 `local_hostname="localhost"` 规避。Linux VPS 上不存在这个问题。

## 目录结构

```
tradebot/
├── trader.py                # 【自动跟单】TG 监听 → 解析 → 下单 主程序
├── signal_parser.py         # 消息解析器（可单独运行测试）
├── tg_listener.py           # Telethon 频道监听
├── main.py                  # 【手动】CLI 下单入口
├── binance_futures.py       # 币安 API 客户端（自实现签名）
├── executor.py              # 下单执行器（两阶段保护）
├── risk.py                  # 风控
├── notify.py                # Telegram 告警
├── mailer.py                # 邮件通知（余额跨档提醒）
├── config.py                # 配置加载
├── health.py                # 心跳 HTTP 服务
├── config.yaml              # 策略与风控参数
├── .env.bot                 # 密钥（不要提交到 git）
├── signals.example.json     # 批量信号示例
├── requirements.txt
└── deploy/
    ├── setup.sh             # VPS 一键初始化
    └── tradebot.service     # systemd 单元
```

## 两种用法

| 方式 | 命令 | 适用 |
|------|------|------|
| **自动跟单** | `python trader.py` | 监听频道，收到信号自动下单，收到止盈止损自动改单 |
| **手动下单** | `python main.py open ...` | 你自己看消息，手工敲命令 |

下面先说自动跟单，手动用法在后面。

## 自动跟单：TG 频道 → 币安

### 1. 申请 Telegram API

1. 手机/浏览器打开 **https://my.telegram.org**
2. 用**你的 TG 手机号**登录（不是 bot，是真人账号）
3. 点 **API development tools** → 随便填应用名（如 `mybot`）
4. 拿到 **api_id**（数字）和 **api_hash**（32 位字符串）

> 为什么用账号而不是 bot：Bot API 只能收到「bot 自己加入的群」的消息，公开频道加不进去。Telethon 用账号身份，能监听任意公开频道。

### 2. 配置

填到 `.env.bot`（推荐，密钥集中管理）：

```bash
TG_API_ID=12345678
TG_API_HASH=abcdef0123456789abcdef0123456789
TG_PHONE=+8613800138000
TG_CHANNEL=@channel_username
```

`TG_CHANNEL` 三种写法都行：`@用户名` / `https://t.me/xxx` / `-1001234567890`

### 3. 先用回放验证解析（不连 TG、不下单）

```bash
python signal_parser.py        # 单测解析规则
python trader.py --replay 历史消息.txt --limit 30
```

回放会把历史消息按顺序喂给解析器和下单逻辑，`dry_run` 下只打印。正常输出：

```
[007] OPEN    CROSSUSDT    short  tp=None sl=None
      [DRY-RUN][兜底] CROSSUSDT SHORT 48087 @~0.10263 名义 4935U 保证金 2467U
[008] TPSL    -            -      tp=0.1365 sl=0.166
      [DRY-RUN] amend CROSSUSDT SHORT SL 0.166 TP 0.1365
完成：开仓 2，改单 2，忽略 26，失败 0
```

看到「开仓 N，改单 N，失败 0」且 pending 清零，说明链路正常。

### 4. 启动实时监听

```bash
python trader.py
```

**首次运行会要求输入手机验证码**（TG 发给你），登录成功后生成 `tg_session.session` 文件，之后免登录。

> 如果账号开了两步验证，终端会让你输入密码。

### 5. 确认无误后开真实下单

```bash
# 测试网真实下单（先跑这个）
python trader.py --live

# 主网真实下单（确认测试网跑通后再来）
# 1. config.yaml 改 testnet: false
# 2. 换主网 API key
python trader.py --live
```

### 信号匹配逻辑（重要）

频道发信号的节奏是「先方向、1-2 分钟后止盈止损」，而且**止盈止损消息里没有币种名**。程序这样处理：

1. 收到 `#OGN 现价空` → 立即开仓 + 挂 7% 双向兜底
2. 收到 `止盈：0,0269-...` / `止损：0,0301` → 缓存
3. 两个都齐了 → 分配给「最早的待更新仓位」执行 amend

**多个仓位同时等待时，靠时间顺序 FIFO 匹配。** 如果频道一次推多个信号，可能错配——所以建议把 `max_open_positions` 设小（2-3），并在 `pending` 报告里核对。

止盈止损只收到一半时，`tpsl_ttl_sec`（默认 90 秒）后会告警提醒。

### 部署到 VPS 常驻

```bash
# systemd 单元已备好，ExecStart 改成 trader.py
sudo vi /etc/systemd/system/tradebot.service
#   ExecStart=/home/trader/tradebot/venv/bin/python trader.py

sudo systemctl start tradebot
sudo journalctl -u tradebot -f
```

**注意**：Telethon 首次登录需要交互输入验证码，建议先在 VPS 上手动跑一次 `python trader.py` 完成登录，再交给 systemd。session 文件生成后就免登录了。

---

## 手动下单（CLI）

### 1. 安装依赖

```bash
cd tradebot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp .env.example .env
vi .env
```

填三个东西：

- `BINANCE_API_KEY` / `BINANCE_API_SECRET`：币安后台创建，**权限只勾「读取」+「合约交易」，绝对不要勾提现**
- `TG_TOKEN` / `TG_CHAT_ID`：找 @BotFather 建 bot

### 3. 先验证，别急着下单

```bash
# 检查配置和连通性
python main.py check

# dry-run 试一笔（不配 key 也能跑，用虚拟 1000U 演示）
python main.py open --symbol BTCUSDT --side long --entry 81000 --tp 83000 --sl 80500
```

`dry-run` 模式下只会打印，不会真的下单。输出长这样：

```
[DRY-RUN] BTCUSDT LONG 0.0012 @~81000 名义 97.20U | SL 80500.0 TP 83000.0 | coid=OPa83ca9f5...
```

看到 `coid=` 说明幂等 ID 生成正常。

## 命令一览

```bash
python main.py check                  # 配置 / 连通性 / 账户检查
python main.py balance                # 查余额
python main.py positions              # 查持仓

# ① 开仓（只给方向，自动挂 7% 止盈 + 7% 止损兜底）
python main.py open \
  --symbol BTCUSDT --side short --entry 83500 \
  --id tg-20261109-001                # 信号唯一ID，用于幂等
# 可选覆盖：--margin-pct 0.5  --leverage 2  --provisional-sl 7  --provisional-tp 7

# ② 收到真实止盈止损后改单
python main.py amend --symbol BTCUSDT --tp 82000 --sl 85000

# 或者一次给全（跳过兜底，直接挂真实值）
python main.py open \
  --symbol BTCUSDT --side long \
  --entry 81000 --tp 83000 --sl 80500

# 查看还在等更新的仓位
python main.py pending
python main.py pending --stale-minutes 5

# 平仓
python main.py close --symbol BTCUSDT

# 批量执行信号文件
python main.py batch signals.json

# 常驻运行（心跳 + 自检）
python main.py serve
```

**切换真实下单**：命令行加 `--live`，或者把 `config.yaml` 里 `dry_run` 改成 `false`。

## 信号文件格式

`signals.json`：

```json
[
  {
    "symbol": "BTCUSDT",
    "direction": "short",
    "entry": 83500,
    "tp1": 82000,
    "sl": 85000,
    "position_pct": 0.10,
    "signal_id": "tg-20261109-001"
  }
]
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `symbol` | 是 | 币安合约交易对，如 `BTCUSDT` |
| `direction` | 是 | `long` / `short` |
| `entry` | 否 | 参考入场价，不填用当前标记价 |
| `tp1` | 否 | 止盈价。**省略则走兜底模式**，只挂 7% 止损等 amend |
| `sl` | 否 | 止损价。同上 |
| `position_pct` | 否 | 名义价值占权益比例，默认取 `config.yaml` |
| `signal_id` | 否 | 幂等标识，强烈建议填 |
| `provisional_sl_pct` | 否 | 兜底止损百分比，默认 7 |

给了 `tp1` 和 `sl` 就校验价位方向：多单必须 `sl < entry < tp1`，空单必须 `tp1 < entry < sl`，不满足直接拒绝。缺任一个则进入兜底模式，不做方向校验。

## 部署到 Linux VPS

### 一键安装

```bash
# 1. 上传（建议放英文路径，中文路径 systemd 会出问题）
scp -r "telegram下单" root@<VPS_IP>:/root/bot

# 2. 跑安装脚本（装依赖、建 venv、配防火墙、装 systemd、配日志轮转）
ssh root@<VPS_IP>
cd /root/bot
sudo bash deploy/setup.sh
```

脚本会自动做：

- 装 `python3-venv` / `ufw` / `chrony` / `logrotate`
- 建 venv 并装 `requirements.txt`
- 没有 `.env.bot` 就从模板复制并提示你填
- 防火墙只放行 SSH(22) + 心跳(8080)
- 生成 systemd 服务（路径自动适配）+ 日志轮转（保留 14 天）

### 填密钥

```bash
vi .env.bot
```

必填：`BINANCE_API_KEY` / `BINANCE_API_SECRET`、`TG_API_ID` / `TG_API_HASH` / `TG_PHONE` / `TG_CHANNEL`

填完再跑一次 `sudo bash deploy/setup.sh` 让它完成 systemd 安装。

### 首次登录 Telegram（必须手动）

Telethon 登录要输手机验证码，systemd 里没有 TTY，所以**必须先手动跑一次**：

```bash
cd /root/bot
./venv/bin/python trader.py
# 看到「监听已启动，等待消息...」后 Ctrl+C 退出
```

这一步会生成 `tg_session.session`，之后就不用再登录了。

### 启动服务

```bash
sudo systemctl start tradebot
sudo journalctl -u tradebot -f      # 看日志
sudo systemctl status tradebot      # 看状态
curl -s localhost:8080/health       # 健康检查
```

### 监控

UptimeRobot 加一个 HTTP 监控，地址 `http://<VPS_IP>:8080/health`，间隔 5 分钟。进程挂了或卡死超过 120 秒，`/health` 返回 503 会触发告警。

**注意**：8080 端口对公网开放意味着别人也能看到你的持仓状态。介意的话把 ufw 里的 8080 删掉，只在服务器本地 curl。

### 常用运维命令

```bash
sudo systemctl restart tradebot     # 重启
sudo systemctl stop tradebot        # 停止
sudo journalctl -u tradebot --since "1 hour ago"   # 看最近一小时日志
```

## 风控参数

在 `config.yaml` 里调。当前默认是 **2x + 每仓 50% 保证金**：

```yaml
leverage: 2                      # 下单杠杆
margin_pct: 0.50                 # 每仓保证金占权益 → 2x 下名义 100%

max_leverage: 2                  # 杠杆上限
max_position_pct: 1.00           # 单笔名义敞口上限
max_margin_pct: 0.50             # 单笔保证金上限
max_total_margin_pct: 0.95       # 全部持仓保证金合计上限
max_open_positions: 2            # 最多同时持仓数
daily_loss_limit_pct: 0.05       # 当日亏 5% 停开新仓
max_consecutive_losses: 3        # 连亏 3 笔熔断

# 信号文本含这些词就跳过不开仓
skip_keywords:
  - 轻仓      # 建议小仓位的票，仓位不匹配自动跟单
  - 补仓      # 需要人工判断补仓时机的票
```

### 能开几个仓，取决于 margin_pct

| 每仓保证金 | 2x 下名义 | 最多仓数 | 总保证金 | 总名义敞口 |
|-----------|----------|---------|---------|-----------|
| **50%** | 100% | **2 个** | 100% | 200% |
| 45% | 90% | 2 个 | 90% | 180% |
| 30% | 60% | 3 个 | 90% | 180% |
| 25% | 50% | 3 个 | 75% | 150% |
| 15% | 30% | 6 个 | 90% | 180% |

当前默认 `margin_pct: 0.50` + `max_total_margin_pct: 1.00`，**两个仓各占 50% 保证金，合计 100%**，可以开满两仓。

**满仓的含义**：两仓都开满后没有余量开第三个仓，也无法追加保证金。逐仓模式下每仓独立强平（2x 需价格反向 50% 才爆），一个仓爆掉不会连带另一个，但账户权益会直接腰斩。

想留缓冲，把 `margin_pct` 降到 0.45（总 90%）。

熔断后需要人工干预，删掉 `state/daily_stats.json` 或把里面 `halted` 改成 `false`。

## 已知限制

**设计原则：宁可漏单，不可错单。** 下面几条都是这个原则的具体体现。

1. **只支持币安 USDT 本位合约**。`#BTC` 会自动补成 `BTCUSDT`；`BTCUSDC` / `BTCBUSD` 直接拒绝；不存在的币种在下单前就会被拦下。
2. **方向有歧义时拒绝**。一条消息里同时出现「现价空」和「现价多」时不开仓，避免反向开错。
3. **多个待更新仓位时，不带币种名的止盈止损会被跳过**。频道发 `止盈：xxx 止损：xxx` 但没写币种、而你同时有 2 个以上待更新仓位时，程序**不会猜**（FIFO 可能把止损挂到错的币上），而是记日志让你手动 `amend`。
4. **战绩统计消息会被过滤**。`获利：24单 止损：4单` 这类消息里的数字不会被当成价格。
5. **`amend` 有 1-2 秒裸奔窗口**。撤旧单到挂新单之间仓位无保护。币安合约不支持直接修改条件单的 `stopPrice`，只能撤+挂。
6. **`closePosition=true` 的条件单会平掉整个仓位**。要分批止盈（TP1 平一半）得改成带 `quantity` 的 `reduceOnly` 单。
7. **兜底 7% 对宽止损币会偏紧**。真实止损 10% 的币，7% 兜底会提前触发。这类币建议开仓时直接给 `--sl`。
8. **1x 合约做空要付资金费率**。按 0.01%/8h 算，一个月约 0.9%。
9. **测试网和主网是两套账户**。密钥不通用，`.env.bot` 里分开配。
10. **不支持双向持仓模式**。检测到同币种两个方向持仓时会直接拒绝，请先在币安切回单向。

## 上线前的检查清单

- [ ] 测试网跑通至少 20 笔，观察滑点和成交价与信号的偏差
- [ ] API Key 权限确认只有「读取 + 合约交易」，提现是关的
- [ ] VPS 公网 IP 已加入币安 API 白名单
- [ ] `.env` 权限是 600（`chmod 600 .env`）
- [ ] SSH 已禁用密码登录
- [ ] UptimeRobot 监控已配置并测试过告警
- [ ] Telegram 告警能正常收到
- [ ] 主网第一笔用最小资金（比如 50U 权益）

## 免责声明

这是执行工具，不含任何策略。信号本身值不值得跟，需要你自己判断。加密合约交易可能导致全部本金损失，请只投入能承受损失的资金。
