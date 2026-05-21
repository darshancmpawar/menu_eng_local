"""User management UI -- create, list, delete users (role-gated)."""

from __future__ import annotations

import html

import streamlit as st

from user_authentication.auth_manager import AuthManager
from user_authentication.session import current_user
from user_authentication.models import ROLE_SUPER_ADMIN


_ROLE_COLORS = {
    "super_admin": ("#3b1114", "#fca5a5"),
    "admin":       ("#2a1508", "#fdba74"),
    "user":        ("#0f2a1d", "#86efac"),
}


def render_user_manager():
    """Render user management page. Only accessible to super_admin and admin."""
    user = current_user()
    if user is None:
        return

    auth = AuthManager()

    # --- Inject page-specific CSS ---
    st.markdown("""
    <style>
        .um-section {
            background: #111113; border: 1px solid #27272a;
            border-radius: 14px; padding: 1.25rem 1.5rem;
            margin-bottom: 1rem;
        }
        .um-section-title {
            font-size: 1rem; font-weight: 700; color: #fafafa;
            margin: 0 0 0.15rem; letter-spacing: -0.2px;
        }
        .um-section-desc {
            font-size: 0.75rem; color: #71717a; margin: 0 0 1rem;
        }
        .user-row {
            display: flex; align-items: center; gap: 0.75rem;
            padding: 0.65rem 0.85rem;
            border-bottom: 1px solid #1c1c1f;
            transition: background 0.15s ease;
        }
        .user-row:last-child { border-bottom: none; }
        .user-row:hover { background: #18181b; border-radius: 8px; }
        .user-avatar-sm {
            width: 34px; height: 34px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.7rem; font-weight: 700; color: #fff; flex-shrink: 0;
        }
        .user-info { flex: 1; min-width: 0; }
        .user-info-name {
            font-size: 0.85rem; font-weight: 600; color: #fafafa;
        }
        .user-info-email {
            font-size: 0.72rem; color: #71717a;
        }
        .role-badge {
            display: inline-block; padding: 2px 8px;
            border-radius: 99px; font-size: 0.62rem; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.04em;
        }
    </style>
    """, unsafe_allow_html=True)

    # --- Show success/error ---
    if st.session_state.get("user_mgmt_msg"):
        msg_type, msg_text = st.session_state.pop("user_mgmt_msg")
        if msg_type == "success":
            st.success(msg_text)
        else:
            st.error(msg_text)

    # ---- Create user form ----
    creatable = user.creatable_roles
    if creatable:
        st.markdown(
            '<div class="um-section">'
            '<p class="um-section-title">Create User</p>'
            '<p class="um-section-desc">Add a new user to the system</p>',
            unsafe_allow_html=True,
        )

        with st.form("create_user_form"):
            col1, col2 = st.columns(2)
            with col1:
                new_email = st.text_input("Email", placeholder="user@example.com")
                new_password = st.text_input("Password", type="password")
            with col2:
                new_name = st.text_input("Profile Name", placeholder="John Doe")
                new_role = st.selectbox("Role", creatable)
            create_submitted = st.form_submit_button(
                "Create User", use_container_width=True,
            )

        st.markdown('</div>', unsafe_allow_html=True)

        if create_submitted:
            try:
                auth.create_user(new_email, new_name, new_password, new_role)
                st.session_state["user_mgmt_msg"] = (
                    "success",
                    f"User '{new_email}' created as {new_role}.",
                )
                st.rerun()
            except ValueError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Failed to create user: {e}")

    # ---- User list ----
    st.markdown(
        '<div class="um-section">'
        '<p class="um-section-title">Existing Users</p>'
        '<p class="um-section-desc">Manage users and their roles</p>',
        unsafe_allow_html=True,
    )

    try:
        users = auth.list_users()
    except Exception as e:
        st.error(f"Failed to load users: {e}")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    if not users:
        st.info("No users found.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    for u in users:
        bg, fg = _ROLE_COLORS.get(u.role, ("#18181b", "#a1a1aa"))
        grad_colors = {
            "super_admin": "#ef4444, #dc2626",
            "admin": "#f59e0b, #d97706",
            "user": "#22c55e, #16a34a",
        }
        grad = grad_colors.get(u.role, "#6366f1, #8b5cf6")
        initials = ''.join(w[0] for w in u.profile_name.split()[:2]).upper() if u.profile_name else '?'

        col_info, col_action = st.columns([4, 1])
        with col_info:
            st.markdown(
                f'<div class="user-row">'
                f'<div class="user-avatar-sm" style="background:linear-gradient(135deg,{grad});">'
                f'{html.escape(initials)}</div>'
                f'<div class="user-info">'
                f'<div class="user-info-name">{html.escape(u.profile_name or "")}</div>'
                f'<div class="user-info-email">{html.escape(u.email or "")}</div>'
                f'</div>'
                f'<span class="role-badge" style="background:{bg};color:{fg};">'
                f'{html.escape(u.role or "")}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with col_action:
            if user.role == ROLE_SUPER_ADMIN and u.email != user.email:
                if st.button("Delete", key=f"del_{u.email}"):
                    try:
                        auth.delete_user(u.email)
                        st.session_state["user_mgmt_msg"] = (
                            "success",
                            f"User '{u.email}' deleted.",
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    st.markdown('</div>', unsafe_allow_html=True)
