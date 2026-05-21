"""Tests for PoolBuilder."""

import pandas as pd
import pytest
from src.preprocessor.pool_builder import (
    PoolBuilder, BASE_SLOT_NAMES, _base_slot, _slot_num,
    _expand_slots_in_order,
)


def _make_ontology_df():
    """Create a minimal ontology DataFrame with at least 1 item per slot."""
    rows = []
    for slot in ['welcome_drink', 'soup', 'salad', 'starter', 'bread', 'rice',
                 'healthy_rice', 'dal', 'veg_gravy', 'veg_dry', 'nonveg_main',
                 'curd_side', 'dessert']:
        rows.append({'item': f'{slot}_item_1', 'course_type': slot, 'cuisine_family': 'indian', 'item_color': 'red'})
        rows.append({'item': f'{slot}_item_2', 'course_type': slot, 'cuisine_family': 'indian', 'item_color': 'green'})

    # sambar and rasam via sambar/rasam course_type
    rows.append({'item': 'sambar dal', 'course_type': 'sambar', 'cuisine_family': 'south_indian', 'item_color': 'yellow'})
    rows.append({'item': 'tomato rasam', 'course_type': 'sambar/rasam', 'cuisine_family': 'south_indian', 'item_color': 'orange'})
    rows.append({'item': 'sambar special', 'course_type': 'sambar/rasam', 'cuisine_family': 'south_indian', 'item_color': 'yellow'})

    # infused_water maps to welcome_drink
    rows.append({'item': 'lemon water', 'course_type': 'infused_water', 'cuisine_family': 'indian', 'item_color': 'yellow'})

    return pd.DataFrame(rows)


class TestPoolBuilder:
    def test_all_base_slots_populated(self):
        df = _make_ontology_df()
        pools = PoolBuilder.build_pools(df)
        for slot in BASE_SLOT_NAMES:
            assert len(pools[slot]) > 0, f"Slot {slot} has no items"

    def test_sambar_rasam_split(self):
        df = _make_ontology_df()
        pools = PoolBuilder.build_pools(df)
        # 'tomato rasam' should be in rasam pool
        rasam_items = pools['rasam']['item'].tolist()
        assert 'tomato rasam' in rasam_items
        # 'sambar special' and 'sambar dal' should be in sambar pool
        sambar_items = pools['sambar']['item'].tolist()
        assert 'sambar dal' in sambar_items
        assert 'sambar special' in sambar_items

    def test_welcome_drink_includes_infused_water(self):
        df = _make_ontology_df()
        pools = PoolBuilder.build_pools(df)
        items = pools['welcome_drink']['item'].tolist()
        assert 'lemon water' in items

    def test_empty_slot_raises(self):
        # Create df missing 'dessert'
        df = _make_ontology_df()
        df = df[df['course_type'] != 'dessert']
        with pytest.raises(ValueError, match="dessert"):
            PoolBuilder.build_pools(df)


class TestSlotHelpers:
    def test_base_slot_simple(self):
        assert _base_slot('veg_dry') == 'veg_dry'

    def test_base_slot_numbered(self):
        assert _base_slot('veg_dry__2') == 'veg_dry'

    def test_slot_num_simple(self):
        assert _slot_num('veg_dry') is None

    def test_slot_num_numbered(self):
        assert _slot_num('veg_dry__2') == 2

    def test_expand_slots_single(self):
        result = _expand_slots_in_order(['rice', 'veg_dry'], {'rice': 1, 'veg_dry': 1})
        assert result == ['rice', 'veg_dry']

    def test_expand_slots_multiple(self):
        result = _expand_slots_in_order(['rice', 'veg_dry'], {'rice': 1, 'veg_dry': 2})
        assert result == ['rice', 'veg_dry__1', 'veg_dry__2']

    def test_expand_slots_zero(self):
        result = _expand_slots_in_order(['rice', 'veg_dry'], {'rice': 0, 'veg_dry': 1})
        assert result == ['veg_dry']
