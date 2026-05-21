"""
HTTP client for the Menu Planning Flask API.
"""

import random
import time
from typing import Any, Callable, Dict, List, Optional

import requests


# HTTP statuses where a one-shot retry is safe and likely to succeed:
#   429: rate limiter rejected us — bucket refills quickly, one retry
#        after jitter often lands inside the next burst.
#   503: solver_gate queue full — the request never ran server-side.
#   502/504: proxy hiccup — rare, but a single retry is cheap.
# 5xx beyond these (500, 501) usually mean a genuine server-side error
# that will repeat; don't retry those.
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_BACKOFF_MIN_SEC = 0.2
_RETRY_BACKOFF_MAX_SEC = 0.7


class RuleDiagnosticsBlockedError(RuntimeError):
    """Raised by ``_parse_response`` when the server returned 422 with
    ``error == 'rule_diagnostics_blocked'``.

    Carries the structured ``rule_diagnostics`` list + ``summary`` dict
    so the Streamlit UI can render the diagnostics expander without
    needing a second API round-trip. The default str() of the
    exception is the human-readable server message — works as a plain
    RuntimeError for any caller that doesn't know about the subclass.
    """

    def __init__(self, message: str, diagnostics: List[Dict[str, Any]],
                 summary: Dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.summary = summary


def _parse_response(resp: requests.Response, default_error: str) -> Dict[str, Any]:
    """Decode a JSON API response and raise on any non-success.

    Handles the API's common envelope: ``{"success": bool, ...}``. If the
    server returned HTML or a non-JSON body (for example a 502 from an
    upstream proxy), ``data`` becomes ``{}`` and the fallback message is
    built from the default prefix and status code.

    A 422 with ``error == 'rule_diagnostics_blocked'`` raises a
    :class:`RuleDiagnosticsBlockedError` (subclass of RuntimeError) so
    callers that catch RuntimeError keep working, but the UI can
    isinstance-check to render the diagnostics expander directly.
    """
    ct = resp.headers.get("content-type", "")
    data: Dict[str, Any] = resp.json() if ct.startswith("application/json") else {}
    if (
        resp.status_code == 422
        and data.get("error") == "rule_diagnostics_blocked"
    ):
        # The 422 path carries structured diagnostics in the body —
        # expose them via the typed exception. The .message defaults
        # to the server's human-readable text so plain RuntimeError
        # handlers still get something useful.
        raise RuleDiagnosticsBlockedError(
            data.get("message")
            or "Pre-flight diagnostics blocked the request.",
            diagnostics=data.get("rule_diagnostics") or [],
            summary=data.get("summary") or {},
        )
    if not resp.ok or not data.get("success"):
        raise RuntimeError(data.get("error", f"{default_error} ({resp.status_code})"))
    return data


def _with_one_retry(
    do_request: Callable[[], requests.Response],
    *,
    retryable: bool,
    sleep: Callable[[float], None] = time.sleep,
    rng: Optional[random.Random] = None,
) -> requests.Response:
    """Run ``do_request``; if *retryable* and the response is a transient
    5xx, sleep with jitter and try once more.

    Kept as a tiny helper rather than baked into each method so the retry
    policy is visible in one place and testable without patching every
    call site. Injection seams for ``sleep`` + ``rng`` keep the tests
    deterministic without touching ``time`` globals.
    """
    resp = do_request()
    if not retryable or resp.status_code not in _RETRY_STATUSES:
        return resp
    backoff = (rng or random).uniform(_RETRY_BACKOFF_MIN_SEC, _RETRY_BACKOFF_MAX_SEC)
    sleep(backoff)
    return do_request()


class MenuApiClient:
    """Wrapper around the Flask API endpoints."""

    def __init__(self, base_url: str = "http://localhost:5000"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def health(self) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/api/v1/health", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def list_clients(self) -> List[str]:
        def _do():
            return self.session.get(
                f"{self.base_url}/api/v1/clients", timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        data = _parse_response(resp, "Failed to list clients")
        return data["clients"]

    def plan(
        self,
        client_name: str,
        start_date: str,
        num_days: int = 5,
        time_limit_seconds: int = 240,
    ) -> Dict[str, Any]:
        payload = {
            "client_name": client_name,
            "start_date": start_date,
            "num_days": num_days,
            "time_limit_seconds": time_limit_seconds,
        }

        def _do():
            return self.session.post(
                f"{self.base_url}/api/v1/plan", json=payload,
                timeout=time_limit_seconds + 30,
            )
        # Retry is safe: /plan has no side effects (nothing is written
        # to history until /save), so the worst case is a second solve.
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Plan failed")

    def regenerate(
        self,
        client_name: str,
        base_plan: Dict[str, Dict[str, str]],
        replace_slots: Dict[str, List[str]],
        start_date: Optional[str] = None,
        num_days: int = 5,
        time_limit_seconds: int = 240,
    ) -> Dict[str, Any]:
        payload = {
            "client_name": client_name,
            "base_plan": base_plan,
            "replace_slots": replace_slots,
            "num_days": num_days,
            "time_limit_seconds": time_limit_seconds,
        }
        if start_date:
            payload["start_date"] = start_date

        def _do():
            return self.session.post(
                f"{self.base_url}/api/v1/regenerate", json=payload,
                timeout=time_limit_seconds + 30,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Regenerate failed")

    def save(
        self,
        client_name: str,
        week_plan: Dict[str, Dict[str, str]],
        week_start: str,
    ) -> Dict[str, Any]:
        # /save overwrites on (client, dates) — re-saving the same week
        # is idempotent (DELETE + INSERT under the hood). We still keep
        # this single-shot: a 502/504 retry after a partial write would
        # be a brief flicker but the second call lands on a clean slate.
        payload = {
            "client_name": client_name,
            "week_plan": week_plan,
            "week_start": week_start,
        }
        resp = self.session.post(
            f"{self.base_url}/api/v1/save", json=payload, timeout=30,
        )
        return _parse_response(resp, "Save failed")

    def diagnose(
        self, client_name: str, start_date: str, num_days: int = 5,
    ) -> Dict[str, Any]:
        """Run pre-flight diagnostics for *(client, start_date, num_days)*.

        Never invokes the solver. Returns the structured envelope:
        ``{success, rule_diagnostics, summary, pool_warnings?}``.

        Used by Streamlit to dry-run a config before committing solver
        time, and by the unit tests that verify /diagnose and /plan's
        pre-flight produce identical diagnostics.
        """
        payload = {
            "client_name": client_name,
            "start_date": start_date,
            "num_days": num_days,
        }

        def _do():
            return self.session.post(
                f"{self.base_url}/api/v1/diagnose", json=payload,
                timeout=15,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Diagnose failed")

    def get_saved_plan(
        self, client_name: str, start_date: str, num_days: int = 5,
    ) -> Dict[str, Any]:
        """Return the saved plan for *(client, start_date, num_days)*.

        Response carries ``exists`` (True iff every requested weekday is
        covered) and ``solution`` in the same shape as ``plan()``. The
        UI uses ``exists`` to decide whether to display the saved plan
        directly or fall back to running the solver.
        """
        params = {
            "client_name": client_name,
            "start_date": start_date,
            "num_days": num_days,
        }

        def _do():
            return self.session.get(
                f"{self.base_url}/api/v1/saved-plan", params=params,
                timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Failed to load saved plan")

    # ----- Customisation editor endpoints -----

    def get_editor_metadata(self) -> Dict[str, Any]:
        def _do():
            return self.session.get(
                f"{self.base_url}/api/v1/editor-metadata", timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Failed to load metadata")

    def get_client_config(self, client_name: str) -> Dict[str, Any]:
        def _do():
            return self.session.get(
                f"{self.base_url}/api/v1/client-config/{client_name}", timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Failed to load config")

    def update_client_config(self, client_name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        # PUT is idempotent thanks to the optimistic version check — a
        # retry after 502/504 is safe: either the first call landed
        # (second gets 409), or it didn't (second applies cleanly).
        def _do():
            return self.session.put(
                f"{self.base_url}/api/v1/client-config/{client_name}",
                json=config, timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Save failed")

    def create_client(self, name: str, active_slots: list) -> Dict[str, Any]:
        # Creating the same name twice is caught server-side (409 / "already
        # exists"), so a retry after a proxy 502 is self-correcting.
        def _do():
            return self.session.post(
                f"{self.base_url}/api/v1/client",
                json={"name": name, "active_slots": active_slots},
                timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Create failed")

    def delete_client(self, client_name: str) -> Dict[str, Any]:
        # Idempotent: if the first call landed, the second gets 404.
        def _do():
            return self.session.delete(
                f"{self.base_url}/api/v1/client/{client_name}", timeout=10,
            )
        resp = _with_one_retry(_do, retryable=True)
        return _parse_response(resp, "Delete failed")
