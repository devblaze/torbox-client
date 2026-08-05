"""Pushover notifications: subscription expiry warnings and error-burst alerts.

Designed to never spam. A notification goes out only when
  - the TorBox subscription enters its warning window (at most once per
    calendar day, persisted so restarts don't re-notify), or
  - ``error_burst_threshold`` errors pile up inside a 15-minute window — then
    one alert is sent and the counter goes quiet for an hour.
"""
from __future__ import annotations

import logging
import time
from collections import deque

import httpx

from . import runtime
from .store import store

# Everything here logs under "notify", which the error counter skips — a
# failing Pushover call must never feed the burst that triggers Pushover.
log = logging.getLogger("notify")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
BURST_WINDOW = 15 * 60   # seconds of look-back when counting errors
BURST_COOLDOWN = 60 * 60  # min seconds between two burst alerts

_errors: deque[tuple[float, str]] = deque(maxlen=1000)
_last_burst_notify = 0.0


def configured() -> bool:
    return bool(runtime.get("pushover_enabled")
                and runtime.get("pushover_token") and runtime.get("pushover_user"))


def record_error(message: str) -> None:
    """Count one error toward the burst alert (callable from non-log paths)."""
    _errors.append((time.time(), str(message)))


class ErrorCounter(logging.Handler):
    """Feeds ERROR-and-above log records into the burst counter."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR or record.name == "notify":
            return
        try:
            record_error(record.getMessage())
        except Exception:  # noqa: BLE001 — a broken log call must never crash us
            pass


def handler() -> logging.Handler:
    h = ErrorCounter()
    h.setLevel(logging.ERROR)
    return h


async def send(title: str, message: str, priority: int = 0) -> tuple[bool, str]:
    """Send one Pushover message. Returns (ok, detail) and never raises."""
    if not configured():
        return False, "Pushover is not configured (set token + user key in Settings)"
    payload = {
        "token": runtime.get("pushover_token"),
        "user": runtime.get("pushover_user"),
        "title": title,
        "message": message,
        "priority": priority,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(PUSHOVER_URL, data=payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("Pushover send failed: %s", exc)
        return False, f"send failed: {exc}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    if resp.status_code == 200 and body.get("status") == 1:
        log.info("Pushover notification sent: %s", title)
        return True, "sent"
    detail = "; ".join(body.get("errors") or []) or f"HTTP {resp.status_code}"
    log.warning("Pushover rejected notification: %s", detail)
    return False, detail


async def maybe_notify_error_burst() -> None:
    """Called by the worker every poll; sends at most one alert per cooldown."""
    global _last_burst_notify
    threshold = runtime.get("error_burst_threshold")
    if threshold <= 0 or not configured():
        return
    now = time.time()
    if now - _last_burst_notify < BURST_COOLDOWN:
        return
    recent = [msg for ts, msg in _errors if now - ts <= BURST_WINDOW]
    if len(recent) < threshold:
        return
    _last_burst_notify = now
    _errors.clear()
    await send(
        "torbox-client: repeated errors",
        f"{len(recent)} errors in the last {BURST_WINDOW // 60} minutes.\n"
        f"Latest: {recent[-1][:300]}\n"
        "Check the dashboard Logs tab for details.",
    )


_SUB_KV = "notify.sub_warned_on"


async def maybe_notify_subscription(days_left: float | None, expires_at: str) -> None:
    """At most one warning per calendar day while inside the warning window."""
    warn_days = runtime.get("sub_warn_days")
    if days_left is None or warn_days <= 0 or days_left > warn_days or not configured():
        return
    today = time.strftime("%Y-%m-%d")
    if store.get_kv(_SUB_KV) == today:
        return
    if days_left <= 0:
        title = "TorBox subscription EXPIRED"
        msg = (f"Your TorBox subscription expired ({expires_at}). "
               "Downloads will stop working — renew at torbox.app.")
    else:
        title = "TorBox subscription expiring"
        msg = (f"Your TorBox subscription expires in {days_left:.1f} days "
               f"({expires_at}). Renew at torbox.app.")
    ok, _ = await send(title, msg, priority=1 if days_left <= 2 else 0)
    if ok:
        store.set_kv(_SUB_KV, today)
