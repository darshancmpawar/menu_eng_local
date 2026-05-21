"""
History manager for menu planning.

Loads history from Supabase dataframes, filters by client, computes bans
and signatures, and persists completed weeks back to Supabase.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Set

import pandas as pd

from ..preprocessor.column_mapper import _norm_str


class HistoryManager:
    """Encapsulates menu history for cooldown and signature operations."""

    def __init__(self):
        self._long: Optional[pd.DataFrame] = None
        self._weeks: Optional[pd.DataFrame] = None

    # ----- Loading -----

    def load_from_dataframes(
        self,
        long_df: Optional[pd.DataFrame] = None,
        weeks_df: Optional[pd.DataFrame] = None,
    ) -> 'HistoryManager':
        """Load history from existing DataFrames. Returns self for chaining."""
        self._long = self._ensure_long(long_df)
        self._weeks = self._ensure_weeks(weeks_df)
        return self

    # ----- Schema validation -----

    @staticmethod
    def _ensure_long(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or len(df) == 0:
            return None
        h = df.copy()
        if 'service_date' not in h.columns or 'item_base' not in h.columns:
            return None
        h['service_date'] = pd.to_datetime(h['service_date'], errors='coerce').dt.date
        h['item_base'] = h['item_base'].map(_norm_str)
        if 'slot' in h.columns:
            h['slot'] = h['slot'].map(_norm_str)
        if 'client_name' in h.columns:
            h['client_name'] = h['client_name'].map(_norm_str)
        h = h[h['service_date'].notna() & (h['item_base'] != '')]
        return h

    @staticmethod
    def _ensure_weeks(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if df is None or len(df) == 0:
            return None
        h = df.copy()
        if 'week_start' not in h.columns or 'week_signature' not in h.columns:
            return None
        h['week_start'] = pd.to_datetime(h['week_start'], errors='coerce').dt.date
        h['week_signature'] = h['week_signature'].astype(str)
        if 'client_name' in h.columns:
            h['client_name'] = h['client_name'].map(_norm_str)
        h = h[h['week_start'].notna()]
        return h

    # ----- Filtering -----

    def filter_by_client(self, client_name: str) -> 'HistoryManager':
        """Return a new HistoryManager filtered to a single client."""
        c = _norm_str(client_name) if client_name else ''
        hm = HistoryManager()
        hm._long = self._long
        hm._weeks = self._weeks
        if not c:
            return hm
        if hm._long is not None and 'client_name' in hm._long.columns:
            hm._long = hm._long[hm._long['client_name'] == c]
        if hm._weeks is not None and 'client_name' in hm._weeks.columns:
            hm._weeks = hm._weeks[hm._weeks['client_name'] == c]
        return hm

    # ----- Ban computation -----

    def banned_items_by_date(
        self,
        dates: List[dt.date],
        cooldown_days: int = 20,
        const_slots: List[str] = (),
        repeatable_items: Set[str] = frozenset(),
    ) -> Dict[dt.date, Set[str]]:
        """Return banned items per date based on recent usage."""
        h = self._long
        if h is None:
            return {d: set() for d in dates}
        const_set = set(const_slots)
        out: Dict[dt.date, Set[str]] = {}
        for d in dates:
            start = d - dt.timedelta(days=cooldown_days)
            m = (h['service_date'] >= start) & (h['service_date'] < d)
            if 'slot' in h.columns:
                m &= ~h['slot'].isin(const_set)
            banned = set(h.loc[m, 'item_base'].tolist()) - set(repeatable_items)
            out[d] = banned
        return out

    def recent_week_signatures(
        self,
        week_start: dt.date,
        cooldown_days: int = 30,
    ) -> Set[str]:
        """Return week signatures within the cooldown window."""
        h = self._weeks
        if h is None:
            return set()
        start = week_start - dt.timedelta(days=cooldown_days)
        mask = (h['week_start'] >= start) & (h['week_start'] < week_start)
        return set(h.loc[mask, 'week_signature'].tolist())

    def ricebread_ban_by_date(
        self,
        dates: List[dt.date],
        ricebread_items: Set[str],
        gap_days: int = 10,
        base_slot_fn=None,
    ) -> Dict[dt.date, bool]:
        """Return per-date flag if rice-bread was used too recently."""
        h = self._long
        if h is None or gap_days <= 0 or not ricebread_items:
            return {d: False for d in dates}
        out: Dict[dt.date, bool] = {}
        for d in dates:
            start = d - dt.timedelta(days=gap_days)
            m = (h['service_date'] >= start) & (h['service_date'] < d)
            if 'slot' in h.columns:
                if base_slot_fn is None:
                    m &= h['slot'] == 'bread'
                else:
                    m &= h['slot'].map(base_slot_fn) == 'bread'
            m &= h['item_base'].isin(ricebread_items)
            out[d] = bool(m.any())
        return out

    # ----- Save -----

    def save(
        self,
        week_plan: Dict,
        dates: List[dt.date],
        client_name: str,
        week_start: dt.date,
        week_signature: str,
        supabase_client,
        strip_color_fn=None,
    ):
        """Persist a completed week plan to Supabase with **overwrite**
        semantics.

        Re-saving a plan for the same (client, dates) replaces the
        previously saved rows instead of appending — so the UI's "Save
        again after regenerating" flow ends up with exactly one set of
        rows per date, and "Load saved plan" returns the freshest pick.

        Implementation: DELETE existing rows for (client_name,
        service_date IN dates), then INSERT the new rows. The same goes
        for ``week_signatures`` keyed by ``(client_name, week_start)``.
        The brief window between DELETE and INSERT is the only time the
        DB has no plan for these dates; an INSERT failure surfaces to
        the caller (the API returns 500), and the user retries. We
        accept that small risk in exchange for not needing a transactional
        RPC against Supabase.
        """
        if supabase_client is None:
            raise ValueError("supabase_client is required to save history.")

        # Build long-history rows (keep original client_name to match FK)
        long_rows = []
        for d in dates:
            day_map = week_plan.get(d, {})
            for slot_id, item_val in day_map.items():
                item_base = strip_color_fn(item_val) if strip_color_fn else item_val
                long_rows.append({
                    'service_date': d.isoformat(),
                    'slot': slot_id,
                    'item_base': _norm_str(item_base),
                    'client_name': client_name,
                })

        # Overwrite: clear any previous rows for these (client, date) pairs
        # before inserting the fresh plan. Without this a second save for
        # the same week would accumulate rows (the UNIQUE INDEX only
        # blocks identical-item duplicates, not different items in the
        # same slot).
        date_isos = [d.isoformat() for d in dates]
        if date_isos:
            (
                supabase_client.table('menu_history')
                .delete()
                .eq('client_name', client_name)
                .in_('service_date', date_isos)
                .execute()
            )
        if long_rows:
            supabase_client.table('menu_history').insert(long_rows).execute()

        # Same overwrite rule for the per-week signature row.
        (
            supabase_client.table('week_signatures')
            .delete()
            .eq('client_name', client_name)
            .eq('week_start', week_start.isoformat())
            .execute()
        )
        supabase_client.table('week_signatures').insert({
            'week_start': week_start.isoformat(),
            'week_signature': week_signature,
            'client_name': client_name,
        }).execute()

    # ----- Load saved plan -----

    @staticmethod
    def load_saved_plan(
        supabase_client,
        client_name: str,
        dates: List[dt.date],
    ) -> Dict[dt.date, Dict[str, str]]:
        """Return the saved menu for *client_name* across *dates*.

        Result shape: ``{date: {slot_id: item_base}}``. Only dates that
        have at least one saved row are present in the dict, so callers
        can distinguish "fully saved" (all requested dates present) from
        "partially saved" (some missing) or "not saved" (empty dict).

        ``item_base`` is the de-colorised normalised name we persisted
        in ``menu_history``. The caller is responsible for re-attaching
        a color suffix for display (see ``api.app._enrich_history_plan``).

        If multiple rows exist for the same (date, slot) — possible from
        legacy data before this revision added overwrite semantics — the
        row with the highest ``id`` (newest) wins, so the most recent
        save is what callers see.
        """
        if supabase_client is None:
            raise ValueError("supabase_client is required to load history.")
        if not dates:
            return {}

        date_isos = [d.isoformat() for d in dates]
        resp = (
            supabase_client.table('menu_history')
            .select('service_date, slot, item_base, id')
            .eq('client_name', client_name)
            .in_('service_date', date_isos)
            .execute()
        )
        rows = resp.data or []

        # Newest row wins per (date, slot). Sort by id ascending then
        # overwrite — final state holds the highest-id pick. ``id`` is a
        # monotonically increasing identity column in menu_history.
        rows.sort(key=lambda r: r.get('id') or 0)

        by_iso: Dict[str, Dict[str, str]] = {}
        for r in rows:
            iso = r.get('service_date')
            slot = r.get('slot')
            item_base = r.get('item_base')
            if not iso or not slot:
                continue
            by_iso.setdefault(iso, {})[slot] = item_base or ''

        # Translate ISO strings back to dt.date for the caller. We do
        # this last (rather than at row-iteration time) so an unexpected
        # date format from Supabase doesn't silently drop a row.
        out: Dict[dt.date, Dict[str, str]] = {}
        for iso, slots in by_iso.items():
            try:
                out[dt.date.fromisoformat(iso)] = slots
            except (TypeError, ValueError):
                continue
        return out

    # ----- Signature computation -----

    @staticmethod
    def compute_week_signature(
        week_plan: Dict,
        dates: List[dt.date],
        const_slots: List[str] = (),
        strip_color_fn=None,
    ) -> str:
        """Compute a deterministic signature string for a week plan."""
        const_set = set(const_slots)

        # Infer slot order from the first non-empty day
        slot_order: List[str] = []
        for d in dates:
            day_map = week_plan.get(d, {})
            if day_map:
                slot_order = [k for k in day_map.keys() if k not in const_set]
                break

        parts: List[str] = []
        for d in dates:
            parts.append(d.isoformat())
            day_map = week_plan.get(d, {})
            for slot_id in slot_order:
                val = day_map.get(slot_id, '')
                if strip_color_fn:
                    val = strip_color_fn(val)
                parts.append(f'{slot_id}={_norm_str(val)}')
        return '|'.join(parts)

    # ----- Signature parsing -----

    @staticmethod
    def parse_signature_to_expected_map(sig: str) -> Dict:
        """Parse a week signature into {(date_iso, slot): item_base} map."""
        parts = sig.split('|')
        out: Dict = {}
        i = 0
        while i < len(parts):
            token = parts[i]
            if re.match(r'^\d{4}-\d{2}-\d{2}$', token):
                date_iso = token
                i += 1
                while i < len(parts) and not re.match(r'^\d{4}-\d{2}-\d{2}$', parts[i]):
                    kv = parts[i]
                    if '=' in kv:
                        slot, val = kv.split('=', 1)
                        out[(date_iso, _norm_str(slot))] = _norm_str(val)
                    i += 1
            else:
                i += 1
        return out
