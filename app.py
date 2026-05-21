"""
Streamlit frontend for Ikigai Masala Menu Planning.

Single entry point - auto-starts the Flask API backend in a background thread.

Run with:
    cd ikigai_masala-main
    streamlit run app.py
"""

import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
os.chdir(_APP_DIR)

import datetime as dt
import html
import io
import csv
import logging
import threading
import time

import streamlit as st

from ui.api_client import MenuApiClient, RuleDiagnosticsBlockedError
from ui.formatters import (
    display_label_for_slot_id,
    flatten_api_solution,
    format_item_for_ui,
    format_item_html,
    slot_sort_key,
    THEME_TAG_COLORS,
    THEME_ICONS,
)
from ui.styles import STYLES
from ui.backend_probe import health_check, pick_backend_port
from customisation.main import render_customisation_editor


logger = logging.getLogger(__name__)

# Solver wall-clock budget scales dynamically with plan length.
# Formula: 40 s/day, floored at 60 s, capped at the API's 600 s ceiling.
# The solver internally splits this across 8 restart attempts and enforces
# a 20 s per-attempt floor — so short plans finish quickly while long
# plans (10–15 days) get the headroom CP-SAT needs for a quality solution.
def _planning_time_limit(num_days: int) -> int:
    return min(600, max(60, num_days * 40))


def _render_view_error(view_name: str, exc: BaseException) -> None:
    """Show a clean Streamlit-native error block for an unhandled
    exception inside a top-level view (editor / user-manager / planner).

    The full traceback lands in the server log via ``logger.exception``;
    the user sees a short message + a button to bounce back to the
    planner. Without this guard a render-side bug renders a half-page
    or — depending on Streamlit's config — a full Python traceback,
    neither of which is acceptable for a multi-user deployment.
    """
    logger.exception("Unhandled error in %s view", view_name)
    st.error(
        f"Something went wrong loading the {view_name}. "
        "The error has been logged. Please go back and try again."
    )
    if st.button("Back to planner", key=f"err_back_{view_name}"):
        st.session_state.view = "planner"
        st.rerun()


# ---------------------------------------------------------------------------
# Auto-start Flask API backend
# ---------------------------------------------------------------------------
_BACKEND_URL = None  # set by _ensure_backend_running()


def _start_flask_backend(port: int) -> None:
    # api.app's module-level validate_required_env() raises if any
    # required var is missing; let that bubble up so the Streamlit
    # process shows a clear error instead of a silent backend crash.
    # Logging is configured inside api.app via configure_logging(),
    # so don't install a second root handler here.
    from api.app import app as flask_app
    flask_app.run(host="127.0.0.1", port=port, debug=False,
                  use_reloader=False, threaded=True)


def _ensure_backend_running() -> str:
    """Start the backend if needed and return its base URL.

    Raises ``RuntimeError`` if no port is available or the backend does
    not become healthy within the startup window — the caller should
    surface the error to the user rather than hit an unrelated service
    that happens to sit on port 5000.
    """
    global _BACKEND_URL
    port = pick_backend_port()
    url = f"http://localhost:{port}"
    if health_check(port):
        _BACKEND_URL = url
        return url
    if "flask_started" not in st.session_state:
        t = threading.Thread(
            target=_start_flask_backend, args=(port,), daemon=True,
        )
        t.start()
        st.session_state.flask_started = True
    for _ in range(20):
        if health_check(port):
            _BACKEND_URL = url
            return url
        time.sleep(0.5)
    raise RuntimeError(
        f"Backend did not become healthy on port {port} within 10s. "
        "Check the Streamlit server logs for errors."
    )


# ---------------------------------------------------------------------------
# Page config — MUST be first Streamlit command
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Ikigai Masala - Menu Planner",
    page_icon="https://em-content.zobj.net/source/apple/391/curry-rice_1f35b.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(STYLES, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Backend must be up before the login form (login hits the API).
# ---------------------------------------------------------------------------
# Validate required env vars first so a misconfigured deployment shows a
# clear Streamlit-native error instead of "backend did not become healthy"
# after the spawned thread silently crashes.
try:
    from api.config import validate_required_env
    validate_required_env()
except RuntimeError as e:
    st.error(str(e))
    st.stop()

try:
    _ensure_backend_running()
except RuntimeError as e:
    st.error(str(e))
    st.stop()

# Streamlit reruns the entire script on every widget interaction, so a
# naïve `MenuApiClient(_BACKEND_URL, token=...)` rebuilds a fresh
# requests.Session on every click — connection pool, retry adapter,
# etc. — for no reason. Cache one client per (url, token) pair so the
# underlying HTTPS pool survives across reruns. ``ttl`` is set just
# below the bearer-token lifetime (24h) so a stale entry doesn't keep
# a dead session around forever; logout also explicitly clears it.
@st.cache_resource(ttl=23 * 3600, show_spinner=False)
def _get_api_client(base_url: str) -> MenuApiClient:
    return MenuApiClient(base_url)


client = _get_api_client(_BACKEND_URL)


# Cache low-churn reads (60s TTL): the sidebar's client picker re-renders
# on every interaction but the list itself only changes when a client is
# created/deleted.
@st.cache_data(ttl=60, show_spinner=False)
def _cached_list_clients(_api: MenuApiClient) -> list:
    return _api.list_clients()

# ---------------------------------------------------------------------------
# Session state initialization (only after auth)
# ---------------------------------------------------------------------------
_SESSION_DEFAULTS = {
    "plan": None,
    "plan_dates": [],
    "day_types": {},
    "pool_warnings": [],
    "client_name": None,
    "changes_log": [],
    "view": "planner",
    # "history" when the current plan was loaded from /saved-plan,
    # "solver" when it came from /plan, "modified" once the user has
    # regenerated a cell (so the on-screen plan no longer matches the
    # DB version), "preflight_blocked" when the diagnostic gate stopped
    # the solver from running. Drives the badge on the page header.
    "plan_source": None,
    # Pre-flight rule_diagnostics from the most recent /plan or
    # /saved-plan response (or from a RuleDiagnosticsBlockedError).
    # Empty list = nothing to show. Rendered as the inline expander
    # above the plan table.
    "rule_diagnostics": [],
    "diagnostics_summary": None,
}
for key, default in _SESSION_DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Editor view (full-page)
# ---------------------------------------------------------------------------
if st.session_state.view == "editor":
    try:
        render_customisation_editor(client)
    except Exception as _exc:
        # A Supabase blip while loading client config, a malformed rule
        # in client_rules.json, etc. Log + show a friendly fallback so
        # the user can navigate out instead of staring at a half-page.
        _render_view_error("editor", _exc)
    st.stop()

# ---------------------------------------------------------------------------
# Sidebar (planner view)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""<div class="sidebar-brand">
        <div class="sidebar-brand-row">
            <div class="sidebar-brand-icon">&#127835;</div>
            <div>
                <h2>Ikigai Masala</h2>
                <p>Weekly Menu Planner</p>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

    try:
        clients_list = _cached_list_clients(client)
    except (ConnectionError, OSError, ValueError):
        clients_list = []
        st.error("Cannot reach API.")

    selected_client = st.selectbox("Client",
        clients_list if clients_list else ["(no clients)"],
        key="planner_client_select")
    start_date = st.date_input("Start date", value=dt.date.today(),
                               min_value=dt.date(2020, 1, 1),
                               max_value=dt.date.today() + dt.timedelta(days=730),
                               key="planner_start_date")
    num_days = st.slider("Weekdays", min_value=1, max_value=10, value=5,
                         key="planner_num_days",
                         help="Number of weekdays (Sat/Sun are skipped)")

    st.divider()
    generate_clicked = st.button("Generate Menu Plan", type="primary",
                                 key="planner_generate_btn",
                                 use_container_width=True)

# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------
_hdr_col1, _hdr_col2 = st.columns([5, 2])
_SOURCE_BADGES = {
    # bg, fg, label, title attr
    "history":  ("#0f2a1d", "#86efac", "Loaded from history",
                 "These exact dates already had a saved plan — shown as-is."),
    "solver":   ("#1e0a3a", "#c4b5fd", "Freshly generated",
                 "No saved plan for these dates — solver produced this from scratch."),
    "modified": ("#2a1508", "#fdba74", "Modified — unsaved",
                 "You regenerated at least one cell since this plan was loaded."),
    "preflight_blocked": ("#3b1114", "#fca5a5", "Pre-flight blocked",
                 "Diagnostic checks found a guaranteed failure; solver skipped."),
}


# bg, fg, label per Diagnostic severity (matches docs/api.md examples).
_SEVERITY_STYLE = {
    "error":   ("#3b1114", "#fca5a5", "Error"),
    "warning": ("#2a1508", "#fdba74", "Warning"),
    "info":    ("#0f1a2e", "#93c5fd", "Info"),
}


def _render_diagnostics_expander(diagnostics, summary):
    """Render the inline 'Diagnostics' expander above the plan table.

    Auto-expanded when any error is present (the user must act);
    collapsed otherwise. Sectioned by severity so errors are visible
    first. Reuses the design tokens from ``ui/styles.py``.
    """
    if not diagnostics:
        return
    has_error = bool(summary and summary.get("errors", 0)) or any(
        d.get("severity") == "error" for d in diagnostics
    )
    counts = []
    if summary:
        if summary.get("errors"):
            counts.append(f"{summary['errors']} error"
                          f"{'s' if summary['errors'] != 1 else ''}")
        if summary.get("warnings"):
            counts.append(f"{summary['warnings']} warning"
                          f"{'s' if summary['warnings'] != 1 else ''}")
        if summary.get("infos"):
            counts.append(f"{summary['infos']} info")
    label = (
        f"Diagnostics ({', '.join(counts)})" if counts else "Diagnostics"
    )

    with st.expander(label, expanded=has_error):
        # Group by severity so errors come first regardless of how the
        # server sorted them.
        order = ("error", "warning", "info")
        grouped = {sev: [d for d in diagnostics if d.get("severity") == sev] for sev in order}
        for sev in order:
            items = grouped[sev]
            if not items:
                continue
            bg, fg, sev_label = _SEVERITY_STYLE.get(sev, ("#27272a", "#a1a1aa", sev.title()))
            st.markdown(
                f'<p style="font-size:0.85rem;font-weight:700;color:{fg};'
                f'margin:0.5rem 0 0.4rem;">{sev_label}'
                f' ({len(items)})</p>',
                unsafe_allow_html=True,
            )
            for d in items:
                rule_pill = html.escape(d.get("rule_type") or d.get("rule") or "?")
                msg = html.escape(d.get("message") or "")
                suggestion = html.escape(d.get("suggestion") or "")
                affected = d.get("affected") or {}
                chips = []
                # Surface the most commonly-useful affected fields as
                # chips; everything else stays inside ``affected`` for
                # the API surface but isn't visualised.
                for k in ("date", "day_type", "slot"):
                    if k in affected:
                        chips.append(
                            f'<span style="background:#27272a;color:#a1a1aa;'
                            f'border-radius:99px;padding:1px 8px;font-size:0.65rem;'
                            f'margin-right:4px;">{html.escape(str(affected[k]))}</span>'
                        )
                chip_html = ''.join(chips)
                st.markdown(
                    f'<div style="background:{bg};border-left:3px solid {fg};'
                    f'padding:0.55rem 0.8rem;border-radius:8px;'
                    f'margin-bottom:0.45rem;">'
                    f'<div style="display:flex;align-items:center;gap:0.45rem;'
                    f'margin-bottom:0.2rem;">'
                    f'<span style="background:{fg};color:{bg};font-weight:700;'
                    f'font-size:0.6rem;letter-spacing:0.04em;text-transform:uppercase;'
                    f'padding:1px 7px;border-radius:99px;">{rule_pill}</span>'
                    f'{chip_html}'
                    f'</div>'
                    f'<div style="color:#fafafa;font-size:0.85rem;'
                    f'line-height:1.4;">{msg}</div>'
                    + (f'<div style="color:#a1a1aa;font-size:0.75rem;'
                       f'margin-top:0.25rem;">Fix: {suggestion}</div>'
                       if suggestion else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )


with _hdr_col1:
    st.markdown('<p class="page-title">Menu Plan</p>', unsafe_allow_html=True)
    if st.session_state.client_name:
        src = st.session_state.get("plan_source")
        badge_html = ""
        if src in _SOURCE_BADGES:
            bg, fg, label, title_attr = _SOURCE_BADGES[src]
            badge_html = (
                f'<span class="plan-source-badge" '
                f'title="{html.escape(title_attr)}" '
                f'style="display:inline-block;margin-left:0.6rem;'
                f'padding:2px 10px;border-radius:99px;font-size:0.7rem;'
                f'font-weight:700;letter-spacing:0.04em;text-transform:uppercase;'
                f'background:{bg};color:{fg};vertical-align:middle;">'
                f'{html.escape(label)}</span>'
            )
        st.markdown(
            f'<p class="page-subtitle">Generated plan for '
            f'{html.escape(st.session_state.client_name)}{badge_html}</p>',
            unsafe_allow_html=True)
    else:
        st.markdown(
            '<p class="page-subtitle">Select a client and generate a plan to get started</p>',
            unsafe_allow_html=True)
with _hdr_col2:
    if st.button("Edit Logic", key="open_editor_btn", use_container_width=True):
        st.session_state.view = "editor"
        st.rerun()

# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------
if generate_clicked:
    if selected_client and selected_client != "(no clients)":
        # Two-step Generate:
        #   1. Ask the API whether a saved plan covers the requested
        #      range. If yes, load it (no solve) and flag the source.
        #   2. Otherwise run the solver as usual and save-on-click
        #      will overwrite if the user later re-runs Save.
        # The user always gets a deterministic Generate: same dates →
        # same plan, until they explicitly Regenerate cells.
        try:
            saved = client.get_saved_plan(
                client_name=selected_client,
                start_date=start_date.isoformat(),
                num_days=num_days,
            )
        except (ConnectionError, OSError, ValueError, RuntimeError) as e:
            # Failure here is non-fatal — fall through to the solver
            # path. Log via st.warning so the user sees that the
            # history lookup misbehaved but Generate still completes.
            st.warning(f"Couldn't check saved history ({e}); generating fresh.")
            saved = {"exists": False}

        if saved.get("exists"):
            with st.spinner(f"Loading saved plan for {selected_client}..."):
                flat_plan, day_types = flatten_api_solution(
                    saved.get("solution", {}),
                )
                st.session_state.plan = flat_plan
                st.session_state.plan_dates = sorted(flat_plan.keys())
                st.session_state.day_types = day_types
                st.session_state.client_name = selected_client
                st.session_state.changes_log = []
                st.session_state.pool_warnings = []
                st.session_state.plan_source = "history"
                st.session_state.rule_diagnostics = []
                st.session_state.diagnostics_summary = None
                st.rerun()
        else:
            with st.spinner(f"Generating plan for {selected_client}..."):
                try:
                    result = client.plan(
                        client_name=selected_client,
                        start_date=start_date.isoformat(),
                        num_days=num_days,
                        time_limit_seconds=_planning_time_limit(num_days),
                    )
                    flat_plan, day_types = flatten_api_solution(result.get("solution", {}))
                    st.session_state.plan = flat_plan
                    st.session_state.plan_dates = sorted(flat_plan.keys())
                    st.session_state.day_types = day_types
                    st.session_state.client_name = selected_client
                    st.session_state.changes_log = []
                    st.session_state.pool_warnings = result.get("pool_warnings", [])
                    st.session_state.plan_source = "solver"
                    # Pre-flight diagnostics from the solver path —
                    # may be empty, may carry warnings / info entries
                    # that should still show in the expander.
                    st.session_state.rule_diagnostics = (
                        result.get("rule_diagnostics") or []
                    )
                    st.session_state.diagnostics_summary = (
                        result.get("summary")
                    )
                    st.rerun()
                except RuleDiagnosticsBlockedError as e:
                    # Pre-flight gate fired. Stash the structured
                    # diagnostics and show the expander on the next
                    # render — no plan table, badge = preflight_blocked.
                    st.session_state.plan = None
                    st.session_state.plan_dates = []
                    st.session_state.day_types = {}
                    st.session_state.client_name = selected_client
                    st.session_state.changes_log = []
                    st.session_state.pool_warnings = []
                    st.session_state.plan_source = "preflight_blocked"
                    st.session_state.rule_diagnostics = e.diagnostics or []
                    st.session_state.diagnostics_summary = e.summary or None
                    st.rerun()
                except (ConnectionError, OSError, ValueError, RuntimeError) as e:
                    st.error(f"Generation failed: {e}")
    else:
        st.warning("Select a valid client first.")

# ---------------------------------------------------------------------------
# Display plan
# ---------------------------------------------------------------------------
plan = st.session_state.plan
plan_dates = st.session_state.plan_dates

# Diagnostics expander renders above the plan table (or instead of the
# empty state, when pre-flight blocked the solver). Reads the stashed
# rule_diagnostics + summary from the most recent /plan response or
# RuleDiagnosticsBlockedError.
_render_diagnostics_expander(
    st.session_state.get("rule_diagnostics") or [],
    st.session_state.get("diagnostics_summary"),
)

# Pre-flight-blocked: no plan rendered, but a clear CTA + the
# diagnostics expander already rendered above.
if (
    not plan
    and st.session_state.get("plan_source") == "preflight_blocked"
):
    st.warning(
        "Pre-flight diagnostics found a guaranteed failure for these "
        "dates. Fix the issues above (or change the dates / client) "
        "and try again."
    )
    st.stop()

if plan and plan_dates:
    if st.session_state.get('save_success_msg'):
        # Legacy session_state key for sessions that pre-date the toast
        # switch; remove it without rerunning so old sessions clear out.
        st.session_state.pop('save_success_msg', None)

    all_slots = set()
    for date_str in plan_dates:
        all_slots.update(plan.get(date_str, {}).keys())
    sorted_slots = sorted(all_slots, key=slot_sort_key)

    total_items = sum(1 for d in plan_dates for s in sorted_slots
                      if plan.get(d, {}).get(s, ""))

    st.markdown(f"""<div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-label">Client</div>
            <div class="metric-value">{html.escape(st.session_state.client_name or "")}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Days</div>
            <div class="metric-value">{len(plan_dates)}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Slots per day</div>
            <div class="metric-value">{len(sorted_slots)}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Total items</div>
            <div class="metric-value">{total_items}</div>
        </div>
    </div>""", unsafe_allow_html=True)

    if st.session_state.pool_warnings:
        with st.expander(f"Pool warnings ({len(st.session_state.pool_warnings)})", expanded=False):
            for w in st.session_state.pool_warnings:
                st.markdown(f'<div class="pool-warn-bar">&#9888; {html.escape(str(w))}</div>', unsafe_allow_html=True)

    # Menu table.
    # Wrapped because the ISO date parse on session_state values
    # (plan_dates) is the most likely place for stale / corrupted
    # state to crash a rerun. If it throws, we'd rather show a clean
    # error + a one-click recovery than render half a table.
    try:
        _day_types = st.session_state.day_types
        header_html = '<tr><th>Slot</th>'
        for d_str in plan_dates:
            d = dt.date.fromisoformat(d_str)
            day_type = _day_types.get(d_str, "")
            bg, fg = THEME_TAG_COLORS.get(day_type, ("#27272a", "#71717a"))
            icon = THEME_ICONS.get(day_type, "")
            label = day_type.replace("_", " ").title() if day_type else ""
            header_html += (
                f'<th><span class="day-label">{d.strftime("%a %d %b")}</span>'
                f'<span class="theme-tag" style="background:{bg};color:{fg};">'
                f'{icon} {label}</span></th>')
        header_html += '</tr>'

        body_html = ''
        for slot_id in sorted_slots:
            body_html += f'<tr><td>{display_label_for_slot_id(slot_id)}</td>'
            for d_str in plan_dates:
                raw_item = plan.get(d_str, {}).get(slot_id, "")
                body_html += f'<td>{format_item_html(raw_item)}</td>'
            body_html += '</tr>'

        st.markdown(
            f'<div class="menu-table-wrap"><table class="menu-table">'
            f'<thead>{header_html}</thead>'
            f'<tbody>{body_html}</tbody></table></div>',
            unsafe_allow_html=True)
    except Exception as _exc:
        logger.exception("Failed to render menu table")
        st.error(
            "Couldn't render the saved plan — the stored data may be "
            "from an older version. Click below to clear it and start "
            "fresh."
        )
        if st.button("Clear plan and reload", key="err_clear_plan"):
            for _k in ("plan", "plan_dates", "day_types", "pool_warnings",
                       "client_name", "changes_log"):
                st.session_state.pop(_k, None)
            st.rerun()

    st.markdown("")

    # Action buttons
    c1, c2, c3, _ = st.columns([1, 1, 1, 3])
    with c1:
        if st.button("Save to History", key="planner_save_btn",
                     use_container_width=True):
            try:
                client.save(client_name=st.session_state.client_name,
                            week_plan=plan, week_start=plan_dates[0])
                # After Save, what's on screen now matches what's in the
                # DB — flip the source badge so the user can tell the
                # plan is persisted (and the next Generate for these
                # dates will replay this saved version, not run the
                # solver again).
                st.session_state.plan_source = "history"
                # Toast skips the full-page rerun — visibly faster than
                # the previous "set flag, rerun, render st.success, pop"
                # round-trip. The icon must be a real emoji ("✓" U+2713
                # is a dingbat, not an emoji — Streamlit's
                # validate_emoji rejects it with a StreamlitAPIException
                # on newer Streamlit / Python 3.14 builds).
                st.toast("Plan saved to history", icon="✅")
            except (ConnectionError, OSError, ValueError, RuntimeError) as e:
                st.error(f"Save failed: {e}")
    with c2:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Slot"] + plan_dates)
        for slot_id in sorted_slots:
            row = [display_label_for_slot_id(slot_id)]
            for d_str in plan_dates:
                row.append(format_item_for_ui(plan.get(d_str, {}).get(slot_id, "")))
            writer.writerow(row)
        st.download_button("Download CSV", data=buf.getvalue(),
            file_name=f"menu_{st.session_state.client_name}.csv",
            mime="text/csv", key="planner_download_csv_btn",
            use_container_width=True)
    with c3:
        if st.button("Clear", key="planner_clear_btn", use_container_width=True):
            st.session_state.plan = None
            st.session_state.plan_dates = []
            st.session_state.changes_log = []
            st.session_state.plan_source = None
            st.session_state.rule_diagnostics = []
            st.session_state.diagnostics_summary = None
            st.rerun()

    # Regeneration
    with st.expander("Regenerate cells"):
        st.caption("Pick slots to replace with fresh items.")

        # Show current item alongside slot id so the picker is readable
        # ("Veg Dry — Aloo Gobi") instead of just the slot label.
        def _slot_with_current(d_str: str):
            day_map = plan.get(d_str, {})
            def _fmt(slot_id: str) -> str:
                slot_label = display_label_for_slot_id(slot_id)
                cur = format_item_for_ui(day_map.get(slot_id, ""))
                return f"{slot_label} — {cur}" if cur else slot_label
            return _fmt

        # Wider columns avoid pill truncation. Past 3 days we wrap into
        # a second row instead of squeezing 5 columns into the panel.
        regen_selections = {}
        regen_cols_per_row = min(len(plan_dates), 3)
        cols = st.columns(regen_cols_per_row)
        for i, d_str in enumerate(plan_dates):
            d = dt.date.fromisoformat(d_str)
            day_type = _day_types.get(d_str, "")
            bg, fg = THEME_TAG_COLORS.get(day_type, ("#27272a", "#71717a"))
            icon = THEME_ICONS.get(day_type, "")
            label = day_type.replace("_", " ").title() if day_type else ""
            col = cols[i % regen_cols_per_row]
            with col:
                st.markdown(
                    f'<div class="regen-day-header">{d.strftime("%a %d %b")} '
                    f'<span class="theme-tag" style="background:{bg};color:{fg};'
                    f'font-size:0.6rem;">{icon} {label}</span></div>',
                    unsafe_allow_html=True)
                day_slots = sorted(plan.get(d_str, {}).keys(), key=slot_sort_key)
                selected = st.multiselect(f"Slots for {d_str}", day_slots,
                    format_func=_slot_with_current(d_str),
                    key=f"regen_{d_str}", label_visibility="collapsed")
                if selected:
                    regen_selections[d_str] = selected

        if st.button("Regenerate Selected", type="primary",
                     key="planner_regenerate_btn"):
            if regen_selections:
                # Snapshot current items for the cells the user picked.
                # We diff against the regenerated plan after the call so
                # the changes log can show "Aloo Gobi -> Bhindi Masala"
                # for each cell that actually changed.
                old_snap = {
                    (d_str, sid): plan.get(d_str, {}).get(sid, "")
                    for d_str, slots in regen_selections.items()
                    for sid in slots
                }
                with st.spinner("Regenerating..."):
                    try:
                        result = client.regenerate(
                            client_name=st.session_state.client_name,
                            base_plan=plan, replace_slots=regen_selections,
                            start_date=plan_dates[0],
                            num_days=len(plan_dates),
                            time_limit_seconds=_planning_time_limit(len(plan_dates)))
                        flat_regen, regen_day_types = flatten_api_solution(result.get("solution", {}))
                        st.session_state.plan = flat_regen if flat_regen else plan
                        if regen_day_types:
                            st.session_state.day_types = regen_day_types
                        st.session_state.plan_dates = sorted(st.session_state.plan.keys())

                        diffs = []
                        for (d_str, sid), old_raw in old_snap.items():
                            new_raw = st.session_state.plan.get(d_str, {}).get(sid, "")
                            old_pretty = format_item_for_ui(old_raw)
                            new_pretty = format_item_for_ui(new_raw)
                            if old_pretty == new_pretty:
                                # Solver picked the same item back — don't
                                # spam the log with no-op rows.
                                continue
                            try:
                                day_label = dt.date.fromisoformat(d_str).strftime("%a %d %b")
                            except ValueError:
                                day_label = d_str
                            diffs.append({
                                "kind": "regen",
                                "day": day_label,
                                "slot": display_label_for_slot_id(sid),
                                "old": old_pretty,
                                "new": new_pretty,
                            })
                        if diffs:
                            st.session_state.changes_log.extend(diffs)
                            # Cell-level regen means the on-screen plan
                            # no longer matches the saved version (if
                            # there was one). Flip to "modified" so the
                            # badge nudges the user to Save again to
                            # persist their edits.
                            st.session_state.plan_source = "modified"
                        else:
                            st.session_state.changes_log.append({
                                "kind": "info",
                                "text": (
                                    f"Regenerated {sum(len(v) for v in regen_selections.values())} "
                                    "cell(s); solver returned the same items."
                                ),
                            })
                            # No actual change — leave plan_source alone so a
                            # no-op regen on a freshly loaded saved plan keeps
                            # the "Loaded from history" badge.
                        st.rerun()
                    except (ConnectionError, OSError, ValueError, RuntimeError) as e:
                        st.error(f"Regeneration failed: {e}")
            else:
                st.warning("Select at least one cell.")

    if st.session_state.changes_log:
        with st.expander("Changes log", expanded=True):
            for entry in st.session_state.changes_log:
                # Entries are dicts (regen diffs / info rows). Strings are
                # tolerated for backward-compat with any session that was
                # alive across the upgrade.
                if isinstance(entry, dict) and entry.get("kind") == "regen":
                    st.markdown(
                        '<div class="log-entry log-diff">'
                        f'<span class="log-day">{html.escape(entry["day"])}</span>'
                        f'<span class="log-sep">&middot;</span>'
                        f'<span class="log-slot">{html.escape(entry["slot"])}</span>'
                        f'<span class="log-sep">&middot;</span>'
                        f'<span class="log-old">{html.escape(entry["old"] or "(empty)")}</span>'
                        '<span class="log-arrow">&rarr;</span>'
                        f'<span class="log-new">{html.escape(entry["new"] or "(empty)")}</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
                    st.markdown(
                        f'<div class="log-entry">{html.escape(text)}</div>',
                        unsafe_allow_html=True,
                    )

else:
    st.markdown("""<div class="empty-state">
        <div class="empty-icon">&#127835;</div>
        <h3>No menu plan yet</h3>
        <p>Select a client and click <b>Generate Menu Plan</b><br>in the sidebar to get started.</p>
    </div>""", unsafe_allow_html=True)
