"""
配置加载：.env 存密钥，config.yaml 存策略与风控参数
环境变量优先级高于 yaml（方便在 systemd 里覆盖）
"""
import os
from dataclasses import dataclass
from typing import List, Optional

import yaml

try:
    from dotenv import load_dotenv
    # 优先加载 bot 专用 .env.bot，避免和同目录其他程序（signal_executor 等）共用密钥
    _here = os.path.dirname(os.path.abspath(__file__))
    _loaded = False
    for _name in (".env.bot", ".env"):
        _p = os.path.join(_here, _name)
        if os.path.exists(_p):
            load_dotenv(_p, override=False)
            _loaded = True
            break
    if not _loaded:
        load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    # ---- 测试网密钥（testnet.binancefuture.com）----
    api_key: str = ""
    api_secret: str = ""
    # ---- 主网密钥（fapi.binance.com，真实资金）----
    mainnet_api_key: str = ""
    mainnet_api_secret: str = ""
    # ---- 环境开关 ----
    testnet: bool = True                 # True=测试网（默认），False=主网
    dry_run: bool = True
    # 下单
    leverage: int = 3                    # 下单杠杆
    margin_pct: float = 0.50             # 单笔保证金占权益比例
    provisional_sl_pct: float = 5.0      # 兜底止损百分比
    provisional_tp_pct: float = 7.0      # 兜底止盈百分比
    # 风控
    max_leverage: int = 3
    max_position_pct: float = 1.50       # 单笔名义敞口上限
    max_margin_pct: float = 0.50         # 单笔保证金上限
    max_total_margin_pct: float = 1.00   # 总保证金上限（1.00=允许满仓两仓）
    max_open_positions: int = 2
    max_symbol_exposure_pct: float = 1.50
    daily_loss_limit_pct: float = 0.20
    max_consecutive_losses: int = 3
    min_order_notional: float = 5.0
    allowed_symbols: Optional[List[str]] = None
    # 信号文本含这些词就跳过不开仓（这些票需要人工补仓/轻仓判断，自动化跟不了）
    skip_keywords: Optional[List[str]] = None
    # 挂单（限价）信号是否跳过。频道发「挂单多 分批进场：3.13-3.08」时，
    # 市价开仓会开在当前价、偏离区间，默认跳过
    skip_limit_orders: bool = True
    # 同币反向信号是否自动反手（先平旧仓再开新仓）。
    # false = 只告警跳过，避免误判导致双向亏损
    allow_reverse: bool = False
    # 告警
    tg_token: str = ""
    tg_chat_id: str = ""
    # 邮件通知（余额跨档提醒）
    smtp_server: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""
    smtp_max_retries: int = 3
    balance_alert_threshold: float = 2000.0   # 余额超过这个值开始提醒
    balance_alert_step: float = 2000.0        # 每跨一个步长发一次
    # TG 频道监听（Telethon）
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone: str = ""
    tg_channel: str = ""
    tg_session: str = "tg_session"
    tg_proxy: str = ""           # 形如 http://127.0.0.1:7897，留空表示直连
    tpsl_ttl_sec: int = 90        # 止盈止损只收到一半时的等待秒数
    # 运维
    state_dir: str = "state"
    log_dir: str = "logs"
    health_port: int = 8080

    # ---------------- 环境相关（只读属性）----------------
    @property
    def active_api_key(self) -> str:
        """按 testnet 开关自动选对应环境的 key"""
        return self.api_key if self.testnet else self.mainnet_api_key

    @property
    def active_api_secret(self) -> str:
        return self.api_secret if self.testnet else self.mainnet_api_secret

    @property
    def base_url(self) -> str:
        return ("https://testnet.binancefuture.com" if self.testnet
                else "https://fapi.binance.com")

    @property
    def env_name(self) -> str:
        return "测试网 TESTNET" if self.testnet else "主网 MAINNET（真实资金）"

    def missing_key_hint(self) -> str:
        """当前环境缺密钥时给提示"""
        if self.testnet:
            return "缺少测试网密钥：BINANCE_API_KEY / BINANCE_API_SECRET"
        return ("缺少主网密钥：BINANCE_MAINNET_API_KEY / BINANCE_MAINNET_API_SECRET"
                "（主网和测试网是两套 key，不能混用）")


def load_config(path: str = "config.yaml") -> Config:
    cfg = Config()

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # 只认 dataclass 真实字段：property（如 active_api_key）没有 setter，
        # 用户误写进 yaml 会直接 AttributeError 崩溃
        from dataclasses import fields as _dc_fields
        valid = {f.name for f in _dc_fields(Config)}
        # 布尔字段强制转换：yaml 里写 "false"（带引号）会被当成真值
        bool_fields = {"testnet", "dry_run"}
        for k, v in data.items():
            if k not in valid:
                continue
            if k in bool_fields:
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    v = bool(v)
            setattr(cfg, k, v)

    # 环境变量覆盖（字符串）
    env_map = {
        "BINANCE_API_KEY": "api_key",
        "BINANCE_API_SECRET": "api_secret",
        "BINANCE_MAINNET_API_KEY": "mainnet_api_key",
        "BINANCE_MAINNET_API_SECRET": "mainnet_api_secret",
        "TG_TOKEN": "tg_token",
        "TG_CHAT_ID": "tg_chat_id",
        "TG_API_HASH": "tg_api_hash",
        "TG_PHONE": "tg_phone",
        "TG_CHANNEL": "tg_channel",
        "TG_SESSION": "tg_session",
        "TG_PROXY": "tg_proxy",
        "SMTP_SERVER": "smtp_server",
        "SMTP_USER": "smtp_user",
        "SMTP_PASSWORD": "smtp_password",
        "SMTP_TO": "smtp_to",
    }
    for env, attr in env_map.items():
        if os.getenv(env):
            setattr(cfg, attr, os.getenv(env))

    # 环境变量覆盖（布尔/数值）
    def _bool(x):
        return x.lower() in ("1", "true", "yes", "on")

    for env, attr, cast in [
        ("TESTNET", "testnet", _bool),
        ("DRY_RUN", "dry_run", _bool),
        ("HEALTH_PORT", "health_port", int),
        ("LEVERAGE", "leverage", int),
        ("MARGIN_PCT", "margin_pct", float),
        ("TG_API_ID", "tg_api_id", int),
        ("SMTP_PORT", "smtp_port", int),
        ("SMTP_MAX_RETRIES", "smtp_max_retries", int),
        ("BALANCE_ALERT_THRESHOLD", "balance_alert_threshold", float),
        ("BALANCE_ALERT_STEP", "balance_alert_step", float),
        ("SKIP_LIMIT_ORDERS", "skip_limit_orders", _bool),
        ("ALLOW_REVERSE", "allow_reverse", _bool),
    ]:
        raw = os.getenv(env)
        # .env 里留空的键（TG_API_ID=）会返回空字符串，不能直接 int('')
        if raw is None or raw.strip() == "":
            continue
        try:
            setattr(cfg, attr, cast(raw.strip()))
        except (ValueError, TypeError) as e:
            raise ValueError(f"环境变量 {env} 值非法: {raw!r} ({e})")

    # 配置自检
    if cfg.margin_pct <= 0 or cfg.margin_pct > 1:
        raise ValueError(f"margin_pct 必须在 (0, 1] 之间，当前 {cfg.margin_pct}")
    if cfg.leverage < 1:
        raise ValueError(f"leverage 必须 >= 1，当前 {cfg.leverage}")
    if cfg.leverage > cfg.max_leverage:
        raise ValueError(
            f"leverage({cfg.leverage}) 大于 max_leverage({cfg.max_leverage})，会被风控全部拒单")

    # 名义敞口上限自动跟随「杠杆 × 保证金」，
    # 否则改了 leverage 忘记改上限，会导致每一单都被风控拒绝
    derived_notional = cfg.margin_pct * cfg.leverage
    if cfg.max_position_pct + 1e-9 < derived_notional:
        cfg.max_position_pct = derived_notional
    if cfg.max_symbol_exposure_pct + 1e-9 < derived_notional:
        cfg.max_symbol_exposure_pct = derived_notional
    if cfg.max_margin_pct + 1e-9 < cfg.margin_pct:
        raise ValueError(
            f"max_margin_pct({cfg.max_margin_pct}) 小于 margin_pct({cfg.margin_pct})，"
            f"每一单都会被风控拒绝")

    # 日亏限制必须 >= 单笔止损，否则第一笔亏损就熔断，当天再也开不了仓
    single_loss_pct = derived_notional * cfg.provisional_sl_pct / 100
    if cfg.daily_loss_limit_pct + 1e-9 < single_loss_pct:
        raise ValueError(
            f"daily_loss_limit_pct({cfg.daily_loss_limit_pct*100:.1f}%) 小于单笔止损 "
            f"({single_loss_pct*100:.1f}% = {derived_notional:.2f}名义 × {cfg.provisional_sl_pct}%)，"
            f"第一笔止损就会触发熔断。建议改成 {single_loss_pct*2.5:.2f} 左右")

    # 连亏熔断也要能容纳至少 2 笔止损，否则等于每亏一笔就停
    if cfg.max_consecutive_losses < 2:
        raise ValueError(
            f"max_consecutive_losses({cfg.max_consecutive_losses}) 太小，"
            f"至少设 2（3x 下每笔止损亏 {single_loss_pct*100:.1f}% 权益）")

    # 环境自检：切到主网必须配主网 key，否则一路报 -2015 很难排查
    if not cfg.testnet and not cfg.mainnet_api_key:
        import logging
        logging.getLogger(__name__).warning(
            "当前是主网模式，但没配置 BINANCE_MAINNET_API_KEY，下单会失败")

    return cfg
