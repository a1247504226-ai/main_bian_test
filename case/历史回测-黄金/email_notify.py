# -*- coding: utf-8 -*-
"""SMTP 邮件推送模块（163 邮箱 SSL/465），使用 EmailMessage 自动处理中文编码。"""
import smtplib, ssl, time
from email.message import EmailMessage
from email.utils import formataddr


def send_email(subject, body, cfg):
    """发送纯文本邮件，失败按 max_retries 重试。返回 (ok, msg)。"""
    retries = int(cfg.get('max_retries', 3))
    last_err = 'unknown'
    for attempt in range(1, retries + 1):
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = formataddr(('GoldSignal', cfg['from']))
            msg['To'] = cfg['to']
            msg.set_content(body)
            ctx = ssl.create_default_context()
            # local_hostname 必须给 ASCII：本机计算机名可能含中文，否则 EHLO 会 UnicodeEncodeError
            with smtplib.SMTP_SSL(cfg['server'], int(cfg['port']),
                                  local_hostname='localhost', context=ctx, timeout=30) as s:
                s.login(cfg['from'], cfg['password'])
                s.send_message(msg)
            return True, 'ok'
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            time.sleep(2 * attempt)
    return False, last_err
