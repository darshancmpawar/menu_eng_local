"""
Cuisine menu rule: enforce cuisine-specific items on specific days.

Uses cell-based context from the solver. For broad cuisine filtering
(south/north/chinese/biryani), prefer ThemeSlotFilterRule instead.
This rule is for fine-grained per-cuisine-per-day constraints.
"""

import logging
from typing import Dict, Any, List

from ortools.sat.python import cp_model
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)
from src.constants import BASE_SLOT_NAMES, EXEMPT_FROM_CUISINE
from ..preprocessor.column_mapper import _norm_str

logger = logging.getLogger(__name__)


class CuisineMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "cuisine",
        "name": "italian_specific_days",
        "cuisine_family": "italian",
        "days_of_week": ["wednesday", "thursday"]
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.CUISINE
        self.cuisine_family = _norm_str(
            rule_config.get('cuisine_family', rule_config.get('cuisine_type', ''))
        )
        self.days_of_week = [d.lower() for d in rule_config.get('days_of_week', [])]

    def validate_config(self) -> bool:
        if not self.cuisine_family:
            return False
        return True

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cells = context.get('cells', [])
        dates = context.get('dates', [])
        find_cells = context.get('find_cells_fn')
        cfg = context.get('cfg')

        if not cells or not find_cells or not cfg:
            return

        cuisine_col = cfg.cuisine_col

        for di, d in enumerate(dates):
            day_name = d.strftime('%A').lower()
            if self.days_of_week and day_name not in self.days_of_week:
                continue

            # Collect all non-exempt base slots for this day
            seen_bases = set()
            for cell in cells:
                if cell.d_idx != di or cell.base_slot in EXEMPT_FROM_CUISINE:
                    continue
                if cell.base_slot in seen_bases:
                    continue
                seen_bases.add(cell.base_slot)

                cuisine_lits = [
                    v for v, row in zip(cell.x_vars, cell.cand_rows)
                    if _norm_str(row.get(cuisine_col, '')) == self.cuisine_family
                ]
                if cuisine_lits:
                    model.Add(sum(cuisine_lits) >= 1)

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """For every requested date whose weekday matches
        ``days_of_week``, scan non-exempt base-slot pools and count
        items with the configured ``cuisine_family``.

        Emits INFO when the rule applies but no items match (the
        ``apply()`` body silently drops the constraint in that case,
        so users wouldn't otherwise know it's a no-op).
        """
        diags: List[Diagnostic] = []
        if not self.cuisine_family:
            return diags
        cuisine_col = ctx.cfg.cuisine_col if ctx.cfg else 'cuisine_family'
        base_slots = ctx.active_base_slots or list(BASE_SLOT_NAMES)

        for d in ctx.dates:
            day_name = d.strftime('%A').lower()
            if self.days_of_week and day_name not in self.days_of_week:
                continue
            day_label = d.strftime('%A %d %b')

            total = 0
            for base in base_slots:
                if base in EXEMPT_FROM_CUISINE:
                    continue
                if (d, base) in ctx.skip_cells:
                    continue
                pool = ctx.pools.get(base)
                if pool is None or len(pool) == 0:
                    continue
                if cuisine_col not in pool.columns:
                    continue
                total += int((pool[cuisine_col].map(_norm_str) == self.cuisine_family).sum())

            if total == 0:
                diags.append(Diagnostic(
                    rule=self.name,
                    rule_type=self.rule_type.value,
                    severity=DiagnosticSeverity.INFO,
                    phase=DiagnosticPhase.APPLY,
                    message=(
                        f"Cuisine rule '{self.name}' targets "
                        f"{self.cuisine_family!r} on {day_label} but no "
                        f"items with that cuisine are in any non-exempt "
                        f"slot pool. The constraint will silently drop."
                    ),
                    suggestion=(
                        f"Add at least one {self.cuisine_family} item to "
                        f"a non-exempt slot, or remove this cuisine rule "
                        f"from the config."
                    ),
                    affected={
                        'date': d.isoformat(),
                        'cuisine_family': self.cuisine_family,
                        'count': 0,
                    },
                ))
        return diags
