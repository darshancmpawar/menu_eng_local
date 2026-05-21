"""Persistent auth-token storage via a browser cookie.

``st.session_state`` is per-Streamlit-session in-memory storage — it
dies on page hard-refresh, new tab, server restart, and Streamlit-Cloud
hibernation. To keep users signed in across those events the bearer
token has to live somewhere the *browser* keeps. We use a cookie.

Why ``streamlit-cookies-controller`` and not ``extra-streamlit-components``:

  Streamlit custom components are rendered inside a sandboxed iframe
  served from a different origin than the main app
  (e.g. ``qjmnz4vd2y0.streamlit.app`` vs ``yourapp.streamlit.app`` on
  Streamlit Cloud). ``extra-streamlit-components.CookieManager`` calls
  ``document.cookie = ...`` from inside that iframe, so the cookie
  lands on the iframe's origin — the browser never sends it back to
  the main app on refresh. This is the bug we hit in production.

  ``streamlit-cookies-controller.CookieController`` uses ``postMessage``
  to ask the *parent* window to set the cookie, putting it on the
  correct origin where the browser will replay it.

Cookie name: ``ikigai_auth``. Lifetime: 12 hours. The cookie value is
the same signed bearer token issued by ``POST /api/v1/auth/login``;
tampering invalidates the HMAC so the server rejects it on the next
request.

Implementation note — why we bypass CookieController and call
``_cookie_controller`` directly:

  CookieController.__init__ only calls the underlying Streamlit
  component (via ``_cookie_controller(method='getAll', key=KEY)``) on
  the FIRST construction per session — subsequent calls take a cached
  path that skips the component render, so the iframe is unmounted and
  JS→Python cookie posts never arrive.

  Calling ``ctl.refresh()`` to force a re-render works for the read
  path, but causes a DuplicateWidgetID error when ``_get_controller()``
  is called twice in the same script run (e.g., once in
  ``_try_restore_session_from_cookie()`` and once in ``persist_token()``
  during the login form submission). The error is silently swallowed
  by the ``except Exception: pass`` in ``persist_token()``, which means
  the cookie write never happens.

  Bypassing the class and calling ``_cookie_controller`` directly gives
  us precise control: the ``getAll`` call (with its stable key) is made
  exactly ONCE per run from ``get_all_cookies()``, and the ``set``/
  ``remove`` calls use auto-keyed instances (no key argument) so they
  never conflict with the ``getAll`` call regardless of call order.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

# Lazily imported so test/script paths that don't run inside Streamlit
# don't pay the import cost or require the dep to be installed.
try:
    from streamlit_cookies_controller.cookie_controller import _cookie_controller
except ImportError:
    _cookie_controller = None  # type: ignore[assignment]


COOKIE_NAME = "ikigai_auth"
COOKIE_TTL_HOURS = 12
# Stable key for the getAll component instance — must be the same on
# every run so Streamlit recognises it as the same component and
# delivers the JS-posted value on subsequent renders.
_COMPONENT_KEY = "ikigai_cookie_ctl"


def get_all_cookies() -> Optional[Dict[str, str]]:
    """Return every cookie the browser sent, as a dict.

    Returns:
        - ``None`` when the dep isn't available — caller falls back to
          "no persistence" mode silently.
        - ``{}`` either when the component hasn't completed its first
          JS→Python round-trip yet OR when there are genuinely no
          cookies. Callers disambiguate via a retry warmup flag in
          session_state (see ``app.py``).
        - ``{name: value, ...}`` once the component is ready.
    """
    if _cookie_controller is None:
        return None
    try:
        cookies = _cookie_controller(
            method="getAll", key=_COMPONENT_KEY, default={}
        ) or {}
    except Exception:
        return None
    return {k: str(v) for k, v in cookies.items()}


def get_persisted_token() -> Optional[str]:
    """Return the auth-token cookie value, or None."""
    cookies = get_all_cookies()
    if not cookies:
        return None
    return cookies.get(COOKIE_NAME)


def persist_token(token: str) -> None:
    """Store *token* in the auth cookie for ``COOKIE_TTL_HOURS`` hours."""
    if _cookie_controller is None:
        return
    # Use a timezone-aware UTC datetime — ``dt.datetime.utcnow()`` is
    # deprecated in Python 3.12+ and emits a DeprecationWarning.
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=COOKIE_TTL_HOURS)
    options = {
        "path": "/",
        "expires": expires.isoformat(),
        "sameSite": "lax",
    }
    try:
        # No key= here: auto-keyed by script position, so this call
        # never conflicts with the getAll call in get_all_cookies().
        _cookie_controller(
            method="set", name=COOKIE_NAME, value=token, options=options
        )
    except Exception:
        pass


def clear_persisted_token() -> None:
    """Delete the auth cookie so the next page load shows the login form."""
    if _cookie_controller is None:
        return
    options = {"path": "/", "sameSite": "lax"}
    try:
        _cookie_controller(method="remove", name=COOKIE_NAME, options=options)
    except Exception:
        pass
