"""
Pre-flight diagnostic aggregator.

Runs every rule's :py:meth:`BaseMenuRule.diagnose` against a
:class:`DiagnoseContext`, collects results, sorts them by severity,
and computes a summary the API surfaces back to the caller.

Also produces the synthetic ``pool_size`` diagnostics that previously
came out of ``api.app._validate_pools`` — folding both surfaces into
one list so the UI renders a single, structured "Diagnostics" expander
instead of two parallel lists.

The aggregator wraps each rule's diagnose() in a try/except. A raising
rule yields exactly one ``WARNING``-severity Diagnostic explaining that
diagnose() crashed — never an ``ERROR``, because an error would trigger
the pre-flight gate and freeze the planner over a bug in diagnostic
code. The traceback is logged server-side so it isn't lost.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from src.constants import BASE_SLOT_NAMES
from src.menu_rules.base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)

logger = logging.getLogger(__name__)


# Sort order: errors first (caller-actionable), then warnings, then info.
_SEVERITY_ORDER = {
    DiagnosticSeverity.ERROR.value: 0,
    DiagnosticSeverity.WARNING.value: 1,
    DiagnosticSeverity.INFO.value: 2,
}


def run_diagnostics(
    rules: Iterable[BaseMenuRule], ctx: DiagnoseContext,
) -> List[Diagnostic]:
    """Run every rule's diagnose() + the synthetic pool_size pass.

    Returns a list sorted by (severity, rule_type, rule). A buggy
    rule's exception is converted to a single WARNING-severity
    Diagnostic — never an ERROR — so a regression in one rule's
    diagnose() can't trigger the pre-flight gate and freeze the
    planner.
    """
    diagnostics: List[Diagnostic] = []

    # Per-rule pass.
    for rule in rules:
        try:
            emitted = rule.diagnose(ctx) or []
        except Exception as exc:  # noqa: BLE001 — intentional: one bad rule must not block
            logger.warning(
                "Rule %r diagnose() raised: %s",
                getattr(rule, 'name', type(rule).__name__),
                exc,
                exc_info=True,
            )
            rule_type = (
                rule.rule_type.value
                if getattr(rule, 'rule_type', None) is not None
                else 'unknown'
            )
            diagnostics.append(Diagnostic(
                rule=getattr(rule, 'name', type(rule).__name__),
                rule_type=rule_type,
                # Important: WARNING, never ERROR. A diagnose() bug must
                # not be self-promoting through the pre-flight gate.
                severity=DiagnosticSeverity.WARNING,
                phase=DiagnosticPhase.PRE_FILTER,
                message=(
                    f"Diagnostic check for rule '{getattr(rule, 'name', '?')}' "
                    f"crashed: {type(exc).__name__}. The rule may or may not "
                    f"fail at solve time; this is a bug in diagnose()."
                ),
                suggestion=(
                    "Server logs carry the traceback (grep by request_id). "
                    "File a fix for the rule's diagnose() method; the planner "
                    "is unaffected."
                ),
                affected={'exception': type(exc).__name__},
            ))
            continue
        diagnostics.extend(emitted)

    # Synthetic pool-size pass. This replaces the old _validate_pools
    # string list with structured Diagnostics so the UI renders one
    # surface, not two.
    diagnostics.extend(pool_size_diagnostics(rules, ctx))

    diagnostics.sort(key=lambda d: (
        _SEVERITY_ORDER.get(d.severity.value, 99),
        d.rule_type,
        d.rule,
    ))
    return diagnostics


def summarize(diagnostics: Iterable[Diagnostic]) -> Dict[str, Any]:
    """Counts per severity + a boolean ``would_succeed`` flag.

    ``would_succeed`` is False iff at least one ERROR is present —
    matches the pre-flight-gate behaviour exactly, so the UI can show
    "this will fail" without re-implementing the predicate.
    """
    errors = warnings = infos = 0
    for d in diagnostics:
        if d.severity == DiagnosticSeverity.ERROR:
            errors += 1
        elif d.severity == DiagnosticSeverity.WARNING:
            warnings += 1
        elif d.severity == DiagnosticSeverity.INFO:
            infos += 1
    return {
        'errors': errors,
        'warnings': warnings,
        'infos': infos,
        'would_succeed': errors == 0,
    }


def has_blocking_errors(diagnostics: Iterable[Diagnostic]) -> bool:
    """True iff at least one diagnostic is severity=ERROR.

    Drives the /plan pre-flight gate that short-circuits to 422.
    """
    return any(d.severity == DiagnosticSeverity.ERROR for d in diagnostics)


def pool_warnings_projection(diagnostics: Iterable[Diagnostic]) -> List[str]:
    """Back-compat: project POOL_SIZE Diagnostics to the old
    ``pool_warnings`` string list shape.

    Kept for one release so older Streamlit builds that still read
    ``response['pool_warnings']`` keep rendering something. New code
    should consume ``rule_diagnostics`` directly.
    """
    out: List[str] = []
    for d in diagnostics:
        if d.rule_type == MenuRuleType.POOL_SIZE.value:
            out.append(d.message)
    return out


# ---------------------------------------------------------------------------
# Synthetic pool-size diagnostics (replaces api.app._validate_pools)
# ---------------------------------------------------------------------------

def pool_size_diagnostics(
    rules: Iterable[BaseMenuRule], ctx: DiagnoseContext,
) -> List[Diagnostic]:
    """Behaviour-preserving rewrite of the old ``_validate_pools``.

    Iterates (date, base_slot), applies theme-filter rules (history
    bans are intentionally NOT applied here — they're emitted by
    ItemCooldownMenuRule.diagnose() as its own Diagnostics so the two
    surfaces don't dedupe each other into silence).

    Emits:
      - severity=WARNING when pool_size < count_needed (will fail)
      - severity=INFO    when pool_size == count_needed (tight, will work)

    Pool-size diagnostics never emit severity=ERROR — pre-filters can
    further drop items, so we leave the definitive "this will block"
    judgement to the rules whose constraint failed (item_cooldown,
    theme_slot_filter, etc.). This matches the legacy _validate_pools
    behaviour which was advisory-only.
    """
    out: List[Diagnostic] = []
    # ``None`` means "client config didn't restrict slots → use the
    # global default list". An *explicit* empty list means "no slots
    # apply" (e.g. a fixture that wants to disable this synthetic
    # pass). The distinction matters for test ergonomics.
    base_slots = (
        list(BASE_SLOT_NAMES)
        if ctx.active_base_slots is None
        else ctx.active_base_slots
    )
    slot_counts = ctx.client_cfg.slot_counts if ctx.client_cfg is not None else {}

    rules_list = list(rules)
    filter_ctx_base = {
        'cfg': ctx.cfg,
        # Intentionally empty: this surface is pool-shape-only, not
        # history-aware. The cooldown rule emits its own diagnostics
        # for the history slice.
        'banned_by_date': {},
        'ricebread_ban_day': {},
        'pools': ctx.pools,
    }

    rule_type_value = MenuRuleType.POOL_SIZE.value

    for d in ctx.dates:
        day_type = ctx.day_types.get(d, '')
        for base in base_slots:
            if (d, base) in ctx.skip_cells:
                continue
            if base not in ctx.pools:
                continue
            pool = ctx.pools[base].copy()

            if base in ('rice', 'healthy_rice') and len(pool) > 0:
                pool = pool[~pool['item'].isin(ctx.cfg.rice_exclude_items)]

            filter_ctx = {**filter_ctx_base, 'slot_num': None}
            for rule in rules_list:
                pool = rule.pre_filter_pool(pool, d, base, day_type, filter_ctx)

            count_needed = slot_counts.get(base, 1) if slot_counts else 1
            pool_size = len(pool)
            day_label = d.strftime('%A %d %b')
            slot_label = base.replace('_', ' ')
            plural = '' if pool_size == 1 else 's'

            if pool_size < count_needed:
                out.append(Diagnostic(
                    rule='pool_size',
                    rule_type=rule_type_value,
                    severity=DiagnosticSeverity.WARNING,
                    phase=DiagnosticPhase.PRE_FILTER,
                    message=(
                        f"{day_type.capitalize()} {day_label}: only {pool_size} "
                        f"{slot_label} item{plural} available, need {count_needed}"
                    ),
                    suggestion=(
                        f"Add more {slot_label} items to the ontology, or "
                        f"reduce the slot count for {slot_label} in the editor."
                    ),
                    affected={
                        'date': d.isoformat(),
                        'slot': base,
                        'day_type': day_type,
                        'pool_size': pool_size,
                        'count_needed': count_needed,
                    },
                ))
            elif pool_size == count_needed:
                out.append(Diagnostic(
                    rule='pool_size',
                    rule_type=rule_type_value,
                    severity=DiagnosticSeverity.INFO,
                    phase=DiagnosticPhase.PRE_FILTER,
                    message=(
                        f"{day_type.capitalize()} {day_label}: exactly {pool_size} "
                        f"{slot_label} item{plural} available for {count_needed} "
                        f"needed (no variety)"
                    ),
                    suggestion=(
                        f"This will work but will produce the same item every "
                        f"time. Add more {slot_label} items to get variety."
                    ),
                    affected={
                        'date': d.isoformat(),
                        'slot': base,
                        'day_type': day_type,
                        'pool_size': pool_size,
                        'count_needed': count_needed,
                    },
                ))
    return out
