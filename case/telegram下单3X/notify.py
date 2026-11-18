"""Telegram 告警 —— 纯 requests 实现，不引入额外 SDK"""
import logging
import threading
import time
from collections import deque

import requests

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: int = 8):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.enabled = bool(token and chat_id)
        # 简单的发送队列，避免阻塞主流程
        self._q = deque()
        self._lock = threading.Lock()
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def __call__(self, text: str):
        self.send(text)

    def send(self, text: str):
        if not self.enabled:
            log.info("[无TG] %s", text)
            return
        with self._lock:
            self._q.append(text)

    def _worker(self):
        while True:
            text = None
            with self._lock:
                if self._q:
                    text = self._q.popleft()
            if text is None:
                time.sleep(0.3)
                continue
            self._post(text)

    def _post(self, text: str, retries: int = 2):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for i in range(retries + 1):
            try:
                r = requests.post(url, data={
                    "chat_id": self.chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": True,
                }, timeout=self.timeout)
                if r.status_code == 200:
                    return
                log.warning("TG 发送失败 %s %s", r.status_code, r.text[:200])
            except Exception as e:
                log.warning("TG 发送异常 %s", e)
            time.sleep(1 + i)
