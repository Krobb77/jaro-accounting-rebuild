"""Phase-3 booking domain service — availability, intents, confirm, manage.

Custom module (no shared pattern fit — recorded in
backend/DECISIONS-backend.md): no existing module owns the booking
lifecycle. Availability math, hold/intent semantics, slot advisory locks,
manage-token auth, and ICS generation are booking-specific and *compose*
the shared primitives (outbox staging + F2 suppression, code digest
storage, CSPRNG tokens, advisory locks, rate budgets) rather than
duplicating them.

Conventions:
- A4: every service function takes ``db`` and performs writes inside the
  caller's transaction; services never commit.
- A6: nothing here logs names, emails, phones, discussion text, or code
  values. Target correlation uses the HMAC-style digest from ``codes``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from . import advisory, codes, outbox, refnums, tokens
from .db import constant_time_compare, new_uuid7, sha256_hex, utcnow
from .errors import ApiError
from .models import AvailabilityRule, Booking, CalendarBlock, EventType, SmsCode

# Canonical constants (IMPLEMENTATION_PLAN D-47–D-52; §3.5 #7–13 owner
# questions — defaults hold until Jared answers).
FIRM_TIMEZONE = "America/Vancouver"
EVENT_CODE = "free-30-min-consultation"
SLOT_MINUTES = 30
MIN_NOTICE = timedelta(hours=24)
BOOKING_HORIZON = timedelta(days=90)
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_CODE_SENDS = 5
INTENT_HOLD_TTL_MINUTES = 15  # mirrors worker.BOOKING_HOLD_TTL (D-04)
CANCEL_CUTOFF = timedelta(hours=24)
RESCHEDULE_CUTOFF = timedelta(hours=24)
MANAGE_TOKEN_LIFETIME = timedelta(days=90)  # post appointment end (D-48)
REMINDER_LEAD = timedelta(hours=24)  # D-49: not_before = slot_start - 24h
NO_SHOW_HOLD_COUNT = 2  # D-22 (threshold pending Jared, §3.5 #8)
NO_SHOW_WINDOW = timedelta(days=365)
CALENDAR_SOURCE = "jared_calendar"

OWNER_PHONE_DISPLAY = "1-(778)-227-4891"
OWNER_EMAIL = "jared@jaroaccounting.com"
SITE_ORIGIN = "https://jaroaccounting.com"

ACTIVE_SLOT_STATUSES = ("pending_verification", "confirmed")

SLOT_DELTA = timedelta(minutes=SLOT_MINUTES)


# --- timezones --------------------------------------------------------------

@lru_cache(maxsize=128)
def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "timezone", "message": "Please choose a valid timezone."}],
        )


def firm_zone() -> ZoneInfo:
    return zone(FIRM_TIMEZONE)


def parse_slot_start(value: str) -> datetime:
    """Parse an ISO-8601 instant; naive input is rejected (slot_start is a
    timestamptz — an instant, never a wall time)."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "slot_start", "message": "Please choose a time from the available slots."}],
        )
    if dt.tzinfo is None:
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "slot_start", "message": "Please choose a time from the available slots."}],
        )
    return dt.astimezone(timezone.utc)


# --- BC stat holidays (D-07 §3.5 #7 default; owner confirms the list) --------

def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian computus — stdlib has no Easter function; this is
    # a pure date-math helper local to the booking module (no shared
    # pattern exists for liturgical dates).
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth ``weekday`` (Mon=0) of the month, e.g. Family Day = 3rd Monday."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _victoria_day(year: int) -> date:
    # Last Monday preceding May 25 (falls May 18–24). This is NOT simply
    # the 3rd Monday of May: the two differ when May 25 is Tue/Wed/Thu
    # (e.g. 2027 → May 24, not May 17; 2023 → May 22, not May 15).
    may25 = date(year, 5, 25)
    monday = may25 - timedelta(days=may25.weekday())
    if monday == may25:  # May 25 is itself a Monday → the Monday before it
        monday -= timedelta(days=7)
    return monday


def bc_stat_holidays(year: int) -> set[date]:
    """BC statutory holidays — firm-local dates the calendar renders
    unavailable. Default list (BOOKING §2.1); Jared confirms observance
    (IMPLEMENTATION_PLAN §3.5 #7).

    The list matches the BC Employment Standards Act exactly: Christmas Eve
    and Boxing Day are NOT stats — the firm is OPEN those days, so they are
    deliberately absent here.
    """
    easter = _easter_sunday(year)
    return {
        date(year, 1, 1),                      # New Year's Day
        _nth_weekday(year, 2, 0, 3),            # Family Day (3rd Mon Feb)
        easter - timedelta(days=2),             # Good Friday
        _victoria_day(year),                      # Victoria Day (last Mon before May 25)
        date(year, 7, 1),                      # Canada Day
        _nth_weekday(year, 8, 0, 1),            # BC Day (1st Mon Aug)
        _nth_weekday(year, 9, 0, 1),            # Labour Day (1st Mon Sep)
        _nth_weekday(year, 10, 0, 2),           # Thanksgiving (2nd Mon Oct)
        date(year, 11, 11),                    # Remembrance Day
        date(year, 12, 25),                    # Christmas Day
    }


# --- guest field normalization ----------------------------------------------

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
)


def valid_email(value: str) -> bool:
    if not value or len(value) > 254 or len(value) < 3 or value.count("@") != 1:
        return False
    return _EMAIL_RE.match(value) is not None


def normalize_phone(country_code: str, phone: str) -> str:
    """Normalize to E.164 (country code default +1).

    Custom helper (no shared pattern fit — the contact schema has no phone
    and the venv ships no libphonenumber): digits only, country code
    forced to start with '+', national part >= 7 digits, total <= 15.
    Raises ApiError(validation_failed) with a per-field message."""
    cc = (country_code or "+1").strip().replace(" ", "")
    if not cc.startswith("+"):
        cc = "+" + cc
    if not re.fullmatch(r"\+\d{1,4}", cc):
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "country_code", "message": "Please choose a country code."}],
        )
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 7 or len(cc[1:] + digits) > 15:
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "phone", "message": "Please enter a valid phone number (at least 7 digits)."}],
        )
    return cc + digits


def mask_email(email: str) -> str:
    """``j•••@example.com`` — A1/A6: responses echo a masked address."""
    local, _, domain = email.partition("@")
    if not domain:
        return "•••"
    shown = local[:1] if local else ""
    return f"{shown}•••@{domain}"


def first_name(name: str) -> str:
    return (name or "").split()[0] if name and name.split() else "there"


# --- availability ------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    start_utc: datetime
    end_utc: datetime


def _active_rules(db) -> list[AvailabilityRule]:
    today = utcnow().astimezone(firm_zone()).date()
    rows = (
        db.execute(select(AvailabilityRule).where(AvailabilityRule.event_code == EVENT_CODE))
        .scalars()
        .all()
    )
    return [
        r
        for r in rows
        if (r.valid_from is None or r.valid_from <= today)
        and (r.valid_until is None or r.valid_until >= today)
    ]


def _busy_starts(db, day: date) -> set[datetime]:
    """UTC slot starts already held (pending_verification/confirmed)."""
    firm = firm_zone()
    day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=firm)
    day_end = day_start + timedelta(days=1)
    rows = (
        db.execute(
            select(Booking.slot_start).where(
                Booking.slot_start >= day_start.astimezone(timezone.utc),
                Booking.slot_start < day_end.astimezone(timezone.utc),
                Booking.status.in_(ACTIVE_SLOT_STATUSES),
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def _calendar_blocks(db, day: date) -> list[CalendarBlock]:
    """Busy blocks overlapping the firm-local day. Any overlap — even
    partial — marks a slot unavailable (D-34 conservative)."""
    firm = firm_zone()
    day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=firm).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    return (
        db.execute(
            select(CalendarBlock).where(
                CalendarBlock.source == CALENDAR_SOURCE,
                CalendarBlock.starts_at < day_end,
                CalendarBlock.ends_at > day_start,
            )
        )
        .scalars()
        .all()
    )


def generate_slots(db, day: date, *, now: datetime | None = None) -> list[Slot]:
    """Server-side slot generation (BOOKING §2.1): working hours → minus
    MIN_NOTICE → minus horizon → minus held bookings → minus calendar
    blocks (any overlap, tentative counts as busy)."""
    now = now or utcnow()
    firm = firm_zone()
    if day in bc_stat_holidays(day.year):
        return []
    rules = [r for r in _active_rules(db) if r.weekday == day.weekday()]
    if not rules:
        return []
    candidates: list[Slot] = []
    for rule in rules:
        t = datetime.combine(day, rule.start_local).replace(tzinfo=firm)
        end_local = datetime.combine(day, rule.end_local).replace(tzinfo=firm)
        while t + SLOT_DELTA <= end_local:
            start_utc = t.astimezone(timezone.utc)
            if start_utc >= now + MIN_NOTICE and start_utc <= now + BOOKING_HORIZON:
                candidates.append(Slot(start_utc, start_utc + SLOT_DELTA))
            t += SLOT_DELTA
    if not candidates:
        return []
    busy = _busy_starts(db, day)
    blocks = _calendar_blocks(db, day)
    free: list[Slot] = []
    for slot in candidates:
        if slot.start_utc in busy:
            continue
        if any(b.starts_at < slot.end_utc and b.ends_at > slot.start_utc for b in blocks):
            continue
        free.append(slot)
    return free


def slot_is_free(db, start_utc: datetime, *, now: datetime | None = None,
                 exclude_booking_id: str | None = None) -> bool:
    """Fresh-clock single-slot check (confirm + reschedule path)."""
    now = now or utcnow()
    firm = firm_zone()
    day = start_utc.astimezone(firm).date()
    if start_utc < now + MIN_NOTICE or start_utc > now + BOOKING_HORIZON:
        return False
    if day in bc_stat_holidays(day.year):
        return False
    local_start = start_utc.astimezone(firm)
    local_end = local_start + SLOT_DELTA
    rules = [r for r in _active_rules(db) if r.weekday == day.weekday()]
    if not any(
        datetime.combine(day, r.start_local).replace(tzinfo=firm) <= local_start
        and local_end <= datetime.combine(day, r.end_local).replace(tzinfo=firm)
        for r in rules
    ):
        return False
    q = select(Booking.id).where(
        Booking.slot_start == start_utc,
        Booking.status.in_(ACTIVE_SLOT_STATUSES),
    )
    if exclude_booking_id is not None:
        q = q.where(Booking.id != exclude_booking_id)
    if db.execute(q).first() is not None:
        return False
    return not any(
        b.starts_at < start_utc + SLOT_DELTA and b.ends_at > start_utc
        for b in _calendar_blocks(db, day)
    )


def available_days(db, year: int, month: int) -> list[dict]:
    """Available-days-only month data (firm-local days with >= 1 free
    slot). The calendar shrinks the 409 path to true concurrency."""
    import calendar as _cal

    now = utcnow()
    days = []
    for day_no in range(1, _cal.monthrange(year, month)[1] + 1):
        day = date(year, month, day_no)
        firm_today = now.astimezone(firm_zone()).date()
        if day < firm_today or day > (now + BOOKING_HORIZON).astimezone(firm_zone()).date():
            days.append({"date": day.isoformat(), "available": False})
            continue
        days.append(
            {"date": day.isoformat(), "available": bool(generate_slots(db, day, now=now))}
        )
    return days


def slot_label(start_utc: datetime, end_utc: datetime, tz_name: str) -> dict:
    """Guest-zone slot presentation + natural-language aria-label."""
    tz = zone(tz_name)
    s = start_utc.astimezone(tz)
    e = end_utc.astimezone(tz)

    def _fmt(d: datetime) -> str:
        hour = d.strftime("%I").lstrip("0") or "0"
        return f"{hour}:{d.strftime('%M')} {d.strftime('%p')}"

    label = f"{_fmt(s)} – {_fmt(e)}"
    # DATE_LONG per EMAIL_TEMPLATES §3.2, e.g. "Tuesday, October 13, 2026".
    date_long = s.strftime("%A, %B %-d, %Y")
    # aria-label per BOOKING §1 (CHANGE 2): natural language, abbreviated
    # date, e.g. "Select 9:00 AM on Tue, Oct 6, 2026".
    aria = f"Select {_fmt(s)} on {s.strftime('%a, %b %-d, %Y')}"
    firm = firm_zone()
    fs, fe = start_utc.astimezone(firm), end_utc.astimezone(firm)
    firm_label = f"{_fmt(fs)} – {_fmt(fe)}"
    return {
        "start": start_utc.isoformat(),
        "end": end_utc.isoformat(),
        "label": label,
        "date_long": date_long,
        "aria_label": aria,
        "firm_label": firm_label,
        "firm_date_long": fs.strftime("%A, %B %-d, %Y"),
    }


def calendar_as_of(db) -> str | None:
    """ISO-8601 of the last successful calendar sync (the staleness marker
    the availability responses carry)."""
    from .models import CalendarSyncState

    row = db.get(CalendarSyncState, CALENDAR_SOURCE)
    if row is None or row.last_ok_at is None:
        return None
    return row.last_ok_at.isoformat()


def get_config(db) -> dict:
    event = db.get(EventType, EVENT_CODE)
    year = utcnow().astimezone(firm_zone()).year
    holidays = sorted({d.isoformat() for y in (year, year + 1) for d in bc_stat_holidays(y)})
    return {
        "firm_timezone": FIRM_TIMEZONE,
        "working_hours": {
            "mon_fri": {"open": "09:00", "close": "17:00"},
            "sat": {"open": "10:00", "close": "15:00"},
        },
        # D-32 default held ("Phone Call" wording); Jared confirms (§3.5 #4).
        "event_type": event.label if event else "Free 30-Min Consultation — Phone Call",
        "min_notice_hours": int(MIN_NOTICE.total_seconds() // 3600),
        "horizon_days": BOOKING_HORIZON.days,
        "holidays": holidays,
        "verification": {
            "code_ttl_minutes": CODE_TTL_MINUTES,
            "max_attempts": CODE_MAX_ATTEMPTS,
            "resend_cooldown_seconds": RESEND_COOLDOWN_SECONDS,
            "max_sends": MAX_CODE_SENDS,
        },
        "cutoffs": {"cancel_hours": 24, "reschedule_hours": 24},
    }


# --- verification codes (intent-scoped) ---------------------------------------

def issue_booking_code(db, *, booking_id: str, email: str,
                       enforce_cooldown: bool = True) -> tuple[str, int]:
    """Issue a fresh 6-digit code for the intent; returns (raw_code,
    sends_used). Supersedes prior live codes for the same mailbox (F5:
    resend never leaves two live codes)."""
    sends_used = (
        db.execute(
            select(func.count())
            .select_from(SmsCode)
            .where(SmsCode.scope == "booking_verify", SmsCode.booking_id == booking_id)
        )
        .scalar()
    )
    if sends_used >= MAX_CODE_SENDS:
        raise ApiError("code_sends_exhausted")
    if enforce_cooldown:
        last = (
            db.execute(
                select(SmsCode)
                .where(SmsCode.scope == "booking_verify", SmsCode.booking_id == booking_id)
                .order_by(SmsCode.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if last is not None:
            elapsed = (utcnow() - last.created_at).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                raise ApiError(
                    "resend_cooldown",
                    headers={"Retry-After": str(int(RESEND_COOLDOWN_SECONDS - elapsed))},
                )
    raw = codes.issue_code(
        db, scope="booking_verify", target=email, booking_id=booking_id,
        ttl_minutes=CODE_TTL_MINUTES, max_attempts=CODE_MAX_ATTEMPTS,
    )
    return raw, sends_used + 1


def stage_code_email(db, *, booking_id: str, email: str, code: str,
                     first: str, send_no: int) -> None:
    """Queue the 6-digit code email (kind ``booking_code``). The dedup
    scope includes the send number so each resend is a distinct intent —
    a resend must actually re-send, not F3-dedupe to the earlier one."""
    outbox.stage_intent(
        db,
        kind="booking_code",
        scope_id=f"{booking_id}:code:{send_no}",
        destination=email,
        snapshot_fields={
            "first_name": first,
            "code": code,
            "expires_minutes": CODE_TTL_MINUTES,
        },
    )
@dataclass
class VerifyOutcome:
    ok: bool
    code: str  # ok | invalid_code | code_expired | code_attempts_exceeded


def verify_booking_code(db, *, booking_id: str, email: str, code: str,
                        lock_backend: advisory.AdvisoryLockBackend) -> VerifyOutcome:
    """Intent-scoped verification under an advisory lock (F5 atomic
    consume).

    Why not ``codes.verify_code``: the shared helper is *target*-scoped
    (one live code per mailbox), which would let two concurrent intents for
    the same email cross-verify each other's codes. Booking verification
    must be *intent*-scoped — only the newest live code for THIS booking
    row verifies. The digest storage, advisory-lock atomicity, and
    5-attempts-then-dead rules are the shared mechanism, reused here.
    """
    now = utcnow()
    key = advisory.advisory_key("booking-verify", booking_id)
    with lock_backend.acquire(db, key):
        row = (
            db.execute(
                select(SmsCode)
                .where(SmsCode.scope == "booking_verify", SmsCode.booking_id == booking_id)
                .order_by(SmsCode.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return VerifyOutcome(False, "invalid_code")
        if row.attempts >= row.max_attempts:
            return VerifyOutcome(False, "code_attempts_exceeded")
        if row.expires_at <= now:
            row.consumed_at = now
            db.flush()
            return VerifyOutcome(False, "code_expired")
        if row.consumed_at is not None:
            # Already consumed (verified or superseded): replay rejected.
            return VerifyOutcome(False, "invalid_code")
        if not constant_time_compare(codes.code_digest(code.strip()), row.code_hash):
            row.attempts += 1
            if row.attempts >= row.max_attempts:
                row.consumed_at = now
                db.flush()
                return VerifyOutcome(False, "code_attempts_exceeded")
            db.flush()
            return VerifyOutcome(False, "invalid_code")
        row.consumed_at = now
        db.flush()
        return VerifyOutcome(True, "ok")


# --- intents ------------------------------------------------------------------

def no_show_count(db, *, email: str) -> int:
    cutoff = utcnow() - NO_SHOW_WINDOW
    return (
        db.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                func.lower(Booking.guest_email) == email.strip().lower(),
                Booking.status == "no_show",
                Booking.no_show_at.is_not(None),
                Booking.no_show_at >= cutoff,
            )
        )
        .scalar()
    )


def create_intent(db, *, slot_start: datetime, guest_timezone: str, name: str,
                  email: str, phone_e164: str, relationship: str, discuss: str,
                  lock_backend: advisory.AdvisoryLockBackend, request_id: str,
                  supersedes_intent_id: str | None = None) -> dict:
    """Create the pending_verification booking row (D-04: the intent IS the
    held slot), issue the code, and queue the code email — all in the
    caller's transaction (A4).

    ``supersedes_intent_id``: the widget passes its previous intent when
    the guest re-submits after a review Change link (email/details/slot
    edit). The old pending intent is atomically expired so the new one
    can take the same slot (email fix) or free the old hold (slot
    change) — otherwise the guest's own previous hold would 409 their
    re-submit and strand the slot until the 15-min sweep. The old intent
    must still be pending; anything else is a 410. No shared "replace"
    primitive existed (intents are immutable by design), so this narrow
    supersede lives here with its audit trail.
    """
    zone(guest_timezone)  # 422 on unknown zones
    email = email.strip().lower()
    if no_show_count(db, email=email) >= NO_SHOW_HOLD_COUNT:
        raise ApiError("booking_hold")
    if supersedes_intent_id:
        old = db.get(Booking, supersedes_intent_id)
        if old is None or old.status != "pending_verification":
            raise ApiError("intent_expired")
        now = utcnow()
        old.status = "cancelled"
        old.cancelled_at = now  # ck_bookings_cancelled_at
        old.cancel_reason = "superseded"
        old.updated_at = now
        db.flush()
    if not slot_is_free(db, slot_start):
        raise ApiError("slot_taken")
    reference = refnums.new_reference_number()
    now = utcnow()
    row = Booking(
        id=new_uuid7(),
        reference_number=reference,
        event_code=EVENT_CODE,
        slot_start=slot_start,
        slot_end=slot_start + SLOT_DELTA,
        guest_timezone=guest_timezone,
        guest_name=name.strip(),
        guest_email=email,
        guest_phone_e164=phone_e164,
        guest_relationship=relationship,
        discuss_notes=discuss.strip(),
        staff_id="jared",
        status="pending_verification",
        revision=1,
        payload_hash=sha256_hex(
            "jaro-booking-payload-v1",
            slot_start.isoformat(), guest_timezone, email, phone_e164, discuss,
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    raw_code, sends_used = issue_booking_code(
        db, booking_id=row.id, email=email, enforce_cooldown=False
    )
    stage_code_email(
        db, booking_id=row.id, email=email, code=raw_code,
        first=first_name(name), send_no=1,
    )
    db.add(
        _audit(
            actor="public",
            action="booking.intent_created",
            entity_id=row.id,
            request_id=request_id,
            detail={
                "reference": reference,
                "slot_start": slot_start.isoformat(),
                **({"supersedes_intent_id": supersedes_intent_id} if supersedes_intent_id else {}),
            },
        )
    )
    db.flush()
    return {
        "intent_id": row.id,
        "reference": reference,
        "code_sent_to_masked": mask_email(email),
        "expires_in_seconds": CODE_TTL_MINUTES * 60,
        "sends_used": sends_used,
        "remaining_sends": MAX_CODE_SENDS - sends_used,
    }


def resend_code(db, *, intent_id: str) -> dict:
    """Resend the 6-digit code: 60s cooldown, max 5 sends per intent
    (D-51). Each resend stages a distinct outbox intent (new dedup scope)
    so the mail actually goes out again."""
    booking = load_intent(db, intent_id)
    raw_code, sends_used = issue_booking_code(db, booking_id=booking.id, email=booking.guest_email)
    stage_code_email(
        db, booking_id=booking.id, email=booking.guest_email, code=raw_code,
        first=first_name(booking.guest_name), send_no=sends_used,
    )
    db.flush()
    return {
        "sent": True,
        "code_sent_to_masked": mask_email(booking.guest_email),
        "remaining_sends": MAX_CODE_SENDS - sends_used,
    }


def _audit(*, actor: str, action: str, entity_id: str, request_id: str, detail: dict):
    from .models import AuditLog

    return AuditLog(
        actor=actor,
        action=action,
        entity_type="booking",
        entity_id=entity_id,
        request_id=request_id,
        detail=detail,
    )


def load_intent(db, intent_id: str, *, confirmed_is_taken: bool = False):
    booking = db.get(Booking, intent_id)
    if booking is None:
        raise ApiError("booking_not_found")
    if booking.status == "cancelled" and booking.cancel_reason == "verification_expired":
        raise ApiError("intent_expired")
    if booking.status == "confirmed":
        # The confirm path maps this to the race-loser 409 (the slot is
        # taken — by this very intent); verify/resend treat a consumed
        # intent as expired (410).
        raise ApiError("slot_taken" if confirmed_is_taken else "intent_expired")
    if booking.status != "pending_verification":
        raise ApiError("intent_expired")
    # D-04: the hold itself expires at 15 min; a row that survived the
    # sweep is still treated as expired (fail-closed on the hold).
    if utcnow() - booking.created_at > timedelta(minutes=INTENT_HOLD_TTL_MINUTES):
        raise ApiError("intent_expired")
    return booking


# --- confirm -------------------------------------------------------------------

def _freeze_projection(booking: Booking, *, email_masked: str, queued_count: int,
                       reschedule_url: str, cancel_url: str, ics_url: str) -> dict:
    rendered = slot_label(booking.slot_start, booking.slot_end, booking.guest_timezone)
    return {
        "booking_id": booking.id,
        "reference": booking.reference_number,
        "slot_start": booking.slot_start.isoformat(),
        "slot_end": booking.slot_end.isoformat(),
        "guest_timezone": booking.guest_timezone,
        "slot_guest": f"{rendered['date_long']} · {rendered['label']} (your time · {booking.guest_timezone})",
        "slot_firm": f"{rendered['firm_date_long']} · {rendered['firm_label']} Vancouver time",
        "email_masked": email_masked,
        "queued_count": queued_count,
        "reschedule_url": reschedule_url,
        "cancel_url": cancel_url,
        "ics_url": ics_url,
        "replayed": False,
    }


def confirm_booking(db, *, intent_id: str, request_id: str) -> dict:
    """Final submit: fresh-clock re-verification → confirmed + 3 staged
    intents + manage token (A4, no commits here).

    CALLER CONTRACT: the caller holds the slot advisory lock
    (``advisory_key("booking-slot", slot_start)``) for the whole call AND
    commits inside it (see the route). The lock serializes concurrent
    confirms; the in-lock commit makes the threading backend match
    pg_advisory_xact_lock's transaction scope, so the loser always sees
    the winner's commit and gets a clean 409.
    """
    from . import calendar_worker

    booking = load_intent(db, intent_id, confirmed_is_taken=True)
    if booking.code_verified_at is None:
        raise ApiError("email_not_verified")
    slot_start = booking.slot_start
    # D-31 fail-closed: no confirm on a stale calendar.
    calendar_worker.require_fresh_calendar(db)
    if not slot_is_free(db, slot_start, exclude_booking_id=booking.id):
        raise ApiError("slot_taken")
    # Double-check the unique backstop under the lock (no concurrent
    # pending row can have appeared since load_intent).
    clash = (
        db.execute(
            select(Booking.id).where(
                Booking.slot_start == slot_start,
                Booking.id != booking.id,
                Booking.status.in_(ACTIVE_SLOT_STATUSES),
            )
        )
        .first()
    )
    if clash is not None:
        raise ApiError("slot_taken")
    now = utcnow()
    booking.status = "confirmed"
    booking.revision += 1
    booking.updated_at = now
    raw_token = tokens.new_token(32)
    booking.manage_token_hash = tokens.token_digest(raw_token)
    db.flush()

    revision = booking.revision
    rendered = slot_label(slot_start, slot_start + SLOT_DELTA, booking.guest_timezone)
    manage_base = f"{SITE_ORIGIN}/booking/manage/{raw_token}"
    # F3 dedup scopes carry the revision: a reschedule stages a fresh
    # confirmation version (EMAIL_TEMPLATES §3.3, D-14 mapping).
    outbox.stage_intent(
        db,
        kind="booking_confirmation",
        scope_id=f"{booking.id}:confirm:v{revision}",
        destination=booking.guest_email,
        snapshot_fields={
            "first_name": first_name(booking.guest_name),
            "service_name": "Free 30-Min Consultation",
            "date_long": rendered["date_long"],
            "date_short": booking.slot_start.astimezone(zone(booking.guest_timezone)).strftime("%b %-d"),
            "time_start_guest_tz": rendered["label"].split(" – ")[0],
            "time_end_guest_tz": rendered["label"].split(" – ")[1],
            "guest_timezone": booking.guest_timezone,
            "firm_time": f"{rendered['firm_date_long']} · {rendered['firm_label']} Vancouver time",
            "meeting_location": f"Phone call — we'll call you at {booking.guest_phone_e164 or 'the number you provided'}",
            "booking_reference": booking.reference_number,
            "preparation_bullets": (
                "Have your most recent tax return handy.\n"
                "List any major life or business changes this year.\n"
                "Prepare your questions in advance."
            ),
            "reschedule_url": f"{manage_base}#reschedule",
            "cancel_url": f"{manage_base}#cancel",
        },
    )
    outbox.stage_intent(
        db,
        kind="booking_owner_notification",
        scope_id=f"{booking.id}:owner:v{revision}",
        destination=OWNER_EMAIL,
        snapshot_fields={
            "headline": f"New booking: {booking.reference_number}",
            "slot_human": f"{rendered['date_long']} · {rendered['label']} ({booking.guest_timezone})",
            "guest_name": booking.guest_name,
            "guest_email": booking.guest_email,
            "guest_phone": booking.guest_phone_e164 or "—",
            "guest_relationship": booking.guest_relationship.replace("_", " ").title(),
            "discuss": booking.discuss_notes or "—",
            "booking_reference": booking.reference_number,
        },
    )
    outbox.stage_intent(
        db,
        kind="booking_reminder",
        scope_id=f"{booking.id}:reminder:v{revision}",
        destination=booking.guest_email,
        not_before=slot_start - REMINDER_LEAD,
        snapshot_fields={
            "first_name": first_name(booking.guest_name),
            "service_name": "Free 30-Min Consultation",
            "date_long": rendered["date_long"],
            "time_range": rendered["label"],
            "guest_timezone": booking.guest_timezone,
            "booking_reference": booking.reference_number,
            "reschedule_url": f"{manage_base}#reschedule",
            "cancel_url": f"{manage_base}#cancel",
        },
    )
    db.add(
        _audit(
            actor="public",
            action="booking.confirmed",
            entity_id=booking.id,
            request_id=request_id,
            detail={"reference": booking.reference_number, "queued_count": 3},
        )
    )
    db.flush()
    return _freeze_projection(
        booking,
        email_masked=mask_email(booking.guest_email),
        queued_count=3,
        reschedule_url=f"{manage_base}#reschedule",
        cancel_url=f"{manage_base}#cancel",
        ics_url=f"{manage_base}/ics",
    )


# --- manage links --------------------------------------------------------------

def resolve_manage_token(db, raw_token: str) -> Booking:
    """Resolve a manage token to its booking. Invalid tokens → generic 404
    (no oracle, A6). Expired tokens (90d post appointment end) → 410."""
    digest = tokens.token_digest(raw_token)
    booking = (
        db.execute(select(Booking).where(Booking.manage_token_hash == digest))
        .scalars()
        .first()
    )
    if booking is None:
        raise ApiError("booking_not_found")
    if not constant_time_compare(booking.manage_token_hash or "", digest):
        raise ApiError("booking_not_found")
    if booking.slot_end + MANAGE_TOKEN_LIFETIME < utcnow():
        raise ApiError("manage_token_expired")
    return booking


def allowed_actions(booking: Booking, *, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    if booking.status != "confirmed":
        return []
    if booking.slot_start - now >= CANCEL_CUTOFF:
        return ["reschedule", "cancel"]
    return []


def manage_projection(booking: Booking) -> dict:
    rendered = slot_label(booking.slot_start, booking.slot_end, booking.guest_timezone)
    return {
        "reference": booking.reference_number,
        "status": booking.status,
        "guest_name": booking.guest_name,
        "guest_email": booking.guest_email,
        "guest_phone": booking.guest_phone_e164,
        "guest_relationship": booking.guest_relationship,
        "slot_start": booking.slot_start.isoformat(),
        "slot_end": booking.slot_end.isoformat(),
        "guest_timezone": booking.guest_timezone,
        "slot_guest": f"{rendered['date_long']} · {rendered['label']} (your time · {booking.guest_timezone})",
        "slot_firm": f"{rendered['firm_date_long']} · {rendered['firm_label']} Vancouver time",
        "allowed_actions": allowed_actions(booking),
        "cutoffs": {
            "cancel_hours": int(CANCEL_CUTOFF.total_seconds() // 3600),
            "reschedule_hours": int(RESCHEDULE_CUTOFF.total_seconds() // 3600),
        },
        "supersedes": booking.supersedes,
    }


def reschedule_booking(db, *, booking: Booking, new_slot_start: datetime,
                       lock_backend: advisory.AdvisoryLockBackend, request_id: str) -> dict:
    """All-or-nothing reschedule: lock old+new slots ascending → fresh
    availability on the new slot → old row 'rescheduled' (token revoked) →
    new confirmed row with a fresh reference, token, and intents. A raced
    new slot → 409 and the old booking is untouched."""
    from . import calendar_worker

    now = utcnow()
    if booking.status != "confirmed":
        raise ApiError("booking_not_found")
    if booking.slot_start - now < RESCHEDULE_CUTOFF:
        raise ApiError("reschedule_too_late")
    calendar_worker.require_fresh_calendar(db)
    old_key = advisory.advisory_key("booking-slot", booking.slot_start.isoformat())
    new_key = advisory.advisory_key("booking-slot", new_slot_start.isoformat())
    first, second = sorted((old_key, new_key))
    with lock_backend.acquire(db, first):
        with lock_backend.acquire(db, second):
            if not slot_is_free(db, new_slot_start, exclude_booking_id=booking.id):
                raise ApiError("slot_taken")
            old_ref = booking.reference_number
            old_rendered = slot_label(
                booking.slot_start, booking.slot_end, booking.guest_timezone
            )
            # The confirm staged the old slot's intents under the
            # pre-increment revision — capture it for F2 suppression.
            staged_revision = booking.revision
            booking.status = "rescheduled"
            booking.manage_token_hash = None  # old links die immediately
            booking.revision += 1
            booking.updated_at = now
            db.flush()

            new = Booking(
                id=new_uuid7(),
                reference_number=refnums.new_reference_number(),
                event_code=EVENT_CODE,
                slot_start=new_slot_start,
                slot_end=new_slot_start + SLOT_DELTA,
                guest_timezone=booking.guest_timezone,
                guest_name=booking.guest_name,
                guest_email=booking.guest_email,
                guest_phone_e164=booking.guest_phone_e164,
                guest_relationship=booking.guest_relationship,
                discuss_notes=booking.discuss_notes,
                staff_id="jared",
                status="confirmed",
                revision=1,
                payload_hash=sha256_hex(
                    "jaro-booking-payload-v1", new_slot_start.isoformat(),
                    booking.guest_timezone, booking.guest_email,
                ),
                code_verified_at=now,  # verified via the manage token
                supersedes=booking.id,
                created_at=now,
                updated_at=now,
            )
            db.add(new)
            db.flush()
            raw_token = tokens.new_token(32)
            new.manage_token_hash = tokens.token_digest(raw_token)
            manage_base = f"{SITE_ORIGIN}/booking/manage/{raw_token}"
            rendered = slot_label(new_slot_start, new_slot_start + SLOT_DELTA, new.guest_timezone)
            outbox.stage_intent(
                db,
                kind="booking_confirmation",
                scope_id=f"{new.id}:confirm:v1",
                destination=new.guest_email,
                snapshot_fields={
                    "first_name": first_name(new.guest_name),
                    "service_name": "Free 30-Min Consultation",
                    "date_long": rendered["date_long"],
                    "date_short": new.slot_start.astimezone(zone(new.guest_timezone)).strftime("%b %-d"),
                    "time_start_guest_tz": rendered["label"].split(" – ")[0],
                    "time_end_guest_tz": rendered["label"].split(" – ")[1],
                    "guest_timezone": new.guest_timezone,
                    "firm_time": f"{rendered['firm_date_long']} · {rendered['firm_label']} Vancouver time",
                    "meeting_location": f"Phone call — we'll call you at {new.guest_phone_e164 or 'the number you provided'}",
                    "booking_reference": new.reference_number,
                    "preparation_bullets": (
                        "Have your most recent tax return handy.\n"
                        "List any major life or business changes this year.\n"
                        "Prepare your questions in advance."
                    ),
                    "reschedule_url": f"{manage_base}#reschedule",
                    "cancel_url": f"{manage_base}#cancel",
                    "rescheduled_from_block": (
                        f"\n- Rescheduled from: {old_rendered['date_long']} · {old_rendered['label']}\n"
                    ),
                },
            )
            outbox.stage_intent(
                db,
                kind="booking_owner_notification",
                scope_id=f"{new.id}:owner:v1",
                destination=OWNER_EMAIL,
                snapshot_fields={
                    "headline": f"Booking rescheduled: {new.reference_number} (was {old_ref})",
                    "slot_human": f"{rendered['date_long']} · {rendered['label']} ({new.guest_timezone})",
                    "guest_name": new.guest_name,
                    "guest_email": new.guest_email,
                    "guest_phone": new.guest_phone_e164 or "—",
                    "guest_relationship": new.guest_relationship.replace("_", " ").title(),
                    "discuss": new.discuss_notes or "—",
                    "booking_reference": new.reference_number,
                },
            )
            outbox.stage_intent(
                db,
                kind="booking_reminder",
                scope_id=f"{new.id}:reminder:v1",
                destination=new.guest_email,
                not_before=new_slot_start - REMINDER_LEAD,
                snapshot_fields={
                    "first_name": first_name(new.guest_name),
                    "service_name": "Free 30-Min Consultation",
                    "date_long": rendered["date_long"],
                    "time_range": rendered["label"],
                    "guest_timezone": new.guest_timezone,
                    "booking_reference": new.reference_number,
                    "reschedule_url": f"{manage_base}#reschedule",
                    "cancel_url": f"{manage_base}#cancel",
                },
            )
            # F2: the old slot's pending intents must never go out stale.
            suppressed = outbox.cancel_pending_intents(
                db, kind="booking_reminder", scope_id=f"{booking.id}:reminder:v{staged_revision}",
                reason="booking_rescheduled",
            )
            db.add(
                _audit(
                    actor="public",
                    action="booking.rescheduled",
                    entity_id=new.id,
                    request_id=request_id,
                    detail={
                        "reference": new.reference_number,
                        "old_reference": old_ref,
                        "supersedes": booking.id,
                        "suppressed_reminders": suppressed,
                    },
                )
            )
            db.flush()
            projection = _freeze_projection(
                new,
                email_masked=mask_email(new.guest_email),
                queued_count=3,
                reschedule_url=f"{manage_base}#reschedule",
                cancel_url=f"{manage_base}#cancel",
                ics_url=f"{manage_base}/ics",
            )
            projection["old_reference"] = old_ref
            return projection


def cancel_booking(db, *, booking: Booking, reason: str | None,
                   request_id: str) -> dict:
    """Guest cancel: status → cancelled, token revoked, pending intents
    suppressed (F2, stable reasons), cancellation notices queued to guest
    + owner."""
    now = utcnow()
    if booking.status != "confirmed":
        raise ApiError("booking_not_found")
    if booking.slot_start - now < CANCEL_CUTOFF:
        raise ApiError("cancel_too_late")
    rendered = slot_label(booking.slot_start, booking.slot_end, booking.guest_timezone)
    # The confirm staged intents under the pre-increment revision — capture
    # it BEFORE bumping so F2 suppression hits the exact dedup scopes.
    staged_revision = booking.revision
    booking.status = "cancelled"
    booking.cancelled_at = now
    booking.cancel_reason = (reason or "").strip()[:500] or "guest_cancelled"
    booking.manage_token_hash = None  # token revoked on cancel
    booking.revision += 1
    booking.updated_at = now
    # F2 cancel-suppression: queued/retry confirmation + reminder intents
    # are cancelled with stable reasons — never silently dropped.
    suppressed = 0
    for kind in ("booking_confirmation", "booking_owner_notification", "booking_reminder"):
        suppressed += outbox.cancel_pending_intents(
            db, kind=kind, scope_id=f"{booking.id}:{_scope_suffix(kind)}:v{staged_revision}",
            reason="booking_cancelled",
        )
    guest_reason = booking.cancel_reason if booking.cancel_reason not in ("", "guest_cancelled") else ""
    outbox.stage_intent(
        db,
        kind="booking_cancellation_notice",
        scope_id=f"{booking.id}:cancel-notice:guest",
        destination=booking.guest_email,
        snapshot_fields={
            "first_name": first_name(booking.guest_name),
            "booking_reference": booking.reference_number,
            "slot_human": f"{rendered['date_long']} · {rendered['label']} ({booking.guest_timezone})",
            "cancel_reason_block": f"- Reason you gave: {guest_reason}\n" if guest_reason else "",
            "detail_block": (
                "Your appointment has been cancelled. If this was a mistake "
                "or you'd like to pick a new time, you can book again any "
                "time — no account needed."
            ),
            # §1 one-link rule: the rebook page URL is the single action
            # link in the guest cancellation notice.
            "action_line": (
                "Book a new time any time (no account needed):\n"
                f"{SITE_ORIGIN}/free-30-min-consultation/\n"
            ),
        },
    )
    outbox.stage_intent(
        db,
        kind="booking_cancellation_notice",
        scope_id=f"{booking.id}:cancel-notice:owner",
        destination=OWNER_EMAIL,
        snapshot_fields={
            "first_name": "Jared",
            "booking_reference": booking.reference_number,
            "slot_human": f"{rendered['date_long']} · {rendered['label']} ({booking.guest_timezone})",
            "cancel_reason_block": f"- Reason: {booking.cancel_reason}\n",
            "detail_block": (
                f"Guest: {booking.guest_name} <{booking.guest_email}> "
                f"{booking.guest_phone_e164 or ''}".strip()
            ),
            "action_line": "",  # owner notice carries no action link (§1)
        },
    )
    db.add(
        _audit(
            actor="public",
            action="booking.cancelled",
            entity_id=booking.id,
            request_id=request_id,
            detail={
                "reference": booking.reference_number,
                "suppressed_intents": suppressed,
                "queued_count": 2,
            },
        )
    )
    db.flush()
    return {
        "cancelled": True,
        "reference": booking.reference_number,
        "queued_count": 2,
        "suppressed_intents": suppressed,
    }


def _scope_suffix(kind: str) -> str:
    return {"booking_confirmation": "confirm", "booking_owner_notification": "owner",
            "booking_reminder": "reminder"}[kind]


# --- no-show (owner lever) -----------------------------------------------------

def mark_no_show(db, *, booking: Booking, request_id: str) -> dict:
    now = utcnow()
    if booking.status not in ("confirmed", "completed"):
        raise ApiError(
            "validation_failed",
            "The request could not be validated.",
            [{"field": "status", "message": "Only confirmed or completed bookings can be marked no-show."}],
        )
    booking.status = "no_show"
    booking.no_show_at = now
    booking.revision += 1
    booking.updated_at = now
    db.add(
        _audit(
            actor="owner",
            action="booking.no_show",
            entity_id=booking.id,
            request_id=request_id,
            detail={"reference": booking.reference_number},
        )
    )
    db.flush()
    return {"no_show": True, "reference": booking.reference_number}


# --- worker eligibility (E3 second gate) ----------------------------------------

def booking_reminder_eligible(session_factory):
    """Eligibility gate for ``booking_reminder`` intents: the reminder only
    goes out while the booking is still confirmed. Cancelled/rescheduled/
    no-show bookings suppress their reminders here even if F2 missed one."""

    def _gate(fields: dict):
        ref = fields.get("booking_reference", "")
        db = session_factory()
        try:
            row = (
                db.execute(select(Booking).where(Booking.reference_number == ref))
                .scalars()
                .first()
            )
            if row is None:
                return False, "booking_not_found"
            if row.status != "confirmed":
                return False, f"booking_{row.status}"
            return True, "ok"
        finally:
            db.close()

    return _gate


# --- ICS download ---------------------------------------------------------------

def build_ics(*, booking: Booking) -> str:
    """Minimal RFC 5545 VEVENT (text/calendar). Custom code (no shared
    pattern fit — the venv ships no ICS library and the worker never needs
    it; stdlib-only construction with UTC instants + TZID echo for the
    guest)."""
    def _fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    rendered = slot_label(booking.slot_start, booking.slot_end, booking.guest_timezone)
    uid = f"{booking.id}@jaroaccounting.com"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Jaro Accounting//Booking//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_fmt(utcnow())}",
        f"DTSTART:{_fmt(booking.slot_start)}",
        f"DTEND:{_fmt(booking.slot_end)}",
        "SUMMARY:Free 30-Min Consultation — Jaro Accounting",
        f"DESCRIPTION:Reference {booking.reference_number}. "
        f"{rendered['date_long']} {rendered['label']} ({booking.guest_timezone}). "
        "Phone call — Jared will call you.",
        "LOCATION:Phone call",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # CRLF per RFC 5545.
    return "\r\n".join(lines) + "\r\n"
