# Operations

Day-2 stuff: how to test, how to debug a broken deploy, how to roll the
legacy-hash kill switch, and what CI does.

---

## Testing

```bash
pytest                                    # default: fast unit + integration, skips @slow
pytest -v                                 # verbose
pytest tests/test_solver.py               # one file
pytest -m slow                            # only the real-Excel / full-pipeline tests
pytest -m ""                              # everything, including slow
pytest --cov=src --cov=api --cov-report=term-missing
```

Markers (defined in `pytest.ini`):

- `slow` — real Excel, real rules, multi-day solve. Skipped by default.
- `unit`, `integration` — available for future marking.

### Fixtures of note

Defined in `tests/conftest.py`:

- `project_root_path`, `sample_data_path`, `ensure_sample_data_exists`
- `fake_supabase` — installs an in-memory fake as the Supabase client so
  API and solver tests run without a live database.

---

## CI

`.github/workflows/ci.yml` runs three jobs in parallel on every PR:

- `pytest` — full suite (slow tests skipped).
- `ruff check --select=F,E9` — real-bug ruleset (undefined names, syntax
  errors, unused imports). Style rules are intentionally out of scope for
  now.
- `bandit -ll -r api src user_authentication scripts` — medium+ severity
  security findings.

A fourth job — `slow-tests` — runs only on push to `main` and manual
`workflow_dispatch` triggers, so PR feedback stays fast.

### Coverage gate

The `pytest` CI job enforces `--cov-fail-under=82`. Configuration lives in
`.coveragerc`: measured surface is `api/`, `src/`, `ui/`,
`user_authentication/`, and the Streamlit-UI modules that can't be
unit-tested (`ui/styles.py`, `user_authentication/login_ui.py`,
`user_authentication/session.py`, `user_authentication/user_manager_ui.py`,
`customisation/*`, `app.py`) are omitted. Current baseline ≈ 83.8%.

Local runs stay plain `pytest` (no coverage) for fast iteration. Measure
coverage locally the same way CI does:

```bash
pytest --cov --cov-report=term-missing
```

When a PR durably raises the baseline, bump the floor in
`.github/workflows/ci.yml` as part of the same change so the gate keeps
progressing upward instead of re-settling at the old number.

---

## Logs + metrics

### Structured logs

Set `LOG_FORMAT=json` to emit one JSON line per log record. Every line
carries `ts`, `level`, `logger`, `msg`, `request_id`, plus any caller-
supplied `extra=` fields. The API's access log lands on `logger="api.app"`
with `msg="http_request"` and fields `method`, `path`, `status`,
`duration_ms`, `user`, `remote_addr`.

Successful `/health` requests are intentionally quiet; failing ones show up.

### Metrics

`GET /api/v1/metrics` (auth-gated) returns an in-process counter snapshot.
Counters populated by the API:

- `plan_requests_total{outcome="success"|"solver_error"}`
- `regenerate_requests_total{outcome="success"|"solver_error"}`
- `solver_failures_total`
- `rule_failures_total{rule=...}`
- `legacy_sha256_verifications_total{result="success"|"fail"|"disabled"}`
- `auth_legacy_upgrades_total{outcome="success"|"fail"}`

Counters reset only on process restart. No histograms or gauges yet —
`api/metrics.py` is deliberately tiny so a future swap to
`prometheus_client` / statsd stays a one-file change.

---

## Persistent login (cookie)

`st.session_state` is per-Streamlit-session in-memory storage — it dies
on hard refresh, new tab, or server restart, which would log every user
back out repeatedly. To keep users signed in across those events the
bearer token is persisted as a 12-hour browser cookie named `ikigai_auth`
via `streamlit-cookies-controller`'s `CookieController`.

`extra-streamlit-components` was deliberately avoided: its
`CookieManager` writes cookies via `document.cookie` inside a sandboxed
component iframe served from a different origin (`qjmnz4vd2y0...`), so
the cookie lands on the iframe's origin and the browser never sends it
back to the main app. `streamlit-cookies-controller` uses `postMessage`
to ask the *parent* window to set the cookie, so it lands on the correct
origin.

### Cookie write (login flow)

`login_ui.py` calls `persist_token(token)` after a successful login,
then waits **300 ms** before calling `st.rerun()`. The pause is required
because `CookieController.set()` is asynchronous — it sends a
`postMessage` to the parent window — and an immediate rerun would abort
the script before the browser receives and persists the cookie.

### Cookie read (page-load restore)

`CookieController.getAll()` returns `{}` on the **first** script run
after mount because the JS→Python handshake hasn't completed yet.
`app.py::_try_restore_session_from_cookie()` handles this with a
one-shot warmup:

1. First run: `getAll()` → `{}` → set warmup flag, sleep 150 ms, `st.rerun()`.
2. Second run (warmup rerun): `getAll()` returns the real cookie dict.

**Library caveat:** `CookieController.__init__` only invokes the
underlying Streamlit component widget (via `_cookie_controller(...)`)
when its `session_state` key is absent. On subsequent runs it takes a
fast cached path, which skips the component call and leaves `getAll()`
returning the stale `{}` default. `cookie_store._get_controller()`
works around this by calling `ctl.refresh()` immediately after
construction whenever the key was already in `session_state` — this
re-invokes the component widget (the first call this run, so no
`DuplicateWidgetID`) and returns whatever value the browser's JS has
posted back since the previous render.

### Restore flow

On page load `app.py::_try_restore_session_from_cookie()`:

1. Reads the cookie via `get_all_cookies()`.
2. If present, calls `GET /api/v1/auth/whoami` to validate the token's
   HMAC signature + TTL — pure server-side check, no Supabase round
   trip (the token carries `email`, `role`, and `profile_name`).
3. If valid: `login_user(...)` populates `session_state` and the user
   sees the authenticated UI immediately.
4. If invalid / expired: the cookie is cleared and the login form is
   shown.

Logout calls `clear_persisted_token()` so a hard refresh after logout
doesn't auto-restore the session.

The cookie is signed by `API_SECRET_KEY` — rotating that key
invalidates every persistent session in flight. That's the right
escape hatch if a token is leaked: rotate, every cookie becomes
useless on next request, all users re-log-in.

## Streamlit-side caches

Two small caches make the planner UI snappy without compromising
freshness:

- `MenuApiClient` is built via `@st.cache_resource` keyed by
  `(backend_url, bearer_token)` — the underlying `requests.Session` (and
  its connection pool) survives across Streamlit reruns instead of
  being torn down on every widget interaction. TTL is 23 hours, just
  under the bearer-token lifetime.
- `list_clients()` is cached with `@st.cache_data` for 60 seconds so
  the sidebar's client picker doesn't hit the API on every rerun. The
  customisation editor's create / delete handlers call
  `st.cache_data.clear()` so a new or removed client shows up
  immediately rather than 60s later.
- `logout_user()` clears both caches so a fresh login can't reuse a
  stale client wired with the previous token.

If a stale picker ever shows up in production despite this, the cause
is almost always a mutation that bypassed `customisation/main.py` —
add a `st.cache_data.clear()` call there or just wait 60s.

---

## Legacy password-hash kill switch

`users.password_hash` currently accepts both bcrypt (current) and a
pre-bcrypt SHA-256 format. The goal is to reach a state where no user
still has a legacy hash, flip the kill switch, and eventually delete the
verification code entirely.

**Playbook:**

1. Watch `legacy_sha256_verifications_total{result="success"}` in
   `/api/v1/metrics`. Every successful login on a legacy hash also triggers
   a warning in the logs.
2. Successful logins transparently rehash to bcrypt —
   `auth_legacy_upgrades_total{outcome="success"}` tracks the drain.
   Failed upgrades (RLS, permission errors) land on `outcome="fail"` and
   log a warning so they don't silently stall.
3. Once the `result="success"` counter stays at 0 long enough to be
   confident, set `AUTH_DISABLE_LEGACY_SHA256=true` in the environment.
   Legacy verifies now return False even for the right password;
   `result="disabled"` counts them so you can see how many users still
   had legacy rows at flip time.
4. If anyone complains, they're a user who never logged in during the
   drain window. Reset their password via the user-management UI. If the
   count stays at 0 for another rotation, cut the PR that deletes
   `_is_legacy_sha256` / `_verify_legacy_sha256`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Process exits at startup with `Missing required environment variables` | `API_SECRET_KEY`, `SUPABASE_URL`, or `SUPABASE_KEY` unset / empty | Set in `.streamlit/secrets.toml` or the container env |
| UI shows `Login error (APIError)` or writes fail silently | `SUPABASE_KEY` is the publishable / anon key, not service-role | Replace with the `sb_secret_...` / service-role key |
| UI shows `Invalid credentials` for a user you just created | Typo in password, or the row was inserted with a malformed hash | Reset via the user-management UI |
| UI shows `Cannot reach API` | Port 5000 in use, or the spawned Flask thread crashed on startup | Kill the stale process; re-run `streamlit run app.py`. Spawned thread crashes also land in the Streamlit server logs. |
| `/health` returns 503 `status=degraded` | `supabase_reachable=false` — network, DNS, or Supabase itself down | Check the Supabase dashboard; verify URL + key |
| Requests fail with httpx `ReadTimeout` after exactly `SUPABASE_TIMEOUT_SECONDS` | Supabase is up but slow on this query | Bump `SUPABASE_TIMEOUT_SECONDS` (default 5) if your DB legitimately needs more time, but first check the Supabase dashboard for query/index pressure |
| `No feasible plan found (INFEASIBLE)` | Over-constrained rules vs available items, or per-client rule config incompatible | Check `pool_warnings` in the response, re-run with logs at INFO; check `/api/v1/metrics` for `rule_failures_total` |
| 503 `Server at capacity` under load | `solver_gate` queue full; this is the intended backpressure | Retry after a few seconds; clients with the built-in retry (`MenuApiClient`) handle this automatically |
| 504 `Request timed out waiting in queue` | Request waited > `QUEUE_TIMEOUT` (default 300s) | Retry; if it persists the solver is stuck — restart the process |
| 409 on `PUT /client-config` | Another admin edited the same client between your GET and PUT | Refresh the editor (the Streamlit UI does this on save failure) and re-apply |
| `Failed to load config for X: Internal server error` in the customisation editor | Logs say `clients.version column missing — falling back to version=1` | Re-run `scripts/create_tables.sql` in the Supabase SQL editor — the Phase 2 #14 migration adds `clients.version`. The editor stays usable in fallback mode, but optimistic-concurrency on PUT is disabled until the column exists. |
| Any `Internal server error` toast in the UI | Generic catch-all wrapped a real exception | Read the response body — every 500 carries a `request_id`. Grep the access log (`logger="api.app", msg="http_request"`) for that id; the matching ERROR line a few rows earlier is the real exception with a traceback. |
| `Widening history lookback from 45 to N days` in logs | A per-client rule's `cooldown_days` > 30 triggered the dynamic widening | Informational. Keeps the Supabase window ≥ the longest rule cooldown. |

---

## Project layout

```
ikigai_masala-main/
├── app.py                    Streamlit entry (spawns Flask)
├── api/
│   ├── app.py                Flask API + request tracing
│   ├── auth.py               Bearer-token signing / verification
│   ├── concurrency.py        Solve gate + worker tuning
│   ├── logging_config.py     dictConfig + JSON formatter + ContextVar request_id
│   ├── metrics.py            In-process counters
│   └── config.py             Path/limit constants + env validation
├── src/
│   ├── db.py                 Supabase singleton
│   ├── constants.py
│   ├── solver/               CP-SAT solver, regenerator, formatter
│   ├── menu_rules/           Rule classes + loader
│   ├── preprocessor/         Excel → pools pipeline
│   ├── client/               ClientConfig(Loader), ConcurrentEditError
│   └── history/              HistoryManager
├── ui/                       API client (with retry) + formatters
├── customisation/            Streamlit editor UIs
├── user_authentication/      Login UI, AuthManager, session helpers
├── data/
│   ├── raw/menu_items.xlsx
│   └── configs/*.json
├── scripts/                  Supabase seeders + SQL schema
├── tests/                    Pytest suite
├── docs/                     setup, architecture, api, operations
├── pytest.ini
├── requirements.txt          runtime
└── requirements-dev.txt      runtime + pytest + ruff + bandit
```

For a file-level symbol map optimised for Claude sessions, see
`../CLAUDE.md` at the repo root.
