import asyncio
from datetime import timezone, timedelta

from telethon import TelegramClient
from telethon.errors import FloodWaitError


# ============================================================
# Telegram 配置
# ============================================================

API_ID = 30069682
API_HASH = "0d169b2953d6fddb1c79a264f910df50"

# 必须和之前登录成功的 session 一致
SESSION_NAME = "telegram_monitor"

# 代理
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

# 频道 ID
CHANNEL_ID = -1001976446916

# 获取最近 1000 条
MESSAGE_LIMIT = 1000

# 输出文件
OUTPUT_FILE = "telegram_history_1000.txt"


# ============================================================
# Telegram Client
# ============================================================

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,

    proxy={
        "proxy_type": "http",
        "addr": PROXY_HOST,
        "port": PROXY_PORT,
    },

    connection_retries=5,
    retry_delay=3,
    timeout=20,
    auto_reconnect=True,

    # 不需要接收实时更新
    receive_updates=False,
)


# ============================================================
# UTC -> 上海时间
# ============================================================

def to_shanghai(dt):

    if not dt:
        return ""

    shanghai_tz = timezone(timedelta(hours=8))

    return dt.astimezone(shanghai_tz).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ============================================================
# 获取历史消息
# ============================================================

async def get_history():

    print("=" * 70)
    print("开始获取 Telegram 历史消息")
    print("=" * 70)

    entity = await client.get_entity(CHANNEL_ID)

    channel_name = getattr(
        entity,
        "title",
        "未知频道"
    )

    print(f"频道：{channel_name}")
    print(f"数量：{MESSAGE_LIMIT}")
    print()

    count = 0

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        async for message in client.iter_messages(
            entity,
            limit=MESSAGE_LIMIT
        ):

            text = message.message or ""

#             f.write("=" * 70 + "\n")

 #            f.write(
 #                f"Message ID: {message.id}\n"
 #            )

 #            f.write(
 #                f"Time: {to_shanghai(message.date)}\n"
 #            )

            f.write("=" * 70 + "\n")

            f.write(text)

            f.write("\n\n")

            count += 1

            if count % 100 == 0:
                print(f"已保存：{count} 条")

    print()
    print("=" * 70)
    print("历史消息获取完成")
    print("=" * 70)

    print(f"频道：{channel_name}")
    print(f"实际保存：{count} 条")
    print(f"文件：{OUTPUT_FILE}")


# ============================================================
# Main
# ============================================================

async def main():

    try:

        print("正在连接 Telegram...")

        await client.start()

        print("Telegram 连接成功")
        print()

        await get_history()

    except FloodWaitError as e:

        print(
            f"Telegram 请求频繁，需要等待 {e.seconds} 秒"
        )

    except Exception as e:

        print()
        print("=" * 70)
        print("❌ 程序异常")
        print("=" * 70)

        print(
            f"错误类型：{type(e).__name__}"
        )

        print(
            f"错误：{e}"
        )

    finally:

        await client.disconnect()

        print()
        print("Telegram 已断开")


# ============================================================
# 使用 Telethon 自己的 Event Loop
# ============================================================

if __name__ == "__main__":

    client.loop.run_until_complete(main())