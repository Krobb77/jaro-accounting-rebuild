"""DONE 4 — every route returns the A1 envelope on a forced error.

Iterates the real OpenAPI inventory: for each path+method, forces an
error (401 unauthenticated / 422 empty body / 404 / 501 skeleton /
503 stale calendar) and asserts the {code, message, request_id, fields[]}
shape with X-Request-ID echoed.
"""

from __future__ import annotations

import os
import re
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DUMMY_ID = "00000000-0000-0000-0000-000000000000"
SKIP_PATHS = {"/healthz", "/readyz", "/openapi.json", "/docs", "/redoc"}
# Routes that legitimately return 200 on a parameterless forced request
# (public config with no required params) — not error paths.
ALLOW_200 = {"/api/v1/booking/config"}


@pytest.fixture(autouse=True)
def _clean_rate_windows(db_admin):
    """The forced-error sweep below hammers ~70 rate-limited routes from one
    test source; the shared per-source throttle would 429 later tests.
    The session-scoped DB needs explicit cleanup (test isolation)."""
    yield
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM rate_windows"))


def _forced_requests(paths: dict):
    """Yield (method, path, kwargs) forced-error requests for every route."""
    for path, ops in sorted(paths.items()):
        if path in SKIP_PATHS:
            continue
        concrete = re.sub(r"\{[^}]+\}", DUMMY_ID, path)
        for method, _op in sorted(ops.items()):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            kwargs: dict = {}
            if method in {"post", "put", "patch"}:
                kwargs["json"] = {}
            yield method.upper(), concrete, kwargs


def test_every_route_returns_a1_envelope_on_forced_error(client):
    spec = client.get("/openapi.json").json()
    requests = list(_forced_requests(spec["paths"]))
    assert len(requests) >= 60, f"route inventory looks too small: {len(requests)}"

    failures = []
    for method, path, kwargs in requests:
        r = client.request(method, path, **kwargs)
        if path in ALLOW_200 and r.status_code == 200:
            continue  # legitimately parameterless-200; not an error path
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        ok = (
            r.status_code >= 400
            and body is not None
            and isinstance(body.get("code"), str)
            and isinstance(body.get("message"), str)
            and isinstance(body.get("request_id"), str)
            and isinstance(body.get("fields"), list)
            and r.headers.get("X-Request-ID") == body["request_id"]
        )
        if not ok:
            failures.append((method, path, r.status_code, body))
    assert not failures, f"routes without A1 envelope: {failures}"


def test_envelope_shape_on_known_errors(client):
    # 404
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert set(r.json()) == {"code", "message", "request_id", "fields"}
    assert r.headers["X-Request-ID"] == r.json()["request_id"]
    assert r.headers["Cache-Control"] == "no-store"

    # 422 validation with per-field pairs, no raw input echoed
    r = client.post("/api/v1/portal/login", json={"email": "a@b.com"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_failed"
    assert all(set(f) == {"field", "message"} for f in body["fields"])
    assert any(f["field"] == "password" for f in body["fields"])
    # raw secrets are never echoed back in field errors
    r = client.post("/api/v1/portal/login", json={"email": "a@b.com", "password": "s3cret!"})
    assert "s3cret!" not in r.text

    # 401 on protected route without session
    r = client.get("/api/v1/portal/dashboard")
    assert r.status_code == 401
    assert r.json()["code"] == "session_expired"
