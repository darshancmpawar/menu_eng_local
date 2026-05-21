#!/usr/bin/env python3
"""Seed the initial super_admin user.

Required env vars:
    SUPABASE_URL    Supabase project URL
    SUPABASE_KEY    Supabase anon/service key
    ADMIN_EMAIL     email for the super_admin account
    ADMIN_PASSWORD  password (>= 8 chars)

Optional:
    ADMIN_NAME      display name (defaults to the email's local part)

Usage:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    export ADMIN_EMAIL="you@company.com"
    export ADMIN_PASSWORD="<strong password>"
    python scripts/seed_admin.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from user_authentication.auth_manager import AuthManager

ROLE = "super_admin"
MIN_PASSWORD_LEN = 8


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: {name} env var is required", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    email = _required("ADMIN_EMAIL")
    password = _required("ADMIN_PASSWORD")
    if len(password) < MIN_PASSWORD_LEN:
        print(
            f"ERROR: ADMIN_PASSWORD must be at least {MIN_PASSWORD_LEN} characters",
            file=sys.stderr,
        )
        sys.exit(1)
    name = os.environ.get("ADMIN_NAME", "").strip() or email.split("@", 1)[0]

    auth = AuthManager()
    try:
        user = auth.create_user(email, name, password, ROLE)
    except ValueError as e:
        print(f"Skipped: {e}")
        return
    # Deliberately do not echo the password back.
    print(f"Created {user.role}: {user.email} ({user.profile_name})")


if __name__ == "__main__":
    main()
