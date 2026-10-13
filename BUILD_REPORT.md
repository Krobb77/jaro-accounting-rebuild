# BUILD_REPORT.md — Jaro Accounting rebuild

**Date:** 2026-10-06. **Phase:** 4 (final) — Verification, ops hardening,
cutover prep, closing deliverables. Build order was 0 → 1 → 2 → 3 → 5 → 4
(the portal/auth phase 5 built after booking phase 3, before verification).

This report is the honest record: exact test counts on consecutive runs,
every decision and assumption (linked to `DECISIONS.md`), remaining owner
inputs, and known limitations. Nothing in this report was rounded.

---

## 1. Per-phase summary

### Phase 0 — Foundation (reconciled schema, API skeleton, shared machinery)

- Canonical schema: **31 tables** (26 baseline + invitation_events,
  password_resets, terms_versions, terms_acceptances, calendar_sync_state;
  login_events folded into audit_log). Table count corrected twice along
  the way (plan's "31" was an arithmetic slip for the wrong reason; the
  final 31 includes `calendar_sync_state`, added in Phase 3).
- Linear idempotent Alembic migrations (0001–0013); fail-closed downgrades
  (B1) refuse with live rows in `portal_invitations`/`bookings`/
  `portal_users`/`*_events`.
- API skeleton: A1 error envelope on every route, A2 closed Pydantic
  models (`extra="forbid"`), canonical prefixes
  `/api/v1`, `/api/v1/admin/*`, `/api/v1/portal/*`, `/api/v1/booking/*`.
- Shared machinery: outbox intent staging (frozen snapshot + payload
  digest, E1–E4 lease-claim worker), idempotency helper (scope hash +
  409 on conflict), 6-digit codes (F5), token service (SHA-256 storage,
  constant-time compare), Argon2id, TOTP, session middleware (`__Host-`
  cookie, 15-min sliding / 12h absolute), rate budgets over
  `rate_windows` + advisory locks (fail-closed 503), PII-safe logging
  (A6), `/healthz` + `/readyz` (A7), `verify_runtime` fail-closed startup
  (H2), capture-mode mail (H1), three DB roles.
- Deliberate seams: calendar provider stubbed (owner question #1),
  SQLite-local vs Postgres-deploy, threading advisory-lock fallback.

### Phase 1 — Static pages (7 + trust content + design system)

- All 7 pages + privacy/terms/legal/engagement-terms/FAQ, shared chrome
  (two-tier sticky header, footer trust strip, branded 404, skip link),
  corrected CSS upgrades (single hover `#B41E1C`, hero scrim + load-bearing
  H2 text-shadow halo pinned by contrast tests), structured SEO
  (OG/JSON-LD/sitemap/robots).
- Content rules honored: hero typo fixed ("focus on it"), bio figures as
  summarized facts (never quotes), verbatim prices, Consulting Services
  copy marked DRAFT (owner sign-off #18), testimonials component with zero
  entries (section hidden, nothing fabricated), blog placeholder deleted
  (`/blog/` → 301 `/`), no social icons (originals were bare homepages),
  no CPA/CPABC claims (pending owner confirmation), FAQ strictly from
  spec facts.
- `frontend/build.py` (stdlib-only) is the source of truth: `src/` →
  `dist/`; fail-closed `img_tag()`.

### Phase 2 — Contact form (lead pipeline)

- `POST /api/v1/inquiries`: closed schema, `Idempotency-Key`, A5 budgets
  (incl. per-recipient 5/hr mail-loop defense), A1 per-field 422s;
  route-owned transaction writes the inquiry + stages two outbox intents
  (owner notice, submitter receipt) + audit row; returns
  `{inquiry_id, reference, queued_count}` (`MSG-YYYY-XXXXXX` reference).
- Text-only email renderings of the EMAIL_TEMPLATES §4 copy (subject,
  preheader, reference, ~200-char excerpt, SLA "1–2 business days"
  pending owner confirmation #6, "didn't submit this?" line, Reply-To
  Jared).
- Frontend `contact.js` (vanilla, C1–C4): normalized errors, busy-guard +
  disabled button + server idempotency (three double-submit layers),
  per-submit `data-endpoint` read (test seam), honest "queued" success
  copy.

### Phase 3 — Booking widget (the centerpiece)

- Availability: `config` / `availability` / `slots` endpoints (firm zone
  America/Vancouver, 24h min notice, 90d horizon, BC stat holidays,
  `as_of` staleness marker); server-side slot math minus bookings minus
  `calendar_blocks`; fail-closed 503 on stale calendar (D-31).
- Widget flow: timezone-first modal → available-days calendar → slot →
  native `<dialog>` interstitial → info form (Relationship defaults to
  Prospective Client, D-30) → 6-digit code (10-min TTL, 5 attempts, 60s
  resend cooldown, max 5 sends) → review with Change links (email change
  → fresh intent via `supersedes_intent_id`; zero network writes on
  Change click, asserted).
- Intent IS the `pending_verification` booking row (D-04); partial
  `UNIQUE(slot_start)` backstop; 15-min sweep; confirm re-verifies on a
  fresh clock under a route-held slot lock (race → clean 409);
  reference `JARO-2026-XXXXXX`; manage token (90d, revoked on cancel);
  dynamic thank-you from the stored record (`.ics`, one link set,
  `queued_count`, `replayed` note); reschedule all-or-nothing with
  `supersedes`; cancel ≥24h with F2 suppression of pending intents;
  no-show lever + 2-in-12mo `booking_hold`.
- Calendar hybrid: one-way 10-min read → `calendar_blocks`; optional
  write-back, failures → `calendar_sync_pending`.

### Phase 5 — Client login, invitations, portal (built before Phase 4)

- Owner bootstrap: one-shot env secret → `410` thereafter; owner login
  requires TOTP (no SMS fallback, AUTH D1).
- Invitation lifecycle: create (409 on active) → captured production
  HTML email (Mawer anatomy: branded header, greeting, capability
  description, CTA, **bold 7-day expiry**, username line, support
  contact, footer + confidentiality notice) → verify (masked email, safe
  410s) → set-password (12-char + blocklist, idempotent) → terms gate
  (no-dismiss modal, hash-matched acceptance) → TOTP enroll (two codes +
  10 recovery codes shown once) → dashboard. Resend = revoke-old +
  mint-new; revoke cancels the queued email (F2); 7-day expiry + audit.
- Login + 2FA: anti-enumeration (dummy Argon2, identical 401s),
  TOTP-first with SMS fallback to verified numbers only, password reset
  (always-200 request, 24h link, all-sessions revoked on complete),
  owner-only client TOTP-disable (reason required, forces re-enrollment).
- Sessions: 15-min sliding (60s touch-throttle), 12h absolute cap,
  ≤120s warning banner + one-click idempotent extend, 5 concurrent
  (6th evicts oldest), per-client inventory + revoke, owner revoke-all.
- F1 dashboard (needs-attention panel, snapshot cards, 20-event activity
  feed, America/Vancouver timestamps); F2 vault (25 MB cap, magic-byte
  verification, UUID filenames outside web root, soft retire, byte-
  identical downloads); F3 filings (5-state machine, `filed` ≠
  `confirmed`, "Message to client" notes); F4 invoices (immutable PDF at
  issuance via fpdf2, preview IS the stored PDF, sequential series, void
  never recycles, no payment-capture endpoint exists — asserted absent).

### Phase 4 — Verification, ops hardening, cutover prep (this phase)

- **Worker `-m` CLI** (`app/worker_main.py`): the Phase-0 residual,
  wired thin (settings → `verify_runtime` → engine → factory → mail
  backend → `Worker.main()`); `--once` for smoke/cron; refuses non-worker
  workload (G2) and fails closed on bad config (H2, exit 2).
- **7 new backend tests** (`test_18_chaos.py`): dead-SMTP drill (retry +
  backoff, nothing lost, no live-send attempt, recovery → captured),
  budget-store outage → 503 on login/booking/inquiry, worker kill
  mid-send → lease-expiry re-claim without double-send (Worker-level),
  worker CLI happy-path + two refusal cases, G1 guarded-DSN subprocess
  refusal.
- **3 new G3/G4 tests** (`test_11_docs_as_tests.py`): root README parsed
  as assertions, root README setup steps executed end to end (migrate →
  boot API → worker `--once` → frontend build), OpenAPI covers the full
  route table (derived from the live app), error-code catalog complete
  (all 51 raised codes registered).
- **Ops:** `ops/nginx.conf` (static dist, `try_files` rewrites for
  `/portal/*` and `/booking/manage/*`, legacy redirect map, API proxy),
  `ops/runbook.md` (backup/restore, dead-letter + 5xx alerting,
  migration-first releases, capture-in-staging, H6 drill status),
  `ops/deploy-checklist.md` (G6 smoke), `ops/cutover.md` (document only:
  freeze, DNS, redirects, mail auth, smoke, rollback).
- **Docs:** `README.md`, `docs/OWNER_ONBOARDING.md` (Jared's checkable
  procedures), consolidated `DECISIONS.md` (complete record), this report.

---

## 2. Verification — exact counts

Full suite run 3 consecutive times on 2026-10-06 (disposable SQLite;
Playwright Chromium via `~/workspace/pw-browsers/chromium-1243`).

| Suite | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Backend pytest (`backend/tests/`) | 128 passed | 128 passed | 128 passed |
| Frontend static (`test_build.py` + `test_pages.py`) | 65 passed | 65 passed | 65 passed |
| Playwright journeys (contact 4 + booking 6 + portal 6) | 16 passed | 16 passed | 16 passed |

Test inventory: backend 128 = phases 0–5 (116) + Phase-4 additions
(`test_18_chaos.py`: 7 — dead-SMTP drill, budget-store 503 ×3 surfaces,
worker-level kill-mid-send, worker CLI ×3, G1 guarded-DSN; `test_11`:
+4 — root README parse, root README setup execution, OpenAPI route-table,
error-catalog completeness; `test_17_18`: 1 — A4 commit-before-send
regression; net +12). Frontend static 65 (11 build +
54 pages). Playwright 16 journeys (4 contact + 6 booking + 6 portal),
desktop + phone.

Note on one anomalous intermediate run: a frontend-static run executed
concurrently with a Playwright run failed (3 failed, 49 errors) because
the Playwright module fixture rebuilds `dist/` in place while the static
tests read it — a test-isolation artifact of concurrent execution, not a
product defect. All runs reported above were executed without concurrency
and are clean 3× green.

**Incident during final verification (found and fixed, 2026-10-06):** an
intermittent `401 session_expired` in the portal Playwright journeys —
three different tests failed across runs, each passing in isolation.
Diagnosis (via a looped HTTP repro + middleware instrumentation):
FastAPI sends the response before closing the request `AsyncExitStack`,
so the `get_db` yield-teardown `db.commit()` landed *after* the client
could fire its follow-up; the follow-up's SQLite read snapshot predated
the commit (`ROW NOT FOUND` for a valid, unrevoked session). This was a
genuine product race, not a test flake — any client acting instantly on
a `Set-Cookie` (the portal JS redirects straight to terms) could hit it.
Fix: new `backend/app/transactions.py` `TransactionMiddleware` commits
the route's session after `call_next` returns the response object but
before bytes are sent; `get_db` now only stashes/closes. All five test
`get_db` overrides updated to the same contract. Verified: 0/160
immediate read-after-write failures post-fix (vs ~8/240 pre-fix);
portal journeys 6/6 on 3 consecutive runs. Regression test:
`test_17_18_session_committed_before_response_sent`.

### Rehearsed flows (§3.2) — every one exists and passes

| Flow | Test |
|---|---|
| Double-submit booking → one row | `test_16_booking.py::test_7_double_submit_replay` |
| Concurrent slot race → one booking + clean 409 | `test_6_slot_race_exactly_one_booking` |
| Expired 6-digit code rejected | `test_4_code_lifecycle` (+ `test_10_codes.py`) |
| Replayed idempotency key → stored result | `test_05_idempotency.py`, `test_7_double_submit_replay` |
| Cancelled booking suppresses confirmation + reminder (F2) | `test_10_cancel_ge24h_f2_suppression` |
| Expired invite → `expired` + audit | `test_17_portal.py::test_17_02_invite_lifecycle` |
| Token replay rejected (used invite, consumed code/challenge) | `test_17_02`, `test_17_05`, `test_10_codes.py` |
| Revoke cancels queued email, link dead | `test_17_02_invite_lifecycle` |
| Resend = new token, old dead | `test_17_02_invite_lifecycle` |
| Stuffing → 429, then budget-store kill → 503 | `test_17_04_stuffing_budgets`, `test_06_rate_budgets.py::test_route_503_when_budget_store_dead` |
| DB-leak drill (no raw tokens/passwords/TOTP secrets/session IDs) | `test_17_10_db_leak_drill` |
| Migration `upgrade → downgrade → upgrade` incl. B1 refusals | `test_01_migrations.py` (+ `test_17_17_migration_rehearsal`) |
| Dead SMTP → `retry` + backoff, nothing lost, no live-send | `test_18_chaos.py` (new) |
| Budget-store outage → 503 on login/booking/inquiry | `test_18_chaos.py` (new) |
| Worker kill mid-send → lease expiry → re-claim, no double-send | `test_08_outbox.py` + `test_18_chaos.py` (new, Worker-level) |
| G1: suite refuses a guarded (non-disposable) DSN | `test_18_chaos.py::test_g1_refuses_guarded_dsn` (new) |

### G3 docs-as-tests

- `backend/.env.example` parses; every key consumed by `app/config.py`
  (`KNOWN_KEYS`); critical gates present.
- Backend README + root README setup steps **executed** as tests
  (migrate → boot → probe; worker `--once`; frontend build into scratch
  dir).
- All nav/footer links resolve (frontend static tests); social URLs are
  never bare homepages (asserted absent); privacy/terms/legal exist and
  are footer-linked.
- OpenAPI exported from the real app contains the full route table
  (derived from the live app, not a hand list); error-code catalog
  complete (51/51 raised codes registered).
- Grep gates: no `*register*`/`*signup*` routes or identifiers (the
  no-public-signup invariant); no payment-capture/messaging/e-signature
  endpoints; no `#A91D1B`/`#007AB7`; no client TOTP self-disable route.

### G4 contract check

OpenAPI is exported from the real app in tests and asserted against the
route table. There is no generated JS client to byte-compare — the
frontend is vanilla JS with hand-written fetch calls by design (Phase 2),
so the inventory-vs-live-export assertion is the applicable form of G4.

---

## 3. How to run the site

See `README.md` (root): venv → `cp backend/.env.example backend/.env` →
`alembic upgrade head` → `uvicorn app.main:create_app --factory` →
`python -m app.worker_main` (with `JARO_WORKLOAD=worker`) →
`python frontend/build.py` → nginx (`ops/nginx.conf`) serves `dist/` and
proxies `/api/` to uvicorn. Probes at `/healthz`, `/readyz`. Owner
bootstrap via `POST /api/v1/admin/bootstrap`, then
`docs/OWNER_ONBOARDING.md`.

---

## 4. Decisions and assumptions

All in `DECISIONS.md` (consolidated complete record; per-workstream
detail in `backend/DECISIONS-backend.md` and
`frontend/DECISIONS-frontend.md`). Headline items:

- SQLite-local / Postgres-deploy; sync SQLAlchemy; uuid7-as-String(36)
  PKs; advisory-lock interface with threading fallback.
- Session cookie `Path=/` + `__Host-` (RFC 6265bis correction of the
  plan's contradictory `Path=/portal`).
- No public signup (locked invariant, grep-gated); invite-only portal,
  7-day single-use tokens; owner TOTP mandatory, no SMS fallback.
- Outbox as the only queue; honest `queued`/`captured` vocabulary (never
  "sent"); F2 cancel-suppression; E2 digest re-verification.
- Booking intent IS the `pending_verification` row; partial unique slot
  backstop; fail-closed calendar (503 on staleness).
- Invoices: PDF immutable at issuance; no payment capture in v1.
- Rate budgets are starting values (D-21) — tune with Jared pre-launch.
- Backup drill **undrilled** (tracked open item, owner Jared, target:
  onboarding week).

---

## 5. Remaining owner inputs (20 questions + unconfirmed items)

From IMPLEMENTATION_PLAN §3.5; build proceeds on the stated defaults.

1. **Calendar provider** — Google Calendar API vs CalDAV (one-way read).
   Default: provider-agnostic interface; wire before launch.
2. **Portal name** — "myJaro" (template-parameterized; rename is one
   change).
3. **Relationship-combobox default** — "Prospective Client" preselected
   (held).
4. **Consultation channel wording** — "Free 30-Min Consultation — Phone
   Call" (held).
5. **Invoice defaults** — +30d due date, numbering prefix/series,
   per-line tax behavior, e-transfer instructions text.
6. **Contact response SLA** — "1–2 business days" (in the receipt email;
   a business commitment).
7. **BC stat-holiday observance** — confirm closed-dates list (incl.
   Christmas Eve).
8. **No-show policy** — 2 in 12 months → `booking_hold` (threshold +
   message tunable).
9. **Booking windows** — 24h min notice / 90d horizon (tunable).
10. **Reminder timing** — 24h before (held).
11. **Manage-link lifetime** — 90 days post-appointment (held).
12. **Rate-limit budgets** — numeric allowances (starting values; tune
   pre-launch).
13. **Calendar-outage posture** — fail-closed 503 (held) vs degraded
   hours-only with warning.
14. **Invite-phone verification** — Jared-supplied numbers pre-marked
   verified (held) vs always-SMS-verify.
15. **Owner bootstrap mechanism** — one-shot env secret + endpoint
   (shipped; confirm).
16. **Dossier internal notes** — include in v1 (held yes).
17. **Document taxonomy** — seed list + upload allowlist additions.
18. **Consulting Services copy** — DRAFT shipped; Jared signs off.
19. **Content sign-offs** — bio wording, owner photo, map embed, blog
   strategy, real social profile URLs, testimonial tenure anchors,
   heritage/founding year, CPABC member-directory confirmation.
20. **Contact receipt excerpt** — ~200-char excerpt (held) vs full echo.

Unconfirmed items: portal name "myJaro"; invoice defaults; calendar
provider; testimonial/blog/social content; CPABC claims (none published).

---

## 6. Known limitations

- **SQLite-local vs Postgres-deploy.** Postgres is not installed in this
  environment; migrations target Postgres DDL but are verified on
  SQLite. Postgres-only paths (advisory locks, DB roles as GRANTs) are
  interface-abstracted and string-checked locally — the first deploy
  against real Postgres is itself a verification event.
- **Calendar provider stubbed.** Availability math is real; the provider
  read is a stub until Jared picks Google vs CalDAV (question #1).
- **No real-time AV.** Uploads are magic-byte verified and quarantined
  by review; `scan_status` defaults to `unscanned` and the UI never
  claims otherwise.
- **Playwright Chromium via direct download** (`~/workspace/pw-browsers/
  chromium-1243`) — `playwright install` stalled in this environment.
- **Backup drill undrilled.** Written procedure, never executed; tracked
  open item (owner Jared, target: onboarding week).
- **No live email ever sent.** Every test and drill ran in capture mode.
  Production sending needs the explicit opt-in + SPF/DKIM/DMARC.
- **Two residual timing-sensitive tests** noted in earlier phases
  (subagent-guard flakes, green on rerun) — not observed in Phase 4.
- **Session-warning Playwright journey takes ~35s** (waits for the real
  frontend poll tick) — accepted, no test hooks in production frontend.

---

## 7. Residual-risk register

Every ASSUMPTION(MED/LOW) and open owner question carries a status:

| Item | Status |
|---|---|
| Rate budgets (D-21, ASSUMPTION) | Accepted as starting values; **tune pre-launch** (owner #12) |
| Calendar provider (D-28) | **Open** — owner #1; stubbed |
| Portal name "myJaro" (D-29) | **Open** — owner #2; parameterized |
| Invoice defaults (D-27) | **Open** — owner #5 |
| Contact SLA (D-33) | **Open** — owner #6; "1–2 business days" in copy |
| No-show threshold (D-22) | **Open** — owner #8; 2-in-12mo held |
| Calendar-outage posture (D-31) | **Open** — owner #13; fail-closed held |
| Invite-phone verification (D-26) | **Open** — owner #14; pre-verified held |
| Backup drill (H6) | **Open** — tracked, owner Jared, target onboarding week |
| CPABC claims | **Deferred** — none published; owner #19 |
| Testimonials/blog/social | **Deferred** — components ready, content pending owner #19 |
| Terms v1 copy | **Deferred** — placeholder marked draft; finalize with Jared |
| Consulting Services copy | **Deferred** — DRAFT; owner #18 |
| Session 12h cap, 5-concurrent, TOTP params (D-02/D-24/D-36) | Accepted (AUTH design, least-strict defensible) |
| No public "request an invitation" form | **Held** — would recreate the spam/enumeration surface |

---

## 8. Re-verification after owner-question decisions (2026-10-08)

The frontend/copy and backend/config implementations of the §3.5 owner
questions (recorded in `DECISIONS.md` "Owner-question decisions
(2026-10-08)") were re-verified end to end by the subagent pass below.
No live email sent at any point (`MAIL_ALLOW_SEND=false` /
`MAIL_BACKEND=capture` everywhere; backend suite runs against disposable
tmp_path SQLite via the G1 gate).

**What changed, one line per area:**
- Frontend copy: `[[portal_name]]`/`[[event_channel]]` build-time tokens
  ("myJaro" kept; "Phone Call" stays default), relationship combobox
  labels aligned to plan vocabulary, SLA literal removed from
  `contact-us.html` (now rendered from the API's `response_sla`),
  consulting copy finalized (owner-review comment only, no visible
  draft badge), Q19 content-honesty audit (no CPA claims / testimonials /
  blog / social / photo / map / founding-year copy in `dist/`).
- Backend config: five named env-overridable invoice defaults
  (`JARO_INVOICE_NUMBER_PREFIX`, Net-30 terms, 5% GST default with
  `tax_label` plumbed through the PDF), BC stat list corrected to the
  official 10 (Boxing Day removed), `CALENDAR_PROVIDER` default `google`,
  `BOOKING_HOLD_MESSAGE` promoted to a named OWNER-REVIEWABLE constant,
  booking-window literals named (`MIN_NOTICE`, `BOOKING_HORIZON`,
  `REMINDER_LEAD`), invite-phone pre-verified default flag,
  upload-allowlist env-overridable, receipt-excerpt 200-char constant,
  new Alembic migration `0014_invoice_series_prefix` (head now 0014).

**Exact counts, 3 consecutive runs each (2026-10-08):**

| Suite | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Backend pytest (`backend/tests/`) | 133 passed | 133 passed | 133 passed |
| Frontend static (`test_build.py` + `test_pages.py`) | 65 passed | 65 passed | 65 passed |
| Playwright journeys (contact 4 + booking 6 + portal 6) | 16 passed | 16 passed | 16 passed |

Baseline before the decision changes was 128 backend; the decisions
added 5 backend tests (stat-holiday list, invoice owner defaults,
internal-note isolation, calendar provider/degraded-mode flags,
excerpt-chars constant) and zero net frontend-static tests (65 = 65;
the frontend workstream's "81" is 65 static + 16 journeys).

**Build reproducibility:** `python frontend/build.py` regenerates
`dist/` byte-identically across consecutive builds (identical tree
checksum after two rebuilds; 21 pages).

**Bugs found/fixed during this re-verification: none.** Zero failures
across all 9 runs (backend 133×3, frontend static 65×3, Playwright
16×3); no flakes, no environment issues, no mechanical breakages from
the decision changes. The two fixes made by the implementation
workstreams before this pass stand as recorded in `DECISIONS.md`: the
`tax_label` variable-shadowing bug in `invoice_pdf.py` (latent), the
date-sensitive `_next_slot` collision in `test_8_idempotency_conflict`
(fails on the pristine tree on/after 2026-10-07, Thanksgiving week —
fixed with distinct hours), and the Victoria Day formula bug (was 3rd
Monday of May; now last Monday preceding May 25, verified against
2023/2026/2027/2028).

**Verdict:** green across the board. The decision changes introduce no
regressions; the site remains in the state described by §2, with the
owner-question decisions now implemented as named, env-overridable
constants and verified by the counts above.

*End of BUILD_REPORT.md — Phase 4 closing deliverable. The repo is in a
state where a fresh reader can go from README to a running site + green
suite.*

## 2026-10-13 — preview, issue sweep, and flake fixes

**Preview:** served `frontend/dist` with `/api/*` proxied to the API; screenshot-reviewed home, services, about, contact, booking, portal login (desktop + mobile).

**Issues found and fixed:**
1. **Login-page heading overlap (visual):** the CSS reset zeroes all margins and base gave `h2`/`h3` a bottom margin but `h1` was missed — "Client sign in" collided with the posture line. Fixed in `frontend/src/css/base.css` (`h1, h2, h3` margin rule); hero unaffected (own margin). Verified via screenshot.
2. **TOTP enrollment 30s-boundary flake (product):** `verify_enrollment_pair` required exactly (current,next) codes, so a client reading codes just before a rollover failed when the server verified just after — ~10% flake in the portal journeys, same failure real users would hit. Now accepts (prev,current) or (current,next); D-36 property unchanged. New: `backend/tests/test_19_totp.py` (4 tests).
3. **Rate-limiter `source` dimension shared across budgets (product bug):** the key omitted the budget, so one global per-IP counter served every budget — availability browsing burned the confirm budget's allowance → spurious 429s (the manage-page reschedule POST 429'd). Namespaced by budget. New regression test `test_source_dimension_is_per_budget`.
4. **Journey-test races (test-only):** portal tests read `#totp-manual-key` before the async enroll POST populated it (new `_read_manual_key()` waiter, 2 call sites); the manage-page test assumed the first available day had ≥2 free slots (now scans current+next month and navigates the calendar selects).

**Verification after fixes (2026-10-13):** backend **138/138** passed (133 + 4 TOTP + 1 rate-limit); frontend **81/81** passed (65 static + 16 journeys). No live email (capture mode throughout).
