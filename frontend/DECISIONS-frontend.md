# Phase-0 Frontend — Decisions & Assumptions

Date: 2026-10-05. Phase-0 FRONTEND builder. This file records every
assumption so Phase 1+ can rely on (or deliberately change) them.

## 1. Build script vs handwritten HTML — build script wins

`frontend/build.py` (stdlib-only Python) is the **source of truth**; it
assembles `frontend/src/` into `frontend/dist/`. `dist/` is generated output —
never hand-edit it (a test asserts rebuilds are byte-identical).

Rationale: the prime directive is "tokens and chrome built ONCE as reusable
partials — every page in Phase 1 assembles from these." Handwritten HTML per
page would drift within weeks (the exact failure mode the original WordPress
theme's inline CSS produced). The build script keeps one header, one footer,
one token file, and enforces image guidance at build time (fail-closed:
missing asset or empty alt aborts the build).

Trade-offs accepted:
- Building requires Python (the repo already ships a venv; the script is
  stdlib-only, no pip installs).
- Authors write pages as HTML + a tiny frontmatter block, not pure HTML —
  documented in `build.py`'s docstring.
- Served-as-static stays true: nginx serves `dist/` directly, no runtime
  templating, no Python in the serving path.

## 2. Image naming

Copied from `jaroaccounting-replication/assets/` (the 2026-10-05 final
matched set) into `frontend/src/assets/`:

| Original file | Clean name |
|---|---|
| `media-generation-hero-matched-set-0-8da82a0f-…​.webp` | `hero.webp` |
| `media-generation-tax-matched-set-0-71891921-…​.webp` | `service-tax.webp` |
| `media-generation-bookkeeping-matched-set-0-3149d1f7-…​.webp` | `service-bookkeeping.webp` |
| `media-generation-consulting-matched-set-0-33f1fc64-…​.webp` | `service-consulting.webp` |

Decisions:
- Original `.webp` format kept (no re-encode; they're already Web/P).
- Short descriptive names instead of `media-generation-…` UUIDs — the UUIDs
  carry no meaning and would rot every reference.
- `srcset` policy: `img_tag()` emits `srcset`/`sizes` **only when `variants=`
  is supplied**. Today we ship a single source file per image, so no srcset
  is emitted. Phase 1 should generate multi-width renditions (e.g. 800/1600w)
  and pass `variants="800:hero-800.webp,1600:hero-1600.webp"` — the helper and
  the `[[img ...]]` directive already support it; nothing else needs changing.
- Hero is `loading="eager"` + `fetchpriority="high"`; everything else lazy.
  Every `<img>` gets a non-empty `alt` (build-fails otherwise) and an
  `aspect-ratio` style to prevent layout shift.

## 3. Chrome decisions

- **Sticky header**: the original site's header is NOT sticky, but
  IMPLEMENTATION_PLAN §2 "1.2 Chrome" explicitly specifies a two-tier sticky
  header, and the subagent task orders the same. Plan wins over the
  replication spec here; `position: sticky` + `overflow-x: clip` (R6) keeps
  stickiness working.
- **Utility lane**: Client Login (lock icon) → `/portal/login`; phone
  `tel:1-(778)-227-4891` displaying `1-(778)-227-4891`; hours snippet
  "Mon–Fri 9–5 · Sat 10–3" (hidden under 40rem to keep the lane compact).
- **Primary lane**: text logo "Jaro Accounting" + tagline
  "Accounting. Tax. Strategy."; nav order Home / About Us / Our Services /
  Contact Us / FREE 30 Min Consultation!; red pill CTA "Book FREE 30 Min
  Consultation" → `/free-30-min-consultation/`.
- **Mobile nav**: checkbox-driven per the original + R3 polish (grid
  `0fr→1fr` expand, +/× icon swap, ≥44px targets). The `<input>` is a direct
  child of `.site-header` and a preceding sibling of `.primary-lane` — the
  CSS relies on that sibling relationship; moving the input breaks the menu.
  The input stays keyboard-focusable; its focus ring is drawn on the visible
  label instead.
- **Skip link**: exact text "Skip to content" (per task), first child of
  `<body>`, targets `<main id="main">`. The builder fails any page whose body
  does not contain exactly one `id="main"` landmark.
- **Footer**: sitemap-of-last-resort (Explore / Client Area / Get In Touch
  columns), address `215 Mowat Street, New Westminster, BC V3M 4B1`, email
  `jared@jaroaccounting.com`, phone, Client Login link, trust strip with
  Privacy / Terms / Legal links → `/privacy/`, `/terms/`, `/legal/`.
  **These three pages do not exist yet — Phase 1 MUST create them** (the
  Phase-1 DONE criteria require footer trust links to resolve).
- **404**: branded chrome + plain-language "We can't find that page" with
  Home / Services / Contact buttons and the phone number.

## 4. URL & path conventions

- Absolute asset URLs (`/css/style.css`, `/assets/hero.webp`) — assumes nginx
  serves `dist/` at the domain root. No relative `../` paths anywhere.
- Canonical page URLs keep trailing slashes (`/about-us/`, …) per the cutover
  plan; canonical meta = `https://jaroaccounting.com` + path.
- `src/pages/<name>.html` → `dist/<name>.html` (flat). If Phase 1 wants
  directory-style URLs, extend `build()`'s output mapping — frontmatter
  `path:` already carries the canonical path independently of the filename.
- Favicon: none shipped (the original had none determinable). Add one in
  Phase 1 if desired — `head.html` is the single place to link it.

## 5. Token / CSS decisions

- Single hover token `--color-primary-hover: #B41E1C`. The old deep-red hover
  shade and the blue secondary preset are excluded entirely — tests grep
  `dist/` for both and fail on any occurrence (even in comments).
- `#767676` (`--color-neutral-500-text`) used for small muted text — the
  documented AA deviation (4.54:1 on white vs 3.36:1 for `#8c8c8c`).
- Layer order `reset, tokens, base, layout, components, utilities, print`
  (AR1/AR6); every rule lives in a layer; `style.css` is the only `<link>`.
- Component upgrades (cards, hero scrim, forms, calendar, slots) land in
  Phase 1. Phase 0 ships: the token block, reset, base (fluid type, focus
  rings, reduced-motion kill switch, overflow-x clip, touch refinements),
  layout scaffolding, chrome components (skip link, buttons B1–B3/B5,
  header/nav, footer, learn-more link, 404), utilities (`.sr-only`),
  print basics.
- `js/` exists in `src/` but is intentionally empty — no JavaScript is needed
  for Phase-0 chrome (checkbox nav, no carousels). `build()` copies `src/js/`
  to `dist/js/` only when it contains files.

## 6. Open items for Phase 1

1. Real page copy (7 pages) — explicitly out of scope here; `index.html` is
   an assembly proof with placeholder labels only.
2. `/privacy/`, `/terms/`, `/legal/` pages (footer trust strip links to them).
3. Multi-width image renditions + `variants=` wiring in page directives.
4. Favicon (optional).
5. Component upgrades per CSS_DESIGN_UPGRADES.md Parts 2–3 (cards, hero
   scrim, forms, calendar/slots, select chevron, hours table).

---

# Phase-1 Static Pages — Decisions & Assumptions

Date: 2026-10-05. Phase-1 builder (all static pages). Every judgment call
below is recorded so Phase 2+ and Jared can rely on or deliberately change it.

## 1. Consulting Services copy — DRAFT, pending Jared

The original "Consulting Services" heading had no body copy (GROUNDED thin).
Phase 1 ships NEW body copy built strictly from the homepage's consulting
bullets (incorporation, holding companies, paying yourself out of the
business). It is marked `<!-- DRAFT (2026-10-05) ... -->` in
`src/pages/our-services.html` and is NOT final: owner input D-36
(IMPLEMENTATION_PLAN §3.5 #18) must sign it off before launch. The pricing-
honesty note ("What's not included") and the tax-savings qualifier on the
same page are Phase-1 additions per plan §1.3; both stay within spec facts
(tax filings are priced separately on the same page; one-off cleanups are
always quoted in writing — a business commitment, not a new policy).

## 2. Testimonials — component built, zero entries, hidden when empty

`src/partials` CSS (`.testimonials`, `.testimonial`) + data file
`src/data/testimonials.json` (currently `[]`) + a `[[testimonials]]`
build directive. Zero entries render NOTHING — no section, no placeholder
text. No testimonials are fabricated; the owner provides real ones later,
at which point adding entries to the JSON file is the entire change. The
directive is already placed on the homepage so entries appear automatically.

## 3. Blog — placeholder deleted, no replacement

The original's single "Hello World!" post is gone: no `blog.html` is built
and `/blog/` is excluded from `sitemap.xml`. Cutover: nginx must
301 `/blog/` → `/` (recorded for the Phase-4 redirect map alongside the
legacy variants). No blog index, no feed — a blog is an owner decision
(IMPLEMENTATION_PLAN §3.5 #19, "blog strategy").

## 4. Social links — omitted, owner input required

Footer (and contact page) carry NO social icons. The original's
Facebook/Twitter/Instagram/YouTube links were bare platform homepages, not
business profiles — replicating them would ship dead-end links, and
inventing profile URLs would be fabrication. A test asserts no bare-
homepage social links exist anywhere in dist/. Real profile URLs are owner
input (§3.5 #19); when provided, they go in the footer contact column.

## 5. FAQ — strictly spec facts; FAQPage JSON-LD on /faq/ only

12 FAQs in 4 categories (Hours & Contact, Services & Pricing, Booking,
About the Firm), every answer grounded in the replication spec (hours,
address, services, verbatim prices, booking page, "across Canada", Jared's
bio). No invented policies (no refunds, no turnaround promises, no SLAs).
Category "tabs" are anchor links to category sections; every question has
an id (`faq-pricing-2`, …) for URL-hash deep links. `FAQPage` JSON-LD is
emitted ONLY on `/faq/` — putting it on every page would mark up content
that isn't there (structured-data spam). `Review`/`aggregateRating` are
omitted entirely until real testimonials exist (see §2).

## 6. Playwright — browser check RAN (16/16 pass)

`playwright` 1.63.0 (pip) is in the repo venv. `playwright install chromium`
stalled with 0 bytes after 8+ minutes, so Chromium 153.0.8010.12 ("Chrome
for Testing") was fetched by direct CDN download instead and extracted to
`~/workspace/pw-browsers/chromium-1243` (set `PLAYWRIGHT_BROWSERS_PATH`
there; launch via `executable_path=` since only the full Chromium build was
fetched, not the headless shell). A smoke script (`/tmp/pw_smoke.py`,
throwaway) ran against `dist/` served over local HTTP — 16/16 PASS:
mobile nav toggle visible at 360px, expands/collapses on toggle, keeps its
`aria-label`; no horizontal overflow at 360px on index/about-us/our-
services/contact-us/free-30-min-consultation/faq/privacy; first Tab lands
on the skip link and Enter moves focus to `#main` (this is why every
`<main>` carries `tabindex="-1"`); desktop hero headline renders, nav
toggle hidden, booking CTA visible. The pytest suite keeps the hermetic
static assertions for DONE #9 (markup + CSS rules); re-run the live
browser pass in Phase 4 against the deployed origin.

## 7. Heading levels — semantic, not literal h5s

The original used h5s for card titles and section labels. The rebuild uses
h1 (hero, one per page) → h2 (sections) → h3 (card titles, styled with
`.card-title`), because skipped heading levels are an a11y defect and the
A9–A14 upgrades require a sound outline. Visual result matches the original
(small bold card headings); no information changed.

## 8. Links — gray #4D4D4D with persistent underline (corrected B4)

Per the corrected values: in-content links (`.site-main a:not(.btn)`) are
`#4D4D4D` with a persistent 1px underline (2px on hover/focus). Red is
reserved for pill CTAs, so hue alone never marks a link (WCAG 1.4.1).
Header/footer chrome links keep their own affordances. The upgrades doc's
T8 pull-quote cite was corrected the same way: `#767676` instead of
`#8c8c8c` (the cite is 0.875rem text; A11 requires the AA-safe gray).

## 9. Utility lane — no Privacy/Terms links

The two-tier header's utility lane carries Client Login (lock icon),
phone, and the hours snippet only. Privacy/Terms/Legal live in the footer
trust strip, which is what DONE #2 requires ("linked from the footer trust
strip"). Keeps the lane compact on phones.

## 10. Sitemap — 11 URLs

`https://jaroaccounting.com/` + the 5 surviving spec pages
(`/about-us/`, `/our-services/`, `/contact-us/`,
`/free-30-min-consultation/`, `/thank-you-for-your-booking/`) + the 5
trust pages (`/privacy/`, `/terms/`, `/legal/`, `/engagement-terms/`,
`/faq/`). `/blog/` excluded (301 → `/`, see §3). The thank-you page IS
included: DONE #7 says "all 7 pages + new trust pages" and it is one of the
7 surviving pages. Frontmatter `sitemap: false` is supported for future
exclusions. `robots.txt` allows all + names the sitemap.

## 11. Forms — F1–F3 + F6 now; F4/F5/F7 with the Phase-3 widget

The contact form gets the full F1 field system, F2 `aria-invalid` states,
F3 error-summary styles, and F6 select chevron. F4 (calendar day grid),
F5 (slot radio pills), and F7 (captcha row — the captcha itself is deleted
in favor of mailbox-proof codes) are booking-widget patterns and land with
the Phase-3 widget. The H5 scroll cue was deliberately omitted (verdict:
whisper-quiet or omit). A12 keeps Phase-0's reduced-motion kill switch
rather than re-gating every motion rule under `no-preference` — same
outcome, single source of truth in `base.css`.

## 12. Images — multi-width renditions generated; onerror hides breakage

Pillow (build/test-time only; `build.py` itself stays stdlib-only) generated
`hero-800/1600.webp` and `service-{tax,bookkeeping,consulting}-400/800.webp`
(LANCZOS, quality 82). `variants=` + `sizes=` are wired on every image
(cards: `(min-width: 56rem) 30vw, 90vw`). Every `<img>` also carries
`onerror="this.hidden=true"` per DONE #4 — if a strict CSP is ever added,
move this to a delegated error listener (inline handlers and CSP don't mix).

## 13. Hero contrast — recomputed honestly; the H2 halo is load-bearing

`test_pages.py` does two things: (a) parses the CSS and asserts the scrim
implements the corrected design (central alpha 57.37% desktop ≥ the
documented 55% minimum; 75.85% mobile, strengthened); (b) recomputes white-
text contrast over the actual shipped `hero.webp` (cover-crop aware,
brightest pixel in the headline rect) through the full rendered stack.

Finding: scrim-only worst-pixel is 2.28:1 desktop / 4.16:1 mobile on this
bright image — the shipped photo's center is a near-white wall, brighter
than whatever reference the workstream's 4.61/8.86 figures assumed (those
imply a reference background around L 0.27–0.42 and could NOT be reproduced
with a scrim-only model against hero.webp). WITH the H2 text-shadow halo
(the combined 0.82 peak alpha at glyph edges, where WCAG measures),
contrast is 8.19:1 desktop / 11.58:1 mobile — comfortably AA. The halo is
therefore load-bearing for this image, and the test guards it: weakening
the halo fails the build. No scrim darkening was applied — the corrected
design values stand as specified.

## 14. JSON-LD plumbing

`head.html` has a `{{jsonld}}` slot. Every page gets `localbusiness.json`
(`AccountingService`, validated as JSON at build time — invalid JSON
fail-closes the build). Frontmatter `jsonld: faq` appends extra blocks.
OG tags (`og:title/description/url/image`, `twitter:card`) derive from the
page's own frontmatter; `og:image` is the matched-set hero.

## 15. Flat dist/ mapping kept; /portal/* allowlisted in tests

`src/pages/about-us.html` → `dist/about-us.html` (flat, per Phase-0
convention); nginx serves `/about-us/` → `about-us.html`, and canonical
meta uses the trailing-slash URLs. The link-integrity test allowlists
`/portal/login` (+ `/portal/accept`) — the Phase-5 shells don't exist in
dist/ yet, and the test must not force dead links or forbid future ones.

## 16. Tooling notes

- Pillow 12.3.0 installed into `/home/hatch/workspace/jaroaccounting-rebuild/.venv`
  (image renditions + contrast tests). `playwright` 1.63.0 pip package also
  installed; Chromium 153 was fetched by direct CDN download to
  `~/workspace/pw-browsers/chromium-1243` and the DONE #9 smoke run passed
  16/16 (see §6).
- Test suite: `frontend/tests/test_build.py` (Phase 0, untouched) +
  `frontend/tests/test_pages.py` (Phase 1, 54 tests) — 65 passed on
  3 consecutive runs.

---

# Phase-2 Contact Form Wiring — Decisions & Assumptions

Date: 2026-10-05. Phase-2 builder (contact lead pipeline frontend).
`POST /api/v1/inquiries` is live on the backend; this wires the existing
contact page shell to it.

## 1. `scripts:` frontmatter — the builder grows one feature

`build.py` gains a `scripts:` frontmatter key (`scripts: contact.js` →
`<script type="module" src="/js/contact.js"></script>` before `</body>`).
Fail-closed like images: a bad name or a file missing from `src/js/`
aborts the build. ES modules (deferred by default); no inline scripts
(the Phase-1 CSP note on inline `onerror` handlers is unaffected — page
scripts are external files). This is the reusable seam the Phase-3
booking widget will use too; hand-placing `<script>` tags in page bodies
would rot the same way hand-placed chrome did.

## 2. contact.js — playbook C1–C4, no framework

Vanilla JS (the site has no framework; React-isms from the playbook are
translated): `ApiRequestError` normalizes every failure (C1); the catch
switches on `error.code` for per-field rendering, idempotency-conflict
guidance, rate-limit wait copy, and a try-again panel for 5xx/network
(C2); a `busy` flag + disabled button with `finally` re-enable (C3);
`crypto.randomUUID()` per submit ATTEMPT as `Idempotency-Key` (C4).
Native HTML constraints (`required`, `type=email`, `maxlength`) own
client validation; the server only sees structurally valid data.

## 3. `data-endpoint` is read per submit, not once at load

The form's `data-endpoint` (default `/api/v1/inquiries`) is read inside
the submit handler so a test harness can retarget the form without a
reload — this is the seam the Playwright server-error test uses
(pointing at a test-only 500 route). Same-origin relative URL in
production (nginx serves `dist/` at the root and proxies `/api`).

## 4. Success and error states reuse existing components

- Success panel uses `.notice-box` (the component Phase 1 generalized
  for "the consultation shell, thank-you page, and future Phase-2/3
  states") — no new CSS. Copy is F7-honest: "your message is queued",
  never "sent".
- Per-field errors reuse F2 (`aria-invalid`, `.field-error`) and F3
  (`.error-summary`, `role="alert"`, focus moved to the summary) —
  both shipped in Phase 1 for exactly this.
- The "Prefer to talk? Book a free consultation" cross-link and the PII
  microcopy ("Please don't include SINs…") are untouched, per plan.
- The old `action="/contact-us/" method="post"` is removed: JS owns
  submission, and a dead action to a static file is worse than none.
  No-JS fallback is out of scope (the C-series is JS by design).

## 5. Double-submit defense is three layers

Disabled button (synchronous in the handler) → `busy` guard (covers
submit events that bypass the button: rapid Enters, `requestSubmit`) →
server-side idempotency key. The Playwright test drives the middle layer
(two `requestSubmit()` calls in one task) and asserts exactly one row.

## 6. Playwright environment quirks (recorded for Phase 3+)

- Chromium is reused from `~/workspace/pw-browsers/chromium-1243` via
  `executable_path` (never re-downloaded); launch with
  `--no-proxy-server` — the sandbox's proxy env vars would otherwise
  route loopback traffic at the egress proxy.
- The test's httpx readiness probe uses `httpx.Client(trust_env=False)`
  for the same reason (proxy `no_proxy` parsing raised `InvalidURL` on
  `127.0.0.1` URLs).
- The journey test stands up the real app + `dist/` on one origin via a
  test-only `StaticFiles` mount (the nginx split's stand-in) and runs the
  real outbox worker in-process, asserting both capture artifacts.

## 7. Verification

- `frontend/tests/test_contact_journey.py`: 4 Playwright tests
  (desktop journey, phone journey + no-overflow, double-submit,
  server-error recovery) — all pass.
- Existing suites untouched and green: `test_build.py` +
  `test_pages.py` — 65 passed.

---

# Phase 3 — Booking widget frontend (2026-10-06)

New: `src/js/booking.js` (widget), `src/js/booking-api.js` (shared
helpers), `src/js/booking-thanks.js` (thank-you renderer),
`src/js/booking-manage.js` + `src/pages/booking-manage.html` (tokenized
manage page), `src/css/booking.css` (imported in `style.css`).
Rewrote `src/pages/free-30-min-consultation.html` (replaces the
"being rebuilt" shell) and upgraded
`src/pages/thank-you-for-your-booking.html` (dynamic render + static
fallback).

## Decisions

1. **Manage page added as in-scope necessity.** The confirmation's
   one link set (`/booking/manage/{token}#reschedule|#cancel`) must
   resolve somewhere; without a page the links are dead. Token from
   the URL path; invalid/expired/revoked tokens render one generic
   404 panel (A5 — never distinguishes the cases).
2. **Deploy note (Phase 4):** the web server must serve
   `booking-manage.html` for `/booking/manage/*` (e.g. nginx
   `try_files /booking-manage.html =404`); the same applies to the
   pretty `/thank-you-for-your-booking/` URL. Noted in the page
   source; Playwright stands in with test-only routes.
3. **Change-link semantics.** Links are plain step switches — zero
   network writes on click (asserted in Playwright). Email change →
   form with values preserved → re-submit creates a fresh intent via
   `supersedes_intent_id` (new code to the new address; old hold
   expired atomically — no 409 on your own slot). Slot/timezone change
   → back to the calendar (fresh availability re-check is
   server-side at confirm/intent).
4. **Timezone-first is a hard gate.** The modal can't be
   Escape-dismissed until a zone is chosen; every slot renders in the
   guest zone with the firm-zone equivalent beneath. Country re-sorts
   the IANA list (optgroup), never restricts it; default
   Canada / America/Vancouver.
5. **Double-submit wiring.** The details form uses only the form
   `submit` listener (Enter + click); an earlier `onclick` + `submit`
   double-wiring was removed — it fired two intent POSTs.
6. **Thank-you page contract.** Renders from the stored confirm
   projection (`reference`, `email_masked`, `reschedule_url`,
   `cancel_url`, `ics_url`, `replayed` — flat fields, not nested).
   `replayed` shows the "reloaded from your earlier confirmation"
   note. No stored result → the honest static copy stays.
7. **Failure honesty.** Stale calendar → no days, explicit message
   with the phone number, working retry (fail-closed, D-31). 409 at
   intent or confirm → "That time was just taken" + refreshed slot
   list. Interstitial `<dialog>` uses native showModal (focus-trapped);
   backdrop click dismisses, Escape never skips verification.
8. **Timezone modal bug found by tests:** closing the dialog never
   unhid the calendar step — fixed (`showStep("calendar")` on
   confirm; a zone change resets day/slot).

## Verification

- `frontend/tests/test_booking_journey.py`: 5 Playwright tests —
  full booking desktop, phone journey (+ no horizontal overflow),
  change-link no-write rules (+ email-change → fresh code path),
  409 re-pick (two contexts, exactly one booking), stale-calendar
  fail-closed + retry recovery. All pass.
- `test_contact_journey.py` (Phase 2): 4 passed — unaffected.
- Test scaffolding: module fixture rebuilds `dist/`, syncs a fresh
  stub calendar, serves pretty URLs via test-only routes; codes are
  read from the captured outbox via `destination_hash`
  (`jaro-outbox-dest-v1` domain).

## Phase 5 — Portal frontend (2026-10-06)

### Pages

- `/portal/login/` — branded login, posture text ("Two-step verification
  required · Signed out after 15 minutes idle · Access by invitation only"),
  "Need help signing in?" expander with phishing guidance (what a real Jaro
  invite looks like: from jared@jaroaccounting.com, one link to
  /portal/accept, 7-day expiry, never asks for password).
- `/portal/accept/` — invitation verify → masked email → set-password form.
- `/portal/reset-password/` — request (always 200, no oracle) + complete.
- `/portal/` — dashboard with terms modal (no dismiss: Accept or Sign out),
  TOTP enrollment (manual key + two-code confirm + recovery codes shown
  once), session-warning banner.
- `/portal/documents/`, `/portal/filings/`, `/portal/invoices/` — F2/F3/F4.
- `/portal/admin/` — owner console (dossier tabs, invitations, audit).

### Key judgments

1. **Session watch polls the authoritative `expires_in_seconds`.** The
   `startSessionWatch` polls every 30s; at ≤120s shows the banner with
   countdown; one-click extend is idempotent (≤1/60s server-side). 401 →
   redirect to login with `?reason=session-expired`.
2. **Terms modal has no dismiss.** Accept or Sign out — the backend 403s
   all portal routes until accepted (except terms/2fa/session).
3. **TOTP enrollment UI.** Manual key (copyable) + provisioning URI link;
   two consecutive codes required; recovery codes shown once with a
   "I have saved" checkbox gating the Done button.
4. **Upload uses Idempotency-Key.** If the session expires mid-upload, the
   user signs back in and retries — a completed upload won't duplicate.
5. **Nginx deploy note.** Pretty URLs (`/portal/*`) must map to flat dist
   files: `/portal/` → `portal.html`, `/portal/login/` → `portal-login.html`,
   etc. (same pattern as booking-manage: `try_files /portal-login.html =404`).

### Verification

- `frontend/tests/test_portal_journey.py`: 6 Playwright tests — first-run
  desktop (invite → 2FA → terms → dashboard), vault round-trip
  (upload → Jared sees → share → download byte-identical), filing lifecycle
  (five-step timeline), invoice PDF compare (byte-identical), session-timeout
  warning + extend, phone first-run (no horizontal overflow). All pass.
- Test scaffolding: module fixture rebuilds `dist/`, serves pretty URLs via
  test-only routes; rate_windows cleaned between tests (autouse fixture).
