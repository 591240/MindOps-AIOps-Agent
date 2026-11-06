"""结构化日志（文档 §7.3）。

每条告警输出一行 JSON：
{"ts","level","event","trace_id","alert_id","fingerprint","source","component",
 "severity","deduped","incident_id","latency_ms"}
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid

_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.created % 1 * 1000):03d}Z",
            "level": record.levelname,
            "event": getattr(record, "event_name", record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def get_logger() -> logging.Logger:
    setup_logging()
    return logging.getLogger("mindops")


def log_event(level: int, event: str, **fields) -> None:
    get_logger().log(level, event, extra={"event_name": event, "fields": fields})


def new_trace_id() -> str:
    return uuid.uuid4().hex


class Timer:
    """§7.3 latency_ms"""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 2)
