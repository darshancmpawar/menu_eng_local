"""Tests for SolutionFormatter."""

import datetime as dt

import pytest

from src.solver.solution_formatter import SolutionFormatter


@pytest.fixture
def sample_plan():
    """A minimal week plan with 2 days and 3 slots each."""
    d1 = dt.date(2026, 3, 23)  # Monday
    d2 = dt.date(2026, 3, 24)  # Tuesday
    plan = {
        d1: {
            'welcome_drink': 'mango lassi(Y)',
            'rice': 'jeera rice(Y)',
            'dal': 'dal makhani(R)',
        },
        d2: {
            'welcome_drink': 'mint lemonade(G)',
            'rice': 'fried rice(Y)',
            'dal': 'sambar(R)',
        },
    }
    return plan, [d1, d2]


class TestSolutionFormatter:
    def test_init(self, sample_plan):
        plan, dates = sample_plan
        f = SolutionFormatter(plan, dates)
        assert f.week_plan == plan
        assert f.dates == dates

    def test_to_dict(self, sample_plan):
        plan, dates = sample_plan
        f = SolutionFormatter(plan, dates)
        d = f.to_dict()
        assert '2026-03-23' in d
        assert '2026-03-24' in d
        assert d['2026-03-23']['day_type'] == 'mix'
        assert d['2026-03-24']['day_type'] == 'chinese'
        assert d['2026-03-23']['items']['rice']['item'] == 'jeera rice(Y)'
        assert d['2026-03-23']['items']['rice']['item_base'] == 'jeera rice'

    def test_empty_plan(self):
        f = SolutionFormatter({}, [])
        d = f.to_dict()
        assert d == {}
