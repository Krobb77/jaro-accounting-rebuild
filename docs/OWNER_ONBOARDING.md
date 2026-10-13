# Owner Onboarding — Jared's Hands-On Walkthrough

**Audience:** Jared (business owner, the only `owner`-role account).
**Format:** hands-on — Jared performs each action live during the onboarding
session (Phase 4 DONE #7). The build team watches and answers; nothing is
"shown" without being done.

Prerequisites for the session: production deployed, mail auth done
(`ops/cutover.md` §4), and Jared has the one-shot bootstrap secret from
the secret store.

---

## 1. Bootstrap + first login + TOTP enrollment ☐

- [ ] Set `OWNER_INITIAL_PASSWORD` in the secret store (exactly one of the
      var or the `_FILE` mount — H4 exactly-one-wins).
- [ ] `POST /api/v1/admin/bootstrap` with the secret → creates Jared's
      owner row. **Second call returns `410 bootstrap_consumed`** — the
      secret is single-use; after this step it is worthless (verify the
      410 in the session).
- [ ] Log in at `/portal/login` with email + the bootstrap password.
- [ ] **TOTP enrollment is mandatory** (owner has no SMS fallback — AUTH
      D1): scan/copy the manual key into an authenticator app, enter **two
      consecutive codes** to confirm, and **save the 10 recovery codes**
      shown once (they are never shown again).
- [ ] **Forced rotation:** change the password immediately
      (`password/change`) — the bootstrap secret must not remain a valid
      credential past this session. Confirm the old password no longer
      works.
- [ ] Note the posture line on the login page: "Two-step verification
      required · Signed out after 15 minutes idle · Access by invitation
      only." That is the contract every client sees too.

## 2. Invitation workflow ☐

The model: **Jared governs every account — there is no public signup.**
Each invitation is a single-use token with a **7-day expiry** (bold in the
email; coupled to the invite record — if the lifetime ever changes, the
copy changes with it).

- [ ] **Create:** owner console → Invitations → New. Enter client name,
      email, optional phone/note. The raw token is returned **once** —
      copy it only if sending outside the system email (normally the
      system emails it).
- [ ] **Read the trail:** open the invitation → `invitation_events`
      shows `created → emailed → accepted`; `email_status` uses the honest
      vocabulary (`queued`, `captured`, … — never "sent").
- [ ] **Resend = new token:** Resend **revokes the old token immediately**
      and issues a fresh invitation (old link dies on the spot; the old
      row reads `revoked`, reason `resend`). Never forward an old invite
      link after resending.
- [ ] **Revoke:** revoking a `created` invitation **cancels the queued
      email** (F2) — no stale invitation ever goes out. Revoking an
      `emailed` one kills the link; the client sees the safe generic
      `410 invitation_invalid` (no validity oracle).
- [ ] **Expiry:** after 7 days the invite flips to `expired` (+ audit row)
      automatically. Expired invites are re-issued, never extended.
- [ ] **Phishing guidance (what clients see):** the login page's "Need
      help signing in?" expander teaches clients what a real Jaro invite
      looks like — from `jared@jaroaccounting.com`, **one link** to
      `/portal/accept`, bold 7-day expiry, username shown, **"we'll never
      ask for your password by email or phone."** If a client forwards you
      a suspicious email, compare it against that anatomy.

## 3. Booking operations ☐

- [ ] **Reading availability:** the booking widget reads your real
      calendar (one-way read, 10-min poll). A fully-booked day renders
      disabled; BC stat holidays render unavailable. Slots show in the
      guest's timezone with the firm time beneath.
- [ ] **Calendar outage:** if the calendar read fails or goes stale past
      30 minutes, booking **fail-closes** (guests see an honest "call
      1-(778)-227-4891" message, never guesswork slots). Check
      `calendar_sync_state.last_error` first.
- [ ] **`calendar_sync_pending`:** a confirmed booking whose calendar
      write-back failed is flagged, not lost — the booking is real, the
      calendar event just didn't land. Re-check your calendar manually for
      these.
- [ ] **No-show lever:** owner console → booking → mark `no_show`. Two
      no-shows in 12 months puts the guest on `booking_hold` (they're told
      to call you). Threshold/message are tunable — confirm yours (§5 #8
      in the owner questions).
- [ ] **Manage links:** each confirmation email carries reschedule/cancel
      links (tokenized, valid 90 days post-appointment, dead on cancel).
      Cancel ≥ 24h ahead suppresses the pending reminder automatically —
      nothing stale goes out.

## 4. Portal operations ☐

- [ ] **Documents:** client uploads appear in the dossier immediately
      (visible to you, `document_received` notice queued). Your uploads to
      a client queue `document_shared`. **Retire** hides a file from the
      client but retains it (audit-logged) — there is no hard delete in v1.
      Files are `unscanned` by default (no real-time AV in v1 — never tell
      a client a file was "scanned").
- [ ] **Filings:** create → advance through `collecting → awaiting_info →
      preparing → filed → confirmed`. The **"Message to client" note** on
      `awaiting_info` is what the client sees on their dashboard — write
      it as client-facing copy. `filed` ≠ `confirmed` (sent to CRA vs
      acknowledged by CRA) — never conflate them.
- [ ] **Invoices:** issue (line items + per-line tax, due date default
      +30d) → the PDF is **generated at issuance and stored immutably** —
      the stored bytes are the invoice of record, and the client's
      download is byte-identical. Preview *is* the stored PDF. `pay`
      records `paid_at` + method note (money moves outside the portal);
      `void` needs a reason and **never recycles the number**. Series
      config (prefix/numbering) lives in the owner console — confirm the
      defaults (§5 owner question #5) before the first real invoice.
- [ ] **Revoke-all-sessions:** per-client lever kills every live session
      immediately (lost device, suspected sharing). The client just logs
      in again.
- [ ] **Client TOTP-disable:** if a client is locked out of 2FA, only you
      can disable it (reason required, audit-logged) — the client is then
      **forced to re-enroll at next login**. Clients cannot disable their
      own TOTP, ever.

## 5. Session and timeout expectations ☐

- [ ] Sessions slide on activity but expire after **15 minutes idle** —
      the banner at ≤ 2 minutes offers one-click "Stay signed in".
- [ ] **12-hour absolute cap:** even with activity, everyone re-logs-in
      after 12 hours. This is deliberate (bounds a stolen session), not a
      bug — tell clients before they discover it mid-tax-season.
- [ ] Expiry lands on `/portal/login?reason=session-expired` with the
      message; no data is lost (uploads are resumable, forms keep drafts
      client-side where the widget supports it).
- [ ] Six concurrent logins per client; the 6th evicts the oldest (with an
      audit event). If a client reports being "kicked out," check their
      session list before assuming an attack.

## 6. Backup-drill scheduling ☐

- [ ] **Acknowledge the open item:** backup + restore has been *written*
      (runbook §2) but **never drilled**. This is the top ops risk.
- [ ] **Schedule the drill for this week:** restore the latest dump +
      storage snapshot to a scratch host, log in as owner, download one
      invoice PDF byte-identical, render one booking. When it passes,
      update runbook §2 with the date and mark this item DRILLED.
- [ ] Until then: confirm the nightly dump + storage rsync jobs are
      actually running (check timestamps, not just the cron entry).

---

**Sign-off.** When every box above is checked live, Jared notes the date
here: _______________. The residual-risk register (BUILD_REPORT.md §8)
stays open until the 20 owner defaults (DECISIONS.md, decided 2026-10-08
as reversible config) are confirmed by Jared or formally deferred.
