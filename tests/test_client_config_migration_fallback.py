"""Tests for the column-missing graceful-fallback paths.

If a deployment hasn't applied the Phase 2 #14 migration that added
``clients.version``, ``get_client_version`` and ``bump_version_if_matches``
must NOT 500 the editor. Instead they log a clear ERROR pointing at the
fix and degrade to "no concurrency check" so the editor stays usable.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from src.client.client_config import (
    ClientConfigLoader,
    _is_undefined_column,
)


# ---------------------------------------------------------------------------
# Helpers — minimal Supabase-shaped fake just for these tests.
# ---------------------------------------------------------------------------


class _PostgrestStyleError(Exception):
    """Mimic supabase-py's APIError shape (has a `code` attribute)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _undefined_column_err():
    return _PostgrestStyleError(
        "42703", 'column clients.version does not exist'
    )


# ---------------------------------------------------------------------------
# _is_undefined_column unit
# ---------------------------------------------------------------------------


class TestIsUndefinedColumnDetection:
    def test_matches_postgres_code(self):
        assert _is_undefined_column(
            _PostgrestStyleError("42703", "anything"),
        )

    def test_matches_message_when_no_code(self):
        assert _is_undefined_column(
            RuntimeError('column "version" does not exist'),
        )

    def test_does_not_match_unrelated_errors(self):
        assert _is_undefined_column(ValueError("nope")) is False
        assert _is_undefined_column(
            _PostgrestStyleError("23505", "unique violation"),
        ) is False


# ---------------------------------------------------------------------------
# Loader fallback paths
# ---------------------------------------------------------------------------


def _loader_with_mock_supabase(monkeypatch):
    """Build a ClientConfigLoader whose `_sb` is a MagicMock we can drive."""
    sb = MagicMock()
    monkeypatch.setattr(
        "src.client.client_config.get_supabase", lambda: sb,
    )
    return ClientConfigLoader(), sb


class TestGetClientVersionFallback:
    def test_returns_1_when_version_column_missing(self, monkeypatch, caplog):
        loader, sb = _loader_with_mock_supabase(monkeypatch)
        # Initial select('version') raises the column-missing error.
        version_query = sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value
        version_query.execute.side_effect = _undefined_column_err()
        # Subsequent select('name') (existence probe) succeeds.
        existence_query = MagicMock()
        existence_query.data = {"name": "Cargil"}

        def _table_select(*args, **kwargs):
            chain = MagicMock()
            chain.eq.return_value.maybe_single.return_value.execute = (
                version_query.execute if args == ("version",) else
                MagicMock(return_value=existence_query)
            )
            return chain

        sb.table.return_value.select = _table_select

        caplog.set_level(logging.ERROR, logger="src.client.client_config")
        assert loader.get_client_version("Cargil") == 1
        assert any(
            "version column missing" in rec.message for rec in caplog.records
        )

    def test_re_raises_unrelated_supabase_errors(self, monkeypatch):
        """A network / auth error must NOT be silently swallowed."""
        loader, sb = _loader_with_mock_supabase(monkeypatch)
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.side_effect = (
            RuntimeError("network down")
        )
        with pytest.raises(RuntimeError, match="network down"):
            loader.get_client_version("Cargil")

    def test_unknown_client_still_404s_under_fallback(
        self, monkeypatch, caplog,
    ):
        """When the column is missing AND the client doesn't exist, the
        existence probe raises ValueError so the API still 404s."""
        loader, sb = _loader_with_mock_supabase(monkeypatch)

        def _select(*args, **kwargs):
            chain = MagicMock()
            if args == ("version",):
                chain.eq.return_value.maybe_single.return_value.execute.side_effect = (
                    _undefined_column_err()
                )
            else:
                # Existence probe: row not found.
                resp = MagicMock()
                resp.data = None
                chain.eq.return_value.maybe_single.return_value.execute.return_value = resp
            return chain

        sb.table.return_value.select = _select
        caplog.set_level(logging.ERROR, logger="src.client.client_config")
        with pytest.raises(ValueError, match="Unknown client"):
            loader.get_client_version("Ghost")


class TestBumpVersionFallback:
    def test_falls_back_when_column_missing(self, monkeypatch, caplog):
        loader, sb = _loader_with_mock_supabase(monkeypatch)

        # update().eq().eq().execute() raises 42703.
        sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.side_effect = (
            _undefined_column_err()
        )
        # _require_client_exists's select succeeds.
        sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = (
            {"name": "Cargil"}
        )

        caplog.set_level(logging.ERROR, logger="src.client.client_config")
        result = loader.bump_version_if_matches("Cargil", expected=1)
        assert result == 1
        assert any(
            "without concurrency check" in rec.message
            for rec in caplog.records
        )
