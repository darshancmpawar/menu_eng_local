"""
Non-veg menu rules.

* :class:`NonvegBiryaniWeeklyRule` — CP-SAT cap: at most N nonveg
  biryani days across the week. ``max_per_week`` is the per-5-day rate;
  for plans longer than 5 days the cap scales proportionally (a 10-day
  plan permits 2× the configured baseline) so the rule does not collide
  with the theme schedule's repeated Biryani Wednesdays.
* :class:`NonvegDryPreferenceRule` — pre-filter: for nonveg_main slot
  2+, prefer dry items; fall back to gravy; on biryani/chinese days
  exclude the theme items (those go in slot 1) first.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List

import pandas as pd
from ortools.sat.python import cp_model

from ..preprocessor.column_mapper import _to_bool01
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)


# Baseline week length the per-week cap in the rule config is tuned for.
# Mirrors PremiumMenuRule._BASELINE_DAYS — keeps both horizon-scoped
# limits scaling on the same yardstick.
_BASELINE_DAYS = 5


# ---------------------------------------------------------------------------
# NonvegBiryaniWeeklyRule
# ---------------------------------------------------------------------------


class NonvegBiryaniWeeklyRule(BaseMenuRule):
    """
    Config:
    {
        "type": "nonveg_biryani_weekly",
        "name": "nonveg_biryani_once_per_week",
        "max_per_week": 1
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.NONVEG_BIRYANI_WEEKLY
        self.max_per_week = int(rule_config.get('max_per_week', 1))

    def validate_config(self) -> bool:
        return self.max_per_week >= 0

    def effective_max(self, num_days: int) -> int:
        """Cap scaled to ``num_days``.

        The configured ``max_per_week`` describes the baseline 5-day
        week. For longer horizons the theme calendar repeats Wednesday
        Biryani every week, and ``ThemeSlotFilterRule`` forces slot 1
        on those days to be a biryani item — so the cap must grow with
        the plan or the model becomes infeasible.
        """
        if num_days <= _BASELINE_DAYS:
            return self.max_per_week
        scale = num_days / _BASELINE_DAYS
        return max(self.max_per_week, math.ceil(self.max_per_week * scale))

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cells = context.get('cells', [])
        dates = context.get('dates', [])
        link_any = context.get('link_any_fn')

        if not cells or not link_any:
            return

        biryani_day_vars = []

        for di in range(len(dates)):
            nv_cells = [c for c in cells if c.d_idx == di and c.base_slot == 'nonveg_main']
            if not nv_cells:
                continue

            biryani_lits = [
                v for c in nv_cells
                for v, r in zip(c.x_vars, c.cand_rows)
                if int(r.get('is_nonveg_biryani', 0)) == 1
            ]

            if biryani_lits:
                day_has_biryani = model.NewBoolVar(f'nonveg_biryani_day_{di}')
                link_any(model, biryani_lits, day_has_biryani)
                biryani_day_vars.append(day_has_biryani)

        if biryani_day_vars:
            model.Add(sum(biryani_day_vars) <= self.effective_max(len(dates)))

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Constraint is ``sum(nonveg_biryani_day_vars) <= max_per_week``.

        Emits INFO when the constraint is a no-op (no
        ``is_nonveg_biryani=1`` items anywhere) so users can see why
        their cap isn't doing anything, and ERROR when the count of
        biryani-themed days on the horizon exceeds the effective cap —
        which the theme filter would force-fail since slot 1 on a
        Biryani day MUST carry a biryani item.
        """
        diags: List[Diagnostic] = []
        pool = ctx.pools.get('nonveg_main')
        if pool is None:
            return diags
        eff_max = self.effective_max(len(ctx.dates))
        if 'is_nonveg_biryani' not in pool.columns:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.INFO,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"Nonveg biryani weekly cap is set "
                    f"(max_per_week={self.max_per_week}) but the "
                    f"'is_nonveg_biryani' column is missing from the "
                    f"nonveg_main pool. The constraint is a no-op."
                ),
                suggestion=(
                    "Populate the is_nonveg_biryani flag column in the "
                    "ontology Excel, or remove this rule."
                ),
                affected={'max_per_week': self.max_per_week},
            ))
            return diags
        biryani_count = int(
            pool['is_nonveg_biryani'].fillna(0).astype(int).eq(1).sum()
        )
        if biryani_count == 0:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.INFO,
                phase=DiagnosticPhase.APPLY,
                message=(
                    "Nonveg biryani weekly cap is set but the "
                    "nonveg_main pool has 0 items with "
                    "is_nonveg_biryani=1. The constraint is a no-op."
                ),
                suggestion="No action needed unless you expected biryani items.",
                affected={
                    'max_per_week': self.max_per_week,
                    'biryani_count': 0,
                },
            ))
            return diags

        # Biryani-themed days the schedule will produce on this horizon.
        # If more days have biryani as their theme than the effective
        # cap permits, slot 1 on the extra days has nothing legal to
        # pick — model is infeasible. Catch it pre-flight.
        biryani_days = sum(
            1 for d in ctx.dates
            if ctx.day_types.get(d) == 'biryani'
            and (d, 'nonveg_main') not in ctx.skip_cells
        )
        if biryani_days > eff_max:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.ERROR,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"Nonveg biryani cap allows {eff_max} biryani "
                    f"day{'s' if eff_max != 1 else ''} on this "
                    f"{len(ctx.dates)}-day plan, but the theme schedule "
                    f"has {biryani_days} biryani-theme day"
                    f"{'s' if biryani_days != 1 else ''}. Each one "
                    f"requires a biryani item in nonveg_main slot 1 "
                    f"(theme filter), so the cap and the theme rule "
                    f"collide — plan is infeasible."
                ),
                suggestion=(
                    f"Raise max_per_week so the scaled cap covers all "
                    f"biryani days (current effective cap on "
                    f"{len(ctx.dates)} days: {eff_max}), or shorten "
                    f"the plan so only {eff_max} biryani day"
                    f"{'s' if eff_max != 1 else ''} land in the horizon."
                ),
                affected={
                    'max_per_week': self.max_per_week,
                    'effective_max': eff_max,
                    'biryani_days_on_horizon': biryani_days,
                    'num_days': len(ctx.dates),
                },
            ))
        return diags


# ---------------------------------------------------------------------------
# NonvegDryPreferenceRule
# ---------------------------------------------------------------------------


class NonvegDryPreferenceRule(BaseMenuRule):
    """
    Config:
    {
        "type": "nonveg_dry_preference",
        "name": "prefer_nonveg_dry_slot2"
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.NONVEG_DRY_PREFERENCE

    def pre_filter_pool(self, pool: pd.DataFrame, date: dt.date,
                        base_slot: str, day_type: str,
                        filter_context: Dict[str, Any]) -> pd.DataFrame:
        # Only applies to nonveg_main slots numbered 2+ (slot_num >= 2)
        slot_num = filter_context.get('slot_num')
        if base_slot != 'nonveg_main' or not slot_num or slot_num < 2:
            return pool
        if len(pool) == 0:
            return pool

        cfg = filter_context.get('cfg')
        banned = filter_context.get('banned_by_date', {}).get(date, set())
        pools = filter_context.get('pools', {})

        # On biryani/chinese days: use full nonveg pool minus biryani/chinese items
        if day_type in ('biryani', 'chinese') and 'nonveg_main' in pools:
            alt_pool = pools['nonveg_main'].copy()
            if cfg:
                if cfg.f_chinese_nonveg and cfg.f_chinese_nonveg in alt_pool.columns:
                    alt_pool = alt_pool[alt_pool[cfg.f_chinese_nonveg].map(_to_bool01) == 0]
                if cfg.f_nonveg_biryani and cfg.f_nonveg_biryani in alt_pool.columns:
                    alt_pool = alt_pool[alt_pool[cfg.f_nonveg_biryani].map(_to_bool01) == 0]
            if banned:
                alt_pool = alt_pool[~alt_pool['item'].isin(banned)]
            if len(alt_pool) > 0:
                pool = alt_pool

        # Prefer dry items — reads the column populated by ColumnMapper.apply()
        # rather than re-running the heuristic per row.
        if 'is_nonveg_dry' in pool.columns:
            dry_pool = pool[pool['is_nonveg_dry'].map(_to_bool01) == 1]
            if len(dry_pool) > 0:
                return dry_pool

        # Fallback: prefer gravy items
        if 'is_nonveg_gravy' in pool.columns:
            gravy_pool = pool[pool['is_nonveg_gravy'].map(_to_bool01) == 1]
            if len(gravy_pool) > 0:
                return gravy_pool

        return pool

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        pass  # All filtering happens in pre_filter_pool
