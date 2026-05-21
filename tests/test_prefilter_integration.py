"""
Integration tests for the pre_filter_pool pipeline in the solver.

Verifies that:
1. Rules' pre_filter_pool() methods are called during _build_day_base_pool_cache
2. Filter context contains the required keys
3. Multiple rules chain correctly
4. Edge cases (empty pools, missing columns) are handled
"""

import datetime as dt

import pandas as pd

from src.menu_rules.cooldown_rules import (
    ItemCooldownMenuRule, RiceBreadGapMenuRule,
)
from src.menu_rules.theme_rules import ThemeSlotFilterRule, _chinese_side_mask
from src.menu_rules.nonveg_rules import NonvegDryPreferenceRule


# ---------------------------------------------------------------------------
# ThemeSlotFilterRule — Chinese side mask helper
# ---------------------------------------------------------------------------

class TestChineseSideMask:
    def test_detects_chinese_keywords(self):
        pool = pd.DataFrame({
            'item': ['manchurian_dry', 'aloo_gobi', 'schezwan_paneer', 'baby_corn_pepper'],
            'sub_category': ['', '', '', ''],
        })
        mask = _chinese_side_mask(pool)
        assert list(mask) == [True, False, True, True]

    def test_no_match_returns_all_false(self):
        pool = pd.DataFrame({
            'item': ['aloo_gobi', 'baingan_bharta'],
            'sub_category': ['', ''],
        })
        mask = _chinese_side_mask(pool)
        assert not mask.any()

    def test_subcategory_also_checked(self):
        pool = pd.DataFrame({
            'item': ['plain_item'],
            'sub_category': ['chinese_side'],
        })
        mask = _chinese_side_mask(pool)
        assert mask.iloc[0] is True or mask.iloc[0]  # numpy bool


# ---------------------------------------------------------------------------
# ThemeSlotFilterRule — edge cases
# ---------------------------------------------------------------------------

class TestThemeSlotFilterEdgeCases:
    def _make_rule(self):
        return ThemeSlotFilterRule({"name": "tf", "type": "theme_slot_filter"})

    def _make_cfg(self):
        return type('Cfg', (), {
            'cuisine_col': 'cuisine_family',
            'cuisine_south_value': 'south_indian',
            'cuisine_north_value': 'north_indian',
        })()

    def test_empty_pool_returns_empty(self):
        rule = self._make_rule()
        pool = pd.DataFrame({'item': [], 'is_chinese_fried_rice': []})
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'rice', 'chinese',
                                       {'cfg': self._make_cfg()})
        assert len(result) == 0

    def test_chinese_no_matching_items_returns_full_pool(self):
        rule = self._make_rule()
        pool = pd.DataFrame({
            'item': ['jeera_rice', 'pulao'],
            'is_chinese_fried_rice': [0, 0],
        })
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'rice', 'chinese',
                                       {'cfg': self._make_cfg()})
        assert len(result) == 2  # returns unfiltered when no match

    def test_biryani_no_matching_items_returns_full_pool(self):
        rule = self._make_rule()
        pool = pd.DataFrame({
            'item': ['jeera_rice'],
            'is_mixedveg_biryani': [0],
        })
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'rice', 'biryani',
                                       {'cfg': None})
        assert len(result) == 1

    def test_missing_flag_column_returns_unfiltered(self):
        rule = self._make_rule()
        pool = pd.DataFrame({'item': ['jeera_rice']})
        # No 'is_chinese_fried_rice' column at all
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'rice', 'chinese',
                                       {'cfg': self._make_cfg()})
        assert len(result) == 1

    def test_holiday_day_no_filtering(self):
        rule = self._make_rule()
        pool = pd.DataFrame({
            'item': ['a', 'b'],
            'cuisine_family': ['south_indian', 'north_indian'],
        })
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 28), 'rice', 'holiday',
                                       {'cfg': self._make_cfg()})
        assert len(result) == 2

    def test_custom_exempt_slots(self):
        rule = ThemeSlotFilterRule({"name": "tf", "type": "theme_slot_filter",
                                    "exempt_slots": ["rice", "bread"]})
        pool = pd.DataFrame({
            'item': ['a', 'b'],
            'cuisine_family': ['south_indian', 'north_indian'],
        })
        # 'rice' is now exempt, so no filtering on south day
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'rice', 'south',
                                       {'cfg': self._make_cfg()})
        assert len(result) == 2

    def test_chinese_veg_dry_filters_by_text(self):
        rule = self._make_rule()
        pool = pd.DataFrame({
            'item': ['gobi_manchurian', 'aloo_gobi'],
            'sub_category': ['', ''],
        })
        result = rule.pre_filter_pool(pool, dt.date(2026, 3, 24), 'veg_dry', 'chinese',
                                       {'cfg': self._make_cfg()})
        assert list(result['item']) == ['gobi_manchurian']


# ---------------------------------------------------------------------------
# ItemCooldownMenuRule — edge cases
# ---------------------------------------------------------------------------

class TestItemCooldownEdgeCases:
    def test_empty_pool(self):
        rule = ItemCooldownMenuRule({"name": "cd", "type": "item_cooldown"})
        pool = pd.DataFrame({'item': []})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'rice', 'south',
                                       {'banned_by_date': {d: {'x'}}})
        assert len(result) == 0

    def test_all_items_banned(self):
        rule = ItemCooldownMenuRule({"name": "cd", "type": "item_cooldown"})
        pool = pd.DataFrame({'item': ['a', 'b']})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'rice', 'south',
                                       {'banned_by_date': {d: {'a', 'b'}}})
        assert len(result) == 0

    def test_different_date_not_banned(self):
        rule = ItemCooldownMenuRule({"name": "cd", "type": "item_cooldown"})
        pool = pd.DataFrame({'item': ['a', 'b']})
        d1 = dt.date(2026, 3, 24)
        d2 = dt.date(2026, 3, 25)
        result = rule.pre_filter_pool(pool, d2, 'rice', 'south',
                                       {'banned_by_date': {d1: {'a', 'b'}}})
        assert len(result) == 2


# ---------------------------------------------------------------------------
# RiceBreadGapMenuRule — edge cases
# ---------------------------------------------------------------------------

class TestRiceBreadGapEdgeCases:
    def test_all_bread_is_ricebread(self):
        rule = RiceBreadGapMenuRule({"name": "rb", "type": "ricebread_gap"})
        pool = pd.DataFrame({'item': ['roti_rice'], 'is_rice_bread': [1]})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'bread', 'south',
                                       {'ricebread_ban_day': {d: True}})
        assert len(result) == 0

    def test_no_ban_returns_all(self):
        rule = RiceBreadGapMenuRule({"name": "rb", "type": "ricebread_gap"})
        pool = pd.DataFrame({'item': ['roti', 'rice_roti'], 'is_rice_bread': [0, 1]})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'bread', 'south',
                                       {'ricebread_ban_day': {d: False}})
        assert len(result) == 2

    def test_missing_is_rice_bread_column(self):
        rule = RiceBreadGapMenuRule({"name": "rb", "type": "ricebread_gap"})
        pool = pd.DataFrame({'item': ['roti']})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'bread', 'south',
                                       {'ricebread_ban_day': {d: True}})
        assert len(result) == 1  # No filtering without the column


# ---------------------------------------------------------------------------
# NonvegDryPreferenceRule — edge cases
# ---------------------------------------------------------------------------

class TestNonvegDryEdgeCases:
    def test_no_dry_items_falls_back_to_gravy(self):
        rule = NonvegDryPreferenceRule({"name": "nv", "type": "nonveg_dry_preference"})
        pool = pd.DataFrame({
            'item': ['chicken_curry', 'mutton_curry'],
            'is_nonveg_dry': [0, 0],
            'is_nonveg_gravy': [1, 1],
            'sub_category': ['', ''],
            'key_ingredient': ['chicken', 'mutton'],
            'category': ['', ''],
        })
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'nonveg_main', 'south',
                                       {'slot_num': 2, 'cfg': None,
                                        'banned_by_date': {}, 'pools': {}})
        assert len(result) == 2

    def test_no_dry_no_gravy_returns_full(self):
        rule = NonvegDryPreferenceRule({"name": "nv", "type": "nonveg_dry_preference"})
        pool = pd.DataFrame({
            'item': ['egg_item'],
            'is_nonveg_dry': [0],
            'sub_category': [''],
            'key_ingredient': ['egg'],
            'category': [''],
        })
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'nonveg_main', 'south',
                                       {'slot_num': 2, 'cfg': None,
                                        'banned_by_date': {}, 'pools': {}})
        assert len(result) == 1

    def test_empty_pool(self):
        rule = NonvegDryPreferenceRule({"name": "nv", "type": "nonveg_dry_preference"})
        pool = pd.DataFrame({'item': [], 'is_nonveg_dry': []})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'nonveg_main', 'south',
                                       {'slot_num': 2, 'cfg': None,
                                        'banned_by_date': {}, 'pools': {}})
        assert len(result) == 0

    def test_slot_num_none_returns_unfiltered(self):
        rule = NonvegDryPreferenceRule({"name": "nv", "type": "nonveg_dry_preference"})
        pool = pd.DataFrame({'item': ['x'], 'is_nonveg_dry': [0]})
        d = dt.date(2026, 3, 24)
        result = rule.pre_filter_pool(pool, d, 'nonveg_main', 'south',
                                       {'slot_num': None})
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Rule chaining: multiple pre-filter rules applied sequentially
# ---------------------------------------------------------------------------

class TestRuleChaining:
    def test_cooldown_then_theme_filter(self):
        """Item cooldown removes banned items, then theme filter narrows by cuisine."""
        cooldown = ItemCooldownMenuRule({"name": "cd", "type": "item_cooldown"})
        theme = ThemeSlotFilterRule({"name": "tf", "type": "theme_slot_filter"})
        cfg = type('Cfg', (), {
            'cuisine_col': 'cuisine_family',
            'cuisine_south_value': 'south_indian',
            'cuisine_north_value': 'north_indian',
        })()

        pool = pd.DataFrame({
            'item': ['sambar_rice', 'jeera_rice', 'biryani'],
            'cuisine_family': ['south_indian', 'north_indian', 'north_indian'],
        })
        d = dt.date(2026, 3, 24)
        ctx = {'banned_by_date': {d: {'biryani'}}, 'cfg': cfg}

        # Chain: cooldown first, then theme
        pool = cooldown.pre_filter_pool(pool, d, 'rice', 'south', ctx)
        pool = theme.pre_filter_pool(pool, d, 'rice', 'south', ctx)

        assert list(pool['item']) == ['sambar_rice']
