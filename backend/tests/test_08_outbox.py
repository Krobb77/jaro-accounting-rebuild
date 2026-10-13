"""DONE 8 — outbox round-trip in capture mode (E1–E4).

- stage intent -> worker claims (lease token match) -> captured.
- kill worker mid-send -> lease expiry -> second claim succeeds (no double-send).
- digest mismatch -> render_input_mismatch, intent killed.
- lease finish requires token match (stale worker fenced).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import outbox  # noqa: E402
from app.db import utcnow  # noqa: E402
from app.mail import CaptureMailBackend  # noqa: E402
from app.models import EmailOutbox, OutboxAttempt  # noqa: E402


@pytest.fixture()
def worker_env(app, db_admin, tmp_path):
    """Committed-session worker pointed at the app DB + a capture dir."""
    from sqlalchemy.orm import sessionmaker

    capture_dir = tmp_path / "mail"
    backend = CaptureMailBackend(str(capture_dir))
    factory = sessionmaker(bind=db_admin, autoflush=False, expire_on_commit=False)
    worker = outbox.OutboxWorker(factory, backend)
    yield worker, factory, capture_dir
    # cleanup: remove intents created by the test
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_attempts"))
        conn.execute(text("DELETE FROM email_outbox"))


def _stage(factory, **kw):
    db = factory()
    try:
        row = outbox.stage_intent(
            db,
            kind=kw.get("kind", "inquiry_receipt"),
            scope_id=kw.get("scope_id", "scope-1"),
            destination=kw.get("destination", "client@example.com"),
            snapshot_fields=kw.get("fields", {"request_id": "req-1"}),
            not_before=kw.get("not_before"),
        )
        db.commit()
        return row.id
    finally:
        db.close()


def _status(factory, intent_id):
    db = factory()
    try:
        return db.get(EmailOutbox, intent_id).status
    finally:
        db.close()


def test_outbox_round_trip_capture(worker_env):
    worker, factory, capture_dir = worker_env
    intent_id = _stage(factory, scope_id="rt-1")
    report = worker.run_cycle()
    assert report["done"] == 1
    assert _status(factory, intent_id) == "captured"
    artifacts = list(capture_dir.glob(f"{intent_id}-attempt*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text())
    assert artifact["to"] == "client@example.com"
    assert "req-1" in artifact["text_body"]
    # numbered attempt row (E4)
    db = factory()
    try:
        attempts = db.execute(
            select(OutboxAttempt).where(OutboxAttempt.outbox_id == intent_id)).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].outcome == "captured"
    finally:
        db.close()


def test_kill_mid_send_then_reclaim_no_double_send(worker_env):
    worker, factory, capture_dir = worker_env
    intent_id = _stage(factory, scope_id="kill-1")

    # First claim (worker starts sending)...
    db = factory()
    try:
        claim = outbox.claim_intent(db, intent_id)
        db.commit()
        assert claim.claimed
        lease_token = claim.lease_token
    finally:
        db.close()

    # ...worker dies before finish: nothing was captured yet.
    assert list(capture_dir.glob(f"{intent_id}-attempt*.json")) == []

    # Lease expires; a second claim succeeds (conditional-UPDATE reclaim)...
    db = factory()
    try:
        db.execute(
            text("UPDATE email_outbox SET lease_expires_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=1), "id": intent_id},
        )
        db.commit()
        claim2 = outbox.claim_intent(db, intent_id)
        db.commit()
        assert claim2.claimed
        assert claim2.lease_token != lease_token
        # ...then that worker also dies: expire the lease again so the
        # worker cycle reclaims and delivers it exactly once.
        db.execute(
            text("UPDATE email_outbox SET lease_expires_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=1), "id": intent_id},
        )
        db.commit()
    finally:
        db.close()

    # ...and the reclaimed send captures exactly once for its attempt.
    report = worker.run_cycle()
    assert _status(factory, intent_id) == "captured"
    artifacts = list(capture_dir.glob(f"{intent_id}-attempt*.json"))
    assert len(artifacts) == 1, "double-send detected"


def test_stale_lease_finish_rejected(worker_env):
    worker, factory, capture_dir = worker_env
    intent_id = _stage(factory, scope_id="stale-1")
    db = factory()
    try:
        claim = outbox.claim_intent(db, intent_id)
        db.commit()
        ok = outbox._finish(db, intent_id, "wrong-token", status="captured",
                            outcome="captured")
        assert ok is False
        db.rollback()
        # the real lease still holds: a second claim fails while leased
        claim2 = outbox.claim_intent(db, intent_id)
        db.rollback()
        assert not claim2.claimed
        assert claim.lease_token
    finally:
        db.close()


def test_digest_mismatch_kills_intent(worker_env):
    worker, factory, capture_dir = worker_env
    intent_id = _stage(factory, scope_id="tamper-1")
    # Tamper with the frozen snapshot after staging (E2 violation).
    db = factory()
    try:
        row = db.get(EmailOutbox, intent_id)
        snap = dict(row.frozen_snapshot)
        snap["fields"] = {**snap["fields"], "request_id": "forged"}
        row.frozen_snapshot = snap
        db.commit()
    finally:
        db.close()
    report = worker.run_cycle()
    assert report["killed"] == 1
    assert _status(factory, intent_id) == "dead"
    db = factory()
    try:
        attempt = db.execute(
            select(OutboxAttempt).where(OutboxAttempt.outbox_id == intent_id)).scalars().one()
        assert attempt.outcome == "render_input_mismatch"
    finally:
        db.close()
    assert list(capture_dir.glob(f"{intent_id}-attempt*.json")) == []


def test_dedup_key_returns_existing_row(worker_env):
    worker, factory, capture_dir = worker_env
    db = factory()
    try:
        r1 = outbox.stage_intent(db, kind="invite", scope_id="dup-1",
                                 destination="a@example.com", snapshot_fields={})
        r2 = outbox.stage_intent(db, kind="invite", scope_id="dup-1",
                                 destination="a@example.com", snapshot_fields={})
        assert r1.id == r2.id  # F3: re-issue finds the existing row
        db.commit()
    finally:
        db.close()


def test_cancel_suppression(worker_env):
    worker, factory, capture_dir = worker_env
    intent_id = _stage(factory, kind="booking_confirmation", scope_id="cancel-1")
    db = factory()
    try:
        n = outbox.cancel_pending_intents(db, kind="booking_confirmation",
                                          scope_id="cancel-1", reason="booking_cancelled")
        db.commit()
        assert n == 1
    finally:
        db.close()
    assert _status(factory, intent_id) == "cancelled"
    report = worker.run_cycle()
    assert report["done"] == 0  # suppressed intent is never claimed
    assert list(capture_dir.glob(f"{intent_id}-attempt*.json")) == []
