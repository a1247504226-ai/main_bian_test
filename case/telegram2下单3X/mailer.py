"""
邮件通知（SMTP SSL）

用途：余额跨过设定档位时推送提醒。
163 邮箱必须用「授权码」而不是登录密码，端口 465 + SSL。

设计要点：
1. 后台线程发送，不阻塞下单主流程
2. 失败重试，最多 max_retries 次
3. 发送失败只记日志，绝不抛异常打断交易
"""
import logging
import smtplib
import threading
from collections import deque
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)


class Mailer:
    def __init__(
        self,
        server: str,
        port: int = 465,
        user: str = "",
        password: str = "",
        to: str = "",
        max_retries: int = 3,
        timeout: int = 15,
    ):
        self.server = server
        self.port = port
        self.user = user
        self.password = password
        self.to = to
        self.max_retries = max_retries
        self.timeout = timeout
        self.enabled = bool(server and user and password and to)
        self._q = deque()
        self._lock = threading.Lock()
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def send(self, subject: str, body: str):
        """异步入队，立即返回"""
        if not self.enabled:
            log.debug("[无邮件] %s | %s", subject, body.replace("\n", " ")[:120])
            return
        with self._lock:
            self._q.append((subject, body))

    def send_sync(self, subject: str, body: str) -> bool:
        """同步发送，返回是否成功（部署自检用）"""
        return self._post(subject, body)

    def _worker(self):
        while True:
            item = None
            with self._lock:
                if self._q:
                    item = self._q.popleft()
            if item is None:
                threading.Event().wait(0.5)
                continue
            self._post(*item)

    def _post(self, subject: str, body: str) -> bool:
        if not self.enabled:
            return False
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        # 中文发件人名会被 formataddr 用 ascii 编码而报错，直接只写邮箱地址
        msg["From"] = self.user
        msg["To"] = self.to

        for attempt in range(1, self.max_retries + 1):
            try:
                # local_hostname 必须显式给纯 ASCII：
                # Windows 中文主机名会让 smtplib 的 ehlo 用 ascii 编码而崩溃
                with smtplib.SMTP_SSL(self.server, self.port,
                                      local_hostname="localhost",
                                      timeout=self.timeout) as s:
                    s.login(self.user, self.password)
                    s.sendmail(self.user, [self.to], msg.as_string())
                log.info("邮件已发送: %s", subject)
                return True
            except Exception as e:
                log.warning("邮件发送失败(%d/%d): %s", attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    threading.Event().wait(2 ** attempt)
        log.error("邮件最终发送失败: %s", subject)
        return False


class BalanceWatcher:
    """余额跨档提醒

    只在「跨过一个档位」时发一次，不会每笔交易都刷邮件。
    例：阈值 2000、步长 2000 → 跨过 2000/4000/6000… 各发一次。
    """

    def __init__(self, mailer: Mailer, threshold: float = 2000.0,
                 step: float = 2000.0, state_file: str = "state/balance_alert.json"):
        import json
        import os
        self.mailer = mailer
        self.threshold = threshold
        self.step = max(step, 1.0)
        self.state_file = state_file
        self.last_level = 0.0
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    self.last_level = json.load(f).get("last_level", 0.0)
            except Exception:
                pass

    def _save(self):
        import json
        import os
        d = os.path.dirname(self.state_file)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"last_level": self.last_level}, f)

    def check(self, balance: float, extra: str = ""):
        """余额跨档时发邮件"""
        if balance < self.threshold:
            return
        level = int(balance // self.step) * self.step
        if level <= self.last_level:
            return
        self.last_level = level
        self._save()
        body = (
            f"账户余额已突破 {level:,.0f} USDT\n\n"
            f"当前权益：{balance:,.2f} USDT\n"
            f"{extra}\n\n"
            f"—— 交易机器人自动通知"
        )
        self.mailer.send(f"【交易机器人】余额突破 {level:,.0f} U", body)
