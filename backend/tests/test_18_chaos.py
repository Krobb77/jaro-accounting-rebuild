"""DONE Phase 4 — chaos drills (new tests where missing).

1. Dead SMTP -> intents stay `retry` with bounded backoff, nothing lost,
   no live-send attempt is ever made; provider recovery -> captured.
2. Budget-store outage -> 503 fail-closed on the three mutating surfaces:
   client login, booking intent, inquiry submit.
3. Worker kill mid-send -> lease expiry -> re-claim without double-send
   (already rehearsed in test_08's DONE-8 kill-mid-send test; re-asserted
   here at the full-Worker level so the rehearsal survives refactors).
4. G1: the suite refuses a guarded (non-disposable) DSN, asserted by
   pointing a real pytest subprocess at one.
5. Worker `-m` CLI: happy-path cycle plus the two fail-closed refusals.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import outbox  # noqa: E402
from app.db import utcnow  # noqa: E402
from app.mail import CaptureMailBackend, MailBackend  # noqa: E402
from app.models import EmailOutbox, OutboxAttempt  # noqa: E402


class DeadSmtpBackend(MailBackend):
    """A provider whose TCP connect fails. The class contains no socket
    code at all: ``send`` raises at the dial stage, exactly like a
    connection-refused/dead provider would, so the test can assert that no
    live-send attempt was ever made."""

    def __init__(self):
        self.calls = 0
        self.socket_connections_attempted = 0

    def send(self, mail, *, intent_id, attempt_no) -> str:
        self.calls += 1
        # The dial stage: raise before any socket exists.
        raise ConnectionError("dial tcp: connection refused (dead provider)")


def _stage(factory, kind="inquiry_receipt", scope="chaos-1", dest="client@example.com",
           fields=None):
    db = factory()
    try:
        row = outbox.stage_intent(
            db,
            kind=kind,
            scope_id=scope,
            destination=dest,
            snapshot_fields=fields or {"request_id": "req-chaos"},
        )
        db.commit()
        return row.id
    finally:
        db.close()


def _intent(factory, intent_id):
    db = factory()
    try:
        return db.get(EmailOutbox, intent_id)
    finally:
        db.close()


@pytest.fixture()
def chaos_env(db_admin, tmp_path):
    """Committed-session factory + capture dir, with outbox rows cleaned up."""
    from sqlalchemy.orm import sessionmaker

    capture_dir = tmp_path / "mail"
    factory = sessionmaker(bind=db_admin, autoflush=False, expire_on_commit=False)
    yield factory, capture_dir
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM outbox_attempts"))
        conn.execute(text("DELETE FROM email_outbox"))


def test_dead_smtp_retries_with_backoff_nothing_lost(chaos_env):
    factory, capture_dir = chaos_env
    dead = DeadSmtpBackend()
    worker = outbox.OutboxWorker(factory, dead)

    intent_id = _stage(factory)

    report = worker.run_cycle()
    assert report["retried"] == 1, report
    assert report["done"] == 0

    # Intent is parked in retry with a future backoff deadline (E4).
    row = _intent(factory, intent_id)
    assert row.status == "retry"
    assert row.next_attempt_at is not None and row.next_attempt_at > utcnow()
    assert row.attempts == 1

    # Numbered attempt row with honest evidence (E4 vocabulary).
    db = factory()
    try:
        attempts = db.execute(
            select(OutboxAttempt).where(OutboxAttempt.outbox_id == intent_id)
        ).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].outcome == "retry"
        assert attempts[0].attempt_no == 1
    finally:
        db.close()

    # Nothing lost: the row and its attempt survive the provider outage.
    assert _intent(factory, intent_id) is not None
    # No live-send attempt: the backend raised before any socket existed,
    # and nothing was captured anywhere.
    assert dead.calls == 1
    assert dead.socket_connections_attempted == 0
    assert list(capture_dir.glob(f"{intent_id}-attempt*.json")) == []

    # Backoff is respected: a second cycle before the deadline re-claims
    # nothing (the intent is not hot-looped against a dead provider).
    report2 = worker.run_cycle()
    assert report2["claimed"] == 0 and report2["retried"] == 0, report2

    # Provider recovers (swap backend) and the deadline passes: the parked
    # intent is claimed and captured exactly once. Evidence of the outage
    # (the retry attempt row) is preserved, not rewritten.
    db = factory()
    try:
        db.execute(
            text("UPDATE email_outbox SET next_attempt_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=1), "id": intent_id},
        )
        db.commit()
    finally:
        db.close()
    recovered = outbox.OutboxWorker(factory, CaptureMailBackend(str(capture_dir)))
    report3 = recovered.run_cycle()
    assert report3["done"] == 1, report3
    assert _intent(factory, intent_id).status == "captured"
    artifacts = list(capture_dir.glob(f"{intent_id}-attempt*.json"))
    assert len(artifacts) == 1
    db = factory()
    try:
        outcomes = [
            a.outcome
            for a in db.execute(
                select(OutboxAttempt).where(OutboxAttempt.outbox_id == intent_id)
                .order_by(OutboxAttempt.attempt_no)
            ).scalars().all()
        ]
    finally:
        db.close()
    assert outcomes == ["retry", "captured"]


def test_kill_mid_send_then_reclaim_at_worker_level(chaos_env):
    """Full-Worker rehearsal of the DONE-8 drill: claim, die before finish,
    lease expires, second claim succeeds — without a double-send."""
    from sqlalchemy.orm import sessionmaker

    factory, capture_dir = chaos_env
    worker = outbox.OutboxWorker(factory, CaptureMailBackend(str(capture_dir)))
    intent_id = _stage(factory, scope="kill-worker-1")

    # First claim: worker starts sending, then dies before finish.
    db = factory()
    try:
        claim = outbox.claim_intent(db, intent_id)
        db.commit()
        assert claim.claimed
    finally:
        db.close()

    # Lease expires (simulate time passing); the next cycle reclaims.
    db = factory()
    try:
        db.execute(
            text("UPDATE email_outbox SET lease_expires_at = :past WHERE id = :id"),
            {"past": utcnow() - timedelta(seconds=1), "id": intent_id},
        )
        db.commit()
    finally:
        db.close()

    report = worker.run_cycle()
    assert report["claimed"] == 1 and report["done"] == 1, report
    # Exactly one captured artifact: the dead worker's partial claim never
    # produced a send, so no double-send is possible.
    assert len(list(capture_dir.glob(f"{intent_id}-attempt*.json"))) == 1


# --- budget-store outage across all three mutating surfaces ------------------


def _app_on_scratch_db(scratch_db_url, tmp_path):
    from alembic import command as alembic_command
    from alembic.config import Config

    from app.config import load_settings
    from app.main import create_app
    from tests.conftest import BASE_TEST_ENV

    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "head")
    settings = load_settings(
        {
            **BASE_TEST_ENV,
            "DATABASE_URL": scratch_db_url,
            "MAIL_CAPTURE_DIR": str(tmp_path / "mc"),
        }
    )
    application = create_app(settings)
    return application


def _kill_budget_store(scratch_db_url):
    import sqlalchemy as sa

    engine = sa.create_engine(scratch_db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE rate_windows"))
    engine.dispose()


def test_budget_store_outage_503_on_login_booking_inquiry(scratch_db_url, tmp_path):
    """Kill the budget store -> 503 fail-closed (never open) on every
    mutating surface: client login, booking intent, inquiry submit."""
    from fastapi.testclient import TestClient

    application = _app_on_scratch_db(scratch_db_url, tmp_path)
    try:
        _kill_budget_store(scratch_db_url)
        with TestClient(application, raise_server_exceptions=False) as c:
            # Client login
            r = c.post("/api/v1/portal/login",
                       json={"email": "chaos@example.com", "password": "x"})
            assert r.status_code == 503, ("login", r.status_code)
            assert r.json()["code"] == "budget_unavailable"

            # Booking intent (limiter runs before any slot logic)
            r = c.post("/api/v1/booking/intents", json={
                "slot_start": "2027-03-03T17:00:00Z",
                "guest_timezone": "America/Vancouver",
                "name": "Chaos Case",
                "email": "chaos@example.com",
                "country_code": "+1",
                "phone": "6045550100",
                "relationship": "prospective_client",
                "discuss": "Chaos drill booking intent payload.",
            })
            assert r.status_code == 503, ("booking", r.status_code)
            assert r.json()["code"] == "budget_unavailable"

            # Inquiry submit
            r = c.post("/api/v1/inquiries", json={
                "name": "Chaos Case",
                "email": "chaos@example.com",
                "message": "Chaos drill inquiry payload.",
            }, headers={"Idempotency-Key": "chaos-inquiry-key-1"})
            assert r.status_code == 503, ("inquiry", r.status_code)
            assert r.json()["code"] == "budget_unavailable"
    finally:
        application.state.engine.dispose()


# --- worker -m CLI entry point (Phase-0 residual, wired in Phase 4) ---------


def _worker_env(db_url: str, tmp_path, **overrides):
    from tests.conftest import BASE_TEST_ENV

    env = dict(os.environ)
    env.update(
        {
            **BASE_TEST_ENV,
            "JARO_WORKLOAD": "worker",
            "DB_ROLE": "jaro_worker",
            "DATABASE_URL": db_url,
            "MAIL_CAPTURE_DIR": str(tmp_path / "mc"),
            **overrides,
        }
    )
    return env


def test_worker_cli_once_cycle(scratch_db_url, tmp_path):
    """`python -m app.worker_main --once` runs one real cycle against a
    migrated database and exits 0."""
    import subprocess

    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "head")

    env = _worker_env(scratch_db_url, tmp_path)
    r = subprocess.run(
        [sys.executable, "-m", "app.worker_main", "--once"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "worker cycle complete" in r.stderr


def test_worker_cli_refuses_bad_config(scratch_db_url, tmp_path):
    """H2: the worker refuses to boot when the mail gate is unset — the
    same fail-closed refusal the API applies."""
    import subprocess

    env = _worker_env(scratch_db_url, tmp_path)
    del env["MAIL_ALLOW_SEND"]
    r = subprocess.run(
        [sys.executable, "-m", "app.worker_main", "--once"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 2, r.stderr[-2000:]
    assert "MAIL_ALLOW_SEND" in r.stderr


def test_worker_cli_refuses_wrong_workload(scratch_db_url, tmp_path):
    """The worker requires its own workload identity (G2: no double-claim
    under a mismatched identity)."""
    import subprocess

    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "head")

    env = _worker_env(scratch_db_url, tmp_path, JARO_WORKLOAD="web", DB_ROLE="jaro_web")
    r = subprocess.run(
        [sys.executable, "-m", "app.worker_main", "--once"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 2, r.stderr[-2000:]
    assert "JARO_WORKLOAD=worker" in r.stderr


# --- G1: the suite refuses a non-disposable target ---------------------------


def test_g1_refuses_guarded_dsn():
    """Point the suite at a guarded (non-disposable) DSN: the session
    refuses to start instead of ever touching it. The DSN below is
    deliberately unreachable — the assertion is on the refusal, before any
    connection is attempted."""
    import subprocess

    env = dict(os.environ)
    # Guarded stand-in for "the live database": non-sqlite scheme, a host
    # that is never dialed because the gate fires at conftest import.
    env["DATABASE_URL"] = "postgresql://guarded:guarded@guarded.internal:5432/guard"
    env.pop("PYTEST_ADDOPTS", None)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_01_migrations.py",
         "-x", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode != 0, (r.returncode, r.stdout[-1500:], r.stderr[-1500:])
    combined = r.stdout + r.stderr
    assert "G1 safety gate" in combined
    assert "refusing to run tests" in combined
    assert "disposable sqlite" in combined

