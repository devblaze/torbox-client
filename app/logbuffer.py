"""In-memory ring buffer of log records for the web UI's Logs tab.

Captures everything at DEBUG regardless of LOG_LEVEL (which only controls the
console); the buffer is bounded so memory stays flat. Nothing is persisted —
a restart clears it, same as `docker logs`.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque

_MAX = 2000
_buf: deque[dict] = deque(maxlen=_MAX)
_lock = threading.Lock()

# The buffer is served over HTTP (/ui/api/logs) and httpx logs full request URLs,
# so anything secret must be scrubbed before a record reaches any handler.
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


class RedactSecrets(logging.Filter):
    """Redacts known secret values and token/bearer patterns from log records.

    Attached to every handler (not a logger) because propagated child-logger
    records bypass ancestor-logger filters but always hit the handlers.
    """

    def __init__(self, *secrets: str):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = msg
        for secret in self._secrets:
            if secret in red:
                red = red.replace(secret, "***")
        red = _TOKEN_RE.sub(r"\1***", red)
        red = _BEARER_RE.sub("Bearer ***", red)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" — {record.exc_info[0].__name__}: {record.exc_info[1]}"
        except Exception:  # noqa: BLE001 — a broken log call must never crash us
            return
        with _lock:
            _buf.append({
                "ts": record.created,
                "levelno": record.levelno,
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            })


def handler() -> logging.Handler:
    h = _BufferHandler()
    h.setLevel(logging.DEBUG)
    return h


def entries(limit: int = 500, min_level: str = "DEBUG") -> list[dict]:
    """Most recent ``limit`` records at/above ``min_level``, oldest first."""
    threshold = logging.getLevelName(min_level.upper())
    if not isinstance(threshold, int):
        threshold = logging.DEBUG
    with _lock:
        matched = [e for e in _buf if e["levelno"] >= threshold]
    return matched[-limit:]
