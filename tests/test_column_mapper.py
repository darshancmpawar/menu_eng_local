"""Tests for ColumnMapper alias detection and normalization."""

import pandas as pd
from src.preprocessor.column_mapper import (
    ColumnMapper, pick_col, _norm_str, _norm_color, _to_bool01,
    _is_deepfried_starter_row, _is_nonveg_dry_row,
)


class TestPickCol:
    def test_exact_match(self):
        df = pd.DataFrame(columns=['item', 'course_type'])
        assert pick_col(df, ['item']) == 'item'

    def test_case_insensitive(self):
        df = pd.DataFrame(columns=['Item', 'Course_Type'])
        assert pick_col(df, ['item']) == 'Item'

    def test_alias_fallback(self):
        df = pd.DataFrame(columns=['menu_items', 'course'])
        assert pick_col(df, ['item', 'menu_items', 'menu_item']) == 'menu_items'

    def test_no_match(self):
        df = pd.DataFrame(columns=['foo', 'bar'])
        assert pick_col(df, ['item', 'menu_items']) is None


class TestNormHelpers:
    def test_norm_str_na(self):
        assert _norm_str(None) == ''
        assert _norm_str(float('nan')) == ''

    def test_norm_str_strip_lower(self):
        assert _norm_str('  Hello World  ') == 'hello world'

    def test_norm_color_empty(self):
        assert _norm_color('') == 'unknown'
        assert _norm_color('NA') == 'unknown'
        assert _norm_color('nan') == 'unknown'
        assert _norm_color(None) == 'unknown'

    def test_norm_color_valid(self):
        assert _norm_color('Red') == 'red'
        assert _norm_color('Light Green') == 'light_green'

    def test_to_bool01(self):
        assert _to_bool01(1) == 1
        assert _to_bool01(0) == 0
        assert _to_bool01('y') == 1
        assert _to_bool01('yes') == 1
        assert _to_bool01('true') == 1
        assert _to_bool01('no') == 0
        assert _to_bool01(None) == 0
        assert _to_bool01(float('nan')) == 0


class TestColumnMapper:
    def _make_df(self, cols):
        return pd.DataFrame({c: ['x'] for c in cols})

    def test_detect_canonical_names(self):
        df = self._make_df(['item', 'course_type', 'cuisine_family', 'item_color'])
        mapper = ColumnMapper().detect(df)
        assert mapper.validate()['valid'] is True
        assert mapper.rename_map == {}

    def test_detect_aliases(self):
        df = self._make_df(['menu_items', 'course', 'colour'])
        mapper = ColumnMapper().detect(df)
        assert mapper.validate()['valid'] is True
        assert 'menu_items' in mapper.rename_map
        assert mapper.rename_map['menu_items'] == 'item'

    def test_missing_required(self):
        df = self._make_df(['foo', 'bar'])
        mapper = ColumnMapper().detect(df)
        result = mapper.validate()
        assert result['valid'] is False
        assert 'item' in result['missing_columns']

    def test_flag_alias_detection(self):
        df = self._make_df(['item', 'course_type', 'is_non_veg_dry'])
        mapper = ColumnMapper().detect(df)
        assert 'is_non_veg_dry' in mapper.rename_map
        assert mapper.rename_map['is_non_veg_dry'] == 'is_nonveg_dry'

    def test_apply_creates_item_id(self):
        df = pd.DataFrame({'item': ['Paneer Tikka', 'Dal Makhani'], 'course_type': ['starter', 'dal']})
        mapper = ColumnMapper().detect(df)
        result = mapper.apply(df)
        assert 'item_id' in result.columns
        assert result['item_id'].iloc[0] == 'Paneer Tikka'

    def test_apply_normalizes_flags(self):
        df = pd.DataFrame({
            'item': ['A', 'B'],
            'course_type': ['starter', 'rice'],
            'is_liquid_rice': [1, 0],
            'is_rice_bread': ['y', 'no'],
        })
        mapper = ColumnMapper().detect(df)
        result = mapper.apply(df)
        assert result['is_liquid_rice'].tolist() == [1, 0]
        assert result['is_rice_bread'].tolist() == [1, 0]
        # Missing flag columns default to 0
        assert result['is_chinese_fried_rice'].tolist() == [0, 0]

    def test_apply_normalizes_color(self):
        df = pd.DataFrame({
            'item': ['A'], 'course_type': ['starter'], 'item_color': ['NA']
        })
        mapper = ColumnMapper().detect(df)
        result = mapper.apply(df)
        assert result['item_color'].iloc[0] == 'unknown'

    def test_apply_computes_key_eff(self):
        df = pd.DataFrame({
            'item': ['Paneer Tikka'],
            'course_type': ['starter'],
            'key_ingredient': ['paneer'],
            'cuisine_family': ['north_indian'],
            'sub_category': ['tandoor'],
        })
        mapper = ColumnMapper().detect(df)
        result = mapper.apply(df)
        assert 'key_eff' in result.columns
        # For non-chicken/egg/rice items, key_eff = key_ingredient
        assert result['key_eff'].iloc[0] == 'paneer'


class TestDeepFriedDetection:
    def test_flag_based(self):
        row = pd.Series({'item': 'some item', 'sub_category': '', 'is_deep_fried_starter': 1})
        assert _is_deepfried_starter_row(row) is True

    def test_keyword_based(self):
        row = pd.Series({'item': 'onion pakoda', 'sub_category': ''})
        assert _is_deepfried_starter_row(row) is True

    def test_no_match(self):
        row = pd.Series({'item': 'paneer tikka', 'sub_category': 'tandoor'})
        assert _is_deepfried_starter_row(row) is False


class TestNonvegDryDetection:
    def test_flag_based(self):
        row = pd.Series({
            'item': 'chicken fry', 'sub_category': '', 'key_ingredient': '',
            'is_nonveg_dry': 1, 'category': '',
        })
        assert _is_nonveg_dry_row(row) is True

    def test_category_based(self):
        row = pd.Series({
            'item': 'chicken', 'sub_category': '', 'key_ingredient': '',
            'is_nonveg_dry': 0, 'category': 'chicken_dry',
        })
        assert _is_nonveg_dry_row(row) is True

    def test_text_based(self):
        row = pd.Series({
            'item': 'chicken dry fry', 'sub_category': 'chicken_dry',
            'key_ingredient': '', 'is_nonveg_dry': 0, 'category': '',
        })
        assert _is_nonveg_dry_row(row) is True
