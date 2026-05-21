"""
Pool builder: maps normalized DataFrame rows into per-slot item pools.

Each pool is a DataFrame containing only the items eligible for that slot.
Handles sambar/rasam splitting, welcome_drink mapping, and slot expansion.
"""

from typing import Dict, List, Optional, Set

import pandas as pd

from .column_mapper import _norm_str
from src.constants import (
    SLOT_SUFFIX_SEP, BASE_SLOT_NAMES,
)

# course_type -> slot mapping for simple 1:1 cases
_SIMPLE_MAPPING: Dict[str, Set[str]] = {
    'welcome_drink': {'welcome_drink', 'infused_water'},
    'soup': {'soup'},
    'salad': {'salad'},
    'starter': {'starter'},
    'bread': {'bread'},
    'rice': {'rice'},
    'healthy_rice': {'healthy_rice', 'healthy rice', 'healthy-rice'},
    'dal': {'dal'},
    'veg_gravy': {'veg_gravy'},
    'veg_dry': {'veg_dry'},
    'nonveg_main': {'nonveg_main'},
    'curd_side': {'curd_side'},
    'dessert': {'dessert'},
}


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------

def _base_slot(slot_id: str) -> str:
    s = _norm_str(slot_id)
    if SLOT_SUFFIX_SEP in s:
        left, right = s.rsplit(SLOT_SUFFIX_SEP, 1)
        if right.isdigit():
            return left
    return s


def _slot_num(slot_id: str) -> Optional[int]:
    s = _norm_str(slot_id)
    if SLOT_SUFFIX_SEP in s:
        _, right = s.rsplit(SLOT_SUFFIX_SEP, 1)
        if right.isdigit():
            return int(right)
    return None


def _expand_slots_in_order(base_slots: List[str], slot_counts: Dict[str, int]) -> List[str]:
    """Expand base slot names into numbered instances based on slot_counts."""
    out: List[str] = []
    for s in base_slots:
        n = int(slot_counts.get(s, 1))
        if n <= 0:
            continue
        if n == 1:
            out.append(s)
        else:
            out.extend(f'{s}{SLOT_SUFFIX_SEP}{i}' for i in range(1, n + 1))
    return out


# ---------------------------------------------------------------------------
# PoolBuilder
# ---------------------------------------------------------------------------

class PoolBuilder:
    """
    Builds per-slot item pools from a normalized DataFrame.

    Usage:
        pools = PoolBuilder.build_pools(df)
    """

    @staticmethod
    def build_pools(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """
        Build pools dict mapping each base slot name to the eligible items.

        Handles sambar/rasam splitting where course_type='sambar/rasam' is
        split by item name containing 'rasam'.
        """
        pools: Dict[str, pd.DataFrame] = {}

        # Simple 1:1 course_type -> slot mappings
        for slot, course_types in _SIMPLE_MAPPING.items():
            pools[slot] = df[df['course_type'].isin(course_types)].copy()

        # Special handling: sambar/rasam split
        is_rasam_text = df['item'].str.contains('rasam', na=False)
        pools['rasam'] = df[
            (df['course_type'] == 'rasam') |
            ((df['course_type'] == 'sambar/rasam') & is_rasam_text)
        ].copy()
        pools['sambar'] = df[
            (df['course_type'] == 'sambar') |
            ((df['course_type'] == 'sambar/rasam') & ~is_rasam_text)
        ].copy()

        # Validate all base slots have items
        for slot in BASE_SLOT_NAMES:
            if slot not in pools or len(pools[slot]) == 0:
                raise ValueError(f"Slot '{slot}' has 0 items after mapping.")

        return pools
