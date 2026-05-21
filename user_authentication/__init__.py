"""User authentication and role-based access control for Ikigai Masala.

Submodules:
    models        — User dataclass, role constants
    auth_manager  — AuthManager (Supabase CRUD, password hashing)
    session       — Streamlit session-state helpers (requires streamlit)
    login_ui      — Login form component (requires streamlit)
    user_manager_ui — User management page (requires streamlit)
"""

from user_authentication.models import (
    User,
    ROLE_SUPER_ADMIN,
    ROLE_ADMIN,
    ROLE_USER,
)
from user_authentication.auth_manager import AuthManager

__all__ = [
    "User",
    "ROLE_SUPER_ADMIN",
    "ROLE_ADMIN",
    "ROLE_USER",
    "AuthManager",
]
