"""
Theme-based static pool pre-filtering.

Applied BEFORE solver variable creation to restrict candidate pools
based on the day's theme (Chinese, Biryani, South, North, Mix).
"""

from __future__ import annotations

from typing import Set

import pandas as pd

from .column_mapper import _norm_str, _to_bool01


def _is_chinese_cuisine_value(x: str) -> bool:
    s = _norm_str(x)
    return s == 'chinese' or 'chinese' in s


def _filter_flag(pool: pd.DataFrame, flag_col: str | None, val: int) -> pd.DataFrame:
    if flag_col is None or flag_col not in pool.columns:
        return pool
    return pool[pool[flag_col] == val]


def starter_theme_match_row(row: pd.Series, cfg, day_type: str) -> bool:
    cf = _norm_str(row.get(cfg.cuisine_col, ''))
    if day_type == 'south':
        return cf == cfg.cuisine_south_value
    if day_type == 'north':
        return cf == cfg.cuisine_north_value
    if day_type == 'mix':
        return cf in {cfg.cuisine_south_value, cfg.cuisine_north_value}
    if day_type == 'chinese':
        flag_col = cfg.f_chinese_starter or 'is_chinese_starter'
        return int(row.get(flag_col, 0)) == 1 if flag_col in row.index else _is_chinese_cuisine_value(cf)
    return False


def starter_theme_mask(pool: pd.DataFrame, cfg, day_type: str) -> pd.Series:
    if len(pool) == 0:
        return pd.Series([], dtype=bool, index=pool.index)
    return pool.apply(lambda r: starter_theme_match_row(r, cfg, day_type), axis=1)


def chinese_side_mask(pool: pd.DataFrame, cfg) -> pd.Series:
    if len(pool) == 0:
        return pd.Series([], dtype=bool, index=pool.index)
    flag_col = cfg.f_chinese_starter or 'is_chinese_starter'
    by_flag = pool[flag_col].map(_to_bool01) == 1 if flag_col in pool.columns else pd.Series(False, index=pool.index)
    by_cuisine = pool[cfg.cuisine_col].map(_is_chinese_cuisine_value) if cfg.cuisine_col in pool.columns else pd.Series(False, index=pool.index)
    return (by_flag | by_cuisine).astype(bool)


def theme_preference_mask(base_slot: str, pool: pd.DataFrame, cfg, day_type: str) -> pd.Series:
    if len(pool) == 0:
        return pd.Series([], dtype=bool, index=pool.index)
    if base_slot == 'starter' and cfg.prefer_theme_starter:
        if day_type in ('mix', 'south', 'north', 'chinese'):
            return starter_theme_mask(pool, cfg, day_type)
        return pd.Series(False, index=pool.index)
    if base_slot == 'veg_dry' and day_type == 'chinese':
        return chinese_side_mask(pool, cfg)
    return pd.Series(False, index=pool.index)


def apply_non_theme_exclusions(base_slot: str, pool: pd.DataFrame, cfg, day_type: str) -> pd.DataFrame:
    if day_type != 'chinese' and base_slot in ('starter', 'veg_dry'):
        return pool[~chinese_side_mask(pool, cfg)]
    return pool


def apply_theme_slot_locks(base_slot: str, pool: pd.DataFrame, cfg, day_type: str) -> pd.DataFrame:
    out = pool
    if day_type == 'chinese':
        if base_slot == 'rice':
            out = _filter_flag(out, cfg.f_chinese_rice, 1)
        elif base_slot == 'veg_gravy':
            out = _filter_flag(out, cfg.f_chinese_veg_gravy, 1)
        elif base_slot == 'nonveg_main':
            out = _filter_flag(out, cfg.f_chinese_nonveg, 1)
    else:
        if base_slot == 'rice':
            out = _filter_flag(out, cfg.f_chinese_rice, 0)
        elif base_slot == 'veg_gravy':
            out = _filter_flag(out, cfg.f_chinese_veg_gravy, 0)
        elif base_slot == 'nonveg_main':
            out = _filter_flag(out, cfg.f_chinese_nonveg, 0)

    if day_type == 'biryani':
        if base_slot == 'nonveg_main':
            out = _filter_flag(out, cfg.f_nonveg_biryani, 1)
        elif base_slot == 'rice':
            out = _filter_flag(out, cfg.f_veg_biryani, 1)
    else:
        if base_slot == 'rice':
            out = _filter_flag(out, cfg.f_veg_biryani, 0)
        elif base_slot == 'nonveg_main':
            out = _filter_flag(out, cfg.f_nonveg_biryani, 0)
    return out


def apply_cuisine_theme_filters(base_slot: str, pool: pd.DataFrame, cfg, day_type: str, exempt_from_cuisine: Set[str]) -> pd.DataFrame:
    out = pool
    if base_slot not in exempt_from_cuisine:
        if day_type == 'south':
            out = out[out[cfg.cuisine_col] == cfg.cuisine_south_value]
        elif day_type == 'north':
            out = out[out[cfg.cuisine_col] == cfg.cuisine_north_value]
        elif day_type == 'mix':
            out = out[out[cfg.cuisine_col].isin({cfg.cuisine_south_value, cfg.cuisine_north_value})]
    if base_slot == 'bread':
        if day_type == 'south':
            out = out[out[cfg.cuisine_col] == cfg.cuisine_south_value]
        else:
            out = out[out[cfg.cuisine_col] != cfg.cuisine_south_value]
    return out


def enforce_day_slot_filters_static(base_slot: str, pool: pd.DataFrame, cfg, day_type: str, exempt_from_cuisine: Set[str]) -> pd.DataFrame:
    """Apply all static theme filters to a pool for a given day type."""
    out = pool
    if base_slot in ('rice', 'healthy_rice') and len(out) > 0:
        out = out[~out['item'].isin(cfg.rice_exclude_items)]
    out = apply_theme_slot_locks(base_slot, out, cfg, day_type)
    out = apply_non_theme_exclusions(base_slot, out, cfg, day_type)
    out = apply_cuisine_theme_filters(base_slot, out, cfg, day_type, exempt_from_cuisine)
    return out
