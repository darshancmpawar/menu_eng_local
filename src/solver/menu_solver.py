"""
Menu planning solver using Google OR-Tools CP-SAT.

Cell-based architecture: each (day, slot) pair has a pre-filtered candidate pool.
The solver creates one boolean variable per candidate per cell and selects exactly one.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Set, Tuple

logger = logging.getLogger(__name__)

import pandas as pd
from ortools.sat.python import cp_model

from ._helpers import weekday_type_for_config as _weekday_type_cfg
from ..menu_rules.base_menu_rule import BaseMenuRule, MenuRuleSeverity
from src.constants import (
    BASE_SLOT_NAMES, CONSTANT_ITEMS, EXEMPT_FROM_CUISINE,
    RICE_EXCLUDE_ITEMS, THEME_FALLBACK_SLOTS,
)
from ..preprocessor.pool_builder import _base_slot, _slot_num, _expand_slots_in_order
from ..preprocessor.column_mapper import _norm_str, _norm_color, _to_bool01
from .solver_context import SolverContext


# ---------------------------------------------------------------------------
# Config dataclass (runtime solver configuration)
# ---------------------------------------------------------------------------

# Default candidate pool caps per base slot (used in multi-restart strategy)
DEFAULT_CAP_BY_SLOT: Dict[str, int] = {
    'rice': 1600, 'healthy_rice': 1200, 'veg_gravy': 1400,
    'nonveg_main': 1400, 'curd_side': 1400, 'veg_dry': 1100,
    'bread': 1100, 'starter': 1200, 'soup': 900, 'salad': 900,
    'dal': 1000, 'dessert': 1000, 'welcome_drink': 1000,
    'sambar': 900, 'rasam': 900,
}
DEFAULT_CAP = 900  # fallback for slots not in DEFAULT_CAP_BY_SLOT

# Multi-restart strategy defaults
DEFAULT_CAP_MULTIPLIERS = (1, 2)  # try 1x then 2x candidate pool sizes
DEFAULT_RESTARTS_PER_MULTIPLIER = 4  # attempts per multiplier
DEFAULT_SEED_MULT_FACTOR = 1000  # seed formula: base + mult * FACTOR + restart * 17
DEFAULT_SEED_RESTART_STEP = 17

# Penalty/bonus weights
REGEN_SIMILARITY_PENALTY = -10_000  # penalty for re-selecting old items during regen
REGEN_CAP_MULTIPLIER = 1.5  # candidate cap multiplier for regeneration


@dataclass
class SolverConfig:
    """Runtime configuration for the CP-SAT menu solver."""
    days: int = 5
    start_date: dt.date = field(default_factory=dt.date.today)
    seed: int = 7
    time_limit_sec: int = 240
    slot_counts: Optional[Dict[str, int]] = None
    active_base_slots: Optional[List[str]] = None
    explicit_dates: Optional[List[dt.date]] = None
    # Color constraints
    color_col: str = 'item_color'
    color_slots: List[str] = field(default_factory=lambda: [
        'starter', 'rice', 'veg_gravy', 'veg_dry', 'nonveg_main', 'dal', 'dessert',
    ])
    min_distinct_colors_per_day: int = 4
    min_distinct_colors_per_day_chinese: int = 4
    min_distinct_colors_per_day_biryani: int = 4
    max_same_color_per_day: int = 2
    ignore_rice_gravy_color_diff_on_chinese_day: bool = True
    # Premium item constraints
    premium_flag_col: Optional[str] = None
    premium_min_per_horizon: int = 1
    premium_max_per_horizon: int = 2
    premium_max_per_day: int = 1
    # Rice exclusions — see src.constants.RICE_EXCLUDE_ITEMS.
    rice_exclude_items: Set[str] = field(default_factory=lambda: set(RICE_EXCLUDE_ITEMS))
    # Cuisine theme settings
    cuisine_col: str = 'cuisine_family'
    cuisine_south_value: str = 'south_indian'
    cuisine_north_value: str = 'north_indian'
    # Flag column names for theme filtering
    f_chinese_rice: Optional[str] = 'is_chinese_fried_rice'
    f_chinese_nonveg: Optional[str] = 'is_chinese_chicken_gravy'
    f_chinese_veg_gravy: Optional[str] = 'is_chinese_veg_gravy'
    f_chinese_starter: Optional[str] = 'is_chinese_starter'
    f_nonveg_biryani: Optional[str] = 'is_nonveg_biryani'
    f_veg_biryani: Optional[str] = 'is_mixedveg_biryani'
    f_raita: Optional[str] = 'is_raita'
    # Theme preferences
    prefer_theme_starter: bool = True
    # Solver strategy
    cap_by_slot: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CAP_BY_SLOT))
    cap_default: int = DEFAULT_CAP
    cap_multipliers: Tuple[int, ...] = DEFAULT_CAP_MULTIPLIERS
    restarts_per_multiplier: int = DEFAULT_RESTARTS_PER_MULTIPLIER
    deterministic: bool = True
    # Per-client theme map (overrides global weekday_type)
    theme_map: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Cell — the core abstraction
# ---------------------------------------------------------------------------

class _Cell:
    """A single (day, slot) decision point with a pre-filtered candidate pool."""
    __slots__ = ('d_idx', 'date', 'slot_id', 'base_slot',
                 'cand_df', 'theme_pref_flags', 'x_vars', 'cand_rows')

    def __init__(self, d_idx: int, date: dt.date, slot_id: str,
                 base_slot: str, cand_df: pd.DataFrame,
                 theme_pref_flags: List[bool]):
        self.d_idx = d_idx
        self.date = date
        self.slot_id = slot_id
        self.base_slot = base_slot
        self.cand_df = cand_df
        self.theme_pref_flags = list(theme_pref_flags)
        self.x_vars: List[cp_model.IntVar] = []
        self.cand_rows: List[pd.Series] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _color_initial(x) -> str:
    c = _norm_color(x)
    if c == 'unknown':
        return ''
    base = c.split('_')[-1]
    return base[:1].upper() if base else ''


def _fmt_item_with_color(row: pd.Series, color_col: str) -> str:
    item = str(row.get('item', ''))
    ini = _color_initial(row.get(color_col, 'unknown'))
    return f'{item}({ini})' if ini else item


def _min_distinct_for_day(cfg: SolverConfig, day_type: str) -> int:
    if day_type == 'chinese':
        return cfg.min_distinct_colors_per_day_chinese
    if day_type == 'biryani':
        return cfg.min_distinct_colors_per_day_biryani
    return cfg.min_distinct_colors_per_day


def _find_cells(cells: List[_Cell], di: int, base_slot: str) -> List[_Cell]:
    """Linear-scan lookup — kept for tests / ad-hoc use. Production uses
    ``_make_find_cells`` which backs the lookup with a dict."""
    return [c for c in cells if c.d_idx == di and c.base_slot == base_slot]


def _make_find_cells(cells: List[_Cell]):
    """Build an O(1) ``(d_idx, base_slot) -> [cells]`` lookup as a closure.

    Preserves the ``(cells, di, base_slot)`` signature used by rule modules;
    the first argument is ignored because the index already closes over the
    cell list.
    """
    index: Dict[Tuple[int, str], List[_Cell]] = {}
    for c in cells:
        index.setdefault((c.d_idx, c.base_slot), []).append(c)

    def _find(_cells, di: int, base_slot: str) -> List[_Cell]:
        return index.get((di, base_slot), [])

    return _find


def _link_any(model: cp_model.CpModel, lits: List, y) -> None:
    if not lits:
        model.Add(y == 0)
        return
    model.Add(sum(lits) >= y)
    for lit in lits:
        model.Add(lit <= y)


def _sample_with_priority(pool: pd.DataFrame, cap: int,
                          priority_mask: pd.Series,
                          rng: random.Random) -> pd.DataFrame:
    if len(pool) <= cap:
        return pool
    pm = priority_mask.reindex(pool.index).fillna(False).astype(bool)
    pri, oth = pool[pm], pool[~pm]
    if len(pri) >= cap:
        return pri.sample(cap, random_state=rng.randint(1, 10**9))
    if len(pri) == 0:
        return pool.sample(cap, random_state=rng.randint(1, 10**9))
    need = cap - len(pri)
    if len(oth) > need:
        oth = oth.sample(need, random_state=rng.randint(1, 10**9))
    return pd.concat([pri, oth], axis=0)


def _sample_cell_candidates(pool: pd.DataFrame, pref_mask: pd.Series,
                            cap: int, rng: random.Random) -> Tuple[pd.DataFrame, List[bool]]:
    pref2 = pref_mask.reindex(pool.index).fillna(False).astype(bool)
    if len(pool) > cap:
        if bool(pref2.any()):
            pool = _sample_with_priority(pool, cap, pref2, rng)
        else:
            pool = pool.sample(cap, random_state=rng.randint(1, 10**9))
    pref2 = pref2.reindex(pool.index).fillna(False).astype(bool)
    return pool.reset_index(drop=True), pref2.tolist()


# ---------------------------------------------------------------------------
# MenuSolver
# ---------------------------------------------------------------------------

class MenuSolver:
    """
    Cell-based CP-SAT menu planner.

    Each (day, slot) cell has a pre-filtered candidate pool. The solver
    picks exactly one candidate per cell subject to hard constraints.
    """

    # Used by regenerator.py to derive regen caps
    CAP_BY_SLOT_BASE: Dict[str, int] = dict(DEFAULT_CAP_BY_SLOT)

    def __init__(
        self,
        pools: Dict[str, pd.DataFrame],
        solver_config: SolverConfig,
        menu_rules: Optional[List[BaseMenuRule]] = None,
        banned_by_date: Optional[Dict[dt.date, Set[str]]] = None,
        ricebread_ban_day: Optional[Dict[dt.date, bool]] = None,
        recent_sigs: Optional[Set[str]] = None,
        skip_cells: Optional[Set[Tuple[dt.date, str]]] = None,
    ):
        self.pools = pools
        self.cfg = solver_config
        self.menu_rules = menu_rules or []
        self.banned_by_date = banned_by_date or {}
        self.ricebread_ban_day = ricebread_ban_day or {}
        self.recent_sigs = recent_sigs or set()
        self.skip_cells = skip_cells or set()
        # Soft rules that threw during apply / get_objective_terms.
        # Scoped to the winning attempt only — cleared at the start of
        # each restart so callers (API, regenerator) don't see failures
        # from cells that were discarded. On total failure the list
        # reflects the last attempt's failures, which is the most
        # actionable for diagnostics.
        self.rule_failures: List[Dict[str, Any]] = []
        # Stamped onto each rule_failures entry so diagnostics can tell
        # which multi-restart attempt produced which failure.
        self._current_attempt_seed: Optional[int] = None

    def _record_rule_failure(self, rule, phase: str, exc: BaseException) -> None:
        """Log a soft-rule failure with traceback and remember it on self."""
        name = getattr(rule, 'name', type(rule).__name__)
        logger.warning(
            "Soft rule %r failed during %s: %s",
            name, phase, exc, exc_info=True,
        )
        entry: Dict[str, Any] = {
            'rule': name,
            'phase': phase,
            'error': f'{type(exc).__name__}: {exc}',
            'attempt_seed': self._current_attempt_seed,
        }
        # Dedupe inside a single attempt so the same rule failing on
        # every cell of this attempt surfaces once. Across attempts,
        # rule_failures is cleared in solve() so there's no cross-
        # attempt bleed.
        if entry not in self.rule_failures:
            self.rule_failures.append(entry)

    def solve(self, locked=None, forbidden=None, similarity=None) -> Tuple[Dict, List[dt.date]]:
        """
        Solve the menu plan with multi-restart strategy.

        Returns:
            (week_plan, dates) where week_plan maps date -> {slot_id: item_string}
        """
        self.rule_failures = []
        self._current_attempt_seed = None
        if self.cfg.explicit_dates:
            dates = list(self.cfg.explicit_dates)
        else:
            dates = [self.cfg.start_date + dt.timedelta(days=i) for i in range(self.cfg.days)]
        base_slots = self.cfg.active_base_slots or BASE_SLOT_NAMES
        expanded_slots = _expand_slots_in_order(
            base_slots, self.cfg.slot_counts or {s: 1 for s in base_slots}
        )

        cap_multipliers = self.cfg.cap_multipliers
        restarts_per_mult = self.cfg.restarts_per_multiplier
        base_seed = int(self.cfg.seed)
        total_time = float(self.cfg.time_limit_sec)
        per_attempt_time = max(20.0, total_time / (len(cap_multipliers) * restarts_per_mult))
        last_err = None
        # Per-attempt failure tally — used to pick a specific, actionable
        # final error message instead of the generic "likely causes" string.
        attempt_outcomes: Dict[str, int] = {
            'time_limit': 0, 'infeasible': 0, 'empty_pool': 0, 'other': 0,
        }
        orig_seed, orig_time = self.cfg.seed, self.cfg.time_limit_sec

        try:
            for mult in cap_multipliers:
                cap_default = self.cfg.cap_default * mult
                cap_by_slot = {k: v * mult for k, v in self.cfg.cap_by_slot.items()}

                for r in range(restarts_per_mult):
                    attempt_seed = base_seed + mult * DEFAULT_SEED_MULT_FACTOR + r * DEFAULT_SEED_RESTART_STEP
                    rng = random.Random(attempt_seed)
                    self.cfg.seed = attempt_seed
                    self.cfg.time_limit_sec = int(per_attempt_time)
                    # Reset per-attempt failure bucket and remember which
                    # attempt is running — callers that inspect
                    # rule_failures should only see the winning
                    # attempt's failures, not residue from attempts we
                    # abandoned with RuntimeError.
                    self._current_attempt_seed = attempt_seed
                    self.rule_failures = []

                    try:
                        cells = self._build_cells(
                            dates, expanded_slots, cap_default, cap_by_slot, rng
                        )
                        chosen_rows = self._solve_cpsat(
                            dates, cells, locked=locked, similarity=similarity,
                            forbidden=forbidden,
                        )
                        week_plan = self._rows_to_week_plan(
                            chosen_rows, dates, expanded_slots
                        )
                        return week_plan, dates
                    except RuntimeError as e:
                        last_err = e
                        msg = str(e).lower()
                        if 'time limit' in msg:
                            attempt_outcomes['time_limit'] += 1
                        elif 'infeasible' in msg:
                            attempt_outcomes['infeasible'] += 1
                        elif 'empty pool' in msg:
                            attempt_outcomes['empty_pool'] += 1
                        else:
                            attempt_outcomes['other'] += 1
                        continue

            total_attempts = sum(attempt_outcomes.values())
            raise RuntimeError(
                self._build_failure_message(attempt_outcomes, total_attempts, per_attempt_time)
            ) from last_err
        finally:
            self.cfg.seed, self.cfg.time_limit_sec = orig_seed, orig_time

    @staticmethod
    def _build_failure_message(
        outcomes: Dict[str, int], total: int, per_attempt_sec: float,
    ) -> str:
        """Pick the most actionable error message based on per-attempt failure mix.

        Aimed at non-technical users: lead with what went wrong in plain
        English, then give one or two concrete next steps they can try.
        """
        tl = outcomes.get('time_limit', 0)
        inf = outcomes.get('infeasible', 0)
        ep = outcomes.get('empty_pool', 0)

        if tl == total and total > 0:
            return (
                "Plan generation took too long to finish. "
                "This usually happens on longer plans (8+ days). "
                "Try generating a shorter plan (e.g. 5 days), or split your range "
                "into two smaller plans and generate them separately."
            )
        if inf == total and total > 0:
            return (
                "Can't build a valid menu with the current rules and items. "
                "There aren't enough unique items to satisfy every constraint "
                "(themes, item cooldown, colours, premium limits). "
                "Try one of: (1) generate a shorter plan, "
                "(2) add more items to the client's menu, "
                "or (3) lower the item cooldown so items can repeat sooner. "
                "Open the diagnostics panel to see which rule is the tightest fit."
            )
        if ep == total and total > 0:
            return (
                "A menu slot ran out of options after filtering "
                "(item cooldown, rice-bread gap, or theme filter removed everything). "
                "Open the diagnostics panel — it will name the exact slot and date. "
                "Fix: add more items for that slot, or shorten the cooldown."
            )
        # Mixed failure: report the breakdown so support can debug.
        parts = []
        if tl: parts.append(f"{tl} timed out")
        if inf: parts.append(f"{inf} found no valid menu")
        if ep: parts.append(f"{ep} ran out of candidates")
        if outcomes.get('other'): parts.append(f"{outcomes['other']} other")
        mix = ", ".join(parts) if parts else "unknown reasons"
        return (
            f"Plan generation failed across {total} attempts ({mix}). "
            "Try a shorter plan first — if that works, the longer plan likely "
            "needs more time or looser rules. Otherwise check the diagnostics panel."
        )

    # ----- Cell building -----

    def _build_cells(
        self, dates: List[dt.date], expanded_slots: List[str],
        cap_default: int, cap_by_slot: Dict[str, int], rng: random.Random,
    ) -> List[_Cell]:
        cells: List[_Cell] = []
        base_slots = list(dict.fromkeys(_base_slot(s) for s in expanded_slots))

        # Pre-build per (day_idx, base_slot) pool cache
        cache = self._build_day_base_pool_cache(dates, base_slots, expanded_slots)

        for di, d in enumerate(dates):
            for slot_id in expanded_slots:
                base = _base_slot(slot_id)
                if (d, base) in self.skip_cells:
                    continue
                pool2, pref_mask, day_type = cache[di, slot_id]

                if len(pool2) == 0:
                    extra = ''
                    if base == 'bread' and self.ricebread_ban_day.get(d, False):
                        extra = ' (rice-bread banned by gap rule)'
                    raise RuntimeError(
                        f'Empty pool after filters: {d.isoformat()} '
                        f'slot={slot_id} day_type={day_type}{extra}'
                    )

                cap = cap_by_slot.get(base, cap_default)
                sampled, theme_flags = _sample_cell_candidates(pool2, pref_mask, cap, rng)
                cells.append(_Cell(di, d, slot_id, base, sampled, theme_flags))

        return cells

    def _build_day_base_pool_cache(
        self, dates: List[dt.date], base_slots: List[str],
        expanded_slots: List[str],
    ) -> Dict:
        cache = {}

        # Build shared filter context for rule pre_filter_pool calls
        base_filter_ctx: Dict[str, Any] = {
            'cfg': self.cfg,
            'banned_by_date': self.banned_by_date,
            'ricebread_ban_day': self.ricebread_ban_day,
            'pools': self.pools,
        }

        for di, d in enumerate(dates):
            day_type = _weekday_type_cfg(d, self.cfg.theme_map)

            # First pass: build base-slot level pools (shared across slot numbers)
            base_pools: Dict[str, pd.DataFrame] = {}
            for base in base_slots:
                pool2 = self.pools[base].copy()

                # Exclude steamed rice etc. from flavor rice/healthy_rice
                if base in ('rice', 'healthy_rice') and len(pool2) > 0:
                    pool2 = pool2[~pool2['item'].isin(self.cfg.rice_exclude_items)]

                # Apply rule pre-filters (item cooldown, ricebread gap,
                # theme slot filters, etc.)
                filter_ctx = {**base_filter_ctx, 'slot_num': None}
                for rule in self.menu_rules:
                    pool2 = rule.pre_filter_pool(pool2, d, base, day_type, filter_ctx)

                base_pools[base] = pool2

            # Second pass: per expanded slot (handles slot_num for nonveg_dry etc.)
            for slot_id in expanded_slots:
                base = _base_slot(slot_id)
                slot_num = _slot_num(slot_id)
                pool2 = base_pools[base]

                # Apply slot-number-aware pre-filters (e.g. nonveg dry preference)
                if slot_num is not None and slot_num >= 2:
                    filter_ctx = {**base_filter_ctx, 'slot_num': slot_num}
                    for rule in self.menu_rules:
                        pool2 = rule.pre_filter_pool(pool2, d, base, day_type, filter_ctx)

                # Theme preference mask (for sampling priority + fallback penalty)
                pref_mask = self._compute_theme_pref_mask(pool2, base, day_type)

                cache[di, slot_id] = (pool2, pref_mask, day_type)
        return cache

    @staticmethod
    def _compute_theme_pref_mask(pool: pd.DataFrame, base_slot: str,
                                 day_type: str) -> pd.Series:
        """Mark items matching the day's theme as preferred.

        Only meaningful for THEME_FALLBACK_SLOTS (starter, veg_dry) where the
        pool is NOT hard-filtered by cuisine but we still want to prefer
        theme-matching items via sampling priority and fallback penalty.
        """
        if len(pool) == 0 or base_slot not in THEME_FALLBACK_SLOTS:
            return pd.Series(False, index=pool.index)

        if day_type == 'south' and 'cuisine_family' in pool.columns:
            return pool['cuisine_family'].map(_norm_str) == 'south_indian'
        if day_type == 'north' and 'cuisine_family' in pool.columns:
            return pool['cuisine_family'].map(_norm_str) == 'north_indian'
        if day_type == 'chinese':
            # Chinese starters have flag; veg_dry uses text heuristics
            if base_slot == 'starter' and 'is_chinese_starter' in pool.columns:
                return pool['is_chinese_starter'].map(_to_bool01) == 1
            # veg_dry: chinese side mask heuristic
            text = (pool['item'].astype(str) + ' ' +
                    pool.get('sub_category', pd.Series('', index=pool.index)).astype(str))
            text = text.str.lower()
            return (
                text.str.contains('chinese', na=False) |
                text.str.contains('manchurian', na=False) |
                text.str.contains('schezwan', na=False) |
                text.str.contains('szechuan', na=False) |
                text.str.contains('gobi.65', na=False) |
                text.str.contains('baby.corn', na=False) |
                text.str.contains('noodle', na=False) |
                text.str.contains('chilli', na=False)
            )
        # mix, biryani, holiday: no preference
        return pd.Series(False, index=pool.index)

    # ----- CP-SAT model -----

    def _solve_cpsat(
        self, dates: List[dt.date], cells: List[_Cell],
        locked=None, similarity=None, forbidden=None,
    ) -> Dict:
        rng = random.Random(self.cfg.seed)
        model = cp_model.CpModel()
        day_types = [_weekday_type_cfg(d, self.cfg.theme_map) for d in dates]

        known_colors, known_welcome_colors = self._collect_known_colors(cells)
        build_result = self._build_decision_variables(
            model, cells, day_types, locked=locked, forbidden=forbidden,
        )
        (item_to_vars, day_color_vars, day_rice_color_vars,
         day_gravy_color_vars, day_premium_vars, day_welcome_color_vars,
         monday_south_lits, monday_north_lits, theme_fallback_bools) = build_result

        context = self._build_context(
            cells, dates, day_types,
            item_to_vars, day_color_vars, day_rice_color_vars,
            day_gravy_color_vars, day_premium_vars, day_welcome_color_vars,
            monday_south_lits, monday_north_lits, theme_fallback_bools,
            known_colors, known_welcome_colors,
        )

        # Built-in color constraints (uniqueness is handled by UniqueItemsMenuRule)
        self._add_color_constraints(model, dates, day_types, known_colors,
                                    day_color_vars, day_rice_color_vars,
                                    day_gravy_color_vars)

        self._apply_rules_and_objective(model, cells, rng, similarity, context)

        solver = self._configure_and_solve(model)
        return self._extract_solution_rows(solver, cells, dates)

    def _build_context(
        self, cells, dates, day_types,
        item_to_vars, day_color_vars, day_rice_color_vars,
        day_gravy_color_vars, day_premium_vars, day_welcome_color_vars,
        monday_south_lits, monday_north_lits, theme_fallback_bools,
        known_colors, known_welcome_colors,
    ) -> SolverContext:
        """Assemble the rule-facing context.

        Returns a plain ``dict`` typed as :class:`SolverContext`
        (a ``TypedDict``), so rules keep using ``.get()`` access while
        the solver↔rule contract stays statically checkable.
        """
        return {
            'cells': cells,
            'dates': dates,
            'day_types': day_types,
            'item_to_vars': item_to_vars,
            'day_color_vars': day_color_vars,
            'day_rice_color_vars': day_rice_color_vars,
            'day_gravy_color_vars': day_gravy_color_vars,
            'day_premium_vars': day_premium_vars,
            'day_welcome_color_vars': day_welcome_color_vars,
            'monday_south_lits': monday_south_lits,
            'monday_north_lits': monday_north_lits,
            'theme_fallback_bools': theme_fallback_bools,
            'known_colors': known_colors,
            'known_welcome_colors': known_welcome_colors,
            'cfg': self.cfg,
            'recent_sigs': self.recent_sigs,
            'find_cells_fn': _make_find_cells(cells),
            'link_any_fn': _link_any,
        }

    def _apply_rules_and_objective(self, model, cells, rng, similarity, context) -> None:
        """Run every rule's ``apply`` then assemble the objective.

        Hard rules (default severity) that raise cause the solve to fail
        rather than silently drop their constraint; soft rules only warn.
        """
        for rule in self.menu_rules:
            try:
                rule.apply(model, {}, None, context)
            except Exception as e:  # noqa: BLE001 — severity decides what happens
                severity = getattr(rule, 'severity', MenuRuleSeverity.HARD)
                if severity == MenuRuleSeverity.HARD:
                    raise RuntimeError(
                        f"Hard menu rule '{rule.name}' failed: {type(e).__name__}: {e}"
                    ) from e
                # Soft rule. Record every exception type — previously only
                # ValueError/KeyError/AttributeError were caught, so a
                # buggy soft rule raising TypeError or RuntimeError would
                # crash the whole solve and leak details via the 500,
                # defeating the "soft rules never block" contract.
                self._record_rule_failure(rule, 'apply', e)
        self._build_objective(model, cells, rng, similarity, context)

    def _configure_and_solve(self, model) -> cp_model.CpSolver:
        """Set CP-SAT parameters, solve, and translate infeasibility into a
        RuntimeError. Returns the solver so callers can read variable values.
        """
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(self.cfg.time_limit_sec)
        solver.parameters.random_seed = int(self.cfg.seed)
        if self.cfg.deterministic:
            solver.parameters.num_search_workers = 1
        else:
            try:
                from api.concurrency import get_worker_count
                solver.parameters.num_search_workers = get_worker_count()
            except ImportError:
                solver.parameters.num_search_workers = 8
        solver.parameters.cp_model_presolve = True

        status = solver.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return solver
        if status == cp_model.INFEASIBLE:
            raise RuntimeError('No feasible plan found (INFEASIBLE).')
        if status == cp_model.UNKNOWN:
            raise RuntimeError('No feasible plan found (TIME LIMIT).')
        if status == cp_model.MODEL_INVALID:
            raise RuntimeError('CP-SAT model invalid.')
        raise RuntimeError(f'CP-SAT failed with status={status}.')

    def _collect_known_colors(self, cells: List[_Cell]) -> Tuple[List[str], List[str]]:
        known_colors: Set[str] = set()
        known_welcome: Set[str] = set()
        for cell in cells:
            if cell.base_slot in self.cfg.color_slots:
                for c in cell.cand_df[self.cfg.color_col].tolist():
                    col = _norm_color(c)
                    if col != 'unknown':
                        known_colors.add(col)
            if cell.base_slot == 'welcome_drink':
                for c in cell.cand_df[self.cfg.color_col].tolist():
                    col = _norm_color(c)
                    if col != 'unknown':
                        known_welcome.add(col)
        return sorted(known_colors), sorted(known_welcome)

    def _build_decision_variables(
        self, model: cp_model.CpModel, cells: List[_Cell],
        day_types: List[str], locked=None, forbidden=None,
    ):
        item_to_vars: Dict[str, List] = {}
        day_color_vars: Dict[Tuple, List] = {}
        day_rice_color_vars: Dict[Tuple, List] = {}
        day_gravy_color_vars: Dict[Tuple, List] = {}
        day_premium_vars: Dict[int, List] = {}
        day_welcome_color_vars: Dict[Tuple, List] = {}
        monday_south_lits: List = []
        monday_north_lits: List = []
        theme_fallback_bools: List = []

        for cell in cells:
            di = cell.d_idx
            slot_id = cell.slot_id
            base = cell.base_slot
            x_vars: List = []
            cand_rows: List = []

            for j in range(len(cell.cand_df)):
                row = cell.cand_df.iloc[j]
                item_base = _norm_str(row.get('item', ''))
                var = model.NewBoolVar(f'x_d{di}_{slot_id}_{j}')
                x_vars.append(var)
                cand_rows.append(row)

                item_to_vars.setdefault(item_base, []).append(var)

                # Premium tracking
                if self.cfg.premium_flag_col and int(row.get(self.cfg.premium_flag_col, 0)) == 1:
                    day_premium_vars.setdefault(di, []).append(var)

                # Color tracking
                if base in self.cfg.color_slots:
                    col = _norm_color(row.get(self.cfg.color_col, 'unknown'))
                    if col != 'unknown':
                        day_color_vars.setdefault((di, col), []).append(var)
                        if base == 'rice':
                            day_rice_color_vars.setdefault((di, col), []).append(var)
                        elif base == 'veg_gravy':
                            day_gravy_color_vars.setdefault((di, col), []).append(var)

                if base == 'welcome_drink':
                    col = _norm_color(row.get(self.cfg.color_col, 'unknown'))
                    if col != 'unknown':
                        day_welcome_color_vars.setdefault((di, col), []).append(var)

                # Monday mix tracking
                if day_types[di] == 'mix' and base not in EXEMPT_FROM_CUISINE:
                    cf = _norm_str(row.get(self.cfg.cuisine_col, ''))
                    if cf == self.cfg.cuisine_south_value:
                        monday_south_lits.append(var)
                    elif cf == self.cfg.cuisine_north_value:
                        monday_north_lits.append(var)

                # Locked/forbidden
                if locked and (cell.date, slot_id) in locked:
                    if item_base != _norm_str(locked[cell.date, slot_id]):
                        model.Add(var == 0)
                if forbidden and (cell.date, slot_id) in forbidden:
                    if item_base in forbidden[cell.date, slot_id]:
                        model.Add(var == 0)

            # Exactly one candidate per cell
            model.Add(sum(x_vars) == 1)
            cell.x_vars = x_vars
            cell.cand_rows = cand_rows

            # Theme fallback tracking
            if cell.base_slot in THEME_FALLBACK_SLOTS:
                pref_flags = [bool(v) for v in cell.theme_pref_flags]
                if pref_flags and any(pref_flags) and not all(pref_flags):
                    fallback_lits = [v for v, pf in zip(x_vars, pref_flags) if not pf]
                    if fallback_lits:
                        fb = model.NewBoolVar(f'theme_fallback_{di}_{slot_id}')
                        _link_any(model, fallback_lits, fb)
                        theme_fallback_bools.append(fb)

        return (item_to_vars, day_color_vars, day_rice_color_vars,
                day_gravy_color_vars, day_premium_vars, day_welcome_color_vars,
                monday_south_lits, monday_north_lits, theme_fallback_bools)

    # ----- Built-in constraints -----

    def _add_color_constraints(self, model, dates, day_types, known_colors,
                               day_color_vars, day_rice_color_vars,
                               day_gravy_color_vars):
        cfg = self.cfg
        for di, _ in enumerate(dates):
            day_type = day_types[di]
            min_dist = _min_distinct_for_day(cfg, day_type)

            for col in known_colors:
                lits = day_color_vars.get((di, col), [])
                if lits:
                    model.Add(sum(lits) <= cfg.max_same_color_per_day)

            y_vars = []
            for col in known_colors:
                lits = day_color_vars.get((di, col), [])
                if not lits:
                    continue
                y = model.NewBoolVar(f'y_color_{di}_{col}')
                _link_any(model, lits, y)
                y_vars.append(y)
            if y_vars:
                model.Add(sum(y_vars) >= min_dist)

            if not (cfg.ignore_rice_gravy_color_diff_on_chinese_day and day_type == 'chinese'):
                for col in known_colors:
                    r_lits = day_rice_color_vars.get((di, col), [])
                    g_lits = day_gravy_color_vars.get((di, col), [])
                    if r_lits and g_lits:
                        model.Add(sum(r_lits) + sum(g_lits) <= 1)

    # ----- Objective -----

    def _build_objective(self, model, cells, rng, similarity, context):
        obj_terms = []

        if similarity:
            for cell in cells:
                for var, row in zip(cell.x_vars, cell.cand_rows):
                    sc = int(similarity.get(
                        (cell.date, cell.slot_id, _norm_str(row.get('item', ''))), 0
                    ))
                    if sc:
                        obj_terms.append(var * sc)
            for cell in cells:
                for var in cell.x_vars:
                    obj_terms.append(var * rng.randint(0, 3))
        else:
            for cell in cells:
                for var in cell.x_vars:
                    obj_terms.append(var * rng.randint(0, 1000))

        # Collect objective terms from rules. These are always treated as
        # soft — get_objective_terms() only shapes the objective, so a
        # failing rule just means "that preference doesn't apply this
        # solve". Catch Exception (not a narrow tuple) so a buggy rule
        # raising TypeError / RuntimeError / anything else is recorded
        # rather than crashing the solve.
        for rule in self.menu_rules:
            try:
                terms = rule.get_objective_terms(model, context)
                obj_terms.extend(terms)
            except Exception as e:  # noqa: BLE001 — recorded, not swallowed silently
                self._record_rule_failure(rule, 'get_objective_terms', e)

        if obj_terms:
            model.Maximize(sum(obj_terms))

    # ----- Solution extraction -----

    def _extract_solution_rows(self, solver, cells, dates):
        chosen = {d: {} for d in dates}
        for cell in cells:
            pick_idx = next(
                (j for j, var in enumerate(cell.x_vars) if solver.Value(var) == 1),
                None,
            )
            if pick_idx is None:
                raise RuntimeError('Solver solution missing selection in a cell.')
            chosen[cell.date][cell.slot_id] = cell.cand_rows[pick_idx]
        return chosen

    def _rows_to_week_plan(self, chosen_rows, dates, expanded_slots):
        week_plan = {}
        for d in dates:
            day_out = {}
            for slot_id in expanded_slots:
                if slot_id in chosen_rows[d]:
                    day_out[slot_id] = _fmt_item_with_color(
                        chosen_rows[d][slot_id], self.cfg.color_col
                    )
            day_out.update(CONSTANT_ITEMS)
            week_plan[d] = day_out
        return week_plan
