"""Tests for api.config.validate_required_env."""

import pytest

from api.config import REQUIRED_ENV_VARS, validate_required_env


class TestValidateRequiredEnv:
    def test_passes_when_all_set(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "x")
        validate_required_env()  # no raise

    def test_raises_listing_every_missing_var(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(RuntimeError) as exc:
            validate_required_env()
        msg = str(exc.value)
        for name in REQUIRED_ENV_VARS:
            assert name in msg, f"error message should name {name}"

    def test_empty_string_counts_as_missing(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "x")
        monkeypatch.setenv("API_SECRET_KEY", "")
        with pytest.raises(RuntimeError) as exc:
            validate_required_env()
        assert "API_SECRET_KEY" in str(exc.value)

    def test_whitespace_only_counts_as_missing(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "x")
        monkeypatch.setenv("SUPABASE_URL", "   ")
        with pytest.raises(RuntimeError) as exc:
            validate_required_env()
        assert "SUPABASE_URL" in str(exc.value)

    def test_error_names_only_missing_vars(self, monkeypatch):
        for name in REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "x")
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            validate_required_env()
        msg = str(exc.value)
        assert "SUPABASE_KEY" in msg
        assert "API_SECRET_KEY" not in msg
        assert "SUPABASE_URL" not in msg
