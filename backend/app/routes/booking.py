"""Booking routes — /api/v1/booking/* (D-05 canonical prefix).

Phase 3: the full booking widget flow per docs/BOOKING.md + the canonical
reconciliations (IMPLEMENTATION_PLAN §3.1/D-04):
- availability (staleness gate is real — D-31 fail-closed)
- intents (the intent IS the pending_verification booking row), 6-digit
  mailbox-proof codes, confirm (idempotent), tokenized manage links
  (reschedule/cancel), .ics download, no-show lever (admin)
"""

from __future__ import annotations

from datetime import date as dt_date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.exc import IntegrityError

from .. import advisory
from .. import booking as booking_lib
from .. import calendar_worker, idempotency as idem
from ..db import utcnow
from ..errors import ApiError
from ..idempotency import ReplayHit, require_idempotency_key
from ..models import Booking
from ..rate_limits import rate_limit_dependency
from . import DB, StrictModel

router = APIRouter(prefix="/api/v1/booking", tags=["booking"])


def _locks(request: Request):
    return request.app.state.lock_backend


# --- request models (A2 closed) -----------------------------------------------

GUEST_RELATIONSHIPS = ("prospective_client", "current_client", "referral_professional", "other")


class BookingIntentRequest(StrictModel):
    slot_start: str = Field(min_length=1, max_length=64)
    guest_timezone: str = Field(min_length=3, max_length=64)
    name: str
    email: str
    phone: str
    country_code: str = "+1"
    relationship: str = "prospective_client"
    discuss: str
    # Previous pending intent this one supersedes (review Change-link
    # re-submit: email/details/slot edit). The old hold is expired
    # atomically so the guest's own previous attempt can't 409 them.
    supersedes_intent_id: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise PydanticCustomError("value_error", "Please enter your name (2–100 characters).")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not booking_lib.valid_email(v):
            raise PydanticCustomError("value_error", "Please enter a valid email address, like you@example.com.")
        return v.lower()

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise PydanticCustomError("value_error", "Please enter your phone number.")
        if len(v) > 32:
            raise PydanticCustomError("value_error", "Please enter a valid phone number.")
        return v

    @field_validator("relationship")
    @classmethod
    def _relationship(cls, v: str) -> str:
        if v not in GUEST_RELATIONSHIPS:
            raise PydanticCustomError("value_error", "Please choose one of the listed options.")
        return v

    @field_validator("discuss")
    @classmethod
    def _discuss(cls, v: str) -> str:
        v = v.strip()
        if not (10 <= len(v) <= 1000):
            raise PydanticCustomError(
                "value_error", "Please tell us a little about what you'd like to discuss (10–1000 characters)."
            )
        return v


class VerifyCodeRequest(StrictModel):
    code: str = Field(min_length=6, max_length=6)


class ConfirmRequest(StrictModel):
    intent_id: str = Field(min_length=1, max_length=64)


class ManageActionRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=500)


class RescheduleRequest(StrictModel):
    new_slot_start: str = Field(min_length=1, max_length=64)


# --- helpers -------------------------------------------------------------------

def _replay_response(hit: ReplayHit):
    # JSONResponse bypasses response_model so the replayed flag survives
    # (the Phase-2 pattern — DECISIONS-backend.md Phase-2 item 3).
    return JSONResponse(status_code=hit.status_code, content=hit.result)


def _idempotent_execute(db, request: Request, *, route: str, payload: dict, execute):
    """A3 wrapper: begin → execute → store, mapping ReplayHit/IntegrityError
    to the typed recovery responses."""
    key = require_idempotency_key(request)
    try:
        payload_hash = idem.begin(db, route=route, client_key=key, payload=payload)
    except ReplayHit as hit:
        return _replay_response(hit), True
    try:
        result, status_code = execute()
    except IntegrityError as exc:
        # The unraceable backstop (F6/B2): the partial unique on slot_start
        # fired under concurrency — a clean 409, never a 500.
        if "ix_bookings_slot_active" in str(exc.orig or exc):
            raise ApiError("slot_taken")
        raise
    idem.store(db, route=route, client_key=key, payload_hash=payload_hash,
               result=result, status_code=status_code)
    return result, False


# --- availability (D-31 fail-closed staleness gate) ------------------------------

@router.get("/config", operation_id="bookingConfig")
def booking_config(db=DB):
    return booking_lib.get_config(db)


@router.get(
    "/availability",
    operation_id="bookingAvailability",
    # A5: calendar browsing is budgeted but not throttled into uselessness
    # (120/hour route-source — IMPLEMENTATION_PLAN §6.1/D-21 starting value).
    dependencies=[Depends(rate_limit_dependency("booking.slots"))],
)
def booking_availability(request: Request, db=DB, year: int = 0, month: int = 0, tz: str = ""):
    calendar_worker.require_fresh_calendar(db)
    if not (1900 <= year <= 2200) or not (1 <= month <= 12):
        raise ApiError(
            "validation_failed", "The request could not be validated.",
            [{"field": "month", "message": "Please choose a valid year and month."}],
        )
    guest_tz = tz.strip() or booking_lib.FIRM_TIMEZONE
    booking_lib.zone(guest_tz)  # 422 on unknown zones
    return {
        "year": year,
        "month": month,
        "days": booking_lib.available_days(db, year, month),
        "as_of": booking_lib.calendar_as_of(db),
        "guest_timezone_echo": guest_tz,
    }


@router.get(
    "/slots",
    operation_id="bookingSlots",
    dependencies=[Depends(rate_limit_dependency("booking.slots"))],
)
def booking_slots(request: Request, db=DB, date: str = "", tz: str = ""):
    calendar_worker.require_fresh_calendar(db)
    try:
        day = dt_date.fromisoformat(date)
    except ValueError:
        raise ApiError(
            "validation_failed", "The request could not be validated.",
            [{"field": "date", "message": "Please choose a day from the calendar."}],
        )
    guest_tz = tz.strip() or booking_lib.FIRM_TIMEZONE
    booking_lib.zone(guest_tz)
    slots = booking_lib.generate_slots(db, day)
    return {
        "date": day.isoformat(),
        "guest_timezone": guest_tz,
        "slots": [booking_lib.slot_label(s.start_utc, s.end_utc, guest_tz) for s in slots],
        "as_of": booking_lib.calendar_as_of(db),
    }


# --- intent / verify / confirm ---------------------------------------------------

@router.post(
    "/intents",
    operation_id="bookingCreateIntent",
    status_code=201,
    # A5: per-email code-send budget is the target dimension (each intent
    # queues a code email to the form's address).
    dependencies=[Depends(rate_limit_dependency("booking.intents", target_from="email"))],
)
def booking_create_intent(body: BookingIntentRequest, request: Request, db=DB):
    slot_start = booking_lib.parse_slot_start(body.slot_start)
    phone_e164 = booking_lib.normalize_phone(body.country_code, body.phone)
    rid = getattr(request.state, "request_id", "unknown")
    try:
        result = booking_lib.create_intent(
            db,
            slot_start=slot_start,
            guest_timezone=body.guest_timezone.strip(),
            name=body.name,
            email=body.email,
            phone_e164=phone_e164,
            relationship=body.relationship,
            discuss=body.discuss,
            lock_backend=_locks(request),
            request_id=rid,
            supersedes_intent_id=body.supersedes_intent_id,
        )
    except IntegrityError as exc:
        # Fail-fast 409: a second intent on the same slot loses the unique
        # race — the typed recovery, not a 500.
        if "ix_bookings_slot_active" in str(exc.orig or exc):
            raise ApiError("slot_taken")
        raise
    return JSONResponse(status_code=201, content=result)


@router.post("/intents/{intent_id}/verify", operation_id="bookingVerifyCode")
def booking_verify_code(intent_id: str, body: VerifyCodeRequest, request: Request, db=DB):
    # The attempt counter must survive the error response: a failed guess
    # still counts, otherwise floods get free retries. Like the rate
    # limiter (DECISIONS-backend.md Phase-0 item 13), the counter commits
    # in its own short-lived transaction — NOT the route's, which get_db
    # rolls back when we raise the typed error below.
    session_factory = request.app.state.session_factory
    vdb = session_factory()
    try:
        booking = booking_lib.load_intent(vdb, intent_id)
        outcome = booking_lib.verify_booking_code(
            vdb, booking_id=booking.id, email=booking.guest_email, code=body.code,
            lock_backend=_locks(request),
        )
        if outcome.ok:
            booking.code_verified_at = utcnow()
        vdb.commit()
    except Exception:
        vdb.rollback()
        raise
    finally:
        vdb.close()
    if not outcome.ok:
        if outcome.code == "code_expired":
            raise ApiError("code_expired")
        if outcome.code == "code_attempts_exceeded":
            raise ApiError("code_attempts_exceeded", headers={"Retry-After": "60"})
        raise ApiError(
            "invalid_code", None,
            [{"field": "code", "message": "That code isn't right. Check the code and try again."}],
        )
    return {"verified": True}


@router.post(
    "/intents/{intent_id}/resend-code",
    operation_id="bookingResendCode",
    dependencies=[Depends(rate_limit_dependency("booking.intents"))],
)
def booking_resend_code(intent_id: str, request: Request, db=DB):
    return booking_lib.resend_code(db, intent_id=intent_id)


@router.post(
    "/confirm",
    operation_id="bookingConfirm",
    dependencies=[Depends(rate_limit_dependency("booking.confirm"))],
)
def booking_confirm(body: ConfirmRequest, request: Request, db=DB):
    rid = getattr(request.state, "request_id", "unknown")
    key = require_idempotency_key(request)  # A3: missing key fails loudly (422)
    locks = _locks(request)
    # Read-only probe for the lock key; the authoritative read happens
    # inside the lock on a fresh snapshot.
    probe = db.get(Booking, body.intent_id)
    if probe is None:
        raise ApiError("booking_not_found")
    slot_key = advisory.advisory_key("booking-slot", probe.slot_start.isoformat())
    with locks.acquire(db, slot_key):
        # Fresh snapshot after the lock: on SQLite a session's first SELECT
        # pins its read snapshot, so a loser that read before the winner's
        # commit would otherwise re-read stale state even while holding the
        # lock. No writes have happened yet — the rollback is safe. (On
        # Postgres/READ COMMITTED this is a semantic no-op.)
        db.rollback()
        payload = {"intent_id": body.intent_id}
        try:
            payload_hash = idem.begin(db, route="booking.confirm", client_key=key, payload=payload)
        except ReplayHit as hit:
            return _replay_response(hit)
        try:
            # The route holds the slot lock for the whole confirm (see
            # confirm_booking's contract).
            result = booking_lib.confirm_booking(
                db, intent_id=body.intent_id, request_id=rid
            )
        except IntegrityError as exc:
            # The unraceable backstop (F6/B2): the partial unique on
            # slot_start fired under concurrency — a clean 409, never a 500.
            if "ix_bookings_slot_active" in str(exc.orig or exc):
                raise ApiError("slot_taken")
            raise
        idem.store(db, route="booking.confirm", client_key=key,
                   payload_hash=payload_hash, result=result, status_code=200)
        # Commit INSIDE the slot lock. pg_advisory_xact_lock is
        # transaction-scoped (held until commit); the threading backend
        # (SQLite) releases at context exit. Committing here makes the lock
        # cover the commit on both dialects — the next acquirer always sees
        # the winner's commit, so the loser gets a clean 409. get_db's
        # trailing commit is then a no-op.
        db.commit()
        return JSONResponse(status_code=200, content=result)


# --- manage links -----------------------------------------------------------------

def _resolve_manage_token(db, token: str) -> Booking:
    return booking_lib.resolve_manage_token(db, token)


@router.get("/manage/{token}", operation_id="bookingManageGet")
def booking_manage_get(token: str, db=DB):
    booking = _resolve_manage_token(db, token)
    return booking_lib.manage_projection(booking)


@router.get("/manage/{token}/ics", operation_id="bookingManageIcs")
def booking_manage_ics(token: str, db=DB):
    booking = _resolve_manage_token(db, token)
    ics = booking_lib.build_ics(booking=booking)
    return Response(
        content=ics,
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="booking-{booking.reference_number}.ics"'},
    )


@router.post(
    "/manage/{token}/reschedule",
    operation_id="bookingReschedule",
    dependencies=[Depends(rate_limit_dependency("booking.confirm"))],
)
def booking_reschedule(token: str, body: RescheduleRequest, request: Request, db=DB):
    rid = getattr(request.state, "request_id", "unknown")
    new_slot_start = booking_lib.parse_slot_start(body.new_slot_start)
    booking = _resolve_manage_token(db, token)

    def _execute():
        result = booking_lib.reschedule_booking(
            db, booking=booking, new_slot_start=new_slot_start,
            lock_backend=_locks(request), request_id=rid,
        )
        return result, 200

    # The token digest (never the raw token) scopes the idempotency payload.
    from ..tokens import token_digest

    payload = {"manage": "reschedule", "token_digest": token_digest(token),
               "new_slot_start": new_slot_start.isoformat()}
    outcome, replayed = _idempotent_execute(
        db, request, route="booking.manage.reschedule", payload=payload, execute=_execute
    )
    if replayed:
        return outcome
    return JSONResponse(status_code=200, content=outcome)


@router.post("/manage/{token}/cancel", operation_id="bookingManageCancel")
def booking_manage_cancel(token: str, body: ManageActionRequest, request: Request, db=DB):
    rid = getattr(request.state, "request_id", "unknown")
    booking = _resolve_manage_token(db, token)

    def _execute():
        result = booking_lib.cancel_booking(
            db, booking=booking, reason=body.reason, request_id=rid
        )
        return result, 200

    from ..tokens import token_digest

    payload = {"manage": "cancel", "token_digest": token_digest(token),
               "reason": body.reason or ""}
    outcome, replayed = _idempotent_execute(
        db, request, route="booking.manage.cancel", payload=payload, execute=_execute
    )
    if replayed:
        return outcome
    return JSONResponse(status_code=200, content=outcome)
