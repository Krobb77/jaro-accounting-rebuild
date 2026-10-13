"""DONE 3 — /healthz and /readyz.

- GET /healthz -> 200, no DB touched.
- GET /readyz -> 200 with DB + outbox-claim probe results.
- readyz under injected DB outage -> 503 within the hard timeout.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine  # noqa: E402

from app.routes.public import READYZ_TIMEOUT_SECONDS  # noqa: E402


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_readyz_ok(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["db"] is True
    assert body["checks"]["outbox_claim"] is True


def test_readyz_db_outage_503_within_timeout(app, db):
    from fastapi.testclient import TestClient

    # Inject a DB outage: an engine whose connects always fail.
    broken = create_engine("sqlite:////nonexistent-dir-xyz-123/db.sqlite")
    original = app.state.engine
    app.state.engine = broken
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            start = time.monotonic()
            r = c.get("/readyz")
            elapsed = time.monotonic() - start
    finally:
        app.state.engine = original
        broken.dispose()
    assert r.status_code == 503
    assert r.json()["code"] == "not_ready"
    assert elapsed < READYZ_TIMEOUT_SECONDS
