"""DONE 5 — idempotency.

- same key + same payload -> stored result replayed (replayed: true)
- same key + different payload -> 409 idempotency_conflict
- missing key on a keyed route -> 422 idempotency_key_required

Exercised through the production dependency + helper on a minimal app
(the Phase-0 route inventory has no executing keyed route yet; the
mechanism under test is the real one).
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import idempotency  # noqa: E402
from app.errors import (  # noqa: E402
    ApiError,
    api_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging_config import RequestIDMiddleware  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402


@pytest.fixture()
def keyed_client(db):
    app = FastAPI()

    async def _db():
        yield db

    @app.post("/submit")
    async def submit(request: Request, payload: dict, key: str = Depends(idempotency.require_idempotency_key),
                     session=Depends(_db)):
        route = "POST /submit"
        try:
            phash = idempotency.begin(session, route=route, client_key=key, payload=payload)
        except idempotency.ReplayHit as hit:
            return JSONResponse(status_code=hit.status_code, content=hit.result)
        result = {"stored": payload.get("value"), "writes": 1}
        idempotency.store(session, route=route, client_key=key, payload_hash=phash,
                          result=result, status_code=201)
        session.commit()
        return JSONResponse(status_code=201, content=result)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_middleware(RequestIDMiddleware)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_replay_returns_stored_result(keyed_client):
    key = "key-abc-123"
    r1 = keyed_client.post("/submit", json={"value": 1}, headers={"Idempotency-Key": key})
    assert r1.status_code == 201
    assert r1.json() == {"stored": 1, "writes": 1}

    r2 = keyed_client.post("/submit", json={"value": 1}, headers={"Idempotency-Key": key})
    assert r2.status_code == 201
    assert r2.json() == {"stored": 1, "writes": 1, "replayed": True}


def test_conflict_on_different_payload(keyed_client):
    key = "key-def-456"
    r1 = keyed_client.post("/submit", json={"value": 1}, headers={"Idempotency-Key": key})
    assert r1.status_code == 201

    r2 = keyed_client.post("/submit", json={"value": 2}, headers={"Idempotency-Key": key})
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_missing_key_422(keyed_client):
    r = keyed_client.post("/submit", json={"value": 1})
    assert r.status_code == 422
    assert r.json()["code"] == "idempotency_key_required"


def test_scope_hash_composition():
    # sha256(route || ':' || client_key) with a domain separator (D-14/A3).
    h1 = idempotency.scope_hash("POST /x", "k")
    assert h1 == idempotency.scope_hash("POST /x", "k")
    assert h1 != idempotency.scope_hash("POST /y", "k")
    assert h1 != idempotency.scope_hash("POST /x", "k2")


def test_payload_hash_is_canonical():
    assert idempotency.payload_hash({"b": 1, "a": 2}) == idempotency.payload_hash({"a": 2, "b": 1})
