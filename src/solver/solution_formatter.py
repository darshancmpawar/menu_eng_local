"""
Solution formatter for presenting menu plans.

Handles slot-based output format with color suffixes and constant items.
"""

import datetime as dt
from typing import Dict, Any, List, Optional

from ._helpers import (
    weekday_type_for_config as _weekday_type_cfg,
    theme_label as _theme_label,
    strip_color_suffix as _strip_color_suffix,
)
from src.constants import DISPLAY_SLOT_NAME
from ..preprocessor.pool_builder import _base_slot, _slot_num


def _display_slot(slot_id: str) -> str:
    base = _base_slot(slot_id)
    num = _slot_num(slot_id)
    base_disp = DISPLAY_SLOT_NAME.get(base, base.replace('_', ' ').title())
    return base_disp if num is None else f'{base_disp} {num}'


class SolutionFormatter:
    """
    Formats cell-based menu planning solutions for output.

    Expects week_plan = {date: {slot_id: item_string_with_color}}
    """

    def __init__(self, week_plan: Dict[dt.date, Dict[str, str]], dates: List[dt.date],
                 theme_map: Optional[Dict[str, str]] = None):
        self.week_plan = week_plan
        self.dates = dates
        self._theme_map = theme_map

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result = {}
        for d in self.dates:
            day_key = d.isoformat()
            day_type = _weekday_type_cfg(d, self._theme_map)
            result[day_key] = {
                'theme': _theme_label(day_type),
                'day_type': day_type,
                'items': {},
            }
            for slot_id, item_str in self.week_plan.get(d, {}).items():
                result[day_key]['items'][slot_id] = {
                    'display_name': _display_slot(slot_id),
                    'item': item_str,
                    'item_base': _strip_color_suffix(item_str),
                }
        return result
