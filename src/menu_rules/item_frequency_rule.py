"""
Item frequency rule: cap how many days per week contain a matching item.

CP-SAT phase rule — adds cardinality constraints on a selector-matched
set of candidates.  Supports ``min_per_week`` and/or ``max_per_week``.

**Day-level semantics**: counts are "how many days have >= 1 matching item",
not total occurrences.  For slots with count=1/day this is equivalent.
"""

from typing import Dict, Any, List, Optional

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
from ..preprocessor.column_mapper import _norm_str


_SELECTOR_KEYS = frozenset({'flag', 'sub_category', 'item', 'key_ingredient'})


class ItemFrequencyRule(BaseMenuRule):
    """
    Config example::

        {
            "type": "item_frequency",
            "name": "tekion_liquid_rice_once",
            "base_slot": "rice",
            "selector": {"flag": "is_liquid_rice"},
            "min_per_week": 1,
            "max_per_week": 1
        }

    Selector must contain exactly one key from:
    ``flag``, ``sub_category``, ``item``, ``key_ingredient``.
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.ITEM_FREQUENCY

        sel = rule_config.get('selector', {})
        present = [k for k in _SELECTOR_KEYS if k in sel]
        if len(present) == 1:
            self.sel_kind: str = present[0]
            raw_val = sel[self.sel_kind]
            # For flag selectors the value is the column name (kept as-is).
            # For text selectors the value is normalised for matching.
            self.sel_value: str = raw_val if self.sel_kind == 'flag' else _norm_str(str(raw_val))
        else:
            self.sel_kind = ''
            self.sel_value = ''

        self.base_slot: Optional[str] = rule_config.get('base_slot')
        self.min_per_week: Optional[int] = (
            int(rule_config['min_per_week']) if 'min_per_week' in rule_config else None
        )
        self.max_per_week: Optional[int] = (
            int(rule_config['max_per_week']) if 'max_per_week' in rule_config else None
        )

    def validate_config(self) -> bool:
        return not self._collect_errors()

    def validation_errors(self) -> List[str]:
        return self._collect_errors()

    def _collect_errors(self) -> List[str]:
        errs: List[str] = []
        if not self.sel_kind:
            errs.append(
                "selector must contain exactly one of "
                + ", ".join(_SELECTOR_KEYS)
            )
        if self.min_per_week is None and self.max_per_week is None:
            errs.append("at least one of min_per_week / max_per_week is required")
        if self.min_per_week is not None and self.min_per_week < 0:
            errs.append(f"min_per_week must be >= 0 (got {self.min_per_week})")
        if self.max_per_week is not None and self.max_per_week < 0:
            errs.append(f"max_per_week must be >= 0 (got {self.max_per_week})")
        if (
            self.min_per_week is not None
            and self.max_per_week is not None
            and self.min_per_week > self.max_per_week
        ):
            errs.append(
                f"min_per_week ({self.min_per_week}) must be <= "
                f"max_per_week ({self.max_per_week})"
            )
        return errs

    # -- matching helpers --

    def _row_matches(self, row) -> bool:
        """Return True if a candidate row matches the selector."""
        if self.sel_kind == 'flag':
            return int(row.get(self.sel_value, 0)) == 1
        col_map = {
            'sub_category': 'sub_category',
            'item': 'item',
            'key_ingredient': 'key_ingredient',
        }
        col = col_map.get(self.sel_kind, '')
        return _norm_str(str(row.get(col, ''))) == self.sel_value

    # -- CP-SAT --

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cells = context.get('cells', [])
        dates = context.get('dates', [])
        link_any = context.get('link_any_fn')

        if not cells or not link_any:
            return

        day_vars: List = []

        for di in range(len(dates)):
            day_cells = [
                c for c in cells
                if c.d_idx == di
                and (self.base_slot is None or c.base_slot == self.base_slot)
            ]
            if not day_cells:
                continue

            matching_lits = [
                v
                for c in day_cells
                for v, r in zip(c.x_vars, c.cand_rows)
                if self._row_matches(r)
            ]

            if matching_lits:
                day_has = model.NewBoolVar(f'{self.name}_day_{di}')
                link_any(model, matching_lits, day_has)
                day_vars.append(day_has)

        if not day_vars:
            return

        if self.max_per_week is not None:
            model.Add(sum(day_vars) <= self.max_per_week)
        if self.min_per_week is not None:
            model.Add(sum(day_vars) >= self.min_per_week)

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Per-week, count how many dates have at least one item
        matching the selector across the configured slot(s).

        Emits ERROR when ``min_per_week`` cannot be met because there
        aren't enough qualifying days (the selector doesn't match any
        item in any active slot pool on enough dates).
        """
        diags: List[Diagnostic] = []
        if not self.sel_kind or self.min_per_week is None or self.min_per_week == 0:
            return diags
        scoped_slots = (
            [self.base_slot]
            if self.base_slot
            else (ctx.active_base_slots or list(BASE_SLOT_NAMES))
        )

        qualifying_days = 0
        for d in ctx.dates:
            day_has_match = False
            for base in scoped_slots:
                if (d, base) in ctx.skip_cells:
                    continue
                pool = ctx.pools.get(base)
                if pool is None or len(pool) == 0:
                    continue
                if self._pool_has_match(pool):
                    day_has_match = True
                    break
            if day_has_match:
                qualifying_days += 1

        if qualifying_days < self.min_per_week:
            sel_summary = f"{self.sel_kind}={self.sel_value!r}"
            scope = self.base_slot or "any active slot"
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.ERROR,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"item_frequency requires ≥{self.min_per_week} day"
                    f"{'s' if self.min_per_week != 1 else ''} matching "
                    f"{sel_summary} in {scope}, but only "
                    f"{qualifying_days} of {len(ctx.dates)} dates can "
                    f"satisfy it."
                ),
                suggestion=(
                    f"Add items matching {sel_summary} to the relevant "
                    f"slot pool, or lower min_per_week in the config."
                ),
                affected={
                    'selector_kind': self.sel_kind,
                    'selector_value': self.sel_value,
                    'base_slot': self.base_slot,
                    'min_per_week': self.min_per_week,
                    'qualifying_days': qualifying_days,
                    'total_dates': len(ctx.dates),
                },
            ))
        return diags

    def _pool_has_match(self, pool) -> bool:
        """Vectorised version of ``_row_matches`` over a pool DataFrame."""
        if self.sel_kind == 'flag':
            if self.sel_value not in pool.columns:
                return False
            return bool((pool[self.sel_value].fillna(0).astype(int) == 1).any())
        col_map = {
            'sub_category': 'sub_category',
            'item': 'item',
            'key_ingredient': 'key_ingredient',
        }
        col = col_map.get(self.sel_kind, '')
        if not col or col not in pool.columns:
            return False
        return bool((pool[col].astype(str).map(_norm_str) == self.sel_value).any())
