"""Shared route dependencies: DB session (A4 route-owned transaction)."""

from __future__ import annotations

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """A2 — closed wire models. Unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")


def get_db(request: Request):
    """A4: one transaction per request, owned by the route. Services receive
    the session and never commit.

    Lifecycle (commit/rollback) is owned by TransactionMiddleware, which
    finalizes the transaction after the route returns the response object
    but before the response bytes are sent. The post-yield teardown of a
    yield-dependency is NOT a safe commit point: FastAPI awaits/sends the
    response before closing the request AsyncExitStack, so committing here
    would let a client act on the response (fresh session cookie, booking
    reference) before the rows are visible — a read-after-write race that
    produced intermittent 401 session_expired in the portal journeys
    (2026-10-06). The session is stashed on ``request.state.db_session``
    for the middleware; it is closed here as before.
    """
    db = request.app.state.session_factory()
    request.state.db_session = db
    try:
        yield db
    finally:
        db.close()


DB = Depends(get_db)
