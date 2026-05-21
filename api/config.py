"""Application configuration constants, sourced from environment variables."""

from __future__ import annotations

import datetime as dt
import os

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_EXCEL_PATH = os.path.join(_BASE_DIR, "data", "raw", "menu_items.xlsx")
MENU_RULES_CONFIG_PATH = os.path.join(_BASE_DIR, "data", "configs", "indian_menu_rules.json")
CLIENT_RULES_CONFIG_PATH = os.path.join(_BASE_DIR, "data", "configs", "client_rules.json")

# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "5000"))
DEBUG = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Solver limits
# ---------------------------------------------------------------------------
MIN_NUM_DAYS = 1
MAX_NUM_DAYS = 20
MIN_TIME_LIMIT_SECONDS = 10
MAX_TIME_LIMIT_SECONDS = 600

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
SUPABASE_TIMEOUT_SECONDS = int(os.getenv("SUPABASE_TIMEOUT_SECONDS", "10"))

# ---------------------------------------------------------------------------
# Auth (kept for api/auth.py backward-compat; not required by the app)
# ---------------------------------------------------------------------------
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")
API_TOKEN_TTL_SECONDS = int(os.getenv("API_TOKEN_TTL_SECONDS", str(24 * 3600)))

# ---------------------------------------------------------------------------
# App timezone
# ---------------------------------------------------------------------------
_APP_TZ = os.getenv("APP_TZ", "Asia/Kolkata")

# ---------------------------------------------------------------------------
# Required environment variables (checked at startup)
# ---------------------------------------------------------------------------
REQUIRED_ENV_VARS = ["SUPABASE_URL", "SUPABASE_KEY", "API_SECRET_KEY"]


def _get_env(name: str) -> str:
    """Return the value of *name* from os.environ or Streamlit secrets."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    try:
        import streamlit as st
        val = str(st.secrets.get(name, "")).strip()
    except Exception:
        pass
    return val


def validate_required_env() -> None:
    """Raise RuntimeError listing every missing or empty required variable."""
    missing = [name for name in REQUIRED_ENV_VARS if not _get_env(name)]
    if missing:
        raise RuntimeError(
            f"Required environment variable(s) not set or empty: "
            f"{', '.join(missing)}. "
            f"Set them in your environment or Streamlit secrets."
        )


def today_in_app_tz() -> dt.date:
    """Return today's date in the application timezone."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(_APP_TZ)).date()
    except Exception:
        return dt.date.today()
