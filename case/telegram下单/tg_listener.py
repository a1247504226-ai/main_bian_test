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
from typing import Awaitable, Callable, Dict, Optional

import signal_parser as sp

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import SessionPasswordNeededError
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False
    TelegramClient = None


class SessionInvalidError(RuntimeError):
    """Telegram 登录态被服务端作废（session 不能再用了，必须重新登录）"""


# Telethon 不同版本类名略有差异，按类名判断最稳
_DEAD_SESSION_ERRORS = {
    "AuthKeyDuplicatedError", "AuthKeyUnregisteredError", "AuthKeyInvalidError",
    "SessionRevokedError", "SessionExpiredError",
}


def is_dead_session(exc: BaseException) -> bool:
    return type(exc).__name__ in _DEAD_SESSION_ERRORS


def session_invalid_hint(session_path: str) -> str:
    name = session_path if session_path.endswith(".session") else session_path + ".session"
    return (
        "Telegram 登录态已被服务端作废，程序收不到消息、也不会下单。\n"
        "原因：同一份 session 文件在两个不同 IP 上同时使用过\n"
        "（常见：两台机器共用同一个 .session，或代理节点切换导致出口 IP 变化）。\n"
        "修复步骤：\n"
        "  1) 停掉所有正在运行本程序的机器/进程（同一 session 只能一个进程用）\n"
        f"  2) 旧文件 {name} 已永久失效，先改名成 {name}.bak\n"
        "  3) 在运行机上执行：python tg_login.py\n"
        "     按提示输入手机号 → Telegram 验证码 → 两步验证密码（如有）\n"
        "  4) 重新启动 trader.py；只在一台机器跑，代理节点固定不要自动切换"
    )


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
    scheme = (u.scheme or "http").lower()
    # socks5h/socks4a 统一成 python-socks 认的名字
    if scheme.startswith("socks5"):
        scheme = "socks5"
    elif scheme.startswith("socks4"):
        scheme = "socks4"
    return {
        "proxy_type": scheme,
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
        # 记录每个消息最后处理过的文本，用来识别「假编辑」：
        # 点赞、表情回应、回复计数变化都会触发 MessageEdited，但正文没变
        self._last_text: Dict[int, str] = {}

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
        except Exception as e:
            if is_dead_session(e):
                raise SessionInvalidError(session_invalid_hint(self.session_path)) from e
            raise

        try:
            me = await self.client.get_me()
        except Exception as e:
            if is_dead_session(e):
                raise SessionInvalidError(session_invalid_hint(self.session_path)) from e
            raise
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
            self._last_text[msg_id] = text
            # 长期运行防内存膨胀：超过 1 万条只留最近 5000
            if len(self._last_text) > 10000:
                keep = list(self._last_text)[-5000:]
                self._last_text = {k: self._last_text[k] for k in keep}
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
            msg_id = event.message.id
            text = (event.message.message or "").strip()
            if not text:
                return
            # 点赞/表情回应/回复计数变化也会触发 MessageEdited，但正文没变。
            # 这类事件必须忽略：否则每条被点赞的旧信号都会重新解析一遍，
            # 白白消耗币安 API 权重（418 的元凶之一）
            if self._last_text.get(msg_id) == text:
                log.debug("消息 #%d 触发编辑事件但正文未变（点赞/表情/回复），忽略", msg_id)
                return
            self._last_text[msg_id] = text

            # 正文真的变了：交给 trader 判断。
            # 开仓信号交给 trader 判断「同向 → 忽略防重复 / 反向 → 反手」，
            # 不能在这里一刀切丢弃，否则频道真改了方向就漏单
            ev = sp.parse(text)
            log.info("消息 #%d 正文变更（%s），重新解析", msg_id, ev.kind)
            try:
                try:
                    await on_message(text, msg_id, from_edit=True)
                except TypeError:
                    # 兼容只接受两个参数的回调
                    await on_message(text, msg_id)
            except Exception as e:
                log.exception("处理编辑消息异常: %s", e)

        log.info("监听已启动，等待消息...（Ctrl+C 退出）")
        try:
            await self.client.run_until_disconnected()
        except Exception as e:
            # AuthKeyDuplicated：Telegram 认为同一 session 在两个 IP 上使用，
            # 直接把 auth key 作废。旧 session 永久失效，只能重新登录。
            if is_dead_session(e):
                raise SessionInvalidError(session_invalid_hint(self.session_path)) from e
            raise
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
