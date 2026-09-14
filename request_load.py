from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

try:
    from flask import g, has_request_context
except Exception:  # pragma: no cover
    g = None

    def has_request_context() -> bool:
        return False


_lock = threading.Lock()
_active_requests = 0
_last_request_at: datetime | None = None


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except Exception:
        return max(minimum, int(default))


def mark_request_start() -> None:
    global _active_requests
    if has_request_context() and g is not None:
        if getattr(g, "_request_load_started", False):
            return
        g._request_load_started = True
        g._request_load_ended = False
    with _lock:
        _active_requests += 1


def mark_request_end(response: Any = None) -> Any:
    global _active_requests, _last_request_at
    if has_request_context() and g is not None:
        if not getattr(g, "_request_load_started", False) or getattr(g, "_request_load_ended", False):
            return response
        g._request_load_ended = True
    with _lock:
        _active_requests = max(0, _active_requests - 1)
        _last_request_at = datetime.utcnow()
    return response


def get_web_load_snapshot() -> dict:
    with _lock:
        active = int(_active_requests)
        last = _last_request_at
    seconds_since = None
    if isinstance(last, datetime):
        seconds_since = (datetime.utcnow() - last).total_seconds()
    return {
        "active_requests": active,
        "last_request_at": last.isoformat() + "Z" if isinstance(last, datetime) else "",
        "seconds_since_last_request": seconds_since,
    }


def is_web_busy() -> bool:
    snapshot = get_web_load_snapshot()
    if snapshot["active_requests"] > 0:
        return True
    seconds_since = snapshot["seconds_since_last_request"]
    idle_gap = _env_int("WEB_LOAD_MIN_SECONDS_AFTER_REQUEST", 10, minimum=0)
    return seconds_since is not None and seconds_since < idle_gap
