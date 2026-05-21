"""Weighted solver concurrency gate.

Each plan request is assigned a *weight* based on its number of days; the
gate admits requests as long as the total active weight stays under
``MAX_SOLVER_WEIGHT`` AND the active count stays under ``MAX_SOLVER_COUNT``.

This lets many short plans run concurrently while keeping a hard ceiling
on simultaneous heavy plans. Rejections still return 503 immediately so
the Streamlit client can retry with jitter — no internal queueing.

Tunables (env-overridable for ops):
    MAX_SOLVER_WEIGHT  total weight units across all active solvers
    MAX_SOLVER_COUNT   hard cap on simultaneous solvers (safety net)
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
from functools import wraps
from typing import Callable, Dict, Iterator

from flask import jsonify

from api import metrics


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MAX_SOLVER_WEIGHT = _env_int("MAX_SOLVER_WEIGHT", 8)
MAX_SOLVER_COUNT = _env_int("MAX_SOLVER_COUNT", 6)
DEFAULT_REQUEST_DAYS = 5  # used when caller (e.g. decorator path) doesn't know

_lock = threading.Lock()
_active_weight = 0
_active_count = 0


def request_weight(num_days: int) -> int:
    """Map plan length → admission weight.

    1–3 days → 1, 4–6 → 2, 7–9 → 3, 10–12 → 4, 13–15 → 5.
    Capped at ``MAX_SOLVER_WEIGHT`` so an oversized request can still be
    admitted alone.
    """
    n = max(1, int(num_days))
    return min(MAX_SOLVER_WEIGHT, max(1, math.ceil(n / 3)))


def _try_acquire(weight: int) -> bool:
    """Reserve ``weight`` units + 1 slot atomically. Returns True on success."""
    global _active_weight, _active_count
    with _lock:
        if _active_count >= MAX_SOLVER_COUNT:
            return False
        if _active_weight + weight > MAX_SOLVER_WEIGHT:
            return False
        _active_weight += weight
        _active_count += 1
        return True


def _release(weight: int) -> None:
    global _active_weight, _active_count
    with _lock:
        _active_weight = max(0, _active_weight - weight)
        _active_count = max(0, _active_count - 1)


def get_stats() -> Dict[str, int]:
    """Snapshot of solver-gate state for /health and metrics."""
    with _lock:
        return {
            "active": _active_count,
            "active_weight": _active_weight,
            "max_concurrent": MAX_SOLVER_COUNT,
            "max_weight": MAX_SOLVER_WEIGHT,
            "queued": 0,
        }


def get_worker_count() -> int:
    """CP-SAT internal worker count when running in non-deterministic mode."""
    return 4


@contextlib.contextmanager
def solver_slot(num_days: int = DEFAULT_REQUEST_DAYS) -> Iterator[bool]:
    """Try to admit a solve weighted by ``num_days``.

    Yields ``True`` if a slot was reserved (caller must do the work), or
    ``False`` if the gate rejected the request (caller should 503). The
    slot is released automatically when the ``with`` block exits.
    """
    weight = request_weight(num_days)
    if not _try_acquire(weight):
        metrics.incr("solver_gate_rejected_total")
        yield False
        return
    try:
        yield True
    finally:
        _release(weight)


def solver_gate(fn: Callable) -> Callable:
    """Backwards-compatible decorator using the 5-day default weight.

    Endpoints that know ``num_days`` should prefer ``solver_slot`` so the
    gate can size the request properly.
    """
    @wraps(fn)
    def inner(*args, **kwargs):
        with solver_slot(DEFAULT_REQUEST_DAYS) as admitted:
            if not admitted:
                return jsonify({
                    "success": False,
                    "error": "Solver busy — too many concurrent requests. Retry shortly.",
                }), 503
            return fn(*args, **kwargs)
    return inner


def reset_for_tests() -> None:
    """Clear gate state. Call from test fixtures."""
    global _active_weight, _active_count
    with _lock:
        _active_weight = 0
        _active_count = 0
