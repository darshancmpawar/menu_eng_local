"""Bearer-token auth for the Flask API.

Tokens are URL-safe signed payloads issued by ``POST /api/v1/auth/login``
and verified by ``require_api_auth``. Tokens are self-contained and
time-limited; the server keeps no session state.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable, Optional

from flask import g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api.config import API_SECRET_KEY, API_TOKEN_TTL_SECONDS
from user_authentication.auth_manager import AuthManager
from user_authentication.models import ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_USER

logger = logging.getLogger(__name__)

_SALT = "menu-api-auth-v1"

# Role hierarchy: higher rank implies all lower privileges.
_ROLE_RANK = {ROLE_USER: 1, ROLE_ADMIN: 2, ROLE_SUPER_ADMIN: 3}


def _secret_key() -> str:
    if API_SECRET_KEY:
        return API_SECRET_KEY
    logger.error(
        "API_SECRET_KEY is not configured. Token issuance/verification is disabled. "
        "Set API_SECRET_KEY before starting authenticated API flows."
    )
    raise RuntimeError("API auth misconfigured: API_SECRET_KEY is required")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


def issue_token(email: str, role: str, profile_name: str = "") -> str:
    """Mint a signed bearer token.

    ``profile_name`` is included so /api/v1/auth/whoami can answer
    without hitting Supabase — handy when the Streamlit frontend
    rehydrates a session from a stored cookie on page load and just
    needs to confirm "is this token still mine, and what's my display
    name?". Tokens issued before this field was added simply have an
    empty profile_name (handled gracefully by callers).
    """
    return _serializer().dumps(
        {"email": email, "role": role, "profile_name": profile_name},
    )


def decode_token(token: str) -> dict:
    return _serializer().loads(token, max_age=API_TOKEN_TTL_SECONDS)


def _extract_bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer "):].strip() or None


def require_api_auth(*, min_role: str = ROLE_USER) -> Callable:
    """Decorator: require a valid bearer token with at least ``min_role``.

    Decoded payload is attached to ``flask.g.api_user``.
    """
    if min_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role: {min_role}")
    required_rank = _ROLE_RANK[min_role]

    def wrap(fn: Callable) -> Callable:
        @wraps(fn)
        def inner(*args, **kwargs):
            token = _extract_bearer_token()
            if not token:
                return jsonify({"success": False, "error": "Missing bearer token"}), 401
            try:
                payload = decode_token(token)
            except SignatureExpired:
                return jsonify({"success": False, "error": "Token expired"}), 401
            except BadSignature:
                return jsonify({"success": False, "error": "Invalid token"}), 401
            user_role = payload.get("role")
            if user_role not in _ROLE_RANK:
                return jsonify({"success": False, "error": "Invalid role in token"}), 403
            if _ROLE_RANK[user_role] < required_rank:
                return jsonify({"success": False, "error": "Insufficient role"}), 403
            g.api_user = payload
            return fn(*args, **kwargs)

        return inner

    return wrap


def api_login():
    """POST /api/v1/auth/login — exchange credentials for a bearer token."""
    try:
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        if not email or not password:
            return jsonify({"success": False, "error": "email and password required"}), 400

        # Rate-limit BEFORE bcrypt so a brute-force script can't
        # saturate the threadpool with ~250ms password checks. Two
        # parallel buckets:
        #   - login_ip: protects the threadpool against a flood from
        #     one source. Generous (30/min) so a corporate NAT with
        #     many real users doesn't false-positive.
        #   - login_email: protects an individual account against
        #     credential-stuffing. Tight (5/min, then one every 12s).
        # We check both — both must accept. Either rejection short-
        # circuits to a 429 with Retry-After before we touch the
        # password, leaking nothing about whether the email exists.
        from api.rate_limit import check_rate_limit
        ip_key = f"ip:{request.remote_addr or 'unknown'}"
        email_key = f"email:{email}"
        for limit_name, key in (
            ("login_ip", ip_key),
            ("login_email", email_key),
        ):
            rejection = check_rate_limit(limit_name, key)
            if rejection is not None:
                return rejection

        user = AuthManager().authenticate(email, password)
        if user is None:
            return jsonify({"success": False, "error": "Invalid credentials"}), 401

        return jsonify({
            "success": True,
            "token": issue_token(user.email, user.role, user.profile_name),
            "email": user.email,
            "role": user.role,
            "profile_name": user.profile_name,
            "ttl_seconds": API_TOKEN_TTL_SECONDS,
        })
    except Exception as e:
        logger.exception("Login failed unexpectedly")
        # Include the exception class name so the client can report it back
        # for diagnosis. Full details stay in server logs.
        return jsonify({
            "success": False,
            "error": f"Login error ({type(e).__name__})",
        }), 500


def api_whoami():
    """GET /api/v1/auth/whoami — confirm the bearer token is still
    valid and return the principal it represents.

    Used by the Streamlit frontend to rehydrate a session from a
    stored cookie on page load: if the token is good, call
    login_user(...) and skip the login form; if it's expired/invalid,
    the auth decorator returns 401, the frontend wipes the cookie and
    shows the form. Decoded entirely from the token's signed payload
    so this endpoint never hits Supabase.
    """
    payload = g.api_user  # populated by require_api_auth
    return jsonify({
        "success": True,
        "email": payload.get("email", ""),
        "role": payload.get("role", ""),
        "profile_name": payload.get("profile_name", ""),
    })
