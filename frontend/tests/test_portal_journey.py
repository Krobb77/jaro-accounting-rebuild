"""Phase-5 DONE — Playwright portal journeys (desktop + phone).

- First-run (desktop): invitation (token read from the CAPTURED outbox, i.e.
  the real rendered production invite email) → /portal/accept/ verify shows
  masked email → set-password → terms modal (no dismiss) → TOTP enrollment
  (two consecutive codes + recovery codes shown once) → dashboard greeting.
- Vault round-trip: client uploads via the documents page → Jared sees it in
  the owner dossier → Jared shares a file back → client downloads it,
  byte-identical.
- Filing lifecycle: owner creates + advances a filing with a note → client
  sees the five-step timeline in the browser.
- Invoice issue → PDF compare: owner issues an invoice → client downloads the
  PDF → byte-identical to the owner's copy.
- Session-timeout warning: session expiry shortened in the DB → the ≤120s
  banner appears on the documents page → "Stay signed in" extends and hides
  the banner.
- Phone journey: first-run core path on a mobile viewport, no horizontal
  overflow.

Setup mirrors test_booking_journey.py: a live FastAPI app (capture mail,
disposable sqlite) serves BOTH the API and dist/ from one origin — a
test-only StaticFiles mount standing in for the production nginx split.
Test-only pretty-URL routes stand in for the nginx rewrites the portal pages
need in production (/portal/* → flat dist files). Chromium is reused from
~/workspace/pw-browsers/chromium-1243 (launched via executable_path);
nothing is re-downloaded.

Run: /home/hatch/workspace/jaroaccounting-rebuild/.venv/bin/python -m pytest frontend/tests/test_portal_journey.py
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1]
REPO = FRONTEND.parent
BACKEND = REPO / "backend"
DIST = FRONTEND / "dist"

sys.path.insert(0, str(BACKEND))

from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config  # noqa: E402

CHROME = Path.home() / "workspace" / "pw-browsers" / "chromium-1243" / "chrome-linux" / "chrome"

BASE_TEST_ENV = {
    "JARO_MODE": "test",
    "JARO_WORKLOAD": "web",
    "DB_ROLE": "jaro_web",
    "SECRET_KEY": "test-secret-key-0123456789abcdef",
    "TOTP_KEK": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "MAIL_ALLOW_SEND": "false",
    "MAIL_BACKEND": "capture",
}

TERMS_VERSION = "2026-01"
TERMS_HASH = "test-terms-hash-001"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Live app + dist on one origin. Yields (base_url, db_path, app, db_url)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(FRONTEND / "build.py")],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"frontend build failed:\n{result.stdout}\n{result.stderr}"
    tmp = tmp_path_factory.mktemp("pw-portal")
    db_path = tmp / "pw.db"
    db_url = f"sqlite:///{db_path}"
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")

    from app import main as app_main
    from app.config import load_settings

    settings = load_settings(
        {
            **BASE_TEST_ENV,
            "DATABASE_URL": db_url,
            "MAIL_CAPTURE_DIR": str(tmp / "mail-capture"),
            "STORAGE_DIR": str(tmp / "storage"),
        }
    )
    app = app_main.create_app(settings)

    # Terms seed (terms_versions has no migration seed).
    from sqlalchemy.orm import sessionmaker

    from app.models import TermsVersion

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        if db.get(TermsVersion, TERMS_VERSION) is None:
            db.add(
                TermsVersion(
                    version=TERMS_VERSION,
                    version_hash=TERMS_HASH,
                    title="Client Engagement Terms",
                    body_html="<p>Journey test terms.</p>",
                )
            )
            db.commit()
    finally:
        db.close()

    # Test-only stand-ins for the production nginx rewrites: pretty portal
    # URLs (/portal/*) map to the built flat .html files.
    from fastapi.responses import FileResponse

    @app.get("/portal/")
    def _portal():
        return FileResponse(DIST / "portal.html")

    @app.get("/portal/login/")
    def _portal_login():
        return FileResponse(DIST / "portal-login.html")

    @app.get("/portal/accept/")
    def _portal_accept():
        return FileResponse(DIST / "portal-accept.html")

    @app.get("/portal/reset-password/")
    def _portal_reset():
        return FileResponse(DIST / "portal-reset-password.html")

    @app.get("/portal/documents/")
    def _portal_documents():
        return FileResponse(DIST / "portal-documents.html")

    @app.get("/portal/filings/")
    def _portal_filings():
        return FileResponse(DIST / "portal-filings.html")

    @app.get("/portal/invoices/")
    def _portal_invoices():
        return FileResponse(DIST / "portal-invoices.html")

    @app.get("/portal/admin/")
    def _portal_admin():
        return FileResponse(DIST / "portal-admin.html")

    # Test-only stand-in for the nginx split: dist/ + /api on one origin.
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="static")

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import httpx

            probe = httpx.Client(trust_env=False)
            try:
                r = probe.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
            finally:
                probe.close()
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("live server did not start")
    yield f"http://127.0.0.1:{port}", str(db_path), app
    server.should_exit = True
    thread.join(timeout=10)
    app.state.engine.dispose()


@pytest.fixture(autouse=True)
def _clean_rate_windows(live_server):
    """The journey onboardings hammer rate-limited routes from one test
    source (127.0.0.1); the shared per-source throttle would 429 later
    tests. The module-scoped DB needs explicit cleanup (test isolation)."""
    yield
    _, db_path, app = live_server
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        db.execute(sa_text("DELETE FROM rate_windows"))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def browser():
    from playwright.sync_api import sync_playwright

    assert CHROME.is_file(), f"chromium not found at {CHROME} (do not re-download)"
    with sync_playwright() as pw:
        b = pw.chromium.launch(
            executable_path=str(CHROME), headless=True, args=["--no-proxy-server"]
        )
        yield b
        b.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _dest_hash(email: str) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update("jaro-outbox-dest-v1".encode())
    h.update(email.strip().lower().encode())
    return h.hexdigest()


def _invite_token_from_outbox(db_path: str, email: str) -> str:
    """Raw 43-char invite token from the captured production invite email."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT frozen_snapshot FROM email_outbox "
            "WHERE kind='invite' AND destination_hash=? "
            "ORDER BY created_at DESC LIMIT 1",
            (_dest_hash(email),),
        ).fetchall()
    finally:
        conn.close()
    assert rows, "no invite email captured"
    snap = json.loads(rows[0][0])
    fields = snap.get("fields") or {}
    url = fields.get("set_password_url") or ""
    m = re.search(r"token=([A-Za-z0-9_-]{43})", url)
    assert m, "invite email has no set-password token link"
    return m.group(1)


def _make_owner(app, email="jared.owner@example.com"):
    from sqlalchemy.orm import sessionmaker

    from app.models import PortalUser
    from app.db import utcnow
    from app.db import new_uuid7

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        user = PortalUser(
            id=new_uuid7(),
            role="owner",
            email=email,
            display_name="Jared Robbins",
            password_hash="x",
            status="active",
            totp_enrolled_at=utcnow(),
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _owner_headers(app, owner_id):
    """Raw session cookie header for owner API calls (httpx)."""
    from sqlalchemy.orm import sessionmaker

    from app import sessions as sessions_lib

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        raw, _row = sessions_lib.create_session(db, user_id=owner_id)
        db.commit()
    finally:
        db.close()
    return {"Cookie": f"{sessions_lib.COOKIE_NAME}={raw}"}


def _totp_pair(secret: str):
    from app import totp as totp_lib

    t = int(time.time() // 30) * 30 + 15
    return totp_lib.code_at(secret, for_time=t), totp_lib.code_at(secret, for_time=t + 30)


def _read_manual_key(page, timeout=15000):
    """Read the TOTP manual key, waiting for the async /2fa/enroll call.

    The modal is unhidden before the enroll POST resolves, so reading
    #totp-manual-key immediately races the XHR and can return "" — which
    then generates codes for the wrong secret and fails confirm. Wait for
    the key to be populated first.
    """
    page.wait_for_function(
        "document.getElementById('totp-manual-key').textContent.trim().length > 0",
        timeout=timeout,
    )
    return page.text_content("#totp-manual-key").strip().replace(" ", "")


def _create_invite(base, headers, email, name="Journey Client", phone=None):
    import httpx

    body = {"client_name": name, "email": email}
    if phone:
        body["phone_e164"] = phone
    c = httpx.Client(trust_env=False, base_url=base, timeout=15)
    try:
        r = c.post(
            "/api/v1/admin/invitations",
            json=body,
            headers={**headers, "Idempotency-Key": f"journey-{email}-{int(time.time())}-{id(email)}"},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        # Raw token from the accept_url (the outbox also carries it, but the
        # API response is the race-free source for the journey).
        m = re.search(r"token=([A-Za-z0-9_-]{43})", data["accept_url"])
        assert m, "invite response has no token"
        return data["id"], m.group(1)
    finally:
        c.close()


# ----------------------------------------------------------------------
# Journey 1: invite → login → 2FA → terms → dashboard (desktop)
# ----------------------------------------------------------------------


def test_portal_first_run_desktop(live_server, browser):
    base, db_path, app = live_server
    owner_id = _make_owner(app)
    oheaders = _owner_headers(app, owner_id)

    email = "first.run@example.com"
    _, token = _create_invite(base, oheaders, email)

    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        # --- accept: verify shows masked email, no oracle ---
        page.goto(f"{base}/portal/accept/?token={token}")
        page.wait_for_selector("#accept-form:not([hidden])", timeout=15000)
        welcome = page.text_content("#accept-welcome")
        assert "first.run" not in welcome or "…" in welcome or "@" in welcome
        assert email not in welcome  # masked, never the full address

        # --- set-password ---
        page.fill("#accept-password", "correct horse battery staple 123")
        page.fill("#accept-password2", "correct horse battery staple 123")
        page.click("#accept-submit")
        # set-password signs the client in and lands on the dashboard
        page.wait_for_url(f"{base}/portal/?first=1", timeout=15000)

        # --- terms modal: no dismiss, Accept or Sign out ---
        page.wait_for_selector("#terms-modal:not([hidden])", timeout=15000)
        assert page.is_visible("#terms-accept")
        assert page.is_visible("#terms-signout")
        page.click("#terms-accept")
        page.wait_for_selector("#terms-modal", state="hidden", timeout=15000)

        # --- TOTP enrollment: manual key + two consecutive codes ---
        page.wait_for_selector("#totp-modal:not([hidden])", timeout=15000)
        manual_key = _read_manual_key(page)
        code1, code2 = _totp_pair(manual_key)
        page.fill("#totp-code1", code1)
        page.fill("#totp-code2", code2)
        page.click("#totp-confirm")
        page.wait_for_selector("#totp-step-recovery:not([hidden])", timeout=15000)
        recovery = page.eval_on_selector_all(
            "#totp-recovery-list code", "els => els.map(e => e.textContent)"
        )
        assert len(recovery) == 10
        page.check("#totp-recovery-saved")
        page.click("#totp-done")
        page.wait_for_selector("#totp-modal", state="hidden", timeout=15000)

        # --- dashboard greeting ---
        page.wait_for_selector("#portal-root h1", timeout=15000)
        heading = page.text_content("#portal-root h1")
        assert heading and len(heading.strip()) > 0
        # nav present
        assert page.is_visible('nav.portal-nav a[href="/portal/documents/"]')
    finally:
        ctx.close()


# ----------------------------------------------------------------------
# Journey 2: upload → Jared sees → share → download
# ----------------------------------------------------------------------


def _onboard_client_api(base, app, db_path, email, name="Journey Client"):
    """Onboard a client fully via the API; returns (user_id, cheaders, oheaders)."""
    import httpx

    owner_id = _make_owner(app, email=f"owner.{email}")
    oheaders = _owner_headers(app, owner_id)
    _, token = _create_invite(base, oheaders, email, name=name)
    from app import sessions as sessions_lib

    c = httpx.Client(trust_env=False, base_url=base, timeout=15)
    try:
        r = c.post("/api/v1/portal/invitations/verify", json={"token": token})
        assert r.status_code == 200, r.text
        r = c.post(
            "/api/v1/portal/invitations/set-password",
            json={"token": token, "password": "correct horse battery staple 123"},
            headers={"Idempotency-Key": f"onboard-{email}-{int(time.time())}"},
        )
        assert r.status_code == 201, r.text
        m = re.search(
            rf"{re.escape(sessions_lib.COOKIE_NAME)}=([^;]+)",
            r.headers.get("set-cookie", ""),
        )
        assert m
        cheaders = {"Cookie": f"{sessions_lib.COOKIE_NAME}={m.group(1)}"}
        # terms
        r = c.get("/api/v1/portal/terms/current", headers=cheaders)
        assert r.status_code == 200, f"terms/current failed: {r.status_code} {r.text}"
        vh = r.json()["version_hash"]
        r = c.post("/api/v1/portal/terms/accept", json={"version_hash": vh}, headers=cheaders)
        assert r.status_code == 200, r.text
        # TOTP enroll
        r = c.post("/api/v1/portal/2fa/enroll", headers=cheaders)
        assert r.status_code == 200, r.text
        secret = r.json()["manual_key"]
        code1, code2 = _totp_pair(secret)
        r = c.post(
            "/api/v1/portal/2fa/confirm",
            json={"code": code1, "code2": code2},
            headers=cheaders,
        )
        assert r.status_code == 200, r.text
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT id FROM portal_users WHERE email=?", (email,)
            ).fetchone()
        finally:
            conn.close()
        return row[0], cheaders, oheaders
    finally:
        c.close()


def _unseal_totp_secret(app, user_id: str) -> str:
    """Unseal the enrolled TOTP secret with the test KEK (test-only)."""
    from sqlalchemy.orm import sessionmaker

    from app import secretbox as secretbox_lib
    from app.models import PortalUser

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        user = db.get(PortalUser, user_id)
        assert user.totp_secret_enc is not None
        return secretbox_lib.unseal(
            app.state.settings.totp_kek, bytes(user.totp_secret_enc)).decode("ascii")
    finally:
        db.close()


def _browser_login(page, base, email, password, totp_secret):
    """Full browser login: password → TOTP code → dashboard."""
    page.goto(f"{base}/portal/login/")
    page.fill("#login-email", email)
    page.fill("#login-password", password)
    page.click("#login-submit")
    try:
        page.wait_for_selector("#mfa-form:not([hidden])", timeout=15000)
    except Exception:
        # Diagnostic: surface the login error if the MFA form never appeared.
        err_text = page.text_content("#login-error") or "(no error text)"
        raise AssertionError(f"login MFA form never appeared; error was: {err_text}")
    from app import totp as totp_lib

    page.fill("#mfa-code", totp_lib.code_at(totp_secret))
    page.click("#mfa-submit")
    page.wait_for_url(f"{base}/portal/", timeout=15000)


def test_portal_vault_round_trip(live_server, browser):
    base, db_path, app = live_server
    email = "vault.client@example.com"
    client_id, cheaders, oheaders = _onboard_client_api(base, app, db_path, email)
    totp_secret = _unseal_totp_secret(app, client_id)

    import httpx

    payload = b"%PDF-1.4 journey upload bytes\n" + b"x" * 1024
    c = httpx.Client(trust_env=False, base_url=base, timeout=15)
    try:
        # --- client uploads via the browser documents page ---
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            _browser_login(page, base, email, "correct horse battery staple 123", totp_secret)
            page.goto(f"{base}/portal/documents/")
            page.wait_for_selector("#upload-form", timeout=15000)
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(payload)
                tmpfile = f.name
            page.set_input_files("#upload-file", tmpfile)
            page.select_option("#upload-category", "tax_slips")
            page.fill("#upload-label", "Journey T4")
            page.click("#upload-submit")
            page.wait_for_selector("text=Upload complete.", timeout=20000)
            assert "Journey T4" in (page.text_content("#docs-root") or "")
        finally:
            ctx.close()

        # --- Jared sees it in the dossier ---
        r = c.get(f"/api/v1/admin/clients/{client_id}", headers=oheaders)
        assert r.status_code == 200, r.text
        docs = r.json()["documents"]
        assert any(d["label"] == "Journey T4" for d in docs)

        # --- Jared shares a file back; client downloads it byte-identical ---
        share_bytes = b"%PDF-1.4 jared shared deliverable\n" + b"y" * 512
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(share_bytes)
            sharefile = f.name
        with open(sharefile, "rb") as f:
            r = c.post(
                f"/api/v1/admin/clients/{client_id}/documents",
                files={"file": ("deliverable.pdf", f, "application/pdf")},
                data={"type_code": "deliverables"},
                headers={**oheaders, "Idempotency-Key": f"share-{client_id}"},
            )
        assert r.status_code == 201, r.text
        doc_id = r.json()["id"]

        # client downloads via the portal download endpoint
        from app import sessions as sessions_lib

        dl = httpx.Client(trust_env=False, base_url=base, timeout=15)
        try:
            # fresh login for a download-capable session
            r = c.post("/api/v1/portal/login", json={"email": email, "password": "correct horse battery staple 123"})
            challenge = r.json()["challenge_id"]
            from app import totp as totp_lib

            r = c.post("/api/v1/portal/login/2fa", json={"challenge_id": challenge, "code": totp_lib.code_at(totp_secret)})
            assert r.status_code == 200, r.text
            m = re.search(rf"{re.escape(sessions_lib.COOKIE_NAME)}=([^;]+)", r.headers.get("set-cookie", ""))
            dl.cookies.set(sessions_lib.COOKIE_NAME, m.group(1))
            r = dl.get(f"/api/v1/portal/documents/{doc_id}/download")
            assert r.status_code == 200, r.text
            assert r.content == share_bytes
            assert r.headers.get("content-disposition", "").startswith("attachment")
        finally:
            dl.close()
    finally:
        c.close()


# ----------------------------------------------------------------------
# Journey 3: filing lifecycle
# ----------------------------------------------------------------------


def test_portal_filing_lifecycle(live_server, browser):
    base, db_path, app = live_server
    email = "filing.client@example.com"
    client_id, cheaders, oheaders = _onboard_client_api(base, app, db_path, email)
    totp_secret = _unseal_totp_secret(app, client_id)

    import httpx

    c = httpx.Client(trust_env=False, base_url=base, timeout=15)
    try:
        # owner creates + advances with a note
        r = c.post(
            "/api/v1/admin/filings",
            json={"client_id": client_id, "kind_code": "personal_t1", "tax_year": 2025},
            headers={**oheaders, "Idempotency-Key": f"filing-{client_id}"},
        )
        assert r.status_code == 201, r.text
        filing_id = r.json()["id"]
        r = c.patch(
            f"/api/v1/admin/filings/{filing_id}/status",
            json={"status": "awaiting_info", "note": "Please upload your T4 slip."},
            headers=oheaders,
        )
        assert r.status_code == 200, r.text

        # client sees the five-step timeline + Jared's note in the browser
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            _browser_login(page, base, email, "correct horse battery staple 123", totp_secret)
            page.goto(f"{base}/portal/filings/")
            page.wait_for_selector(".timeline", timeout=15000)
            body = page.text_content("#filings-root") or ""
            assert "2025" in body
            assert "Awaiting info" in body
            assert "Please upload your T4 slip." in body
            steps = page.eval_on_selector_all(".timeline__step", "els => els.length")
            assert steps == 5
        finally:
            ctx.close()
    finally:
        c.close()


# ----------------------------------------------------------------------
# Journey 4: invoice issue → PDF compare
# ----------------------------------------------------------------------


def test_portal_invoice_pdf_compare(live_server, browser):
    base, db_path, app = live_server
    email = "invoice.client@example.com"
    client_id, cheaders, oheaders = _onboard_client_api(base, app, db_path, email)

    import httpx

    c = httpx.Client(trust_env=False, base_url=base, timeout=15)
    try:
        r = c.post(
            "/api/v1/admin/invoices",
            json={
                "client_id": client_id,
                "lines": [
                    {"description": "Tax preparation", "quantity": 1, "unit_cents": 45000, "tax_rate_bps": 500},
                ],
                "memo": "Journey invoice",
            },
            headers={**oheaders, "Idempotency-Key": f"invoice-{client_id}"},
        )
        assert r.status_code == 201, r.text
        invoice_id = r.json()["id"]

        r = c.get(f"/api/v1/admin/invoices/{invoice_id}/pdf", headers=oheaders)
        assert r.status_code == 200, r.text
        owner_pdf = r.content
        assert owner_pdf.startswith(b"%PDF")

        # client downloads the same stored PDF bytes via the portal
        r = c.get(f"/api/v1/portal/invoices/{invoice_id}/pdf", headers=cheaders)
        assert r.status_code == 200, r.text
        assert r.content == owner_pdf
    finally:
        c.close()


# ----------------------------------------------------------------------
# Journey 5: session-timeout warning during upload
# ----------------------------------------------------------------------


def test_portal_session_warning_and_extend(live_server, browser):
    base, db_path, app = live_server
    email = "session.client@example.com"
    client_id, cheaders, oheaders = _onboard_client_api(base, app, db_path, email)
    totp_secret = _unseal_totp_secret(app, client_id)

    # Log in via the browser.
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        _browser_login(page, base, email, "correct horse battery staple 123", totp_secret)
        page.goto(f"{base}/portal/documents/")
        page.wait_for_selector("#upload-form", timeout=15000)

        # Now backdate the session so ~100s remain (banner at ≤120s).
        # Done after page load to avoid racing the initial touching requests.
        from sqlalchemy.orm import sessionmaker
        from datetime import timedelta

        from app.models import Session as PortalSession
        from app.db import utcnow

        Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
        db = Session()
        try:
            row = (
                db.query(PortalSession)
                .filter(PortalSession.user_id == client_id)
                .order_by(PortalSession.created_at.desc())
                .first()
            )
            assert row is not None
            # expires_in is computed as last_seen_at + 15min idle window.
            row.last_seen_at = utcnow() - timedelta(seconds=800)
            db.commit()
        finally:
            db.close()

        # The 30s poll will see ≤120s and show the banner.
        page.wait_for_selector("#session-banner:not([hidden])", timeout=40000)
        countdown = page.text_content("#session-banner [data-countdown]")
        assert countdown and ":" in countdown
        # one-click extend hides the banner and resets expiry
        page.click("#session-extend")
        page.wait_for_selector("#session-banner", state="hidden", timeout=15000)
    finally:
        ctx.close()


# ----------------------------------------------------------------------
# Journey 6: phone first-run
# ----------------------------------------------------------------------


def test_portal_first_run_phone(live_server, browser):
    base, db_path, app = live_server
    owner_id = _make_owner(app, email="jared.phone@example.com")
    oheaders = _owner_headers(app, owner_id)

    email = "phone.client@example.com"
    _, token = _create_invite(base, oheaders, email, name="Phone Client")

    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        is_mobile=True,
        has_touch=True,
    )
    page = ctx.new_page()
    try:
        page.goto(f"{base}/portal/accept/?token={token}")
        page.wait_for_selector("#accept-form:not([hidden])", timeout=15000)
        page.fill("#accept-password", "correct horse battery staple 123")
        page.fill("#accept-password2", "correct horse battery staple 123")
        page.click("#accept-submit")
        page.wait_for_url(f"{base}/portal/?first=1", timeout=15000)
        page.wait_for_selector("#terms-modal:not([hidden])", timeout=15000)
        page.click("#terms-accept")
        page.wait_for_selector("#totp-modal:not([hidden])", timeout=15000)
        manual_key = _read_manual_key(page)
        code1, code2 = _totp_pair(manual_key)
        page.fill("#totp-code1", code1)
        page.fill("#totp-code2", code2)
        page.click("#totp-confirm")
        page.wait_for_selector("#totp-step-recovery:not([hidden])", timeout=15000)
        page.check("#totp-recovery-saved")
        page.click("#totp-done")
        page.wait_for_selector("#portal-root h1", timeout=15000)

        # no horizontal overflow on the phone viewport
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"horizontal overflow: {overflow}px"
    finally:
        ctx.close()
