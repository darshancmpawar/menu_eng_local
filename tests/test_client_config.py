"""Tests for ClientConfigLoader (Supabase backend)."""

import pytest
from unittest.mock import MagicMock, patch

from src.client.client_config import (
    ClientConfigLoader,
    _dedupe_preserve_order,
    DEFAULT_THEME_MAP,
)


# ---------------------------------------------------------------------------
# Fixtures — fake Supabase responses
# ---------------------------------------------------------------------------

FAKE_CATEGORIES = {
    'menu_cat_1': ['bread', 'veg_dry', 'rice', 'veg_gravy', 'nonveg_main', 'dal',
                   'sambar', 'rasam', 'curd_side', 'dessert', 'salad'],
    'menu_cat_3': ['bread', 'veg_dry', 'rice', 'veg_gravy', 'dal', 'sambar', 'rasam',
                   'curd_side', 'dessert', 'welcome_drink'],
    'menu_cat_4': ['bread', 'veg_dry', 'welcome_drink', 'rice', 'veg_gravy', 'nonveg_main',
                   'soup', 'dal', 'sambar', 'rasam', 'curd_side', 'dessert'],
    'menu_cat_9': ['bread', 'veg_dry', 'rice', 'veg_gravy', 'dal', 'sambar', 'nonveg_main',
                   'rasam', 'curd_side', 'dessert', 'welcome_drink'],
}

FAKE_CLIENTS = [
    {'name': 'Rippling', 'menu_category': 'menu_cat_4'},
    {'name': 'Tekion', 'menu_category': 'menu_cat_1'},
    {'name': 'Vector', 'menu_category': 'menu_cat_3'},
    {'name': 'Stripe', 'menu_category': 'menu_cat_9'},
]

FAKE_SLOT_OVERRIDES = {
    'Rippling': [{'client_name': 'Rippling', 'slot': 'veg_dry', 'count': 2}],
    'Stripe': [{'client_name': 'Stripe', 'slot': 'nonveg_main', 'count': 2}],
}

FAKE_SETTINGS = {
    'core_min_one_slots': ['bread', 'rice', 'starter', 'veg_dry', 'welcome_drink',
                            'curd_side', 'nonveg_main', 'veg_gravy'],
    'constant_slots': ['white_rice', 'papad', 'pickle', 'chutney'],
}


def _make_response(data):
    """Create a mock Supabase response object."""
    resp = MagicMock()
    resp.data = data
    return resp


def _build_mock_sb():
    """Build a mock supabase client that responds to our test queries."""
    sb = MagicMock()

    def table_side_effect(table_name):
        tbl = MagicMock()

        def select_side_effect(cols='*'):
            chain = MagicMock()

            def eq_side_effect(col, val):
                eq_chain = MagicMock()

                def maybe_single_side_effect():
                    ms_chain = MagicMock()
                    if table_name == 'clients':
                        match = [c for c in FAKE_CLIENTS if c[col] == val]
                        ms_chain.execute.return_value = _make_response(match[0] if match else None)
                    elif table_name == 'menu_categories':
                        if val in FAKE_CATEGORIES:
                            ms_chain.execute.return_value = _make_response(
                                {'name': val, 'slots': FAKE_CATEGORIES[val]}
                            )
                        else:
                            ms_chain.execute.return_value = _make_response(None)
                    elif table_name == 'app_settings':
                        if val in FAKE_SETTINGS:
                            ms_chain.execute.return_value = _make_response({'key': val, 'value': FAKE_SETTINGS[val]})
                        else:
                            ms_chain.execute.return_value = _make_response(None)
                    return ms_chain

                eq_chain.maybe_single.side_effect = maybe_single_side_effect

                if table_name == 'slot_count_overrides':
                    eq_chain.execute.return_value = _make_response(
                        FAKE_SLOT_OVERRIDES.get(val, [])
                    )
                elif table_name == 'theme_overrides':
                    eq_chain.execute.return_value = _make_response([])
                elif table_name == 'clients':
                    match = [c for c in FAKE_CLIENTS if c[col] == val]
                    eq_chain.execute.return_value = _make_response(match)

                return eq_chain

            def order_side_effect(col):
                o_chain = MagicMock()
                if table_name == 'clients':
                    sorted_names = sorted(FAKE_CLIENTS, key=lambda c: c['name'])
                    o_chain.execute.return_value = _make_response(sorted_names)
                return o_chain

            chain.eq.side_effect = eq_side_effect
            chain.order.side_effect = order_side_effect

            if table_name == 'clients':
                chain.execute.return_value = _make_response(FAKE_CLIENTS)
            elif table_name == 'menu_categories':
                cat_rows = [{'name': k, 'slots': v} for k, v in FAKE_CATEGORIES.items()]
                chain.execute.return_value = _make_response(cat_rows)
            elif table_name == 'slot_count_overrides':
                all_sco = []
                for v in FAKE_SLOT_OVERRIDES.values():
                    all_sco.extend(v)
                chain.execute.return_value = _make_response(all_sco)
            elif table_name == 'theme_overrides':
                chain.execute.return_value = _make_response([])

            return chain

        tbl.select.side_effect = select_side_effect

        def insert_side_effect(data):
            m = MagicMock()
            m.execute.return_value = _make_response([data] if isinstance(data, dict) else data)
            return m

        def delete_side_effect():
            d = MagicMock()
            d.eq.return_value = MagicMock()
            d.eq.return_value.execute.return_value = _make_response([])
            return d

        def update_side_effect(data):
            u = MagicMock()
            u.eq.return_value = MagicMock()
            u.eq.return_value.execute.return_value = _make_response([data])
            return u

        tbl.insert.side_effect = insert_side_effect
        tbl.delete.side_effect = delete_side_effect
        tbl.update.side_effect = update_side_effect

        return tbl

    sb.table.side_effect = table_side_effect
    return sb


@pytest.fixture
def loader():
    """Return a ClientConfigLoader backed by a mocked Supabase client."""
    mock_sb = _build_mock_sb()
    with patch('src.client.client_config.get_supabase', return_value=mock_sb):
        return ClientConfigLoader()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClientConfigLoader:
    def test_load(self, loader):
        assert len(loader.client_names) == 4

    def test_menu_categories_property(self, loader):
        cats = loader.menu_categories
        assert 'menu_cat_1' in cats
        assert 'bread' in cats['menu_cat_1']

    def test_get_client_rippling(self, loader):
        cfg = loader.get_client('Rippling')
        assert cfg.name == 'Rippling'
        # Rippling has veg_dry: 2, so should have veg_dry__1 and veg_dry__2
        assert 'veg_dry__1' in cfg.active_slots
        assert 'veg_dry__2' in cfg.active_slots
        assert 'veg_dry' not in cfg.active_slots

    def test_get_client_stripe(self, loader):
        cfg = loader.get_client('Stripe')
        assert 'nonveg_main__1' in cfg.active_slots
        assert 'nonveg_main__2' in cfg.active_slots

    def test_get_client_vector_no_overrides(self, loader):
        cfg = loader.get_client('Vector')
        assert 'veg_dry' in cfg.active_slots

    def test_unknown_client_raises(self, loader):
        with pytest.raises(ValueError, match="Unknown client"):
            loader.get_client('NonExistent')

    def test_slot_counts(self, loader):
        counts = loader.get_slot_counts_for_client('Rippling')
        assert counts['veg_dry'] == 2
        assert counts['rice'] == 1

    def test_get_client_menu_category(self, loader):
        cat = loader.get_client_menu_category('Rippling')
        assert cat == 'menu_cat_4'

    def test_active_slots_for_client(self, loader):
        slots = loader.get_active_slots_for_client('Rippling')
        assert 'bread' in slots
        assert 'veg_dry' in slots
        assert isinstance(slots, list)

    def test_slots_for_menu_category(self, loader):
        slots = loader.get_slots_for_menu_category('menu_cat_1')
        assert 'bread' in slots
        assert 'nonveg_main' in slots

    def test_find_or_create_existing_category(self, loader):
        # menu_cat_3 has these exact slots
        slots = ['bread', 'veg_dry', 'rice', 'veg_gravy', 'dal', 'sambar', 'rasam',
                 'curd_side', 'dessert', 'welcome_drink']
        cat_name = loader.find_or_create_menu_category(slots)
        assert cat_name == 'menu_cat_3'

    def test_validate(self, loader):
        loader.validate()

    def test_theme_map_defaults(self, loader):
        theme = loader.get_theme_map_for_client('Rippling')
        assert theme == DEFAULT_THEME_MAP

    def test_core_min_one_slots(self, loader):
        slots = loader.core_min_one_slots
        assert 'bread' in slots
        assert 'rice' in slots


class TestHelpers:
    def test_dedupe_preserve_order(self):
        assert _dedupe_preserve_order(['a', 'b', 'a', 'c']) == ['a', 'b', 'c']
