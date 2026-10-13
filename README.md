# Jaro Accounting — website rebuild

From-scratch custom rebuild of [jaroaccounting.com](https://jaroaccounting.com)
(accounting firm, owner Jared Robbins, New Westminster BC). **Not WordPress.**

Static marketing pages + a Python/FastAPI API + PostgreSQL (system of
record) + a single background worker (transactional email outbox with
lease-claim) + a transactional email provider. Three mutating surfaces:
the contact/lead pipeline, the booking widget for the free 30-minute
consultation, and the invitation-only client portal (dashboard, document
vault, filing visibility, invoices) with Jared's owner console.

**No public signup anywhere** — Jared governs every account via 7-day
single-use invitation tokens. No online payments in v1.

## Stack

| Layer | Technology |
|---|---|
| Marketing pages | Vanilla HTML/CSS/JS, stdlib-only Python build script (`frontend/build.py` → `frontend/dist/`), served by nginx |
| API | FastAPI (sync SQLAlchemy), canonical prefixes `/api/v1`, `/api/v1/admin/*`, `/api/v1/portal/*`, `/api/v1/booking/*` |
| Database | PostgreSQL (deploy) / SQLite (local verification); linear idempotent Alembic migrations |
| Background worker | Single process, `python -m app.worker_main`: email outbox (lease-claim), calendar sync, expiry sweeps |
| Mail | Capture mode in non-prod; SMTP behind the explicit `MAIL_ALLOW_SEND` gate |
| Secrets | `NAME` xor `NAME_FILE` (H3/H4); three DB roles (`jaro_web`, `jaro_worker`, `jaro_migrate`) |

Key design rules (see `DECISIONS.md`): A1 error envelope on every route,
closed Pydantic models (`extra="forbid"`), route-owned transactions,
outbox as the only queue, honest status vocabulary (`queued`/`captured` —
never "sent"), fail-closed everywhere (budgets, calendar, startup).

## How to run locally

Prerequisites: Python 3.12 and the provided virtualenv at
`/home/hatch/workspace/jaroaccounting-rebuild/.venv` (all dependencies
installed; do not recreate it).

```bash
cd /home/hatch/workspace/jaroaccounting-rebuild

# 1. Backend env (copy and fill in; never commit a real .env)
cp backend/.env.example backend/.env
# Minimum for local dev: JARO_MODE=development, JARO_WORKLOAD=web,
# DB_ROLE=jaro_web, DATABASE_URL=<redacted>
# SECRET_KEY=<random 32+ chars>, TOTP_KEK=<base64 32 bytes>,
# MAIL_ALLOW_SEND=false, MAIL_BACKEND=capture

# 2. Migrate (H5: migration-first — the one-shot jaro_migrate job runs this
#    before any app traffic; the app never migrates itself)
cd backend && DATABASE_URL=<redacted> ../.venv/bin/alembic upgrade head

# 3. Serve the API (factory entry point; import never boots — H2)
../.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

# 4. Worker (separate terminal; workload identity required)
JARO_WORKLOAD=worker DB_ROLE=jaro_worker ../.venv/bin/python -m app.worker_main

# 5. Build the static frontend
cd ../frontend && ../.venv/bin/python build.py   # src/ -> dist/

# 6. Serve dist/ (dev stand-in for nginx)
cd dist && python3 -m http.server 8080
# Production: ops/nginx.conf (static dist/ + /api/ -> uvicorn)
```

Probes: `GET /healthz` (liveness), `GET /readyz` (DB + outbox-claim probe).

**First owner login:** set `OWNER_INITIAL_PASSWORD`, then
`POST /api/v1/admin/bootstrap` (one-shot; second call → `410`). See
`docs/OWNER_ONBOARDING.md` for Jared's full walkthrough.

## How to run tests

```bash
cd /home/hatch/workspace/jaroaccounting-rebuild
./.venv/bin/pytest backend/tests/ -q          # backend (disposable sqlite)
./.venv/bin/pytest frontend/tests/test_build.py frontend/tests/test_pages.py -q  # static
./.venv/bin/pytest frontend/tests/test_contact_journey.py \
  frontend/tests/test_booking_journey.py \
  frontend/tests/test_portal_journey.py -q    # Playwright journeys
```

Playwright reuses Chromium at `~/workspace/pw-browsers/chromium-1243`
(launched via `executable_path` with `--no-proxy-server`; do not
re-download). The full verification record — exact counts on 3 consecutive
runs — is in `BUILD_REPORT.md`.

**G1 safety gate:** the suite runs only against disposable SQLite files. If
`DATABASE_URL` is set to anything non-sqlite, the session refuses to start.

## Capture mode (H1)

Non-prod never sends real email: `MAIL_ALLOW_SEND=false` +
`MAIL_BACKEND=capture` renders every message to an inspectable JSON
artifact in `MAIL_CAPTURE_DIR` instead of delivering it. Tests assert on
captured artifacts and DB state, never on delivery. Real sending requires
the explicit opt-in (`MAIL_ALLOW_SEND=true` AND `MAIL_BACKEND=smtp`,
credentials from the secret store) — and the app refuses to boot if the
gate is unset or misspelled. **No live email has ever been sent from this
build.**

## Project structure

```
.
├── README.md                  # this file
├── BUILD_REPORT.md            # per-phase build record + verification counts
├── DECISIONS.md               # consolidated decision log (complete record)
├── backend/
│   ├── app/                   # FastAPI app (routes, outbox, worker, auth, portal…)
│   │   └── worker_main.py     # `python -m app.worker_main` entry point
│   ├── alembic/versions/      # linear idempotent migrations (0001–0013)
│   ├── tests/                 # pytest suite (G1-gated, disposable sqlite)
│   ├── .env.example           # every key consumed by app/config.py (G3-tested)
│   └── README.md              # backend run/test reference
├── frontend/
│   ├── src/                   # pages, partials, css, js (build inputs)
│   ├── build.py               # stdlib-only assembler: src/ -> dist/
│   ├── dist/                  # generated output (never hand-edit)
│   └── tests/                 # static assertions + Playwright journeys
├── ops/
│   ├── nginx.conf             # production edge config
│   ├── runbook.md             # backup/restore, alerting, releases (H6)
│   ├── deploy-checklist.md    # G6 pre-flight + smoke
│   └── cutover.md             # WordPress cutover plan (document only)
└── docs/
    └── OWNER_ONBOARDING.md    # Jared's hands-on walkthrough
```

## Docs & decisions

- `DECISIONS.md` — every assumption/deviation, per phase (consolidated).
- `backend/DECISIONS-backend.md`, `frontend/DECISIONS-frontend.md` — the
  detailed per-workstream logs.
- `docs/OWNER_ONBOARDING.md` — Jared's checkable procedures.
- `ops/runbook.md`, `ops/cutover.md`, `ops/deploy-checklist.md` — operations.
- Source research: `../jaroaccounting-replication/` (spec, implementation
  plan, mined platform patterns) — read-only reference, not part of this
  repo's runtime.
