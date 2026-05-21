"""Shared database client — one ExcelDatabase instance per process.

Consumers import ``get_supabase`` from this module so they all share the
same instance. The name is kept as ``get_supabase`` for backward
compatibility; it now returns an ExcelDatabase instead of a Supabase client,
but the query-builder interface is identical.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_db_client = None
_db_lock = threading.Lock()


def get_supabase():
    """Return the process-wide Excel-backed database client."""
    global _db_client
    if _db_client is None:
        with _db_lock:
            if _db_client is None:
                from src.excel_db import ExcelDatabase
                db_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "database",
                )
                _db_client = ExcelDatabase(db_dir)
    return _db_client
