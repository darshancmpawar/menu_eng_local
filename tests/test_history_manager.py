"""Tests for HistoryManager."""

import datetime as dt
import pandas as pd
import pytest
from src.history.history_manager import HistoryManager


def _make_long_df():
    """Create synthetic long history."""
    return pd.DataFrame([
        {'service_date': '2026-03-01', 'slot': 'rice', 'item_base': 'jeera rice', 'client_name': 'Rippling'},
        {'service_date': '2026-03-02', 'slot': 'rice', 'item_base': 'lemon rice', 'client_name': 'Rippling'},
        {'service_date': '2026-03-03', 'slot': 'bread', 'item_base': 'rice roti', 'client_name': 'Rippling'},
        {'service_date': '2026-03-10', 'slot': 'rice', 'item_base': 'pulao', 'client_name': 'Stripe'},
        {'service_date': '2026-03-15', 'slot': 'white_rice', 'item_base': 'steamed rice', 'client_name': 'Rippling'},
    ])


def _make_weeks_df():
    return pd.DataFrame([
        {'week_start': '2026-03-02', 'week_signature': 'sig1', 'client_name': 'Rippling'},
        {'week_start': '2026-02-15', 'week_signature': 'sig2', 'client_name': 'Rippling'},
    ])


class TestHistoryManager:
    def test_load_from_dataframes(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df(), _make_weeks_df())
        assert hm._long is not None
        assert hm._weeks is not None

    def test_empty_history(self):
        hm = HistoryManager()
        dates = [dt.date(2026, 3, 20)]
        bans = hm.banned_items_by_date(dates)
        assert bans[dates[0]] == set()

    def test_banned_items(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df())
        dates = [dt.date(2026, 3, 20)]
        bans = hm.banned_items_by_date(dates, cooldown_days=20)
        # Items from March 1-19 within 20 day window of March 20
        assert 'jeera rice' in bans[dates[0]]
        assert 'lemon rice' in bans[dates[0]]

    def test_banned_items_excludes_const_slots(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df())
        dates = [dt.date(2026, 3, 20)]
        bans = hm.banned_items_by_date(dates, cooldown_days=20, const_slots=['white_rice'])
        assert 'steamed rice' not in bans[dates[0]]

    def test_banned_items_excludes_repeatable(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df())
        dates = [dt.date(2026, 3, 20)]
        bans = hm.banned_items_by_date(
            dates, cooldown_days=20, repeatable_items={'jeera rice'}
        )
        assert 'jeera rice' not in bans[dates[0]]

    def test_filter_by_client(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df(), _make_weeks_df())
        filtered = hm.filter_by_client('Rippling')
        dates = [dt.date(2026, 3, 20)]
        bans = filtered.banned_items_by_date(dates, cooldown_days=20)
        assert 'pulao' not in bans[dates[0]]  # Stripe item

    def test_recent_week_signatures(self):
        hm = HistoryManager().load_from_dataframes(weeks_df=_make_weeks_df())
        sigs = hm.recent_week_signatures(dt.date(2026, 3, 16), cooldown_days=30)
        assert 'sig1' in sigs
        assert 'sig2' in sigs

    def test_ricebread_ban(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df())
        dates = [dt.date(2026, 3, 10)]
        result = hm.ricebread_ban_by_date(dates, ricebread_items={'rice roti'}, gap_days=10)
        assert result[dates[0]] is True

    def test_ricebread_no_ban(self):
        hm = HistoryManager().load_from_dataframes(_make_long_df())
        dates = [dt.date(2026, 3, 20)]
        result = hm.ricebread_ban_by_date(dates, ricebread_items={'rice roti'}, gap_days=10)
        assert result[dates[0]] is False

    def test_compute_week_signature(self):
        plan = {
            dt.date(2026, 3, 16): {'rice': 'jeera rice', 'bread': 'naan'},
            dt.date(2026, 3, 17): {'rice': 'lemon rice', 'bread': 'roti'},
        }
        dates = [dt.date(2026, 3, 16), dt.date(2026, 3, 17)]
        sig = HistoryManager.compute_week_signature(plan, dates)
        assert '2026-03-16' in sig
        assert 'rice=jeera rice' in sig

    def test_parse_signature(self):
        sig = '2026-03-16|rice=jeera rice|bread=naan|2026-03-17|rice=lemon rice'
        result = HistoryManager.parse_signature_to_expected_map(sig)
        assert result[('2026-03-16', 'rice')] == 'jeera rice'
        assert result[('2026-03-17', 'rice')] == 'lemon rice'

    def test_save_writes_to_supabase(self):
        """save() inserts long rows + week signature, and deletes any
        previous rows for the same (client, dates) first so re-saving
        produces overwrite semantics."""
        from tests.fake_supabase import FakeSupabase
        fake = FakeSupabase(seed={'menu_history': [], 'week_signatures': []})
        plan = {
            dt.date(2026, 3, 16): {'rice': 'jeera rice', 'bread': 'naan'},
        }
        dates = [dt.date(2026, 3, 16)]

        hm = HistoryManager()
        hm.save(plan, dates, 'Rippling', dt.date(2026, 3, 16), 'test_sig',
                supabase_client=fake)

        long_rows = fake.rows('menu_history')
        assert len(long_rows) == 2  # rice + bread
        assert all(r['client_name'] == 'Rippling' for r in long_rows)
        sigs = fake.rows('week_signatures')
        assert len(sigs) == 1
        assert sigs[0]['week_signature'] == 'test_sig'

    def test_save_overwrites_existing_rows_for_same_dates(self):
        """A second save for the same (client, dates) replaces the
        first save's rows rather than appending. The UNIQUE INDEX on
        menu_history only blocks *identical* duplicates; without the
        explicit delete, two different items in the same slot for the
        same date would both be stored — making cooldown + load
        ambiguous.
        """
        from tests.fake_supabase import FakeSupabase
        fake = FakeSupabase(seed={'menu_history': [], 'week_signatures': []})
        dates = [dt.date(2026, 3, 16)]
        hm = HistoryManager()

        # First save.
        hm.save({dt.date(2026, 3, 16): {'rice': 'jeera rice'}},
                dates, 'Rippling', dt.date(2026, 3, 16),
                'sig_a', supabase_client=fake)
        assert {r['item_base'] for r in fake.rows('menu_history')} == {'jeera rice'}
        assert len(fake.rows('week_signatures')) == 1

        # Second save with a different item in the same slot — must
        # leave only the new row.
        hm.save({dt.date(2026, 3, 16): {'rice': 'biryani'}},
                dates, 'Rippling', dt.date(2026, 3, 16),
                'sig_b', supabase_client=fake)
        items = [r['item_base'] for r in fake.rows('menu_history')]
        assert items == ['biryani']
        # Signature row also overwrites, not appends.
        sigs = fake.rows('week_signatures')
        assert len(sigs) == 1
        assert sigs[0]['week_signature'] == 'sig_b'

    def test_save_only_overwrites_for_matching_client(self):
        """Re-saving for Rippling must not touch Stripe's history rows
        for the same date — the delete filter is keyed on client_name.
        """
        from tests.fake_supabase import FakeSupabase
        fake = FakeSupabase(seed={
            'menu_history': [
                {'id': 1, 'client_name': 'Stripe',
                 'service_date': '2026-03-16', 'slot': 'rice',
                 'item_base': 'stripe rice'},
            ],
            'week_signatures': [],
        })

        hm = HistoryManager()
        hm.save({dt.date(2026, 3, 16): {'rice': 'jeera rice'}},
                [dt.date(2026, 3, 16)], 'Rippling', dt.date(2026, 3, 16),
                'sig_a', supabase_client=fake)
        clients = {r['client_name'] for r in fake.rows('menu_history')}
        assert clients == {'Stripe', 'Rippling'}

    def test_save_requires_supabase_client(self):
        hm = HistoryManager()
        with pytest.raises(ValueError):
            hm.save({}, [], 'Rippling', dt.date(2026, 3, 16), 'sig',
                    supabase_client=None)


class TestLoadSavedPlan:
    """Verify the readback path used by /api/v1/saved-plan."""

    def _seed(self, rows):
        from tests.fake_supabase import FakeSupabase
        return FakeSupabase(seed={'menu_history': rows})

    def test_returns_empty_when_no_rows(self):
        fake = self._seed([])
        out = HistoryManager.load_saved_plan(
            fake, 'Rippling', [dt.date(2026, 3, 16)],
        )
        assert out == {}

    def test_returns_saved_items_grouped_by_date(self):
        fake = self._seed([
            {'id': 1, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'jeera_rice'},
            {'id': 2, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'bread',
             'item_base': 'naan'},
            {'id': 3, 'client_name': 'Rippling',
             'service_date': '2026-03-17', 'slot': 'rice',
             'item_base': 'lemon_rice'},
        ])
        out = HistoryManager.load_saved_plan(
            fake, 'Rippling',
            [dt.date(2026, 3, 16), dt.date(2026, 3, 17)],
        )
        assert out[dt.date(2026, 3, 16)] == {
            'rice': 'jeera_rice', 'bread': 'naan',
        }
        assert out[dt.date(2026, 3, 17)] == {'rice': 'lemon_rice'}

    def test_newest_row_wins_per_slot(self):
        """Legacy data (pre-overwrite) can have multiple rows for the
        same (date, slot). Highest-id wins so the most recent save is
        what callers see — matches the human intuition for the
        'overwrite on save' semantics."""
        fake = self._seed([
            {'id': 1, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'old_rice'},
            {'id': 7, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'new_rice'},
        ])
        out = HistoryManager.load_saved_plan(
            fake, 'Rippling', [dt.date(2026, 3, 16)],
        )
        assert out[dt.date(2026, 3, 16)] == {'rice': 'new_rice'}

    def test_filters_other_clients(self):
        fake = self._seed([
            {'id': 1, 'client_name': 'Stripe',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'stripe_rice'},
            {'id': 2, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'rippling_rice'},
        ])
        out = HistoryManager.load_saved_plan(
            fake, 'Rippling', [dt.date(2026, 3, 16)],
        )
        assert out[dt.date(2026, 3, 16)] == {'rice': 'rippling_rice'}

    def test_only_returns_dates_with_rows(self):
        """Caller distinguishes 'fully saved' from 'partial' by checking
        len(out) vs len(requested_dates). Don't pad missing dates with
        empty dicts."""
        fake = self._seed([
            {'id': 1, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'jeera_rice'},
        ])
        out = HistoryManager.load_saved_plan(
            fake, 'Rippling',
            [dt.date(2026, 3, 16), dt.date(2026, 3, 17)],
        )
        assert list(out.keys()) == [dt.date(2026, 3, 16)]

    def test_empty_dates_is_noop(self):
        fake = self._seed([
            {'id': 1, 'client_name': 'Rippling',
             'service_date': '2026-03-16', 'slot': 'rice',
             'item_base': 'jeera_rice'},
        ])
        assert HistoryManager.load_saved_plan(fake, 'Rippling', []) == {}

    def test_requires_supabase_client(self):
        with pytest.raises(ValueError):
            HistoryManager.load_saved_plan(None, 'Rippling', [dt.date(2026, 3, 16)])
