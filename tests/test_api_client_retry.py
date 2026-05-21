"""Tests for MenuApiClient's one-shot retry on transient 5xx responses.

The retry policy: any request with ``retryable=True`` gets a single
retry with 200-700ms of jitter when the server returns 502, 503, or 504.
Other statuses (including 500 and 4xx) are not retried. /save is
intentionally not retryable because a retry after the first call landed
would duplicate rows in menu_history.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from ui.api_client import MenuApiClient, _with_one_retry


def _fake_response(status: int, payload: Optional[Dict] = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 400
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload or {}
    return resp


class TestRetryHelper:
    def test_single_success_no_retry(self):
        calls: List = []

        def _do():
            calls.append(1)
            return _fake_response(200, {"success": True})

        out = _with_one_retry(
            _do, retryable=True,
            sleep=lambda _s: None, rng=random.Random(0),
        )
        assert out.status_code == 200
        assert len(calls) == 1

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_transient_5xx_gets_one_retry(self, status):
        calls: List = []

        def _do():
            calls.append(1)
            return _fake_response(status, {"success": False, "error": "busy"})

        out = _with_one_retry(
            _do, retryable=True,
            sleep=lambda _s: None, rng=random.Random(0),
        )
        assert out.status_code == status
        assert len(calls) == 2, "retryable 502/503/504 must be attempted twice"

    def test_500_is_not_retried(self):
        calls: List = []

        def _do():
            calls.append(1)
            return _fake_response(500, {"success": False, "error": "boom"})

        _with_one_retry(
            _do, retryable=True,
            sleep=lambda _s: None, rng=random.Random(0),
        )
        assert len(calls) == 1, "500 is a real server error, not transient"

    def test_409_is_not_retried(self):
        calls: List = []

        def _do():
            calls.append(1)
            return _fake_response(409, {"success": False, "error": "stale"})

        _with_one_retry(
            _do, retryable=True,
            sleep=lambda _s: None, rng=random.Random(0),
        )
        assert len(calls) == 1, "4xx is a client error; retry would just bounce"

    def test_retry_recovers_on_second_attempt(self):
        """The common case this feature is meant for: first call hits
        a 503 from the solver_gate queue, second call runs cleanly."""
        statuses = iter([503, 200])

        def _do():
            status = next(statuses)
            return _fake_response(status, {"success": status == 200})

        sleeps: List[float] = []
        out = _with_one_retry(
            _do, retryable=True,
            sleep=sleeps.append, rng=random.Random(42),
        )
        assert out.status_code == 200
        assert len(sleeps) == 1
        assert 0.2 <= sleeps[0] <= 0.7, f"backoff out of range: {sleeps[0]}"

    def test_not_retryable_disables_retry_on_transient_5xx(self):
        calls: List = []

        def _do():
            calls.append(1)
            return _fake_response(503, {})

        _with_one_retry(
            _do, retryable=False,
            sleep=lambda _s: None, rng=random.Random(0),
        )
        assert len(calls) == 1, "retryable=False (e.g. /save) must not retry"


class TestClientIntegration:
    """Smoke tests that prove the call sites go through the retry helper
    (or not) for the methods we care about."""

    def _patch_session(self, monkeypatch, responses_iter):
        """Replace ``session.post/get/put/delete`` with a callable that
        emits the next response from *responses_iter*. Records calls."""
        client = MenuApiClient("http://fake.invalid", token="t")

        log = {"post": [], "get": [], "put": [], "delete": []}

        def _mk(verb):
            def _call(*args, **kwargs):
                log[verb].append((args, kwargs))
                return next(responses_iter)
            return _call

        for verb in ("post", "get", "put", "delete"):
            monkeypatch.setattr(client.session, verb, _mk(verb))
        # Keep tests fast + deterministic regardless of retry jitter.
        import ui.api_client as mod
        monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
        return client, log

    def test_plan_retries_once_on_503(self, monkeypatch):
        responses = iter([
            _fake_response(503, {"success": False, "error": "queue full"}),
            _fake_response(200, {
                "success": True, "solution": {}, "message": "ok",
            }),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        result = client.plan("X", "2026-03-23", num_days=1, time_limit_seconds=30)
        assert result["success"] is True
        assert len(log["post"]) == 2

    def test_save_is_not_retried(self, monkeypatch):
        """Server-side /save is overwrite-idempotent (DELETE + INSERT)
        so a retry would be safe — but the auto-retry path stays off:
        a user-facing "Plan saved" toast on the second attempt is more
        confusing than just bubbling the error up so the user can
        explicitly retry. Pin the single-shot behaviour."""
        responses = iter([
            _fake_response(503, {"success": False, "error": "queue full"}),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        with pytest.raises(RuntimeError):
            client.save("X", {"2026-03-23": {"bread": "naan(B)"}}, "2026-03-23")
        assert len(log["post"]) == 1, "save must be single-shot"

    def test_get_client_config_retries_on_502(self, monkeypatch):
        responses = iter([
            _fake_response(502, {}),
            _fake_response(200, {"success": True, "version": 1}),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        result = client.get_client_config("X")
        assert result["version"] == 1
        assert len(log["get"]) == 2

    def test_get_saved_plan_retries_on_503(self, monkeypatch):
        """/saved-plan is a pure read — a transient 503 from a proxy or
        upstream blip should auto-retry once like every other GET."""
        responses = iter([
            _fake_response(503, {"success": False, "error": "blip"}),
            _fake_response(200, {
                "success": True, "exists": False, "covered_dates": [],
                "source": "history", "solution": {},
            }),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        result = client.get_saved_plan("X", "2026-03-23", num_days=1)
        assert result["exists"] is False
        assert len(log["get"]) == 2

    def test_plan_422_raises_rule_diagnostics_blocked_error(self, monkeypatch):
        """/plan returns 422 with rule_diagnostics_blocked when pre-flight
        fails. The client must raise the typed exception so the UI can
        render the structured expander without re-querying.
        """
        from ui.api_client import RuleDiagnosticsBlockedError
        diag = {
            "rule": "item_cooldown_20d",
            "rule_type": "item_cooldown",
            "severity": "error", "phase": "pre_filter",
            "message": "cooldown banned all candidates",
            "suggestion": "lower cooldown_days",
            "affected": {"date": "2026-05-13", "slot": "starter"},
        }
        responses = iter([
            _fake_response(422, {
                "success": False, "error": "rule_diagnostics_blocked",
                "message": "Pre-flight blocked the request.",
                "rule_diagnostics": [diag],
                "summary": {"errors": 1, "warnings": 0, "infos": 0,
                            "would_succeed": False},
            }),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        with pytest.raises(RuleDiagnosticsBlockedError) as exc_info:
            client.plan("X", "2026-03-23", num_days=1, time_limit_seconds=30)
        # Diagnostics + summary survive to the caller — UI can render
        # them directly without a second round-trip.
        assert exc_info.value.diagnostics == [diag]
        assert exc_info.value.summary["errors"] == 1
        # 422 is NOT in _RETRY_STATUSES, so no second call.
        assert len(log["post"]) == 1

    def test_diagnose_endpoint_retries_on_503(self, monkeypatch):
        """The new /diagnose endpoint is a pure read, same retry policy
        as other GET-shaped readbacks."""
        responses = iter([
            _fake_response(503, {"success": False, "error": "blip"}),
            _fake_response(200, {
                "success": True, "rule_diagnostics": [],
                "summary": {"errors": 0, "warnings": 0, "infos": 0,
                            "would_succeed": True},
            }),
        ])
        client, log = self._patch_session(monkeypatch, responses)
        result = client.diagnose("X", "2026-03-23", num_days=1)
        assert result["summary"]["would_succeed"] is True
        assert len(log["post"]) == 2
