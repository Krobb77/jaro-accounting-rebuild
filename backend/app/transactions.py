"""Request-scoped transaction middleware (A4).

Generalizable pattern: one transaction per request, finalized by
middleware — not by a yield-dependency teardown.

Why not commit in the ``get_db`` yield-dependency teardown? FastAPI
(>=0.1xx, verified on 0.142.2) runs yield-dependency teardown on the
request ``AsyncExitStack``, which exits *after* the response bytes are
sent (``routing.py``: ``await response(scope, receive, send)`` precedes
``request_stack.__aexit__``). Committing there leaves a read-after-write
race: a client that acts immediately on the response — e.g. following a
fresh ``Set-Cookie`` session with ``GET /api/v1/portal/terms/current`` —
can arrive before the rows are visible and get a spurious 401
``session_expired``. Proven 2026-10-06 via an intermittent failure in
the portal Playwright journeys (session row present and valid in a
post-failure dump, but ``ROW NOT FOUND`` in a lookup issued before the
late commit landed).

``TransactionMiddleware`` commits *after* the route returns the response
object (so serialization and ``response_model`` validation still see
uncommitted state exactly as before) but *before* the ASGI server sends
the bytes — ``BaseHTTPMiddleware``'s ``call_next`` returns the response
without sending it. A commit failure therefore correctly surfaces as a
500 instead of a phantom success, and idempotency records are durable
before the client can retry.

The route still owns its transaction (A4): it receives the session via
the ``DB`` dependency, which stashes it on ``request.state.db_session``
for this middleware to finalize. Routes that never touch the DB
(``/healthz``) simply have no session to finalize.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class TransactionMiddleware(BaseHTTPMiddleware):
    """Commit the request's DB session before the response is sent."""

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            db = getattr(request.state, "db_session", None)
            if db is not None:
                db.rollback()
            raise
        db = getattr(request.state, "db_session", None)
        if db is not None:
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        return response
