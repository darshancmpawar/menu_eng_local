"""Token-bucket rate limiter for Flask endpoints.

Each named limit maintains a per-key token bucket. ``rate_limit("name")``
is used as a Flask route decorator; ``check_rate_limit("name", key)`` is
the function form used directly in endpoint bodies.
"""

from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Callable, Dict, Optional, Tuple

from flask import jsonify, request

from api import metrics


class _TokenBucketLimiter:
    """Per-key token bucket.

    Each unique *key* gets its own bucket starting at *capacity* tokens.
    Tokens refill at *refill_per_second*. ``try_acquire`` takes one token
    and returns ``(allowed, retry_after_seconds)``.
    """

    def __init__(self, name: str, capacity: int, refill_per_second: float):
        self.name = name
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def try_acquire(self, key: str, now: Optional[float] = None) -> Tuple[bool, float]:
        if now is None:
            now = time.monotonic()
        with self._lock:
            tokens, last_ts = self._buckets.get(key, (float(self.capacity), now))
            elapsed = now - last_ts
            tokens = min(float(self.capacity), tokens + elapsed * self.refill_per_second)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            retry_after = (1.0 - tokens) / self.refill_per_second
            self._buckets[key] = (tokens, now)
            return False, retry_after

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Named limits. Modify capacity / refill_per_second here to tune throttling.
_LIMITS: Dict[str, _TokenBucketLimiter] = {
    "plan":        _TokenBucketLimiter("plan",        capacity=5,  refill_per_second=0.1),
    "regenerate":  _TokenBucketLimiter("regenerate",  capacity=10, refill_per_second=0.5),
    "login_ip":    _TokenBucketLimiter("login_ip",    capacity=30, refill_per_second=0.5),
    "login_email": _TokenBucketLimiter("login_email", capacity=5,  refill_per_second=1/12),
}


def reset_for_tests() -> None:
    """Clear all bucket state. Call from test fixtures."""
    for lim in _LIMITS.values():
        lim.reset()


def check_rate_limit(limit_name: str, key: str):
    """Check *key* against the named limit.

    Returns ``None`` if the request is allowed, or a Flask (response, 429)
    tuple if it is rejected.
    """
    limiter = _LIMITS.get(limit_name)
    if limiter is None:
        return None
    allowed, retry_after = limiter.try_acquire(key)
    if allowed:
        metrics.incr("rate_limit_allowed_total", limit=limit_name)
        return None
    metrics.incr("rate_limit_rejected_total", limit=limit_name)
    resp = jsonify({
        "success": False,
        "error": "Too many requests",
        "retry_after": round(retry_after, 2),
    })
    resp.headers["Retry-After"] = str(int(retry_after) + 1)
    return resp, 429


def rate_limit(limit_name: str) -> Callable:
    """Flask route decorator: rate-limit by remote IP."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def inner(*args, **kwargs):
            key = f"ip:{request.remote_addr or 'unknown'}"
            rejection = check_rate_limit(limit_name, key)
            if rejection is not None:
                return rejection
            return fn(*args, **kwargs)
        return inner
    return decorator
