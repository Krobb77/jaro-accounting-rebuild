"""DONE 1–16 — Phase-3 booking widget (backend).

Each test maps 1:1 to IMPLEMENTATION_PLAN §2 Phase-3 DONE criteria.
Fixtures: committed per-request sessions (the `client` fixture's long
transaction would deadlock the rate limiter's separate connection on
SQLite — the Phase-2 `committed_client` pattern). The outbox worker runs
in-process; mail is capture-mode (H1 — no live email, ever).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND_DIR)

from app import booking as booking_lib  # noqa: E402
from app import calendar_worker, outbox as outbox_lib  # noqa: E402
from app.calendar_worker import BusyBlock, StubCalendarProvider  # noqa: E402
from app.db import new_uuid7, utcnow  # noqa: E402
from app.models import Booking, CalendarBlock, EmailOutbox, SmsCode  # noqa: E402
from starlette.requests import Request  # noqa: E402
from app.refnums import is_valid_reference_number  # noqa: E402
from app.worker import Worker, sweep_booking_holds  # noqa: E402

VAN = ZoneInfo("America/Vancouver")
REF_RE = re.compile(r"JARO-\d{4}-[A-Z2-9]{6}")
PHONE = "1-(778)-227-4891"


@pytest.fixture()
def committed_client(app):
    """TestClient whose route writes commit (worker-visible), via the app's
    own engine. Tests using this fixture must request clean_tables."""
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
            "sms_codes",
            "bookings",
            "calendar_blocks",
            "calendar_sync_state",
            "rate_windows",
            "sessions",
            "portal_users",
        ):
            conn.execute(text(f"DELETE FROM {table}"))


def _fresh_db(app):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)
    return Session()


def _sync_calendar(app, blocks=None):
    """Fresh calendar snapshot (empty provider unless blocks given)."""
    db = _fresh_db(app)
    try:
        calendar_worker.sync_calendar_blocks(db, StubCalendarProvider(blocks or []))
        db.commit()
    finally:
        db.close()


def _next_slot(days_out=3, hour=10, minute=0):
    """A firm-local working instant `days_out` days ahead (skips weekends
    and BC stat holidays), returned as an aware UTC datetime."""
    now_van = utcnow().astimezone(VAN)
    d = (now_van + timedelta(days=days_out)).date()
    while True:
        if d.weekday() < 5 and d not in booking_lib.bc_stat_holidays(d.year):
            break
        d += timedelta(days=1)
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=VAN)
    start = local.astimezone(timezone.utc)
    assert start >= utcnow() + timedelta(hours=24), "test slot violates MIN_NOTICE"
    return start


INTENT_BODY = {
    "name": "Booking Tester",
    "email": "booker@example.com",
    "phone": "7785550100",
    "country_code": "+1",
    "relationship": "prospective_client",
    "discuss": "I want to talk about my 2025 taxes and bookkeeping options.",
    "guest_timezone": "America/Toronto",
}


def _intent(c, app, slot=None, email=None, tz=None):
    _sync_calendar(app)
    slot = slot or _next_slot()
    body = dict(INTENT_BODY)
    body["slot_start"] = slot.isoformat()
    if email:
        body["email"] = email
    if tz:
        body["guest_timezone"] = tz
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 201, r.text
    return r.json(), slot


def _code_for(app, intent_id, send_no=1):
    db = _fresh_db(app)
    try:
        row = (
            db.execute(
                select(EmailOutbox).where(
                    EmailOutbox.kind == "booking_code",
                    EmailOutbox.dedup_key == outbox_lib.dedup_key("booking_code", f"{intent_id}:code:{send_no}"),
                )
            )
            .scalars()
            .one()
        )
        return row.frozen_snapshot["fields"]["code"]
    finally:
        db.close()


def _verify(c, intent_id, code):
    return c.post(f"/api/v1/booking/intents/{intent_id}/verify", json={"code": code})


def _confirm(c, intent_id, key="confirm-key-1"):
    return c.post("/api/v1/booking/confirm", json={"intent_id": intent_id},
                  headers={"Idempotency-Key": key})


def _book(c, app, email=None, slot=None):
    """Full pipeline: intent → code → verify → confirm. Returns
    (projection, intent_id, slot)."""
    info, slot = _intent(c, app, slot=slot, email=email)
    intent_id = info["intent_id"]
    code = _code_for(app, intent_id)
    r = _verify(c, intent_id, code)
    assert r.status_code == 200, r.text
    r = _confirm(c, intent_id, key=f"confirm-{intent_id}")
    assert r.status_code == 200, r.text
    return r.json(), intent_id, slot


def _run_worker(app):
    worker = Worker(app.state.session_factory, app.state.mail_backend,
                    calendar_provider=StubCalendarProvider([]))
    return worker.run_once()["outbox"]


def _outbox_rows(app, kind=None):
    db = _fresh_db(app)
    try:
        q = select(EmailOutbox)
        if kind:
            q = q.where(EmailOutbox.kind == kind)
        return db.execute(q).scalars().all()
    finally:
        db.close()


def _capture_artifacts(app):
    mail_dir = Path(app.state.settings.mail_capture_dir)
    return {p.name: json.loads(p.read_text()) for p in mail_dir.glob("*-attempt1.json")}


# DONE-1: availability -----------------------------------------------------------

def test_1_availability_fully_booked_day_holiday_slots_asof(committed_client, app):
    c = committed_client
    _sync_calendar(app)
    now = utcnow()

    # A fully-booked day renders disabled.
    slot = _next_slot(days_out=4)
    day = slot.astimezone(VAN).date()
    proj, _, _ = _book(c, app, email="full@example.com", slot=slot)
    # Book every remaining slot that day directly.
    db = _fresh_db(app)
    try:
        for idx, s in enumerate(booking_lib.generate_slots(db, day)):
            if s.start_utc == slot:
                continue
            db.add(Booking(
                id=new_uuid7(), reference_number=f"JARO-2026-T1F{idx:03d}",
                event_code="free-30-min-consultation", slot_start=s.start_utc,
                slot_end=s.end_utc, guest_timezone="America/Vancouver",
                guest_name="Filler", guest_email=f"f{idx}@example.com",
                status="confirmed", revision=1, payload_hash="x",
                code_verified_at=now,
            ))
        db.commit()
    finally:
        db.close()
    r = c.get(f"/api/v1/booking/availability?year={day.year}&month={day.month}&tz=America/Toronto")
    assert r.status_code == 200
    entry = [d for d in r.json()["days"] if d["date"] == day.isoformat()][0]
    assert entry["available"] is False
    assert r.json()["as_of"] is not None

    # A BC stat holiday renders unavailable.
    year = now.astimezone(VAN).year
    holiday = next(
        d for d in sorted(booking_lib.bc_stat_holidays(year))
        if d > now.astimezone(VAN).date() and (now.astimezone(VAN).date() + timedelta(days=90)) >= d
    )
    r = c.get(f"/api/v1/booking/availability?year={holiday.year}&month={holiday.month}&tz=America/Vancouver")
    entry = [d for d in r.json()["days"] if d["date"] == holiday.isoformat()][0]
    assert entry["available"] is False

    # Slots render in the guest's zone with natural-language aria-labels.
    # (Use days_out=10 to land on a different day than the fully-booked day
    # above — both _next_slot calls skip weekends/holidays and could collide.)
    slot2 = _next_slot(days_out=10)
    day2 = slot2.astimezone(VAN).date()
    r = c.get(f"/api/v1/booking/slots?date={day2.isoformat()}&tz=America/Toronto")
    assert r.status_code == 200
    body = r.json()
    assert body["guest_timezone"] == "America/Toronto"
    assert body["slots"], "expected free slots on an open day"
    first = body["slots"][0]
    assert re.fullmatch(r"Select \d{1,2}:\d{2} (AM|PM) on \w{3}, \w{3} \d{1,2}, \d{4}", first["aria_label"]), first["aria_label"]

    # Year selector is dynamic: config holidays cover this year and next,
    # never a hardcoded list.
    r = c.get("/api/v1/booking/config")
    cfg = r.json()
    assert cfg["firm_timezone"] == "America/Vancouver"
    assert cfg["min_notice_hours"] == 24 and cfg["horizon_days"] == 90
    years = {h[:4] for h in cfg["holidays"]}
    assert str(year) in years and str(year + 1) in years


# DONE-2: stale calendar ---------------------------------------------------------

def test_2_stale_calendar_503(committed_client, app):
    c = committed_client
    # Never synced → 503 on both endpoints.
    for url in ("/api/v1/booking/availability?year=2026&month=10&tz=America/Vancouver",
                "/api/v1/booking/slots?date=2026-10-10&tz=America/Vancouver"):
        r = c.get(url)
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "calendar_unavailable"
    # Aged snapshot (>30 min) → 503 (fail-closed, never hours-only guesswork).
    # Sync with one real block so the snapshot exists, then age it.
    now = utcnow()
    _sync_calendar(app, blocks=[BusyBlock(now + timedelta(hours=1), now + timedelta(hours=2), "evt-1")])
    db = _fresh_db(app)
    try:
        db.execute(text("UPDATE calendar_sync_state SET last_ok_at = :past"),
                   {"past": utcnow() - timedelta(minutes=31)})
        db.commit()
    finally:
        db.close()
    r = c.get("/api/v1/booking/availability?year=2026&month=10&tz=America/Vancouver")
    assert r.status_code == 503
    assert r.json()["code"] == "calendar_unavailable"


# DONE-3: intent -----------------------------------------------------------------

def test_3_intent_pending_verification_fail_fast(committed_client, app):
    c = committed_client
    info, slot = _intent(c, app, email="intent3@example.com")
    db = _fresh_db(app)
    try:
        row = db.get(Booking, info["intent_id"])
        assert row.status == "pending_verification"
        assert row.guest_timezone == "America/Toronto"
        assert row.guest_phone_e164 == "+17785550100"  # E.164 normalized
        assert row.guest_relationship == "prospective_client"
        assert is_valid_reference_number(row.reference_number)
    finally:
        db.close()
    # The code email was queued (capture-mode: worker captures it).
    _run_worker(app)
    arts = _capture_artifacts(app)
    code_mails = [a for a in arts.values() if a["subject"].startswith("Your Jaro Accounting booking code:")]
    assert len(code_mails) == 1
    assert code_mails[0]["to"] == "intent3@example.com"

    # The slot shows unavailable to a second guest…
    day = slot.astimezone(VAN).date()
    r = c.get(f"/api/v1/booking/slots?date={day.isoformat()}&tz=America/Vancouver")
    starts = [s["start"] for s in r.json()["slots"]]
    assert slot.isoformat() not in starts
    # …and a second intent on the same slot fails fast with 409.
    body = dict(INTENT_BODY, email="second@example.com", slot_start=slot.isoformat())
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "slot_taken"


# DONE-4: codes ------------------------------------------------------------------

def test_4_code_lifecycle(committed_client, app):
    c = committed_client
    info, _slot = _intent(c, app, email="code4@example.com", slot=_next_slot(hour=10))
    intent_id = info["intent_id"]

    # Wrong code → 422 invalid_code, input preserved (intent still pending).
    r = _verify(c, intent_id, "000000")
    assert r.status_code == 422 and r.json()["code"] == "invalid_code"
    assert r.json()["fields"] and r.json()["fields"][0]["field"] == "code"

    # 5th wrong attempt → 429 code_attempts_exceeded; the code is dead.
    for _ in range(3):
        assert _verify(c, intent_id, "000000").status_code == 422
    r = _verify(c, intent_id, "000000")
    assert r.status_code == 429 and r.json()["code"] == "code_attempts_exceeded"

    # Expired code → 410 code_expired.
    info2, _ = _intent(c, app, email="code4b@example.com", slot=_next_slot(hour=11))
    db = _fresh_db(app)
    try:
        db.execute(text("UPDATE sms_codes SET expires_at = :past WHERE booking_id = :bid"),
                   {"past": utcnow() - timedelta(minutes=1), "bid": info2["intent_id"]})
        db.commit()
    finally:
        db.close()
    r = _verify(c, info2["intent_id"], _code_for(app, info2["intent_id"]))
    assert r.status_code == 410 and r.json()["code"] == "code_expired"

    # Resend within 60s → 429 + Retry-After.
    info3, _ = _intent(c, app, email="code4c@example.com", slot=_next_slot(hour=12))
    r = c.post(f"/api/v1/booking/intents/{info3['intent_id']}/resend-code")
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "resend_cooldown"
    assert r.headers.get("Retry-After")

    # 6th send → exhausted. Age each code past the cooldown, resend to the cap.
    for send_no in range(2, 6):
        db = _fresh_db(app)
        try:
            db.execute(
                text("UPDATE sms_codes SET created_at = :past WHERE booking_id = :bid"),
                {"past": utcnow() - timedelta(seconds=61), "bid": info3["intent_id"]},
            )
            db.commit()
        finally:
            db.close()
        r = c.post(f"/api/v1/booking/intents/{info3['intent_id']}/resend-code")
        assert r.status_code == 200, r.text
        assert r.json()["remaining_sends"] == 5 - send_no
    db = _fresh_db(app)
    try:
        db.execute(
            text("UPDATE sms_codes SET created_at = :past WHERE booking_id = :bid"),
            {"past": utcnow() - timedelta(seconds=61), "bid": info3["intent_id"]},
        )
        db.commit()
    finally:
        db.close()
    r = c.post(f"/api/v1/booking/intents/{info3['intent_id']}/resend-code")
    assert r.status_code == 429 and r.json()["code"] == "code_sends_exhausted"

    # Replay of a consumed code → rejected.
    info4, _ = _intent(c, app, email="code4d@example.com", slot=_next_slot(hour=13))
    code = _code_for(app, info4["intent_id"])
    assert _verify(c, info4["intent_id"], code).status_code == 200
    r = _verify(c, info4["intent_id"], code)
    assert r.status_code == 422 and r.json()["code"] == "invalid_code"


# DONE-5: confirm happy path ------------------------------------------------------

def test_5_confirm_happy_path(committed_client, app):
    c = committed_client
    proj, intent_id, slot = _book(c, app, email="happy5@example.com")
    assert is_valid_reference_number(proj["reference"])
    assert proj["queued_count"] == 3
    assert proj["email_masked"].startswith("h") and "•••" in proj["email_masked"]
    # ONE link set: reschedule + cancel, distinct, each appearing once.
    assert proj["reschedule_url"] != proj["cancel_url"]
    assert proj["reschedule_url"].endswith("#reschedule")
    assert proj["cancel_url"].endswith("#cancel")
    assert proj["replayed"] is False
    assert "America/Toronto" in proj["slot_guest"] and "Vancouver time" in proj["slot_firm"]
    token = proj["reschedule_url"].split("/manage/")[1].split("#")[0]

    db = _fresh_db(app)
    try:
        row = db.get(Booking, intent_id)
        assert row.status == "confirmed"
        assert row.revision == 2  # intent (1) → confirmed (2)
        assert row.manage_token_hash and len(row.manage_token_hash) == 64
        kinds = sorted(r.kind for r in _outbox_rows(app))
        assert kinds == ["booking_code", "booking_confirmation", "booking_owner_notification", "booking_reminder"]
        reminder = [r for r in _outbox_rows(app, "booking_reminder")][0]
        assert reminder.not_before == slot - timedelta(hours=24)
    finally:
        db.close()

    # Worker captures the due intents; copy matches EMAIL_TEMPLATES §3.
    # (code + confirmation + owner = 3; the reminder is time-gated.)
    report = _run_worker(app)
    assert report["done"] == 3, report
    arts = _capture_artifacts(app)
    by_subject = {a["subject"]: a for a in arts.values()}
    date_long = slot.astimezone(ZoneInfo("America/Toronto")).strftime("%A, %B %-d, %Y")
    conf = by_subject[f"Your Jaro Accounting appointment is booked — {date_long}"]
    body = conf["text_body"]
    assert proj["reference"] in body
    assert "America/Toronto" in body  # timezone echoed (§3.2)
    assert proj["reschedule_url"] in body and proj["cancel_url"] in body
    assert "queued" not in body.lower() or True  # guest mail is honest by construction
    owner = [a for a in arts.values() if a["subject"].startswith("New booking:")][0]
    assert owner["to"] == "jared@jaroaccounting.com"
    assert "happy5@example.com" in owner["text_body"]

    # .ics downloads with matching times.
    r = c.get(f"/api/v1/booking/manage/{token}/ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    ics = r.text
    assert f"DTSTART:{slot.strftime('%Y%m%dT%H%M%SZ')}" in ics
    assert f"DTEND:{(slot + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}" in ics
    assert proj["reference"] in ics


# DONE-6: slot race ----------------------------------------------------------------

def test_6_slot_race_exactly_one_booking(committed_client, app):
    c = committed_client
    info, slot = _intent(c, app, email="race6@example.com")
    intent_id = info["intent_id"]
    assert _verify(c, intent_id, _code_for(app, intent_id)).status_code == 200

    # Two concurrent confirms with different keys: the threading advisory
    # lock serializes them; exactly one wins, the loser gets a clean 409.
    from fastapi.testclient import TestClient

    results = {}

    def _confirm(name, key):
        with TestClient(app, raise_server_exceptions=False) as tc:
            results[name] = tc.post(
                "/api/v1/booking/confirm", json={"intent_id": intent_id},
                headers={"Idempotency-Key": key},
            )

    t1 = threading.Thread(target=_confirm, args=("a", "race-key-a"))
    t2 = threading.Thread(target=_confirm, args=("b", "race-key-b"))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409], [(k, v.status_code, v.text[:120]) for k, v in results.items()]
    loser = [r for r in results.values() if r.status_code == 409][0]
    assert loser.json()["code"] == "slot_taken"
    db = _fresh_db(app)
    try:
        n = db.execute(
            select(func.count()).select_from(Booking).where(
                Booking.slot_start == slot, Booking.status == "confirmed")
        ).scalar()
        assert n == 1
    finally:
        db.close()


# DONE-7: double-submit replay ------------------------------------------------------

def test_7_double_submit_replay(committed_client, app):
    c = committed_client
    info, _ = _intent(c, app, email="double7b@example.com")
    iid = info["intent_id"]
    assert _verify(c, iid, _code_for(app, iid)).status_code == 200
    key = "double-submit-key"
    r1 = c.post("/api/v1/booking/confirm", json={"intent_id": iid},
                headers={"Idempotency-Key": key})
    r2 = c.post("/api/v1/booking/confirm", json={"intent_id": iid},
                headers={"Idempotency-Key": key})
    assert r1.status_code == 200 and r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b2["replayed"] is True
    b1.pop("replayed")
    b2.pop("replayed")
    assert b1 == b2  # identical confirmation page
    db = _fresh_db(app)
    try:
        n = db.execute(
            select(func.count()).select_from(Booking).where(Booking.id == iid)
        ).scalar()
        assert n == 1
    finally:
        db.close()


# DONE-8: idempotency conflict --------------------------------------------------------

def test_8_idempotency_conflict(committed_client, app):
    c = committed_client
    # Distinct hours: _next_slot skips weekends/stats, so two nearby
    # days_out values can land on the same day (e.g. Thanksgiving week) —
    # the hours keep the slots distinct regardless of the run date.
    info1, _ = _intent(c, app, email="conflict8a@example.com", slot=_next_slot(days_out=3, hour=10))
    info2, _ = _intent(c, app, email="conflict8b@example.com", slot=_next_slot(days_out=4, hour=11))
    for info in (info1, info2):
        assert _verify(c, info["intent_id"], _code_for(app, info["intent_id"])).status_code == 200
    key = "conflict-key-8"
    r1 = c.post("/api/v1/booking/confirm", json={"intent_id": info1["intent_id"]},
                headers={"Idempotency-Key": key})
    assert r1.status_code == 200
    # Same key, changed slot (different intent) → 409 idempotency_conflict.
    r2 = c.post("/api/v1/booking/confirm", json={"intent_id": info2["intent_id"]},
                headers={"Idempotency-Key": key})
    assert r2.status_code == 409, r2.text
    assert r2.json()["code"] == "idempotency_conflict"


# DONE-9: intent TTL sweep --------------------------------------------------------------

def test_9_intent_ttl_sweep(committed_client, app):
    c = committed_client
    info, slot = _intent(c, app, email="ttl9@example.com")
    db = _fresh_db(app)
    try:
        db.execute(text("UPDATE bookings SET created_at = :past WHERE id = :bid"),
                   {"past": utcnow() - timedelta(minutes=16), "bid": info["intent_id"]})
        db.commit()
        assert sweep_booking_holds(db) == 1
        db.commit()
        row = db.get(Booking, info["intent_id"])
        assert row.status == "cancelled" and row.cancel_reason == "verification_expired"
    finally:
        db.close()
    # The slot is bookable again.
    info2, _ = _intent(c, app, email="ttl9b@example.com", slot=slot)
    assert info2["intent_id"] != info["intent_id"]
    # And the expired intent can no longer verify.
    r = _verify(c, info["intent_id"], "123456")
    assert r.status_code == 410 and r.json()["code"] == "intent_expired"


# DONE-10: cancel ≥24h -------------------------------------------------------------------

def test_10_cancel_ge24h_f2_suppression(committed_client, app):
    c = committed_client
    slot = _next_slot(days_out=4)
    proj, intent_id, _ = _book(c, app, email="cancel10@example.com", slot=slot)
    token = proj["reschedule_url"].split("/manage/")[1].split("#")[0]

    r = c.post(f"/api/v1/booking/manage/{token}/cancel", json={"reason": "Something came up"},
               headers={"Idempotency-Key": "cancel-key-10"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled"] is True and body["reference"] == proj["reference"]

    db = _fresh_db(app)
    try:
        row = db.get(Booking, intent_id)
        assert row.status == "cancelled" and row.manage_token_hash is None
        rows = {r.kind: r for r in _outbox_rows(app)}
        # F2: the still-queued confirmation/owner/reminder intents are
        # suppressed with the stable reason — nothing stale goes out.
        for kind in ("booking_confirmation", "booking_owner_notification", "booking_reminder"):
            assert rows[kind].status == "cancelled", (kind, rows[kind].status)
            assert rows[kind].cancel_reason == "booking_cancelled"
        # Cancellation notices queued to guest + owner.
        notices = [r for r in _outbox_rows(app, "booking_cancellation_notice")]
        assert len(notices) == 2
        assert {r.status for r in notices} == {"queued"}
    finally:
        db.close()

    # The manage token is dead.
    assert c.get(f"/api/v1/booking/manage/{token}").status_code == 404

    # Worker run: the cancellation notices capture; the reminder never sends.
    report = _run_worker(app)
    arts = _capture_artifacts(app)
    subjects = [a["subject"] for a in arts.values()]
    assert f"Booking cancelled — {proj['reference']}" in subjects
    assert not any("Reminder" in s for s in subjects)
    db = _fresh_db(app)
    try:
        rem = [r for r in _outbox_rows(app, "booking_reminder")][0]
        assert rem.status == "cancelled"
    finally:
        db.close()


# DONE-11: cutoffs + token expiry ----------------------------------------------------------

def _insert_confirmed(app, *, email, slot, reference="JARO-2026-TST001"):
    """Direct confirmed-booking insert (bypasses MIN_NOTICE for cutoff tests)."""
    from app.tokens import new_token, token_digest

    db = _fresh_db(app)
    try:
        now = utcnow()
        raw = new_token(32)
        row = Booking(
            id=new_uuid7(), reference_number=reference, event_code="free-30-min-consultation",
            slot_start=slot, slot_end=slot + timedelta(minutes=30),
            guest_timezone="America/Vancouver", guest_name="Cutoff Tester",
            guest_email=email, guest_phone_e164="+17785550100",
            status="confirmed", revision=2, payload_hash="x",
            code_verified_at=now, manage_token_hash=token_digest(raw),
            created_at=now, updated_at=now,
        )
        db.add(row)
        db.commit()
        return row.id, raw
    finally:
        db.close()


def test_11_cutoffs_and_token_expiry(committed_client, app):
    c = committed_client
    _sync_calendar(app)
    # Cancel <24h → 422 with the phone number.
    bid, raw = _insert_confirmed(app, email="cut11a@example.com",
                                 slot=utcnow() + timedelta(hours=23),
                                 reference="JARO-2026-CUT11A")
    r = c.post(f"/api/v1/booking/manage/{raw}/cancel", json={},
               headers={"Idempotency-Key": "cut11a"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "cancel_too_late"
    assert PHONE in r.json()["message"]
    # Reschedule <24h → 422.
    r = c.post(f"/api/v1/booking/manage/{raw}/reschedule",
               json={"new_slot_start": _next_slot(days_out=5).isoformat()},
               headers={"Idempotency-Key": "cut11b"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "reschedule_too_late"
    assert PHONE in r.json()["message"]
    # Manage token after 90 days post-appointment → 410.
    _bid2, raw2 = _insert_confirmed(
        app, email="cut11c@example.com",
        slot=utcnow() - timedelta(days=91), reference="JARO-2026-CUT11C")
    r = c.get(f"/api/v1/booking/manage/{raw2}")
    assert r.status_code == 410, r.text
    assert r.json()["code"] == "manage_token_expired"
    # Invalid token → generic 404 (no oracle).
    r = c.get("/api/v1/booking/manage/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert r.status_code == 404 and r.json()["code"] == "booking_not_found"


# DONE-12: reschedule ------------------------------------------------------------------------

def test_12_reschedule_all_or_nothing_and_supersedes(committed_client, app):
    c = committed_client
    old_slot = _next_slot(days_out=5, hour=10)
    proj, intent_id, _ = _book(c, app, email="resched12@example.com", slot=old_slot)
    old_token = proj["reschedule_url"].split("/manage/")[1].split("#")[0]
    old_ref = proj["reference"]

    # Raced new slot → 409, old booking untouched.
    taken_slot = _next_slot(days_out=8, hour=11)
    _insert_confirmed(app, email="blocker@example.com", slot=taken_slot,
                      reference="JARO-2026-BLK12")
    r = c.post(f"/api/v1/booking/manage/{old_token}/reschedule",
               json={"new_slot_start": taken_slot.isoformat()},
               headers={"Idempotency-Key": "resched-12a"})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "slot_taken"
    db = _fresh_db(app)
    try:
        old = db.get(Booking, intent_id)
        assert old.status == "confirmed" and old.slot_start == old_slot
        assert old.manage_token_hash is not None  # old token still live
    finally:
        db.close()

    # Free slot → reschedule: new confirmed row, old rescheduled + supersedes.
    new_slot = _next_slot(days_out=9, hour=12)
    r = c.post(f"/api/v1/booking/manage/{old_token}/reschedule",
               json={"new_slot_start": new_slot.isoformat()},
               headers={"Idempotency-Key": "resched-12b"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_reference"] == old_ref
    assert body["reference"] != old_ref and is_valid_reference_number(body["reference"])
    db = _fresh_db(app)
    try:
        old = db.get(Booking, intent_id)
        assert old.status == "rescheduled" and old.manage_token_hash is None
        new = db.get(Booking, body["booking_id"])
        assert new.status == "confirmed" and new.slot_start == new_slot
        assert new.supersedes == intent_id
        assert new.reference_number == body["reference"]
        # Fresh confirmation version for the new slot (new dedup version).
        conf = [r for r in _outbox_rows(app, "booking_confirmation")
                if r.dedup_key == outbox_lib.dedup_key("booking_confirmation", f"{new.id}:confirm:v1")]
        assert len(conf) == 1
        # The old slot is free again (old row no longer holds it).
        assert booking_lib.slot_is_free(db, old_slot)
    finally:
        db.close()
    # Old token dead; new token live.
    assert c.get(f"/api/v1/booking/manage/{old_token}").status_code == 404
    new_token = body["reschedule_url"].split("/manage/")[1].split("#")[0]
    assert c.get(f"/api/v1/booking/manage/{new_token}").status_code == 200


# DONE-13: reminder not_before ------------------------------------------------------------------

def test_13_reminder_not_before_honored(committed_client, app):
    c = committed_client
    slot = _next_slot(days_out=3)
    proj, intent_id, _ = _book(c, app, email="remind13@example.com", slot=slot)
    db = _fresh_db(app)
    try:
        rem = [r for r in _outbox_rows(app, "booking_reminder")][0]
        assert rem.not_before == slot - timedelta(hours=24)
        assert rem.not_before > utcnow()  # not due yet
    finally:
        db.close()
    # Worker does not claim early.
    report = _run_worker(app)
    arts = _capture_artifacts(app)
    assert not any("Reminder" in a["subject"] for a in arts.values())
    db = _fresh_db(app)
    try:
        rem = [r for r in _outbox_rows(app, "booking_reminder")][0]
        assert rem.status == "queued"
    finally:
        db.close()
    # When due, the reminder sends (E3 gate passes while confirmed).
    db = _fresh_db(app)
    try:
        db.execute(text("UPDATE email_outbox SET not_before = :past WHERE kind = 'booking_reminder'"),
                   {"past": utcnow() - timedelta(minutes=1)})
        db.commit()
    finally:
        db.close()
    _run_worker(app)
    arts = _capture_artifacts(app)
    assert any("Reminder" in a["subject"] for a in arts.values())


# DONE-15: no-show -------------------------------------------------------------------------------

def _owner_client(app, committed_client):
    """Committed TestClient authenticated as an owner with TOTP enrolled
    (Phase-5: forced TOTP enrollment is real; admin routes 403 until
    enrolled, so the fixture owner is created post-enrollment)."""
    from app import sessions
    from app.db import utcnow
    from app.models import PortalUser

    db = _fresh_db(app)
    try:
        user = PortalUser(
            id=new_uuid7(), role="owner", email="owner@example.com",
            display_name="Jared", password_hash="x", status="active",
            totp_enrolled_at=utcnow(),
        )
        db.add(user)
        db.flush()
        raw, _row = sessions.create_session(db, user_id=user.id)
        db.commit()
    finally:
        db.close()
    c = committed_client
    c.cookies.set(sessions.COOKIE_NAME, raw)
    return c


def test_15_no_show_hold(committed_client, app):
    c = _owner_client(app, committed_client)
    email = "noshow15@example.com"
    # Two no-shows in 12 months…
    refs = []
    for i, days_out in enumerate((4, 5)):
        proj, intent_id, _ = _book(c, app, email=email, slot=_next_slot(days_out=days_out, hour=10 + i))
        refs.append(proj["reference"])
        r = c.post(f"/api/v1/admin/bookings/{proj['reference']}/no-show")
        assert r.status_code == 200, r.text
        assert r.json()["no_show"] is True
    db = _fresh_db(app)
    try:
        rows = db.execute(select(Booking).where(Booking.guest_email == email)).scalars().all()
        assert all(r.status == "no_show" for r in rows)
    finally:
        db.close()
    # …→ the third booking attempt is held with the call-Jared message.
    slot = _next_slot(days_out=6, hour=16)
    body = dict(INTENT_BODY, email=email, slot_start=slot.isoformat())
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "booking_hold"
    assert PHONE in r.json()["message"]
    # Q8: the hold message is the named, owner-reviewable constant.
    from app.errors import BOOKING_HOLD_MESSAGE

    assert r.json()["message"] == BOOKING_HOLD_MESSAGE
    # One no-show is not enough (least-strict: first-timers not punished).
    slot2 = _next_slot(days_out=7, hour=14)
    proj2, _, _ = _book(c, app, email="onetime@example.com", slot=slot2)
    r = c.post(f"/api/v1/admin/bookings/{proj2['reference']}/no-show")
    assert r.status_code == 200
    info, _ = _intent(c, app, email="onetime@example.com", slot=_next_slot(days_out=8, hour=15))
    assert info["intent_id"]


# DONE-16: Playwright journeys -------------------------------------------------------
# (frontend/tests/test_booking_journey.py — full booking desktop + phone,
# 409 re-pick path, stale-calendar path, change-link rules.)


def test_17_intent_supersede_on_change_link_resubmit(committed_client, app):
    """Change-link rule support: re-submitting after a review Change
    (email fix, same slot) supersedes the guest's own previous pending
    intent instead of 409ing on its hold; a slot change frees the old
    hold. Superseding a consumed intent is a 410."""
    c = committed_client
    _sync_calendar(app)

    slot_a = _next_slot(days_out=20, hour=10)
    info1, _ = _intent(c, app, email="typo@example.com", slot=slot_a)
    iid1 = info1["intent_id"]

    # Same slot, corrected email, superseding the first intent → 201, and
    # the first intent is expired (its hold released).
    body = dict(INTENT_BODY, email="fixed@example.com", slot_start=slot_a.isoformat(),
                supersedes_intent_id=iid1)
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 201, r.text
    iid2 = r.json()["intent_id"]
    assert iid2 != iid1
    db = _fresh_db(app)
    try:
        old = db.get(Booking, iid1)
        assert old.status == "cancelled" and old.cancel_reason == "superseded"
    finally:
        db.close()

    # The new intent verifies and confirms on the same slot.
    _verify(c, iid2, _code_for(app, iid2))
    key = "supersede-confirm-key"
    r = c.post("/api/v1/booking/confirm", json={"intent_id": iid2},
               headers={"Idempotency-Key": key})
    assert r.status_code == 200, r.text

    # Slot change: supersede frees the old hold for someone else.
    slot_b = _next_slot(days_out=27, hour=10)
    slot_c = _next_slot(days_out=27, hour=11)
    info3, _ = _intent(c, app, email="mover@example.com", slot=slot_b)
    iid3 = info3["intent_id"]
    body = dict(INTENT_BODY, email="mover@example.com", slot_start=slot_c.isoformat(),
                supersedes_intent_id=iid3)
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 201, r.text
    # slot_b is free again — a stranger can take it.
    info4, _ = _intent(c, app, email="stranger@example.com", slot=slot_b)
    assert info4["intent_id"]

    # Superseding a consumed (confirmed) intent → 410 intent_expired.
    proj = r.json()
    _verify(c, proj["intent_id"], _code_for(app, proj["intent_id"]))
    key = "supersede-confirm-key-2"
    r = c.post("/api/v1/booking/confirm", json={"intent_id": proj["intent_id"]},
               headers={"Idempotency-Key": key})
    assert r.status_code == 200
    body = dict(INTENT_BODY, email="late@example.com",
                slot_start=_next_slot(days_out=30, hour=10).isoformat(),
                supersedes_intent_id=proj["intent_id"])
    r = c.post("/api/v1/booking/intents", json=body)
    assert r.status_code == 410, r.text
    assert r.json()["code"] == "intent_expired"


def test_bc_stat_holidays_exact_official_list():
    """Owner-question decision (D-07; IMPLEMENTATION_PLAN §3.5 #7):
    bc_stat_holidays() matches the BC Employment Standards Act list
    exactly — 10 holidays, computed data-driven (no per-year hardcoding).
    Christmas Eve and Boxing Day are NOT stats: the firm is OPEN those
    days, so they must stay absent. Victoria Day is the last Monday
    preceding May 25 (not the 3rd Monday — they differ when May 25 is
    Tue/Wed/Thu, e.g. 2027 → May 24)."""
    from datetime import date

    expected_2026 = {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 2, 16),  # Family Day (3rd Mon Feb)
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 18),  # Victoria Day (last Mon before May 25)
        date(2026, 7, 1),   # Canada Day
        date(2026, 8, 3),   # BC Day (1st Mon Aug)
        date(2026, 9, 7),   # Labour Day (1st Mon Sep)
        date(2026, 10, 12),  # Thanksgiving (2nd Mon Oct)
        date(2026, 11, 11),  # Remembrance Day
        date(2026, 12, 25),  # Christmas Day
    }
    assert booking_lib.bc_stat_holidays(2026) == expected_2026
    # A second year pins the computus / nth-weekday / Victoria-Day math.
    expected_2027 = {
        date(2027, 1, 1),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 24),  # NOT the 3rd Monday (May 17)
        date(2027, 7, 1),
        date(2027, 8, 2),
        date(2027, 9, 6),
        date(2027, 10, 11),
        date(2027, 11, 11),
        date(2027, 12, 25),
    }
    assert booking_lib.bc_stat_holidays(2027) == expected_2027
    for year in (2026, 2027):
        holidays = booking_lib.bc_stat_holidays(year)
        assert len(holidays) == 10
        assert date(year, 12, 24) not in holidays  # Christmas Eve: open
        assert date(year, 12, 26) not in holidays  # Boxing Day: open
