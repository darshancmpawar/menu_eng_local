"""
Flask API Application for Menu Planning System.

Endpoints:
  POST /api/v1/plan — Generate a menu plan for a client
  POST /api/v1/regenerate — Regenerate selected cells
  POST /api/v1/save — Save plan to history
  GET  /api/v1/clients — List available clients
  GET  /api/v1/health — Health check
"""

import datetime as dt
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from flask import Flask, request, jsonify, g, has_request_context
from flask_cors import CORS

from api.concurrency import solver_slot, get_stats as _solver_stats
from api.rate_limit import rate_limit
from api import metrics

from api.config import (
    DEFAULT_EXCEL_PATH, MENU_RULES_CONFIG_PATH,
    API_HOST, API_PORT, DEBUG, APP_VERSION,
    MIN_NUM_DAYS, MAX_NUM_DAYS, MIN_TIME_LIMIT_SECONDS, MAX_TIME_LIMIT_SECONDS,
    validate_required_env, today_in_app_tz,
)
from api.logging_config import (
    configure_logging,
    new_request_id,
    request_id_var,
)

# Install the logging config before anything else logs. Idempotent, so
# callers that also import us (Streamlit entry, tests) don't double-up.
configure_logging()

# Fail fast if required secrets / URLs are unset. The alternative is an
# opaque KeyError or Supabase auth error on the first request — which
# happens in production long after the process looked healthy.
validate_required_env()
from src.preprocessor import ExcelReader, DataCleanser
from src.preprocessor.pool_builder import PoolBuilder, _base_slot
from src.constants import BASE_SLOT_NAMES, CONST_SLOTS, REPEATABLE_ITEM_BASES
from src.client import ClientConfigLoader
from src.client.client_config import DEFAULT_THEME_MAP, AVAILABLE_THEMES  # noqa: F401 — surfaced in editor-metadata response
from src.history import HistoryManager
from src.menu_rules import MenuRuleLoader
from src.menu_rules import (
    DiagnoseContext,
    run_diagnostics,
    summarize as _summarize_diags,
    has_blocking_errors,
    pool_warnings_projection,
)
from src.solver.menu_solver import MenuSolver, SolverConfig
from src.solver._helpers import (
    weekday_type_for_config as _weekday_type_cfg,
    strip_color_suffix,
    items_from_day as _items_from_day,
)
from src.solver.solution_formatter import SolutionFormatter
from src.solver.regenerator import MenuRegenerator

logger = logging.getLogger(__name__)

# Generic message returned to clients when the server hits an unexpected
# error. The real exception is logged server-side with exc_info; we must not
# echo exception details back to the caller, since Supabase errors and
# similar can reveal connection strings, internal hostnames, or schema info.
_INTERNAL_ERROR_MSG = "Internal server error"


def _internal_error_response(status: int = 500):
    """Return a generic-error JSON response with the current request_id.

    The body never carries exception details (security), but it does
    surface the request_id so an admin debugging "Internal server error"
    in the UI can grep that id in the access log and find the real
    traceback. Fix for the recurring "I see Internal server error and
    have no way to triage" pain.
    """
    rid = getattr(g, 'request_id', None) if has_request_context() else None
    body = {'success': False, 'error': _INTERNAL_ERROR_MSG}
    if rid:
        body['request_id'] = rid
    return jsonify(body), status

# Record when the process started so /health can report uptime. Used
# for liveness / deploy-tracking rather than anything load-bearing.
_STARTED_AT = time.time()

app = Flask(__name__)

# CORS: default to loopback-only (the Streamlit frontend calls the API
# server-side via `requests`, so no browser origin needs access). Set
# CORS_ALLOWED_ORIGINS="https://prod.example.com,https://staging.example.com"
# to permit additional origins in production.
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
CORS(app, origins=_cors_origins)


# ---------------------------------------------------------------------------
# Request tracing — one access log line per request with timing + user.
# ---------------------------------------------------------------------------

@app.before_request
def _trace_request_start() -> None:
    # Prefer a caller-supplied X-Request-ID so traces can be correlated
    # across services; otherwise mint our own.
    rid = request.headers.get("X-Request-ID", "").strip() or new_request_id()
    g.request_id = rid
    # Every request's before_request overwrites the ContextVar, so
    # thread-pool reuse can't leak an id from the previous request.
    request_id_var.set(rid)
    g._t0 = time.perf_counter()


@app.after_request
def _trace_request_end(response):
    t0 = getattr(g, "_t0", None)
    duration_ms = (
        int((time.perf_counter() - t0) * 1000) if t0 is not None else None
    )
    rid = getattr(g, "request_id", "-")
    response.headers["X-Request-ID"] = rid

    user_email = None

    # /health gets spammy fast — skip its access log unless it errored.
    if request.path == "/api/v1/health" and response.status_code < 400:
        return response

    logger.info(
        "http_request",
        extra={
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "user": user_email,
            "remote_addr": request.remote_addr,
        },
    )
    return response


@app.teardown_request
def _trace_request_teardown(_exc):
    # Return the ContextVar to its sentinel so log records emitted
    # after this request finishes (e.g. background cleanup, the next
    # test in a pytest session) don't inherit a stale request id.
    # Using set() rather than reset() because tokens are single-use
    # and the after_request path may have already released it.
    request_id_var.set("-")


# Thread-safe lazy singletons
_init_lock = threading.Lock()
_client_loader = None
_pools = None
_df = None
_menu_rules = None


def _get_client_loader():
    global _client_loader
    if _client_loader is None:
        with _init_lock:
            if _client_loader is None:
                _client_loader = ClientConfigLoader()
    return _client_loader


def _get_menu_data():
    global _pools, _df
    if _pools is None:
        with _init_lock:
            if _pools is None:
                reader = ExcelReader(DEFAULT_EXCEL_PATH)
                raw_df = reader.read()
                cleanser = DataCleanser(raw_df)
                _df = cleanser.clean()
                _pools = PoolBuilder.build_pools(_df)
    return _df, _pools


def _get_menu_rules():
    global _menu_rules
    if _menu_rules is None:
        with _init_lock:
            if _menu_rules is None:
                loader = MenuRuleLoader(MENU_RULES_CONFIG_PATH)
                _menu_rules = loader.load_from_file()
    return _menu_rules


def _rules_and_skip_for_client(client_name, dates):
    """Return (rules, skip_cells) for a client, merging generic + per-client."""
    generic = _get_menu_rules()
    loader = MenuRuleLoader()
    rules = loader.load_for_client(client_name, generic)
    skip_cells = set()
    for rule in rules:
        if hasattr(rule, 'compute_skip_cells'):
            skip_cells |= rule.compute_skip_cells(dates)
    return rules, skip_cells


# Floor lookback. Must cover every history-consuming rule's cooldown.
# With the default rules (week-signature 30d, item cooldown 20d,
# rice-bread gap 10d), 45d gives 15d of slack. For per-client overrides
# that push cooldowns past this floor, _effective_history_window()
# widens the window at runtime instead of silently cutting off data.
_HISTORY_WINDOW_DAYS = 45
_HISTORY_WINDOW_SLACK_DAYS = 15


def _effective_history_window(rules) -> int:
    """Return a window that covers every rule's cooldown + slack.

    Rules expose their cooldown via either ``cooldown_days`` (item /
    week-signature cooldowns) or ``gap_days`` (rice-bread gap). We take
    the max across both attributes on all rules, add slack, and take the
    larger of that and the ``_HISTORY_WINDOW_DAYS`` floor. Widening is
    logged so operators notice a per-client rule is pushing queries
    further back than usual.

    Skipping this check is how "we quietly miss the last 10 days of
    history" bugs happen: ``_HISTORY_WINDOW_DAYS`` is a fixed constant,
    but cooldowns can be overridden per-client or in future rules.
    """
    max_cd = 0
    for r in rules or []:
        for attr in ('cooldown_days', 'gap_days'):
            value = getattr(r, attr, None)
            if isinstance(value, int) and value > max_cd:
                max_cd = value
    effective = max(_HISTORY_WINDOW_DAYS, max_cd + _HISTORY_WINDOW_SLACK_DAYS)
    if effective > _HISTORY_WINDOW_DAYS:
        logger.warning(
            "Widening history lookback from %d to %d days to cover "
            "max rule cooldown %d + %d slack",
            _HISTORY_WINDOW_DAYS, effective, max_cd, _HISTORY_WINDOW_SLACK_DAYS,
        )
    return effective


def _build_history_context(
    df, client_name, start_date, weekday_dates, window_days=None,
):
    """Shared helper to build history-based solver inputs from Supabase.

    Pushes ``client_name`` and ``service_date >= cutoff`` filters down to
    Supabase so the query hits the ``(client_name, service_date DESC)``
    index on ``menu_history`` (and the analogous index on
    ``week_signatures``) instead of scanning every row for every tenant.

    *window_days* is the backward-lookback in days. Callers should pass
    ``_effective_history_window(rules)`` so per-client rule overrides
    don't silently truncate the window. Falling back to the floor
    keeps the function usable in tests / scripts that don't assemble
    rules upfront.
    """
    import pandas as pd
    from src.db import get_supabase

    if window_days is None:
        window_days = _HISTORY_WINDOW_DAYS
    earliest = start_date - dt.timedelta(days=window_days)
    earliest_iso = earliest.isoformat()

    hm = HistoryManager()
    sb = get_supabase()
    long_resp = (
        sb.table('menu_history')
        .select('*')
        .eq('client_name', client_name)
        .gte('service_date', earliest_iso)
        .execute()
    )
    weeks_resp = (
        sb.table('week_signatures')
        .select('*')
        .eq('client_name', client_name)
        .gte('week_start', earliest_iso)
        .execute()
    )
    long_df = pd.DataFrame(long_resp.data) if long_resp.data else None
    weeks_df = pd.DataFrame(weeks_resp.data) if weeks_resp.data else None
    hm.load_from_dataframes(long_df, weeks_df)
    # Rows are already scoped to this client at the DB layer, but leave
    # the in-memory filter in place as belt-and-suspenders for anyone
    # who seeds the manager from an unfiltered DataFrame.
    hm = hm.filter_by_client(client_name)

    banned = hm.banned_items_by_date(weekday_dates, const_slots=CONST_SLOTS,
                                      repeatable_items=REPEATABLE_ITEM_BASES)
    ricebread_items = set(
        df.loc[df.get('is_rice_bread', 0) == 1, 'item'].tolist()
    ) if 'is_rice_bread' in df.columns else set()
    rb_ban = hm.ricebread_ban_by_date(weekday_dates, ricebread_items)
    recent_sigs = hm.recent_week_signatures(start_date)
    return banned, rb_ban, recent_sigs


def _cached_on_g(key: str, compute):
    """Memoize ``compute()`` on Flask's ``g`` for the current request.

    ClientConfigLoader properties read Supabase on every access (no
    in-process cache, so admin edits are picked up immediately). Some of
    them — client_names, menu_categories — end up fetched multiple
    times per request: once from _require_known_client, once from the
    endpoint body, once from editor-metadata etc. Caching on ``g`` keeps
    the "live reads across requests" guarantee while collapsing the
    intra-request round trips.

    Outside a request context (module-import paths, bare scripts) there
    is no ``g`` to hang on to, so we just call compute() uncached.
    """
    if not has_request_context():
        return compute()
    cache = getattr(g, '_clientcfg_cache', None)
    if cache is None:
        cache = {}
        g._clientcfg_cache = cache
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _request_client_names():
    return _cached_on_g(
        'client_names',
        lambda: _get_client_loader().client_names,
    )


def _request_menu_categories():
    return _cached_on_g(
        'menu_categories',
        lambda: _get_client_loader().menu_categories,
    )


def _count_rule_failures(failures) -> None:
    """Bump ``rule_failures_total{rule=<name>}`` for every failure the
    solver recorded on this request. Keeps the metrics surface aligned
    with the response's ``rule_warnings`` payload so a Prometheus alert
    on rule_failures_total doesn't disagree with what the client saw.
    """
    if not failures:
        return
    for entry in failures:
        rule_name = entry.get('rule', 'unknown') if isinstance(entry, dict) else 'unknown'
        metrics.incr('rule_failures_total', rule=rule_name)


def _require_known_client(client_name):
    """Validate ``client_name`` is non-empty and refers to a known client.

    Raises ``ValueError`` with a user-safe message so the caller's 400
    handler picks it up. Keeps invalid input from reaching solver setup
    or Supabase config reads, where it would surface as a less-clear
    error deep in the stack.
    """
    if not client_name or not isinstance(client_name, str):
        raise ValueError('client_name is required')
    if client_name not in _request_client_names():
        raise ValueError(f"Unknown client: {client_name}")


def _weekdays_from(start_date, num_days):
    """Return up to num_days weekday dates (skip Sat/Sun) starting from start_date."""
    dates = []
    d = start_date
    while len(dates) < num_days:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d)
        d += dt.timedelta(days=1)
    return dates


def _client_base_slots(client_cfg):
    """Return unique base slot names the client uses (excluding constants).

    Handles expanded slot IDs like veg_dry__1, veg_dry__2 by extracting
    the base name so the solver gets ['veg_dry'] not ['veg_dry__1', 'veg_dry__2'].
    """
    seen = set()
    result = []
    for s in client_cfg.active_slots:
        if s in CONST_SLOTS:
            continue
        base = _base_slot(s)
        if base not in seen:
            seen.add(base)
            result.append(base)
    return result


def _build_solver_config(df, client_cfg, start_date, num_days, time_limit, weekday_dates):
    """Shared helper to build SolverConfig."""
    active_base = _client_base_slots(client_cfg)
    return SolverConfig(
        days=num_days,
        start_date=start_date,
        time_limit_sec=time_limit,
        slot_counts=client_cfg.slot_counts,
        active_base_slots=active_base or None,
        explicit_dates=weekday_dates,
        premium_flag_col='is_premium_veg' if 'is_premium_veg' in df.columns and int(df['is_premium_veg'].sum()) > 0 else None,
        theme_map=client_cfg.theme_map or None,
    )


@dataclass
class SolverInputs:
    """Bundle of everything MenuSolver / MenuRegenerator need for one request."""
    client_name: str
    client_cfg: Any
    df: Any
    pools: Dict[str, Any]
    start_date: dt.date
    num_days: int
    time_limit: int
    weekday_dates: List[dt.date]
    rules: List[Any]
    skip_cells: Set[Any]
    banned: Dict[Any, Any]
    rb_ban: Dict[Any, Any]
    recent_sigs: List[Any]
    cfg: SolverConfig


def _prepare_solver_inputs(data: Dict[str, Any]) -> SolverInputs:
    """Parse request body and assemble all inputs the solver/regenerator need.

    Raises ``ValueError`` with a user-facing message on missing/invalid input.
    """
    client_name = data.get('client_name')
    _require_known_client(client_name)

    start_date_str = data.get('start_date')
    num_days = max(MIN_NUM_DAYS, min(MAX_NUM_DAYS, int(data.get('num_days', 5))))
    time_limit = max(
        MIN_TIME_LIMIT_SECONDS,
        min(MAX_TIME_LIMIT_SECONDS, int(data.get('time_limit_seconds', 240))),
    )

    client_cfg = _get_client_loader().get_client(client_name)
    df, pools = _get_menu_data()
    start_date = dt.date.fromisoformat(start_date_str) if start_date_str else today_in_app_tz()
    weekday_dates = _weekdays_from(start_date, num_days)
    rules, skip_cells = _rules_and_skip_for_client(client_name, weekday_dates)
    window_days = _effective_history_window(rules)
    banned, rb_ban, recent_sigs = _build_history_context(
        df, client_name, start_date, weekday_dates, window_days=window_days,
    )
    cfg = _build_solver_config(df, client_cfg, start_date, num_days, time_limit, weekday_dates)

    return SolverInputs(
        client_name=client_name,
        client_cfg=client_cfg,
        df=df,
        pools=pools,
        start_date=start_date,
        num_days=num_days,
        time_limit=time_limit,
        weekday_dates=weekday_dates,
        rules=rules,
        skip_cells=skip_cells,
        banned=banned,
        rb_ban=rb_ban,
        recent_sigs=recent_sigs,
        cfg=cfg,
    )


def _build_diagnose_context(inputs: SolverInputs) -> DiagnoseContext:
    """Project the SolverInputs bundle into a DiagnoseContext the
    rule diagnose() methods can consume.

    Computes the per-date day_types map up front (the rules want
    O(1) lookup, not repeated weekday_type_for_config calls), and
    surfaces the client's active base slots so diagnose() iterates
    over the slots that will actually be solved (not the global
    BASE_SLOT_NAMES list).
    """
    day_types = {
        d: _weekday_type_cfg(d, inputs.cfg.theme_map)
        for d in inputs.weekday_dates
    }
    active_base = inputs.cfg.active_base_slots
    return DiagnoseContext(
        pools=inputs.pools,
        dates=inputs.weekday_dates,
        day_types=day_types,
        cfg=inputs.cfg,
        df=inputs.df,
        banned_by_date=inputs.banned,
        ricebread_ban_day=inputs.rb_ban,
        skip_cells=inputs.skip_cells,
        client_cfg=inputs.client_cfg,
        active_base_slots=active_base,
    )


def _run_preflight(inputs: SolverInputs):
    """Shared pre-flight pass used by both /plan and /diagnose.

    Returns ``(diagnostics, summary)`` where:
      - ``diagnostics`` is the full sorted list of Diagnostic objects
        produced by every rule + the synthetic pool_size pass.
      - ``summary`` is the ``{errors, warnings, infos, would_succeed}``
        dict produced by ``summarize()``.

    A single call site for both endpoints keeps the two surfaces in
    lockstep: /diagnose and /plan's gate emit identical diagnostics
    for identical inputs. ``test_diagnose_matches_plan_preflight``
    pins this invariant.
    """
    ctx = _build_diagnose_context(inputs)
    diags = run_diagnostics(inputs.rules, ctx)
    return diags, _summarize_diags(diags)


def _record_diag_metrics(diagnostics) -> None:
    """Bump ``rule_diagnostics_total{rule=<name>,severity=<sev>}`` once
    per emitted Diagnostic. Mirrors ``_count_rule_failures`` so a
    Prometheus alert can fire on either surface symmetrically.
    """
    for d in diagnostics:
        metrics.incr(
            'rule_diagnostics_total',
            rule=d.rule,
            severity=d.severity.value,
        )


@app.route('/api/v1/clients', methods=['GET'])
def list_clients():
    try:
        return jsonify({'success': True, 'clients': _request_client_names()})
    except (FileNotFoundError, ValueError, KeyError) as e:
        logger.error("Failed to list clients: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/plan', methods=['POST'])
@rate_limit("plan")
def plan_menu():
    try:
        inputs = _prepare_solver_inputs(request.get_json() or {})

        # Pre-flight gate: run every rule's diagnose() against the
        # assembled inputs. If any diagnostic is severity=error, the
        # solver would (with overwhelming probability) fail — so we
        # short-circuit with 422 and the structured diagnostics
        # before spending solver budget.
        diagnostics, summary = _run_preflight(inputs)
        _record_diag_metrics(diagnostics)
        diag_dicts = [d.to_dict() for d in diagnostics]
        # Denormalised pool_warnings projection kept for one release so
        # older Streamlit builds that still read this key keep rendering
        # something. New code consumes ``rule_diagnostics``.
        pool_warnings = pool_warnings_projection(diagnostics)

        if has_blocking_errors(diagnostics):
            metrics.incr('plan_requests_total', outcome='preflight_blocked')
            body = {
                'success': False,
                'error': 'rule_diagnostics_blocked',
                'message': (
                    f"Pre-flight diagnostics found "
                    f"{summary['errors']} blocking issue"
                    f"{'s' if summary['errors'] != 1 else ''} for "
                    f"{inputs.client_name}; solver skipped."
                ),
                'rule_diagnostics': diag_dicts,
                'summary': summary,
            }
            if pool_warnings:
                body['pool_warnings'] = pool_warnings
            return jsonify(body), 422

        # Weighted admission control — sized by plan length so short plans
        # don't queue behind heavy ones.
        with solver_slot(inputs.cfg.days) as admitted:
            if not admitted:
                return jsonify({
                    'success': False,
                    'error': 'Solver busy — too many concurrent requests. Retry shortly.',
                }), 503

            solver = MenuSolver(
                pools=inputs.pools,
                solver_config=inputs.cfg,
                menu_rules=inputs.rules,
                banned_by_date=inputs.banned,
                ricebread_ban_day=inputs.rb_ban,
                recent_sigs=inputs.recent_sigs,
                skip_cells=inputs.skip_cells,
            )

            week_plan, plan_dates = solver.solve()

        formatter = SolutionFormatter(
            week_plan, plan_dates, theme_map=inputs.client_cfg.theme_map or None,
        )
        response = {
            'success': True,
            'message': f'Menu plan generated for {inputs.client_name}',
            'solution': formatter.to_dict(),
            'rule_diagnostics': diag_dicts,
            'summary': summary,
        }
        if pool_warnings:
            response['pool_warnings'] = pool_warnings
        if solver.rule_failures:
            response['rule_warnings'] = solver.rule_failures
            _count_rule_failures(solver.rule_failures)
        metrics.incr('plan_requests_total', outcome='success')
        return jsonify(response)

    except (ValueError, KeyError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        logger.warning("Solver failed: %s", e)
        # Counts infeasibility + exhausted-restarts from the CP-SAT path;
        # this is the SLO-relevant failure mode (vs 4xx, which is caller
        # input error).
        metrics.incr('plan_requests_total', outcome='solver_error')
        metrics.incr('solver_failures_total')
        return jsonify({'success': False, 'error': str(e)}), 500
    except (FileNotFoundError, OSError) as e:
        logger.error("Data loading error: %s", e, exc_info=True)
        return _internal_error_response(500)
    except Exception as e:
        logger.error("Unexpected error in plan: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/regenerate', methods=['POST'])
@rate_limit("regenerate")
def regenerate_cells():
    try:
        data = request.get_json() or {}
        base_plan_raw = data.get('base_plan', {})
        replace_slots_raw = data.get('replace_slots', {})
        if not base_plan_raw:
            return jsonify({'success': False, 'error': 'base_plan is required'}), 400
        if not replace_slots_raw:
            return jsonify({'success': False, 'error': 'replace_slots is required'}), 400

        inputs = _prepare_solver_inputs(data)

        base_plan = {
            dt.date.fromisoformat(d_str): _items_from_day(slots)
            for d_str, slots in base_plan_raw.items()
        }
        replace_mask = {
            dt.date.fromisoformat(d_str): set(slot_list)
            for d_str, slot_list in replace_slots_raw.items()
        }

        with solver_slot(inputs.cfg.days) as admitted:
            if not admitted:
                return jsonify({
                    'success': False,
                    'error': 'Solver busy — too many concurrent requests. Retry shortly.',
                }), 503

            regen = MenuRegenerator(
                pools=inputs.pools,
                df=inputs.df,
                solver_config=inputs.cfg,
                menu_rules=inputs.rules,
                banned_by_date=inputs.banned,
                ricebread_ban_day=inputs.rb_ban,
                recent_sigs=inputs.recent_sigs,
                skip_cells=inputs.skip_cells,
            )

            week_plan, plan_dates = regen.regenerate(base_plan, replace_mask)

        formatter = SolutionFormatter(
            week_plan, plan_dates, theme_map=inputs.client_cfg.theme_map or None,
        )
        response = {
            'success': True,
            'message': f'Regenerated {sum(len(v) for v in replace_mask.values())} cells for {inputs.client_name}',
            'solution': formatter.to_dict(),
        }
        if regen.rule_failures:
            response['rule_warnings'] = regen.rule_failures
            _count_rule_failures(regen.rule_failures)
        metrics.incr('regenerate_requests_total', outcome='success')
        return jsonify(response)

    except (ValueError, KeyError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except RuntimeError as e:
        logger.warning("Regeneration failed: %s", e)
        metrics.incr('regenerate_requests_total', outcome='solver_error')
        metrics.incr('solver_failures_total')
        return jsonify({'success': False, 'error': str(e)}), 500
    except (FileNotFoundError, OSError) as e:
        logger.error("Data loading error: %s", e, exc_info=True)
        return _internal_error_response(500)
    except Exception as e:
        logger.error("Unexpected error in regenerate: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/save', methods=['POST'])
def save_plan():
    try:
        data = request.get_json(silent=True) or {}
        client_name = data.get('client_name')
        week_plan_raw = data.get('week_plan', {})
        week_start_str = data.get('week_start')

        _require_known_client(client_name)
        if not week_plan_raw:
            return jsonify({'success': False, 'error': 'week_plan is required'}), 400
        if not week_start_str:
            return jsonify({'success': False, 'error': 'week_start is required'}), 400

        # Convert string date keys to date objects, extracting items from solution format
        week_plan = {
            dt.date.fromisoformat(d_str): _items_from_day(day_data)
            for d_str, day_data in week_plan_raw.items()
        }

        dates = sorted(week_plan.keys())
        week_start = dt.date.fromisoformat(week_start_str)

        sig = HistoryManager.compute_week_signature(
            week_plan, dates, const_slots=CONST_SLOTS,
            strip_color_fn=strip_color_suffix,
        )

        hm = HistoryManager()
        # Get Supabase client for persistent storage
        from src.db import get_supabase
        sb = get_supabase()
        # HistoryManager.save is now overwrite-on-conflict: re-saving for
        # the same (client, dates) replaces the prior rows instead of
        # appending. See HistoryManager.save docstring.
        hm.save(week_plan, dates, client_name, week_start, sig,
                supabase_client=sb,
                strip_color_fn=strip_color_suffix)

        return jsonify({'success': True, 'message': 'Plan saved to history'})

    except (ValueError, KeyError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except (FileNotFoundError, OSError) as e:
        logger.error("Save failed: %s", e, exc_info=True)
        return _internal_error_response(500)
    except Exception as e:
        logger.error("Unexpected save error: %s", e, exc_info=True)
        return _internal_error_response(500)


def _build_item_color_lookup(df) -> Dict[str, str]:
    """Return ``{normalised_item_name: color_initial_letter}``.

    Used by ``saved_plan`` to re-attach a color suffix to history rows.
    ``menu_history.item_base`` is stored without color (the cooldown
    rules are color-agnostic), but the UI's table renderer expects
    ``item(C)``-shaped strings — without this lookup, loaded plans show
    no color pills.

    Falls back gracefully: items not in *df* (admin-renamed, removed,
    legacy entries) round-trip without a color suffix instead of
    crashing.
    """
    from src.preprocessor.column_mapper import _norm_str, _norm_color

    out: Dict[str, str] = {}
    if df is None or 'item' not in df.columns:
        return out
    color_col = 'item_color' if 'item_color' in df.columns else None
    for _, row in df[['item'] + ([color_col] if color_col else [])].iterrows():
        name = _norm_str(row.get('item', ''))
        if not name:
            continue
        if color_col is None:
            out.setdefault(name, '')
            continue
        col = _norm_color(row.get(color_col, 'unknown'))
        if col == 'unknown' or '_' not in col:
            out.setdefault(name, '')
            continue
        # _norm_color returns shapes like "light_red", "medium_green";
        # the UI's display logic uses the last token's first letter
        # (matches src.solver.menu_solver._color_initial).
        base = col.split('_')[-1]
        out.setdefault(name, base[:1].upper() if base else '')
    return out


def _enrich_history_plan(
    saved: Dict[dt.date, Dict[str, str]], df,
) -> Dict[dt.date, Dict[str, str]]:
    """Turn ``{date: {slot: item_base}}`` (history shape) into
    ``{date: {slot: item_with_color}}`` (UI shape) by looking up each
    item's color in the loaded Excel *df* and appending ``(C)``.

    Constant slots (white_rice, papad, pickle, chutney) round-trip as-is
    since they never carried a color suffix in the first place. Items
    that no longer exist in the ontology fall through without a suffix
    — the UI handles missing-color gracefully (no color pill).
    """
    color_lookup = _build_item_color_lookup(df)
    out: Dict[dt.date, Dict[str, str]] = {}
    for d, slots in saved.items():
        day_out: Dict[str, str] = {}
        for slot_id, item_base in slots.items():
            if not item_base:
                continue
            if slot_id in CONST_SLOTS:
                day_out[slot_id] = item_base
                continue
            initial = color_lookup.get(item_base, '')
            day_out[slot_id] = f'{item_base}({initial})' if initial else item_base
        out[d] = day_out
    return out


@app.route('/api/v1/saved-plan', methods=['GET'])
def saved_plan():
    """Return the saved plan for a client + date range, if one exists.

    Query params:
        client_name (required): the client to look up.
        start_date  (optional): YYYY-MM-DD; defaults to today in
            APP_TZ.
        num_days    (optional): number of weekdays from start_date;
            defaults to 5. Sat/Sun are skipped, mirroring /plan.

    Response shape mirrors /plan so the UI can use one code path:
        {
          "success": True,
          "exists": <bool>,           # True iff every requested date
                                      # has at least one saved row.
          "covered_dates": [...],     # ISO date strings that DID have
                                      # saved rows (could be a strict
                                      # subset of the requested range).
          "source": "history",
          "solution": <SolutionFormatter.to_dict() output>,
        }

    When ``exists`` is False the ``solution`` only contains the days
    that were partially saved; the caller decides whether to fall back
    to /plan. We never call the solver from this endpoint — it's a
    pure read path.
    """
    try:
        client_name = request.args.get('client_name', '').strip()
        _require_known_client(client_name)

        start_date_str = request.args.get('start_date')
        num_days = max(
            MIN_NUM_DAYS,
            min(MAX_NUM_DAYS, int(request.args.get('num_days', 5))),
        )
        start_date = (
            dt.date.fromisoformat(start_date_str)
            if start_date_str else today_in_app_tz()
        )
        weekday_dates = _weekdays_from(start_date, num_days)

        loader = _get_client_loader()
        client_cfg = loader.get_client(client_name)

        from src.db import get_supabase
        sb = get_supabase()
        raw_saved = HistoryManager.load_saved_plan(
            sb, client_name, weekday_dates,
        )

        # Enrich with color suffix so the UI's renderer matches /plan.
        df, _pools = _get_menu_data()
        enriched = _enrich_history_plan(raw_saved, df)

        formatter = SolutionFormatter(
            enriched, weekday_dates,
            theme_map=client_cfg.theme_map or None,
        )
        covered = sorted(d.isoformat() for d in enriched.keys())
        exists = len(enriched) == len(weekday_dates) and len(enriched) > 0

        return jsonify({
            'success': True,
            'exists': exists,
            'covered_dates': covered,
            'source': 'history',
            'solution': formatter.to_dict(),
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error("Failed to load saved plan: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/editor-metadata', methods=['GET'])
def editor_metadata():
    """Return metadata needed by the customisation editor UI."""
    try:
        return jsonify({
            'success': True,
            'base_slot_names': list(BASE_SLOT_NAMES),
            'const_slots': list(CONST_SLOTS),
            'default_theme_map': DEFAULT_THEME_MAP,
            'available_themes': AVAILABLE_THEMES,
            'clients': _request_client_names(),
            'menu_categories': _request_menu_categories(),
        })
    except Exception as e:
        logger.error("Failed to load editor metadata: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/client-config/<client_name>', methods=['GET'])
def get_client_config(client_name):
    """Return the full editable config for one client.

    Includes a ``version`` field + an ``ETag: "<version>"`` response
    header so callers can issue optimistic-concurrency-safe PUTs.
    """
    try:
        loader = _get_client_loader()
        base_slots = loader.get_active_slots_for_client(client_name)
        menu_category = loader.get_client_menu_category(client_name)
        cfg = loader.get_client(client_name)
        version = loader.get_client_version(client_name)
        response = jsonify({
            'success': True,
            'name': cfg.name,
            'menu_category': menu_category,
            'active_base_slots': [s for s in base_slots if s not in CONST_SLOTS],
            'slot_counts': cfg.slot_counts,
            'theme_map': cfg.theme_map,
            'version': version,
        })
        response.headers['ETag'] = f'"{version}"'
        return response
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        logger.error("Failed to load client config: %s", e, exc_info=True)
        return _internal_error_response(500)


_ETAG_RE = re.compile(r'^\s*(?:W/)?"?(\d+)"?\s*$')


def _expected_version(data: Dict[str, Any]) -> Optional[int]:
    """Extract the expected version from the request.

    Accepts either ``{"version": N}`` in the JSON body (preferred by our
    own UI) or an ``If-Match: "N"`` / ``If-Match: N`` header for HTTP
    clients that want to speak the standard idiom.
    """
    if 'version' in data:
        try:
            return int(data['version'])
        except (TypeError, ValueError):
            raise ValueError("version must be an integer")
    header = request.headers.get('If-Match', '').strip()
    if header:
        m = _ETAG_RE.match(header)
        if not m:
            raise ValueError("If-Match header must be a quoted integer")
        return int(m.group(1))
    return None


@app.route('/api/v1/client-config/<client_name>', methods=['PUT'])
def update_client_config(client_name):
    """Update a client's configuration (slots, slot counts, theme overrides).

    Requires an optimistic-concurrency version from the caller to avoid
    last-write-wins when two admins edit the same client. Either:
      * ``{"version": N}`` in the JSON body (what our Streamlit UI sends), or
      * ``If-Match: "N"`` header for standard HTTP clients.

    Responds 409 Conflict with the current version when the check fails.
    """
    from src.client.client_config import ConcurrentEditError

    try:
        data = request.get_json(silent=True) or {}
        loader = _get_client_loader()

        expected = _expected_version(data)
        if expected is None:
            return jsonify({
                'success': False,
                'error': (
                    'version is required (include "version" in the JSON '
                    'body or send an If-Match header with the ETag from '
                    'GET /client-config). This prevents silently '
                    'overwriting another admin\'s changes.'
                ),
            }), 400

        # Bump first: the conditional update is the actual race gate.
        # If another writer snuck in between the caller's GET and this
        # PUT, the update matches zero rows and we 409 before doing any
        # partial sub-update.
        new_version = loader.bump_version_if_matches(client_name, expected)

        if 'active_base_slots' in data:
            loader.update_client_slots(client_name, data['active_base_slots'])
        if 'slot_counts' in data:
            loader.update_client_slot_counts(client_name, data['slot_counts'])
        if 'theme_map' in data:
            loader.update_client_theme_overrides(client_name, data['theme_map'])

        response = jsonify({
            'success': True,
            'message': f'Config updated for {client_name}',
            'version': new_version,
        })
        response.headers['ETag'] = f'"{new_version}"'
        return response
    except ConcurrentEditError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'current_version': e.current_version,
        }), 409
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error("Error updating client config: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/client', methods=['POST'])
def create_client():
    """Create a new client."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        active_slots = data.get('active_slots', list(BASE_SLOT_NAMES))
        if not name:
            return jsonify({'success': False, 'error': 'name is required'}), 400

        loader = _get_client_loader()
        loader.create_client(name, active_slots)

        return jsonify({'success': True, 'message': f'Client {name} created'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error("Failed to create client: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/client/<client_name>', methods=['DELETE'])
def delete_client(client_name):
    """Delete a client."""
    try:
        loader = _get_client_loader()
        loader.delete_client(client_name)

        # No reload needed — Supabase reads are always live
        return jsonify({'success': True, 'message': f'Client {client_name} deleted'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        logger.error("Failed to delete client: %s", e, exc_info=True)
        return _internal_error_response(500)


@app.route('/api/v1/diagnose', methods=['POST'])
def diagnose_plan():
    """Pre-flight rule diagnostic. Same body shape as /plan but never
    invokes the solver — returns structured ``rule_diagnostics`` so the
    UI can show *why* a plan would fail before the user spends solver
    budget.

    Replaces the old /validate-pools endpoint; pool-size warnings are
    folded into the same ``rule_diagnostics`` list (look for entries
    with ``rule_type == 'pool_size'``).

    Response::

        {
          "success": true,
          "rule_diagnostics": [{rule, rule_type, severity, phase,
                                message, suggestion, affected}, …],
          "summary": {errors, warnings, infos, would_succeed},
          "pool_warnings": [...]   # back-compat projection, one release
        }
    """
    try:
        inputs = _prepare_solver_inputs(request.get_json() or {})
        diagnostics, summary = _run_preflight(inputs)
        _record_diag_metrics(diagnostics)
        diag_dicts = [d.to_dict() for d in diagnostics]
        body = {
            'success': True,
            'rule_diagnostics': diag_dicts,
            'summary': summary,
        }
        pool_warnings = pool_warnings_projection(diagnostics)
        if pool_warnings:
            body['pool_warnings'] = pool_warnings
        return jsonify(body)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error("Failed to run diagnostics: %s", e, exc_info=True)
        return _internal_error_response(500)


# Set after the first /health call observes schema drift, cleared on
# the next non-drift result. Lets us log the loud "run the migration"
# ERROR once per occurrence rather than every 30s of uptime-monitor
# noise — a re-pageable signal needs to BE re-pageable.
_drift_logged = False


def _probe_supabase():
    """Cheap reachability + schema-drift check.

    A single ``select('name, version').limit(1)`` query against the
    ``clients`` table verifies, in one round-trip:

      1. Supabase is reachable / authenticated / not blocked by RLS.
      2. The Phase 2 #14 migration has been applied (the ``version``
         column exists on ``clients``).

    Returns ``(reachable: bool, schema_info: dict)``. The dict carries
    ``status`` ∈ {"ok", "drift_detected", "unknown"} and a list of
    ``missing`` ``"table.column"`` strings. Operators read it from the
    /health response body — uptime monitors only see the HTTP status,
    which stays 200 when drift is present (the app still serves via
    the runtime fallback in client_config.py).
    """
    global _drift_logged
    try:
        from src.db import get_supabase
        get_supabase().table('clients').select('name, version').limit(
            1,
        ).execute()
        if _drift_logged:
            logger.info(
                "Schema drift cleared: clients.version is now visible. "
                "Optimistic-concurrency on PUT /client-config is back in "
                "effect."
            )
            _drift_logged = False
        return True, {"status": "ok", "missing": []}
    except Exception as exc:  # noqa: BLE001 — both error classes converted to dict states
        # Distinguish "DB has no clients.version column" (caller needs
        # to apply the migration) from "Supabase is just unreachable"
        # (network / auth issue).
        from src.client.client_config import _is_undefined_column
        if _is_undefined_column(exc):
            if not _drift_logged:
                logger.error(
                    "Schema drift: clients.version column missing. "
                    "Re-run scripts/create_tables.sql in the Supabase "
                    "SQL editor (the ALTER TABLE ... ADD COLUMN IF NOT "
                    "EXISTS is idempotent). The editor + concurrency "
                    "code degrade gracefully until the column is "
                    "added, but optimistic-concurrency on PUT is "
                    "disabled in this state."
                )
                _drift_logged = True
            return True, {
                "status": "drift_detected",
                "missing": ["clients.version"],
            }
        logger.warning("Supabase health probe failed: %s", exc)
        return False, {"status": "unknown", "missing": []}


@app.route('/api/v1/metrics', methods=['GET'])
def metrics_snapshot():
    """Return a point-in-time snapshot of every in-process counter.

    Labels are collapsed into the key using Prometheus text-format
    conventions (``rule_failures_total{rule="cuisine"}``), so a future
    swap to the real prometheus_client stays a one-file change in
    ``api/metrics.py`` without the caller surface moving.

    Gated behind auth because request volume and rule-failure patterns
    leak information about traffic shape; operators should scrape via
    a token just like any other admin endpoint.
    """
    return jsonify({
        'success': True,
        'uptime_seconds': int(time.time() - _STARTED_AT),
        'counters': metrics.snapshot(),
    })


@app.route('/api/v1/health', methods=['GET'])
def health():
    """Liveness + readiness combined.

    Returns 200 with status=healthy when Supabase is reachable, 503
    with status=degraded when it isn't. Schema drift (e.g. the user
    deployed code that needs ``clients.version`` against an unmigrated
    database) is reported in the body's ``schema`` field but does NOT
    flip the HTTP status — the app keeps serving via the runtime
    fallback in client_config.py, and we don't want to wake operators
    at 3am for a "please run a migration" task. The error log written
    by ``_probe_supabase`` is the primary signal for that.
    """
    supabase_up, schema_info = _probe_supabase()
    body = {
        'status': 'healthy' if supabase_up else 'degraded',
        'version': APP_VERSION,
        'uptime_seconds': int(time.time() - _STARTED_AT),
        'supabase_reachable': supabase_up,
        'schema': schema_info,
        'queue': _solver_stats(),
    }
    return jsonify(body), (200 if supabase_up else 503)


@app.route('/')
def root():
    return jsonify({
        'name': 'Ikigai Masala Menu Planning API',
        'version': APP_VERSION,
        'docs': '/api/v1/clients',
    })


if __name__ == '__main__':
    # Logging was already configured at module import.
    app.run(host=API_HOST, port=API_PORT, debug=DEBUG)
