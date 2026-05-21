"""Tests for the persistent-login cookie store.

The hard part of the cookie-rehydrate flow lives in the Streamlit
script itself (``app.py::_try_restore_session_from_cookie``) and is
genuinely difficult to unit-test without a Streamlit runtime. These
tests cover the lower-level primitives in
``user_authentication.cookie_store``: the read paths, the no-dep
fallback, and the constants every caller depends on.

The module bypasses ``CookieController`` and calls the lower-level
``_cookie_controller`` function directly with a ``method=`` kwarg
(see the cookie_store docstring for the full rationale). The tests
patch that bound name and assert against the kwargs used.
"""

from __future__ import annotations

import pytest


# Streamlit imports cookie_store at module load. The session_state
# fixture mocks it so the module-level import succeeds without a
# Streamlit runtime.
@pytest.fixture(autouse=True)
def _streamlit_session_state(monkeypatch):
    """Replace ``st.session_state`` with a plain dict for the duration of
    each test, so any cookie_store callers that touch session_state have
    somewhere to write without needing a Streamlit context.
    """
    import streamlit as st

    fake_state = {}
    monkeypatch.setattr(st, "session_state", fake_state)
    yield fake_state


def _install_fake_controller(monkeypatch, fn):
    """Patch ``cookie_store._cookie_controller`` with the given callable.

    The real function is the Streamlit custom-component frontend; tests
    replace it with a Python function that returns whatever the test
    needs and records its kwargs for assertions.
    """
    from user_authentication import cookie_store
    monkeypatch.setattr(cookie_store, "_cookie_controller", fn)


class TestGetAllCookies:
    def test_returns_none_when_dep_missing(self, monkeypatch):
        from user_authentication import cookie_store
        monkeypatch.setattr(cookie_store, "_cookie_controller", None)
        assert cookie_store.get_all_cookies() is None

    def test_returns_empty_dict_when_controller_warming_up(self, monkeypatch):
        """First call returns ``{}`` — the controller hasn't received the
        browser's cookies yet. Caller must rerun to disambiguate."""
        _install_fake_controller(monkeypatch, lambda **kw: {})
        from user_authentication import cookie_store
        assert cookie_store.get_all_cookies() == {}

    def test_returns_empty_dict_when_get_all_returns_none(self, monkeypatch):
        """Some library versions return ``None`` instead of ``{}`` when
        the cookie list hasn't loaded yet. Both must normalise to ``{}``
        so the warmup logic in ``app.py`` works.
        """
        _install_fake_controller(monkeypatch, lambda **kw: None)
        from user_authentication import cookie_store
        assert cookie_store.get_all_cookies() == {}

    def test_returns_loaded_cookies_dict(self, monkeypatch):
        _install_fake_controller(
            monkeypatch,
            lambda **kw: {"ikigai_auth": "abc.def.ghi", "other": "x"},
        )
        from user_authentication import cookie_store
        result = cookie_store.get_all_cookies()
        assert result == {"ikigai_auth": "abc.def.ghi", "other": "x"}

    def test_coerces_non_string_values_to_str(self, monkeypatch):
        """Defensive: a cookie controller can sometimes return numbers
        / bools depending on the cookie source / encoding."""
        _install_fake_controller(
            monkeypatch, lambda **kw: {"flag": True, "n": 42},
        )
        from user_authentication import cookie_store
        result = cookie_store.get_all_cookies()
        assert result == {"flag": "True", "n": "42"}

    def test_returns_none_on_unexpected_exception(self, monkeypatch):
        """A misbehaving controller (e.g. transport stripped the
        component channel) must NOT crash the auth gate."""
        def _raises(**_kw):
            raise RuntimeError("ws closed")
        _install_fake_controller(monkeypatch, _raises)
        from user_authentication import cookie_store
        assert cookie_store.get_all_cookies() is None

    def test_uses_stable_component_key_for_get_all(self, monkeypatch):
        """The ``getAll`` invocation must use the same stable ``key`` on
        every run, so Streamlit recognises it as the same component
        instance and delivers the JS-posted cookies on subsequent
        renders. A regression here breaks rehydration entirely.
        """
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            return {}
        _install_fake_controller(monkeypatch, _spy)
        from user_authentication import cookie_store
        cookie_store.get_all_cookies()
        assert captured.get("method") == "getAll"
        assert captured.get("key") == "ikigai_cookie_ctl"


class TestGetPersistedToken:
    """Convenience wrapper around ``get_all_cookies`` — used by callers
    that don't need to inspect the warmup state (i.e. anything that
    runs after the auth gate has already established the session).
    """

    def test_returns_token_when_cookie_present(self, monkeypatch):
        from user_authentication import cookie_store
        _install_fake_controller(
            monkeypatch,
            lambda **kw: {cookie_store.COOKIE_NAME: "the-token"},
        )
        assert cookie_store.get_persisted_token() == "the-token"

    def test_returns_none_when_cookie_missing(self, monkeypatch):
        _install_fake_controller(monkeypatch, lambda **kw: {"unrelated": "x"})
        from user_authentication import cookie_store
        assert cookie_store.get_persisted_token() is None

    def test_returns_none_when_warming_up(self, monkeypatch):
        """``get_persisted_token`` can't tell warming-up apart from
        no-cookie; it returns None for both. The warmup
        disambiguation is the auth-gate's job, not this helper's.
        """
        _install_fake_controller(monkeypatch, lambda **kw: {})
        from user_authentication import cookie_store
        assert cookie_store.get_persisted_token() is None


class TestPersistTokenWriteSafety:
    """The write path is fire-and-forget (the controller dispatches
    the cookie set asynchronously via postMessage to the parent
    window). We just need to confirm it doesn't blow up in the
    no-dep / exception cases, and uses the right cookie shape.
    """

    def test_persist_no_op_when_dep_missing(self, monkeypatch):
        from user_authentication import cookie_store
        monkeypatch.setattr(cookie_store, "_cookie_controller", None)
        # Must not raise.
        cookie_store.persist_token("anything")

    def test_persist_swallows_exceptions(self, monkeypatch):
        def _raises(**_kw):
            raise RuntimeError("set-cookie blocked")
        _install_fake_controller(monkeypatch, _raises)
        from user_authentication import cookie_store
        # A misbehaving cookie write must NOT block the login flow.
        cookie_store.persist_token("anything")

    def test_persist_calls_set_with_cookie_name_and_token(self, monkeypatch):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            return None
        _install_fake_controller(monkeypatch, _spy)

        from user_authentication import cookie_store
        cookie_store.persist_token("the-bearer")

        assert captured.get("method") == "set"
        assert captured.get("name") == cookie_store.COOKIE_NAME
        assert captured.get("value") == "the-bearer"

        # Validate cookie options shape: lax samesite, root path, and a
        # timezone-aware expiry that's roughly TTL hours away. ``expires``
        # is sent as an ISO-8601 string so the JS bridge can parse it.
        opts = captured.get("options") or {}
        assert opts.get("path") == "/"
        # SameSite=lax is the modern web default; strict would break
        # cross-tab navigation. Pin it so a future caller can't
        # accidentally regress it.
        assert opts.get("sameSite") == "lax"
        assert "expires" in opts

        import datetime as dt
        expires = dt.datetime.fromisoformat(opts["expires"])
        # The persist_token function uses a tz-aware UTC expiry; tolerate
        # naive expiries from older code paths by normalising both sides.
        if expires.tzinfo is None:
            now = dt.datetime.utcnow()
        else:
            now = dt.datetime.now(dt.timezone.utc)
        delta = expires - now
        assert dt.timedelta(hours=cookie_store.COOKIE_TTL_HOURS - 1) <= delta
        assert delta <= dt.timedelta(hours=cookie_store.COOKIE_TTL_HOURS + 1)


class TestClearPersistedToken:
    def test_clear_no_op_when_dep_missing(self, monkeypatch):
        from user_authentication import cookie_store
        monkeypatch.setattr(cookie_store, "_cookie_controller", None)
        cookie_store.clear_persisted_token()  # must not raise

    def test_clear_calls_remove_with_cookie_name(self, monkeypatch):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            return None
        _install_fake_controller(monkeypatch, _spy)

        from user_authentication import cookie_store
        cookie_store.clear_persisted_token()

        assert captured.get("method") == "remove"
        assert captured.get("name") == cookie_store.COOKIE_NAME
        opts = captured.get("options") or {}
        assert opts.get("path") == "/"
        assert opts.get("sameSite") == "lax"

    def test_clear_swallows_exceptions(self, monkeypatch):
        def _raises(**_kw):
            raise KeyError("cookie wasn't there to delete")
        _install_fake_controller(monkeypatch, _raises)
        from user_authentication import cookie_store
        # The desired state is "no cookie" — already there is fine.
        cookie_store.clear_persisted_token()
