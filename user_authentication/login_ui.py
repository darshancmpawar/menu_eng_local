"""Login form UI component."""

from __future__ import annotations

import time

import streamlit as st

from ui.api_client import MenuApiClient
from user_authentication.cookie_store import persist_token
from user_authentication.models import User
from user_authentication.session import login_user


def render_login_form(api_base_url: str = "http://localhost:5000"):
    """Render a centered login form. Returns True if user just logged in."""
    st.markdown("""
    <style>
        .login-wrapper {
            display: flex; align-items: center; justify-content: center;
            min-height: 70vh;
        }
        .login-card {
            width: 100%; max-width: 380px; margin: 0 auto;
            padding: 2.5rem 2rem 2rem;
            background: #111113;
            border: 1px solid #27272a;
            border-radius: 16px;
            box-shadow: 0 8px 40px rgba(0,0,0,0.5), 0 0 80px rgba(124,58,237,0.06);
        }
        .login-brand {
            text-align: center; margin-bottom: 2rem;
        }
        .login-brand-icon {
            width: 52px; height: 52px; margin: 0 auto 0.75rem;
            border-radius: 14px;
            background: linear-gradient(135deg, #7c3aed, #a78bfa);
            display: flex; align-items: center; justify-content: center;
            font-size: 1.4rem;
            box-shadow: 0 0 30px rgba(124,58,237,0.3);
        }
        .login-brand h1 {
            margin: 0; font-size: 1.35rem; font-weight: 800;
            color: #fafafa; letter-spacing: -0.5px;
        }
        .login-brand p {
            margin: 0.25rem 0 0; font-size: 0.8rem; color: #71717a;
            font-weight: 400;
        }
        .login-footer {
            text-align: center; margin-top: 1.5rem;
            font-size: 0.7rem; color: #3f3f46;
        }
    </style>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown(
            '<div class="login-brand">'
            '<div class="login-brand-icon">&#127835;</div>'
            '<h1>Ikigai Masala</h1>'
            '<p>Sign in to your account</p>'
            '</div>',
            unsafe_allow_html=True,
        )

        with st.form("login_form"):
            email = st.text_input("Email", placeholder="you@example.com")
            password = st.text_input("Password", type="password")
            st.markdown("")
            submitted = st.form_submit_button("Sign In", use_container_width=True)

        if submitted:
            if not email or not password:
                st.error("Please enter both email and password.")
                return False
            try:
                api = MenuApiClient(api_base_url)
                data = api.login(email, password)
                user = User(
                    email=data["email"],
                    profile_name=data["profile_name"],
                    role=data["role"],
                )
                login_user(user, token=data["token"])
                # Persist the token in a browser cookie so the user
                # stays signed in across hard refreshes / new tabs /
                # server restarts. 12h lifetime, signed by the API,
                # auto-cleared on logout.
                persist_token(data["token"])
                # Give the browser time to receive the postMessage from
                # CookieController and actually write the cookie before
                # st.rerun() tears down the component channel.
                time.sleep(0.3)
                st.rerun()
            except RuntimeError as e:
                st.error(f"{e}")
            except Exception as e:
                st.error(f"Login error: {e}")
            return False

    return False
