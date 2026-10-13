# Jaro Accounting — Operations Runbook (H6)

Last reviewed: 2026-10-06. Owner of this document: the build team until
Jared's onboarding session (Phase 4 DONE #7), then Jared.

Honesty rule (H6): this runbook states plainly what has been drilled and
what has not. **The backup-and-restore procedure below has NOT been
drilled.** It is a tracked open item: owner Jared, target date — the week
after cutover, during the onboarding session. "Assumed but untested"
backups are the top ops risk for a firm whose revenue depends on
consultations.

## 1. Service inventory

| Component | What it is | Process / unit |
|---|---|---|
| Web (API) | `uvicorn app.main:create_app --factory`, workload `web` / role `jaro_web` | systemd `jaro-web.service` (2 workers suggested) |
| Worker | `python -m app.worker_main`, workload `worker` / role `jaro_worker` | systemd `jaro-worker.service` (exactly 1 instance — see §6) |
| Database | PostgreSQL, system of record | managed Postgres or local `postgres` service |
| Storage volume | Document vault + invoice PDFs (`STORAGE_DIR`, local driver v1) | mounted volume, backed up with the DB (see §2) |
| Edge | nginx (`ops/nginx.conf`): static `frontend/dist/`, `/api/` → uvicorn | `nginx` |
| Mail | Transactional provider via SMTP (capture mode in non-prod) | external; no local MTA |

Health: `GET /healthz` (liveness, no DB), `GET /readyz` (DB + outbox-claim
probe, 5s hard timeout, single-flight). The deploy and the load balancer
gate traffic on `/readyz`, restart on `/healthz` failure.

## 2. Backup and restore

### What must be backed up together

1. **Postgres** — full logical dump (`pg_dump --format=custom`) nightly,
   retained 30 days. Point-in-time recovery (WAL archiving) is the v2
   upgrade; the nightly dump is the v1 floor.
2. **The document volume** (`STORAGE_DIR`) — filesystem snapshot or
   `rsync` to the backup host, taken at the same nightly window as the
   dump. The DB stores backend-qualified keys (`local:…`); a DB restore
   without the volume (or vice versa) leaves dangling keys or orphaned
   bytes — **they restore as a pair.**

### Nightly backup (deploy-time values in brackets)

```bash
# Postgres (role: backup; never the app roles)
pg_dump --format=custom -h [PGHOST] -U jaro_backup jaro \
  > /backups/jaro-$(date +%F).dump
# Document volume
rsync -a --delete /srv/jaroaccounting/var/storage/ \
  backup-host:/backups/jaro-storage-$(date +%F)/
```

### Restore procedure (disaster)

1. Stop web + worker (prevent writes mid-restore).
2. `pg_restore --clean -d jaro /backups/jaro-<date>.dump`
3. `rsync` the matching-date storage snapshot back to `STORAGE_DIR`.
4. Run the migration-first check: `alembic upgrade head` (no-op if the
   dump is current; H5 ordering — data shape before code).
5. Start worker, then web; verify `/readyz` 200 and one owner login.

### ⚠ Backup-drill status: NOT DRILLED

**Tracked open item — owner: Jared — target: onboarding session week
(post-cutover).** The drill is: restore the latest dump + storage snapshot
to a scratch host, boot the app against it, and confirm (a) Jared can log
in, (b) one invoice PDF downloads byte-identical, (c) one booking record
renders. Until this row is marked DRILLED with a date, treat every backup
as unverified. This item is also recorded in BUILD_REPORT.md.

## 3. Releasing (H5 migration-first)

1. Build the artifact; pin it by digest (the deploy checklist verifies
   the digest, not a floating tag).
2. **Run migrations FIRST as a one-shot job** with the `jaro_migrate`
   role: `alembic upgrade head`. The job must complete before any new
   code takes traffic. Code rollback never auto-reverses data (downgrades
   with live rows in `portal_invitations`/`bookings`/`portal_users`/
   `*_events` REFUSE by design — B1).
3. Deploy the new web + worker builds; shift traffic.
4. Smoke per `ops/deploy-checklist.md` (7 pages, probes, no dev packages).

If the migration job fails: traffic stays on the old build; the old code
remains compatible because migrations are additive until the new code
ships (the project's migration policy).

## 4. Mail safety (H1 capture-in-staging)

- **Non-prod (dev/staging):** `MAIL_ALLOW_SEND=false`, `MAIL_BACKEND=capture`.
  Mail renders to JSON artifacts in `MAIL_CAPTURE_DIR` — inspectable, never
  delivered. The app refuses to boot if `MAIL_ALLOW_SEND` is unset or
  misspelled (H2); `SmtpMailBackend.send` re-checks the gate (defense in
  depth).
- **Production real sending** requires the explicit opt-in:
  `MAIL_ALLOW_SEND=true` AND `MAIL_BACKEND=smtp` with SMTP creds from the
  secret store (`SMTP_PASSWORD` / `_FILE`, never in the repo or logs).
- **The staging gate test** (`no live send attempted in staging`) is part
  of CI: a dead-provider drill asserts intents park in `retry` with
  backoff and nothing is lost. Before the first production send, verify
  SPF/DKIM/DMARC + envelope-from on the bounce subdomain (cutover doc §4).

## 5. Alerting

### Worker dead-letter (intents stuck `dead`)

A `dead` intent means the worker gave up (digest mismatch, unknown
template, permanent provider rejection) — mail Jared expected to go out
did not. Query every 5 minutes; page on any row:

```sql
SELECT id, kind, scope_id, updated_at
FROM email_outbox
WHERE status = 'dead' AND updated_at > now() - interval '15 minutes';
```

Response: inspect `outbox_attempts` for the intent (numbered evidence,
`evidence_label` tells you why); re-stage with `stage_intent` only after
the cause is fixed. Never hand-edit a frozen snapshot (E2 digest would
kill it anyway).

### 5xx spikes

Alert on the web access log: > 1% 5xx over 5 minutes, or any 5xx on
`/api/v1/booking/*` or `/api/v1/inquiries` (the revenue surfaces).
Every 5xx carries the A1 envelope `{code, message, request_id}` — the
`request_id` is the correlation key into the app logs. Note the A6
discipline: logs never contain emails/phones/bodies/tokens, so the alert
payload is safe to page with.

### Readiness

`/readyz` != 200 for > 2 minutes → investigate (DB or outbox-claim probe
failing). Under injected DB outage the endpoint 503s within the 5s hard
timeout — it fails fast, it does not hang the load balancer.

## 6. Worker operations

- Exactly **one** worker instance. The lease-claim protocol survives a
  second claimant (conditional UPDATE + lease token), but two workers is a
  misconfiguration, not a scaling strategy — alert if two heartbeats
  appear.
- **Worker kill mid-send is a rehearsed non-event** (DONE-8 drill): the
  lease expires, the next cycle re-claims, and the capture/send artifact
  proves no double-send (one artifact per attempt number).
- Restarting the worker is always safe: claims are idempotent, sweeps are
  idempotent, and `run_once()` (`--once`) does a single cycle for cron or
  smoke use.
- Calendar sync failures never crash the loop; they record into
  `calendar_sync_state` and availability fail-closes (503) once staleness
  exceeds 30 minutes (D-31). If availability starts 503ing, check the
  calendar provider credentials first.

## 7. Common incidents

| Symptom | Likely cause | First action |
|---|---|---|
| `/readyz` 503 | DB down or outbox claim failing | Check DB; check `email_outbox` lease rows |
| Booking pages 503 `calendar_unavailable` | Calendar sync stale > 30 min | Check provider creds; `calendar_sync_state.last_error` |
| Logins 503 `budget_unavailable` | `rate_windows` store unreachable | Check DB; budgets fail closed by design (never open) |
| Intents stuck `retry` | Mail provider down | Check provider; nothing is lost — they drain on recovery |
| Session `401 session_expired` waves | Clock skew or 12h absolute cap hit | Expected behavior at the cap; check NTP if widespread |
| Disk growth on storage volume | Uploads | 25 MB/file cap; retire (soft-delete) old client files |

## 8. Log access

App logs: `journalctl -u jaro-web -u jaro-worker` (structured, PII-safe by
the A6 filter — emails/phones/bodies/tokens are stripped before they reach
the log). Request correlation: `X-Request-ID` response header ↔ A1
`request_id`. Audit trail: the `audit_log` table (append-only; runtime
roles have no UPDATE/DELETE grants — B6) is the owner-visible record for
logins, invitations, filings, invoices, and documents.
