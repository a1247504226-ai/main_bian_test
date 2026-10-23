# -*- coding: utf-8 -*-
"""测试邮件推送配置是否正确（会真实发一封测试邮件）。"""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from email_notify import send_email

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, 'email_config.json'), 'r', encoding='utf-8') as f:
    cfg = json.load(f)

subject = "[测试] 黄金AU策略邮件推送"
body = (f"这是一封测试邮件，用于验证邮件推送配置。\n\n"
        f"发件: {cfg['from']}\n收件: {cfg['to']}\n"
        f"服务器: {cfg['server']}:{cfg['port']}\n"
        f"时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"若你收到本邮件，说明配置成功。")
ok, msg = send_email(subject, body, cfg)
print(f"发送结果: {'成功' if ok else '失败'}  ({msg})")
sys.exit(0 if ok else 1)
