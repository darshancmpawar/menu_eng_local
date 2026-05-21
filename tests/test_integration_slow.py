"""End-to-end integration test — real Excel, real rules, multi-day solve.

This is the one test in the suite that exercises the full pipeline the
way the API does: Excel ontology -> pool build -> CP-SAT with every
rule from the production config -> extracted plan. It's marked ``slow``
because a 5-day solve with the full ruleset takes 20-60s depending on
the host. PR CI skips it by default; the nightly / manual workflow
runs it.

What it proves that the unit tests don't:
  * The real rule config in ``data/configs/indian_menu_rules.json``
    loads without warnings.
  * Those rules, together, produce a feasible plan on the bundled
    ontology — no silent pool exhaustion, no INFEASIBLE from conflicting
    pre-filters.
  * Every active slot is filled on every day.
  * No rule ends up on ``solver.rule_failures`` (soft rules shouldn't
    be silently failing in production either).
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("pandas", reason="pandas not installed")
pytest.importorskip("ortools", reason="ortools not installed")

from src.menu_rules import MenuRuleLoader
from src.preprocessor import ExcelReader, DataCleanser
from src.preprocessor.pool_builder import PoolBuilder
from src.solver.menu_solver import MenuSolver, SolverConfig


@pytest.fixture(scope="module")
def cleaned_menu(ensure_sample_data_exists):
    raw = ExcelReader(str(ensure_sample_data_exists)).read()
    return DataCleanser(raw).clean()


@pytest.fixture(scope="module")
def pools(cleaned_menu):
    return PoolBuilder.build_pools(cleaned_menu)


@pytest.fixture(scope="module")
def production_rules():
    """Load the shipped rules config — the same file the API serves from."""
    loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
    rules = loader.load_from_file()
    assert rules, "expected shipped rules to load cleanly"
    return rules


# Keep a generous time budget on CI — this test is not trying to exercise
# the solver's fast-path, just prove it produces a plan with the full rule
# set on real data.
_TIME_LIMIT_SEC = 240


_ACTIVE_SLOTS = [
    'welcome_drink', 'starter', 'soup', 'salad',
    'rice', 'dal', 'veg_gravy', 'veg_dry', 'bread',
    'curd_side', 'dessert',
]


@pytest.mark.slow
def test_full_week_solve_with_production_rules(
    cleaned_menu, pools, production_rules,
):
    """A 5-weekday plan with the shipped rule set must fill every slot
    on every day and not leave any rule on rule_failures."""
    # Monday 2026-03-23 → Friday 2026-03-27 (the weekday range the
    # theme_map covers). Keeping to weekdays avoids dragging in the
    # weekend placeholder themes.
    start = dt.date(2026, 3, 23)
    cfg = SolverConfig(
        days=5,
        start_date=start,
        time_limit_sec=_TIME_LIMIT_SEC,
        active_base_slots=_ACTIVE_SLOTS,
        slot_counts={s: 1 for s in _ACTIVE_SLOTS},
    )

    solver = MenuSolver(
        pools=pools, solver_config=cfg, menu_rules=production_rules,
    )
    plan, dates = solver.solve()

    assert len(dates) == 5, "solver must return one entry per requested day"
    for d in dates:
        day_map = plan[d]
        for slot in _ACTIVE_SLOTS:
            assert slot in day_map, f"day {d} missing slot {slot!r}"
            assert day_map[slot], f"day {d} has empty item for slot {slot!r}"

    # Any soft rule that failed mid-solve lands here. In production we
    # surface it to the admin via the response; in CI we fail the run so
    # a regression in a rule's rule_config doesn't creep in unnoticed.
    assert not solver.rule_failures, (
        "production rules should not record any rule_failures on the "
        f"bundled ontology; got: {solver.rule_failures}"
    )


@pytest.mark.slow
def test_full_week_has_no_item_repeats_within_day(
    cleaned_menu, pools, production_rules,
):
    """Basic quality gate: the same item must not appear twice in one
    day's plan. The unique_items rule in the production config enforces
    this across the horizon, but this assertion guards the narrower
    per-day invariant explicitly."""
    cfg = SolverConfig(
        days=5,
        start_date=dt.date(2026, 3, 23),
        time_limit_sec=_TIME_LIMIT_SEC,
        active_base_slots=_ACTIVE_SLOTS,
        slot_counts={s: 1 for s in _ACTIVE_SLOTS},
    )

    solver = MenuSolver(
        pools=pools, solver_config=cfg, menu_rules=production_rules,
    )
    plan, dates = solver.solve()

    for d in dates:
        items = [plan[d][slot] for slot in _ACTIVE_SLOTS]
        # Strip color suffix — '(Y)' etc. — before dedupe so
        # 'jeera_rice(Y)' and 'jeera_rice(W)' still count as one item.
        from src.solver._helpers import strip_color_suffix
        bases = [strip_color_suffix(x) for x in items]
        assert len(set(bases)) == len(bases), (
            f"day {d} has a duplicate item across slots: {items}"
        )
