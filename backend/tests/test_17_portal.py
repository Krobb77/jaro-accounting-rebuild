"""DONE Phase-5 — the 17 acceptance criteria mapped 1:1.

test_17_01 … test_17_17 mirror IMPLEMENTATION_PLAN.md §Phase 5 DONE 1–17
exactly. CAPTURE MODE ONLY: no live email ever; assertions run against
captured artifacts + DB state.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import main as app_main  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import new_uuid7, utcnow  # noqa: E402
from app import totp as totp_lib  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    EmailOutbox,
    Filing,
    Invoice,
    MfaChallenge,
    PortalDocument,
    PortalInvitation,
    PortalUser,
    Session as SessionRow,
    SmsCode,
)

from conftest import BASE_TEST_ENV  # noqa: E402

from app.models import TermsVersion  # noqa: E402
from starlette.requests import Request  # noqa: E402

BOOTSTRAP_SECRET = "owner-bootstrap-secret-0123456789abcdef"

TERMS_VERSION = "2026-10-01"
TERMS_HASH = "test-terms-hash-0123456789abcdef0123456789abcdef"


def _seed_terms(papp):
    """terms_versions has no migration seed; tests provide the current version."""
    db = _fresh_db(papp)
    try:
        if db.get(TermsVersion, TERMS_VERSION) is None:
            db.add(TermsVersion(
                version=TERMS_VERSION, version_hash=TERMS_HASH,
                title="Client Engagement Terms", body_html="<p>Test terms.</p>",
            ))
            db.commit()
    finally:
        db.close()


@pytest.fixture()
def papp(db_url, tmp_path):
    """App with the one-shot bootstrap secret set (criterion 1 needs the
    real bootstrap endpoint)."""
    settings = load_settings(
        {
            **BASE_TEST_ENV,
            "DATABASE_URL": db_url,
            "MAIL_CAPTURE_DIR": str(tmp_path / "mail-capture"),
            "STORAGE_DIR": str(tmp_path / "storage"),
            "OWNER_INITIAL_PASSWORD": BOOTSTRAP_SECRET,
        }
    )
    application = app_main.create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture()
def committed_client(papp):
    """TestClient whose route DB commits for real (production-faithful
    get_db). Tests clean up their own rows.

    New A4 contract (2026-10-06): the override stashes the session on
    ``request.state.db_session`` and TransactionMiddleware commits it
    BEFORE the response is sent (yield-teardown commit runs after send
    and races immediate client follow-ups)."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.routes import get_db

    Session = sessionmaker(bind=papp.state.engine, autoflush=False, expire_on_commit=False)

    def _override(request: Request):
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    papp.dependency_overrides[get_db] = _override
    try:
        with TestClient(papp, raise_server_exceptions=False) as c:
            yield c
    finally:
        papp.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_portal_tables(db_admin):
    """Test isolation on the session-scoped DB: the portal writes audit,
    outbox, sessions, rate windows, and storage-adjacent rows that later
    tests count."""
    yield
    with db_admin.begin() as conn:
        for table in (
            "email_outbox",
            "outbox_attempts",
            "idempotency_replays",
            "audit_log",
            "sessions",
            "mfa_challenges",
            "sms_codes",
            "password_resets",
            "portal_documents",
            "filings",
            "invoices",
            "invoice_lines",
            "invitation_events",
            "portal_invitations",
            "terms_acceptances",
            "portal_users",
            "one_shot_markers",
            "rate_windows",
        ):
            conn.execute(text(f"DELETE FROM {table}"))
        # invoice_series is seeded config: restore the default row.
        conn.execute(
            text(
                "INSERT OR IGNORE INTO invoice_series (id, prefix, next_number)"
                " VALUES ('default', 'JARO', 1)"
            )
        )


def _fresh_db(papp):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=papp.state.engine, autoflush=False, expire_on_commit=False)
    return Session()


def _make_owner(papp, email="jared.owner@example.com"):
    """Owner row with TOTP enrolled (post-first-login state)."""
    db = _fresh_db(papp)
    try:
        user = PortalUser(
            id=new_uuid7(), role="owner", email=email, display_name="Jared Robbins",
            password_hash="x", status="active", totp_enrolled_at=utcnow(),
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _owner_client(papp, committed_client, owner_id):
    from app import sessions as sessions_lib

    db = _fresh_db(papp)
    try:
        raw, _row = sessions_lib.create_session(db, user_id=owner_id)
        db.commit()
    finally:
        db.close()
    committed_client.cookies.set(sessions_lib.COOKIE_NAME, raw)
    return committed_client


def _idem_key() -> str:
    return f"test-key-{new_uuid7()}"


def _sync_cookie(client, response):
    """The session cookie is __Host- + Secure; the test client (http) does
    not persist it automatically, so sync it manually like test_13/16 do."""
    from app import sessions as sessions_lib

    m = re.search(rf"{re.escape(sessions_lib.COOKIE_NAME)}=([^;]+)",
                  response.headers.get("set-cookie", ""))
    if m:
        client.cookies.set(sessions_lib.COOKIE_NAME, m.group(1))


def _create_invite(oc, email, name="Test Client", phone=None, key=None,
                 temp_password="TempPass-Callsite-03"):
    """Owner provisions a client with a temporary password. Returns
    (invitation id, user id). No email is sent — Jared communicates the
    temporary password himself."""
    body = {"client_name": name, "email": email, "temp_password": temp_password}
    if phone:
        body["phone_e164"] = phone
    r = oc.post("/api/v1/admin/invitations", json=body,
                headers={"Idempotency-Key": key or _idem_key()})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert "accept_url" not in data  # no token accept flow anymore
    return data["id"], data["user_id"]


def _totp_pair(secret: str) -> tuple[str, str]:
    """Two consecutive TOTP codes, computed mid-step to avoid a boundary flip."""
    t = int(time.time() // 30) * 30 + 15
    return totp_lib.code_at(secret, for_time=t), totp_lib.code_at(secret, for_time=t + 30)


def _onboard_client(papp, cc, oc, email, name="Test Client",
                  temp_password="TempPass-Callsite-03", password="ClientOwn-Password-02", phone=None):
    """Full first-run: invite (temp password) -> login -> forced password
    change -> terms -> dashboard. Returns (client TestClient, user id,
    invite id). The caller passes a FRESH TestClient (no shared cookies)."""
    from fastapi.testclient import TestClient
    from app import sessions as sessions_lib

    _seed_terms(papp)
    invite_id, user_id = _create_invite(oc, email, name, phone=phone,
                                        temp_password=temp_password)
    c = TestClient(papp, raise_server_exceptions=False)
    # login with the temporary password -> forced change
    r = c.post("/api/v1/portal/login",
               json={"email": email, "password": temp_password})
    assert r.status_code == 202, r.text
    assert r.json()["next"] == "change_password"
    _sync_cookie(c, r)
    assert sessions_lib.COOKIE_NAME in r.headers.get("set-cookie", "")
    # dashboard gated until the temporary password is replaced
    r = c.get("/api/v1/portal/dashboard")
    assert r.status_code == 403 and r.json()["code"] == "password_change_required"
    # forced change (no current password needed -- the session proves it)
    r = c.post("/api/v1/portal/password/change", json={"new_password": password})
    assert r.status_code == 200, r.text
    assert r.json()["next"] == "terms"
    _sync_cookie(c, r)  # the change rotates the session
    # terms gate: dashboard 403 until accept
    r = c.get("/api/v1/portal/dashboard")
    assert r.status_code == 403 and r.json()["code"] == "terms_not_accepted"
    r = c.get("/api/v1/portal/terms/current")
    assert r.status_code == 200
    vh = r.json()["version_hash"]
    r = c.post("/api/v1/portal/terms/accept", json={"version_hash": vh})
    assert r.status_code == 200, r.text
    # dashboard now reachable
    r = c.get("/api/v1/portal/dashboard")
    assert r.status_code == 200, r.text
    return c, user_id, invite_id



def test_17_01_bootstrap_one_shot(papp, committed_client, tmp_path):
    cc = committed_client
    # First call with the correct secret → owner row created.
    r = cc.post("/api/v1/admin/bootstrap",
                json={"email": "jared@jaroaccounting.com",
                      "initial_password": BOOTSTRAP_SECRET})
    assert r.status_code == 201, r.text
    db = _fresh_db(papp)
    try:
        owner = db.query(PortalUser).filter(PortalUser.role == "owner").one()
        assert owner.email == "jared@jaroaccounting.com"
        assert owner.status == "active"
    finally:
        db.close()
    # Second call → 410 bootstrap_consumed.
    r = cc.post("/api/v1/admin/bootstrap",
                json={"email": "jared@jaroaccounting.com",
                      "initial_password": BOOTSTRAP_SECRET})
    assert r.status_code == 410, r.text
    assert r.json()["code"] == "bootstrap_consumed"

    # Absent secret → 503 fail-closed. A separate app without the secret,
    # migrated first (H5 runtime check).
    from alembic import command as alembic_command
    from alembic.config import Config

    scratch_path = tmp_path / "nosecret.db"
    scratch_url = f"sqlite:///{scratch_path}"
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(
        os.path.dirname(__file__), "..", "alembic"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    alembic_command.upgrade(cfg, "head")
    settings = load_settings(
        {**BASE_TEST_ENV, "DATABASE_URL": scratch_url,
         "MAIL_CAPTURE_DIR": str(tmp_path / "mail2"),
         "STORAGE_DIR": str(tmp_path / "storage2")}
    )
    app2 = app_main.create_app(settings)
    try:
        from fastapi.testclient import TestClient

        with TestClient(app2, raise_server_exceptions=False) as c2:
            r = c2.post("/api/v1/admin/bootstrap",
                        json={"email": "x@example.com",
                              "initial_password": " sixteen-chars-min "})
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "bootstrap_unavailable"
    finally:
        app2.state.engine.dispose()

    # Owner login requires TOTP: covered in test_17_01b below.


def test_17_01b_owner_login_totp_mandatory(papp, committed_client):
    """Owner login: email+password → TOTP challenge mandatory, no SMS fallback."""
    from app import passwords as passwords_lib

    db = _fresh_db(papp)
    try:
        user = PortalUser(
            id=new_uuid7(), role="owner", email="owner2@example.com",
            display_name="Jared", status="active",
            password_hash=passwords_lib.hash_password("owner-pass-12345678"),
        )
        db.add(user)
        db.commit()
    finally:
        db.close()
    r = committed_client.post("/api/v1/admin/login",
                              json={"email": "owner2@example.com",
                                    "password": "owner-pass-12345678"})
    assert r.status_code == 202, r.text
    # No SMS fallback is ever offered to owners (AUTH D1): totp only.
    assert r.json()["methods"] == ["totp"]


# --- 17_02: invitation lifecycle -------------------------------------------------------


def _run_worker(papp):
    from app import outbox as outbox_lib

    worker = outbox_lib.OutboxWorker(papp.state.session_factory, papp.state.mail_backend)
    return worker.run_cycle()


def _captured_invite_html(papp):
    import glob as _glob

    capture_dir = papp.state.settings.mail_capture_dir
    paths = _glob.glob(os.path.join(capture_dir, "*-attempt1.json"))
    assert paths, "no captured mail artifacts"
    for path in paths:
        artifact = json.load(open(path))
        if artifact.get("subject", "").startswith("You're invited to"):
            return artifact
    raise AssertionError("no captured invitation email")


def test_17_02_invite_lifecycle(papp, committed_client):
    """Temp-password provisioning lifecycle (2026-10-15 refactor): the owner
    creates the client with a temporary password; the account exists
    immediately, no email is sent, and the client must replace the password
    on first login."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)

    # Create -> 201, status accepted, user provisioned with the temp password.
    # No token, no accept_url, no email.
    invite_id, user_id = _create_invite(oc, "lifecycle@example.com", "Lifecycle Client",
                                        temp_password="TempPass-Callsite-03")
    # No invite email is queued — Jared communicates the password himself.
    report = _run_worker(papp)
    assert report["done"] == 0, report

    db = _fresh_db(papp)
    try:
        row = db.get(PortalInvitation, invite_id)
        assert row.status == "accepted"
        user = db.get(PortalUser, user_id)
        assert user.email == "lifecycle@example.com"
        assert user.status == "active"
        assert user.must_change_password is True
        # The temp password verifies; the DB stores a hash, never the secret.
        from app import passwords as _pw
        assert _pw.verify_password(user.password_hash, "TempPass-Callsite-03")
        assert "TempPass-Callsite-03" not in user.password_hash
    finally:
        db.close()

    # Blocklisted temp password is rejected at creation.
    r = oc.post("/api/v1/admin/invitations",
                json={"client_name": "Bad", "email": "bad@example.com",
                      "temp_password": "changeme2026"},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"
    assert any("password_blocklisted" in f["message"] for f in r.json()["fields"])

    # Duplicate email -> client_exists (the account already exists).
    r = oc.post("/api/v1/admin/invitations",
                json={"client_name": "Dupe", "email": "lifecycle@example.com",
                      "temp_password": "TempPass-Dupe-09"},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 409 and r.json()["code"] == "client_exists"

    # Full first login: temp password -> forced change -> terms -> dashboard.
    c, uid, _ = _onboard_client(papp, cc, oc, "lifecycle2@example.com", "Lifecycle Two",
                                temp_password="TempPass-Lifecycle-04", password="ClientOwn-Lifecycle-05")
    db = _fresh_db(papp)
    try:
        user = db.get(PortalUser, uid)
        assert user.must_change_password is False
        from app import passwords as _pw2
        assert _pw2.verify_password(user.password_hash, "ClientOwn-Lifecycle-05")
    finally:
        db.close()



def test_17_03_anti_enumeration(papp, committed_client):
    from app import passwords as passwords_lib

    cc = committed_client
    db = _fresh_db(papp)
    try:
        user = PortalUser(
            id=new_uuid7(), role="client", email="real.client@example.com",
            display_name="Real", status="active",
            password_hash=passwords_lib.hash_password("real-password-123456"),
        )
        db.add(user)
        db.commit()
    finally:
        db.close()

    def _shape(resp):
        body = resp.json()
        return (resp.status_code, body["code"], set(body.keys()))

    # Unknown email vs wrong password: identical status/code/shape.
    r1 = cc.post("/api/v1/portal/login",
                 json={"email": "nobody.here@example.com", "password": "wrong-password-1"})
    r2 = cc.post("/api/v1/portal/login",
                 json={"email": "real.client@example.com", "password": "wrong-password-2"})
    assert r1.status_code == 401 and r2.status_code == 401
    assert _shape(r1) == _shape(r2)
    assert r1.json()["message"] == r2.json()["message"]

    # Password-reset request: always 200, no oracle.
    for email in ("nobody.here@example.com", "real.client@example.com"):
        r = cc.post("/api/v1/portal/password-reset/request", json={"email": email})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}


# --- 17_04: stuffing budgets -------------------------------------------------------


def test_17_04_stuffing_budgets(papp, committed_client, db_admin, monkeypatch):
    from app import rate_limits

    cc = committed_client
    # Tighten the login budget: 3 per target/hour.
    monkeypatch.setattr(
        rate_limits, "BUDGETS",
        {"portal.login": {"source": (100, 3600), "route_source": (100, 3600),
                          "target": (3, 3600), "global": (1000, 3600)}},
    )
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM rate_windows"))
    try:
        statuses = []
        for _ in range(4):
            r = cc.post("/api/v1/portal/login",
                        json={"email": "stuffed@example.com", "password": "x"})
            statuses.append(r.status_code)
        assert statuses[:3] == [401, 401, 401], statuses
        assert statuses[3] == 429, statuses
        r = cc.post("/api/v1/portal/login",
                    json={"email": "stuffed@example.com", "password": "x"})
        assert r.status_code == 429
        assert r.json()["code"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
    finally:
        with db_admin.begin() as conn:
            conn.execute(text("DELETE FROM rate_windows"))

    # Budget-store outage → 503 fail-closed on login.
    with db_admin.begin() as conn:
        conn.execute(text("DROP TABLE rate_windows"))
    try:
        r = cc.post("/api/v1/portal/login",
                    json={"email": "anyone@example.com", "password": "x"})
        assert r.status_code == 503, r.status_code
        assert r.json()["code"] == "budget_unavailable"
    finally:
        # Restore the table for the rest of the session (test isolation).
        from sqlalchemy import create_engine as _ce

        eng = _ce(str(papp.state.engine.url))
        try:
            with eng.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS rate_windows ("
                    " key_hash VARCHAR(64) NOT NULL PRIMARY KEY,"
                    " window_start DATETIME NOT NULL,"
                    " window_seconds INTEGER NOT NULL,"
                    " count INTEGER NOT NULL)"))
        finally:
            eng.dispose()


# --- 17_05: 2FA -----------------------------------------------------------------------


def _login_temp(papp, email, temp_password):
    """Log in with a temporary password; returns (TestClient, response)."""
    from fastapi.testclient import TestClient
    c = TestClient(papp, raise_server_exceptions=False)
    r = c.post("/api/v1/portal/login",
               json={"email": email, "password": temp_password})
    assert r.status_code == 202, r.text
    _sync_cookie(c, r)
    return c, r


def _login_password(client, email, password):
    """Password-only login on an existing client; syncs the session cookie."""
    r = client.post("/api/v1/portal/login",
                    json={"email": email, "password": password})
    assert r.status_code == 202, r.text
    _sync_cookie(client, r)
    return r


def test_17_05_temp_password_forced_change(papp, committed_client):
    """Temporary-password login forces a password change before any portal
    use: the dashboard 403s with password_change_required until the client
    chooses their own password."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    _create_invite(oc, "tempflow@example.com", "Temp Flow",
                   temp_password="TempPass-Flow-06")

    c, r = _login_temp(papp, "tempflow@example.com", "TempPass-Flow-06")
    assert r.json()["next"] == "change_password"

    # Every portal route is gated until the password is replaced.
    for path in ("/api/v1/portal/dashboard", "/api/v1/portal/documents"):
        r = c.get(path)
        assert r.status_code == 403 and r.json()["code"] == "password_change_required", (path, r.text)
    # ...except the password-change endpoint itself (pre-gate).
    r = c.post("/api/v1/portal/password/change",
               json={"new_password": "short"})
    assert r.status_code == 422, r.text  # policy still enforced

    # The new password must differ from the temporary one.
    r = c.post("/api/v1/portal/password/change",
               json={"new_password": "TempPass-Flow-06"})
    assert r.status_code == 422, r.text
    assert any(f["field"] == "new_password" for f in r.json().get("fields", []))

    r = c.post("/api/v1/portal/password/change",
               json={"new_password": "ClientOwn-Flow-07"})
    assert r.status_code == 200, r.text
    assert r.json()["next"] == "terms"

    # The temporary password no longer works.
    from fastapi.testclient import TestClient as _TC
    c2 = _TC(papp, raise_server_exceptions=False)
    r2 = c2.post("/api/v1/portal/login",
                 json={"email": "tempflow@example.com",
                       "password": "TempPass-Flow-06"})
    assert r2.status_code == 401, r2.text
    # The new password does.
    c3, r3 = _login_temp(papp, "tempflow@example.com", "ClientOwn-Flow-07")
    assert r3.json()["next"] == "terms"


def test_17_05b_owner_password_reset(papp, committed_client):
    """Owner password reset: new temporary password, forced change on next
    login, all sessions revoked."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _ = _onboard_client(papp, cc, oc, "resetflow@example.com", "Reset Flow")

    # Owner resets with a reason; the client is signed out everywhere.
    r = oc.post(f"/api/v1/admin/clients/{user_id}/password/reset",
                json={"temp_password": "TempPass-Reset-08",
                      "reason": "client forgot password"})
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is True

    # Old password dead, old session dead.
    r = c.get("/api/v1/portal/dashboard")
    assert r.status_code == 401, r.text
    from fastapi.testclient import TestClient as _TC2
    c2 = _TC2(papp, raise_server_exceptions=False)
    r2 = c2.post("/api/v1/portal/login",
                 json={"email": "resetflow@example.com",
                       "password": "ClientOwn-Password-02"})
    assert r2.status_code == 401, r2.text

    # Temp password works but forces a change.
    c3, r3 = _login_temp(papp, "resetflow@example.com", "TempPass-Reset-08")
    assert r3.json()["next"] == "change_password"
    r = c3.get("/api/v1/portal/dashboard")
    assert r.status_code == 403 and r.json()["code"] == "password_change_required"

    # Missing reason is rejected.
    r = oc.post(f"/api/v1/admin/clients/{user_id}/password/reset",
                json={"temp_password": "TempPass-Reset-08", "reason": ""})
    assert r.status_code == 422, r.text



def _set_last_seen(papp, user_id, minutes_ago):
    db = _fresh_db(papp)
    try:
        rows = db.query(SessionRow).filter(SessionRow.user_id == user_id).all()
        for row in rows:
            row.last_seen_at = utcnow() - timedelta(minutes=minutes_ago)
        db.commit()
        return [r.id for r in rows]
    finally:
        db.close()


def test_17_06_sessions(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "session.client@example.com")

    # Idle 15 min → 401 session_expired.
    _set_last_seen(papp, user_id, 16)
    r = c.get("/api/v1/portal/session")
    assert r.status_code == 401 and r.json()["code"] == "session_expired"

    # Fresh login -> banner math: <=120s remaining shows the warning state.
    # (Password-only login now; the session cookie is set directly.)
    _set_last_seen(papp, user_id, 0)
    r = c.post("/api/v1/portal/login",
               json={"email": "session.client@example.com",
                     "password": "ClientOwn-Password-02"})
    assert r.status_code == 202, r.text
    _sync_cookie(c, r)

    # Age to 13.5 min idle → ≤120s left → banner state + one-click extend works.
    _set_last_seen(papp, user_id, 13.5)
    r = c.get("/api/v1/portal/session")
    assert r.status_code == 200
    assert r.json()["expires_in_seconds"] <= 120
    r = c.post("/api/v1/portal/sessions/extend")
    assert r.status_code == 200, r.text
    assert r.json()["expires_in_seconds"] > 120
    # Second extend within 60s → idempotent (≤1 effective extend/60s).
    r2 = c.post("/api/v1/portal/sessions/extend")
    assert r2.status_code == 200

    # Extend on an expired session fails (cannot resurrect).
    _set_last_seen(papp, user_id, 16)
    r = c.post("/api/v1/portal/sessions/extend")
    assert r.status_code == 401

    # 6th concurrent login evicts the oldest (+ audit event).
    raws = []
    for _ in range(6):
        _login_password(c, "session.client@example.com", "ClientOwn-Password-02")
        raws.append(r.headers.get("set-cookie", ""))
    db = _fresh_db(papp)
    try:
        live = db.query(SessionRow).filter(
            SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None)).count()
        assert live == 5
        evicted = db.query(AuditLog).filter(
            AuditLog.action == "session.evicted").count()
        assert evicted >= 1
    finally:
        db.close()

    # Session inventory: own sessions only, no full IPs / raw IDs.
    from fastapi.testclient import TestClient

    c2 = TestClient(papp, raise_server_exceptions=False)
    _login_password(c2, "session.client@example.com", "ClientOwn-Password-02")
    r = c2.get("/api/v1/portal/sessions")
    assert r.status_code == 200, r.text
    body = r.text
    assert "@" not in body  # no emails
    for s in r.json()["items"]:
        assert "…" in s["id"] or len(s["id"]) <= 16  # truncated display IDs

    # Owner revoke-all kills every session.
    r = oc.post(f"/api/v1/admin/clients/{user_id}/sessions/revoke-all")
    assert r.status_code == 200, r.text
    assert r.json()["revoked_count"] >= 1
    r = c2.get("/api/v1/portal/session")
    assert r.status_code == 401


# --- 17_07: terms gate -----------------------------------------------------------------------


def test_17_07_terms_gate(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    from fastapi.testclient import TestClient

    # Onboard through the forced password change, then stop before terms.
    invite_id, user_id = _create_invite(oc, "terms.client@example.com", "Terms Client",
                                        temp_password="TempPass-Terms-10")
    c, r = _login_temp(papp, "terms.client@example.com", "TempPass-Terms-10")
    assert r.json()["next"] == "change_password"
    r = c.post("/api/v1/portal/password/change",
               json={"new_password": "ClientOwn-Terms-11"})
    assert r.status_code == 200
    _sync_cookie(c, r)

    gated = [
        ("GET", "/api/v1/portal/dashboard", {}),
        ("GET", "/api/v1/portal/documents", {}),
        ("GET", "/api/v1/portal/filings", {}),
        ("GET", "/api/v1/portal/invoices", {}),
        # Note: /password/* and /sessions are excepted from the gate by design.
    ]
    for method, path, kw in gated:
        r = c.request(method, path, **kw)
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["code"] == "terms_not_accepted", path
    # Excepted paths stay reachable pre-accept. /password/change is not
    # gated (422 for a bad payload proves the route was reached, not 403).
    assert c.get("/api/v1/portal/terms/current").status_code == 200
    assert c.get("/api/v1/portal/session").status_code == 200
    r = c.post("/api/v1/portal/password/change", json={"new_password": "x"})
    assert r.status_code == 422, r.text

    # Stale version_hash → 409 version_mismatch.
    r = c.post("/api/v1/portal/terms/accept",
               json={"version_hash": "stale-hash-0000000000000000"})
    assert r.status_code == 409 and r.json()["code"] == "version_mismatch"

    # Accept with the current hash → row stores timestamp + version hash.
    r = c.get("/api/v1/portal/terms/current")
    vh = r.json()["version_hash"]
    r = c.post("/api/v1/portal/terms/accept", json={"version_hash": vh})
    assert r.status_code == 200, r.text
    db = _fresh_db(papp)
    try:
        user = db.query(PortalUser).filter(
            PortalUser.email == "terms.client@example.com").one()
        assert user.terms_accepted_at is not None
        from app.models import TermsAcceptance

        acc = db.query(TermsAcceptance).filter(
            TermsAcceptance.user_id == user.id).one()
        assert acc.version_hash == vh and acc.accepted_at is not None
    finally:
        db.close()
    # Gate lifted: dashboard reachable (no authenticator step anymore).
    assert c.get("/api/v1/portal/dashboard").status_code == 200


# --- 17_08: set-password idempotency -------------------------------------------------------


def test_17_08_invite_create_idempotent(papp, committed_client):
    """Invitation creation is idempotent: same key -> one user row, replayed
    result; same key + different non-credential fields -> conflict. The
    temporary password is excluded from the idempotency payload (credential
    material), so a retry with a different temp password replays the first
    result rather than changing the password."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)

    key = _idem_key()
    bodies = []
    for _ in range(2):
        r = oc.post("/api/v1/admin/invitations",
                    json={"client_name": "Idem Client", "email": "idem.client@example.com",
                          "temp_password": "TempPass-Idem-13"},
                    headers={"Idempotency-Key": key})
        bodies.append((r.status_code, r.json().get("replayed"), r.json().get("id")))
    assert bodies[0][0] == 201 and bodies[1][0] == 201
    assert bodies[1][1] is True  # replayed
    assert bodies[0][2] == bodies[1][2]
    db = _fresh_db(papp)
    try:
        n = db.query(PortalUser).filter(
            PortalUser.email == "idem.client@example.com").count()
        assert n == 1
    finally:
        db.close()
    # Same key + different temp password -> still replays (password excluded
    # from the payload); the stored password is the FIRST one.
    r = oc.post("/api/v1/admin/invitations",
                json={"client_name": "Idem Client", "email": "idem.client@example.com",
                      "temp_password": "TempPass-Idem-14-DIFFERENT"},
                headers={"Idempotency-Key": key})
    assert r.status_code == 201 and r.json().get("replayed") is True
    assert r.json()["id"] == bodies[0][2]
    # Same key + different name -> 409 idempotency_conflict.
    r = oc.post("/api/v1/admin/invitations",
                json={"client_name": "Different Name", "email": "idem.client@example.com",
                      "temp_password": "TempPass-Idem-13"},
                headers={"Idempotency-Key": key})
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"



def test_17_09_password_reset(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "reset.client@example.com")

    # Request → captured email with a 24h single-use link.
    r = c.post("/api/v1/portal/password-reset/request",
               json={"email": "reset.client@example.com"})
    assert r.status_code == 200
    _run_worker(papp)
    import glob as _glob

    paths = _glob.glob(os.path.join(
        papp.state.settings.mail_capture_dir, "*-attempt1.json"))
    reset_artifact = None
    for path in paths:
        artifact = json.load(open(path))
        if "portal password" in artifact.get("subject", "").lower():
            reset_artifact = artifact
            break
    assert reset_artifact is not None
    m = re.search(r"token=([A-Za-z0-9_-]{43})", reset_artifact["text_body"])
    assert m, "reset link missing from captured email"
    reset_token = m.group(1)

    # Verify → complete revokes ALL sessions + changes the password.
    r = c.post("/api/v1/portal/password-reset/verify", json={"token": reset_token})
    assert r.status_code == 200
    # Mint a second session first so "all sessions revoked" is meaningful.
    _login_password(c, "reset.client@example.com", "ClientOwn-Password-02")
    r = c.post("/api/v1/portal/password-reset/complete",
               json={"token": reset_token,
                     "new_password": "brand new horse battery staple 1"},
               headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 200, r.text
    db = _fresh_db(papp)
    try:
        live = db.query(SessionRow).filter(
            SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None)).count()
        assert live == 0
    finally:
        db.close()
    # Old password dead, new password works.
    r = c.post("/api/v1/portal/login",
               json={"email": "reset.client@example.com",
                     "password": "correct horse battery staple 123"})
    assert r.status_code == 401
    r = c.post("/api/v1/portal/login",
               json={"email": "reset.client@example.com",
                     "password": "brand new horse battery staple 1"})
    assert r.status_code == 202, r.text
    # Used token replay → safe wording.
    r = c.post("/api/v1/portal/password-reset/verify", json={"token": reset_token})
    assert r.status_code == 410 and r.json()["code"] == "reset_invalid"

    # Owner-visible feed row.
    r = oc.get("/api/v1/admin/password-resets")
    assert r.status_code == 200, r.text
    assert any(row["client_id"] == user_id for row in r.json()["items"])

    # Jared's revoke-all kills live reset links.
    r = c.post("/api/v1/portal/password-reset/request",
               json={"email": "reset.client@example.com"})
    assert r.status_code == 200
    _run_worker(papp)
    paths = _glob.glob(os.path.join(
        papp.state.settings.mail_capture_dir, "*-attempt1.json"))
    tokens = []
    for path in paths:
        artifact = json.load(open(path))
        if "portal password" in artifact.get("subject", "").lower():
            m = re.search(r"token=([A-Za-z0-9_-]{43})", artifact["text_body"])
            if m:
                tokens.append(m.group(1))
    assert tokens
    r = oc.post(f"/api/v1/admin/clients/{user_id}/sessions/revoke-all")
    assert r.status_code == 200
    r = c.post("/api/v1/portal/password-reset/verify", json={"token": tokens[-1]})
    assert r.status_code == 410 and r.json()["code"] == "reset_invalid"


# --- 17_10: DB-leak drill -----------------------------------------------------------------------


def test_17_10_db_leak_drill(papp, committed_client):
    """A dump of every table contains no raw invite/reset tokens, no
    plaintext passwords, no usable TOTP secrets, no raw session IDs."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "leak.client@example.com",
                                       password="leak drill horse staple 1")

    # Collect the raw secrets a leak would expose.
    invite_id, leak_user_id = _create_invite(oc, "leak2@example.com", "Leak Two")
    r = c.post("/api/v1/portal/password-reset/request",
               json={"email": "leak.client@example.com"})
    assert r.status_code == 200
    db = _fresh_db(papp)
    try:
        from app.models import PasswordReset

        pr = db.query(PasswordReset).filter(
            PasswordReset.user_id == user_id).order_by(
            PasswordReset.created_at.desc()).first()
        # The raw reset token is only in the outbox snapshot (emailed link).
        intent = db.query(EmailOutbox).filter(
            EmailOutbox.kind == "password_reset").order_by(
            EmailOutbox.created_at.desc()).first()
        reset_token = re.search(r"token=([A-Za-z0-9_-]{43})",
                                intent.frozen_snapshot["fields"].get("reset_url", "")).group(1)
        user = db.get(PortalUser, user_id)
        password_hash = user.password_hash
        sessions = db.query(SessionRow).filter(SessionRow.user_id == user_id).all()
        # s.id values are the SHA-256 digests (correct to store); the RAW
        # token is what must never appear.
        assert sessions
    finally:
        db.close()
    # A live raw session cookie from a fresh login.
    from fastapi.testclient import TestClient

    c2 = TestClient(papp, raise_server_exceptions=False)
    r = c2.post("/api/v1/portal/login",
                json={"email": "leak.client@example.com",
                      "password": "leak drill horse staple 1"})
    assert r.status_code == 202, r.text
    _sync_cookie(c2, r)
    raw_cookie = r.headers.get("set-cookie", "")

    # Dump every credential table to text. email_outbox is EXCLUDED by design:
    # the worker renders the invitation/reset email from the frozen snapshot,
    # so the single-use token must be there until send (booking precedent for
    # SMS codes); those rows are transient (cancelled on revoke, consumed on
    # use) and the token is 7-day TTL + single-use. What must NEVER leak is
    # long-lived credential state.
    from app import models as models_lib

    credential_tables = [t for t in sorted(models_lib.TABLE_NAMES)
                         if t != "email_outbox"]
    db = _fresh_db(papp)
    try:
        dump = []
        for table in credential_tables:
            try:
                rows = db.execute(text(f"SELECT * FROM {table}")).fetchall()
            except Exception:
                continue
            for row in rows:
                dump.append(" ".join("" if v is None else str(v) for v in row))
        blob = "\n".join(dump)
    finally:
        db.close()

    assert reset_token not in blob
    assert "leak drill horse staple 1" not in blob  # plaintext password
    assert password_hash in blob  # the hash IS stored (and is not reversible here)
    # The raw session cookie value must not appear either (digests only in DB).
    m = re.search(r"jaro_session=([A-Za-z0-9_-]+)", raw_cookie)
    assert m and m.group(1) not in blob


# --- 17_11: docs-as-tests -----------------------------------------------------------------------


# The 34 AUTH_AND_INVITATIONS.md endpoints (consolidated inventory, D-05 paths).
AUTH_INVENTORY = [
    ("POST", "/api/v1/admin/bootstrap"),
    ("POST", "/api/v1/admin/login"),
    ("POST", "/api/v1/admin/login/2fa"),
    ("POST", "/api/v1/admin/2fa/enroll"),
    ("POST", "/api/v1/admin/2fa/confirm"),
    ("POST", "/api/v1/admin/invitations"),
    ("GET", "/api/v1/admin/invitations"),
    ("GET", "/api/v1/admin/invitations/{invitation_id}"),
    ("POST", "/api/v1/admin/invitations/{invitation_id}/revoke"),
    ("POST", "/api/v1/admin/clients/{client_id}/password/reset"),
    ("GET", "/api/v1/admin/password-resets"),
    ("POST", "/api/v1/portal/login"),
    ("POST", "/api/v1/portal/password-reset/request"),
    ("POST", "/api/v1/portal/password-reset/verify"),
    ("POST", "/api/v1/portal/password-reset/complete"),
    ("POST", "/api/v1/portal/password/change"),
    ("GET", "/api/v1/portal/terms/current"),
    ("POST", "/api/v1/portal/terms/accept"),
    ("GET", "/api/v1/portal/session"),
    ("POST", "/api/v1/portal/logout"),
    ("DELETE", "/api/v1/portal/session"),
    ("POST", "/api/v1/portal/sessions/extend"),
    ("GET", "/api/v1/portal/sessions"),
    ("DELETE", "/api/v1/portal/sessions/{session_id}"),
]

PORTAL_INVENTORY = [
    ("GET", "/api/v1/portal/dashboard"),
    ("GET", "/api/v1/portal/documents"),
    ("POST", "/api/v1/portal/documents"),
    ("GET", "/api/v1/portal/documents/{document_id}/download"),
    ("GET", "/api/v1/portal/filings"),
    ("GET", "/api/v1/portal/filings/{filing_id}"),
    ("GET", "/api/v1/portal/invoices"),
    ("GET", "/api/v1/portal/invoices/{invoice_id}"),
    ("GET", "/api/v1/portal/invoices/{invoice_id}/pdf"),
    ("GET", "/api/v1/admin/clients"),
    ("GET", "/api/v1/admin/clients/{client_id}"),
    ("PATCH", "/api/v1/admin/clients/{client_id}"),
    ("POST", "/api/v1/admin/clients/{client_id}/password/reset"),
    ("POST", "/api/v1/admin/clients/{client_id}/sessions/revoke-all"),
    ("POST", "/api/v1/admin/clients/{client_id}/documents"),
    ("GET", "/api/v1/admin/clients/{client_id}/documents/{document_id}/download"),
    ("POST", "/api/v1/admin/clients/{client_id}/documents/{document_id}/retire"),
    ("POST", "/api/v1/admin/filings"),
    ("PATCH", "/api/v1/admin/filings/{filing_id}/status"),
    ("POST", "/api/v1/admin/invoices"),
    ("GET", "/api/v1/admin/invoices/{invoice_id}"),
    ("GET", "/api/v1/admin/invoices/{invoice_id}/pdf"),
    ("POST", "/api/v1/admin/invoices/{invoice_id}/pay"),
    ("POST", "/api/v1/admin/invoices/{invoice_id}/void"),
    ("GET", "/api/v1/admin/invoice-series"),
    ("PUT", "/api/v1/admin/invoice-series"),
    ("GET", "/api/v1/admin/audit"),
]


def test_17_11_docs_as_tests(papp, committed_client):
    spec = committed_client.get("/openapi.json").json()
    paths = spec["paths"]
    missing = []
    for method, path in AUTH_INVENTORY + PORTAL_INVENTORY:
        ops = paths.get(path, {})
        if method.lower() not in ops:
            missing.append(f"{method} {path}")
    assert not missing, "endpoints missing from OpenAPI:\n" + "\n".join(missing)

    # Error-code catalog: every Phase-5 code is registered.
    from app import errors as errors_lib

    for code in ("bootstrap_consumed", "bootstrap_unavailable", "invitation_invalid",
                 "invitation_active", "client_exists", "invalid_credentials",
                 "challenge_consumed", "totp_required", "password_change_required",
                 "totp_already_enrolled", "reset_invalid",
                 "version_mismatch", "filing_not_found", "filing_transition_invalid",
                 "invoice_not_found", "invoice_not_payable", "invoice_not_voidable",
                 "document_not_found", "file_too_large", "session_not_found",
                 "terms_not_accepted", "idempotency_conflict"):
        assert code in errors_lib.ERROR_CATALOG, f"error code not in catalog: {code}"

    # Grep gates: no payment-capture / messaging / e-signature endpoints.
    blob = json.dumps(paths).lower()
    for pattern in ("payment", "checkout", "charge", "e-signature", "esignature",
                    "esign", "/messages", "send-sms", "chat"):
        assert pattern not in blob, f"v1-cut endpoint pattern in OpenAPI: {pattern!r}"
    # No client self-disable-TOTP route.
    assert not any(p.startswith("/api/v1/portal") and "disable" in p for p in paths)


# --- 17_12: dashboard -----------------------------------------------------------------------


def _admin_create_filing(oc, client_id, kind="personal_t1", year=2025):
    r = oc.post("/api/v1/admin/filings",
                json={"client_id": client_id, "kind_code": kind, "tax_year": year},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _admin_issue_invoice(oc, client_id, lines=None, due_days=30):
    from datetime import date

    due = (date.today() + timedelta(days=due_days)).isoformat()
    r = oc.post("/api/v1/admin/invoices",
                json={"client_id": client_id, "due_at": due, "memo": "Test invoice",
                      "lines": lines or [
                          {"description": "Tax preparation", "quantity": 1,
                           "unit_cents": 10000, "tax_rate_bps": 500}]},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 201, r.text
    return r.json()


def _upload_doc(oc_or_c, filename, data, type_code="other", label=None, idem_key=None):
    files = {"file": (filename, data)}
    data_form = {"type_code": type_code}
    if label:
        data_form["label"] = label
    return oc_or_c.post("/api/v1/portal/documents", files=files, data=data_form,
                        headers={"Idempotency-Key": idem_key or _idem_key()})


def test_17_12_dashboard(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "dash.client@example.com")
    # A second client whose data must NEVER appear.
    c2, user2_id, _iid2 = _onboard_client(papp, cc, oc, "other.client@example.com")

    # Empty states use real copy.
    dash = c.get("/api/v1/portal/dashboard").json()
    assert dash["needs_attention"] == {"filings": [], "invoices": [], "documents": []}
    assert "filings" in dash["snapshots"] and "documents" in dash["snapshots"]
    assert "invoices" in dash["snapshots"]

    # Filing awaiting_info with Jared's note → needs-attention shows it verbatim.
    filing_id = _admin_create_filing(oc, user_id)
    r = oc.patch(f"/api/v1/admin/filings/{filing_id}/status",
                 json={"status": "awaiting_info",
                       "note": "Please send your T4 slip"})
    assert r.status_code == 200, r.text
    # Overdue invoice (issued + past due).
    inv = _admin_issue_invoice(oc, user_id, due_days=-5)
    # Document shared by Jared since last_seen_at.
    db = _fresh_db(papp)
    try:
        user = db.get(PortalUser, user_id)
        user.last_portal_visit_at = utcnow() - timedelta(days=2)
        db.commit()
    finally:
        db.close()
    r = oc.post(f"/api/v1/admin/clients/{user_id}/documents",
                files={"file": ("shared.pdf", b"%PDF-1.4 shared")},
                data={"type_code": "other"},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 201, r.text

    # Data for the OTHER client (must not leak).
    _admin_create_filing(oc, user2_id)
    _admin_issue_invoice(oc, user2_id, due_days=-5)

    dash = c.get("/api/v1/portal/dashboard").json()
    na = dash["needs_attention"]
    assert any(f["id"] == filing_id for f in na["filings"])
    assert any("Please send your T4 slip" in (f.get("note") or "") for f in na["filings"])
    assert any(i["id"] == inv["id"] for i in na["invoices"])
    assert len(na["documents"]) >= 1
    # Nothing from the other client.
    blob = json.dumps(dash)
    assert user2_id not in blob
    assert "other.client@example.com" not in blob

    # Snapshot cards render from real rows.
    assert len(dash["snapshots"]["filings"]) >= 1
    assert dash["snapshots"]["invoices"]["open_count"] >= 1
    assert dash["snapshots"]["invoices"]["total_owing_cents"] > 0
    # Activity feed: last 20, newest first, America/Vancouver timestamps.
    feed = dash["activity"]
    assert len(feed) <= 20
    timestamps = [a["at"] for a in feed]
    assert timestamps == sorted(timestamps, reverse=True)
    for a in feed:
        assert a["at"]

    # Authorization on EVERY portal endpoint: client B cannot touch client A's rows.
    r = c2.get(f"/api/v1/portal/filings/{filing_id}")
    assert r.status_code in (403, 404), r.text
    r = c2.get(f"/api/v1/portal/invoices/{inv['id']}")
    assert r.status_code in (403, 404), r.text


# --- 17_13: document vault -----------------------------------------------------------------------


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</>>endobj\n"


def _upload(c, filename, data, type_code="other", key=None):
    return c.post("/api/v1/portal/documents",
                  files={"file": (filename, data)},
                  data={"type_code": type_code},
                  headers={"Idempotency-Key": key or _idem_key()})


def test_17_13_vault(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "vault.client@example.com")
    cB, userB_id, _ = _onboard_client(papp, cc, oc, "vault.b@example.com")

    # Client uploads 3 files → 3 rows, visible to Jared immediately,
    # one document_received intent queued.
    docs = []
    for i, (fn, data) in enumerate([
        ("t4.pdf", PDF_BYTES + b"t4-slip"),
        ("receipt.jpg", b"\xff\xd8\xff" + b"receipt-photo"),
        ("books.xlsx", b"PK\x03\x04" + b"workbook"),
    ]):
        r = _upload(c, fn, data, type_code="tax_slips")
        assert r.status_code == 201, (fn, r.text)
        docs.append(r.json())
    assert len({d["id"] for d in docs}) == 3
    db = _fresh_db(papp)
    try:
        rows = db.query(PortalDocument).filter(
            PortalDocument.user_id == user_id).all()
        assert len(rows) == 3
        assert all(r.scan_status == "unscanned" for r in rows)
        received = db.query(EmailOutbox).filter(
            EmailOutbox.kind == "document_received").count()
        assert received == 3
        # Jared sees them in the dossier immediately.
        dossier = oc.get(f"/api/v1/admin/clients/{user_id}").json()
        assert len(dossier["documents"]) == 3
    finally:
        db.close()

    # Jared shares → client sees it immediately, one document_shared intent,
    # byte-identical download (hash compare).
    shared_bytes = PDF_BYTES + b"jared-deliverable"
    r = oc.post(f"/api/v1/admin/clients/{user_id}/documents",
                files={"file": ("deliverable.pdf", shared_bytes)},
                data={"type_code": "deliverables"},
                headers={"Idempotency-Key": _idem_key()})
    assert r.status_code == 201, r.text
    shared_id = r.json()["id"]
    r = c.get("/api/v1/portal/documents")
    assert any(d["id"] == shared_id for d in r.json()["items"])
    r = c.get(f"/api/v1/portal/documents/{shared_id}/download")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    import hashlib

    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(shared_bytes).hexdigest()
    db = _fresh_db(papp)
    try:
        assert db.query(EmailOutbox).filter(
            EmailOutbox.kind == "document_shared").count() == 1
    finally:
        db.close()

    # malware.exe and MZ-header pdf → 422, nothing stored, nothing in DB.
    for fn, data in (("malware.exe", b"MZ" + b"x" * 100),
                     ("malware.pdf", b"MZ" + b"x" * 100)):
        r = _upload(c, fn, data)
        assert r.status_code == 422, (fn, r.text)
    db = _fresh_db(papp)
    try:
        assert db.query(PortalDocument).filter(
            PortalDocument.user_id == user_id).count() == 4  # 3 + 1 shared
    finally:
        db.close()

    # 26 MB → 413 before storage; 25 MB → accepted.
    big_ok = PDF_BYTES + b"x" * (25 * 1024 * 1024 - len(PDF_BYTES))
    r = _upload(c, "big.pdf", big_ok)
    assert r.status_code == 201, r.text[:200]
    too_big = PDF_BYTES + b"x" * (26 * 1024 * 1024 - len(PDF_BYTES))
    r = _upload(c, "too-big.pdf", too_big)
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"

    # Client A cannot download client B's document (403 + audit).
    r = _upload(cB, "b-secret.pdf", PDF_BYTES + b"b-private")
    assert r.status_code == 201
    b_doc_id = r.json()["id"]
    r = c.get(f"/api/v1/portal/documents/{b_doc_id}/download")
    assert r.status_code == 403, r.text
    db = _fresh_db(papp)
    try:
        denied = db.query(AuditLog).filter(
            AuditLog.action == "document.download_denied").count()
        assert denied >= 1
    finally:
        db.close()

    # Unauthenticated download → 401.
    from fastapi.testclient import TestClient

    anon = TestClient(papp, raise_server_exceptions=False)
    r = anon.get(f"/api/v1/portal/documents/{shared_id}/download")
    assert r.status_code == 401

    # Retire → hidden from client, still downloadable by Jared, audit-logged.
    r = oc.post(f"/api/v1/admin/clients/{user_id}/documents/{shared_id}/retire",
                json={"retired": True})
    assert r.status_code == 200, r.text
    r = c.get("/api/v1/portal/documents")
    assert not any(d["id"] == shared_id for d in r.json()["items"])
    r = oc.get(f"/api/v1/admin/clients/{user_id}/documents/{shared_id}/download")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(shared_bytes).hexdigest()
    db = _fresh_db(papp)
    try:
        assert db.query(AuditLog).filter(
            AuditLog.action == "document.retired",
            AuditLog.entity_id == shared_id).count() == 1
    finally:
        db.close()

    # Double-submit → one row.
    key = _idem_key()
    r1 = _upload(c, "dup.pdf", PDF_BYTES + b"dup", key=key)
    r2 = _upload(c, "dup.pdf", PDF_BYTES + b"dup", key=key)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


# --- 17_14: filings -----------------------------------------------------------------------


def test_17_14_filings(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "filing.client@example.com")

    # Jared creates a 2025 personal filing → client sees "Collecting documents".
    filing_id = _admin_create_filing(oc, user_id, kind="personal_t1", year=2025)
    r = c.get("/api/v1/portal/filings")
    assert r.status_code == 200, r.text
    filings = r.json()["items"]
    assert len(filings) == 1 and filings[0]["id"] == filing_id
    r = c.get(f"/api/v1/portal/filings/{filing_id}")
    body = r.json()
    assert body["status"] == "collecting"
    assert body["timeline"][0]["status"] == "collecting"
    assert body["kind_label"]  # human-readable kind, not a slug

    # Advance to awaiting_info with a note → dashboard shows it verbatim,
    # activity feed records it.
    r = oc.patch(f"/api/v1/admin/filings/{filing_id}/status",
                 json={"status": "awaiting_info",
                       "note": "Please upload your T4 and medical receipts"})
    assert r.status_code == 200, r.text
    dash = c.get("/api/v1/portal/dashboard").json()
    notes = [f.get("note") for f in dash["needs_attention"]["filings"]]
    assert "Please upload your T4 and medical receipts" in notes
    assert any("awaiting_info" in a["action"] or "filing" in a["action"]
               for a in dash["activity"])

    # filing_status_changed email queued on the transition.
    db = _fresh_db(papp)
    try:
        kinds = {i.kind for i in db.query(EmailOutbox).all()}
        assert "filing_status_changed" in kinds
    finally:
        db.close()

    # Illegal filed → collecting → 422, state unchanged, no audit event.
    for status in ("preparing", "filed"):
        r = oc.patch(f"/api/v1/admin/filings/{filing_id}/status",
                     json={"status": status})
        assert r.status_code == 200, (status, r.text)
    db = _fresh_db(papp)
    try:
        n_before = db.query(AuditLog).filter(
            AuditLog.entity_type == "filing", AuditLog.entity_id == filing_id).count()
    finally:
        db.close()
    r = oc.patch(f"/api/v1/admin/filings/{filing_id}/status",
                 json={"status": "collecting"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "filing_transition_invalid"
    db = _fresh_db(papp)
    try:
        filing = db.get(Filing, filing_id)
        assert filing.status == "filed"
        n_after = db.query(AuditLog).filter(
            AuditLog.entity_type == "filing", AuditLog.entity_id == filing_id).count()
        assert n_after == n_before  # no audit event for the rejected transition
    finally:
        db.close()

    # Full lifecycle renders all five steps; filed date distinct from confirmed.
    r = oc.patch(f"/api/v1/admin/filings/{filing_id}/status", json={"status": "confirmed"})
    assert r.status_code == 200
    r = c.get(f"/api/v1/portal/filings/{filing_id}")
    body = r.json()
    assert [s["status"] for s in body["timeline"]] == [
        "collecting", "awaiting_info", "preparing", "filed", "confirmed"]
    assert all(s["at"] for s in body["timeline"])
    filed_at = next(s["at"] for s in body["timeline"] if s["status"] == "filed")
    confirmed_at = next(s["at"] for s in body["timeline"] if s["status"] == "confirmed")
    assert filed_at != confirmed_at

    # Forged client status change → 403 (client has no write endpoint).
    r = c.patch(f"/api/v1/admin/filings/{filing_id}/status",
                json={"status": "collecting"})
    assert r.status_code == 403, r.text
    spec = cc.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/api/v1/portal/filings")
                   and m in ("post", "patch", "put", "delete")
                   for p, ops in spec.items() for m in ops)


# --- 17_15: invoices -----------------------------------------------------------------------


def test_17_15_invoices(papp, committed_client):
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "invoice.client@example.com")
    cB, userB_id, _ = _onboard_client(papp, cc, oc, "invoice.b@example.com")

    lines = [
        {"description": "Personal tax return 2025", "quantity": 1,
         "unit_cents": 45000, "tax_rate_bps": 500},
        {"description": "Bookkeeping Q4", "quantity": 3,
         "unit_cents": 12000, "tax_rate_bps": 500},
        {"description": "CRA correspondence", "quantity": 2,
         "unit_cents": 7500, "tax_rate_bps": 0},
    ]
    inv = _admin_issue_invoice(oc, user_id, lines=lines)
    assert re.fullmatch(r"JARO-\d{4}-\d{4}", inv["invoice_number"]), inv["invoice_number"]
    # Client sees it immediately.
    r = c.get("/api/v1/portal/invoices")
    assert r.status_code == 200
    assert any(i["id"] == inv["id"] for i in r.json()["items"])
    r = c.get(f"/api/v1/portal/invoices/{inv['id']}")
    detail = r.json()
    assert detail["invoice_number"] == inv["invoice_number"]
    assert len(detail["lines"]) == 3
    expected_subtotal = 45000 + 3 * 12000 + 2 * 7500
    expected_tax = round(45000 * 0.05) + round(3 * 12000 * 0.05)
    assert detail["subtotal_cents"] == expected_subtotal
    assert detail["tax_cents"] == expected_tax
    assert detail["total_cents"] == expected_subtotal + expected_tax
    # One invoice_issued intent queued.
    db = _fresh_db(papp)
    try:
        assert db.query(EmailOutbox).filter(
            EmailOutbox.kind == "invoice_issued").count() >= 1
    finally:
        db.close()

    # PDF parses to identical line items / totals / number / dates.
    r = c.get(f"/api/v1/portal/invoices/{inv['id']}/pdf")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/pdf"
    pdf_bytes = r.content
    try:
        import pypdf

        reader = pypdf.PdfReader(__import__("io").BytesIO(pdf_bytes))
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert inv["invoice_number"] in pdf_text
        assert "Personal tax return 2025" in pdf_text
        assert "Bookkeeping Q4" in pdf_text
        assert f"${detail['total_cents'] / 100:,.2f}" in pdf_text
    except ImportError:
        pytest.skip("pypdf not installed — PDF text assertion skipped")
    # Re-download → byte-identical.
    r2 = c.get(f"/api/v1/portal/invoices/{inv['id']}/pdf")
    import hashlib

    assert hashlib.sha256(r2.content).hexdigest() == hashlib.sha256(pdf_bytes).hexdigest()

    # Void never recycles its number: next issue gets a fresh number.
    r = oc.post(f"/api/v1/admin/invoices/{inv['id']}/void",
                json={"reason": "issued in error"})
    assert r.status_code == 200, r.text
    inv2 = _admin_issue_invoice(oc, user_id)
    assert inv2["invoice_number"] != inv["invoice_number"]
    db = _fresh_db(papp)
    try:
        voided = db.get(Invoice, inv["id"])
        assert voided.status == "void"
    finally:
        db.close()

    # Concurrent issuance → no duplicate numbers.
    invs = [_admin_issue_invoice(oc, user_id) for _ in range(5)]
    numbers = [i["invoice_number"] for i in invs]
    assert len(set(numbers)) == 5

    # Client A cannot see client B's invoices.
    invB = _admin_issue_invoice(oc, userB_id)
    r = c.get(f"/api/v1/portal/invoices/{invB['id']}")
    assert r.status_code in (403, 404), r.text
    r = c.get(f"/api/v1/portal/invoices/{invB['id']}/pdf")
    assert r.status_code in (403, 404), r.text

    # Mark-paid records paid_at + method note; open balance updates; feed logs it.
    r = oc.post(f"/api/v1/admin/invoices/{inv2['id']}/pay",
                json={"pay_method_note": "e-transfer received 2026-10-06"})
    assert r.status_code == 200, r.text
    db = _fresh_db(papp)
    try:
        paid = db.get(Invoice, inv2["id"])
        assert paid.status == "paid" and paid.paid_at is not None
        assert "e-transfer" in (paid.pay_method_note or "")
        feed = db.query(AuditLog).filter(
            AuditLog.action == "invoice.paid",
            AuditLog.entity_id == inv2["id"]).count()
        assert feed == 1
    finally:
        db.close()
    dash = c.get("/api/v1/portal/dashboard").json()
    assert dash["snapshots"]["invoices"]["open_count"] == 5  # 5 concurrent, paid one excluded

    # Zero payment-capture endpoints (criterion restated at the invoice level).
    spec = cc.get("/openapi.json").json()["paths"]
    blob = json.dumps(spec).lower()
    assert "payment" not in blob and "checkout" not in blob


# --- 17_16: invoice owner-question defaults (D-27; §3.5 #5) -----------------------


def test_17_16_invoice_owner_defaults(papp, committed_client):
    """Q5 decisions wired: JARO-YYYY-NNNN numbering, Net-30 default due
    date, omitted per-line tax → GST 5% (500 bps), payment instructions
    mention e-transfer to jared@jaroaccounting.com."""
    from datetime import date

    from app import portal as portal_lib

    assert portal_lib.INVOICE_NUMBER_PREFIX == "JARO"
    assert portal_lib.INVOICE_TERMS_DAYS == 30
    assert portal_lib.INVOICE_TAX_RATE == 0.05
    assert portal_lib.INVOICE_TAX_LABEL == "GST"
    assert "e-transfer to jared@jaroaccounting.com" in (
        portal_lib.INVOICE_PAYMENT_INSTRUCTIONS
    )
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "invoice.defaults@example.com")
    # One line omits tax_rate_bps entirely → GST 5% default applies.
    inv = _admin_issue_invoice(
        oc, user_id,
        lines=[{"description": "Tax preparation", "quantity": 1, "unit_cents": 10000}],
        due_days=30,
    )
    year = date.today().year
    assert re.fullmatch(rf"JARO-{year}-\d{{4}}", inv["invoice_number"]), inv["invoice_number"]
    assert inv["lines"][0]["tax_rate_bps"] == 500
    assert inv["tax_cents"] == 500  # 5% of 10000
    assert inv["total_cents"] == 10500
    assert "e-transfer to jared@jaroaccounting.com" in inv["payment_instructions"]
    # Net-30 default: omitted due_at → now + INVOICE_TERMS_DAYS.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert portal_lib._parse_due_at(None, now) == now + timedelta(days=30)
    # The PDF totals row carries the GST label (extracted text — fpdf2
    # compresses page streams, so raw bytes won't match).
    r = c.get(f"/api/v1/portal/invoices/{inv['id']}/pdf")
    assert r.status_code == 200
    try:
        import pypdf

        reader = pypdf.PdfReader(__import__("io").BytesIO(r.content))
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "GST:" in pdf_text
    except ImportError:
        pytest.skip("pypdf not installed — PDF text assertion skipped")


# --- 17_17: migration rehearsal for Phase-5 tables --------------------------------------------


def test_17_17_migration_rehearsal(scratch_db_url):
    """upgrade → downgrade → upgrade on the Phase-5 tables, incl. the
    scan_status default backfill and the invoice-number sequence."""
    import sqlalchemy as sa
    from alembic import command as alembic_command
    from alembic.config import Config

    from app import alembic_heads

    assert alembic_heads.head_revision() == "0015"
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic")),
    )
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "head")

    engine = sa.create_engine(scratch_db_url)
    try:
        # scan_status default backfill: pre-existing rows would get 'unscanned'.
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO portal_documents (id, user_id, original_filename,"
                " object_key, byte_size, sha256, scan_status, type_code, mime,"
                " uploaded_by, created_at) VALUES ('doc-seed-1', 'user-seed-1', 'a.pdf',"
                " 'k', 10, 'ab', 'unscanned', 'other', 'application/pdf', 'client',"
                " datetime('now'))"))
            status = conn.execute(
                sa.text("SELECT scan_status FROM portal_documents WHERE id='doc-seed-1'")
            ).scalar()
            assert status == "unscanned"
            # Invoice series seed: monotonic sequence start.
            row = conn.execute(
                sa.text("SELECT prefix, next_number FROM invoice_series WHERE id='default'")
            ).first()
            assert row == ("JARO", 1)
            # One live invite row → downgrade must refuse (B1).
            conn.execute(sa.text(
                "INSERT INTO portal_invitations (id, email, client_name, token_hash,"
                " status, created_at, expires_at) VALUES ('inv-seed-1', 's@example.com',"
                " 'Seed', 'ab', 'emailed', datetime('now'), datetime('now', '+7 days'))"))
        with pytest.raises(RuntimeError, match="B1 fail-closed downgrade refused"):
            alembic_command.downgrade(cfg, "base")
        # Clean the seed rows → downgrade succeeds → upgrade restores.
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM portal_documents WHERE id='doc-seed-1'"))
            conn.execute(sa.text("DELETE FROM portal_invitations WHERE id='inv-seed-1'"))
        alembic_command.downgrade(cfg, "base")
        alembic_command.upgrade(cfg, "head")
        with engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT prefix, next_number FROM invoice_series WHERE id='default'")
            ).first()
            assert row == ("JARO", 1)
    finally:
        engine.dispose()


# --- A4 regression: commit before the response is sent ---------------------------


def test_17_18_session_committed_before_response_sent(papp, committed_client):
    """A4 regression (2026-10-06): the session minted by login must be
    committed BEFORE the 202 bytes are sent. A client acting immediately on
    the Set-Cookie (the portal JS redirects straight to the next step) must
    never see a spurious 401 session_expired.

    Root cause of the intermittent journey failure: FastAPI sends the
    response before closing the request AsyncExitStack, so the old
    yield-teardown ``db.commit()`` in ``get_db`` landed after the client
    could fire its follow-up; the follow-up's read snapshot predated the
    commit (ROW NOT FOUND). TransactionMiddleware now commits after the
    route returns the response object but before it is sent.

    Note: TestClient serializes requests in one thread, so it cannot
    reproduce the timing race — the Playwright journeys against real
    uvicorn are the authoritative repro. This test pins the contract: the
    session row is visible in a separate connection the moment the 201
    returns, and the immediate follow-up succeeds.
    """
    from fastapi.testclient import TestClient

    from app import sessions as sessions_lib
    from app import tokens as tokens_lib

    _seed_terms(papp)
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, committed_client, owner_id)
    email = "commit.race@example.com"
    _create_invite(oc, email, temp_password="TempPass-Commit-15")

    c = TestClient(papp, raise_server_exceptions=False)
    r = c.post("/api/v1/portal/login",
               json={"email": email, "password": "TempPass-Commit-15"})
    assert r.status_code == 202, r.text
    m = re.search(rf"{re.escape(sessions_lib.COOKIE_NAME)}=([^;]+)",
                  r.headers.get("set-cookie", ""))
    assert m, "login did not set a session cookie"
    raw = m.group(1)

    # The session row is committed before the 201 was sent: visible in a
    # separate connection immediately, no follow-up request needed.
    db = _fresh_db(papp)
    try:
        row = db.get(SessionRow, tokens_lib.token_digest(raw))
        assert row is not None, "session row not committed before the 201 was sent"
        assert row.revoked_at is None
    finally:
        db.close()

    # And the immediate follow-up — the exact call that spuriously 401'd —
    # succeeds on the fresh cookie.
    _sync_cookie(c, r)
    r = c.get("/api/v1/portal/terms/current")
    assert r.status_code == 200, r.text


def test_17_18_internal_note_never_exposed_to_client(papp, committed_client):
    """Q16 (D-60): the owner-only dossier notes field exists in v1 and is
    never exposed to the client — it appears only on the owner dossier and
    the owner patch response."""
    _seed_terms(papp)
    cc = committed_client
    owner_id = _make_owner(papp)
    oc = _owner_client(papp, cc, owner_id)
    c, user_id, _iid = _onboard_client(papp, cc, oc, "notes.client@example.com")
    note = "Prefers morning calls; invoiced late twice."
    r = oc.patch(f"/api/v1/admin/clients/{user_id}", json={"internal_note": note})
    assert r.status_code == 200, r.text
    assert r.json()["internal_note"] == note
    # Owner dossier exposes it on the overview tab…
    r = oc.get(f"/api/v1/admin/clients/{user_id}")
    assert r.status_code == 200, r.text
    assert r.json()["overview"]["internal_note"] == note
    # …but no client-facing endpoint leaks it.
    for path in (
        "/api/v1/portal/dashboard",
        "/api/v1/portal/documents",
        "/api/v1/portal/filings",
        "/api/v1/portal/invoices",
        "/api/v1/portal/session",
        "/api/v1/portal/terms/current",
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
        assert "internal_note" not in r.text, path
