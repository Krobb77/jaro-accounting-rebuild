"""Calendar worker skeleton — one-way READ of Jared's calendar.

The provider interface is provider-agnostic (Google Calendar API vs CalDAV
is the open owner question D-28); the worker materializes busy blocks into
``calendar_blocks`` on a 10-minute cadence (D-15). Tentative blocks count as
busy (D-34).

Fail-closed availability (D-31 default): when the calendar snapshot is
stale (> 30 min) or the last read failed, the availability endpoints return
503 ``calendar_unavailable`` — booking is never offered on guesswork.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import new_uuid7, utcnow
from .errors import ApiError
from .models import CalendarBlock, CalendarSyncState

POLL_CADENCE = timedelta(minutes=10)
STALE_THRESHOLD = timedelta(minutes=30)
CALENDAR_SOURCE = "jared_calendar"

# Owner-question decision (D-28; IMPLEMENTATION_PLAN §3.5 #1): the DEFAULT
# calendar provider for the one-way read into ``calendar_blocks``.
# 'caldav' is the documented alternative. The provider-agnostic
# ``CalendarProvider`` interface below is untouched — the Google wiring
# itself remains a stub behind it (Jared hasn't confirmed the provider),
# but the default is now chosen and recorded. Reversible: set
# JARO_CALENDAR_PROVIDER or edit this constant; no code depends on the
# value yet (the worker takes its provider instance from the caller).
CALENDAR_PROVIDER = os.environ.get("JARO_CALENDAR_PROVIDER", "google")

# Owner-question decision (D-31; IMPLEMENTATION_PLAN §3.5 #13): keep
# fail-closed 503 on calendar outage. This flag is the future lever for an
# hours-only degraded mode — it currently changes NOTHING. Rationale:
# stale calendar data risking a double-booked Jared is worse than a clear
# "call us" message.
CALENDAR_DEGRADED_MODE = os.environ.get("JARO_CALENDAR_DEGRADED_MODE", "false") == "true"


@dataclass(frozen=True)
class BusyBlock:
    starts_at: datetime
    ends_at: datetime
    external_id: str
    tentative: bool = False


class CalendarProvider(ABC):
    @abstractmethod
    def fetch_busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        """One-way READ: busy blocks in [start, end). Raises on read failure."""


class StubCalendarProvider(CalendarProvider):
    """Phase-0 provider stub. Returns canned blocks; ``fail_next`` simulates
    a provider outage for the fail-closed tests."""

    def __init__(self, blocks: list[BusyBlock] | None = None):
        self.blocks = list(blocks or [])
        self.fail_next = False
        self.calls = 0

    def fetch_busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("stubbed calendar provider outage")
        return [b for b in self.blocks if b.starts_at < end and b.ends_at > start]


def sync_calendar_blocks(
    db,
    provider: CalendarProvider,
    *,
    horizon_days: int = 120,
    source: str = CALENDAR_SOURCE,
) -> dict:
    """One-way sync: fetch busy blocks, upsert into calendar_blocks.

    Blocks that disappeared from the provider are pruned (they are a
    materialization, not history). The sync-state row advances on every
    successful poll — including an empty one — so the fail-closed gate can
    distinguish "empty but fresh" from "never synced". Returns a small
    report dict.
    """
    now = utcnow()
    start, end = now - timedelta(days=1), now + timedelta(days=horizon_days)
    blocks = provider.fetch_busy(start, end)
    seen: set[str] = set()
    for block in blocks:
        seen.add(block.external_id)
        existing = (
            db.execute(
                select(CalendarBlock).where(
                    CalendarBlock.source == source,
                    CalendarBlock.external_id == block.external_id,
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            db.add(
                CalendarBlock(
                    id=new_uuid7(),
                    source=source,
                    external_id=block.external_id,
                    starts_at=block.starts_at,
                    ends_at=block.ends_at,
                    tentative=block.tentative,
                    synced_at=now,
                )
            )
        else:
            existing.starts_at = block.starts_at
            existing.ends_at = block.ends_at
            existing.tentative = block.tentative
            existing.synced_at = now
    # Prune blocks the provider no longer reports.
    stale = (
        db.execute(select(CalendarBlock).where(CalendarBlock.source == source))
        .scalars()
        .all()
    )
    pruned = 0
    for row in stale:
        if row.external_id not in seen:
            db.delete(row)
            pruned += 1
    _record_sync_ok(db, source, now)
    db.flush()
    return {"upserted": len(seen), "pruned": pruned, "synced_at": now.isoformat()}


def _record_sync_ok(db, source: str, now) -> None:
    row = db.get(CalendarSyncState, source)
    if row is None:
        row = CalendarSyncState(source=source)
        db.add(row)
    row.last_ok_at = now
    row.last_error_at = None
    row.last_error = None


def record_sync_error(db, source: str, exc: Exception) -> None:
    """Observability for the read-error path (D-15): the gate itself stays
    staleness-based, but the last failure is inspectable."""
    now = utcnow()
    row = db.get(CalendarSyncState, source)
    if row is None:
        row = CalendarSyncState(source=source)
        db.add(row)
    row.last_error_at = now
    row.last_error = type(exc).__name__[:256]
    db.flush()


def snapshot_age(db, *, source: str = CALENDAR_SOURCE):
    """Age of the last successful calendar sync, or None when never synced."""
    row = db.get(CalendarSyncState, source)
    if row is None or row.last_ok_at is None:
        return None
    return utcnow() - row.last_ok_at


def require_fresh_calendar(db, *, source: str = CALENDAR_SOURCE) -> None:
    """Fail-closed gate for the availability endpoints (D-31)."""
    age = snapshot_age(db, source=source)
    if age is None or age > STALE_THRESHOLD:
        raise ApiError("calendar_unavailable")
