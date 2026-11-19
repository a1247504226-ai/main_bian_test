"""
心跳 HTTP 服务 —— 给 UptimeRobot 之类的免费监控用

如果进程挂了或卡死，这个端口就不响应，外部监控会立刻告警。
比「程序自己发消息说我活着」可靠得多。
"""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)


class _State:
    started_at = time.time()
    last_beat = time.time()
    last_signal = ""
    extra: dict = {}


STATE = _State()


def beat(**kwargs):
    """主循环每轮调用，刷新心跳"""
    STATE.last_beat = time.time()
    if kwargs:
        STATE.extra.update(kwargs)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/health", "/"):
            self.send_response(404)
            self.end_headers()
            return

        now = time.time()
        stale = now - STATE.last_beat
        # 超过 120 秒没心跳视为不健康
        healthy = stale < 120

        body = json.dumps({
            "status": "ok" if healthy else "stale",
            "uptime_sec": int(now - STATE.started_at),
            "last_beat_sec_ago": int(stale),
            "last_signal": STATE.last_signal,
            **STATE.extra,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默，避免刷日志


def start(port: int = 8080):
    def _run():
        try:
            srv = HTTPServer(("0.0.0.0", port), _Handler)
            log.info("心跳服务监听 :%d/health", port)
            srv.serve_forever()
        except Exception as e:
            log.error("心跳服务启动失败: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
