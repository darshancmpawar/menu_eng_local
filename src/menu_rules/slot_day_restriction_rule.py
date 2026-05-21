"""
Slot-day restriction rule: skip a slot entirely on certain weekdays.

This rule does not participate in the pre-filter or CP-SAT phases.
Instead, it exposes ``compute_skip_cells()`` which the API layer calls
before constructing the solver.  The returned ``(date, base_slot)`` pairs
are forwarded to ``MenuSolver(skip_cells=…)`` which prevents cell
creation for those combinations.

All expansions of the base slot are skipped together (e.g. if a client
has ``nonveg_main`` count=2, both ``nonveg_main__1`` and ``nonveg_main__2``
are removed on restricted days).
"""

import datetime as dt
from typing import Dict, Any, List, Set, Tuple

from ortools.sat.python import cp_model
from .base_menu_rule import BaseMenuRule, MenuRuleType


_WEEKDAY_TOKENS: Dict[str, int] = {
    'mon': 0, 'monday': 0,
    'tue': 1, 'tuesday': 1,
    'wed': 2, 'wednesday': 2,
    'thu': 3, 'thursday': 3,
    'fri': 4, 'friday': 4,
    'sat': 5, 'saturday': 5,
    'sun': 6, 'sunday': 6,
}


class SlotDayRestrictionRule(BaseMenuRule):
    """
    Config example::

        {
            "type": "slot_day_restriction",
            "name": "tekion_nonveg_mwf",
            "base_slot": "nonveg_main",
            "allowed_weekdays": ["mon", "wed", "fri"]
        }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.SLOT_DAY_RESTRICTION
        self.base_slot: str = rule_config.get('base_slot', '')
        raw_days = rule_config.get('allowed_weekdays', [])
        self.allowed_weekdays: Set[int] = set()
        for tok in raw_days:
            if isinstance(tok, str):
                idx = _WEEKDAY_TOKENS.get(tok.strip().lower())
                if idx is not None:
                    self.allowed_weekdays.add(idx)

    def validate_config(self) -> bool:
        return bool(self.base_slot) and len(self.allowed_weekdays) > 0

    def compute_skip_cells(
        self, dates: List[dt.date],
    ) -> Set[Tuple[dt.date, str]]:
        """Return (date, base_slot) pairs that should be skipped."""
        return {
            (d, self.base_slot)
            for d in dates
            if d.weekday() not in self.allowed_weekdays
        }

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        pass  # Cell skipping is handled by the solver, not CP-SAT constraints
