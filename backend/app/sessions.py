"""Session middleware — opaque cookie, sliding window, touch throttle, cap.

Cookie: ``__Host-jaro_session`` — HttpOnly, Secure, SameSite=Lax, Path=/.
(AUTH D42 adopts the ``__Host-`` prefix; RFC 6265bis requires Path=/ for
``__Host-`` cookies, and the API lives under /api/v1/* anyway, so Path=/
— not /portal. See backend/DECISIONS-backend.md.)

- 15-minute sliding idle window on ``last_seen_at``
- ``last_seen_at`` rewritten at most once per 60s (D-35 write-amplification guard)
- 12-hour absolute cap (D-02)
- 5 concurrent sessions per user; the 6th login evicts the oldest (D-24)
- every authed response carries ``X-Session-Expires-In`` (milliseconds)

The middleware validates the cookie against the DB and exposes a frozen
``SessionView`` on ``request.state.session``. Route-owned transactions (A4)
stay with the route: anything needing the ORM row re-fetches it by id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import tokens
from .db import utcnow
from .errors import ApiError
from .models import PortalUser, Session

COOKIE_NAME = "__Host-jaro_session"
SESSION_IDLE = timedelta(minutes=15)
TOUCH_THROTTLE = timedelta(seconds=60)
ABSOLUTE_CAP = timedelta(hours=12)
MAX_CONCURRENT_SESSIONS = 5
EXPIRES_IN_HEADER = "X-Session-Expires-In"


@dataclass(frozen=True)
class SessionView:
    id: str
    user_id: str
    mfa_verified_at: datetime
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime


def create_session(
    db,
    *,
    user_id: str,
    ip_hash: str | None = None,
    user_agent_hash: str | None = None,
    user_agent_summary: str | None = None,
) -> tuple[str, Session]:
    """Create a session; returns (raw_token, row). Enforces the 5-session
    cap with evict-oldest (D-24)."""
    now = utcnow()
    raw = tokens.new_token(32)
    row = Session(
        id=tokens.token_digest(raw),
        user_id=user_id,
        mfa_verified_at=now,
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + ABSOLUTE_CAP,
        ip_hash=ip_hash,
        user_agent_hash=user_agent_hash,
        user_agent_summary=user_agent_summary,
    )
    db.add(row)
    db.flush()
    enforce_session_cap(db, user_id)
    return raw, row


def enforce_session_cap(db, user_id: str, limit: int = MAX_CONCURRENT_SESSIONS) -> int:
    """Keep the ``limit`` newest live sessions; revoke the rest. Returns the
    number evicted."""
    live = (
        db.execute(
            select(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .order_by(Session.created_at.desc(), Session.id.desc())
        )
        .scalars()
        .all()
    )
    evicted = 0
    now = utcnow()
    for row in live[limit:]:
        row.revoked_at = now
        evicted += 1
    if evicted:
        db.flush()
    return evicted


def revoke_all_sessions(db, user_id: str) -> int:
    now = utcnow()
    rows = (
        db.execute(
            select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now
    db.flush()
    return len(rows)


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def expires_in_ms(view: SessionView, now=None) -> int:
    now = now or utcnow()
    idle_deadline = view.last_seen_at + SESSION_IDLE
    deadline = min(idle_deadline, view.absolute_expires_at)
    return max(0, int((deadline - now).total_seconds() * 1000))


class SessionMiddleware(BaseHTTPMiddleware):
    """Validates the session cookie; touch-throttled ``last_seen_at`` writes
    happen here in a short-lived DB session so route transactions stay
    route-owned (A4)."""

    def __init__(self, app, session_factory):
        super().__init__(app)
        self._session_factory = session_factory

    async def dispatch(self, request: Request, call_next):
        request.state.session = None
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            db = self._session_factory()
            try:
                row = db.get(Session, tokens.token_digest(raw))
                now = utcnow()
                if (
                    row is not None
                    and row.revoked_at is None
                    and now <= row.absolute_expires_at
                    and now - row.last_seen_at <= SESSION_IDLE
                ):
                    # The session-status read is exempt from touching: it is
                    # the frontend's idle countdown probe, and touching it
                    # would make "signed out after 15 minutes idle" unreachable
                    # (the ≤120s warning banner could never appear).
                    is_status_probe = request.url.path == "/api/v1/portal/session"
                    if not is_status_probe and now - row.last_seen_at >= TOUCH_THROTTLE:
                        row.last_seen_at = now
                        db.commit()
                    request.state.session = SessionView(
                        id=row.id,
                        user_id=row.user_id,
                        mfa_verified_at=row.mfa_verified_at,
                        created_at=row.created_at,
                        last_seen_at=row.last_seen_at,
                        absolute_expires_at=row.absolute_expires_at,
                    )
                # Idle-expired / revoked / absent sessions simply leave
                # request.state.session as None; routes translate that into
                # 401 session_expired with a generic message (A6).
            finally:
                db.close()
        response = await call_next(request)
        session = request.state.session
        if session is not None:
            response.headers[EXPIRES_IN_HEADER] = str(expires_in_ms(session))
        return response


def require_session(request: Request) -> SessionView:
    session = getattr(request.state, "session", None)
    if session is None:
        raise ApiError("session_expired")
    return session


def require_owner(request: Request, db) -> PortalUser:
    session = require_session(request)
    user = db.get(PortalUser, session.user_id)
    if user is None or user.role != "owner" or user.status != "active":
        raise ApiError("forbidden")
    return user
