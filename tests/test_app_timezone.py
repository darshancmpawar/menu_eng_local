"""Tests for api.config.today_in_app_tz and APP_TIMEZONE handling."""

import datetime as dt
import importlib
import os

import pytest


@pytest.fixture
def reload_config():
    """Reload api.config with a given APP_TIMEZONE value.

    api.config resolves APP_TZ at import time, so tests that care about
    a specific zone have to re-import the module rather than patch.
    """
    def _reload(tz_value: str | None):
        if tz_value is None:
            os.environ.pop("APP_TIMEZONE", None)
        else:
            os.environ["APP_TIMEZONE"] = tz_value
        import api.config as cfg
        return importlib.reload(cfg)
    yield _reload
    os.environ.pop("APP_TIMEZONE", None)
    import api.config as cfg
    importlib.reload(cfg)


def test_default_tz_is_asia_kolkata(reload_config):
    cfg = reload_config(None)
    # ZoneInfo compares by name via __str__ / key
    assert str(cfg.APP_TZ) == "Asia/Kolkata"


def test_today_uses_configured_tz(reload_config):
    """If we pin the zone to a UTC+14 zone the date there might be
    ahead of UTC — and behind in UTC-11. Compare against the explicit
    datetime in that zone."""
    cfg = reload_config("Pacific/Kiritimati")  # UTC+14, no DST
    expected = dt.datetime.now(cfg.APP_TZ).date()
    assert cfg.today_in_app_tz() == expected


def test_unknown_tz_falls_back_to_utc(reload_config, caplog):
    cfg = reload_config("Not/A_Real_Zone")
    assert cfg.APP_TZ == dt.timezone.utc
    assert any("falling back to UTC" in rec.message for rec in caplog.records)


def test_today_matches_utc_when_tz_is_utc(reload_config):
    cfg = reload_config("UTC")
    assert cfg.today_in_app_tz() == dt.datetime.now(dt.timezone.utc).date()
