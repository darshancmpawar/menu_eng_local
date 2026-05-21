"""End-to-end tests for the /api/v1/metrics counter wiring.

Unit tests for the counter module itself live in test_metrics.py; this
file is specifically about "does hitting /plan actually move
plan_requests_total". The two guarantees we care about:

  1. Successful plan/regenerate requests bump the success outcome.
  2. Solver-side failures bump both solver_failures_total and the
     request's outcome=solver_error series — so Prometheus alerts on
     either work.
"""

import pytest

flask = pytest.importorskip("flask", reason="Flask not installed")
from api.app import app
from api import metrics
import api.auth as api_auth
from api.auth import issue_token
from user_authentication.models import ROLE_SUPER_ADMIN


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _auth_secret(monkeypatch):
    monkeypatch.setattr(api_auth, "API_SECRET_KEY", "test-secret-key")


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {issue_token('test@example.com', ROLE_SUPER_ADMIN)}"}


class TestMetricsEndpoint:
    def test_snapshot_has_counters_key(self, client, auth_headers):
        resp = client.get('/api/v1/metrics', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'counters' in data
        assert 'uptime_seconds' in data

    def test_snapshot_reflects_increments(self, client, auth_headers):
        metrics.incr('sample_total')
        metrics.incr('sample_total')
        resp = client.get('/api/v1/metrics', headers=auth_headers)
        assert resp.get_json()['counters']['sample_total'] == 2


class TestPlanWiring:
    def test_successful_plan_bumps_success_outcome(
        self, client, auth_headers, fake_supabase,
    ):
        resp = client.post('/api/v1/plan', json={
            'client_name': 'Rippling',
            'start_date': '2026-03-23',
            'num_days': 1,
            'time_limit_seconds': 30,
        }, headers=auth_headers)
        assert resp.status_code == 200

        snap = metrics.snapshot()
        assert snap.get('plan_requests_total{outcome="success"}') == 1
        assert 'plan_requests_total{outcome="solver_error"}' not in snap
        assert 'solver_failures_total' not in snap

    def test_solver_failure_bumps_error_outcomes(
        self, client, auth_headers, fake_supabase, monkeypatch,
    ):
        """A solver RuntimeError must bump both outcome=solver_error
        and solver_failures_total so alerts on either fire together."""
        import api.app as api_app

        class _FailingSolver:
            rule_failures = []

            def __init__(self, *_a, **_kw):
                pass

            def solve(self, *_a, **_kw):
                raise RuntimeError("no feasible plan after restarts")

        monkeypatch.setattr(api_app, 'MenuSolver', _FailingSolver)

        resp = client.post('/api/v1/plan', json={
            'client_name': 'Rippling',
            'start_date': '2026-03-23',
            'num_days': 1,
            'time_limit_seconds': 30,
        }, headers=auth_headers)
        assert resp.status_code == 500

        snap = metrics.snapshot()
        assert snap.get('plan_requests_total{outcome="solver_error"}') == 1
        assert snap.get('solver_failures_total') == 1
        assert 'plan_requests_total{outcome="success"}' not in snap


class TestRuleFailureWiring:
    def test_soft_rule_failure_bumps_per_rule_counter(
        self, client, auth_headers, fake_supabase, monkeypatch,
    ):
        """A solve that records entries on solver.rule_failures must
        increment rule_failures_total once per entry, keyed by rule
        name."""
        import api.app as api_app

        class _StubSolver:
            def __init__(self, *_a, **_kw):
                self.rule_failures = [
                    {'rule': 'cuisine', 'phase': 'apply', 'error': '...'},
                    {'rule': 'theme_day', 'phase': 'get_objective_terms', 'error': '...'},
                    {'rule': 'cuisine', 'phase': 'get_objective_terms', 'error': '...'},
                ]

            def solve(self, *_a, **_kw):
                # Return an empty-but-valid shape so the formatter can run.
                import datetime as dt
                d = dt.date(2026, 3, 23)
                return {d: {}}, [d]

        monkeypatch.setattr(api_app, 'MenuSolver', _StubSolver)

        # Formatter walks expanded_slots, so fall back to a no-op formatter
        # to keep the test scoped to the metrics assertion.
        class _StubFormatter:
            def __init__(self, *_a, **_kw):
                pass

            def to_dict(self):
                return {}

        monkeypatch.setattr(api_app, 'SolutionFormatter', _StubFormatter)

        resp = client.post('/api/v1/plan', json={
            'client_name': 'Rippling',
            'start_date': '2026-03-23',
            'num_days': 1,
            'time_limit_seconds': 30,
        }, headers=auth_headers)
        assert resp.status_code == 200

        snap = metrics.snapshot()
        # 2 cuisine entries + 1 theme_day entry, all counted.
        assert snap.get('rule_failures_total{rule="cuisine"}') == 2
        assert snap.get('rule_failures_total{rule="theme_day"}') == 1


class TestRegenerateWiring:
    def test_regenerate_success_bumps_counter(
        self, client, auth_headers, fake_supabase, monkeypatch,
    ):
        import api.app as api_app

        class _StubRegen:
            def __init__(self, *_a, **_kw):
                self.rule_failures = []

            def regenerate(self, *_a, **_kw):
                import datetime as dt
                d = dt.date(2026, 3, 23)
                return {d: {}}, [d]

        class _StubFormatter:
            def __init__(self, *_a, **_kw):
                pass

            def to_dict(self):
                return {}

        monkeypatch.setattr(api_app, 'MenuRegenerator', _StubRegen)
        monkeypatch.setattr(api_app, 'SolutionFormatter', _StubFormatter)

        resp = client.post('/api/v1/regenerate', json={
            'client_name': 'Rippling',
            'start_date': '2026-03-23',
            'num_days': 1,
            'base_plan': {'2026-03-23': {'bread': 'plain_chapatti(B)'}},
            'replace_slots': {'2026-03-23': ['bread']},
        }, headers=auth_headers)
        assert resp.status_code == 200

        snap = metrics.snapshot()
        assert snap.get('regenerate_requests_total{outcome="success"}') == 1
