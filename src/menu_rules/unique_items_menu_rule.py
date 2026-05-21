"""
Unique items menu rule: each item at most once per planning session.

Uses item_to_vars from context (built by solver) to enforce uniqueness.
"""

import logging
from typing import Any, Dict, List

from ortools.sat.python import cp_model
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)
from src.constants import BASE_SLOT_NAMES, REPEATABLE_ITEM_BASES

logger = logging.getLogger(__name__)


class UniqueItemsMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "unique_items",
        "name": "unique_items_session",
        "scope": "session"
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.UNIQUE_ITEMS
        self.scope = rule_config.get('scope', 'session').lower()

    def validate_config(self) -> bool:
        return self.scope in ('session',)

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        item_to_vars = context.get('item_to_vars', {})
        if not item_to_vars:
            return
        repeatable = set(REPEATABLE_ITEM_BASES)
        for item_base, vars_ in item_to_vars.items():
            if item_base not in repeatable:
                model.Add(sum(vars_) <= 1)

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Horizon-wide pool capacity check.

        Across the whole plan, each (date, slot, slot_num) cell needs a
        DISTINCT item (uniqueness is session-scoped). For a base_slot
        with ``slot_count = k`` and ``d`` active days, that's ``k * d``
        distinct items needed. If the pool has fewer items than that,
        the plan is infeasible no matter how the solver picks — surface
        it here before the solver runs.

        This catches the multi-slot ×  long-horizon case where a client
        (e.g. Stripe with ``nonveg_main: 2``) needs 20 unique items for
        a 10-day plan but only has 12-15 in the pool.
        """
        diags: List[Diagnostic] = []
        slot_counts: Dict[str, int] = {}
        if ctx.client_cfg is not None:
            slot_counts = dict(getattr(ctx.client_cfg, 'slot_counts', {}) or {})
        repeatable = set(REPEATABLE_ITEM_BASES)
        base_slots = ctx.active_base_slots or list(BASE_SLOT_NAMES)

        for base in base_slots:
            if base in repeatable:
                continue
            pool = ctx.pools.get(base)
            if pool is None or len(pool) == 0 or 'item' not in pool.columns:
                continue

            count_per_day = slot_counts.get(base, 1)
            active_days = sum(
                1 for d in ctx.dates if (d, base) not in ctx.skip_cells
            )
            cells_needed = count_per_day * active_days
            pool_size = int(pool['item'].nunique())
            if cells_needed <= pool_size:
                continue  # plenty of items for this slot

            slot_label = base.replace('_', ' ')
            shortfall = cells_needed - pool_size
            diags.append(Diagnostic(
                rule=self.name,
                rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.ERROR,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"{slot_label.capitalize()} pool has {pool_size} distinct "
                    f"item{'s' if pool_size != 1 else ''} but the plan needs "
                    f"{cells_needed} (= {count_per_day} per day × "
                    f"{active_days} active days). Short by {shortfall}. "
                    f"Unique-items rule makes this infeasible."
                ),
                suggestion=(
                    f"Add at least {shortfall} more {slot_label} item"
                    f"{'s' if shortfall != 1 else ''} to this client's menu, "
                    f"or generate a shorter plan."
                ),
                affected={
                    'slot': base,
                    'slot_count_per_day': count_per_day,
                    'active_days': active_days,
                    'cells_needed': cells_needed,
                    'pool_size': pool_size,
                    'shortfall': shortfall,
                },
            ))
        return diags
