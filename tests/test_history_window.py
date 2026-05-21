"""Tests for the dynamic history-lookback window.

The API queries menu_history and week_signatures with a backward
lookback that has to cover the deepest rule cooldown. A fixed
constant used to do the job, but per-client rule overrides can push
cooldowns past the constant and silently truncate the data that's
passed to the solver. ``_effective_history_window(rules)`` is the
runtime version that widens the lookback as needed.
"""

from __future__ import annotations

import logging


from api.app import (
    _effective_history_window,
    _HISTORY_WINDOW_DAYS,
    _HISTORY_WINDOW_SLACK_DAYS,
)


class _Rule:
    """Minimal stand-in for a BaseMenuRule — all we need is the
    ``cooldown_days`` / ``gap_days`` attribute shape."""
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


class TestEffectiveHistoryWindow:
    def test_empty_rules_uses_floor(self):
        assert _effective_history_window([]) == _HISTORY_WINDOW_DAYS

    def test_none_rules_is_safe(self):
        assert _effective_history_window(None) == _HISTORY_WINDOW_DAYS

    def test_small_cooldowns_stay_at_floor(self):
        rules = [_Rule(cooldown_days=10), _Rule(gap_days=5)]
        assert _effective_history_window(rules) == _HISTORY_WINDOW_DAYS

    def test_cooldown_exceeding_floor_widens_window(self):
        # 60d cooldown + 15d slack = 75, bigger than the 45d floor.
        rules = [_Rule(cooldown_days=60)]
        assert _effective_history_window(rules) == 60 + _HISTORY_WINDOW_SLACK_DAYS

    def test_takes_max_across_rules_and_attributes(self):
        rules = [
            _Rule(cooldown_days=20),
            _Rule(gap_days=50),        # this one's the winner
            _Rule(cooldown_days=30),
        ]
        assert _effective_history_window(rules) == 50 + _HISTORY_WINDOW_SLACK_DAYS

    def test_ignores_non_int_attrs(self):
        """A rule that surfaces a non-int cooldown (e.g. a dict from an
        unusual config path) must not blow up the helper."""
        rules = [
            _Rule(cooldown_days="not-a-number"),
            _Rule(gap_days=None),
            _Rule(cooldown_days=35),
        ]
        # 35 is the only meaningful value; 35+15 > 45 floor, so window = 50.
        assert _effective_history_window(rules) == 50

    def test_widening_logs_a_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="api.app")
        rules = [_Rule(cooldown_days=90)]
        _effective_history_window(rules)
        assert any(
            "Widening history lookback" in rec.message for rec in caplog.records
        )

    def test_floor_window_does_not_log(self, caplog):
        caplog.set_level(logging.WARNING, logger="api.app")
        _effective_history_window([_Rule(cooldown_days=10)])
        assert not any(
            "Widening history lookback" in rec.message for rec in caplog.records
        )


class TestSolverInputsPicksCorrectWindow:
    """/plan must assemble rules, compute the effective window, and pass
    it down to _build_history_context. A bug where someone accidentally
    hard-codes the floor again would regress here."""

    def test_plan_threads_effective_window_into_history_context(
        self, fake_supabase, monkeypatch,
    ):
        import api.app as api_app

        captured = {}

        def _capture(df, client_name, start_date, weekday_dates, window_days=None):
            captured["window_days"] = window_days
            # Return empty history so the rest of the solve can proceed.
            import pandas as pd
            from src.history import HistoryManager
            hm = HistoryManager()
            hm.load_from_dataframes(pd.DataFrame(), pd.DataFrame())
            return (
                hm.banned_items_by_date(weekday_dates, const_slots=set()),
                hm.ricebread_ban_by_date(weekday_dates),
                hm.recent_week_signatures(start_date),
            )

        monkeypatch.setattr(api_app, "_build_history_context", _capture)

        # Force a rule with a deep cooldown so the widening path triggers.
        class _DeepRule:
            cooldown_days = 90

        monkeypatch.setattr(
            api_app, "_rules_and_skip_for_client",
            lambda name, dates: ([_DeepRule()], set()),
        )

        import api.auth as api_auth
        from api.auth import issue_token
        from user_authentication.models import ROLE_SUPER_ADMIN
        monkeypatch.setattr(api_auth, "API_SECRET_KEY", "test-secret-key")
        token = issue_token("t@test.com", ROLE_SUPER_ADMIN)

        with api_app.app.test_client() as c:
            c.post("/api/v1/plan", json={
                "client_name": "Rippling",
                "start_date": "2026-03-23",
                "num_days": 1,
                "time_limit_seconds": 30,
            }, headers={"Authorization": f"Bearer {token}"})

        # 90 + 15 slack = 105; the default 45-day floor must be ignored.
        assert captured["window_days"] == 105, (
            f"expected widened window (90+slack), got {captured['window_days']}"
        )
