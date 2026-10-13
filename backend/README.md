# Jaro Accounting — Backend (Phase 0: Foundation)

FastAPI + SQLAlchemy backend for the jaroaccounting.com rebuild. Phase 0
delivers the reconciled schema (30 tables), the API skeleton under the
canonical `/api/v1`, `/api/v1/admin/*`, `/api/v1/portal/*`,
`/api/v1/booking/*` prefixes, and the shared machinery every later phase
builds on: outbox + lease-claim worker, idempotency, rate budgets, 6-digit
codes, token service, Argon2id, TOTP, session middleware, capture-mode mail,
storage abstraction, and the calendar worker skeleton.

Feature routes (booking flows, portal F1–F4, invitations/auth) are skeleton
`501 not_implemented` in Phase 0 — the honest-vocabulary and envelope
contracts they will honor are already enforced. See
`DECISIONS-backend.md` for every assumption resolved during the build.

## How to run

Prerequisites: Python 3.12 and the provided virtualenv at
`/home/hatch/workspace/jaroaccounting-rebuild/.venv` (all dependencies
installed; do not recreate it).

```bash
cd /home/hatch/workspace/jaroaccounting-rebuild/backend

# 1. Configure (copy and fill in; never commit a real .env)
cp .env.example .env

# 2. Migrate (H5: migration-first — the one-shot jaro_migrate job runs this
#    before any app traffic; the app never migrates itself)
DATABASE_URL="sqlite:///./jaro_dev.db" ../.venv/bin/python -m alembic -c alembic.ini upgrade head

# 3. Serve (factory entry point; import never boots — H2 fail-closed startup)
../.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Probes: `GET /healthz` (liveness, no DB), `GET /readyz` (DB + outbox-claim
probe, single-flight, 5s hard timeout).

The background worker (outbox + calendar sync + expiry sweeps):

```bash
# JARO_WORKLOAD=worker pairs with DB_ROLE=jaro_worker (H2 refuses anything else)
JARO_WORKLOAD=worker DB_ROLE=jaro_worker ../.venv/bin/python -m app.worker_main
# Single cycle (deploy smoke / cron): --once
JARO_WORKLOAD=worker DB_ROLE=jaro_worker ../.venv/bin/python -m app.worker_main --once
```

`app/worker_main.py` is the thin process entry point: settings →
`verify_runtime` → engine → session factory → mail backend →
`Worker.main()`. All reliability behavior (lease-claim, bounded retries,
capture mode) lives in `app/worker.py` / `app/outbox.py`. Tests drive
`Worker.run_once()` directly.

## Tests

```bash
cd /home/hatch/workspace/jaroaccounting-rebuild/backend
../.venv/bin/pytest tests/ -q
```

The suite is organized by the 14 Phase-0 DONE criteria
(`test_01_migrations.py` … `test_14_calendar.py`). All databases are
disposable SQLite files under pytest's tmp dir (G1 safety gate in
`tests/conftest.py` refuses any non-disposable target).

## Layout

```
app/                FastAPI application
  main.py           create_app factory (no module-level app: import never boots)
  config.py         H3/H4 secrets (NAME xor NAME_FILE), explicit mode allowlist
  runtime_check.py  H2 verify_runtime — fail-closed startup
  errors.py         A1 error envelope {code, message, request_id, fields[]}
  db.py             engine/session, UTCDateTime, uuid7, hashing helpers
  models.py         30 tables (canonical reconciled set)
  advisory.py       advisory-lock abstraction (pg_advisory_xact_lock / threading fallback)
  tokens.py         256-bit CSPRNG tokens, SHA-256 storage, constant-time compare
  codes.py          F5 6-digit codes (10-min TTL, atomic consume, 5 attempts)
  passwords.py      Argon2id + common-password blocklist
  totp.py           TOTP (SHA-1, 30s, 6-digit, ±1 step)
  secretbox.py      AES-256-GCM for secrets at rest (TOTP KEK)
  sessions.py       session middleware (__Host- cookie, 15-min sliding, 12h cap)
  rate_limits.py    A5 budgets over rate_windows (4 dimensions, fail-closed 503)
  idempotency.py    A3 scope hash + payload-hash 409 + stored replay
  outbox.py         E1–E4 staging + lease-claim worker cycle
  mail.py           H1 capture backend; MAIL_ALLOW_SEND gate; Phase-0 templates
  storage.py        StorageBackend + local-filesystem driver (local: keys)
  calendar_worker.py one-way calendar READ -> calendar_blocks; staleness gate
  worker.py         single worker: outbox cycle + calendar sync + expiry sweeps
  logging_config.py A6 PII-safe logging (filter + query-stripped access logs)
  routes/           public / booking / portal / admin (canonical prefixes)
alembic/            linear zero-padded idempotent migrations (0001–0007)
tests/              pytest suite, one file per DONE criterion
```

## Configuration

See `.env.example`. Notable gates:

- `JARO_MODE` must be on the allowlist (`development|staging|production|test`).
- `DB_ROLE` must match `JARO_WORKLOAD` (`web→jaro_web`, `worker→jaro_worker`,
  `migrate→jaro_migrate`). Postgres is the deploy target; roles are
  documented, string-checked locally.
- `MAIL_ALLOW_SEND` must be exactly `true`/`false`; real sending additionally
  requires `MAIL_BACKEND=smtp`. Non-prod never sends.
- Boot refuses unless the database is at the pinned Alembic head.

## Deploy notes (H5)

1. Run migrations as the `jaro_migrate` role (one-shot job) before traffic shifts.
2. Code rollback never auto-reverses data; downgrades with live rows in
   `portal_invitations`/`bookings`/`portal_users` refuse (B1).
3. `verify_runtime` runs at every boot; `/readyz` gates traffic.
