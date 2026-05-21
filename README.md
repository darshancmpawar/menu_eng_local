# Ikigai Masala

Constraint-based weekly menu planner for corporate meal providers. Generates
Indian menus that respect cuisine themes, item cooldowns, color variety,
per-client customizations, and history.

- **Frontend:** Streamlit
- **Backend:** Flask API (auto-started by Streamlit on port 5000)
- **Solver:** Google OR-Tools CP-SAT
- **Database:** Supabase (PostgreSQL) — clients, users, history, config
- **Auth:** bcrypt passwords + signed bearer tokens between Streamlit and Flask

---

## Quick start (Docker — recommended)

```bash
cp .env.example .env       # edit and fill in SUPABASE_URL / SUPABASE_KEY / API_SECRET_KEY
docker compose up --build
```

Open `http://localhost:8501`, log in, pick a client, generate a plan.

> First-time setup: run the three schema files (`scripts/*.sql`) once in
> the Supabase SQL editor, and seed an admin via the Python script — see
> [docs/setup.md](docs/setup.md).

## Quick start (local Python)

```bash
cd ikigai_masala-main
pip install -r requirements-dev.txt

# one-time in the Supabase SQL editor:
#   scripts/create_tables.sql, create_history_tables.sql, create_users_table.sql

cat > .streamlit/secrets.toml <<EOF
SUPABASE_URL   = "https://<your-project>.supabase.co"
SUPABASE_KEY   = "<service_role key>"
API_SECRET_KEY = "$(python -c 'import secrets; print(secrets.token_hex(32))')"
EOF

ADMIN_EMAIL="you@company.com" ADMIN_PASSWORD="<≥8 chars>" \
  python scripts/seed_admin.py

streamlit run app.py
```

---

## Documentation

- [docs/setup.md](docs/setup.md) — prerequisites, install, secrets, seed,
  every env var the app reads.
- [docs/architecture.md](docs/architecture.md) — system diagram, layer
  overview, design choices, login / plan / save / regenerate sequence
  diagrams.
- [docs/api.md](docs/api.md) — endpoint table, response shapes (plan,
  health, metrics), concurrency semantics, rules reference, data model,
  output formats.
- [docs/operations.md](docs/operations.md) — testing, CI, structured
  logs + metrics, legacy-hash kill-switch playbook, troubleshooting table,
  project layout.

For a file-level symbol map optimised for Claude Code sessions, see
[`../CLAUDE.md`](../CLAUDE.md).

---

## Tests

```bash
pytest                # default (skips @slow)
pytest -m slow        # real-Excel full-pipeline tests
```

CI runs pytest + `ruff check --select=F,E9` + `bandit -ll` on every PR;
the slow suite runs on push-to-main and manual dispatch. See
[docs/operations.md](docs/operations.md#ci).
