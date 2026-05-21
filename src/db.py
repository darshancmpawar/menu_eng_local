"""Shared Supabase client — one connection per process.

Consumers (client_config, auth_manager, api.app) import ``get_supabase``
from this module so they all reuse the same ``supabase.Client`` instance
rather than each maintaining their own singleton.

The client is built with a bounded timeout (see
``api.config.SUPABASE_TIMEOUT_SECONDS``) so a slow/unhealthy Supabase
fails fast instead of pinning Flask threads. supabase-py's defaults
are 120s for PostgREST and 20s for storage — both far too long for an
interactive admin UI.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_sb_client = None
_sb_lock = threading.Lock()


def _build_client_options(timeout_seconds: float):
    """Return a ``ClientOptions`` configured with bounded timeouts on
    every Supabase sub-client we might touch.

    Returns ``None`` when the installed supabase-py version doesn't
    expose the timeout fields (older 1.x or some pre-release 2.x), so
    the caller falls back to ``create_client`` defaults gracefully —
    we'd rather have an unbounded client than no client at all.
    """
    try:
        from supabase.client import ClientOptions
    except ImportError:
        return None
    bound = max(1, int(timeout_seconds))
    try:
        return ClientOptions(
            postgrest_client_timeout=bound,   # SQL surface — what we actually use
            storage_client_timeout=bound,     # we don't use storage today, set for parity
            function_client_timeout=bound,    # ditto for edge functions
        )
    except TypeError:
        # Older supabase-py without one or more of these kwargs.
        logger.warning(
            "supabase-py version doesn't accept timeout kwargs on "
            "ClientOptions; using library defaults (could be 120s)."
        )
        return None


def get_supabase():
    """Return a process-wide Supabase client, created lazily on first use."""
    global _sb_client
    if _sb_client is None:
        with _sb_lock:
            if _sb_client is None:
                from supabase import create_client
                try:
                    import streamlit as st
                    url = st.secrets["SUPABASE_URL"]
                    key = st.secrets["SUPABASE_KEY"]
                except Exception:
                    url = os.environ["SUPABASE_URL"]
                    key = os.environ["SUPABASE_KEY"]
                # Read the timeout lazily so api.config doesn't have to
                # be importable at module load (keeps the dep graph
                # acyclic for tests that import src.db without api).
                from api.config import SUPABASE_TIMEOUT_SECONDS
                options = _build_client_options(SUPABASE_TIMEOUT_SECONDS)
                if options is not None:
                    _sb_client = create_client(url, key, options=options)
                else:
                    _sb_client = create_client(url, key)
    return _sb_client
