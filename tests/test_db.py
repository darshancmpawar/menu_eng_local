"""Tests for ``src.db.get_supabase`` — specifically the timeout wiring.

The actual ``create_client`` call hits supabase-py code that we don't
want to exercise in tests (it tries to validate the URL / open a
session). Tests stub it out so we can assert on the ``options`` we
pass without a real Supabase backend.

Important: these tests do NOT pop modules from ``sys.modules`` —
doing so was tried first and broke subsequent test files because
``api.app``'s lazy-singleton chain holds references that get out of
sync with reloaded modules. Everything here uses ``monkeypatch.setattr``
so changes are scoped to one test.
"""

from __future__ import annotations

from unittest.mock import MagicMock



class TestBuildClientOptions:
    def test_returns_options_with_all_three_timeouts_when_supported(self):
        """Default supabase-py >= 2.x exposes postgrest /
        storage / function timeouts. All three should be set so any
        sub-client we might touch fails fast."""
        from src.db import _build_client_options
        opts = _build_client_options(timeout_seconds=5.0)
        assert opts is not None
        assert opts.postgrest_client_timeout == 5
        assert opts.storage_client_timeout == 5
        assert opts.function_client_timeout == 5

    def test_subsecond_timeout_floors_to_one(self):
        """Env vars come in as floats but supabase-py wants ints —
        the helper coerces. Values like 0.5 should floor to 1, not
        0 (which would be 'instant timeout', breaking everything)."""
        from src.db import _build_client_options
        opts = _build_client_options(timeout_seconds=0.5)
        assert opts is not None
        assert opts.postgrest_client_timeout == 1

    def test_returns_none_when_kwarg_unsupported(self, monkeypatch):
        """A supabase-py version that has ClientOptions but doesn't
        accept the timeout kwargs (very old 2.x) must not crash —
        the helper returns None and the caller falls back to default
        ``create_client(url, key)`` (no options)."""
        class _OldClientOptions:
            def __init__(self, **kwargs):
                if "postgrest_client_timeout" in kwargs:
                    raise TypeError(
                        "unexpected keyword 'postgrest_client_timeout'"
                    )

        # Patch the symbol the helper imports — keep the real module
        # otherwise so other tests aren't disturbed.
        import supabase.client as supabase_client_mod
        monkeypatch.setattr(
            supabase_client_mod, "ClientOptions", _OldClientOptions,
        )
        from src.db import _build_client_options
        assert _build_client_options(5.0) is None


class TestGetSupabasePassesOptions:
    """Stub ``supabase.create_client`` so we can capture the options
    kwarg the lazy singleton passes on first call. Test scope only —
    the real client is never built."""

    def test_create_client_called_with_options(self, monkeypatch):
        captured = {}

        def _fake_create_client(url, key, options=None):
            captured["url"] = url
            captured["key"] = key
            captured["options"] = options
            return MagicMock(name="fake_supabase_client")

        # Patch supabase.create_client in place — the lazy import
        # inside get_supabase reads from this module attribute every
        # time, so monkeypatch is enough.
        import supabase
        monkeypatch.setattr(supabase, "create_client", _fake_create_client)

        # Force the lazy singleton to rebuild on this call.
        import src.db as db_mod
        monkeypatch.setattr(db_mod, "_sb_client", None)

        # Pin URL / KEY so the lazy build path picks them up from env.
        monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase.invalid")
        monkeypatch.setenv("SUPABASE_KEY", "fake-key")

        client = db_mod.get_supabase()
        assert client is not None
        assert captured["url"] == "http://fake-supabase.invalid"
        assert captured["key"] == "fake-key"
        assert captured["options"] is not None
        # Whatever SUPABASE_TIMEOUT_SECONDS is currently set to (5 by
        # default), that's what should land on the options object.
        from api.config import SUPABASE_TIMEOUT_SECONDS
        assert captured["options"].postgrest_client_timeout == int(
            SUPABASE_TIMEOUT_SECONDS,
        )

    def test_create_client_called_without_options_when_unsupported(
        self, monkeypatch,
    ):
        """When _build_client_options returns None (older supabase-py),
        the caller must call create_client(url, key) without the
        options kwarg — getting *some* client is better than none."""
        captured = {}

        def _fake_create_client(url, key, options=None):
            captured["options"] = options
            return MagicMock(name="fake_client")

        import supabase
        monkeypatch.setattr(supabase, "create_client", _fake_create_client)

        # Force _build_client_options to return None.
        import src.db as db_mod
        monkeypatch.setattr(db_mod, "_build_client_options", lambda _t: None)
        monkeypatch.setattr(db_mod, "_sb_client", None)

        monkeypatch.setenv("SUPABASE_URL", "http://fake-supabase.invalid")
        monkeypatch.setenv("SUPABASE_KEY", "fake-key")

        db_mod.get_supabase()
        assert captured["options"] is None


class TestDefaultTimeoutValue:
    def test_default_is_5_seconds(self):
        """The constant is read at api.config import time. With no
        SUPABASE_TIMEOUT_SECONDS env var set (which is the case for
        the test suite — conftest doesn't set it), the default must
        be 5s. Short enough that a slow Supabase fails fast; long
        enough for normal reads (~200ms p99)."""
        # Read directly — don't reimport, that would poison other
        # tests as we learned the hard way.
        from api.config import SUPABASE_TIMEOUT_SECONDS
        assert SUPABASE_TIMEOUT_SECONDS == 5.0
