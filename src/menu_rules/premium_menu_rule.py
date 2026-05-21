"""
Premium menu rule: max 1 premium item per day, scaled premium days per horizon.

``min_per_horizon`` and ``max_per_horizon`` in the config are treated as
the rate per 5-day baseline week. For plans longer than 5 days the limits
scale proportionally (10-day plan → 2× the baseline). This keeps the
"premium items are sparse but present" intent constant regardless of
plan length, instead of an absolute ceiling that gets stricter as plans
grow.
"""

import math
from typing import Dict, Any, List
from ortools.sat.python import cp_model
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)
from src.constants import BASE_SLOT_NAMES


# Baseline week length the per-horizon limits in the rule config are tuned for.
_BASELINE_DAYS = 5


class PremiumMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "premium",
        "name": "premium_limits",
        "max_per_day": 1,
        "min_per_horizon": 1,
        "max_per_horizon": 2
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.PREMIUM
        self.max_per_day = rule_config.get('max_per_day', 1)
        self.min_per_horizon = rule_config.get('min_per_horizon', 1)
        self.max_per_horizon = rule_config.get('max_per_horizon', 2)

    def validate_config(self) -> bool:
        return not self._collect_errors()

    def validation_errors(self) -> List[str]:
        return self._collect_errors()

    def _collect_errors(self) -> List[str]:
        errs: List[str] = []
        if self.max_per_day < 0:
            errs.append(f"max_per_day must be >= 0 (got {self.max_per_day})")
        if self.min_per_horizon < 0:
            errs.append(
                f"min_per_horizon must be >= 0 (got {self.min_per_horizon})"
            )
        if self.max_per_horizon < 0:
            errs.append(
                f"max_per_horizon must be >= 0 (got {self.max_per_horizon})"
            )
        if self.min_per_horizon > self.max_per_horizon:
            errs.append(
                f"min_per_horizon ({self.min_per_horizon}) must be <= "
                f"max_per_horizon ({self.max_per_horizon})"
            )
        return errs

    def effective_limits(self, num_days: int) -> tuple[int, int]:
        """Return (min, max) premium days for a horizon of ``num_days``.

        Scales the configured per-5-day rates linearly; plans of 5 days
        or fewer keep the configured baseline so existing weekly behavior
        is unchanged.
        """
        if num_days <= _BASELINE_DAYS:
            return self.min_per_horizon, self.max_per_horizon
        scale = num_days / _BASELINE_DAYS
        eff_min = math.ceil(self.min_per_horizon * scale)
        eff_max = math.ceil(self.max_per_horizon * scale)
        # Defensive: never let scaling invert the range.
        if eff_min > eff_max:
            eff_min = eff_max
        return eff_min, eff_max

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cfg = context.get('cfg')
        dates = context.get('dates', [])
        day_premium_vars = context.get('day_premium_vars', {})

        if not cfg or not cfg.premium_flag_col:
            return

        eff_min, eff_max = self.effective_limits(len(dates))

        premium_day_bools = []
        for di in range(len(dates)):
            lits = day_premium_vars.get(di, [])
            prem_day = model.NewBoolVar(f'premium_day_{di}')
            if lits:
                model.Add(sum(lits) <= self.max_per_day)
                model.Add(sum(lits) == prem_day)
            else:
                model.Add(prem_day == 0)
            premium_day_bools.append(prem_day)

        total = sum(premium_day_bools)
        has_any = any(len(day_premium_vars.get(di, [])) > 0 for di in range(len(dates)))
        if has_any:
            model.Add(total >= eff_min)
            model.Add(total <= eff_max)
        else:
            model.Add(total == 0)

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Premium constraint requires a number of premium-flagged days
        in the horizon. Diagnose:

          - ERROR when min_per_horizon > 0 but the configured flag
            column is missing OR no items in any active slot pool have
            the flag set. The solver would silently relax in apply()
            (``if not cfg.premium_flag_col: return``), but the user's
            intent ("I want at least N premium days") is lost — surface it.
          - WARNING when premium_count < min_per_horizon (CP-SAT
            cannot promote enough premium days; multi-restart will
            eventually fail).

        Effective limits are scaled by horizon length so a 10-day plan
        gets min=2, max=4 instead of the configured per-5-day baseline.
        """
        diags: List[Diagnostic] = []
        eff_min, eff_max = self.effective_limits(len(ctx.dates))
        flag_col = ctx.cfg.premium_flag_col if ctx.cfg else None
        if not flag_col:
            if eff_min > 0:
                diags.append(Diagnostic(
                    rule=self.name, rule_type=self.rule_type.value,
                    severity=DiagnosticSeverity.ERROR,
                    phase=DiagnosticPhase.APPLY,
                    message=(
                        f"Premium rule requires ≥{eff_min} "
                        f"premium day(s) but no premium_flag_col is "
                        f"configured. The constraint will silently drop."
                    ),
                    suggestion=(
                        "Set SolverConfig.premium_flag_col to the column "
                        "name (e.g. 'is_premium_veg'), or set "
                        "min_per_horizon=0 in the rule config."
                    ),
                    affected={'min_per_horizon': eff_min},
                ))
            return diags

        base_slots = ctx.active_base_slots or list(BASE_SLOT_NAMES)
        per_day_counts: Dict[str, int] = {}
        total_premium = 0
        for d in ctx.dates:
            day_count = 0
            for base in base_slots:
                if (d, base) in ctx.skip_cells:
                    continue
                pool = ctx.pools.get(base)
                if pool is None or len(pool) == 0:
                    continue
                if flag_col not in pool.columns:
                    continue
                day_count += int(pool[flag_col].fillna(0).astype(int).eq(1).sum())
            per_day_counts[d.isoformat()] = day_count
            total_premium += day_count

        if eff_min > 0 and total_premium == 0:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.ERROR,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"Premium rule requires ≥{eff_min} "
                    f"premium day(s) for a {len(ctx.dates)}-day plan, "
                    f"but no items in any slot pool have {flag_col}=1."
                ),
                suggestion=(
                    f"Add at least {eff_min} premium item"
                    f"{'s' if eff_min != 1 else ''} to the "
                    f"ontology (set {flag_col}=1)."
                ),
                affected={
                    'min_per_horizon': eff_min,
                    'max_per_horizon': eff_max,
                    'flag_col': flag_col,
                    'total_premium': 0,
                },
            ))
        elif total_premium and eff_min > 0:
            # Count days that *could* be premium (at least one premium
            # item in a non-skipped slot). The constraint is per-day,
            # so this is the right metric — not the raw item count.
            premium_capable_days = sum(
                1 for c in per_day_counts.values() if c > 0
            )
            if premium_capable_days < eff_min:
                diags.append(Diagnostic(
                    rule=self.name, rule_type=self.rule_type.value,
                    severity=DiagnosticSeverity.ERROR,
                    phase=DiagnosticPhase.APPLY,
                    message=(
                        f"Premium rule needs ≥{eff_min} "
                        f"premium day(s) for a {len(ctx.dates)}-day plan, "
                        f"but only {premium_capable_days} of "
                        f"{len(ctx.dates)} dates can carry a premium "
                        f"item (the others have 0 items with "
                        f"{flag_col}=1 in any slot pool)."
                    ),
                    suggestion=(
                        "Add premium items that match each day's theme "
                        "filters, or relax min_per_horizon."
                    ),
                    affected={
                        'min_per_horizon': eff_min,
                        'max_per_horizon': eff_max,
                        'premium_capable_days': premium_capable_days,
                        'total_dates': len(ctx.dates),
                    },
                ))
        return diags
