import logging
import time

import pytest

from app import notify, runtime
from app.store import Store


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """Pushover 'configured' with the network send captured, state isolated."""
    st = Store(str(tmp_path / "state.db"))
    monkeypatch.setattr(notify, "store", st)
    monkeypatch.setitem(runtime._values, "pushover_enabled", True)
    monkeypatch.setitem(runtime._values, "pushover_token", "t" * 30)
    monkeypatch.setitem(runtime._values, "pushover_user", "u" * 30)
    monkeypatch.setitem(runtime._values, "error_burst_threshold", 5)
    monkeypatch.setitem(runtime._values, "sub_warn_days", 7)
    notify._errors.clear()
    monkeypatch.setattr(notify, "_last_burst_notify", 0.0)
    out = []

    async def fake_send(title, message, priority=0):
        out.append((title, message, priority))
        return True, "sent"

    monkeypatch.setattr(notify, "send", fake_send)
    return out


# --------------------------------------------------------------------------- #
# error burst
# --------------------------------------------------------------------------- #
async def test_burst_below_threshold_is_silent(sent):
    for i in range(4):
        notify.record_error(f"err {i}")
    await notify.maybe_notify_error_burst()
    assert sent == []


async def test_burst_fires_once_then_respects_cooldown(sent):
    for i in range(5):
        notify.record_error(f"err {i}")
    await notify.maybe_notify_error_burst()
    assert len(sent) == 1
    assert "5 errors" in sent[0][1]
    assert "err 4" in sent[0][1]  # latest error included

    # Another burst right away: swallowed by the cooldown.
    for i in range(5):
        notify.record_error(f"more {i}")
    await notify.maybe_notify_error_burst()
    assert len(sent) == 1


async def test_burst_ignores_errors_outside_window(sent):
    old = time.time() - notify.BURST_WINDOW - 10
    for i in range(5):
        notify._errors.append((old, f"stale {i}"))
    await notify.maybe_notify_error_burst()
    assert sent == []


async def test_burst_disabled_when_threshold_zero(sent, monkeypatch):
    monkeypatch.setitem(runtime._values, "error_burst_threshold", 0)
    for i in range(50):
        notify.record_error(f"err {i}")
    await notify.maybe_notify_error_burst()
    assert sent == []


async def test_burst_requires_pushover_configured(sent, monkeypatch):
    monkeypatch.setitem(runtime._values, "pushover_enabled", False)
    for i in range(5):
        notify.record_error(f"err {i}")
    await notify.maybe_notify_error_burst()
    assert sent == []


def test_error_counter_handler_counts_errors_only(sent):
    h = notify.handler()
    mk = lambda name, level: logging.LogRecord(name, level, __file__, 1, "boom", None, None)
    h.emit(mk("worker", logging.ERROR))
    h.emit(mk("worker", logging.WARNING))   # below ERROR — ignored
    h.emit(mk("notify", logging.ERROR))     # own logger — ignored (no feedback loop)
    assert len(notify._errors) == 1


# --------------------------------------------------------------------------- #
# subscription warnings
# --------------------------------------------------------------------------- #
async def test_subscription_warns_once_per_day(sent):
    await notify.maybe_notify_subscription(3.0, "2026-08-07")
    await notify.maybe_notify_subscription(3.0, "2026-08-07")
    assert len(sent) == 1
    assert "3.0 days" in sent[0][1]


async def test_subscription_outside_window_is_silent(sent):
    await notify.maybe_notify_subscription(12.0, "2026-08-16")
    await notify.maybe_notify_subscription(None, "")
    assert sent == []


async def test_subscription_imminent_uses_high_priority(sent):
    await notify.maybe_notify_subscription(1.5, "2026-08-05")
    assert sent[0][2] == 1


async def test_subscription_expired_message(sent):
    await notify.maybe_notify_subscription(-0.5, "2026-08-03")
    assert "EXPIRED" in sent[0][0]
