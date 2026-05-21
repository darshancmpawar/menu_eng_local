"""
Color-based menu rules.

* :class:`ColorVarietyMenuRule` — enforce a minimum number of distinct
  item colors per day.
* :class:`ColorPairingMenuRule` — forbid two given slots from picking
  items of the same color on the same day.
* :class:`WelcomeDrinkColorMenuRule` — forbid consecutive days from
  using the same welcome-drink color.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ortools.sat.python import cp_model

from ..preprocessor.column_mapper import _norm_color
from .base_menu_rule import BaseMenuRule, MenuRuleType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ColorVarietyMenuRule
# ---------------------------------------------------------------------------


class ColorVarietyMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "color_variety",
        "name": "daily_color_variety",
        "min_distinct_colors": {"lunch": 3}
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.COLOR_VARIETY
        self.min_distinct_colors = rule_config.get('min_distinct_colors', None)

    def validate_config(self) -> bool:
        if not isinstance(self.min_distinct_colors, dict) or not self.min_distinct_colors:
            return False
        for val in self.min_distinct_colors.values():
            try:
                if int(val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        dates = context.get('dates', [])
        known_colors = context.get('known_colors', [])
        day_color_vars = context.get('day_color_vars', {})
        link_any = context.get('link_any_fn')

        if not dates or not known_colors or not link_any:
            return

        # Resolve min distinct colors for the current meal type
        meal_type = context.get('meal_type', '')
        min_colors = 0
        if isinstance(self.min_distinct_colors, dict) and meal_type:
            try:
                min_colors = int(self.min_distinct_colors.get(meal_type, 0))
            except (TypeError, ValueError):
                pass
        if min_colors <= 0:
            return

        for di in range(len(dates)):
            y_vars = []
            for col in known_colors:
                lits = day_color_vars.get((di, col), [])
                if not lits:
                    continue
                y = model.NewBoolVar(f'cv_y_{di}_{col}')
                link_any(model, lits, y)
                y_vars.append(y)
            if y_vars:
                model.Add(sum(y_vars) >= min_colors)


# ---------------------------------------------------------------------------
# ColorPairingMenuRule
# ---------------------------------------------------------------------------


class ColorPairingMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "color_pairing",
        "name": "starter_main_color_mismatch",
        "course_type_a": "starter",
        "course_type_b": "veg_gravy"
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.COLOR_PAIRING
        self.course_type_a = rule_config.get('course_type_a', '')
        self.course_type_b = rule_config.get('course_type_b', '')

    def validate_config(self) -> bool:
        if not self.course_type_a or not self.course_type_b:
            return False
        if self.course_type_a == self.course_type_b:
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

        color_col = cfg.color_col

        for di in range(len(dates)):
            cells_a = find_cells(cells, di, self.course_type_a)
            cells_b = find_cells(cells, di, self.course_type_b)
            if not cells_a or not cells_b:
                continue

            # Group variables by color for each course type
            colors_a: Dict[str, list] = {}
            for c in cells_a:
                for var, row in zip(c.x_vars, c.cand_rows):
                    col = _norm_color(row.get(color_col, 'unknown'))
                    if col != 'unknown':
                        colors_a.setdefault(col, []).append(var)

            colors_b: Dict[str, list] = {}
            for c in cells_b:
                for var, row in zip(c.x_vars, c.cand_rows):
                    col = _norm_color(row.get(color_col, 'unknown'))
                    if col != 'unknown':
                        colors_b.setdefault(col, []).append(var)

            # For each shared color: at most one side can select it
            for color in set(colors_a) & set(colors_b):
                model.Add(sum(colors_a[color]) + sum(colors_b[color]) <= 1)


# ---------------------------------------------------------------------------
# WelcomeDrinkColorMenuRule
# ---------------------------------------------------------------------------


class WelcomeDrinkColorMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "welcome_drink_color",
        "name": "welcome_drink_no_repeat_color"
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.WELCOME_DRINK_COLOR

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        dates = context.get('dates', [])
        known_welcome_colors = context.get('known_welcome_colors', [])
        day_welcome_color_vars = context.get('day_welcome_color_vars', {})

        for di in range(len(dates) - 1):
            for col in known_welcome_colors:
                a = day_welcome_color_vars.get((di, col), [])
                b = day_welcome_color_vars.get((di + 1, col), [])
                if a and b:
                    model.Add(sum(a) + sum(b) <= 1)
