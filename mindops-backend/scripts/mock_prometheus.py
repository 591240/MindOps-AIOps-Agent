"""F-11 验收用 mock Prometheus（27-F-11 §9.2 前置）。

零依赖（纯 stdlib），只实现探测用到的两个只读端点：
  GET /api/v1/status/runtimeinfo → {"status":"success","data":{...}}
  GET /api/v1/targets            → {"status":"success","data":{"activeTargets":[12 个，11 up]}}

可选故障注入（用于手工验证 TC-10 / http_error 分支）：
  GET /api/v1/status/runtimeinfo 带查询串 ?mode=redirect  → 302 到公网（验证探测不跟随重定向）
  GET /api/v1/status/runtimeinfo 带查询串 ?mode=slow      → 睡眠 10s（验证 timeout）

启动：python scripts/mock_prometheus.py [port]   # 默认 19090
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 12 个 target，其中 11 个 up（与 e2e_f11.py 的断言一致）
ACTIVE_TARGETS = [
    {"scrapeUrl": f"http://10.0.0.{i}:9100/metrics", "health": "up" if i <= 11 else "down",
     "labels": {"job": "order-svc", "instance": f"10.0.0.{i}:9100"}}
    for i in range(1, 13)
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        mode = (query.get("mode") or [""])[0]

        if mode == "redirect":
            # TC-10：探测端必须 follow_redirects=False，不能落到 Location
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if mode == "slow":
            time.sleep(10)
            return

        if path in ("/api/v1/status", "/api/v1/status/runtimeinfo"):
            self._json(200, {"status": "success", "data": {"version": "2.53.0"}})
        elif path == "/api/v1/targets":
            self._json(200, {"status": "success", "data": {"activeTargets": ACTIVE_TARGETS}})
        else:
            # 路径白名单之外一律 404（同时便于确认探测只打这两条路径）
            self._json(404, {"status": "error", "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        # TC-11：探测只允许 GET，出现写方法即为违规
        self._json(405, {"status": "error", "error": "method not allowed"})

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        sys.stderr.write("mock-prom %s\n" % (fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19090
    print(f"mock Prometheus listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
