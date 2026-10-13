"""DONE 14 — calendar worker (provider stubbed).

- Busy blocks materialize into calendar_blocks.
- Read failure -> availability endpoints 503 (fail-closed) once staleness
  exceeds 30 min.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from app import calendar_worker  # noqa: E402
from app.calendar_worker import BusyBlock, StubCalendarProvider  # noqa: E402
from app.db import utcnow  # noqa: E402
from app.models import CalendarBlock  # noqa: E402


def _blocks():
    now = utcnow()
    return [
        BusyBlock(now + timedelta(hours=1), now + timedelta(hours=2), "evt-1"),
        BusyBlock(now + timedelta(hours=3), now + timedelta(hours=4), "evt-2", tentative=True),
    ]


def _sync_committed(db_admin, provider):
    """Sync calendar blocks in a committed session.

    The booking availability/slots routes carry the A5 rate budget, whose
    check-and-increment runs on a separate connection; syncing through the
    `db` fixture's open (uncommitted) transaction would hold SQLite's write
    lock and turn the budget check into a spurious 503 (the Phase-2
    `committed_client` lesson — DECISIONS-backend.md Phase-2 item 11).
    """
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_admin, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        calendar_worker.sync_calendar_blocks(s, provider)
        s.commit()
    finally:
        s.close()


def test_busy_blocks_materialize(db):
    provider = StubCalendarProvider(_blocks())
    report = calendar_worker.sync_calendar_blocks(db, provider)
    assert report["upserted"] == 2
    rows = db.query(CalendarBlock).all()
    assert len(rows) == 2
    assert {r.external_id for r in rows} == {"evt-1", "evt-2"}
    assert sum(1 for r in rows if r.tentative) == 1

    # Re-sync with one block gone prunes it (materialization, not history).
    provider.blocks = provider.blocks[:1]
    report = calendar_worker.sync_calendar_blocks(db, provider)
    assert report["pruned"] == 1
    assert db.query(CalendarBlock).count() == 1


def test_availability_503_when_never_synced(client, db):
    r = client.get("/api/v1/booking/availability?year=2026&month=10&tz=America/Vancouver")
    assert r.status_code == 503
    assert r.json()["code"] == "calendar_unavailable"
    r = client.get("/api/v1/booking/slots?date=2026-10-10&tz=America/Vancouver")
    assert r.status_code == 503


def test_availability_gate_passes_after_sync(client, db_admin):
    provider = StubCalendarProvider(_blocks())
    _sync_committed(db_admin, provider)
    # The gate passes (fresh snapshot); the Phase-3 endpoints are real now.
    r = client.get("/api/v1/booking/availability?year=2026&month=10&tz=America/Vancouver")
    assert r.status_code == 200
    body = r.json()
    assert body["year"] == 2026 and body["month"] == 10
    assert isinstance(body["days"], list) and body["days"]
    assert body["as_of"] is not None
    assert body["guest_timezone_echo"] == "America/Vancouver"


def test_read_failure_stale_snapshot_503(client, db_admin):
    provider = StubCalendarProvider(_blocks())
    _sync_committed(db_admin, provider)
    # Age the sync-state row past the 30-min staleness threshold...
    with db_admin.begin() as conn:
        conn.execute(
            text("UPDATE calendar_sync_state SET last_ok_at = :past"),
            {"past": utcnow() - timedelta(minutes=31)},
        )
    # ...and fail the provider read: no fresh data is possible, and the
    # failure is recorded (observability) without refreshing the snapshot.
    provider.fail_next = True
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_admin, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        calendar_worker.sync_calendar_blocks(s, provider)
    except ConnectionError as exc:
        s.rollback()
        calendar_worker.record_sync_error(s, calendar_worker.CALENDAR_SOURCE, exc)
        s.commit()
    finally:
        s.close()
    r = client.get("/api/v1/booking/availability?year=2026&month=10&tz=America/Vancouver")
    assert r.status_code == 503
    assert r.json()["code"] == "calendar_unavailable"


def test_worker_run_once_includes_calendar(app, db_admin, tmp_path):
    """The Worker skeleton wires the calendar sync into run_once."""
    from sqlalchemy.orm import sessionmaker
    from app.mail import CaptureMailBackend
    from app.worker import Worker

    provider = StubCalendarProvider(_blocks())
    backend = CaptureMailBackend(str(tmp_path / "mc"))
    factory = sessionmaker(bind=db_admin, autoflush=False, expire_on_commit=False)
    worker = Worker(factory, backend, calendar_provider=provider)
    report = worker.run_once()
    assert report["calendar"]["upserted"] == 2
    assert report["sweeps"]["invitations_expired"] == 0
    # cleanup
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM calendar_blocks"))


def test_owner_decisions_calendar_provider_and_degraded_mode():
    """Q1/Q13: CALENDAR_PROVIDER defaults to 'google' (caldav documented
    alternative); CALENDAR_DEGRADED_MODE defaults False and changes
    nothing — fail-closed 503 stays the outage posture."""
    from app import calendar_worker

    assert calendar_worker.CALENDAR_PROVIDER == "google"
    assert calendar_worker.CALENDAR_DEGRADED_MODE is False
