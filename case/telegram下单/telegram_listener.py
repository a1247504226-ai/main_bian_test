# -*- coding: utf-8 -*-
"""
Telegram 消息监听器
===================

职责只有一件事：把 Telegram 新消息 → SignalParser 解析 → 追加到 signals.jsonl。
下单由 signal_executor.py 负责，两者互不干扰。

用法：
    python telegram_listener.py            # 正式监听
    python telegram_listener.py --once     # 只拉一轮就退出（调试）
    python telegram_listener.py --dump     # 只打印消息，不解析、不写文件（确认能收到）
    python telegram_listener.py --backlog 5  # 首次启动时回补最近 5 条（谨慎，会触发下单）

配置（.env）：
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...      # BotFather 拿的 token
    TELEGRAM_CHAT_ID=-1001234567890           # 频道/群 id，留空则处理所有会话
    TELEGRAM_PROXY=http://127.0.0.1:7890      # 国内必填，否则连不上 api.telegram.org
    TELEGRAM_POLL_TIMEOUT=30

重要安全设计：
    首次启动（没有 telegram_state.json）时，默认**丢弃积压的历史消息**，
    只从"当前最新"开始监听。否则一启动就会把几小时前的信号全部回放下单。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from signal_parser import SignalParser

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载：已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))

SIGNAL_FILE = Path(os.getenv("SIGNAL_FILE") or str(BASE_DIR / "signals.jsonl"))
STATE_FILE = BASE_DIR / "telegram_state.json"
LISTEN_LOG = BASE_DIR / "telegram_listener.log"

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
API_ROOT = f"https://api.telegram.org/bot{TOKEN}"


# ==========================================================================
# 日志：同时写控制台和文件
# ==========================================================================
def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LISTEN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ==========================================================================
# 状态
# ==========================================================================
def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# ==========================================================================
# Telegram API
# ==========================================================================
class TelegramError(Exception):
    pass


def api(method: str, **params: Any) -> Any:
    url = f"{API_ROOT}/{method}"
    try:
        resp = requests.post(url, data=params, timeout=POLL_TIMEOUT + 15, proxies=PROXIES)
    except requests.exceptions.ProxyError as exc:
        raise TelegramError(
            f"代理连接失败（TELEGRAM_PROXY={PROXY}）：{exc}\n"
            f"  → 检查代理软件是否在运行、端口是否正确"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise TelegramError(f"请求超时（{method}）：{exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise TelegramError(
            f"连不上 api.telegram.org：{exc}\n"
            f"  → 国内网络需要代理，请在 .env 里设置 TELEGRAM_PROXY"
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        raise TelegramError(f"返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")

    if not data.get("ok"):
        code = data.get("error_code")
        desc = data.get("description", "")
        if code == 401:
            raise TelegramError(f"Token 无效（401）：{desc}\n  → 检查 TELEGRAM_BOT_TOKEN")
        if code == 409:
            raise TelegramError(
                f"getUpdates 与 webhook 冲突（409）：{desc}\n"
                f"  → 该 bot 设置了 webhook，需要先删除（本程序会自动尝试）"
            )
        raise TelegramError(f"Telegram 返回错误 {code}：{desc}")

    return data.get("result")


def delete_webhook() -> None:
    try:
        api("deleteWebhook", drop_pending_updates="false")
        log("已确认 webhook 为空（getUpdates 可用）")
    except TelegramError as exc:
        log(f"删除 webhook 失败（可忽略）：{exc}")


# ==========================================================================
# 消息提取
# ==========================================================================
def extract_message(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 update 里取出新消息（兼容私聊/群 message 与频道 channel_post）。"""
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return None
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return None
    chat = msg.get("chat") or {}
    return {
        "update_id": update.get("update_id"),
        "message_id": msg.get("message_id"),
        "chat_id": chat.get("id"),
        "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
        "chat_username": chat.get("username") or "",
        "date": msg.get("date"),
        "text": text,
    }


def match_chat(msg: Dict[str, Any]) -> bool:
    """按 TELEGRAM_CHAT_ID 过滤；留空则不过滤。"""
    if not CHAT_ID:
        return True
    want = CHAT_ID.strip()
    got = str(msg.get("chat_id") or "")
    if want.lstrip("-").isdigit():
        return got == want
    return want.lstrip("@").lower() in (msg.get("chat_username") or "").lower()


def push_signal(sig: Dict[str, Any], msg: Dict[str, Any]) -> None:
    """把解析结果追加到 signals.jsonl（执行器从这里增量读取）。"""
    record = dict(sig)
    record["_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    record["_src"] = msg.get("chat_title") or ""
    record["_raw"] = msg.get("text", "")[:500]
    line = json.dumps(record, ensure_ascii=False)
    with SIGNAL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())      # 确保执行器立刻能读到


def describe(sig: Dict[str, Any]) -> str:
    parts = [sig.get("type", "?")]
    if sig.get("symbol"):
        parts.append(str(sig["symbol"]))
    if sig.get("side"):
        parts.append(str(sig["side"]))
    if sig.get("tp_level"):
        parts.append(f"TP{sig['tp_level']}")
    if sig.get("tp"):
        parts.append(f"tp={sig['tp']}")
    if sig.get("sl"):
        parts.append(f"sl={sig['sl']}")
    if sig.get("add"):
        parts.append(f"add={sig['add']}")
    return " | ".join(parts)


# ==========================================================================
# 主流程
# ==========================================================================
def handle_update(update: Dict[str, Any], dump: bool) -> Optional[Dict[str, Any]]:
    msg = extract_message(update)
    if not msg:
        return None

    if not match_chat(msg):
        return None

    head = msg["text"].splitlines()[0][:70]
    # 打印 chat_id，方便第一次用 --dump 时直接抄进 .env 的 TELEGRAM_CHAT_ID
    log(f"[消息] {msg['chat_title']} (chat_id={msg['chat_id']}) | {head!r}")

    if dump:
        return None

    sig = SignalParser.parse_signal(msg["text"])
    if not sig:
        return None

    push_signal(sig, msg)
    log(f"  → 已写入信号：{describe(sig)}")
    return sig


def run(once: bool = False, dump: bool = False, backlog: int = 0) -> None:
    if not TOKEN:
        log("未配置 TELEGRAM_BOT_TOKEN，退出")
        sys.exit(1)

    log("=" * 64)
    log("Telegram 信号监听器启动")
    log(f"代理: {PROXY or '未设置（国内直连通常不通）'}")
    log(f"会话过滤: {CHAT_ID or '不过滤（处理所有会话）'}")
    log(f"信号文件: {SIGNAL_FILE}")
    log(f"模式: {'dump（只看不写）' if dump else ('单轮' if once else '持续监听')}")
    log("=" * 64)

    delete_webhook()

    state = load_state()
    offset = int(state.get("offset", 0) or 0)

    # 首次启动且未要求回补：跳过积压，避免把历史信号全部回放下单
    if offset == 0 and not backlog:
        try:
            latest = api("getUpdates", offset=-1, limit=1, timeout=0)
            if latest:
                newest_id = int(latest[-1]["update_id"])
                offset = newest_id + 1
                save_state({"offset": offset})
                log(f"首次启动：已跳过积压消息，从 update_id={offset} 开始监听")
                log("  （如需回补历史，用 --backlog N）")
            else:
                log("首次启动：当前无积压消息")
        except TelegramError as exc:
            log(f"获取最新 offset 失败：{exc}")
    elif backlog:
        try:
            hist = api("getUpdates", limit=min(backlog, 100), timeout=0)
            log(f"回补模式：将处理最近 {len(hist or [])} 条消息")
            for u in hist or []:
                handle_update(u, dump)
                offset = int(u["update_id"]) + 1
                save_state({"offset": offset})
            log("回补完成，转入实时监听")
        except TelegramError as exc:
            log(f"回补失败：{exc}")

    backoff = 1
    while True:
        try:
            updates: List[Dict[str, Any]] = api(
                "getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                allowed_updates=json.dumps(["message", "channel_post"]),
            ) or []

            backoff = 1
            for u in updates:
                try:
                    handle_update(u, dump)
                except Exception as exc:
                    log(f"处理消息失败（update_id={u.get('update_id')}）：{exc}")
                # 处理完（或失败）再推进 offset，避免重复处理同一条
                offset = int(u["update_id"]) + 1
                save_state({"offset": offset})

            if once:
                log("单轮结束，退出")
                return

        except TelegramError as exc:
            log(f"拉取失败：{exc}")
            if "409" in str(exc):
                delete_webhook()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

        except KeyboardInterrupt:
            log("收到中断，退出")
            return

        except Exception as exc:
            log(f"未预期异常：{exc}")
            time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram 信号监听器")
    parser.add_argument("--once", action="store_true", help="只拉一轮后退出")
    parser.add_argument("--dump", action="store_true", help="只打印消息，不解析不写文件")
    parser.add_argument("--backlog", type=int, default=0, help="首次启动回补最近 N 条（会触发下单）")
    args = parser.parse_args()

    try:
        run(once=args.once, dump=args.dump, backlog=args.backlog)
    except KeyboardInterrupt:
        log("已退出")


if __name__ == "__main__":
    main()
