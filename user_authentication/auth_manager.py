"""Authentication manager — handles login, user CRUD against Supabase users table.

Password storage: bcrypt hash (self-contained, includes salt + cost factor).
Legacy "salt:sha256_hex" hashes are still verified for backward compatibility
and are transparently rehashed to bcrypt on successful login.

A successful legacy verification bumps the
``legacy_sha256_verifications_total`` counter (see ``api.metrics``) so
operators can watch the migration drain. Once it stays at 0 for long
enough to be confident, set ``AUTH_DISABLE_LEGACY_SHA256=true`` in the
environment — the verifier will then reject legacy rows outright, a
prerequisite to deleting this code path entirely.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import List, Optional

import bcrypt

from src.db import get_supabase
from user_authentication.models import User, ALL_ROLES

# Best-effort metrics import — this module is also imported by the
# Streamlit process where the Flask-hosted metrics singleton may not
# be wired yet; counters there are a no-op.
try:
    from api import metrics as _metrics
except Exception:  # pragma: no cover — defensive import
    _metrics = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
# bcrypt hashes always start with "$2" (e.g. "$2b$12$...").  Older SHA-256
# hashes in the DB use the shape "<32-hex-salt>:<64-hex-digest>".

_BCRYPT_ROUNDS = 12


def _hash_password(password: str) -> str:
    """Return a bcrypt hash string (includes salt + cost factor)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode("utf-8")


def _is_legacy_sha256(stored: str) -> bool:
    """True if the stored hash is the old 'salt:sha256_hex' format."""
    if not stored or stored.startswith("$2"):
        return False
    if ":" not in stored:
        return False
    salt, digest = stored.split(":", 1)
    return len(salt) == 32 and len(digest) == 64


def _verify_legacy_sha256(password: str, stored: str) -> bool:
    salt, _ = stored.split(":", 1)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}" == stored


def _legacy_disabled() -> bool:
    """Kill switch for the legacy SHA-256 verification path.

    Read live (not at module import) so operators can flip it without
    a restart in environments that reload config on SIGHUP.
    """
    return os.environ.get("AUTH_DISABLE_LEGACY_SHA256", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _incr_metric(name: str, **labels: str) -> None:
    """Thin wrapper so the metrics dep stays optional."""
    if _metrics is None:
        return
    try:
        _metrics.incr(name, **labels)
    except Exception:  # pragma: no cover — never break login on a metrics bug
        pass


def _verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored bcrypt or legacy SHA-256 hash."""
    if not stored:
        return False
    if stored.startswith("$2"):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False
    if _is_legacy_sha256(stored):
        if _legacy_disabled():
            # Operator has confirmed no legitimate user still has a
            # legacy hash. Reject, count, and move on — the caller
            # surfaces this as "invalid email or password", same as any
            # other failed login.
            _incr_metric("legacy_sha256_verifications_total", result="disabled")
            logger.warning(
                "Legacy SHA-256 hash rejected (kill switch active): "
                "user should reset via the admin UI"
            )
            return False
        ok = _verify_legacy_sha256(password, stored)
        _incr_metric(
            "legacy_sha256_verifications_total",
            result="success" if ok else "fail",
        )
        if ok:
            logger.warning(
                "Legacy SHA-256 hash used — will be rehashed to bcrypt "
                "on this login"
            )
        return ok
    return False


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------

class AuthManager:
    """Handles authentication and user management via Supabase."""

    def __init__(self):
        self._sb = get_supabase()

    # ---- authentication ---------------------------------------------------

    def authenticate(self, email: str, password: str) -> Optional[User]:
        """Verify credentials and return a User on success, None on failure."""
        resp = (
            self._sb.table("users")
            .select("email, profile_name, password_hash, role")
            .eq("email", email.strip().lower())
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        row = rows[0] if rows else None
        if not row:
            return None
        stored = row["password_hash"]
        if not _verify_password(password, stored):
            return None
        # Transparent rehash: upgrade legacy SHA-256 records to bcrypt.
        # Note: if the kill switch is on we never get here (legacy verify
        # returns False above), so this block is specifically the
        # migration path for the window before the switch flips.
        if _is_legacy_sha256(stored):
            try:
                new_hash = _hash_password(password)
                self._sb.table("users").update(
                    {"password_hash": new_hash}
                ).eq("email", row["email"]).execute()
                _incr_metric("auth_legacy_upgrades_total", outcome="success")
            except Exception as exc:
                # Don't block the login on a rehash failure, but make
                # it visible — a repeatedly failing upgrade means the
                # user stays on the legacy hash forever.
                _incr_metric("auth_legacy_upgrades_total", outcome="fail")
                logger.warning(
                    "Legacy-hash rehash failed for user; will retry on "
                    "next login: %s", exc,
                )
        return User(
            email=row["email"],
            profile_name=row["profile_name"],
            role=row["role"],
        )

    # ---- user CRUD --------------------------------------------------------

    def create_user(
        self,
        email: str,
        profile_name: str,
        password: str,
        role: str,
    ) -> User:
        """Create a new user. Raises ValueError on validation failure."""
        email = email.strip().lower()
        profile_name = profile_name.strip()

        if not email or not profile_name or not password:
            raise ValueError("Email, profile name, and password are required.")
        if role not in ALL_ROLES:
            raise ValueError(f"Invalid role: {role}")

        # Check duplicate
        existing = (
            self._sb.table("users")
            .select("email")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if existing.data:
            raise ValueError(f"User with email '{email}' already exists.")

        password_hash = _hash_password(password)
        self._sb.table("users").insert({
            "email": email,
            "profile_name": profile_name,
            "password_hash": password_hash,
            "role": role,
        }).execute()

        return User(email=email, profile_name=profile_name, role=role)

    def list_users(self) -> List[User]:
        """Return all users (no password hashes)."""
        rows = (
            self._sb.table("users")
            .select("email, profile_name, role")
            .order("profile_name")
            .execute()
        )
        return [
            User(email=r["email"], profile_name=r["profile_name"], role=r["role"])
            for r in rows.data
        ]

    def delete_user(self, email: str) -> None:
        """Delete a user by email. Raises ValueError if not found."""
        resp = (
            self._sb.table("users")
            .select("email")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        if not resp.data:
            raise ValueError(f"User '{email}' not found.")
        self._sb.table("users").delete().eq("email", email).execute()

    def update_password(self, email: str, new_password: str) -> None:
        """Update a user's password."""
        email = email.strip().lower()
        if not new_password:
            raise ValueError("Password cannot be empty.")
        password_hash = _hash_password(new_password)
        self._sb.table("users").update({
            "password_hash": password_hash,
        }).eq("email", email).execute()
