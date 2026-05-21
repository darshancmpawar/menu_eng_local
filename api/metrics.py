"""In-process metrics counters.

A lightweight replacement for prometheus_client: each counter is a simple
integer, labelled by keyword arguments which are folded into the key using
Prometheus text-format conventions (``name{k="v",…}``).  The snapshot()
output shape is identical to what a future prometheus_client swap would
produce, so the /metrics endpoint surface doesn't move.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict

_lock = threading.Lock()
_counters: Dict[str, int] = defaultdict(int)


def incr(name: str, **labels) -> None:
    """Increment counter *name* (with optional label dimensions) by 1."""
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        key = f"{name}{{{label_str}}}"
    else:
        key = name
    with _lock:
        _counters[key] += 1


def snapshot() -> Dict[str, int]:
    """Return a point-in-time copy of all counters."""
    with _lock:
        return dict(_counters)


def reset() -> None:
    """Clear all counters (for tests)."""
    with _lock:
        _counters.clear()
