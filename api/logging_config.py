"""Structured (JSON) logging configuration and per-request ID middleware."""

from __future__ import annotations

import json
import logging
import logging.config
import uuid
from contextvars import ContextVar
from typing import Optional

# ContextVar carrying the current request's ID so every log record emitted
# during a request automatically includes it without passing it explicitly.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_configured = False


def new_request_id() -> str:
    """Generate a short, URL-safe unique request identifier."""
    return uuid.uuid4().hex[:16]


class RequestIdFilter(logging.Filter):
    """Inject ``request_id`` from the ContextVar into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Standard fields are always present; the record's ``extra`` dict is
    merged in at the top level so callers can add structured context via
    ``logger.info("msg", extra={...})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        # Merge any extra fields the caller passed in.
        for key, val in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and key not in base:
                base[key] = val
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


def configure_logging(level: Optional[str] = None) -> None:
    """Install the JSON formatter + RequestIdFilter on the root logger.

    Idempotent — safe to call multiple times (only installs once).
    """
    global _configured
    if _configured:
        return
    _configured = True

    effective_level = level or logging.getLevelName(
        logging.getLogger().level or logging.INFO
    )
    root = logging.getLogger()
    root.setLevel(effective_level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    # Avoid double-adding if something already installed a handler.
    if not root.handlers:
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setFormatter(JsonFormatter())
            h.addFilter(RequestIdFilter())
