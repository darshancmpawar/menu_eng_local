"""
Typed schema for the rule-facing solver context.

Rules consume the context as a regular ``dict`` (so existing
``context.get('cells', [])`` style access keeps working), but
:func:`MenuSolver._build_context` annotates the return type with
``SolverContext`` so callers, IDEs, and ``mypy`` see exactly which
fields are populated.

This used to be a ``@dataclass`` that was constructed and then
immediately flattened via ``.as_dict()`` for the rule layer. The
flattening was the only thing every caller actually wanted, so the
dataclass + shim is replaced with a ``TypedDict`` — same type
information, no allocation round-trip, no dual representation to keep
in sync.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, Set, Tuple, TYPE_CHECKING, TypedDict

from ortools.sat.python import cp_model

if TYPE_CHECKING:
    from .menu_solver import SolverConfig, _Cell


class SolverContext(TypedDict):
    """Bundle passed to ``rule.apply()`` and ``rule.get_objective_terms()``."""

    cells: List["_Cell"]
    dates: List[dt.date]
    day_types: List[str]
    item_to_vars: Dict[str, List[cp_model.IntVar]]
    day_color_vars: Dict[Tuple[int, str], List[cp_model.IntVar]]
    day_rice_color_vars: Dict[Tuple[int, str], List[cp_model.IntVar]]
    day_gravy_color_vars: Dict[Tuple[int, str], List[cp_model.IntVar]]
    day_premium_vars: Dict[int, List[cp_model.IntVar]]
    day_welcome_color_vars: Dict[Tuple[int, str], List[cp_model.IntVar]]
    monday_south_lits: List[cp_model.IntVar]
    monday_north_lits: List[cp_model.IntVar]
    theme_fallback_bools: List[cp_model.IntVar]
    known_colors: List[str]
    known_welcome_colors: List[str]
    cfg: "SolverConfig"
    recent_sigs: Set[str]
    find_cells_fn: Callable
    link_any_fn: Callable
