"""A3 — idempotency keys with payload-hash conflict detection.

Scope hash: ``sha256(route || ':' || client_key)`` (the composition is this
repo's canonical form per IMPLEMENTATION_PLAN D-14/A3; the mechanism —
scope hash + payload-hash 409 + stored replay — is the generalizable part).

- same key + same payload  -> stored result, ``replayed: true``
- same key + different payload -> 409 ``idempotency_conflict`` (never silent
  duplicate, never silent overwrite)
- missing/blank key on a keyed route -> 422 ``idempotency_key_required``

Route-owned transaction discipline (A4): ``begin()`` checks, the route
executes, ``store()`` persists — all inside the caller's transaction.
"""

from __future__ import annotations

import json

from fastapi import Request
from .db import sha256_hex
from .errors import ApiError
from .models import IdempotencyReplay

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def scope_hash(route: str, client_key: str) -> str:
    return sha256_hex("jaro-idempotency-v1", route, client_key)


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_hex("jaro-payload-v1", canonical)


def require_idempotency_key(request: Request) -> str:
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not key:
        raise ApiError("idempotency_key_required")
    if len(key) > 200:
        raise ApiError("validation_failed", "Idempotency-Key is too long.", [{"field": "Idempotency-Key", "message": "must be <= 200 characters"}])
    return key


class ReplayHit(Exception):
    """Raised by begin() when the result is already stored."""

    def __init__(self, result: dict, status_code: int):
        super().__init__("replayed")
        self.result = result
        self.status_code = status_code


def begin(db, *, route: str, client_key: str, payload: dict) -> str:
    """Check for a previous execution.

    Returns the payload hash the caller must pass to ``store()`` after
    executing. Raises ``ReplayHit`` (return the stored result with
    ``replayed: true``) or ``ApiError`` 409 on conflict.
    """
    phash = payload_hash(payload)
    row = db.get(IdempotencyReplay, scope_hash(route, client_key))
    if row is None:
        return phash
    if row.payload_hash != phash:
        raise ApiError("idempotency_conflict")
    result = dict(row.result or {})
    result["replayed"] = True
    raise ReplayHit(result, row.status_code)


def store(db, *, route: str, client_key: str, payload_hash: str, result: dict, status_code: int) -> None:
    """Persist the result inside the caller's transaction (A4)."""
    stored = dict(result or {})
    stored.pop("replayed", None)
    db.add(
        IdempotencyReplay(
            scope_hash=scope_hash(route, client_key),
            route=route,
            client_key=client_key,
            payload_hash=payload_hash,
            result=stored,
            status_code=status_code,
        )
    )
    db.flush()
