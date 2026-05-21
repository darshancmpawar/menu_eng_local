"""
UI formatting utilities for menu plan display.
"""

import html
import re
from typing import Any, Dict, Optional, Tuple

from src.constants import DISPLAY_SLOT_NAME, BASE_SLOT_NAMES


# Day-of-week theme labels (Monday=0)
THEME_LABELS = {
    0: "Mix of South + North",
    1: "Chinese / Indo-Chinese",
    2: "Biryani Day",
    3: "South Indian",
    4: "North Indian",
    5: "Weekend Special",
    6: "Weekend Special",
}

# Map color initial -> (full name, CSS bg color, CSS text color)
_COLOR_MAP: Dict[str, Tuple[str, str, str]] = {
    'R': ('Red',    '#3b1114', '#fca5a5'),
    'G': ('Green',  '#0f2a1d', '#86efac'),
    'B': ('Brown',  '#2a1a08', '#d4a56a'),
    'Y': ('Yellow', '#2a2308', '#fde68a'),
    'W': ('White',  '#1f1f23', '#d4d4d8'),
    'O': ('Orange', '#2a1508', '#fdba74'),
    'K': ('Black',  '#18181b', '#a1a1aa'),
}


# Theme badge colors keyed by theme name: (background, foreground, icon)
THEME_TAG_COLORS = {
    'mix':     ('#0f2a1d', '#86efac'),
    'chinese': ('#2a1508', '#fdba74'),
    'biryani': ('#3b1114', '#fca5a5'),
    'south':   ('#0f1a2e', '#93c5fd'),
    'north':   ('#1e0a3a', '#c4b5fd'),
}

THEME_ICONS = {
    'mix':     '&#9670;',   # diamond
    'chinese': '&#9672;',   # circle
    'biryani': '&#9733;',   # star
    'south':   '&#9650;',   # triangle up
    'north':   '&#9632;',   # square
}


def theme_label(weekday: int) -> str:
    return THEME_LABELS.get(weekday, "")


def display_label_for_slot_id(slot_id: str) -> str:
    return DISPLAY_SLOT_NAME.get(slot_id, slot_id.replace("_", " ").title())


def prettify_slot_name(name: str) -> str:
    if not name:
        return ""
    return name.replace("_", " ").strip().title()


def _prettify_item_name(name: str) -> str:
    if not name:
        return ""
    return name.replace("_", " ").strip().title()


def format_item_for_ui(item_str: str) -> str:
    """Format item string for plain-text display (no HTML)."""
    if not item_str:
        return ""
    cleaned = re.sub(r'\s*\([A-Z]\)\s*$', '', item_str)
    return _prettify_item_name(cleaned)


def format_item_html(item_str: str) -> str:
    """Format item string as HTML with colored pill for the color tag.

    Input:  'veg_fried_rice(Y)'
    Output: 'Veg Fried Rice <span class="color-pill" ...>(Yellow)</span>'
    """
    if not item_str:
        return '<span class="cell-empty">&mdash;</span>'
    m = re.search(r'\(([A-Z])\)\s*$', item_str)
    cleaned = re.sub(r'\s*\([A-Z]\)\s*$', '', item_str)
    # Item names originate from the ontology / Supabase, but those are
    # admin-editable, so escape before embedding into st.markdown output
    # that runs with unsafe_allow_html=True.
    name = html.escape(_prettify_item_name(cleaned))

    if m:
        initial = m.group(1)
        color_name, bg, fg = _COLOR_MAP.get(initial, (initial, '#1f1f23', '#a1a1aa'))
        return (
            f'<span class="item-name">{name}</span>'
            f'<span class="color-pill" style="background:{bg};color:{fg};">'
            f'{html.escape(color_name)}</span>'
        )
    return f'<span class="item-name">{name}</span>'


def pretty_text(item_str: str) -> str:
    if not item_str:
        return ""
    cleaned = re.sub(r'\s*\([A-Z]\)\s*$', '', item_str)
    return cleaned.strip().title()


def color_suffix(item_str: str) -> Optional[str]:
    m = re.search(r'\(([A-Z])\)\s*$', item_str)
    return m.group(1) if m else None


def flatten_api_solution(
    raw_solution: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """Turn the API's ``/plan`` response body into UI-friendly structures.

    The API returns ``{date_iso: {theme, day_type, items: {slot: {item, ...}}}}``.
    For rendering, the UI only needs ``{date_iso: {slot: item_str}}`` plus
    ``{date_iso: day_type}`` for the column headers.
    """
    flat: Dict[str, Dict[str, str]] = {}
    day_types: Dict[str, str] = {}
    for date_key, day_data in raw_solution.items():
        if isinstance(day_data, dict) and 'items' in day_data:
            day_types[date_key] = day_data.get('day_type', '')
            source = day_data['items']
        else:
            source = day_data or {}
        slots: Dict[str, str] = {}
        for slot_id, val in source.items():
            if isinstance(val, dict):
                slots[slot_id] = val.get('item', val.get('item_base', ''))
            else:
                slots[slot_id] = str(val)
        flat[date_key] = slots
    return flat, day_types


def slot_sort_key(slot_id: str) -> int:
    """Return sort index for display ordering."""
    base = slot_id.split("__")[0] if "__" in slot_id else slot_id
    try:
        return BASE_SLOT_NAMES.index(base)
    except ValueError:
        return 999
