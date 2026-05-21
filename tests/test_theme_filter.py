"""Tests for theme_filter.py pre-solver static filtering."""

import pytest
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, Set

from src.preprocessor.theme_filter import (
    starter_theme_match_row,
    starter_theme_mask,
    chinese_side_mask,
    theme_preference_mask,
    apply_non_theme_exclusions,
    apply_theme_slot_locks,
    apply_cuisine_theme_filters,
    enforce_day_slot_filters_static,
)


@dataclass
class MockCfg:
    """Minimal mock of SolverConfig for theme_filter tests."""
    cuisine_col: str = 'cuisine'
    cuisine_south_value: str = 'south_indian'
    cuisine_north_value: str = 'north_indian'
    f_chinese_starter: Optional[str] = 'is_chinese_starter'
    f_chinese_rice: Optional[str] = 'is_chinese_rice'
    f_chinese_veg_gravy: Optional[str] = 'is_chinese_veg_gravy'
    f_chinese_nonveg: Optional[str] = 'is_chinese_nonveg'
    f_nonveg_biryani: Optional[str] = 'is_nonveg_biryani'
    f_veg_biryani: Optional[str] = 'is_veg_biryani'
    prefer_theme_starter: bool = True
    rice_exclude_items: Set[str] = field(default_factory=lambda: {'steamed_rice', 'white_rice'})


@pytest.fixture
def starter_pool():
    return pd.DataFrame({
        'item': ['paneer tikka', 'spring roll', 'medu vada', 'aloo tikki'],
        'cuisine': ['north_indian', 'chinese', 'south_indian', 'north_indian'],
        'is_chinese_starter': [0, 1, 0, 0],
    })


@pytest.fixture
def rice_pool():
    return pd.DataFrame({
        'item': ['jeera rice', 'fried rice', 'steamed_rice', 'biryani rice'],
        'cuisine': ['north_indian', 'chinese', 'north_indian', 'south_indian'],
        'is_chinese_rice': [0, 1, 0, 0],
        'is_veg_biryani': [0, 0, 0, 1],
    })


@pytest.fixture
def cfg():
    return MockCfg()


class TestStarterThemeMatchRow:
    def test_south_theme_matches_south_cuisine(self, starter_pool, cfg):
        row = starter_pool.iloc[2]  # medu vada, south_indian
        assert starter_theme_match_row(row, cfg, 'south') is True

    def test_south_theme_rejects_north(self, starter_pool, cfg):
        row = starter_pool.iloc[0]  # paneer tikka, north_indian
        assert starter_theme_match_row(row, cfg, 'south') is False

    def test_north_theme_matches_north(self, starter_pool, cfg):
        row = starter_pool.iloc[0]  # paneer tikka, north_indian
        assert starter_theme_match_row(row, cfg, 'north') is True

    def test_mix_accepts_both(self, starter_pool, cfg):
        assert starter_theme_match_row(starter_pool.iloc[0], cfg, 'mix') is True   # north
        assert starter_theme_match_row(starter_pool.iloc[2], cfg, 'mix') is True   # south

    def test_chinese_uses_flag(self, starter_pool, cfg):
        row = starter_pool.iloc[1]  # spring roll, is_chinese_starter=1
        assert starter_theme_match_row(row, cfg, 'chinese') is True

    def test_chinese_rejects_non_chinese(self, starter_pool, cfg):
        row = starter_pool.iloc[0]  # paneer tikka, is_chinese_starter=0
        assert starter_theme_match_row(row, cfg, 'chinese') is False


class TestStarterThemeMask:
    def test_south_mask(self, starter_pool, cfg):
        mask = starter_theme_mask(starter_pool, cfg, 'south')
        assert mask.sum() == 1  # medu vada only
        assert mask.iloc[2] == True

    def test_empty_pool(self, cfg):
        empty = pd.DataFrame(columns=['item', 'cuisine', 'is_chinese_starter'])
        mask = starter_theme_mask(empty, cfg, 'south')
        assert len(mask) == 0


class TestChineseSideMask:
    def test_detects_chinese_by_flag(self, starter_pool, cfg):
        mask = chinese_side_mask(starter_pool, cfg)
        assert mask.iloc[1] == True  # spring roll (is_chinese_starter=1)

    def test_non_chinese_items(self, starter_pool, cfg):
        mask = chinese_side_mask(starter_pool, cfg)
        assert mask.iloc[0] == False  # paneer tikka


class TestThemePreferenceMask:
    def test_starter_south(self, starter_pool, cfg):
        mask = theme_preference_mask('starter', starter_pool, cfg, 'south')
        assert mask.sum() == 1

    def test_non_starter_slot(self, starter_pool, cfg):
        mask = theme_preference_mask('dal', starter_pool, cfg, 'south')
        assert mask.sum() == 0

    def test_veg_dry_chinese(self, starter_pool, cfg):
        mask = theme_preference_mask('veg_dry', starter_pool, cfg, 'chinese')
        # Should use chinese_side_mask
        assert isinstance(mask, pd.Series)


class TestApplyThemeSlotLocks:
    def test_chinese_rice_filter(self, rice_pool, cfg):
        result = apply_theme_slot_locks('rice', rice_pool, cfg, 'chinese')
        assert len(result) == 1  # only fried rice has is_chinese_rice=1

    def test_non_chinese_rice_excludes_chinese(self, rice_pool, cfg):
        result = apply_theme_slot_locks('rice', rice_pool, cfg, 'south')
        # Should exclude is_chinese_rice=1
        assert 'fried rice' not in result['item'].values

    def test_biryani_rice_filter(self, rice_pool, cfg):
        result = apply_theme_slot_locks('rice', rice_pool, cfg, 'biryani')
        assert len(result) == 1  # only biryani rice has is_veg_biryani=1


class TestApplyNonThemeExclusions:
    def test_non_chinese_day_excludes_chinese_starters(self, starter_pool, cfg):
        result = apply_non_theme_exclusions('starter', starter_pool, cfg, 'south')
        assert 'spring roll' not in result['item'].values

    def test_chinese_day_keeps_all(self, starter_pool, cfg):
        result = apply_non_theme_exclusions('starter', starter_pool, cfg, 'chinese')
        assert len(result) == len(starter_pool)


class TestApplyCuisineThemeFilters:
    def test_south_filters_to_south(self, starter_pool, cfg):
        result = apply_cuisine_theme_filters('starter', starter_pool, cfg, 'south', set())
        assert all(result['cuisine'] == 'south_indian')

    def test_exempt_slot_not_filtered(self, starter_pool, cfg):
        result = apply_cuisine_theme_filters('starter', starter_pool, cfg, 'south', {'starter'})
        assert len(result) == len(starter_pool)

    def test_mix_allows_both(self, starter_pool, cfg):
        result = apply_cuisine_theme_filters('starter', starter_pool, cfg, 'mix', set())
        assert set(result['cuisine'].unique()) <= {'south_indian', 'north_indian'}


class TestEnforceDaySlotFiltersStatic:
    def test_rice_excludes_steamed(self, rice_pool, cfg):
        result = enforce_day_slot_filters_static('rice', rice_pool, cfg, 'south', set())
        assert 'steamed_rice' not in result['item'].values

    def test_empty_pool_returns_empty(self, cfg):
        empty = pd.DataFrame(columns=['item', 'cuisine', 'is_chinese_rice', 'is_veg_biryani'])
        result = enforce_day_slot_filters_static('rice', empty, cfg, 'south', set())
        assert len(result) == 0
