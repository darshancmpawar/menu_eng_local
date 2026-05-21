"""
Ingredient ban rule: drop items whose key_ingredient matches a banned list.

Pre-filter phase rule — case-insensitive exact match on the normalised
``key_ingredient`` column.  Banning ``"corn"`` does NOT affect ``"babycorn"``
because matches are on the full value, not a substring.
"""

import datetime as dt
import pandas as pd
from typing import Dict, Any, List, Set

from ortools.sat.python import cp_model
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)
from src.constants import BASE_SLOT_NAMES


class IngredientBanRule(BaseMenuRule):
    """
    Config example::

        {
            "type": "ingredient_ban",
            "name": "tekion_no_mushroom",
            "ingredients": ["mushroom"]
        }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.INGREDIENT_BAN
        raw = rule_config.get('ingredients', [])
        self.banned: Set[str] = {i.strip().lower() for i in raw if isinstance(i, str)}

    def validate_config(self) -> bool:
        return (
            isinstance(self.config.get('ingredients'), list)
            and len(self.banned) > 0
        )

    def pre_filter_pool(self, pool: pd.DataFrame, date: dt.date,
                        base_slot: str, day_type: str,
                        filter_context: Dict[str, Any]) -> pd.DataFrame:
        if len(pool) == 0 or 'key_ingredient' not in pool.columns or not self.banned:
            return pool
        ki = pool['key_ingredient'].astype(str).str.strip().str.lower()
        return pool[~ki.isin(self.banned)]

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        pass  # All filtering happens in pre_filter_pool

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Per-slot, count how many items the ingredient ban removes.

        Emits:
          - ERROR when the ban would empty an active slot's pool.
          - INFO  when the ban removes items but the pool is still
            healthy (so users can see their ban is taking effect).
        """
        diags: List[Diagnostic] = []
        if not self.banned:
            return diags
        base_slots = ctx.active_base_slots or list(BASE_SLOT_NAMES)
        banned_list = sorted(self.banned)

        for base in base_slots:
            pool = ctx.pools.get(base)
            if pool is None or len(pool) == 0:
                continue
            if 'key_ingredient' not in pool.columns:
                continue
            ki = pool['key_ingredient'].astype(str).str.strip().str.lower()
            mask = ki.isin(self.banned)
            ban_count = int(mask.sum())
            if ban_count == 0:
                continue
            remaining = len(pool) - ban_count
            slot_label = base.replace('_', ' ')
            if remaining == 0:
                diags.append(Diagnostic(
                    rule=self.name, rule_type=self.rule_type.value,
                    severity=DiagnosticSeverity.ERROR,
                    phase=DiagnosticPhase.PRE_FILTER,
                    message=(
                        f"Ingredient ban ({', '.join(banned_list)}) "
                        f"removes all {ban_count} items from the "
                        f"{slot_label} pool. Pool is empty."
                    ),
                    suggestion=(
                        f"Add a non-banned item to the {slot_label} "
                        f"slot, or shrink the banned ingredients list."
                    ),
                    affected={
                        'slot': base,
                        'banned_ingredients': banned_list,
                        'banned_count': ban_count,
                        'pool_size_before': len(pool),
                        'pool_size_after': remaining,
                    },
                ))
            else:
                diags.append(Diagnostic(
                    rule=self.name, rule_type=self.rule_type.value,
                    severity=DiagnosticSeverity.INFO,
                    phase=DiagnosticPhase.PRE_FILTER,
                    message=(
                        f"Ingredient ban removes {ban_count} of "
                        f"{len(pool)} items from the {slot_label} pool "
                        f"({remaining} remain)."
                    ),
                    suggestion="No action needed — ban is working.",
                    affected={
                        'slot': base,
                        'banned_ingredients': banned_list,
                        'banned_count': ban_count,
                        'pool_size_before': len(pool),
                        'pool_size_after': remaining,
                    },
                ))
        return diags
