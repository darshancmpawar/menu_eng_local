"""Helpers for deciding which local port the Flask backend should use.

Pure-Python module with no Streamlit dependency so it can be unit-tested
in isolation. ``app.py`` wires the result into its session state.
"""

from __future__ import annotations

import socket
from typing import Iterable

import requests


BACKEND_PORT_CANDIDATES = (5000, 5001, 5002, 5003, 5004)


def health_check(port: int) -> bool:
    """True iff ``port`` is serving *our* Flask backend.

    Checks status 200 *and* the well-known body shape. A foreign service
    that happens to be on the same port must not pass this check.
    """
    try:
        resp = requests.get(f"http://localhost:{port}/api/v1/health", timeout=1)
    except (requests.ConnectionError, requests.Timeout):
        return False
    if resp.status_code != 200:
        return False
    try:
        return resp.json().get("status") == "healthy"
    except ValueError:
        return False


def port_is_bindable(port: int) -> bool:
    """True if we can bind the given port on localhost right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_backend_port(
    candidates: Iterable[int] = BACKEND_PORT_CANDIDATES,
) -> int:
    """Pick a port for the backend.

    Priority:
    1. A candidate that is already serving our health endpoint (re-run /
       reconnect scenarios).
    2. The first candidate that is free to bind right now.

    Raises ``RuntimeError`` if every candidate is occupied by a foreign
    service — bailing out is better than connecting the UI to an
    unrelated server on port 5000.
    """
    candidates = list(candidates)
    for p in candidates:
        if health_check(p):
            return p
    for p in candidates:
        if port_is_bindable(p):
            return p
    raise RuntimeError(
        f"Every candidate backend port is occupied by another service: "
        f"{candidates}. Free one and reload."
    )
