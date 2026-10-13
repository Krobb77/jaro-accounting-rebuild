# Jaro Accounting — Deploy Checklist (G6)

Run this list on every deploy, in order. The migration job (§2) completes
before traffic shifts (H5); the smoke (§4) runs after. Any NO below aborts
the deploy — fix forward on the old build, never by rushing the new one.

## 1. Pre-flight

- [ ] Artifact built from a clean tree; **digest pinned** (record the digest
      in the deploy log — H5 pins by digest, not by floating tag).
- [ ] `unzip -t`-equivalent / packaging check: no dev-only packages in the
      artifact; junk-class scan clean (CI gate `test_12_packaging`).
- [ ] `.env` for the target present from the secret store (never hand-written
      on the host): `JARO_MODE`, `JARO_WORKLOAD`/`DB_ROLE` pairs
      (web↔jaro_web, worker↔jaro_worker, migrate↔jaro_migrate), `SECRET_KEY`,
      `TOTP_KEK`, `MAIL_ALLOW_SEND`, `MAIL_BACKEND`, `DATABASE_URL`,
      `STORAGE_DIR`. No `*_FILE` mount missing.
- [ ] `MAIL_ALLOW_SEND=false` + `MAIL_BACKEND=capture` on every non-prod
      target (H1 capture-in-staging). Production's first real send needs the
      explicit opt-in per the runbook §4.

## 2. Migration-first (H5)

- [ ] One-shot migrate job (`jaro_migrate` role) runs `alembic upgrade head`
      against the target database **before** any new code takes traffic.
- [ ] Job exit 0; `alembic heads` shows a single head; `alembic current`
      matches the artifact's pinned head.
- [ ] If the job fails: STOP. Traffic stays on the old build (migrations are
      additive until the new code ships). Never auto-reverse data to "fix"
      a deploy — B1 downgrades refuse with live rows by design.

## 3. Process boot (H2)

- [ ] Web: `uvicorn app.main:create_app --factory` boots (H2 refuses bad
      secrets / wrong role / unpinned head / unset mail gate — a refusal
      here is the system working, not a bug).
- [ ] Worker: `python -m app.worker_main` boots with
      `JARO_WORKLOAD=worker` / `DB_ROLE=jaro_worker`; `--once` completes one
      cycle (proves DB + outbox-claim path end to end).
- [ ] Exactly one worker instance (G2: the lease-claim survives a second
      claimant, but two workers is a misconfiguration).

## 4. G6 smoke (built artifact, mocked hosting APIs where applicable)

- [ ] **7 pages render 200:** `/`, `/about-us/`, `/our-services/`,
      `/contact-us/`, `/free-30-min-consultation/`, `/privacy/`, `/terms/`
      (plus `/legal/`, `/faq/`, `/engagement-terms/` on the trust strip).
- [ ] **Probes answer:** `GET /healthz` 200; `GET /readyz` 200 with DB +
      outbox-claim probe green.
- [ ] **No dev-only packages** in the deployed environment
      (`pip freeze` vs the pinned `requirements.txt`).
- [ ] nginx config test passes (`nginx -t`); the `try_files` rewrites serve
      `/portal/login/` → `portal-login.html` and `/booking/manage/abc` →
      `booking-manage.html` (spot-check with curl).
- [ ] Legacy redirects spot-check: `/blog/` → 301 `/`; `www` → apex;
      `/thank-you-for-your-booking/` → 301 `/free-30-min-consultation/`.

## 5. Post-traffic-shift

- [ ] Watch `/readyz` and the 5xx rate for 15 minutes (runbook §5 thresholds).
- [ ] Confirm the worker is claiming: `email_outbox` shows fresh `captured`
      rows or an idle-but-healthy cycle (the `--once` log line).
- [ ] Dead-letter query returns zero new `dead` rows (runbook §5).

## 6. Rollback criteria

Roll back (DNS/traffic revert to the previous artifact) if any of: readiness
fails > 5 minutes, 5xx > 1% over 5 minutes, or the worker dead-letters on
the new build. Code rollback **never** auto-reverses migrations (H5) — the
additive-migration policy keeps the old code compatible with the new schema.
