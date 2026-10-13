# DECISIONS-backend.md — Phase-0 assumption log

Every judgment call made while building the Phase-0 backend, per the prime
directive (generalizable modules first; deviations noted with why no pattern
fit). Doc references: IP = IMPLEMENTATION_PLAN.md, ARCH = docs/ARCHITECTURE.md,
AUTH = docs/AUTH_AND_INVITATIONS.md, PB = FIGMENTRY_PATTERNS_PLAYBOOK.md.

## Schema / migrations

1. **Table count is 30, not 31.** IP §2 says "31 tables" but the canonical
   set is ARCH §2.8's 26 + 4 (invitation_events, password_resets,
   terms_versions, terms_acceptances) = 30, and login_events folds into
   audit_log per D-16 rather than adding a table. The plan's "31" is an
   arithmetic slip. Built 30; `models.TABLE_NAMES` is the machine-checked
   inventory.
2. **Sync SQLAlchemy, not async.** The venv ships no async DB driver
   (no aiosqlite/asyncpg). Sync code runs unchanged against Postgres with a
   sync DBAPI driver (psycopg2); async is a documented future option, not a
   Phase-0 requirement.
3. **UUID PKs are `String(36)` holding canonical uuid7 strings**, not
   SQLAlchemy's `Uuid` type. The ORM `Uuid` type demands `uuid.UUID`
   objects and adds dialect friction; the string form stays time-ordered
   (lexicographic == chronological for uuid7) and is portable. Custom
   deviation: no pattern conflict — ARCH only requires "UUIDv7 PKs".
4. **citext → `String` + functional `lower()` unique index.** Postgres
   renders CITEXT via a type variant; SQLite has no citext. Case-insensitive
   uniqueness is enforced by `UNIQUE(lower(email))` partial/functional
   indexes on both dialects — same semantics, portable DDL.
5. **`text[]` registry columns → JSON/JSONB.** `document_types.allowed_mime`
   and `filing_kinds.allowed_statuses` are JSON (JSONB on Postgres),
   validated as string lists in application code. Same extension-point
   semantics (data-driven registries), no Postgres-only DDL.
6. **Advisory locks abstracted** (`app/advisory.py`): `pg_advisory_xact_lock`
   on Postgres; a process-local threading-lock registry on SQLite. The
   SQLite fallback is NOT cross-process — local verification only,
   documented on the class.
7. **Seed `availability_rules` ids are synthetic strings**
   (`seed-availability-{0..5}`), not uuid7, so the data migration is
   idempotent and human-readable. PK type allows it.
8. **Terms v1 body is an honest placeholder** marked "draft": the registry,
   hashing, and acceptance machinery ship in Phase 0; final copy is
   authored with Jared in Phase 5.
9. **Migrations import type aliases from `app.models`** (CIText, StrList,
   UTCDateTime) but define every Table inline — frozen DDL per migration,
   shared stable primitives only.

## API / sessions

10. **Session cookie is `__Host-jaro_session` with `Path=/`, not
    `Path=/portal`.** The task lists both `Path=/portal` and the `__Host-`
    prefix, but RFC 6265bis requires `Path=/` for `__Host-` cookies
    (browsers reject `__Host-` cookies with any other path), and the API
    lives under `/api/v1/*` — a `/portal`-scoped cookie would never be sent
    to the API. `__Host-` + `Path=/` is the spec-valid combination;
    portal scoping is enforced server-side by route authz. The DONE-13 test
    asserts `HttpOnly; Secure; SameSite=Lax; Path=/`.
11. **Absolute session cap is 12h** (D-02), overriding ARCH §2.1's 24h note —
    the decision log is explicit and the plan's §3.1 vocabulary confirms it.
12. **`X-Session-Expires-In` carries milliseconds** (ARCH §1.2), while
    `GET /api/v1/portal/session` returns `expires_in_seconds` — both are
    kept; the header is the per-response signal, the endpoint the
    authoritative poll target (D-44).
13. **Rate-limit increments run in their own short-lived transaction**,
    not the route's: a route rollback must not refund the attempt (otherwise
    floods get free retries).
14. **Idempotency integration is tested through the production dependency
    + helper on a minimal app**, not through a Phase-0 route — no keyed
    route executes yet in Phase 0. The mechanism (dependency, scope hash,
    409, replay) is the real code; the wiring point is documented for
    Phase 2/3.

## Outbox / mail

15. **`frozen_snapshot.fields.destination` carries the raw address.**
    ARCH stores `destination_hash` (A6) on the row; the worker still needs
    the address to render. The snapshot is the render-truth (E2); the hash
    is the PII-safe reference for logs/eligibility. Documented in code.
16. **Crash-recovery reclaims `sending` rows with expired leases.**
    The first cut of the claim query only matched `queued/retry`, which
    would strand crashed workers' intents forever. E1's lease-expiry path
    requires reclaiming `sending` + expired lease — fixed and covered by
    DONE-8's kill-mid-send test.
17. **Phase-0 mail templates are minimal placeholders** in `app/mail.py`
    (`TEMPLATES`). Real production templates (invitation HTML, booking
    confirmation, contact receipt) are Phase 2/3/5 work; the render-from-
    snapshot + digest + capture pipeline they plug into is real.
18. **SMTP backend exists but is never exercised in tests**
    (`MAIL_ALLOW_SEND=false` everywhere in tests). The gate is enforced in
    `SmtpMailBackend.send` AND at settings validation (defense in depth).

## Ops

19. **Postgres is the deploy target; SQLite is the local-verification
    dialect** (approved pragmatism). DB roles (`jaro_web/jaro_worker/
    jaro_migrate`) are string-checked at startup, not GRANT-enforced
    locally. `DATABASE_URL` accepts `postgresql(+psycopg2)` schemes.
20. **`MAIL_ALLOW_SEND` unset refuses boot** — the DONE-2 criterion makes
    the gate explicit rather than defaulting, so a missing var can never
    silently enable sending.
21. **No `app` object at import time.** `uvicorn app.main:create_app
    --factory` is the entry point; importing the module never boots (H2).
22. **Rate-budget numbers are starting values** (D-21): login 20/20/10/1000,
    booking intents 30/10/5-per-email/1000, etc. Load-test and tune in
    Phase 4.
23. **`verify_runtime` derives the pinned head from the migration scripts
    on disk** (code is the pin, not configuration) and enforces a single
    head.
24. **Access-log PII test sets logger levels explicitly** because pytest
    leaves root at WARNING; production uses `configure_logging()` with
    INFO. Not a code issue — standard logging semantics.
25. **The `db` test fixture's outer transaction is not released by
    `session.commit()`** (the session joins the externally-begun
    transaction). Tests needing cross-connection visibility use
    `committed_db`/`db_admin` and clean up after themselves; documented on
    the fixture.

## Deliberate Phase-0 seams (not built)

- Calendar provider is stubbed (`StubCalendarProvider`); Google vs CalDAV
  is owner question D-28 — the `CalendarProvider` interface is the seam.
- Real email templates (see 17), booking availability math, portal F1–F4,
  invitation/auth flows: skeleton `501 not_implemented` with the A1
  envelope, closed models, and canonical prefixes already in place.
- `python -m app.worker_main` entry point: the `Worker` class is complete
  and tested via `run_once()`; the CLI wrapper lands with Phase 3/5 wiring.

---

# Phase-2 — Contact-form lead pipeline (2026-10-06)

Implements IMPLEMENTATION_PLAN §2 Phase 2 (work items 2.1, 2.3): `POST
/api/v1/inquiries` + the two outbox email templates. Every judgment below
is a deviation-or-adoption note per the prime directive (shared machinery
reused, not rebuilt).

## Reused as-is (no changes)

- Outbox staging/claim/worker (`app/outbox.py`): `stage_intent` (F3
  re-issue), lease-claim protocol, backoff, capture backend — untouched.
- Idempotency helper (`app/idempotency.py`): `require_idempotency_key`,
  `begin`/`store`, scope hash `sha256(route || ':' || client_key)` —
  untouched.
- Rate budgets (`app/rate_limits.py`): the four-dimension check, HMAC
  keys, fail-closed store — untouched; only the `inquiries.submit` budget
  row gained a `target` dimension (see 5).
- A1 envelope + validation handler (`app/errors.py`) — untouched; the
  route's plain-language messages flow through the existing
  `{field, message}` rendering.
- Route-owned transaction (`routes.get_db`) — untouched.

## Judgments

1. **`inquiries.reference` is a new persisted column (migration 0008), not
   derived.** EMAIL_TEMPLATES §4.2 requires a short human-readable
   `{{REQUEST_ID}}` ("MSG-2026-0831") that guests quote in follow-ups and
   that the outbox dedup scopes key on. Format `MSG-YYYY-XXXXXX`
   (unambiguous alphabet) mirrors the booking scheme (D-11) via
   `refnums.new_inquiry_reference()`; the plan's 4-digit example is
   illustrative, the D-11 scheme is the defensible canonical form.
   Migration 0008's downgrade is B1 fail-closed (refuses with live rows —
   the reference is guest-facing); backfills any pre-existing rows from
   the uuid7 tail.
2. **Dedup scopes: `scope_id = <reference>` per kind.** The plan's
   `contact:<request_id>:receipt` shape maps onto the canonical D-14
   composition `sha256(kind || ':' || scope_id)` with the request/inquiry
   reference as the scope: `dedup_key("inquiry_receipt", ref)` and
   `dedup_key("inquiry_owner_notice", ref)`. Asserted 1:1 in
   test_15 (DONE-1).
3. **Replay returns the stored status 201, not 200.** The Phase-0 A3
   helper (`idempotency.begin/store`, `ReplayHit`) replays the stored
   result AND status; test_05 pins `r2.status_code == 201` with
   `replayed: true`. The playbook's figmentry note says 200, but this
   repo's contract is explicit and the `replayed: true` flag carries the
   distinction — changing the helper would be rebuilding shared machinery.
   Kept; noted here.
4. **Response is `{inquiry_id, reference, queued_count}`** — a superset of
   the plan's `{inquiry_id, queued_count}`. The frontend success panel
   needs the reference and there is no lookup endpoint (by design — no
   oracle); returning it is the least-strict honest choice.
5. **Per-recipient receipt budget = target dimension (5/hour/email) on
   `inquiries.submit`.** Each submit queues a receipt to the form's
   address, so the target dimension IS the mail-loop defense (DONE-7);
   the route wires `target_from="email"`. Starting value per D-21; Jared
   tunes in Phase 4. Known edge (not fixed): the dependency reads the raw
   body email, so case-variant addresses get separate target buckets —
   the receipt dedup/idempotency still prevent duplicate mail per key,
   and the budget is a rate control, not an exactness guarantee.
6. **Email templates are text-only renderings of the §4 copy block.**
   The shared render pipeline is text-only in Phase 0 (Phase-0 item 17);
   the §1 HTML skeleton (charcoal header, red accents, confidentiality
   footer) is carried as text structure, and every §4 testable element is
   present verbatim: subject, preheader (first body line — that is what
   inbox clients show as preview), reference block, ~200-char excerpt,
   SLA line, "Didn't submit this form?" line, Reply-To Jared. The full
   HTML render upgrade is a documented Phase-5 seam alongside the
   production invitation template. No HTML was invented half-way.
7. **Audit row is PII-safe by construction.** `audit_log.detail` carries
   only `{reference, queued_count, idempotency_scope}` (the client key is
   a CSPRNG UUID — no PII); the A6 grep gate (10 submits) covers logs,
   audit details, and the HMAC'd budget keys.
8. **Phase-0 latent defect fixed: `audit_log`/`domain_events` id
   `BigInteger` → `Integer` (migration 0009).** The inquiry route is the
   first writer of `audit_log`, and its first insert raised
   `IntegrityError: NOT NULL constraint failed` — SQLAlchemy only applies
   lastrowid retrieval to `Integer` PKs on SQLite, so `BigInteger`
   autoincrement PKs never generated values via the ORM (Postgres would
   have rendered BIGSERIAL, but SQLite is the local-verification
   dialect). `Integer` PKs autoincrement on both dialects. Pre-launch:
   no rows, conversion is a formality; downgrade reverses the type.
9. **`RESPONSE_SLA_DEFAULT = "1–2 business days"` is a module constant**
   in `routes/public.py`, frozen into the receipt snapshot at stage time
   (E2). It is the D-33 suggested default — a business commitment pending
   Jared's confirmation (§3.5 #6); when he confirms, this constant and
   the email copy change together (cross-workstream contract).
10. **Email validation is a conservative stdlib regex** — the venv ships
    no `email-validator` package, and adding a dependency for one route
    was judged heavier than a documented conservative check (rejects
    whitespace/control chars, which also makes the address header-safe
    for the owner-notice Reply-To).
11. **Test-only pattern: `committed_client` (per-request committed
    sessions).** The shared `client` fixture's long-lived uncommitted
    transaction blocks the rate limiter's separate connection on SQLite
    ("database is locked" → spurious 503s) once the route under test
    writes. Committed per-request sessions mirror production's
    short-lived transactions; documented on the fixture.

## Verification

- New `backend/tests/test_15_inquiries.py`: 9 tests mapping the plan's 7
  DONE criteria 1:1 (+ idempotency-conflict and dedup-composition
  extras). Full backend suite: **83 passed** (74 pre-existing + 9 new)
  on 3 consecutive runs.

---

# Phase 3 — Booking widget backend (2026-10-06)

New modules: `app/booking.py` (domain), `app/routes/booking.py` (13
routes), `tests/test_16_booking.py`. Migrations `0010` (manage token,
no-show columns) and `0011` (`calendar_sync_state` table — see D-31
below). Shared machinery reused unchanged: outbox (F2/F3), idempotency
(D-11), codes via `sms_codes` + email, advisory locks, budgets,
capture mail, A1 envelope. Table count is now **31** (was 29).

## Decisions

1. **No separate intents table (D-04).** The booking intent IS the
   `bookings` row with `status='pending_verification'`; the 15-min
   worker sweep expires it to `cancelled/verification_expired`. The
   partial `UNIQUE(slot_start) WHERE status IN
   ('pending_verification','confirmed')` is the unraceable backstop.
2. **Intent-scoped code verification (custom).** The shared
   `codes.verify_code` is target-scoped (per email) and would let two
   pending intents for one email cross-verify. Booking needs
   intent-scoped attempts/sends, so `app/booking.py` keeps its own
   counters on the booking row with a comment explaining the gap.
3. **Verify route uses its own short-lived session.** `get_db` rolls
   back on `ApiError`, which wiped the code-attempt counter on
   `invalid_code`. The verify route opens a dedicated session (the
   rate-limiter precedent, Phase-0 item 13) so attempts persist.
4. **Route-held slot lock across commit (race fix).** The threading
   advisory-lock backend releases at context exit, unlike
   `pg_advisory_xact_lock` (xact-scoped), and SQLite pins the read
   snapshot at first SELECT — so a lock-then-commit in `booking.py`
   let the race loser miss the winner's commit. The confirm route now
   holds `advisory_key("booking-slot", slot_start)` across the whole
   confirm **including `db.commit()` inside the lock**, plus a
   `db.rollback()` right after acquiring (fresh snapshot on SQLite,
   no-op on Postgres). `confirm_booking` documents the caller-holds
   contract instead of taking the lock itself. Loser → clean 409
   `slot_taken`; a confirmed intent re-submitted maps to the same 409
   (`load_intent(..., confirmed_is_taken=True)`).
5. **`calendar_sync_state` table (migration 0011, D-31).** Real gap
   found in testing: an empty-but-fresh calendar sync left no
   `calendar_blocks` rows, so the staleness gate read "never synced"
   → permanent fail-closed 503. The gate now reads
   `calendar_sync_state.last_ok_at`; the worker records sync errors
   too.
6. **Cancellation-notice email template (new).** EMAIL_TEMPLATES §5
   item 8 left it to booking; written following the §1 skeleton (one
   link set for the guest, none for the owner, honest vocabulary).
7. **Intent supersede (`supersedes_intent_id`).** The plan's
   change-link rules (email change → new verification, same slot) are
   unimplementable otherwise: the guest's own previous pending intent
   holds the slot and would 409 the re-submit, stranding the hold
   until the sweep. `create_intent` atomically expires the referenced
   pending intent (`cancelled/superseded`, `cancelled_at` set for
   `ck_bookings_cancelled_at`) before the free check. Same-slot email
   fixes and slot changes both work; the old hold is freed
   immediately. Superseding a consumed intent → 410. No shared
   "replace intent" primitive existed (intents are immutable), hence
   the narrow custom path with audit detail.
8. **Idempotency replay stores raw manage URLs.** The confirm
   idempotency record's `result` keeps the full manage URLs so a
   replayed key renders an identical thank-you page. Deliberate B6
   trade-off, documented: the outbox snapshot must contain the raw URL
   for the E2 render.
9. **Test-only fixes:** migration-head test updated to `0011`; the A1
   envelope inventory test allowlists `GET /api/v1/booking/config`
   (legitimately parameterless-200).

## Verification

- `backend/tests/test_16_booking.py`: 15 tests mapping the plan's 16
  DONE criteria 1:1 (+ intent-supersede extra).
- Full backend suite: **98 passed** (83 pre-existing + 15 new) on 3
  consecutive runs.

## Phase 5 — Client login, invitations, portal (2026-10-06)

### Key judgments

1. **fpdf2 as the one new dependency** (`fpdf2==2.8.4` pinned). Invoice PDFs
   must be deterministic (byte-identical for owner and client) and generated
   at issuance. fpdf2 was chosen over reportlab for smaller footprint and
   deterministic output. Two bugs found: `set_creation_date` needs a
   `datetime` (not string), and core fonts are latin-1 only (added `_latin1()`
   sanitizer on all client-controlled PDF text).
2. **Invite idempotency replay excludes `accept_url`.** The raw token is
   returned ONCE; a replayed idempotency key returns the stored result
   without the token (the token was already issued). This prevents token
   leakage via replay.
3. **Recovery codes at existing `/login/2fa`.** The spec's "10 recovery
   codes (SHA-256, shown once)" are issued at TOTP confirm and verified at
   the existing `/login/2fa` endpoint (no separate recovery endpoint —
   the challenge flow already handles it).
4. **QR omitted from TOTP enrollment.** The provisioning URI + manual key
   are shown; the frontend links the URI (mobile authenticators handle it).
   A rendered QR would require a new dependency for marginal benefit.
5. **Resend = revoke-old + mint-new.** The old row is revoked
   (reason=resend, `superseded_by` set); the old token dies immediately.
   The new invite is a fresh full invitation, never a "reminder".
6. **SMS codes ride the outbox snapshot.** The 6-digit SMS code is in the
   frozen outbox snapshot (capture mode); the worker renders it. No separate
   SMS backend in v1.
7. **Status probe exempt from touch.** `GET /api/v1/portal/session` does not
   update `last_seen_at`; otherwise the poll itself would refresh idle time
   and the ≤120s warning banner could never appear.
8. **Security-counter commits as documented A4 exceptions.** The MFA
   5-strikes counter and SMS attempt counter use explicit `db.commit()` in
   `_consume_login_challenge` and `codes.verify_code` — otherwise the
   `get_db` rollback on `ApiError` would discard the count, allowing
   unlimited guesses (critical security bug, fixed).
9. **Outbox excluded from DB-leak drill by design.** The drill asserts no
   raw tokens/passwords/TOTP secrets/session IDs in credential tables.
   `email_outbox` is excluded: the worker renders from the frozen snapshot,
   tokens there are single-use + TTL'd + cancelled on revoke.
10. **Migration 0013: dropped full unique index on invitation email.** The
    `ix_portal_invitations_email_ci` (full unique) blocked resend. The
    correct invariant is the partial unique
    `ix_portal_invitations_live_email` (one live invite per email, B2).

### Verification

- `backend/tests/test_17_portal.py`: 18 tests mapping the plan's 17 DONE
  criteria 1:1 (+ 2 extras: 17_01b owner TOTP mandatory, 17_05b TOTP enroll).
- Full backend suite: **116 passed** on 3 consecutive runs (2026-10-06).
- `frontend/tests/test_portal_journey.py`: 6 Playwright journeys — all pass.
