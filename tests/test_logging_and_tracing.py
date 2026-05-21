"""Tests for structured logging + request-ID middleware."""

import json
import logging


from api.logging_config import (
    JsonFormatter,
    RequestIdFilter,
    configure_logging,
    new_request_id,
    request_id_var,
)


class TestJsonFormatter:
    def _record(self, **overrides):
        rec = logging.LogRecord(
            name="api.test",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        for k, v in overrides.items():
            setattr(rec, k, v)
        return rec

    def test_emits_valid_json_with_core_fields(self):
        fmt = JsonFormatter()
        rec = self._record(request_id="abc123")
        out = fmt.format(rec)
        payload = json.loads(out)
        assert payload["msg"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "api.test"
        assert payload["request_id"] == "abc123"
        assert payload["ts"].endswith("Z")

    def test_extra_fields_are_embedded(self):
        fmt = JsonFormatter()
        rec = self._record(
            request_id="-", method="POST", path="/api/v1/plan", duration_ms=42,
        )
        payload = json.loads(fmt.format(rec))
        assert payload["method"] == "POST"
        assert payload["path"] == "/api/v1/plan"
        assert payload["duration_ms"] == 42

    def test_unserialisable_extra_is_reprd_not_crashed(self):
        fmt = JsonFormatter()

        class _Weird:
            def __repr__(self):
                return "<weird>"

        rec = self._record(request_id="-", weird=_Weird())
        payload = json.loads(fmt.format(rec))
        assert payload["weird"] == "<weird>"

    def test_exception_info_serialised(self):
        fmt = JsonFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys
            rec = self._record(request_id="-", exc_info=sys.exc_info())
        payload = json.loads(fmt.format(rec))
        assert "exc" in payload
        assert "RuntimeError" in payload["exc"]
        assert "boom" in payload["exc"]


class TestRequestIdFilter:
    def test_attaches_current_request_id(self):
        f = RequestIdFilter()
        rec = logging.LogRecord("x", logging.INFO, "t.py", 1, "m", None, None)
        token = request_id_var.set("abc")
        try:
            assert f.filter(rec) is True
            assert rec.request_id == "abc"
        finally:
            request_id_var.reset(token)

    def test_defaults_to_dash_outside_request(self):
        f = RequestIdFilter()
        rec = logging.LogRecord("x", logging.INFO, "t.py", 1, "m", None, None)
        assert f.filter(rec) is True
        assert rec.request_id == "-"


class TestConfigureLogging:
    def teardown_method(self):
        # Restore the human formatter for subsequent tests.
        configure_logging(log_format="plain")

    def test_json_format_env_switches_formatter(self, caplog, monkeypatch, capsys):
        configure_logging(log_format="json")
        logger = logging.getLogger("api.test.json")
        logger.info("hello", extra={"custom_field": "v"})
        captured = capsys.readouterr()
        # The handler prints one JSON line per record to stderr.
        line = next(
            (l for l in captured.err.splitlines() if l.startswith("{")), None,
        )
        assert line is not None, f"no JSON line found in:\n{captured.err}"
        payload = json.loads(line)
        assert payload["msg"] == "hello"
        assert payload["custom_field"] == "v"


class TestRequestIdMiddleware:
    def test_unique_request_ids_per_request(self):
        seen = {new_request_id() for _ in range(100)}
        assert len(seen) == 100

    def test_after_request_sets_header(self, monkeypatch):
        from api.app import app

        with app.test_client() as c:
            resp = c.get("/api/v1/health")
        assert resp.headers.get("X-Request-ID")

    def test_caller_supplied_request_id_is_honoured(self):
        from api.app import app

        with app.test_client() as c:
            resp = c.get(
                "/api/v1/health",
                headers={"X-Request-ID": "trace-42"},
            )
        assert resp.headers["X-Request-ID"] == "trace-42"

    def test_access_log_emitted_for_non_health_routes(self, caplog):
        from api.app import app
        caplog.set_level(logging.INFO, logger="api.app")

        with app.test_client() as c:
            c.get("/")

        # Root endpoint is non-/health, so an http_request line must land.
        http_records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert http_records, "expected one http_request log line"
        rec = http_records[-1]
        assert rec.path == "/"
        assert rec.status == 200
        assert isinstance(rec.duration_ms, int)

    def test_successful_health_check_is_quiet(self, caplog, fake_supabase):
        """A 200 on /health must not log (would spam every uptime probe).
        Requires fake_supabase so _probe_supabase reports reachable."""
        from api.app import app
        caplog.set_level(logging.INFO, logger="api.app")

        with app.test_client() as c:
            resp = c.get("/api/v1/health")
        assert resp.status_code == 200

        http_records = [r for r in caplog.records if r.getMessage() == "http_request"]
        assert all(r.path != "/api/v1/health" for r in http_records)
