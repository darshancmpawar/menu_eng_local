"""Tests for the per-principal rate limiter."""

from __future__ import annotations

import pytest

from api import metrics
from api.rate_limit import (
    _LIMITS,
    _TokenBucketLimiter,
    rate_limit,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    metrics.reset()
    yield
    reset_for_tests()
    metrics.reset()


class TestTokenBucketLimiterUnit:
    def test_first_N_requests_within_burst_succeed(self):
        limiter = _TokenBucketLimiter("t", capacity=3, refill_per_second=1.0)
        for _ in range(3):
            allowed, _ = limiter.try_acquire("u", now=100.0)
            assert allowed
        allowed, retry = limiter.try_acquire("u", now=100.0)
        assert allowed is False
        assert retry > 0, "retry_after must be positive on reject"

    def test_refill_after_time_elapsed(self):
        limiter = _TokenBucketLimiter("t", capacity=2, refill_per_second=2.0)
        limiter.try_acquire("u", now=100.0)
        limiter.try_acquire("u", now=100.0)
        allowed, _ = limiter.try_acquire("u", now=100.0)
        assert allowed is False
        # 0.5s at 2 tokens/sec = 1 fresh token.
        allowed, _ = limiter.try_acquire("u", now=100.5)
        assert allowed is True

    def test_capacity_is_upper_bound_on_refill(self):
        """A long idle must refill at most up to ``capacity``, not beyond,
        so a burst after being idle for hours still only allows ``capacity``
        acquires in a row."""
        limiter = _TokenBucketLimiter("t", capacity=2, refill_per_second=1000.0)
        limiter.try_acquire("u", now=100.0)   # bucket: 2 → 1
        # Very long idle — a naive impl would refill to a huge number here.
        # All at t = 200 (same instant, so no further refill between calls).
        allowed1, _ = limiter.try_acquire("u", now=200.0)
        allowed2, _ = limiter.try_acquire("u", now=200.0)
        allowed3, _ = limiter.try_acquire("u", now=200.0)
        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False, (
            "bucket must have capped refill at capacity=2"
        )

    def test_different_keys_are_independent(self):
        limiter = _TokenBucketLimiter("t", capacity=1, refill_per_second=1.0)
        assert limiter.try_acquire("alice", now=100.0)[0] is True
        assert limiter.try_acquire("bob", now=100.0)[0] is True, (
            "one user exhausting the bucket must not affect another"
        )
        assert limiter.try_acquire("alice", now=100.0)[0] is False

    def test_retry_after_matches_refill_rate(self):
        # Capacity 1, refill 0.5 tokens/sec => after one grab we wait 2s.
        limiter = _TokenBucketLimiter("t", capacity=1, refill_per_second=0.5)
        limiter.try_acquire("u", now=100.0)
        allowed, retry = limiter.try_acquire("u", now=100.0)
        assert allowed is False
        assert abs(retry - 2.0) < 0.01, f"expected ~2s retry, got {retry}"

    def test_rejects_bad_config(self):
        with pytest.raises(ValueError):
            _TokenBucketLimiter("t", capacity=0, refill_per_second=1.0)
        with pytest.raises(ValueError):
            _TokenBucketLimiter("t", capacity=1, refill_per_second=0.0)


class TestRateLimitDecoratorViaFlask:
    """End-to-end through a real Flask app + test client."""

    @pytest.fixture
    def app(self, monkeypatch):
        from flask import Flask, g, jsonify

        # Shrink the plan limit so the test doesn't have to send 11 requests.
        monkeypatch.setitem(
            _LIMITS, "plan",
            _TokenBucketLimiter("plan", capacity=2, refill_per_second=0.001),
        )

        app = Flask(__name__)

        @app.before_request
        def _fake_auth():
            # Emulate what require_api_auth would have set.
            g.api_user = {"email": "alice@test.com", "role": "admin"}

        @app.route("/plan", methods=["POST"])
        @rate_limit("plan")
        def _plan():
            return jsonify({"success": True})

        @app.route("/by-ip", methods=["POST"])
        @rate_limit("plan")
        def _by_ip():
            # No auth_user → IP-based key.
            g.api_user = None
            return jsonify({"success": True})

        return app

    def test_429_when_bucket_empty(self, app):
        with app.test_client() as c:
            assert c.post("/plan").status_code == 200
            assert c.post("/plan").status_code == 200
            resp = c.post("/plan")
        assert resp.status_code == 429
        data = resp.get_json()
        assert data["success"] is False
        assert "Too many requests" in data["error"]
        assert resp.headers.get("Retry-After")
        assert int(resp.headers["Retry-After"]) >= 1

    def test_429_counter_is_bumped(self, app):
        with app.test_client() as c:
            c.post("/plan"); c.post("/plan"); c.post("/plan")  # 3rd rejects
        snap = metrics.snapshot()
        assert snap.get('rate_limit_allowed_total{limit="plan"}') == 2
        assert snap.get('rate_limit_rejected_total{limit="plan"}') == 1

    def test_different_users_have_separate_buckets(self, app):
        """Driving the key directly via before_request gives us tight
        control over who is 'calling' on each request."""
        from flask import g
        next_user = {"email": "alice@test.com"}

        @app.before_request
        def _switch_user():
            g.api_user = {"email": next_user["email"], "role": "admin"}

        with app.test_client() as c:
            # alice drains her bucket (capacity 2), third call gets 429.
            assert c.post("/plan").status_code == 200
            assert c.post("/plan").status_code == 200
            assert c.post("/plan").status_code == 429

            # Switch principal to bob — his bucket is untouched.
            next_user["email"] = "bob@test.com"
            assert c.post("/plan").status_code == 200


class TestCheckRateLimitHelper:
    """Public helper used by /auth/login (which can't use the decorator
    because there's no g.api_user yet — the bucket key has to come
    from request body / headers)."""

    def test_returns_none_when_allowed(self, monkeypatch):
        from flask import Flask
        from api.rate_limit import check_rate_limit, _LIMITS, _TokenBucketLimiter
        monkeypatch.setitem(
            _LIMITS, "plan",
            _TokenBucketLimiter("plan", capacity=2, refill_per_second=0.001),
        )
        app = Flask(__name__)
        with app.test_request_context("/"):
            assert check_rate_limit("plan", "anyone") is None

    def test_returns_429_response_when_bucket_empty(self, monkeypatch):
        from flask import Flask
        from api.rate_limit import check_rate_limit, _LIMITS, _TokenBucketLimiter
        monkeypatch.setitem(
            _LIMITS, "plan",
            _TokenBucketLimiter("plan", capacity=1, refill_per_second=0.001),
        )
        app = Flask(__name__)
        with app.test_request_context("/"):
            assert check_rate_limit("plan", "u") is None  # drains
            resp = check_rate_limit("plan", "u")
            assert resp is not None
            assert resp.status_code == 429
            data = resp.get_json()
            assert data["success"] is False
            assert "Too many requests" in data["error"]
            assert int(resp.headers["Retry-After"]) >= 1

    def test_unknown_bucket_raises(self):
        from api.rate_limit import check_rate_limit
        with pytest.raises(KeyError):
            check_rate_limit("never-registered", "u")


class TestLoginEndpointRateLimit:
    """Tier 1 #2 — /auth/login is gated by login_ip + login_email
    buckets BEFORE bcrypt so a flood can't saturate the threadpool.
    Both buckets are checked; either rejection is enough to 429."""

    @pytest.fixture
    def app(self, monkeypatch):
        """Shrink the login buckets so tests don't have to send 31
        requests to exercise them."""
        from api.rate_limit import _LIMITS, _TokenBucketLimiter
        # capacity 2, refill so slow it's effectively zero in test time
        monkeypatch.setitem(
            _LIMITS, "login_ip",
            _TokenBucketLimiter("login_ip", capacity=2, refill_per_second=0.001),
        )
        monkeypatch.setitem(
            _LIMITS, "login_email",
            _TokenBucketLimiter("login_email", capacity=2, refill_per_second=0.001),
        )
        from api.app import app
        app.config['TESTING'] = True
        return app

    def _post_login(self, client, email, password="wrong", **headers):
        """Send a login attempt. We expect 401 (bad credentials) on
        allowed attempts and 429 once the bucket is exhausted — never
        a 500. The email_lookup is mocked away because we're testing
        the rate-limit gate, not authentication itself."""
        return client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers=headers,
        )

    def test_third_attempt_from_same_ip_and_email_is_blocked(
        self, app, monkeypatch,
    ):
        # Stub the AuthManager so we don't pay bcrypt and we don't
        # need a Supabase fake — every call is "Invalid credentials".
        import api.auth as api_auth
        monkeypatch.setattr(
            api_auth.AuthManager, "authenticate",
            lambda self, email, pw: None,
        )

        with app.test_client() as c:
            # Capacity 2 — first two attempts pass the rate-limit
            # gate (and 401 because creds are wrong). Third hits 429.
            assert self._post_login(c, "alice@test.com").status_code == 401
            assert self._post_login(c, "alice@test.com").status_code == 401
            resp = self._post_login(c, "alice@test.com")
            assert resp.status_code == 429
            data = resp.get_json()
            assert "Too many requests" in data["error"]
            assert resp.headers.get("Retry-After")

    def test_different_emails_from_same_ip_share_ip_bucket(
        self, app, monkeypatch,
    ):
        """The IP bucket protects the bcrypt threadpool, so it must
        deplete regardless of which email is being attempted. With
        capacity 2 the third email_X@... attempt from the same IP
        is rejected — even though no individual email's bucket is full."""
        import api.auth as api_auth
        monkeypatch.setattr(
            api_auth.AuthManager, "authenticate",
            lambda self, email, pw: None,
        )

        with app.test_client() as c:
            assert self._post_login(c, "a@test.com").status_code == 401
            assert self._post_login(c, "b@test.com").status_code == 401
            # IP bucket exhausted; a NEW email can't sneak past.
            resp = self._post_login(c, "c@test.com")
            assert resp.status_code == 429

    def test_different_ips_have_separate_ip_buckets(
        self, app, monkeypatch,
    ):
        """A user behind another IP can still log in even after
        someone else has fully drained their IP bucket. The Flask
        test client lets us spoof remote_addr via the WSGI
        environ hook 'REMOTE_ADDR'."""
        import api.auth as api_auth
        monkeypatch.setattr(
            api_auth.AuthManager, "authenticate",
            lambda self, email, pw: None,
        )

        with app.test_client() as c:
            # First IP — drain to empty (capacity 2).
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "a@test.com", "password": "x"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
            ).status_code == 401
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "a@test.com", "password": "x"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
            ).status_code == 401
            # Same IP, third attempt → 429.
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "a@test.com", "password": "x"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.1"},
            ).status_code == 429
            # Different IP, NEW email (so login_email bucket is fresh).
            # The ip bucket for 10.0.0.2 is also fresh → 401, not 429.
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "z@test.com", "password": "x"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.2"},
            ).status_code == 401

    def test_email_bucket_blocks_credential_stuffing_across_ips(
        self, app, monkeypatch,
    ):
        """Per-account brute force protection: even if the attacker
        rotates source IPs, a single email can't be hammered past the
        login_email cap."""
        import api.auth as api_auth
        monkeypatch.setattr(
            api_auth.AuthManager, "authenticate",
            lambda self, email, pw: None,
        )

        with app.test_client() as c:
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "victim@test.com", "password": "guess1"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.10"},
            ).status_code == 401
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "victim@test.com", "password": "guess2"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.20"},
            ).status_code == 401
            # Email bucket exhausted; rotating IP doesn't help.
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "victim@test.com", "password": "guess3"},
                environ_overrides={"REMOTE_ADDR": "10.0.0.30"},
            ).status_code == 429

    def test_email_normalised_before_bucket_lookup(
        self, app, monkeypatch,
    ):
        """``Foo@Test.com``, ``foo@test.com`` and ``  FOO@TEST.com  ``
        must all hit the same email bucket — otherwise an attacker
        could trivially side-step the cap by varying the case."""
        import api.auth as api_auth
        monkeypatch.setattr(
            api_auth.AuthManager, "authenticate",
            lambda self, email, pw: None,
        )

        with app.test_client() as c:
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "Foo@Test.com", "password": "x"},
            ).status_code == 401
            assert c.post(
                "/api/v1/auth/login",
                json={"email": "  FOO@TEST.com  ", "password": "x"},
            ).status_code == 401
            # Same logical email, third attempt → 429 from email bucket.
            resp = c.post(
                "/api/v1/auth/login",
                json={"email": "foo@test.com", "password": "x"},
            )
            assert resp.status_code == 429

    def test_429_does_not_call_authenticate(self, app, monkeypatch):
        """Critical: the rate-limit gate must short-circuit BEFORE
        bcrypt. Otherwise the whole point — protecting the
        threadpool — is defeated."""
        import api.auth as api_auth
        call_count = {"n": 0}

        def _counting_auth(self, email, pw):
            call_count["n"] += 1
            return None

        monkeypatch.setattr(api_auth.AuthManager, "authenticate", _counting_auth)

        with app.test_client() as c:
            # Drain the email bucket (capacity 2).
            self._post_login(c, "alice@test.com")
            self._post_login(c, "alice@test.com")
            # Third attempt should NOT reach AuthManager.authenticate.
            resp = self._post_login(c, "alice@test.com")
            assert resp.status_code == 429
            assert call_count["n"] == 2, (
                "rate-limited request must short-circuit before "
                f"calling authenticate(); got {call_count['n']} calls"
            )
