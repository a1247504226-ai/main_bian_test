#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 重新登录（session 被作废后用它重建）
============================================================
什么时候需要：
  报 AuthKeyDuplicatedError / AuthKeyUnregisteredError / SessionRevokedError
  —— 同一份 .session 在两个不同 IP 上同时使用过，Telegram 会永久作废这个 auth key，
  旧文件不能再复用，只能重新登录生成新的。

用法：
  python tg_login.py                         # 用 .env.bot 里的 TG_PHONE
  python tg_login.py +8613800000000          # 临时指定手机号
  python tg_login.py --session tg_session2   # 另建一个 session（两台机器各用各的）
  python tg_login.py --keep                  # 不备份旧 session（默认改名成 .bak）

流程：手机号 → Telegram 验证码 → 两步验证密码（如有）→ 保存新 session

注意：
  1) 先停掉所有正在运行的 trader.py（同一 session 只能一个进程用）
  2) 验证码会发到你已登录的 Telegram 客户端，没有则走短信
"""
import argparse
import asyncio
import getpass
import os
import sys

from config import load_config


def _backup(path: str):
    """旧 session 改名备份（不删，留作排查），返回备份路径"""
    if not os.path.exists(path):
        return None
    bak = path + ".bak"
    i = 1
    while os.path.exists(bak):
        bak = f"{path}.bak{i}"
        i += 1
    os.replace(path, bak)
    return bak


async def _login(cfg, phone: str, session: str, keep: bool,
                 proxy_url: str = "", no_proxy: bool = False,
                 connection: str = "full") -> int:
    from telethon import TelegramClient
    from telethon.network import (ConnectionTcpAbridged, ConnectionTcpFull,
                                  ConnectionTcpIntermediate)
    from tg_listener import build_proxy

    sess_file = session if session.endswith(".session") else session + ".session"
    if not keep:
        bak = _backup(sess_file)
        if bak:
            print(f"旧 session 已备份：{bak}")

    use_proxy = "" if no_proxy else (proxy_url or cfg.tg_proxy)
    proxy = build_proxy(use_proxy)
    if proxy:
        print(f"使用代理 {proxy['addr']}:{proxy['port']} ({proxy['proxy_type']})")
    else:
        print("直连 Telegram（未配代理 / 指定了 --no-proxy）")

    conn_cls = {
        "full": ConnectionTcpFull,
        "abridged": ConnectionTcpAbridged,
        "intermediate": ConnectionTcpIntermediate,
    }.get(connection, ConnectionTcpFull)
    print(f"连接方式：{conn_cls.__name__}")

    client = TelegramClient(session, cfg.tg_api_id, cfg.tg_api_hash,
                            proxy=proxy, connection=conn_cls,
                            connection_retries=3, retry_delay=2, timeout=20)
    try:
        await client.connect()
    except Exception as e:
        print(f"\n❌ 连接 Telegram 失败：{type(e).__name__}: {e}")
        print("   说明代理到 Telegram 的链路不通（不是账号问题）")
        raise
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ 这个 session 仍然有效：{me.first_name} (@{me.username or '-'})")
        print("   如果 trader.py 仍报 AuthKeyDuplicated，说明另一台机器/进程还在用同一份 session。")
        await client.disconnect()
        return 0

    print("开始登录，验证码会发到你的 Telegram 客户端（或短信）...")
    await client.start(
        phone=phone,
        code_callback=lambda: input("Telegram 验证码: ").strip(),
        password=lambda: getpass.getpass("两步验证密码（没有就直接回车）: "),
    )

    me = await client.get_me()
    print(f"✅ 登录成功：{me.first_name} (@{me.username or '-'})")
    print(f"   session 已保存：{sess_file}")
    print("   现在可以重新启动 trader.py；只在一台机器跑，代理节点固定不要自动切换。")
    await client.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram 重新登录（session 作废后重建）")
    ap.add_argument("phone", nargs="?", default="", help="手机号，如 +8613800000000")
    ap.add_argument("--session", default="", help="session 文件名（默认用 .env.bot 的 TG_SESSION）")
    ap.add_argument("--keep", action="store_true", help="不备份旧 session（默认改名成 .bak）")
    ap.add_argument("--proxy", default="", help="临时指定代理，如 socks5://127.0.0.1:7897")
    ap.add_argument("--no-proxy", action="store_true", help="强制直连（TUN 模式下也走代理）")
    ap.add_argument("--connection", default="full",
                    choices=("full", "abridged", "intermediate"),
                    help="MTProto 连接方式，默认 full；连不上可试 abridged")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        print("❌ 缺少 TG_API_ID / TG_API_HASH，请在 .env.bot 里配好")
        return 1

    phone = args.phone or cfg.tg_phone
    if not phone:
        phone = input("手机号（含国家码，如 +8613800000000）: ").strip()
    if not phone:
        print("❌ 手机号不能为空")
        return 1

    session = args.session or cfg.tg_session
    try:
        return asyncio.run(_login(cfg, phone, session, args.keep,
                                  args.proxy, args.no_proxy, args.connection))
    except KeyboardInterrupt:
        print("\n已取消")
        return 1
    except Exception as e:
        name = type(e).__name__
        if name in ("AuthKeyDuplicatedError", "AuthKeyUnregisteredError",
                    "SessionRevokedError", "SessionExpiredError"):
            print(f"\n❌ 旧 session 已被 Telegram 作废（{name}），不能再用。")
            print("   确认旧 .session 已改名/移走（不要加 --keep），然后重新运行本脚本。")
        elif name in ("IncompleteReadError", "ConnectionError", "ConnectionResetError",
                      "TimeoutError", "OSError"):
            print(f"\n❌ 连接 Telegram 被中断：{name}: {e}")
            print("   不是账号问题，是代理到 Telegram 的链路不通。按顺序试：")
            print("     1) Clash/代理切「全局」模式（规则模式常把 Telegram 走直连，被墙）")
            print("     2) 换节点：香港 / 日本 / 新加坡，避开屏蔽 Telegram 的节点")
            print("     3) 改用 socks5：")
            print("        python tg_login.py --proxy socks5://127.0.0.1:7897")
            print("     4) TUN 模式下试直连：python tg_login.py --no-proxy")
        else:
            print(f"\n❌ 登录失败：{name}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
