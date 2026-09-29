import os
import asyncio
from datetime import datetime

from telethon import TelegramClient
from telethon.errors import FloodWaitError


# =========================================================
# 1. Telegram 配置
# =========================================================

API_ID = 30069682
API_HASH = "0d169b2953d6fddb1c79a264f910df50"


# =========================================================
# 2. 代理配置
# =========================================================
#
# 你现在 Binance requests 使用：
#
# HTTP  -> 127.0.0.1:7897
# HTTPS -> 127.0.0.1:7897
#
# 如果这个 7897 是 HTTP/Mixed 代理，
# Telethon 这里显式使用 HTTP。
#

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897


# 如果你的代理是 HTTP：
PROXY_TYPE = "http"

#
# 如果以后确认 7897 是 SOCKS5：
#
# PROXY_TYPE = "socks5"
#


# =========================================================
# 3. 监控配置
# =========================================================

CHECK_INTERVAL = 60

# 每次最多拉多少条
MESSAGE_LIMIT = 20

# Telethon Session
SESSION_NAME = "telegram_monitor"

# 保存最后处理的消息 ID
LAST_ID_FILE = "telegram_last_id.txt"


# =========================================================
# 4. 设置普通 HTTP 请求代理
# =========================================================
#
# 这个主要给 requests 等库使用。
#
# Telethon 不依赖这个，所以后面还会单独传 proxy。
#

os.environ["HTTP_PROXY"] = (
    f"http://{PROXY_HOST}:{PROXY_PORT}"
)

os.environ["HTTPS_PROXY"] = (
    f"http://{PROXY_HOST}:{PROXY_PORT}"
)


# =========================================================
# 5. 读取最后消息 ID
# =========================================================

def load_last_id():

    if not os.path.exists(LAST_ID_FILE):
        return 0

    try:

        with open(
            LAST_ID_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            value = f.read().strip()

            if not value:
                return 0

            return int(value)

    except Exception as e:

        print(
            f"读取 last_id 失败: {e}"
        )

        return 0


# =========================================================
# 6. 保存最后消息 ID
# =========================================================

def save_last_id(message_id):

    temp_file = LAST_ID_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(str(message_id))

    # Windows 下这样也比较安全
    os.replace(
        temp_file,
        LAST_ID_FILE
    )


# =========================================================
# 7. 创建 Telegram Client
# =========================================================

def create_client():

    print()
    print("=" * 70)
    print("创建 Telegram Client")
    print("=" * 70)

    print(
        f"代理: {PROXY_TYPE}://"
        f"{PROXY_HOST}:{PROXY_PORT}"
    )

    if PROXY_TYPE == "http":

        proxy = {
            "proxy_type": "http",
            "addr": PROXY_HOST,
            "port": PROXY_PORT,
        }

    elif PROXY_TYPE == "socks5":

        proxy = {
            "proxy_type": "socks5",
            "addr": PROXY_HOST,
            "port": PROXY_PORT,
        }

    else:

        raise ValueError(
            f"不支持的代理类型: {PROXY_TYPE}"
        )

    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,

        # ⭐ 关键
        proxy=proxy,

        # 网络参数
        connection_retries=5,
        retry_delay=3,
        timeout=20,

        # 自动更新服务器地址
        auto_reconnect=True,
    )

    return client


# =========================================================
# 8. 列出账号可以访问的群/频道
# =========================================================

async def list_chats(client):

    print()
    print("=" * 80)
    print("开始获取你的 Telegram 群/频道")
    print("=" * 80)

    index = 0

    async for dialog in client.iter_dialogs():

        entity = dialog.entity

        # 频道
        if getattr(entity, "broadcast", False):

            chat_type = "频道"

        # 超级群
        elif getattr(entity, "megagroup", False):

            chat_type = "超级群"

        # 普通群
        elif getattr(entity, "title", None):

            chat_type = "群组"

        else:

            continue

        index += 1

        print()
        print(
            f"[{index}] {chat_type}"
        )

        print(
            f"名称: {dialog.name}"
        )

        print(
            f"ID: {dialog.id}"
        )

        username = getattr(
            entity,
            "username",
            None
        )

        if username:

            print(
                f"Username: @{username}"
            )

        print("-" * 80)

    print()
    print(
        f"共找到 {index} 个群/频道"
    )


# =========================================================
# 9. 获取频道新消息
# =========================================================

async def check_messages(
    client,
    channel_id,
    last_id
):

    try:

        messages = await client.get_messages(
            channel_id,
            limit=MESSAGE_LIMIT
        )

        if not messages:

            print("没有获取到消息")

            return last_id

        # Telegram 返回：
        #
        # 最新
        # ↓
        # 旧
        #
        # 所以反转后处理
        new_messages = [

            msg

            for msg in reversed(messages)

            if msg.id > last_id
        ]

        if not new_messages:

            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"没有新消息"
            )

            return last_id

        print()
        print("=" * 80)
        print(
            f"发现 {len(new_messages)} 条新消息"
        )
        print("=" * 80)

        newest_id = last_id

        for msg in new_messages:

            print()
            print("-" * 80)

            print(
                f"Message ID : {msg.id}"
            )

            print(
                f"Date       : {msg.date}"
            )

            print(
                f"Sender ID  : {msg.sender_id}"
            )

            print("Text:")

            print(
                msg.text or "[非文本消息]"
            )

            print("-" * 80)

            newest_id = max(
                newest_id,
                msg.id
            )

        # 保存进度
        save_last_id(newest_id)

        print()
        print(
            f"最新 Message ID: {newest_id}"
        )

        return newest_id

    except FloodWaitError as e:

        print()
        print(
            "⚠️ Telegram FloodWait"
        )

        print(
            f"需要等待 {e.seconds} 秒"
        )

        await asyncio.sleep(
            e.seconds
        )

        return last_id

    except Exception as e:

        print()
        print(
            "❌ 获取消息失败"
        )

        print(
            "错误类型:",
            type(e).__name__
        )

        print(
            "错误:",
            e
        )

        return last_id


# =========================================================
# 10. 主程序
# =========================================================

async def main():

    client = create_client()

    try:

        print()
        print(
            "正在连接 Telegram..."
        )

        await client.start()

        print()
        print(
            "✅ Telegram 连接成功"
        )

        # =================================================
        # 获取当前账号
        # =================================================

        me = await client.get_me()

        print()
        print("=" * 70)
        print("Telegram 登录信息")
        print("=" * 70)

        print(
            "ID:",
            me.id
        )

        print(
            "Username:",
            me.username
        )

        print(
            "Name:",
            me.first_name,
            me.last_name or ""
        )

        # =================================================
        # 列出频道
        # =================================================

        await list_chats(client)

        # =================================================
        # 输入频道 ID
        # =================================================

        print()
        print("=" * 70)
        print("选择需要监控的频道")
        print("=" * 70)

        channel_input = input(
            "请输入频道 ID: "
        ).strip()

        try:

            channel_id = int(
                channel_input
            )

        except ValueError:

            print(
                "❌ 频道 ID 必须是数字"
            )

            return

        # =================================================
        # 验证频道
        # =================================================

        print()
        print(
            "正在验证频道..."
        )

        entity = await client.get_entity(
            channel_id
        )

        print()
        print(
            "✅ 找到频道:"
        )

        print(
            "名称:",
            getattr(
                entity,
                "title",
                "未知"
            )
        )

        print(
            "ID:",
            entity.id
        )

        # =================================================
        # 读取上次处理进度
        # =================================================

        last_id = load_last_id()

        print()
        print(
            "上次处理 Message ID:",
            last_id
        )

        # =================================================
        # 开始循环
        # =================================================

        print()
        print("=" * 80)
        print("开始监控")
        print("=" * 80)

        print(
            f"检查间隔: {CHECK_INTERVAL} 秒"
        )

        print(
            f"每次获取: {MESSAGE_LIMIT} 条"
        )

        print(
            "按 Ctrl+C 退出"
        )

        print("=" * 80)

        while True:

            print()
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"检查消息..."
            )

            last_id = await check_messages(
                client,
                channel_id,
                last_id
            )

            print(
                f"等待 {CHECK_INTERVAL} 秒..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL
            )

    except KeyboardInterrupt:

        print()
        print(
            "程序退出"
        )

    except Exception as e:

        print()
        print("=" * 70)
        print("❌ 程序异常")
        print("=" * 70)

        print(
            "错误类型:",
            type(e).__name__
        )

        print(
            "错误:",
            e
        )

    finally:

        if client.is_connected():

            await client.disconnect()

        print(
            "Telegram 已断开"
        )


# =========================================================
# 启动
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())