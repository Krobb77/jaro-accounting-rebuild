# Jaro Accounting — Cutover Plan (WordPress → rebuild)

**Status: DOCUMENT ONLY — do not execute.** This plan is written ahead of
the cutover so Jared can review, answer the remaining inputs, and schedule
the window. Execution needs his explicit go-ahead on each section.

## 0. Prerequisites (before scheduling the window)

- [ ] Deploy checklist green on the production host (staging already smokes
      per `ops/deploy-checklist.md`).
- [ ] Remaining owner inputs answered that affect launch copy/behavior:
      #2 portal name, #4 channel wording, #6 contact SLA, #12 rate budgets,
      #13 calendar-outage posture, plus the #19 content sign-offs
      (bio, photo, social URLs, CPABC confirmation). See BUILD_REPORT.md.
- [ ] Mail-provider account provisioned; SMTP credentials in the secret
      store (never in the repo).
- [ ] Registrar + DNS provider access confirmed (whoever holds the
      jaroaccounting.com zone).
- [ ] Backup-drill item acknowledged (runbook §2 — undrilled until the
      onboarding week).

## 1. Content freeze on the old site

1. Announce the freeze window to Jared: **no content edits on the
   WordPress site during the migration window** (pages, hours, prices —
   nothing). The rebuild's copy was verified against the spec; last-minute
   WordPress edits would silently diverge from it.
2. Put WordPress into maintenance/read-only for the cutover window itself
   (the booking widget and contact form on the old site must not accept
   submissions that the new system never sees).
3. After cutover: WordPress stays **read-only (not deleted) for one week**
   — the rollback target (§6).

## 2. DNS plan

- **48h before the window:** lower the TTL on the apex and `www` records
  (e.g. to 300s). This is the step people skip; without it a bad cutover
  takes a full TTL to undo.
- **At the window:** point apex + `www` at the new host (A/AAAA, or CNAME
  per the host's instructions). Keep the old WordPress host's records
  noted in the deploy log for the rollback.
- **Verify:** `dig` apex + www from two networks; TLS cert valid for both
  names (the nginx config 301s www → apex, but the cert must still cover
  www or the redirect never gets a clean handshake).

## 3. Redirect map (nginx: ops/nginx.conf — verify each with curl)

The 7 canonical trailing-slash URLs are kept **identical** (`/`,
`/about-us/`, `/our-services/`, `/contact-us/`,
`/free-30-min-consultation/`, `/thank-you-for-your-booking/`, `/blog/`),
so the map covers legacy variants only:

| Legacy | Target | Why |
|---|---|---|
| non-trailing-slash (`/about-us`) | 301 → `/about-us/` | canonical form |
| `?noamp=mobile` (any page) | 301 → same path, query stripped | old mobile variant |
| `www.jaroaccounting.com/*` | 301 → `https://jaroaccounting.com/*` | canonical host |
| `/wp-*`, `/xmlrpc.php` | 301 → `/` | WordPress remnants |
| `/blog/` | 301 → `/` | placeholder post deleted; no blog in v1 |
| `/thank-you-for-your-booking/` (direct visit) | 301 → `/free-30-min-consultation/` | now a dynamic confirmation render — direct visits have no booking context |

## 4. Mail authentication (before the first real send)

- [ ] SPF record authorizes the sending host/service.
- [ ] DKIM signing enabled; public key published in DNS.
- [ ] DMARC published (start `p=quarantine`, move to `p=reject` after a
      week of clean reports).
- [ ] **Envelope-from on the bounce subdomain** (e.g.
      `bounces.jaroaccounting.com`) so bounces don't poison the main
      domain's reputation.
- [ ] `From:` consistent everywhere: `Jaro Accounting
      <jared@jaroaccounting.com>` (the worker's `WorkerConfig.sender`
      default — verify it matches the DNS-authenticated identity).
- [ ] Send one test invitation + one booking confirmation to
      Jared-controlled addresses; confirm headers show SPF/DKIM pass and
      the DMARC report arrives.

## 5. Post-cutover smoke (Jared runs this, build team on call)

- [ ] All 7 canonical URLs return **200** (curl + one real browser pass).
- [ ] Legacy variants return the **301s** in §3 (spot-check five).
- [ ] Probes green: `/healthz` 200, `/readyz` 200.
- [ ] **One end-to-end booking** for the free consultation to a
      Jared-controlled address: intent → code → confirm → captured
      confirmation + owner notice (check capture artifacts first in
      staging; real delivery only in production after §4 is done).
- [ ] **One contact submit** to a Jared-controlled address: inquiry row +
      receipt + owner notice.
- [ ] No `dead` intents in the outbox after the smoke.

## 6. Rollback plan

- **Traffic:** revert the apex + www DNS records to the old WordPress host
  (this is why the TTL was lowered 48h ahead — the revert propagates in
  minutes, not hours).
- **WordPress:** left read-only for one week post-cutover; flipping it
  back to live is the rollback. Decommission (delete) only after the
  first successful production week, on Jared's explicit word.
- **Data:** migrations are **never auto-reversed** (H5/B1). If the new
  build wrote production rows before the rollback, those rows stay —
  downgrades with live rows refuse by design. The rollback is a traffic
  revert, not a data revert; reconcile any new bookings/inquiries by hand
  from the admin console before decommissioning WordPress.
- **Decision point:** if the smoke (§5) fails on any item, roll back
  first and diagnose second. A broken cutover held open "while we fix it"
  is how leads get silently dropped.
