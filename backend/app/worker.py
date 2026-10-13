"""Single background worker (E1) — Phase-0 skeleton.

One process drives three jobs; the outbox is the queue (no broker):
1. outbox cycle — claim due intents, render, submit (capture in non-prod)
2. calendar sync — one-way READ of Jared's calendar -> calendar_blocks
   (10-minute cadence; provider stubbed in Phase 0, D-28)
3. expiry sweeps — invitations -> expired; pending_verification bookings
   older than 15 min -> cancelled/verification_expired (D-04)

``run_once()`` performs a single pass of every job (what tests drive);
``main()`` loops forever with the documented cadences.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from . import calendar_worker, outbox
from . import booking as booking_lib
from .calendar_worker import POLL_CADENCE
from .db import utcnow
from .mail import MailBackend
from .models import Booking, InvitationEvent, PortalInvitation

log = logging.getLogger("jaro.worker")

INVITE_TTL = timedelta(days=7)
BOOKING_HOLD_TTL = timedelta(minutes=15)


def sweep_invitations(db) -> int:
    """Expire live invitations past expires_at; append invitation_events."""
    now = utcnow()
    rows = (
        db.execute(
            select(PortalInvitation).where(
                PortalInvitation.status.in_(("created", "emailed")),
                PortalInvitation.expires_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "expired"
        db.add(
            InvitationEvent(
                invite_id=row.id, event="expired", actor="system", at=now,
                note="expired by worker sweep",
            )
        )
    db.flush()
    return len(rows)


def sweep_booking_holds(db) -> int:
    """Release pending_verification holds older than 15 min (D-04)."""
    cutoff = utcnow() - BOOKING_HOLD_TTL
    rows = (
        db.execute(
            select(Booking).where(
                Booking.status == "pending_verification",
                Booking.created_at <= cutoff,
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for row in rows:
        row.status = "cancelled"
        row.cancelled_at = now
        row.cancel_reason = "verification_expired"
        row.revision += 1
    db.flush()
    return len(rows)


class Worker:
    def __init__(self, session_factory, mail_backend: MailBackend,
                 calendar_provider: calendar_worker.CalendarProvider | None = None):
        self._sessions = session_factory
        # E3 eligibility: the booking reminder is a time-gated intent whose
        # eligibility is re-checked at claim time — a reminder whose booking
        # was cancelled after staging never sends (belt-and-braces behind
        # the F2 cancel-suppression, which fires synchronously at cancel).
        self._outbox = outbox.OutboxWorker(
            session_factory, mail_backend,
            eligibility={"booking_reminder": booking_lib.booking_reminder_eligible(session_factory)},
        )
        self._calendar = calendar_provider
        self._last_calendar_sync = None

    def run_once(self) -> dict:
        report: dict = {}
        report["outbox"] = self._outbox.run_cycle()
        db = self._sessions()
        try:
            report["sweeps"] = {
                "invitations_expired": sweep_invitations(db),
                "booking_holds_released": sweep_booking_holds(db),
            }
            db.commit()
        finally:
            db.close()
        if self._calendar is not None and (
            self._last_calendar_sync is None
            or utcnow() - self._last_calendar_sync >= POLL_CADENCE
        ):
            db = self._sessions()
            try:
                report["calendar"] = calendar_worker.sync_calendar_blocks(db, self._calendar)
                db.commit()
                self._last_calendar_sync = utcnow()
            except Exception as exc:
                db.rollback()
                # Read failures are recorded; availability fail-closes on
                # staleness (D-31). The worker never crashes the loop.
                log.warning("calendar sync failed type=%s", type(exc).__name__)
                try:
                    err_db = self._sessions()
                    try:
                        calendar_worker.record_sync_error(err_db, calendar_worker.CALENDAR_SOURCE, exc)
                        err_db.commit()
                    finally:
                        err_db.close()
                except Exception:
                    log.exception("failed to record calendar sync error")
                report["calendar"] = {"error": type(exc).__name__}
            finally:
                db.close()
        return report

    def main(self, *, poll_seconds: int = 60) -> None:
        log.info("worker starting")
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("worker cycle failed")
            time.sleep(poll_seconds)
