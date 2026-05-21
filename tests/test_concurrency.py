"""Tests for api.concurrency — weighted solver gate.

The gate admits solver requests based on plan length (weight) subject to
two ceilings: total active weight and total active count. Rejected
requests get a 503 immediately so the client can retry with jitter.
"""

import threading

import pytest

from flask import Flask

import api.concurrency as conc


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch):
    """Each test gets a clean gate with small limits so we can force races."""
    monkeypatch.setattr(conc, "MAX_SOLVER_WEIGHT", 4)
    monkeypatch.setattr(conc, "MAX_SOLVER_COUNT", 3)
    conc.reset_for_tests()
    yield
    assert conc._active_count == 0, f"leaked active slots: {conc._active_count}"
    assert conc._active_weight == 0, f"leaked weight: {conc._active_weight}"


def test_request_weight_scales_with_days():
    assert conc.request_weight(1) == 1
    assert conc.request_weight(3) == 1
    assert conc.request_weight(4) == 2
    assert conc.request_weight(6) == 2
    assert conc.request_weight(7) == 3
    assert conc.request_weight(10) == 4
    # Oversized requests cap at MAX_SOLVER_WEIGHT so they can still run alone.
    assert conc.request_weight(50) == conc.MAX_SOLVER_WEIGHT


def test_short_plans_pack_in_more_than_long_plans():
    """3 short plans (weight 1 each) all admit; 2 long plans (weight 4)
    cannot both fit when MAX_SOLVER_WEIGHT=4."""
    assert conc._try_acquire(1)
    assert conc._try_acquire(1)
    assert conc._try_acquire(1)
    # 4th would exceed MAX_SOLVER_COUNT=3
    assert not conc._try_acquire(1)
    conc.reset_for_tests()

    assert conc._try_acquire(4)  # one 10-day plan
    assert not conc._try_acquire(1)  # weight budget full
    conc.reset_for_tests()


def test_mixed_workload_admission():
    """1 long (weight 4) saturates the weight pool; a parallel short plan
    is rejected on weight even though count is fine."""
    assert conc._try_acquire(4)
    assert not conc._try_acquire(1)


def test_count_ceiling_independent_of_weight():
    """Even if weight is available, MAX_SOLVER_COUNT caps simultaneous
    solves to avoid runaway memory."""
    # 4 weight units across 3 solves (1+1+1) fills count but not weight
    assert conc._try_acquire(1)
    assert conc._try_acquire(1)
    assert conc._try_acquire(1)
    # 4th is rejected by count ceiling even though 1 unit of weight is free
    assert not conc._try_acquire(1)


def test_solver_slot_context_manager_releases_on_exit():
    with conc.solver_slot(num_days=5) as admitted:
        assert admitted is True
        assert conc._active_count == 1
        assert conc._active_weight == 2
    assert conc._active_count == 0
    assert conc._active_weight == 0


def test_solver_slot_returns_false_when_full():
    # Saturate
    assert conc._try_acquire(4)
    with conc.solver_slot(num_days=5) as admitted:
        assert admitted is False
        assert conc._active_count == 1  # only the manual one
    # Release the manual acquire
    conc._release(4)


def test_solver_slot_releases_on_exception():
    try:
        with conc.solver_slot(num_days=5) as admitted:
            assert admitted is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conc._active_count == 0
    assert conc._active_weight == 0


def _make_app():
    app = Flask(__name__)

    @app.route("/go")
    @conc.solver_gate
    def go():
        return {"ok": True}

    return app


def test_solver_gate_decorator_admits_single_request():
    app = _make_app()
    with app.test_client() as c:
        resp = c.get("/go")
    assert resp.status_code == 200
    assert conc._active_count == 0


def test_solver_gate_decorator_503s_when_full(monkeypatch):
    """Saturate the gate manually, then a decorated endpoint must 503."""
    monkeypatch.setattr(conc, "MAX_SOLVER_COUNT", 1)
    monkeypatch.setattr(conc, "MAX_SOLVER_WEIGHT", 4)

    # Manually take the only slot
    assert conc._try_acquire(2)
    try:
        app = _make_app()
        with app.test_client() as c:
            resp = c.get("/go")
        assert resp.status_code == 503
    finally:
        conc._release(2)


def test_thread_safety_under_concurrent_acquire():
    """Many threads racing to acquire — total active weight never exceeds
    the configured ceiling."""
    successes = []
    lock = threading.Lock()

    def worker():
        if conc._try_acquire(1):
            with lock:
                successes.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # MAX_SOLVER_COUNT=3 is the binding ceiling (1+1+1+1=4 ≤ MAX_SOLVER_WEIGHT=4)
    assert len(successes) == conc.MAX_SOLVER_COUNT
    # Clean up
    for _ in range(len(successes)):
        conc._release(1)
