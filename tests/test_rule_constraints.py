"""
CP-SAT constraint logic tests for menu rules.

Creates real CP-SAT models, adds decision variables, calls rule.apply(),
and verifies the constraints produce correct solutions.
"""

import datetime as dt

import pandas as pd
from ortools.sat.python import cp_model

from src.menu_rules.coupling_menu_rule import CouplingMenuRule
from src.menu_rules.curd_side_menu_rule import CurdSideMenuRule
from src.menu_rules.nonveg_rules import NonvegBiryaniWeeklyRule
from src.menu_rules.premium_menu_rule import PremiumMenuRule
from src.menu_rules.theme_rules import (
    ThemeDayMenuRule, ThemeStarterPreferenceRule,
)
from src.menu_rules.color_rules import WelcomeDrinkColorMenuRule
from src.menu_rules.cooldown_rules import (
    WeekSignatureCooldownMenuRule, _parse_signature_to_expected_map,
)


# ---------------------------------------------------------------------------
# Helpers to build minimal solver-like context
# ---------------------------------------------------------------------------

class _FakeCell:
    """Lightweight stand-in for menu_solver._Cell."""
    def __init__(self, d_idx, date, slot_id, base_slot, rows, x_vars,
                 theme_pref_flags=None):
        self.d_idx = d_idx
        self.date = date
        self.slot_id = slot_id
        self.base_slot = base_slot
        self.cand_rows = rows
        self.x_vars = x_vars
        self.theme_pref_flags = theme_pref_flags or [False] * len(x_vars)


def _find_cells(cells, di, base_slot):
    return [c for c in cells if c.d_idx == di and c.base_slot == base_slot]


def _link_any(model, lits, y):
    if not lits:
        model.Add(y == 0)
        return
    model.Add(sum(lits) >= y)
    for lit in lits:
        model.Add(lit <= y)


def _solve(model):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    status = solver.Solve(model)
    return solver, status


# ---------------------------------------------------------------------------
# ThemeDayMenuRule — Monday mix
# ---------------------------------------------------------------------------

class TestThemeDayConstraint:
    def test_mix_day_requires_south_and_north(self):
        model = cp_model.CpModel()
        # 3 items: south, north, south
        south1 = model.NewBoolVar('south1')
        north1 = model.NewBoolVar('north1')
        south2 = model.NewBoolVar('south2')
        # Exactly one must be picked
        model.Add(south1 + north1 + south2 == 1)

        rule = ThemeDayMenuRule({"name": "mix", "type": "theme_day"})
        ctx = {
            'day_types': ['mix'],
            'monday_south_lits': [south1, south2],
            'monday_north_lits': [north1],
        }
        rule.apply(model, {}, None, ctx)

        # With exactly-one constraint AND requiring >=1 south AND >=1 north,
        # this should be INFEASIBLE (can't pick both with exactly 1 pick)
        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE

    def test_mix_day_feasible_with_two_slots(self):
        model = cp_model.CpModel()
        s1 = model.NewBoolVar('s1')
        n1 = model.NewBoolVar('n1')
        s2 = model.NewBoolVar('s2')
        n2 = model.NewBoolVar('n2')
        # Two slots, each picks one
        model.Add(s1 + n1 == 1)
        model.Add(s2 + n2 == 1)

        rule = ThemeDayMenuRule({"name": "mix", "type": "theme_day"})
        ctx = {
            'day_types': ['mix'],
            'monday_south_lits': [s1, s2],
            'monday_north_lits': [n1, n2],
        }
        rule.apply(model, {}, None, ctx)

        solver, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        # At least one south AND one north selected
        assert solver.Value(s1) + solver.Value(s2) >= 1
        assert solver.Value(n1) + solver.Value(n2) >= 1

    def test_non_mix_day_no_constraints(self):
        model = cp_model.CpModel()
        v = model.NewBoolVar('v')
        model.Add(v == 1)

        rule = ThemeDayMenuRule({"name": "mix", "type": "theme_day"})
        ctx = {'day_types': ['south'], 'monday_south_lits': [], 'monday_north_lits': []}
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


# ---------------------------------------------------------------------------
# WelcomeDrinkColorMenuRule — consecutive-day color
# ---------------------------------------------------------------------------

class TestWelcomeDrinkColorConstraint:
    def test_consecutive_same_color_blocked(self):
        model = cp_model.CpModel()
        d0_red = model.NewBoolVar('d0_red')
        d1_red = model.NewBoolVar('d1_red')
        # Force both to be selected
        model.Add(d0_red == 1)
        model.Add(d1_red == 1)

        rule = WelcomeDrinkColorMenuRule({"name": "wd", "type": "welcome_drink_color"})
        dates = [dt.date(2026, 3, 23), dt.date(2026, 3, 24)]
        ctx = {
            'dates': dates,
            'known_welcome_colors': ['red'],
            'day_welcome_color_vars': {(0, 'red'): [d0_red], (1, 'red'): [d1_red]},
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE

    def test_different_colors_allowed(self):
        model = cp_model.CpModel()
        d0_red = model.NewBoolVar('d0_red')
        d0_green = model.NewBoolVar('d0_green')
        d1_red = model.NewBoolVar('d1_red')
        d1_green = model.NewBoolVar('d1_green')
        model.Add(d0_red + d0_green == 1)
        model.Add(d1_red + d1_green == 1)

        rule = WelcomeDrinkColorMenuRule({"name": "wd", "type": "welcome_drink_color"})
        dates = [dt.date(2026, 3, 23), dt.date(2026, 3, 24)]
        ctx = {
            'dates': dates,
            'known_welcome_colors': ['red', 'green'],
            'day_welcome_color_vars': {
                (0, 'red'): [d0_red], (0, 'green'): [d0_green],
                (1, 'red'): [d1_red], (1, 'green'): [d1_green],
            },
        }
        rule.apply(model, {}, None, ctx)

        solver, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        # They must pick different colors
        assert solver.Value(d0_red) != solver.Value(d1_red)


# ---------------------------------------------------------------------------
# PremiumMenuRule — daily / horizon limits
# ---------------------------------------------------------------------------

class TestPremiumConstraint:
    def _make_cfg(self):
        return type('Cfg', (), {'premium_flag_col': 'is_premium_veg'})()

    def test_max_per_day_enforced(self):
        model = cp_model.CpModel()
        p1 = model.NewBoolVar('p1')
        p2 = model.NewBoolVar('p2')
        # Try to force both premium items on day 0
        model.Add(p1 == 1)
        model.Add(p2 == 1)

        rule = PremiumMenuRule({"name": "prem", "type": "premium",
                                "max_per_day": 1, "min_per_horizon": 0, "max_per_horizon": 5})
        ctx = {
            'cfg': self._make_cfg(),
            'dates': [dt.date(2026, 3, 23)],
            'day_premium_vars': {0: [p1, p2]},
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE

    def test_horizon_min_enforced(self):
        model = cp_model.CpModel()
        p1 = model.NewBoolVar('p1')
        np1 = model.NewBoolVar('np1')
        model.Add(p1 + np1 == 1)
        # Force no premium
        model.Add(p1 == 0)

        rule = PremiumMenuRule({"name": "prem", "type": "premium",
                                "max_per_day": 1, "min_per_horizon": 1, "max_per_horizon": 2})
        ctx = {
            'cfg': self._make_cfg(),
            'dates': [dt.date(2026, 3, 23)],
            'day_premium_vars': {0: [p1]},
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE

    def test_effective_limits_scale_with_plan_length(self):
        """Configured ``max_per_horizon=2`` is per-5-day baseline; a 10-day
        horizon scales to 4 so longer plans aren't disproportionately tight."""
        rule = PremiumMenuRule({"name": "prem", "type": "premium",
                                "max_per_day": 1, "min_per_horizon": 1, "max_per_horizon": 2})
        # Baseline unchanged for ≤ 5 days
        assert rule.effective_limits(5) == (1, 2)
        assert rule.effective_limits(3) == (1, 2)
        # 10 days = 2× baseline
        assert rule.effective_limits(10) == (2, 4)
        # 7 days rounds up: ceil(7/5 * 1)=2 min, ceil(7/5 * 2)=3 max
        assert rule.effective_limits(7) == (2, 3)

    def test_horizon_max_scales_for_10_day_plans(self):
        """10-day plan with 4 forced premium days should be feasible after
        scaling (was infeasible under the static max_per_horizon=2)."""
        model = cp_model.CpModel()
        prems = [model.NewBoolVar(f'p{i}') for i in range(10)]
        # Force 4 days to be premium days
        for i in range(4):
            model.Add(prems[i] == 1)

        rule = PremiumMenuRule({"name": "prem", "type": "premium",
                                "max_per_day": 1, "min_per_horizon": 1, "max_per_horizon": 2})
        ctx = {
            'cfg': self._make_cfg(),
            'dates': [dt.date(2026, 3, 23) + dt.timedelta(days=i) for i in range(10)],
            'day_premium_vars': {i: [prems[i]] for i in range(10)},
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        # eff_max = ceil(10/5 * 2) = 4, so 4 premium days fits
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


# ---------------------------------------------------------------------------
# NonvegBiryaniWeeklyRule — biryani-per-week cap with horizon scaling
# ---------------------------------------------------------------------------

class TestNonvegBiryaniWeeklyConstraint:
    def _make_cell(self, di, biryani_flags):
        """Build a _FakeCell whose candidate rows carry is_nonveg_biryani."""
        rows = [{'item': f'item_{di}_{i}', 'is_nonveg_biryani': f}
                for i, f in enumerate(biryani_flags)]
        model = cp_model.CpModel()  # placeholder, replaced by caller
        xs = []  # filled by caller
        return rows, xs

    def test_static_cap_blocks_two_biryani_days(self):
        """Baseline cap=1 used as a pure per-horizon cap would force at
        most 1 biryani day even on a 10-day plan. This verifies the
        scaled cap relaxes that — required so 10-day plans with 2
        biryani Wednesdays can solve."""
        model = cp_model.CpModel()
        dates = [dt.date(2026, 4, 6) + dt.timedelta(days=i) for i in range(10)]
        cells = []
        # Each "day" gets one nonveg_main cell with one candidate marked
        # as biryani — forcing it to be picked exercises the per-day
        # boolean wiring inside NonvegBiryaniWeeklyRule.apply().
        for di in range(10):
            x = model.NewBoolVar(f'x_{di}')
            model.Add(x == 1)  # force biryani item every day
            cells.append(_FakeCell(
                d_idx=di, date=dates[di], slot_id='nonveg_main',
                base_slot='nonveg_main',
                rows=[{'item': f'biryani_{di}', 'is_nonveg_biryani': 1}],
                x_vars=[x],
            ))

        rule = NonvegBiryaniWeeklyRule({
            'name': 'bir', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 1,
        })
        ctx = {'cells': cells, 'dates': dates, 'link_any_fn': _link_any}
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        # Effective cap on a 10-day plan is ceil(1 * 10/5) = 2, so 10
        # forced biryani days remain infeasible — but the previous
        # static cap of 1 would have made even a 2-biryani-day plan
        # infeasible. The point of the scaling is that 2 days fit.
        assert status == cp_model.INFEASIBLE

    def test_two_biryani_days_feasible_on_10_day_plan(self):
        """The Stripe failure mode: 2 Biryani Wednesdays in a 10-day
        plan must be feasible. With the scaling fix, effective_max=2
        on a 10-day horizon, so two forced biryani days solve."""
        model = cp_model.CpModel()
        dates = [dt.date(2026, 4, 6) + dt.timedelta(days=i) for i in range(10)]
        cells = []
        # Force exactly 2 of the 10 days to contain a biryani item;
        # the other days have a single non-biryani candidate.
        biryani_day_indices = {2, 7}
        for di in range(10):
            x = model.NewBoolVar(f'x_{di}')
            model.Add(x == 1)
            is_bir = 1 if di in biryani_day_indices else 0
            cells.append(_FakeCell(
                d_idx=di, date=dates[di], slot_id='nonveg_main',
                base_slot='nonveg_main',
                rows=[{'item': f'item_{di}', 'is_nonveg_biryani': is_bir}],
                x_vars=[x],
            ))

        rule = NonvegBiryaniWeeklyRule({
            'name': 'bir', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 1,
        })
        ctx = {'cells': cells, 'dates': dates, 'link_any_fn': _link_any}
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        # 2 biryani days, scaled cap = 2 → feasible.
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    def test_two_biryani_days_infeasible_on_5_day_plan(self):
        """5-day baseline keeps the configured cap=1, so trying to
        force 2 biryani days on a 5-day plan still fails (preserves
        existing behavior for short plans)."""
        model = cp_model.CpModel()
        dates = [dt.date(2026, 4, 6) + dt.timedelta(days=i) for i in range(5)]
        cells = []
        for di in range(5):
            x = model.NewBoolVar(f'x_{di}')
            model.Add(x == 1)
            is_bir = 1 if di in (1, 3) else 0
            cells.append(_FakeCell(
                d_idx=di, date=dates[di], slot_id='nonveg_main',
                base_slot='nonveg_main',
                rows=[{'item': f'item_{di}', 'is_nonveg_biryani': is_bir}],
                x_vars=[x],
            ))

        rule = NonvegBiryaniWeeklyRule({
            'name': 'bir', 'type': 'nonveg_biryani_weekly',
            'max_per_week': 1,
        })
        ctx = {'cells': cells, 'dates': dates, 'link_any_fn': _link_any}
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        # eff_max(5) = 1, so 2 biryani days remains infeasible.
        assert status == cp_model.INFEASIBLE


# ---------------------------------------------------------------------------
# CurdSideMenuRule — biryani/pulao/curd logic
# ---------------------------------------------------------------------------

class TestCurdSideConstraint:
    def test_biryani_day_forces_raita(self):
        model = cp_model.CpModel()
        # Rice candidates
        rice_plain = model.NewBoolVar('rice_plain')
        # Curd candidates: curd and raita
        curd_v = model.NewBoolVar('curd_v')
        raita_v = model.NewBoolVar('raita_v')
        model.Add(rice_plain == 1)
        model.Add(curd_v + raita_v == 1)

        rice_cell = _FakeCell(0, dt.date(2026, 3, 25), 'rice', 'rice',
                              [pd.Series({'sub_category': 'biryani'})], [rice_plain])
        curd_cell = _FakeCell(0, dt.date(2026, 3, 25), 'curd_side', 'curd_side',
                              [pd.Series({'sub_category': 'curd', 'is_raita': 0}),
                               pd.Series({'sub_category': 'raita', 'is_raita': 1})],
                              [curd_v, raita_v])
        cells = [rice_cell, curd_cell]

        rule = CurdSideMenuRule({"name": "curd", "type": "curd_side"})
        ctx = {
            'cells': cells,
            'dates': [dt.date(2026, 3, 25)],
            'day_types': ['biryani'],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
        }
        rule.apply(model, {}, None, ctx)

        solver, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        assert solver.Value(raita_v) == 1
        assert solver.Value(curd_v) == 0

    def test_non_pulao_day_forces_curd(self):
        model = cp_model.CpModel()
        rice_jeera = model.NewBoolVar('rice_jeera')
        curd_v = model.NewBoolVar('curd_v')
        raita_v = model.NewBoolVar('raita_v')
        model.Add(rice_jeera == 1)
        model.Add(curd_v + raita_v == 1)

        rice_cell = _FakeCell(0, dt.date(2026, 3, 27), 'rice', 'rice',
                              [pd.Series({'sub_category': 'jeera_rice'})], [rice_jeera])
        curd_cell = _FakeCell(0, dt.date(2026, 3, 27), 'curd_side', 'curd_side',
                              [pd.Series({'sub_category': 'curd', 'is_raita': 0}),
                               pd.Series({'sub_category': 'raita', 'is_raita': 1})],
                              [curd_v, raita_v])

        rule = CurdSideMenuRule({"name": "curd", "type": "curd_side"})
        ctx = {
            'cells': [rice_cell, curd_cell],
            'dates': [dt.date(2026, 3, 27)],
            'day_types': ['north'],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
        }
        rule.apply(model, {}, None, ctx)

        solver, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        assert solver.Value(curd_v) == 1


# ---------------------------------------------------------------------------
# CouplingMenuRule — rice-bread / deep-fried
# ---------------------------------------------------------------------------

class TestCouplingConstraint:
    def test_ricebread_implies_liquid_rice(self):
        model = cp_model.CpModel()
        bread_rb = model.NewBoolVar('bread_rb')
        bread_naan = model.NewBoolVar('bread_naan')
        rice_liq = model.NewBoolVar('rice_liq')
        rice_jeera = model.NewBoolVar('rice_jeera')
        starter_df = model.NewBoolVar('starter_df')
        starter_reg = model.NewBoolVar('starter_reg')
        vd_df = model.NewBoolVar('vd_df')
        vd_reg = model.NewBoolVar('vd_reg')

        model.Add(bread_rb + bread_naan == 1)
        model.Add(rice_liq + rice_jeera == 1)
        model.Add(starter_df + starter_reg == 1)
        model.Add(vd_df + vd_reg == 1)

        # Force rice-bread
        model.Add(bread_rb == 1)
        # Force NO liquid rice — should be infeasible
        model.Add(rice_jeera == 1)

        bread_cell = _FakeCell(0, dt.date(2026, 3, 23), 'bread', 'bread',
                               [pd.Series({'is_rice_bread': 1}),
                                pd.Series({'is_rice_bread': 0})],
                               [bread_rb, bread_naan])
        rice_cell = _FakeCell(0, dt.date(2026, 3, 23), 'rice', 'rice',
                              [pd.Series({'is_liquid_rice': 1}),
                               pd.Series({'is_liquid_rice': 0})],
                              [rice_liq, rice_jeera])
        starter_cell = _FakeCell(0, dt.date(2026, 3, 23), 'starter', 'starter',
                                 [pd.Series({'is_deep_fried_starter': 1, 'item': 'pakoda', 'sub_category': ''}),
                                  pd.Series({'is_deep_fried_starter': 0, 'item': 'paneer', 'sub_category': ''})],
                                 [starter_df, starter_reg])
        vd_cell = _FakeCell(0, dt.date(2026, 3, 23), 'veg_dry', 'veg_dry',
                            [pd.Series({'is_deep_fried_veg_dry': 1}),
                             pd.Series({'is_deep_fried_veg_dry': 0})],
                            [vd_df, vd_reg])

        rule = CouplingMenuRule({"name": "c", "type": "coupling"})
        ctx = {
            'cells': [bread_cell, rice_cell, starter_cell, vd_cell],
            'dates': [dt.date(2026, 3, 23)],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE

    def test_no_ricebread_no_deepfried_starter_required(self):
        model = cp_model.CpModel()
        bread_naan = model.NewBoolVar('bread_naan')
        rice_jeera = model.NewBoolVar('rice_jeera')
        starter_reg = model.NewBoolVar('starter_reg')
        vd_reg = model.NewBoolVar('vd_reg')
        model.Add(bread_naan == 1)
        model.Add(rice_jeera == 1)
        model.Add(starter_reg == 1)
        model.Add(vd_reg == 1)

        bread_cell = _FakeCell(0, dt.date(2026, 3, 23), 'bread', 'bread',
                               [pd.Series({'is_rice_bread': 0})], [bread_naan])
        rice_cell = _FakeCell(0, dt.date(2026, 3, 23), 'rice', 'rice',
                              [pd.Series({'is_liquid_rice': 0})], [rice_jeera])
        starter_cell = _FakeCell(0, dt.date(2026, 3, 23), 'starter', 'starter',
                                 [pd.Series({'is_deep_fried_starter': 0, 'item': 'paneer', 'sub_category': ''})],
                                 [starter_reg])
        vd_cell = _FakeCell(0, dt.date(2026, 3, 23), 'veg_dry', 'veg_dry',
                            [pd.Series({'is_deep_fried_veg_dry': 0})], [vd_reg])

        rule = CouplingMenuRule({"name": "c", "type": "coupling"})
        ctx = {
            'cells': [bread_cell, rice_cell, starter_cell, vd_cell],
            'dates': [dt.date(2026, 3, 23)],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    def test_deepfried_starter_can_be_flexible_when_pair_unavailable(self):
        model = cp_model.CpModel()
        bread_naan = model.NewBoolVar('bread_naan')
        rice_jeera = model.NewBoolVar('rice_jeera')
        starter_df = model.NewBoolVar('starter_df')
        vd_reg = model.NewBoolVar('vd_reg')

        model.Add(bread_naan == 1)    # no rice-bread candidate exists
        model.Add(rice_jeera == 1)    # no liquid-rice candidate exists
        model.Add(starter_df == 1)    # force deep-fried starter
        model.Add(vd_reg == 1)

        bread_cell = _FakeCell(0, dt.date(2026, 3, 24), 'bread', 'bread',
                               [pd.Series({'is_rice_bread': 0})], [bread_naan])
        rice_cell = _FakeCell(0, dt.date(2026, 3, 24), 'rice', 'rice',
                              [pd.Series({'is_liquid_rice': 0})], [rice_jeera])
        starter_cell = _FakeCell(0, dt.date(2026, 3, 24), 'starter', 'starter',
                                 [pd.Series({'is_deep_fried_starter': 1, 'item': 'spring_roll', 'sub_category': ''})],
                                 [starter_df])
        vd_cell = _FakeCell(0, dt.date(2026, 3, 24), 'veg_dry', 'veg_dry',
                            [pd.Series({'is_deep_fried_veg_dry': 0})], [vd_reg])

        rule = CouplingMenuRule({"name": "c", "type": "coupling"})
        ctx = {
            'cells': [bread_cell, rice_cell, starter_cell, vd_cell],
            'dates': [dt.date(2026, 3, 24)],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


# ---------------------------------------------------------------------------
# WeekSignatureCooldownMenuRule — signature parsing + constraint
# ---------------------------------------------------------------------------

class TestWeekSignatureCooldown:
    def test_parse_signature(self):
        sig = '2026-03-23|rice=biryani|bread=naan|2026-03-24|rice=jeera'
        result = _parse_signature_to_expected_map(sig)
        assert result == {
            ('2026-03-23', 'rice'): 'biryani',
            ('2026-03-23', 'bread'): 'naan',
            ('2026-03-24', 'rice'): 'jeera',
        }

    def test_parse_empty_signature(self):
        assert _parse_signature_to_expected_map('') == {}

    def test_blocks_exact_repeat(self):
        model = cp_model.CpModel()
        r1 = model.NewBoolVar('r1')
        r2 = model.NewBoolVar('r2')
        model.Add(r1 + r2 == 1)
        b1 = model.NewBoolVar('b1')
        b2 = model.NewBoolVar('b2')
        model.Add(b1 + b2 == 1)

        # Force exact match of old signature
        model.Add(r1 == 1)  # biryani
        model.Add(b1 == 1)  # naan

        d = dt.date(2026, 3, 23)
        rice_cell = _FakeCell(0, d, 'rice', 'rice',
                              [pd.Series({'item': 'biryani'}),
                               pd.Series({'item': 'jeera'})], [r1, r2])
        bread_cell = _FakeCell(0, d, 'bread', 'bread',
                               [pd.Series({'item': 'naan'}),
                                pd.Series({'item': 'roti'})], [b1, b2])

        rule = WeekSignatureCooldownMenuRule({"name": "s", "type": "week_signature_cooldown"})
        sig = '2026-03-23|rice=biryani|bread=naan'
        ctx = {
            'cells': [rice_cell, bread_cell],
            'recent_sigs': {sig},
        }
        rule.apply(model, {}, None, ctx)

        _, status = _solve(model)
        assert status == cp_model.INFEASIBLE


# ---------------------------------------------------------------------------
# ThemeStarterPreferenceRule — objective bonus
# ---------------------------------------------------------------------------

class TestThemeStarterPreferenceObjective:
    def test_bonus_applied_to_theme_starters(self):
        model = cp_model.CpModel()
        theme_v = model.NewBoolVar('theme_starter')
        notheme_v = model.NewBoolVar('notheme_starter')
        model.Add(theme_v + notheme_v == 1)

        cell = _FakeCell(0, dt.date(2026, 3, 23), 'starter', 'starter',
                         [{}, {}], [theme_v, notheme_v],
                         theme_pref_flags=[True, False])

        cfg = type('Cfg', (), {'prefer_theme_starter': True})()
        rule = ThemeStarterPreferenceRule({"name": "p", "type": "theme_starter_preference",
                                           "bonus_weight": 100})
        ctx = {
            'cells': [cell],
            'dates': [dt.date(2026, 3, 23)],
            'find_cells_fn': _find_cells,
            'link_any_fn': _link_any,
            'cfg': cfg,
        }
        terms = rule.get_objective_terms(model, ctx)
        assert len(terms) == 1

        model.Maximize(sum(terms))
        solver, status = _solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        # Solver should pick theme starter for bonus
        assert solver.Value(theme_v) == 1

    def test_no_bonus_when_disabled(self):
        model = cp_model.CpModel()
        cfg = type('Cfg', (), {'prefer_theme_starter': False})()
        rule = ThemeStarterPreferenceRule({"name": "p", "type": "theme_starter_preference"})
        terms = rule.get_objective_terms(model, {
            'cells': [], 'dates': [], 'find_cells_fn': _find_cells,
            'link_any_fn': _link_any, 'cfg': cfg,
        })
        assert terms == []
