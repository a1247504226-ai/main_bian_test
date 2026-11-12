"""
Telegram 频道监听

用 Telethon（用户账号 MTProto）而不是 Bot API —— 因为 Bot API 只能收到
「bot 自己加入的群」的消息，公开频道加不进去。

首次运行会要求输入手机验证码，登录后 session 文件会保存，之后免登录。

依赖：pip install telethon
需要：https://my.telegram.org 申请 api_id / api_hash
"""
import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import SessionPasswordNeededError
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False
    TelegramClient = None


def resolve_channel(ch):
    """频道标识归一化

    @username / t.me/xxx  → 原样返回字符串
    -1001234567890        → 转成 int（Telethon 只认 int 形式的频道 ID）
    """
    s = str(ch).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


def build_proxy(proxy_url: str):
    """把 http://127.0.0.1:7897 转成 Telethon 需要的 proxy 参数

    支持 http / socks5，留空返回 None（直连）
    """
    if not proxy_url or not proxy_url.strip():
        return None
    from urllib.parse import urlparse
    u = urlparse(proxy_url.strip())
    if not u.hostname:
        return None
    return {
        "proxy_type": (u.scheme or "http").lower(),
        "addr": u.hostname,
        "port": u.port or 7897,
    }


class TgListener:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        channel: str,
        session_path: str = "tg_session",
        dedup_file: str = "state/seen_msgs.json",
        proxy_url: str = "",
    ):
        if not HAS_TELETHON:
            raise RuntimeError(
                "缺少 telethon，请先执行：pip install telethon")
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.channel = channel
        self.session_path = session_path
        self.proxy_url = proxy_url
        self.client: Optional[TelegramClient] = None
        self._seen = self._load_seen(dedup_file)
        self._seen_file = dedup_file

    # ---------------- 去重 ----------------
    def _load_seen(self, path):
        import json, os
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

    def _save_seen(self):
        import json, os
        d = os.path.dirname(self._seen_file)
        if d:
            os.makedirs(d, exist_ok=True)
        # 只保留最近 5000 条，避免无限增长
        recent = list(self._seen)[-5000:]
        with open(self._seen_file, "w", encoding="utf-8") as f:
            json.dump(recent, f)

    # ---------------- 主循环 ----------------
    async def run(self, on_message: Callable[[str, int], Awaitable[None]]):
        """on_message(text, msg_id) 收到新消息时调用"""
        proxy = build_proxy(self.proxy_url)
        if proxy:
            log.info("使用代理 %s:%s (%s)", proxy["addr"], proxy["port"], proxy["proxy_type"])
        self.client = TelegramClient(
            self.session_path, self.api_id, self.api_hash,
            proxy=proxy,
            connection_retries=5,
            retry_delay=3,
            timeout=20,
        )

        log.info("连接 Telegram...")
        try:
            await self.client.connect()
            if await self.client.is_user_authorized():
                log.info("session 有效，无需验证码")
            else:
                if not self.phone:
                    raise RuntimeError(
                        "session 未授权，且没配 TG_PHONE，无法登录。\n"
                        f"  1) 确认 {self.session_path}.session 在当前目录"
                        f"（当前工作目录：{os.getcwd()}）\n"
                        "  2) 或在 .env.bot 里填 TG_PHONE=+86... 后重新运行"
                        "（会要求输一次验证码）")
                log.info("session 无效，用手机号重新登录...")
                await self.client.start(phone=self.phone)
        except SessionPasswordNeededError:
            log.error("账号开启了两步验证，请在终端手动输入密码")
            raise

        me = await self.client.get_me()
        log.info("已登录：%s (@%s)", me.first_name, me.username or "-")

        # session 文件等价于账号登录态，权限收紧到 600（Windows 上无效，忽略）
        try:
            import os
            sess = self.session_path
            if not sess.endswith(".session"):
                sess += ".session"
            if os.path.exists(sess):
                os.chmod(sess, 0o600)
        except Exception:
            pass

        # 解析频道
        target = resolve_channel(self.channel)
        try:
            entity = await self.client.get_entity(target)
        except Exception as e:
            log.error("找不到频道 %s: %s", target, e)
            raise
        title = getattr(entity, "title", target)
        log.info("开始监听：%s", title)

        @self.client.on(events.NewMessage(chats=entity))
        async def _handler(event):
            text = (event.message.message or "").strip()
            msg_id = event.message.id
            if not text:
                return
            if msg_id in self._seen:
                return
            self._seen.add(msg_id)
            try:
                await on_message(text, msg_id)
            except Exception as e:
                log.exception("处理消息异常: %s", e)
            finally:
                # 落盘去重记录：太稀疏的话进程被杀会重复处理旧消息
                if len(self._seen) % 5 == 0:
                    self._save_seen()

        # 频道经常编辑消息修正止盈止损，不监听的话会一直用错的价位
        @self.client.on(events.MessageEdited(chats=entity))
        async def _edited(event):
            text = (event.message.message or "").strip()
            if not text:
                return
            log.info("消息 #%d 被编辑，重新解析", event.message.id)
            try:
                await on_message(text, event.message.id)
            except Exception as e:
                log.exception("处理编辑消息异常: %s", e)

        log.info("监听已启动，等待消息...（Ctrl+C 退出）")
        try:
            await self.client.run_until_disconnected()
        finally:
            self._save_seen()
            log.info("去重记录已保存，共 %d 条", len(self._seen))

    async def stop(self):
        if self.client:
            await self.client.disconnect()


def check_ready(api_id, api_hash, channel) -> str:
    """部署前自检，返回问题描述，空字符串表示 OK"""
    if not HAS_TELETHON:
        return "未安装 telethon：pip install telethon"
    if not api_id or not api_hash:
        return "缺少 api_id / api_hash（去 https://my.telegram.org 申请）"
    if not channel:
        return "缺少 TG_CHANNEL（频道 @用户名 或 链接）"
    return ""
