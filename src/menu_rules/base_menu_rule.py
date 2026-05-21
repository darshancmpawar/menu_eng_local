"""
Base menu rule class for all rule types.

Rules participate in two phases:
1. **Pre-filter phase** — ``pre_filter_pool()`` is called during candidate pool
   building (before CP-SAT variables exist).  Rules that need to remove items
   from a slot's candidate pool override this method.
2. **CP-SAT phase** — ``apply()`` adds hard constraints and
   ``get_objective_terms()`` contributes soft-constraint terms to the objective.

Rules can also implement an optional **pre-flight diagnostic phase** by
overriding ``diagnose()``. Diagnostics run before the solver and explain
*why* a plan is going to be infeasible (e.g. "item cooldown banned all 8
chinese starter candidates on 2026-05-13"). The aggregator in
``src/menu_rules/diagnostics.py`` wraps each rule's diagnose() in a
try/except so a buggy rule never blocks the planner.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional, Set, Tuple, TYPE_CHECKING

import datetime as dt
import pandas as pd
from ortools.sat.python import cp_model


if TYPE_CHECKING:  # avoid runtime import cycle solver→rules
    from src.solver.menu_solver import SolverConfig


class MenuRuleType(Enum):
    """Types of menu rules supported."""
    # Original MVP rules
    CUISINE = "cuisine"
    COLOR_PAIRING = "color_pairing"
    COLOR_VARIETY = "color_variety"
    UNIQUE_ITEMS = "unique_items"
    # Theme system
    THEME_DAY = "theme_day"
    # Hard constraints
    COUPLING = "coupling"
    CURD_SIDE = "curd_side"
    PREMIUM = "premium"
    WELCOME_DRINK_COLOR = "welcome_drink_color"
    # Cooldown / pre-filter rules
    ITEM_COOLDOWN = "item_cooldown"
    RICEBREAD_GAP = "ricebread_gap"
    WEEK_SIGNATURE_COOLDOWN = "week_signature_cooldown"
    THEME_SLOT_FILTER = "theme_slot_filter"
    NONVEG_DRY_PREFERENCE = "nonveg_dry_preference"
    NONVEG_BIRYANI_WEEKLY = "nonveg_biryani_weekly"
    # Soft constraints
    THEME_STARTER_PREFERENCE = "theme_starter_preference"
    THEME_FALLBACK_PENALTY = "theme_fallback_penalty"
    # Per-client custom rules
    INGREDIENT_BAN = "ingredient_ban"
    ITEM_FREQUENCY = "item_frequency"
    SLOT_DAY_RESTRICTION = "slot_day_restriction"
    # Synthetic — produced by pool_size_diagnostics(), not by a real
    # rule class. Folds the pre-flight pool-size warnings into the
    # same response surface as everything else so the UI only renders
    # one list. Severity is always warning|info, never error.
    POOL_SIZE = "pool_size"


class MenuRuleSeverity(Enum):
    """How the solver treats a failure in ``apply()``.

    HARD: an exception from ``apply()`` means a constraint silently dropped,
    which produces an invalid plan. The solver surfaces the error and fails
    the request.

    SOFT: the rule only expresses a preference (bonus/penalty via the
    objective, or an optional cooldown). A failure logs a warning and the
    solver keeps going.
    """
    HARD = "hard"
    SOFT = "soft"


# ---------------------------------------------------------------------------
# Pre-flight diagnostic data model
# ---------------------------------------------------------------------------

class DiagnosticSeverity(str, Enum):
    """How serious a pre-flight diagnostic is.

    Inherits from ``str`` so ``json.dumps(Diagnostic.to_dict())`` works
    without a custom encoder — the value is the bare string already.
    """
    # WILL block solver — /plan returns 422 short-circuit
    ERROR = "error"
    # Tight enough to flag, but solver still runs
    WARNING = "warning"
    # Notable but expected (e.g. cooldown banned 12/30, pool still healthy)
    INFO = "info"


class DiagnosticPhase(str, Enum):
    """Which solver phase a diagnostic relates to."""
    PRE_FILTER = "pre_filter"
    APPLY = "apply"
    OBJECTIVE = "objective"


@dataclass(frozen=True)
class Diagnostic:
    """A single pre-flight finding about why the solver may fail.

    Diagnostics are emitted by ``BaseMenuRule.diagnose()`` overrides and
    by ``pool_size_diagnostics()``. The aggregator in
    ``src/menu_rules/diagnostics.py`` collects them, sorts by severity
    then rule name, and the API attaches them to /plan and /diagnose
    responses.
    """
    rule: str                           # rule.name — instance label
    rule_type: str                      # MenuRuleType.value — stable id for tests/UI
    severity: DiagnosticSeverity
    phase: DiagnosticPhase
    message: str                        # what's wrong (one sentence)
    suggestion: str                     # how to fix (one sentence)
    # Loose dict on purpose — different rules surface different shapes
    # of "where did this go wrong" data (date, slot, item count, day_type, …).
    # Typing each variant adds maintenance cost for ~10 rule classes
    # without making the UI any safer.
    affected: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe projection. Enums round-trip as plain strings
        thanks to the ``str`` mixin on DiagnosticSeverity/Phase."""
        return {
            "rule": self.rule,
            "rule_type": self.rule_type,
            "severity": self.severity.value,
            "phase": self.phase.value,
            "message": self.message,
            "suggestion": self.suggestion,
            "affected": dict(self.affected),
        }


@dataclass
class DiagnoseContext:
    """All inputs a rule's ``diagnose()`` method might need.

    Passed as the only non-``self`` arg so future fields don't churn 10
    rule signatures. Built by ``api.app._build_diagnose_context`` from
    the same ``SolverInputs`` bundle the solver consumes.
    """
    pools: Dict[str, pd.DataFrame]                       # base_slot -> DataFrame
    dates: List[dt.date]                                 # weekday dates being planned
    day_types: Dict[dt.date, str]                        # 'mix'|'chinese'|'biryani'|'south'|'north'
    cfg: "SolverConfig"                                  # cuisine_col, f_chinese_*, premium_flag_col, …
    df: pd.DataFrame                                     # full ontology
    banned_by_date: Dict[dt.date, Set[str]]              # from HistoryManager
    ricebread_ban_day: Dict[dt.date, bool]               # from HistoryManager
    skip_cells: Set[Tuple[dt.date, str]]                 # from per-client slot_day_restriction
    client_cfg: Any                                      # ClientConfig dataclass
    active_base_slots: Optional[List[str]] = None        # client's configured slots (excl. const)


class BaseMenuRule(ABC):
    """
    Abstract base class for all menu rules.
    All rule types must inherit from this class.
    """

    # Default: failures in apply() are surfaced (plan would be invalid
    # without the constraint). Soft/bonus rules override this to SOFT.
    severity: MenuRuleSeverity = MenuRuleSeverity.HARD

    def __init__(self, rule_config: Dict[str, Any]):
        self.config = rule_config
        self.rule_type = None
        self.enabled = True
        self.name = rule_config.get('name', 'unnamed_rule')
        self.priority = rule_config.get('priority', 1)

    @abstractmethod
    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        """
        Apply the menu rule to the CP-SAT model.

        Args:
            model: OR-Tools CP-SAT model
            variables: Dictionary of decision variables
            menu_data: Menu data (DataFrame or dict)
            context: Additional context including 'cells', 'day_types', etc.
        """
        pass

    def validate_config(self) -> bool:
        """Validate the menu rule configuration.

        Default: accept any config. Override in subclasses that have real
        validation (e.g. required fields, value ranges, enum membership).
        Rules with no config surface beyond the base keys should *not*
        override this method — that's just noise.

        Subclasses that override this to reject invalid configs should
        also populate :py:meth:`validation_errors` so the loader can log
        why the rule was dropped instead of a generic "invalid".
        """
        return True

    def validation_errors(self) -> List[str]:
        """Return human-readable reasons ``validate_config()`` returned False.

        Default: empty. Override in subclasses so the loader logs
        something better than "invalid rule config: <name>".
        """
        return []

    def pre_filter_pool(self, pool: pd.DataFrame, date: dt.date,
                        base_slot: str, day_type: str,
                        filter_context: Dict[str, Any]) -> pd.DataFrame:
        """Filter candidate pool before cell building.

        Called once per (date, base_slot) during pool construction.
        Override in subclasses that need to remove items from candidate pools.

        Args:
            pool: DataFrame of candidate items for this slot.
            date: The planning date.
            base_slot: Base slot name (e.g. 'rice', 'starter').
            day_type: Theme type ('mix', 'chinese', 'biryani', 'south', 'north', …).
            filter_context: Runtime data including 'cfg', 'banned_by_date',
                            'ricebread_ban_day', 'pools' (full unfiltered pools).

        Returns:
            Filtered DataFrame (may be the same object if no filtering needed).
        """
        return pool

    def get_objective_terms(self, model: cp_model.CpModel,
                           context: Dict[str, Any]) -> List:
        """
        Return objective function terms contributed by this rule.

        Override in subclasses for soft constraints.
        Default: returns empty list (no contribution to objective).
        """
        return []

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Pre-flight self-check. Default: no diagnostics.

        Override in subclasses to inspect pools / dates / history /
        config and report what would block (or stress) the solver
        BEFORE it runs. Return any combination of error / warning / info
        Diagnostics; the aggregator sorts and dedupes them.

        Implementations must be cheap (no CP-SAT, no Supabase round
        trips — operate on the inputs in ``ctx``). The aggregator
        catches exceptions raised here and converts each to a single
        ``WARNING``-severity Diagnostic, so a bug in one rule's
        diagnose() never freezes the planner.
        """
        return []

    def get_description(self) -> str:
        return f"{self.rule_type.value}: {self.name}"

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name='{self.name}', enabled={self.enabled})>"
