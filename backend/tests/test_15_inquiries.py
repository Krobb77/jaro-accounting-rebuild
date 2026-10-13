"""Phase 2 — contact-form lead pipeline (IMPLEMENTATION_PLAN §2 DONE 1–7).

Maps 1:1 to the plan's DONE criteria:
  1. valid submit → inquiry row (status='new') + exactly 2 outbox intents in
     one transaction; worker captures both; receipt copy matches
     EMAIL_TEMPLATES §4 (subject, preheader, reference, excerpt, SLA line,
     "didn't submit this?" line).
  2. double-submit (same Idempotency-Key) → one row, one receipt intent,
     replayed response.
  3. invalid → 422 with per-field {field, message} pairs in plain language;
     no row, no intents.
  4. over budget → 429 + Retry-After.
  5. chaos: dead mail provider → intents stay queued/retry with backoff,
     nothing lost; provider recovers → captured. No live send attempted
     (H1 gate).
  6. A6: 10 consecutive submits → logs contain no names, emails, or
     message bodies (grep gate).
  7. receipt goes only to the address entered on the form; per-recipient
     receipt budget blocks form-abuse mail loops.

Extras: idempotency conflict (same key + different payload → 409);
dedup-key canonical composition (D-14).
"""

from __future__ import annotations

import json
import logging
import os
import sys

import pytest
from sqlalchemy import func, select, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import outbox as outbox_lib  # noqa: E402
from app.db import utcnow  # noqa: E402
from app.mail import CaptureMailBackend, MailBackend  # noqa: E402
from app.models import AuditLog, EmailOutbox, IdempotencyReplay, Inquiry  # noqa: E402
from app.routes import public as public_routes  # noqa: E402
from app.refnums import is_valid_inquiry_reference  # noqa: E402
from starlette.requests import Request  # noqa: E402

VALID = {
    "name": "Amelia Hart",
    "email": "amelia.hart@example.com",
    "message": "Hello, I need help with my 2025 taxes. Please call me back. ",
}


@pytest.fixture()
def committed_client(app):
    """TestClient whose route writes commit (worker-visible), via the app's
    own engine. Tests using this fixture must request clean_tables.

    Committed per-request sessions (not the shared `client` fixture's
    long-lived transaction) mirror production's short-lived transactions —
    and are required on SQLite, where the `client` fixture's open write
    transaction would block the rate limiter's separate connection
    ("database is locked" -> spurious 503s)."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.routes import get_db

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)

    def _override(request: Request):
        # New A4 contract (2026-10-06): stash for TransactionMiddleware,
        # which commits BEFORE the response is sent (yield-teardown commit
        # runs after send and races immediate client follow-ups).
        db = Session()
        request.state.db_session = db
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_tables(db_admin):
    yield
    with db_admin.begin() as conn:
        for table in (
            "outbox_attempts",
            "email_outbox",
            "idempotency_replays",
            "audit_log",
            "inquiries",
            "rate_windows",
        ):
            conn.execute(text(f"DELETE FROM {table}"))


def _post(c, payload=None, key="phase2-key-1", email=None):
    body = dict(VALID)
    if payload:
        body.update(payload)
    if email is not None:
        body["email"] = email
    return c.post("/api/v1/inquiries", json=body, headers={"Idempotency-Key": key})


def _count(db, model, *clauses):
    q = select(func.count()).select_from(model)
    for clause in clauses:
        q = q.where(clause)
    return db.execute(q).scalar()


def _fresh_db(app):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    return Session()


# DONE-1 ---------------------------------------------------------------------

def test_1_valid_submit_row_two_intents_worker_captures(committed_client, app):
    c = committed_client
    resp = _post(c, key="phase2-done1-key")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["queued_count"] == 2
    assert is_valid_inquiry_reference(body["reference"])
    ref = body["reference"]

    db = _fresh_db(app)
    try:
        row = db.execute(select(Inquiry).where(Inquiry.id == body["inquiry_id"])).scalar_one()
        assert row.status == "new"
        assert row.reference == ref
        assert row.payload_hash  # idempotency payload hash persisted (A3)
        # Exactly two intents staged in the same transaction as the row.
        intents = db.execute(select(EmailOutbox)).scalars().all()
        assert len(intents) == 2
        by_kind = {i.kind: i for i in intents}
        assert set(by_kind) == {"inquiry_receipt", "inquiry_owner_notice"}
        assert all(i.status == "queued" for i in intents)
        # Canonical dedup composition: sha256(kind || ':' || scope_id),
        # scope carrying the request/inquiry reference (D-14).
        assert by_kind["inquiry_receipt"].dedup_key == outbox_lib.dedup_key(
            "inquiry_receipt", ref
        )
        assert by_kind["inquiry_owner_notice"].dedup_key == outbox_lib.dedup_key(
            "inquiry_owner_notice", ref
        )
        assert by_kind["inquiry_receipt"].dedup_key != by_kind["inquiry_owner_notice"].dedup_key
        # PII-safe audit row (no name/email/body in detail).
        audit = db.execute(select(AuditLog)).scalars().all()
        assert len(audit) == 1
        assert audit[0].action == "inquiry.submitted"
        assert audit[0].entity_id == row.id
        blob = json.dumps(audit[0].detail or {})
        assert "Amelia Hart" not in blob
        assert "amelia.hart@example.com" not in blob
        assert "2025 taxes" not in blob
    finally:
        db.close()

    # Worker captures both through the real capture backend.
    worker = outbox_lib.OutboxWorker(app.state.session_factory, app.state.mail_backend)
    report = worker.run_cycle()
    assert report["done"] == 2

    db = _fresh_db(app)
    try:
        statuses = {i.kind: i.status for i in db.execute(select(EmailOutbox)).scalars().all()}
        assert statuses == {"inquiry_receipt": "captured", "inquiry_owner_notice": "captured"}
    finally:
        db.close()

    capture_dir = app.state.settings.mail_capture_dir
    artifacts = {}
    import glob as _glob

    for path in _glob.glob(os.path.join(capture_dir, "*-attempt1.json")):
        artifacts[json.load(open(path))["subject"]] = json.load(open(path))
    receipt = artifacts[f"We received your message — ref {ref}"]
    assert receipt["to"] == "amelia.hart@example.com"  # ONLY the form's address
    assert receipt["headers"]["Reply-To"] == "Jaro Accounting <jared@jaroaccounting.com>"
    text_body = receipt["text_body"]
    # EMAIL_TEMPLATES §4 block, element by element.
    lines = text_body.splitlines()
    assert lines[0] == f"Thanks, Amelia — your message is with Jared. Your reference: {ref}."  # preheader
    assert f"Your reference: {ref}" in text_body  # reference block
    assert VALID["message"].strip()[:public_routes.RECEIPT_EXCERPT_CHARS] in text_body  # excerpt
    assert "normally within 1–2 business days" in text_body  # SLA line
    assert "Didn't submit this form?" in text_body  # didn't-submit line

    notice = artifacts[f"New website inquiry — {ref}"]
    assert notice["to"] == "jared@jaroaccounting.com"  # Jared only
    assert VALID["message"].strip() in notice["text_body"]  # FULL message
    assert "amelia.hart@example.com" in notice["headers"]["Reply-To"]


# DONE-2 ---------------------------------------------------------------------

def test_2_double_submit_same_key_replays_one_row(committed_client, app):
    c = committed_client
    key = "phase2-done2-key"
    first = _post(c, key=key)
    assert first.status_code == 201
    second = _post(c, key=key)
    # A3 replay per the Phase-0 contract (test_05): the STORED result and
    # status are returned with replayed:true — no re-execution.
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["inquiry_id"] == first.json()["inquiry_id"]
    assert second.json()["reference"] == first.json()["reference"]
    assert "X-Request-ID" in second.headers  # A1 correlation survives replay

    db = _fresh_db(app)
    try:
        assert _count(db, Inquiry) == 1
        assert _count(db, EmailOutbox, EmailOutbox.kind == "inquiry_receipt") == 1
        assert _count(db, EmailOutbox) == 2
    finally:
        db.close()


def test_2b_same_key_different_payload_conflicts(committed_client, app):
    first = _post(committed_client, key="phase2-conflict-key", payload={"message": "version one"})
    assert first.status_code == 201
    second = _post(committed_client, key="phase2-conflict-key", payload={"message": "version two"})
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"
    db = _fresh_db(app)
    try:
        assert _count(db, Inquiry) == 1  # no silent duplicate, no overwrite
    finally:
        db.close()


# DONE-3 ---------------------------------------------------------------------

def test_3_invalid_submit_422_per_field_no_writes(committed_client, app):
    resp = committed_client.post(
        "/api/v1/inquiries",
        json={"name": "   ", "email": "not-an-email", "message": "", "extra": "nope"},
        headers={"Idempotency-Key": "phase2-done3-key"},
    )
    assert resp.status_code == 422
    envelope = resp.json()
    assert envelope["code"] == "validation_failed"
    assert envelope["request_id"]
    fields = {f["field"]: f["message"] for f in envelope["fields"]}
    assert fields["name"] == "Please enter your name."
    assert fields["email"] == "Please enter a valid email address, like you@example.com."
    assert fields["message"] == "Please enter your message."
    assert fields["extra"] == "Extra inputs are not permitted"
    # Nothing was written: no row, no intents, no replay record.
    db = _fresh_db(app)
    try:
        assert _count(db, Inquiry) == 0
        assert _count(db, EmailOutbox) == 0
        assert _count(db, IdempotencyReplay) == 0
    finally:
        db.close()


def test_3b_missing_idempotency_key_422(committed_client, app):
    resp = committed_client.post("/api/v1/inquiries", json=dict(VALID))
    assert resp.status_code == 422
    assert resp.json()["code"] == "idempotency_key_required"
    db = _fresh_db(app)
    try:
        assert _count(db, Inquiry) == 0
    finally:
        db.close()


# DONE-4 ---------------------------------------------------------------------

def test_4_over_budget_429_with_retry_after(committed_client, app, monkeypatch):
    from app import rate_limits

    monkeypatch.setattr(
        rate_limits,
        "BUDGETS",
        {"inquiries.submit": {"source": (1, 3600), "route_source": (100, 3600), "global": (1000, 3600)}},
    )
    first = _post(committed_client, key="phase2-done4-a", email="quota1@example.com")
    assert first.status_code == 201
    second = _post(committed_client, key="phase2-done4-b", email="quota2@example.com")
    assert second.status_code == 429
    assert second.headers["Retry-After"].isdigit() and int(second.headers["Retry-After"]) >= 1
    body = second.json()
    assert body["code"] == "rate_limited"
    assert body["message"] == "Too many requests. Please slow down and try again."  # generic copy
    assert "quota2@example.com" not in json.dumps(body)  # no PII in the 429


# DONE-5 ---------------------------------------------------------------------

class _DeadMailBackend(MailBackend):
    """Chaos drill: the provider is down (transient)."""

    def send(self, mail, *, intent_id, attempt_no):
        raise ConnectionError("provider down (chaos drill)")


def test_5_dead_provider_retries_then_recovers_nothing_lost(committed_client, app):
    c = committed_client
    resp = _post(c, key="phase2-done5-key")
    assert resp.status_code == 201

    # H1 gate: capture mode only — no live send is structurally possible.
    assert isinstance(app.state.mail_backend, CaptureMailBackend)
    assert app.state.settings.mail_allow_send is False

    dead = outbox_lib.OutboxWorker(app.state.session_factory, _DeadMailBackend())
    report = dead.run_cycle()
    assert report["retried"] == 2
    assert report["done"] == 0

    db = _fresh_db(app)
    try:
        intents = db.execute(select(EmailOutbox)).scalars().all()
        assert len(intents) == 2
        for intent in intents:
            assert intent.status == "retry"  # queued/retry with backoff, nothing lost
            assert intent.attempts == 1
            assert intent.next_attempt_at is not None
            assert intent.next_attempt_at > utcnow()
    finally:
        db.close()
    # The dead provider captured nothing.
    import glob as _glob

    assert _glob.glob(os.path.join(app.state.settings.mail_capture_dir, "*.json")) == []

    # Provider recovers: make both intents due again, run the real worker.
    db = _fresh_db(app)
    try:
        for intent in db.execute(select(EmailOutbox)).scalars().all():
            intent.next_attempt_at = None
        db.commit()
    finally:
        db.close()
    worker = outbox_lib.OutboxWorker(app.state.session_factory, app.state.mail_backend)
    report = worker.run_cycle()
    assert report["done"] == 2
    db = _fresh_db(app)
    try:
        statuses = {i.status for i in db.execute(select(EmailOutbox)).scalars().all()}
        assert statuses == {"captured"}
    finally:
        db.close()


# DONE-6 ---------------------------------------------------------------------

def test_6_ten_submits_logs_contain_no_pii(committed_client, app, caplog):
    records = []

    class Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Cap()
    for name in ("jaro.app", "jaro.app.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)  # pytest leaves root at WARNING; be explicit
    try:
        for n in range(10):
            marker = f"piiquay-{n:02d}"
            resp = _post(
                committed_client,
                key=f"phase2-done6-key-{n:02d}",
                payload={
                    "name": f"Zzyzx Piiquay {marker}",
                    "email": f"zzyzx.{marker}@example-mail-test.invalid",
                    "message": f"secret body fragment {marker} 99213",
                },
            )
            assert resp.status_code == 201, resp.text
    finally:
        for name in ("jaro.app", "jaro.app.access"):
            logging.getLogger(name).removeHandler(handler)

    blob = "\n".join(records)
    for n in range(10):
        marker = f"piiquay-{n:02d}"
        assert f"Zzyzx Piiquay {marker}" not in blob
        assert f"zzyzx.{marker}@example-mail-test.invalid" not in blob
        assert f"secret body fragment {marker}" not in blob
    # Belt-and-braces: the rows the route itself wrote are PII-safe too —
    # audit details and pseudonymized budget keys carry no raw PII.
    db = _fresh_db(app)
    try:
        detail_blob = json.dumps(
            [a.detail for a in db.execute(select(AuditLog)).scalars().all()]
        )
        assert "piiquay" not in detail_blob
        assert "example-mail-test.invalid" not in detail_blob
        assert "99213" not in detail_blob
        key_blob = " ".join(
            r[0] for r in db.execute(text("SELECT key_hash FROM rate_windows")).fetchall()
        )
        assert "example-mail-test.invalid" not in key_blob
    finally:
        db.close()


# DONE-7 ---------------------------------------------------------------------

def test_7_receipt_recipient_discipline_and_per_recipient_budget(committed_client, app):
    c = committed_client
    email = "loop.victim@example.com"
    for n in range(5):
        resp = _post(c, key=f"phase2-done7-key-{n}", email=email)
        assert resp.status_code == 201, resp.text
    # 6th submit to the same recipient address trips the per-recipient
    # receipt budget (5/hour) — the form-abuse mail loop is blocked.
    sixth = _post(c, key="phase2-done7-key-5", email=email)
    assert sixth.status_code == 429
    assert sixth.json()["code"] == "rate_limited"

    db = _fresh_db(app)
    try:
        receipts = db.execute(
            select(EmailOutbox).where(EmailOutbox.kind == "inquiry_receipt")
        ).scalars().all()
        notices = db.execute(
            select(EmailOutbox).where(EmailOutbox.kind == "inquiry_owner_notice")
        ).scalars().all()
        assert len(receipts) == 5 and len(notices) == 5
        # Receipt discipline: every receipt goes ONLY to the form's address —
        # never to Jared, never to a looked-up address.
        for intent in receipts:
            assert intent.frozen_snapshot["fields"]["destination"] == email
        for intent in notices:
            assert intent.frozen_snapshot["fields"]["destination"] == "jared@jaroaccounting.com"
        # The blocked 6th submit wrote nothing.
        assert _count(db, Inquiry, Inquiry.email == email) == 5
    finally:
        db.close()


def test_receipt_excerpt_chars_named_constant():
    """Q20: the ~200-char receipt excerpt is driven by the named constant
    RECEIPT_EXCERPT_CHARS (PII minimization — the receipt never echoes the
    full message)."""
    assert public_routes.RECEIPT_EXCERPT_CHARS == 200
    long_message = "x" * 500
    excerpt = public_routes._message_excerpt(long_message)
    assert len(excerpt) == 201  # 200 chars + ellipsis
    assert excerpt == "x" * 200 + "…"
    short = "hello"
    assert public_routes._message_excerpt(short) == "hello"  # no ellipsis
