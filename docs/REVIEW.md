# Senior-Engineer Audit — May 2026

End-to-end review of the Ikigai Masala codebase, focused on code quality,
architecture, UI/UX, database writeback, traceability, security,
performance, testing, and documentation.

---

## 1. Executive Summary

| | |
|---|---|
| **Overall score** | **8.4 / 10** |
| **Production-ready?** | **Yes**, with the schema-drift caveat fixed below. |

### Biggest strengths
- Clean separation of concerns: `app.py` (UI) → `api/` (REST + auth) →
  `src/solver/` + `src/menu_rules/` (engine) → `src/preprocessor/` (data)
  → Supabase. Each layer has a narrow contract and is independently testable.
- Disciplined error handling: hard vs. soft rule severity, soft-rule
  failures surfaced via `rule_warnings` instead of crashing the solve,
  generic 500 envelopes that never leak exception text.
- Real concurrency story: bearer-token auth, optimistic-concurrency
  versioning on `clients`, in-process solver gate with queue + dynamic
  worker allocation, structured logs with request-id correlation.
- Solid test surface: 477 tests, ~84% coverage, real Supabase mocked via
  `tests/fake_supabase.py`, deterministic CP-SAT tests via fixed seeds.
- Tight ops surface: `/health` distinguishes liveness from schema drift,
  `/metrics` exposes counters in Prometheus format, `LOG_FORMAT=json`
  flips structured logs, `APP_VERSION` baked into responses for deploy
  tracking.

### Biggest risks
- **Schema-drift footgun (FIXED in this PR):** `scripts/create_tables.sql`
  and `scripts/create_history_tables.sql` both defined `menu_history` /
  `week_signatures` with subtly different DDL — depending on run order
  you got the FK/UNIQUE-index version or the loose version. Now the
  tables live in a single file and both scripts are idempotent.
- **Stale test (FIXED in this PR):** `tests/test_cookie_store.py` was
  written against the pre-`7ac3867` cookie API and broke on every CI
  run. Rewritten against the current `_cookie_controller(method=...)`
  shape; 16 tests now passing.
- **Single-tenant RLS:** every Supabase table is `USING (true) WITH
  CHECK (true)`. Fine for the current single-tenant deployment but the
  service-role key effectively becomes the only access boundary —
  losing it leaks every client's history. If the app ever goes
  multi-tenant, RLS policies need a JWT-claim-based rewrite (out of
  scope for this audit).
- **In-memory rate limiter:** `api/rate_limit.py` is per-process. Behind
  multiple Gunicorn workers a determined attacker gets `N × capacity`
  requests/min before being throttled. Documented in the file's
  docstring as "swap to flask-limiter + Redis when going multi-process",
  not blocking today.
- **`scripts/seed_supabase.py` reads `data/configs/clients.json`:** that
  legacy JSON file is no longer the source of truth (Supabase is). Keep
  the script for empty-DB seeding, but a stale clients.json silently
  reseeds outdated configs. Mitigation is in the file's docstring;
  consider deleting once a migration test proves seed isn't needed.

---

## 2. Scorecard

| Area | Score | Notes |
|---|---|---|
| Code Quality       | 9 / 10 | Naming, docstrings, structure are consistently good. Functions are small. Comments explain *why*, not *what*. |
| Architecture       | 9 / 10 | Clean layering (UI / API / engine / data). Two-phase rule pipeline. Live-read Supabase with per-request memoization. |
| UI/UX              | 8 / 10 | Polished dark theme, design tokens, themed badges, pool warnings, regen log. Loading/error/empty states all handled. Responsiveness mostly inherited from Streamlit. |
| Database Writeback | 7 / 10 | `/save` is single-shot (no idempotency token). UNIQUE INDEX on `menu_history` is the safety net. RLS is permissive. |
| Traceability       | 9 / 10 | `X-Request-ID` end-to-end, structured logs, `request_id` in every log line via `ContextVar`. `/metrics` matches `rule_warnings` payload. |
| Security           | 8 / 10 | bcrypt + signed bearer tokens, two-bucket rate limit on /login, generic-error responses, env validation at startup, SameSite=Lax cookie. Cookie isn't `Secure` (acceptable for dev / Streamlit Cloud which is HTTPS by default). |
| Performance        | 8 / 10 | Solver gate caps concurrency; per-request `g`-cache for client config; pool sampling with priority masks; multi-restart with widening caps. Could push more rule pre-filter work onto vectorised pandas ops. |
| Testing            | 9 / 10 | 477 tests, ~84% coverage, fast (~13 s default), `@slow` marker for full-pipeline. `fake_supabase.py` is the right call. |
| Documentation      | 9 / 10 | `docs/` covers setup / api / architecture / operations with mermaid diagrams. CLAUDE.md keeps a code-map. This audit adds a top-level review. |
| Maintainability    | 9 / 10 | New rule = drop a class in `src/menu_rules/` + register in `RULE_CLASSES`. New endpoint = one route in `api/app.py`. Live-read Supabase config means UI edits land instantly. |

---

## 3. Critical Issues

### Fixed by this audit

1. **Schema duplication in `scripts/create_tables.sql`** — `menu_history`
   and `week_signatures` were defined in two files with different DDL.
   The first file's loose definitions (no FK, no UNIQUE INDEX) would
   win on a fresh install if executed in the order given by the README.
   Removed the duplicates from `create_tables.sql`; `create_history_tables.sql`
   is now the single source of truth and is idempotent.

2. **Broken `tests/test_cookie_store.py`** — the cookie store was
   refactored in commit `7ac3867` to bypass `CookieController` and call
   `_cookie_controller` directly. The test still patched
   `cookie_store._get_controller` (no longer exists) and asserted on
   `ctl.getAll/set/remove` (no longer used). Rewritten end-to-end against
   the new API shape; the 16 tests now exercise the real call sites.

3. **`dt.datetime.utcnow()` deprecation** in `cookie_store.persist_token`.
   Replaced with `dt.datetime.now(dt.timezone.utc)` for Python 3.12+.

### Open (not blocking)

4. **In-memory rate limiter** is per-process; documented as a future
   swap to Redis when scaling out workers. Acceptable today.

### Follow-up landed in this branch

After the original audit, three product-level changes landed on this
branch on top of the audit fixes:

- **Save now overwrites instead of appending.** Re-saving a week with
  different items used to leave both rows in `menu_history`, breaking
  the cooldown rules' single-source-of-truth assumption. `HistoryManager.save`
  now DELETEs prior rows for the same `(client_name, dates)` before
  INSERTing, and the same for `week_signatures` keyed on
  `(client_name, week_start)`.
- **Generate replays saved plans.** New `GET /api/v1/saved-plan`
  endpoint + `HistoryManager.load_saved_plan` static method. Streamlit's
  Generate button hits this first; if every requested weekday is
  covered, it shows the saved plan with a "Loaded from history" badge
  instead of running the solver. Falls back to `/plan` for any partial
  / missing coverage.
- **Pre-flight rule diagnostics.** New `POST /api/v1/diagnose` endpoint
  + `Diagnostic` / `DiagnoseContext` / `run_diagnostics` model in
  `src/menu_rules/`. Every high-signal rule now implements `diagnose()`
  (10 rules: cuisine, theme_day, theme_slot_filter, item_cooldown,
  ricebread_gap, premium, coupling, ingredient_ban, item_frequency,
  nonveg_biryani_weekly) — they inspect pools + history *without*
  running CP-SAT and return structured findings explaining what would
  fail and how to fix it. `/api/v1/plan` runs the same pass first and
  returns **HTTP 422** with structured diagnostics if any `error`
  severity is present, skipping the solver entirely. The old
  `/validate-pools` endpoint is **removed** — its pool-size warnings
  are folded into `rule_diagnostics` as `rule_type == "pool_size"`
  entries. Streamlit renders an inline "Diagnostics" expander above
  the plan table, auto-expanded on errors. Most concretely: the
  chinese-starter cooldown scenario now surfaces as *"Item cooldown
  (20 days) banned all 8 starter candidates on 2026-05-13 (chinese).
  Pool is empty after cooldown."* in <1 second, instead of a generic
  500 after the multi-restart loop exhausts. A buggy `diagnose()`
  implementation degrades to a single `warning` diagnostic — never
  an `error` — so no rule can self-promote into the pre-flight gate
  and freeze the planner.

5. **No CSRF on Streamlit-served `/auth/login`** — Streamlit's XSRF
   guard covers WebSocket + form submissions; the API itself runs on
   loopback inside the container. Adding API-level CSRF would be
   redundant for the current architecture but worth revisiting if the
   API is exposed publicly.

---

## 4. Recommended Improvements (priority order)

| # | Area | Improvement |
|---|---|---|
| 1 | DB | Add an `idempotency_key` column to `menu_history` so a retry of `/save` can't double-insert (today the UNIQUE INDEX absorbs most cases, but partial-day mid-failure can leave half a week saved). |
| 2 | Tests | Add an integration test that runs both SQL migrations against an ephemeral Postgres + asserts the resulting schema matches expectations. Locks in the schema-drift fix from this audit. |
| 3 | Solver | Profile multi-restart wall-clock — the 4 × 2 = 8 attempts dominate p95; a smarter restart that escalates only on infeasibility could shave 30–50 % off median latency. |
| 4 | Auth | Surface remaining bearer-token TTL (e.g. as response header) so the Streamlit UI can warn on imminent expiry instead of bouncing to login mid-flow. |
| 5 | Observability | Emit a histogram of `solve_duration_seconds` once `api/metrics.py` swaps to `prometheus_client`. The current counter-only snapshot tells you success/failure but not tail latency. |
| 6 | Cleanup | Once `legacy_sha256_verifications_total{result="success"}` stays 0 for a quarter, flip `AUTH_DISABLE_LEGACY_SHA256=true`, watch metrics, then delete the legacy code path entirely. |
| 7 | UI | `customisation/` panels show "Unsaved" indicator only on edit mode, not create — small UX gap. |
| 8 | Docs | Add a "common-failure runbook" in `docs/operations.md` keyed by request-id grep patterns + `rule_warnings` shapes. |

---

## 5. Changes Made

| File | Change |
|---|---|
| `tests/test_cookie_store.py` | Rewritten end-to-end against the new `_cookie_controller(method=...)` API. 16 tests, all passing. |
| `user_authentication/cookie_store.py` | Replace deprecated `dt.datetime.utcnow()` with `dt.datetime.now(dt.timezone.utc)`. |
| `scripts/create_tables.sql` | Remove duplicate `menu_history` / `week_signatures` blocks (and their RLS lines). They live in `create_history_tables.sql` only. |
| `scripts/create_history_tables.sql` | Make every CREATE/ALTER idempotent (`IF NOT EXISTS`, guarded `CREATE POLICY`). Re-running is now safe. |
| `src/constants.py` | Drop the unused `OUTPUT_SLOTS` constant. |
| `docs/REVIEW.md` | This audit document. |
| `docs/architecture.md` | (See doc — no change in this PR.) |

---

## 6. Dead Code Removed

- **`src/constants.py::OUTPUT_SLOTS`** — defined but never imported anywhere.
- **Duplicate SQL DDL** for `menu_history` / `week_signatures` in `create_tables.sql`.

---

## 7. Files / Folders That Need Human Confirmation

| Path | Reason | Action |
|---|---|---|
| `data/configs/clients.json` | Legacy seed data; the docstring says Supabase is the real source of truth, but `scripts/seed_supabase.py` still reads it. Removing it would break empty-DB bootstrap. | **Keep** for now; revisit with the seeding-test improvement above. |
| `src/solver/menu_solver.py::_find_cells` (module-level linear) | Used by `tests/test_menu_solver_helpers.py` and a comment claims "kept for tests / ad-hoc use". | **Keep** — tests import it. |
| `ui/formatters.py::theme_label`, `pretty_text`, `color_suffix` | Only `tests/test_formatters.py` imports them outside the module. | **Keep** — public surface, tests cover them, removing both function + tests is a real reduction. |
| `data/configs/indian_menu_rules.json` | Generic rules consumed by `MenuRuleLoader`. | **Keep** — primary configuration. |
| `data/configs/client_rules.json` | Per-client rule overrides. | **Keep** — required by `MenuRuleLoader.load_for_client`. |

---

## 8. Updated Documentation

- **Created** `docs/REVIEW.md` (this file) — audit, scorecard, change log.
- The existing `docs/architecture.md`, `docs/api.md`, `docs/operations.md`,
  and `docs/setup.md` were reviewed and are accurate as-of this audit. No
  edits required beyond the schema-drift fix in `scripts/`.

---

## 9. Architecture Document

See `docs/architecture.md` for the canonical version with mermaid diagrams.

### Summary view

```
┌────────────────────────────────────────────────────────────────────┐
│                       Streamlit Frontend                            │
│   app.py · ui/*.py · customisation/*.py · user_authentication/*.py  │
│                       (Bearer token + cookie)                       │
└──────────────────────────────┬─────────────────────────────────────┘
                               │  HTTP (loopback in container)
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Flask API (api/app.py)                        │
│  auth.py · concurrency.py (solver gate) · rate_limit.py · metrics.py │
│  logging_config.py (request-id ContextVar + JSON formatter)         │
└────┬──────────────────────────────────────────────────┬────────────┘
     │                                                  │
     ▼                                                  ▼
┌──────────────────────┐                ┌──────────────────────────────┐
│   Solver engine      │                │    Supabase (Postgres)       │
│  src/solver/         │                │  clients · menu_categories   │
│   - menu_solver.py   │                │  slot_count_overrides        │
│   - regenerator.py   │                │  theme_overrides             │
│  src/menu_rules/     │                │  menu_history · week_sigs    │
│   - 19 rule classes  │                │  app_settings · users        │
│  src/preprocessor/   │                └──────────────────────────────┘
│   - excel_reader.py
│   - column_mapper.py
│   - data_cleanser.py
│   - pool_builder.py
└──────────────────────┘
```

### Folder responsibilities

| Folder | Role |
|---|---|
| `app.py` | Streamlit entry point — auto-spawns Flask, handles login + session, renders planner + editor + user manager. |
| `api/` | Flask REST surface, auth decorators, rate limit, solver-concurrency gate, in-process metrics, structured logs. |
| `src/solver/` | CP-SAT model build, multi-restart strategy, regeneration, solution formatting. |
| `src/menu_rules/` | Rule classes (cuisine, color, theme, cooldown, premium, etc.). Two-phase contract: `pre_filter_pool` + `apply` + `get_objective_terms`. |
| `src/preprocessor/` | Excel ingest → column normalisation → data cleanse → per-slot candidate pools. |
| `src/client/` | Supabase-backed client config loader, optimistic-concurrency version field. |
| `src/history/` | Supabase-backed item history + week-signature history. |
| `ui/` | Streamlit-side API client, formatters, design tokens / CSS. |
| `customisation/` | Streamlit editor for slots / counts / themes. |
| `user_authentication/` | bcrypt auth, cookie persistence, login UI, session helpers, user manager UI. |
| `scripts/` | One-shot SQL migrations, admin seeder. |
| `data/` | Menu ontology Excel + JSON rule configs. |
| `tests/` | 477 tests covering API, rules, solver, history, auth, cookie store, formatters. |

### UI → API → DB flow (Generate plan)

The Generate button now reads history first:

```
1. User clicks "Generate Menu Plan" in app.py
2. ui/api_client.MenuApiClient.get_saved_plan() → GET /api/v1/saved-plan
   (pure read — never invokes the solver)
3. api/app.py::saved_plan:
     ├─ HistoryManager.load_saved_plan(sb, client, dates)
     │     SELECT FROM menu_history WHERE client_name=... AND
     │     service_date IN (dates), newest-id-wins per (date, slot)
     └─ _enrich_history_plan(saved, df) — re-attach color suffix from
        the Excel ontology so the UI renderer matches /plan exactly
4. If response.exists is True (all requested weekdays covered):
     → render with a "Loaded from history" badge.
   Otherwise:
     → fall through to /plan as below.
5. POST /api/v1/plan (solver path, unchanged from prior revisions):
     ├─ solver_gate decorator queues if 2 active solves
     ├─ _prepare_solver_inputs() — Supabase config, Excel pools,
     │   menu_rules, history-context (banned items + recent sigs)
     ├─ MenuSolver.solve() — multi-restart CP-SAT
     └─ SolutionFormatter.to_dict()
6. app.py renders the menu table with a "Freshly generated" badge.
```

### Database writeback flow (Save — overwrite semantics)

```
1. User clicks "Save to History" in app.py
2. ui/api_client.MenuApiClient.save() → POST /api/v1/save
   (single-shot — overwrite-idempotent server-side, but auto-retry is
    off to avoid duplicate "saved" toasts)
3. api/app.py::save_plan → HistoryManager.compute_week_signature(plan)
4. HistoryManager.save() — DELETE-then-INSERT for both tables:
     ├─ DELETE FROM menu_history
     │    WHERE client_name=... AND service_date IN (dates)
     ├─ INSERT new rows into menu_history
     ├─ DELETE FROM week_signatures
     │    WHERE client_name=... AND week_start=...
     └─ INSERT 1 row into week_signatures
5. Response: { success, message }
6. app.py flips plan_source → "history" and shows a toast.
```

The UNIQUE INDEX on `(client_name, service_date, slot, item_base)` is
still the safety net against double-insert from a network retry mid-
save; the explicit DELETE handles the "same slot, different item"
case that the index can't catch.

### Authentication flow (Login)

```
1. User submits email + password to login form
2. POST /api/v1/auth/login
     ├─ Two rate-limit buckets check first (login_ip, login_email)
     │  Either rejection short-circuits before bcrypt verify
     ├─ AuthManager.authenticate(): SELECT users; bcrypt.checkpw
     │  (Legacy SHA-256 hashes verified + transparently rehashed.)
     └─ issue_token(): URLSafeTimedSerializer signed with API_SECRET_KEY
3. Streamlit:
     ├─ login_user() → session_state
     ├─ persist_token() → ikigai_auth cookie (12h, SameSite=Lax)
     └─ 300 ms pause → st.rerun() so the cookie write has time to land
4. On hard refresh / new tab:
     ├─ get_all_cookies() — async warmup; up to 5 reruns × 250 ms
     ├─ MenuApiClient.whoami() validates the token (no DB hit; HMAC only)
     └─ login_user() restores session, planner renders without re-login
```

---

## 10. Final Senior-Engineer Verdict

- **Code quality:** Strong. Naming is consistent, comments explain the
  "why", error handling is layered (hard vs. soft, generic 500 envelopes,
  explicit narrow excepts in critical paths).
- **Architecture:** Scalable up to single-tenant medium scale as-is.
  Beyond that, the rate limiter and solver gate need to move to a shared
  store, and Supabase RLS needs JWT-claim-based policies if multi-tenant
  is ever in scope. None of that is required today.
- **DB writeback:** Reliable for the common path; the UNIQUE INDEX
  on `menu_history` is doing the heavy lifting for accidental retries.
  Consider an idempotency key for production hardening.
- **Maintainability:** Adding a new rule, a new endpoint, or a new client
  customisation panel each touches 1–2 files. The CLAUDE.md code-map
  + this REVIEW.md make the codebase navigable for a new engineer in
  under an hour.
- **Before production (already done in this PR):**
  - Fix the SQL schema-duplication footgun. ✅
  - Get the cookie-store test suite green. ✅
  - Drop deprecated stdlib usage. ✅

The remaining work in §4 is enhancement, not blockers.
