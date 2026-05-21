"""Unit tests for the tiny api.metrics counter module."""

import threading

import pytest

from api import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_unlabeled_counter_increments():
    metrics.incr("plan_requests_total")
    metrics.incr("plan_requests_total")
    metrics.incr("plan_requests_total")
    snap = metrics.snapshot()
    assert snap == {"plan_requests_total": 3}


def test_labeled_counter_keyed_by_label_set():
    metrics.incr("plan_requests_total", status="success")
    metrics.incr("plan_requests_total", status="success")
    metrics.incr("plan_requests_total", status="fail")
    snap = metrics.snapshot()
    assert snap == {
        'plan_requests_total{status="fail"}': 1,
        'plan_requests_total{status="success"}': 2,
    }


def test_label_order_does_not_matter():
    # Two calls that semantically match the same series must collapse
    # into one counter regardless of kwarg ordering.
    metrics.incr("x", a="1", b="2")
    metrics.incr("x", b="2", a="1")
    snap = metrics.snapshot()
    assert snap == {'x{a="1",b="2"}': 2}


def test_custom_amount_is_respected():
    metrics.incr("calls", amount=5)
    metrics.incr("calls", amount=2)
    assert metrics.snapshot() == {"calls": 7}


def test_zero_or_negative_amount_is_ignored():
    metrics.incr("calls", amount=0)
    metrics.incr("calls", amount=-3)
    assert metrics.snapshot() == {}


def test_labels_cast_non_string_values():
    """Callers can pass HTTP status codes / ints without str()-ing first."""
    metrics.incr("http_responses_total", status=200)
    metrics.incr("http_responses_total", status=500)
    snap = metrics.snapshot()
    assert snap == {
        'http_responses_total{status="200"}': 1,
        'http_responses_total{status="500"}': 1,
    }


def test_snapshot_keys_are_sorted():
    metrics.incr("z_last")
    metrics.incr("a_first")
    metrics.incr("m_middle")
    assert list(metrics.snapshot().keys()) == ["a_first", "m_middle", "z_last"]


def test_concurrent_increments_do_not_race():
    """Fifty threads each bumping 1000 times should land exactly 50_000."""
    def _loop():
        for _ in range(1000):
            metrics.incr("concurrent")

    threads = [threading.Thread(target=_loop) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert metrics.snapshot() == {"concurrent": 50_000}
