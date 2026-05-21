"""
Tests for the pre-flight rule-diagnostic system.

Covers:
- The :class:`Diagnostic` data model + JSON round-trip.
- The aggregator (:func:`run_diagnostics`, :func:`summarize`,
  :func:`has_blocking_errors`) including buggy-rule isolation.
- One pass/error scenario per high-signal rule's :meth:`diagnose`.

Test data is built inline with tiny pandas DataFrames — no Excel,
no Supabase — so the suite stays fast and deterministic.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from src.menu_rules import (
    Diagnostic,
    DiagnoseContext,
    DiagnosticPhase,
    DiagnosticSeverity,
    has_blocking_errors,
    run_diagnostics,
    summarize,
)
from src.menu_rules.base_menu_rule import BaseMenuRule, MenuRuleType
from src.menu_rules.cooldown_rules import (
    ItemCooldownMenuRule,
    RiceBreadGapMenuRule,
)
from src.menu_rules.coupling_menu_rule import CouplingMenuRule
from src.menu_rules.cuisine_menu_rule import CuisineMenuRule
from src.menu_rules.diagnostics import pool_size_diagnostics
from src.menu_rules.ingredient_ban_rule import IngredientBanRule
from src.menu_rules.item_frequency_rule import ItemFrequencyRule
from src.menu_rules.nonveg_rules import NonvegBiryaniWeeklyRule
from src.menu_rules.premium_menu_rule import PremiumMenuRule
from src.menu_rules.theme_rules import (
    ThemeDayMenuRule,
    ThemeSlotFilterRule,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@dataclass
class _StubClientCfg:
    """Stand-in for ClientConfig used by DiagnoseContext."""
    slot_counts: Dict[str, int] = field(default_factory=dict)


@dataclass
class _StubSolverCfg:
    """Stand-in for SolverConfig — only the fields rule.diagnose()
    reads (cuisine_col, premium_flag_col, f_*_flags, rice_exclude_items).
    """
    cuisine_col: str = 'cuisine_family'
    cuisine_south_value: str = 'south_indian'
    cuisine_north_value: str = 'north_indian'
    premium_flag_col: Optional[str] = 'is_premium_veg'
    f_chinese_rice: Optional[str] = 'is_chinese_fried_rice'
    f_chinese_nonveg: Optional[str] = 'is_chinese_chicken_gravy'
    f_chinese_veg_gravy: Optional[str] = 'is_chinese_veg_gravy'
    f_chinese_starter: Optional[str] = 'is_chinese_starter'
    f_nonveg_biryani: Optional[str] = 'is_nonveg_biryani'
    f_veg_biryani: Optional[str] = 'is_mixedveg_biryani'
    f_raita: Optional[str] = 'is_raita'
    rice_exclude_items: Set[str] = field(default_factory=set)
    theme_map: Optional[Dict[str, str]] = None
    active_base_slots: Optional[List[str]] = None


def _ctx(
    pools: Dict[str, pd.DataFrame],
    dates: Optional[List[dt.date]] = None,
    day_types: Optional[Dict[dt.date, str]] = None,
    banned_by_date: Optional[Dict[dt.date, Set[str]]] = None,
    ricebread_ban_day: Optional[Dict[dt.date, bool]] = None,
    skip_cells: Optional[Set[Tuple[dt.date, str]]] = None,
    slot_counts: Optional[Dict[str, int]] = None,
    df: Optional[pd.DataFrame] = None,
    active_base_slots: Optional[List[str]] = None,
) -> DiagnoseContext:
    dates = dates or [dt.date(2026, 5, 11)]
    day_types = day_types or {d: 'mix' for d in dates}
    return DiagnoseContext(
        pools=pools,
        dates=dates,
        day_types=day_types,
        cfg=_StubSolverCfg(),
        df=df if df is not None else pd.DataFrame(),
        banned_by_date=banned_by_date or {},
        ricebread_ban_day=ricebread_ban_day or {},
        skip_cells=skip_cells or set(),
        client_cfg=_StubClientCfg(slot_counts=slot_counts or {}),
        active_base_slots=active_base_slots,
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class TestDiagnosticDataModel:
    def test_to_dict_round_trip(self):
        d = Diagnostic(
            rule="x", rule_type="cuisine",
            severity=DiagnosticSeverity.ERROR,
            phase=DiagnosticPhase.APPLY,
            message="msg", suggestion="fix",
            affected={"date": "2026-05-11"},
        )
        out = d.to_dict()
        # Enums must JSON-encode as bare strings via the str mixin.
        assert out["severity"] == "error"
        assert out["phase"] == "apply"
        # Other fields are passthrough.
        assert out["rule_type"] == "cuisine"
        assert out["affected"] == {"date": "2026-05-11"}


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class _BuggyRule(BaseMenuRule):
    """Always raises in diagnose() — guards the freeze-the-planner regression."""

    def __init__(self):
        super().__init__({"name": "buggy", "type": "cuisine"})
        self.rule_type = MenuRuleType.CUISINE

    def apply(self, *args, **kwargs):
        pass

    def diagnose(self, ctx):
        raise RuntimeError("simulated diagnose() bug")


class TestAggregator:
    def test_run_returns_empty_when_no_rules_emit(self):
        """No rules + empty active_base_slots → both the per-rule pass
        and the synthetic pool_size pass are no-ops, so the aggregator
        returns []."""
        ctx = _ctx(
            pools={'rice': pd.DataFrame({'item': ['a']})},
            active_base_slots=[],  # short-circuits pool_size_diagnostics
        )
        assert run_diagnostics([], ctx) == []

    def test_buggy_rule_yields_warning_not_error(self):
        """The big invariant: a bug in diagnose() must NOT promote
        itself through the pre-flight gate. Always WARNING, never
        ERROR — otherwise one bad rule freezes the planner.
        """
        ctx = _ctx(pools={'rice': pd.DataFrame({'item': ['a']})})
        result = run_diagnostics([_BuggyRule()], ctx)
        crashed = [d for d in result if d.rule == 'buggy']
        assert len(crashed) == 1
        assert crashed[0].severity == DiagnosticSeverity.WARNING
        assert 'crashed' in crashed[0].message.lower()
        # Pre-flight gate must NOT trip from a diagnose() bug.
        assert not has_blocking_errors(result)

    def test_sort_order_errors_first(self):
        """Severity-then-rule-type ordering. Tests are written against
        the public ordering invariant, not the underlying dict-of-ints.
        """
        ctx = _ctx(pools={'rice': pd.DataFrame({'item': ['a']})})

        class _E(BaseMenuRule):
            def __init__(self):
                super().__init__({"name": "e", "type": "premium"})
                self.rule_type = MenuRuleType.PREMIUM

            def apply(self, *a, **k):
                pass

            def diagnose(self, ctx):
                return [Diagnostic(
                    rule="e", rule_type=MenuRuleType.PREMIUM.value,
                    severity=DiagnosticSeverity.ERROR,
                    phase=DiagnosticPhase.APPLY, message="m",
                    suggestion="s", affected={},
                )]

        class _W(BaseMenuRule):
            def __init__(self):
                super().__init__({"name": "w", "type": "cuisine"})
                self.rule_type = MenuRuleType.CUISINE

            def apply(self, *a, **k):
                pass

            def diagnose(self, ctx):
                return [Diagnostic(
                    rule="w", rule_type=MenuRuleType.CUISINE.value,
                    severity=DiagnosticSeverity.WARNING,
                    phase=DiagnosticPhase.APPLY, message="m",
                    suggestion="s", affected={},
                )]

        out = run_diagnostics([_W(), _E()], ctx)
        severities = [d.severity for d in out]
        # Errors come first regardless of input order.
        assert severities.index(DiagnosticSeverity.ERROR) < severities.index(
            DiagnosticSeverity.WARNING
        )

    def test_summarize_counts_and_would_succeed(self):
        diags = [
            Diagnostic("a", "cuisine", DiagnosticSeverity.WARNING,
                       DiagnosticPhase.APPLY, "m", "s"),
            Diagnostic("b", "cuisine", DiagnosticSeverity.INFO,
                       DiagnosticPhase.APPLY, "m", "s"),
        ]
        s = summarize(diags)
        assert s == {"errors": 0, "warnings": 1, "infos": 1,
                     "would_succeed": True}

        diags.append(Diagnostic(
            "c", "premium", DiagnosticSeverity.ERROR,
            DiagnosticPhase.APPLY, "m", "s",
        ))
        s = summarize(diags)
        assert s["errors"] == 1
        assert s["would_succeed"] is False


# ---------------------------------------------------------------------------
# pool_size_diagnostics — the synthetic /validate-pools replacement
# ---------------------------------------------------------------------------

class TestPoolSizeDiagnostics:
    """The diagnostics produced by pool_size_diagnostics replace the
    old _validate_pools string list. The shape changed (Diagnostic
    instead of plain str) but the *signal* must stay identical: warn
    when pool < needed, info when pool == needed, nothing otherwise.
    """

    def test_warning_when_pool_smaller_than_count_needed(self):
        d = dt.date(2026, 5, 11)
        pool = pd.DataFrame({'item': ['x']})
        ctx = _ctx(
            pools={'starter': pool},
            dates=[d], day_types={d: 'mix'},
            slot_counts={'starter': 2},
            active_base_slots=['starter'],
        )
        diags = pool_size_diagnostics([], ctx)
        warns = [x for x in diags if x.severity == DiagnosticSeverity.WARNING]
        assert len(warns) == 1
        assert 'starter' in warns[0].message
        assert warns[0].affected['pool_size'] == 1

    def test_info_when_pool_exactly_matches_count(self):
        d = dt.date(2026, 5, 11)
        pool = pd.DataFrame({'item': ['x']})
        ctx = _ctx(
            pools={'starter': pool},
            dates=[d], day_types={d: 'mix'},
            slot_counts={'starter': 1},
            active_base_slots=['starter'],
        )
        diags = pool_size_diagnostics([], ctx)
        assert all(d.severity == DiagnosticSeverity.INFO for d in diags)

    def test_skip_cells_suppresses_diagnostics(self):
        d = dt.date(2026, 5, 11)
        pool = pd.DataFrame({'item': []})  # empty pool would trigger warn
        ctx = _ctx(
            pools={'starter': pool},
            dates=[d], day_types={d: 'mix'},
            slot_counts={'starter': 1},
            skip_cells={(d, 'starter')},
            active_base_slots=['starter'],
        )
        diags = pool_size_diagnostics([], ctx)
        # No warning — the slot is skipped on this date.
        assert all(d.affected.get('slot') != 'starter' for d in diags)


# ---------------------------------------------------------------------------
# Per-rule diagnose() implementations
# ---------------------------------------------------------------------------

class TestItemCooldownDiagnose:
    """The headline case: cooldown bans every candidate on a date,
    pool empties. Should fire an ERROR — this is what unblocks the
    chinese-starter scenario."""

    def test_error_when_cooldown_empties_pool(self):
        d = dt.date(2026, 5, 12)
        pool = pd.DataFrame({'item': ['a', 'b', 'c']})
        ctx = _ctx(
            pools={'starter': pool}, dates=[d], day_types={d: 'chinese'},
            banned_by_date={d: {'a', 'b', 'c'}},
            active_base_slots=['starter'],
        )
        rule = ItemCooldownMenuRule({'name': 'cooldown_20', 'type': 'item_cooldown',
                                     'cooldown_days': 20})
        diags = rule.diagnose(ctx)
        assert len(diags) == 1
        assert diags[0].severity == DiagnosticSeverity.ERROR
        assert diags[0].affected['pool_size_after'] == 0
        # Crucial: the suggestion is actionable, not generic.
        assert 'cooldown' in diags[0].suggestion.lower()

    def test_no_diagnostic_when_pool_still_healthy(self):
        d = dt.date(2026, 5, 12)
        pool = pd.DataFrame({'item': ['a', 'b', 'c', 'd']})
        ctx = _ctx(
            pools={'starter': pool}, dates=[d], day_types={d: 'chinese'},
            banned_by_date={d: {'a'}},  # 1 of 4 banned, 3 left
            slot_counts={'starter': 1}, active_base_slots=['starter'],
        )
        rule = ItemCooldownMenuRule({'name': 'cooldown_20', 'type': 'item_cooldown',
                                     'cooldown_days': 20})
        # Below threshold: 3 remain, 1 needed → no warning.
        assert rule.diagnose(ctx) == []


class TestRiceBreadGapDiagnose:
    def test_error_when_only_rice_breads_in_pool(self):
        d = dt.date(2026, 5, 12)
        pool = pd.DataFrame({
            'item': ['naan_a', 'naan_b'],
            'is_rice_bread': [1, 1],
        })
        ctx = _ctx(
            pools={'bread': pool}, dates=[d], day_types={d: 'mix'},
            ricebread_ban_day={d: True},
            active_base_slots=['bread'],
        )
        rule = RiceBreadGapMenuRule({'name': 'rb_gap', 'type': 'ricebread_gap',
                                     'gap_days': 10})
        diags = rule.diagnose(ctx)
        assert any(x.severity == DiagnosticSeverity.ERROR for x in diags)


class TestThemeDayDiagnose:
    def test_error_when_no_south_items_for_mix_monday(self):
        d = dt.date(2026, 5, 11)  # Monday
        pool = pd.DataFrame({
            'item': ['paneer_butter_masala'],
            'cuisine_family': ['north_indian'],
        })
        ctx = _ctx(
            pools={'veg_gravy': pool, 'rice': pool},
            dates=[d], day_types={d: 'mix'},
            active_base_slots=['veg_gravy', 'rice'],
        )
        rule = ThemeDayMenuRule({'name': 'theme_day', 'type': 'theme_day'})
        diags = rule.diagnose(ctx)
        errors = [x for x in diags if x.severity == DiagnosticSeverity.ERROR]
        # The constraint requires both south + north; only north is present
        # → exactly one error (the missing south cuisine).
        assert len(errors) == 1
        assert 'south_indian' in errors[0].message


class TestThemeSlotFilterDiagnose:
    def test_warning_when_chinese_filter_empties_rice_pool(self):
        d = dt.date(2026, 5, 12)
        # Has rice items but none flagged as chinese-fried-rice.
        pool = pd.DataFrame({
            'item': ['jeera_rice', 'lemon_rice'],
            'is_chinese_fried_rice': [0, 0],
        })
        ctx = _ctx(
            pools={'rice': pool}, dates=[d], day_types={d: 'chinese'},
            active_base_slots=['rice'],
        )
        rule = ThemeSlotFilterRule({'name': 'tsf', 'type': 'theme_slot_filter'})
        diags = rule.diagnose(ctx)
        # Never ERROR — rule falls back to unfiltered. But surface
        # the silent fallback as a WARNING.
        assert all(x.severity != DiagnosticSeverity.ERROR for x in diags)
        assert any(x.severity == DiagnosticSeverity.WARNING for x in diags)


class TestCuisineDiagnose:
    def test_info_when_no_matching_cuisine_items(self):
        d = dt.date(2026, 5, 13)  # Wednesday
        pool = pd.DataFrame({
            'item': ['x', 'y'],
            'cuisine_family': ['north_indian', 'south_indian'],
        })
        ctx = _ctx(
            pools={'veg_gravy': pool}, dates=[d], day_types={d: 'mix'},
            active_base_slots=['veg_gravy'],
        )
        rule = CuisineMenuRule({
            'name': 'italian_wed', 'type': 'cuisine',
            'cuisine_family': 'italian', 'days_of_week': ['wednesday'],
        })
        diags = rule.diagnose(ctx)
        # No italian items anywhere → no-op constraint → INFO.
        assert len(diags) == 1
        assert diags[0].severity == DiagnosticSeverity.INFO


class TestCouplingDiagnose:
    def test_warning_on_rice_bread_without_liquid_rice(self):
        bread = pd.DataFrame({
            'item': ['naan_a'], 'is_rice_bread': [1],
        })
        rice = pd.DataFrame({
            'item': ['jeera_rice'], 'is_liquid_rice': [0],
        })
        starter = pd.DataFrame({
            'item': ['a'], 'is_deep_fried_starter': [1],
        })
        ctx = _ctx(pools={'bread': bread, 'rice': rice, 'starter': starter})
        rule = CouplingMenuRule({'name': 'coup', 'type': 'coupling'})
        diags = rule.diagnose(ctx)
        warns = [x for x in diags if 'liquid-rice' in x.message]
        assert warns and warns[0].severity == DiagnosticSeverity.WARNING


class TestPremiumDiagnose:
    def test_error_when_min_per_horizon_unreachable(self):
        dates = [dt.date(2026, 5, 11), dt.date(2026, 5, 12)]
        # No premium items in any pool.
        pool = pd.DataFrame({'item': ['a', 'b'], 'is_premium_veg': [0, 0]})
        ctx = _ctx(
            pools={'veg_gravy': pool},
            dates=dates,
            day_types={d: 'mix' for d in dates},
            active_base_slots=['veg_gravy'],
        )
        rule = PremiumMenuRule({
            'name': 'prem', 'type': 'premium',
            'min_per_horizon': 1, 'max_per_horizon': 2,
        })
        diags = rule.diagnose(ctx)
        assert any(x.severity == DiagnosticSeverity.ERROR for x in diags)

    def test_no_diagnostic_when_min_is_zero(self):
        d = dt.date(2026, 5, 11)
        pool = pd.DataFrame({'item': ['a'], 'is_premium_veg': [0]})
        ctx = _ctx(
            pools={'veg_gravy': pool}, dates=[d], day_types={d: 'mix'},
            active_base_slots=['veg_gravy'],
        )
        rule = PremiumMenuRule({
            'name': 'prem', 'type': 'premium',
            'min_per_horizon': 0, 'max_per_horizon': 2,
        })
        # min_per_horizon=0 → constraint is "0 to 2", always satisfiable.
        assert rule.diagnose(ctx) == []


class TestIngredientBanDiagnose:
    def test_error_when_ban_empties_pool(self):
        pool = pd.DataFrame({
            'item': ['mush_a', 'mush_b'],
            'key_ingredient': ['mushroom', 'mushroom'],
        })
        ctx = _ctx(
            pools={'veg_gravy': pool}, active_base_slots=['veg_gravy'],
        )
        rule = IngredientBanRule({
            'name': 'no_mushroom', 'type': 'ingredient_ban',
            'ingredients': ['mushroom'],
        })
        diags = rule.diagnose(ctx)
        errors = [x for x in diags if x.severity == DiagnosticSeverity.ERROR]
        assert len(errors) == 1
        assert errors[0].affected['banned_count'] == 2

    def test_info_when_ban_leaves_pool_healthy(self):
        pool = pd.DataFrame({
            'item': ['a', 'b', 'c'],
            'key_ingredient': ['mushroom', 'onion', 'tomato'],
        })
        ctx = _ctx(
            pools={'veg_gravy': pool}, active_base_slots=['veg_gravy'],
        )
        rule = IngredientBanRule({
            'name': 'no_mushroom', 'type': 'ingredient_ban',
            'ingredients': ['mushroom'],
        })
        diags = rule.diagnose(ctx)
        assert all(x.severity == DiagnosticSeverity.INFO for x in diags)


class TestItemFrequencyDiagnose:
    def test_error_when_min_per_week_unreachable(self):
        dates = [dt.date(2026, 5, 11), dt.date(2026, 5, 12)]
        pool = pd.DataFrame({
            'item': ['a', 'b'],
            'sub_category': ['x', 'y'],
        })
        ctx = _ctx(
            pools={'rice': pool}, dates=dates,
            day_types={d: 'mix' for d in dates},
            active_base_slots=['rice'],
        )
        rule = ItemFrequencyRule({
            'name': 'paneer_min', 'type': 'item_frequency',
            'base_slot': 'rice',
            'selector': {'sub_category': 'paneer'},
            'min_per_week': 2,
        })
        diags = rule.diagnose(ctx)
        errors = [x for x in diags if x.severity == DiagnosticSeverity.ERROR]
        assert len(errors) == 1
        assert errors[0].affected['min_per_week'] == 2


class TestNonvegBiryaniDiagnose:
    def test_info_when_no_biryani_items(self):
        pool = pd.DataFrame({
            'item': ['chicken_curry'], 'is_nonveg_biryani': [0],
        })
        ctx = _ctx(pools={'nonveg_main': pool})
        rule = NonvegBiryaniWeeklyRule({
            'name': 'nv_birwk', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 1,
        })
        diags = rule.diagnose(ctx)
        # Constraint is a no-op (no biryani items) — INFO so users know.
        assert diags and diags[0].severity == DiagnosticSeverity.INFO

    def test_effective_max_scales_with_plan_length(self):
        # max_per_week=1 is the per-5-day baseline; longer horizons
        # must lift the cap so 10-day plans (2 Biryani Wednesdays)
        # don't collide with the theme schedule.
        rule = NonvegBiryaniWeeklyRule({
            'name': 'nv_birwk', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 1,
        })
        assert rule.effective_max(5) == 1
        assert rule.effective_max(7) == 2
        assert rule.effective_max(10) == 2
        assert rule.effective_max(14) == 3

    def test_error_when_biryani_days_exceed_cap(self):
        # max_per_week=0 with 5 weekdays that include a Biryani
        # Wednesday is the cleanest infeasibility case: cap forbids
        # any biryani day, but the theme schedule mandates one.
        # Diagnose must surface ERROR pre-flight so the user sees
        # the structural conflict before the solver runs.
        pool = pd.DataFrame({
            'item': ['biryani_a'], 'is_nonveg_biryani': [1],
        })
        dates = [dt.date(2026, 4, 6) + dt.timedelta(days=i) for i in range(5)]
        # Mon Apr 6 → ... → Fri Apr 10. Wed Apr 8 is the biryani day.
        day_types = {
            d: {'monday': 'mix', 'tuesday': 'chinese',
                'wednesday': 'biryani', 'thursday': 'south',
                'friday': 'north'}[d.strftime('%A').lower()]
            for d in dates
        }
        ctx = _ctx(pools={'nonveg_main': pool}, dates=dates,
                   day_types=day_types)
        rule = NonvegBiryaniWeeklyRule({
            'name': 'nv_birwk', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 0,
        })
        diags = rule.diagnose(ctx)
        errors = [d for d in diags if d.severity == DiagnosticSeverity.ERROR]
        assert len(errors) == 1
        assert errors[0].affected['biryani_days_on_horizon'] == 1
        assert errors[0].affected['effective_max'] == 0

    def test_no_error_when_cap_covers_schedule(self):
        # 10-day plan has 2 biryani Wednesdays; scaled cap also = 2.
        # Cap exactly matches demand → no error.
        pool = pd.DataFrame({
            'item': ['biryani_a', 'biryani_b'],
            'is_nonveg_biryani': [1, 1],
        })
        dates = []
        d = dt.date(2026, 4, 6)
        while len(dates) < 10:
            if d.weekday() < 5:
                dates.append(d)
            d += dt.timedelta(days=1)
        day_types = {
            d: {'monday': 'mix', 'tuesday': 'chinese',
                'wednesday': 'biryani', 'thursday': 'south',
                'friday': 'north'}[d.strftime('%A').lower()]
            for d in dates
        }
        ctx = _ctx(pools={'nonveg_main': pool}, dates=dates,
                   day_types=day_types)
        rule = NonvegBiryaniWeeklyRule({
            'name': 'nv_birwk', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 2,  # raised cap
        })
        diags = rule.diagnose(ctx)
        errors = [d for d in diags if d.severity == DiagnosticSeverity.ERROR]
        assert not errors
