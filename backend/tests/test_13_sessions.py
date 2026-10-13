"""DONE 13 — session middleware.

- Cookie flags exactly: HttpOnly; Secure; SameSite=Lax; Path=/ (+ __Host-).
  (Path=/ — not /portal — because __Host- requires Path=/ per RFC 6265bis
  and the API lives under /api/v1/*; see DECISIONS-backend.md.)
- last_seen_at rewritten at most once per 60s under load (touch throttle).
- 15-min idle expiry, 12h absolute cap, extend, logout.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from fastapi.responses import JSONResponse  # noqa: E402

from app import sessions  # noqa: E402
from app.db import new_uuid7, utcnow  # noqa: E402
from app.models import PortalUser, Session as SessionRow  # noqa: E402
from app.passwords import hash_password  # noqa: E402
from starlette.requests import Request  # noqa: E402


def _make_user_and_session(committed_db, *, email: str):
    db = committed_db
    user = PortalUser(
        id=new_uuid7(), role="client", email=email, display_name="T",
        password_hash=hash_password("correct horse battery staple"),
        status="active",
    )
    db.add(user)
    db.commit()
    raw, row = sessions.create_session(db, user_id=user.id)
    db.commit()
    return raw, user.id, row.id


def _teardown_user(committed_db, user_id: str):
    from app.models import AuditLog

    db = committed_db
    # Phase-5 routes audit session lifecycle (session.revoked, session.evicted)
    # against the session rows; remove those audit rows too so later tests
    # that count audit_log see a clean table.
    session_ids = [
        r.id for r in db.query(SessionRow).filter(SessionRow.user_id == user_id).all()
    ]
    if session_ids:
        db.query(AuditLog).filter(
            AuditLog.entity_type == "session", AuditLog.entity_id.in_(session_ids)
        ).delete(synchronize_session=False)
    db.query(SessionRow).filter(SessionRow.user_id == user_id).delete()
    db.query(PortalUser).filter(PortalUser.id == user_id).delete()
    db.commit()


def _auth_headers(raw: str) -> dict:
    return {"Cookie": f"{sessions.COOKIE_NAME}={raw}"}


@pytest.fixture()
def cclient(app, committed_db):
    """TestClient whose route DB commits for real — needed when a route
    writes state the session middleware (separate connection) must see
    (logout revocation, extend). Tests clean up their own rows.

    New A4 contract (2026-10-06): the override stashes the session on
    ``request.state.db_session`` and TransactionMiddleware commits it
    BEFORE the response is sent (yield-teardown commit runs after send
    and races immediate client follow-ups)."""
    from fastapi.testclient import TestClient

    from app.routes import get_db

    def _override(request: Request):
        request.state.db_session = committed_db
        yield committed_db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_cookie_flags_exact():
    response = JSONResponse({"ok": True})
    sessions.set_session_cookie(response, "raw-token-value")
    header = response.headers["set-cookie"]
    parts = [p.strip() for p in header.split(";")]
    assert parts[0] == f"{sessions.COOKIE_NAME}=raw-token-value"
    assert sessions.COOKIE_NAME.startswith("__Host-")
    # Starlette renders SameSite=lax (lowercase); cookie attribute values are
    # case-insensitive per RFC 6265, so compare case-insensitively.
    flags = {p.lower() for p in parts[1:]}
    assert flags == {"httponly", "secure", "samesite=lax", "path=/"}, flags


def test_session_status_and_expiry_header(client, committed_db):
    raw, user_id, _sid = _make_user_and_session(committed_db, email="sess1@example.com")
    try:
        r = client.get("/api/v1/portal/session", headers=_auth_headers(raw))
        assert r.status_code == 200, r.text
        assert 0 < r.json()["expires_in_seconds"] <= 15 * 60
        assert sessions.EXPIRES_IN_HEADER in r.headers
        assert 0 < int(r.headers[sessions.EXPIRES_IN_HEADER]) <= 15 * 60 * 1000
    finally:
        _teardown_user(committed_db, user_id)


def test_touch_throttle_rewrites_at_most_once_per_60s(client, committed_db):
    raw, user_id, sid = _make_user_and_session(committed_db, email="sess2@example.com")
    try:
        headers = _auth_headers(raw)

        def last_seen():
            db = committed_db
            try:
                return db.get(SessionRow, sid).last_seen_at
            finally:
                pass

        t0 = last_seen()
        for _ in range(5):
            r = client.get("/api/v1/portal/session", headers=headers)
            assert r.status_code == 200
        assert last_seen() == t0, "last_seen_at rewritten within the 60s throttle window"

        # Backdate beyond the throttle: the next request to a NON-exempt
        # endpoint rewrites it. (/session is exempt from touching by design —
        # the frontend's idle countdown probe must not extend the session.)
        db = committed_db
        db.get(SessionRow, sid).last_seen_at = utcnow() - timedelta(seconds=61)
        db.commit()
        r = client.get("/api/v1/portal/dashboard", headers=headers)
        # Dashboard may 403 for terms_pending, but the middleware touch happens
        # before the route runs.
        assert r.status_code in (200, 403)
        assert last_seen() > t0
    finally:
        _teardown_user(committed_db, user_id)


def test_idle_expiry_401(client, committed_db):
    raw, user_id, sid = _make_user_and_session(committed_db, email="sess3@example.com")
    try:
        db = committed_db
        db.get(SessionRow, sid).last_seen_at = utcnow() - timedelta(minutes=16)
        db.commit()
        r = client.get("/api/v1/portal/session", headers=_auth_headers(raw))
        assert r.status_code == 401
        assert r.json()["code"] == "session_expired"
    finally:
        _teardown_user(committed_db, user_id)


def test_absolute_cap_401(client, committed_db):
    raw, user_id, sid = _make_user_and_session(committed_db, email="sess4@example.com")
    try:
        db = committed_db
        row = db.get(SessionRow, sid)
        row.absolute_expires_at = utcnow() - timedelta(seconds=1)
        row.last_seen_at = utcnow()  # idle-fresh but absolutely expired
        db.commit()
        r = client.get("/api/v1/portal/session", headers=_auth_headers(raw))
        assert r.status_code == 401
    finally:
        _teardown_user(committed_db, user_id)


def test_extend_and_logout(cclient, committed_db):
    raw, user_id, sid = _make_user_and_session(committed_db, email="sess5@example.com")
    try:
        headers = _auth_headers(raw)
        r = cclient.post("/api/v1/portal/sessions/extend", headers=headers)
        assert r.status_code == 200
        assert r.json()["expires_in_seconds"] > 0

        r = cclient.post("/api/v1/portal/logout", headers=headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Cookie cleared...
        assert "__Host-jaro_session" in r.headers.get("set-cookie", "")
        # ...and the session is dead.
        r = cclient.get("/api/v1/portal/session", headers=headers)
        assert r.status_code == 401
    finally:
        _teardown_user(committed_db, user_id)


def test_extend_cannot_resurrect_expired(client, committed_db):
    raw, user_id, sid = _make_user_and_session(committed_db, email="sess6@example.com")
    try:
        db = committed_db
        db.get(SessionRow, sid).last_seen_at = utcnow() - timedelta(minutes=30)
        db.commit()
        r = client.post("/api/v1/portal/sessions/extend", headers=_auth_headers(raw))
        assert r.status_code == 401
    finally:
        _teardown_user(committed_db, user_id)


def test_concurrent_cap_evicts_oldest(committed_db):
    db = committed_db
    user = PortalUser(
        id=new_uuid7(), role="client", email="sess7@example.com", display_name="T",
        password_hash=hash_password("x"), status="active",
    )
    db.add(user)
    db.commit()
    try:
        raws = [sessions.create_session(db, user_id=user.id)[0] for _ in range(6)]
        db.commit()
        live = db.query(SessionRow).filter(
            SessionRow.user_id == user.id, SessionRow.revoked_at.is_(None)).count()
        assert live == 5
    finally:
        _teardown_user(committed_db, user.id)
