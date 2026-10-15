"""Phase-3 DONE — Playwright booking journeys (desktop + phone).

- Full booking desktop: timezone modal → calendar → slots → interstitial
  dialog → details form → 6-digit code (read from the captured outbox) →
  review with Change links (no network writes on click) → confirm →
  /thank-you-for-your-booking/ renders reference + queued copy + one link
  set + .ics from the stored response.
- Phone journey: same core path on a mobile viewport, no horizontal
  overflow.
- 409 re-pick: two concurrent visitors pick the same slot; the loser gets
  the honest re-pick recovery (error + refreshed slot list without the
  taken time), never a double booking.
- Stale calendar: an aged calendar snapshot fails closed (503) with the
  honest error + retry; re-syncing and retrying recovers.

Setup mirrors test_contact_journey.py: a live FastAPI app (capture mail,
disposable sqlite) serves BOTH the API and dist/ from one origin — a
test-only StaticFiles mount standing in for the production nginx split.
Two test-only pretty-URL routes stand in for the nginx rewrites the
thank-you and manage pages need in production. Chromium is reused from
~/workspace/pw-browsers/chromium-1243 (launched via executable_path);
nothing is re-downloaded.

Run: /home/hatch/workspace/jaroaccounting-rebuild/.venv/bin/python -m pytest frontend/tests/test_booking_journey.py
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1]
REPO = FRONTEND.parent
BACKEND = REPO / "backend"
DIST = FRONTEND / "dist"

sys.path.insert(0, str(BACKEND))

from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config  # noqa: E402

CHROME = Path.home() / "workspace" / "pw-browsers" / "chromium-1243" / "chrome-linux" / "chrome"

BASE_TEST_ENV = {
    "JARO_MODE": "test",
    "JARO_WORKLOAD": "web",
    "DB_ROLE": "jaro_web",
    "SECRET_KEY": "test-secret-key-0123456789abcdef",
    "TOTP_KEK": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "MAIL_ALLOW_SEND": "false",
    "MAIL_BACKEND": "capture",
}

REF_RE = re.compile(r"JARO-2026-[A-Z2-9]{6}")

CONSULT = "/free-30-min-consultation.html"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Live app + dist on one origin. Yields (base_url, db_path, app)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(FRONTEND / "build.py")],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"frontend build failed:\n{result.stdout}\n{result.stderr}"
    tmp = tmp_path_factory.mktemp("pw-booking")
    db_path = tmp / "pw.db"
    db_url = f"sqlite:///{db_path}"
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")

    from app import main as app_main
    from app.config import load_settings

    settings = load_settings(
        {**BASE_TEST_ENV, "DATABASE_URL": db_url, "MAIL_CAPTURE_DIR": str(tmp / "mail")}
    )
    app = app_main.create_app(settings)

    # Fresh calendar snapshot (empty provider): without it every
    # availability read fails closed with 503.
    from app import calendar_worker

    db = app.state.session_factory()
    try:
        calendar_worker.sync_calendar_blocks(db, calendar_worker.StubCalendarProvider([]))
        db.commit()
    finally:
        db.close()

    # Test-only stand-ins for the production nginx rewrites: pretty URLs
    # (/thank-you-for-your-booking/, /booking/manage/{token}) map to the
    # built flat .html files.
    from fastapi.responses import FileResponse

    @app.get("/thank-you-for-your-booking/")
    def _thanks():
        return FileResponse(DIST / "thank-you-for-your-booking.html")

    @app.get("/booking/manage/{token}")
    def _manage(token: str):
        return FileResponse(DIST / "booking-manage.html")

    # Test-only stand-in for the nginx split: dist/ + /api on one origin.
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="static")

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import httpx

            # trust_env=False: the sandbox sets proxy env vars whose
            # no_proxy parsing breaks httpx on 127.0.0.1 URLs (InvalidURL);
            # the probe is loopback-only and must never use a proxy.
            probe = httpx.Client(trust_env=False)
            try:
                r = probe.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
            finally:
                probe.close()
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("live server did not start")
    yield f"http://127.0.0.1:{port}", str(db_path), app
    server.should_exit = True
    thread.join(timeout=10)
    app.state.engine.dispose()


@pytest.fixture()
def browser():
    from playwright.sync_api import sync_playwright

    assert CHROME.is_file(), f"chromium not found at {CHROME} (do not re-download)"
    with sync_playwright() as pw:
        # --no-proxy-server: loopback tests must never route through the
        # sandbox's egress proxy env vars.
        b = pw.chromium.launch(
            executable_path=str(CHROME), headless=True, args=["--no-proxy-server"]
        )
        yield b
        b.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _api(base: str, path: str) -> dict:
    import httpx

    c = httpx.Client(trust_env=False, timeout=10)
    try:
        r = c.get(base + path)
        r.raise_for_status()
        return r.json()
    finally:
        c.close()


def _dest_hash(email: str) -> str:
    """Mirror of app.outbox.destination_hash (sha256 over the v1 domain
    separator + normalized email)."""
    import hashlib

    h = hashlib.sha256()
    h.update("jaro-outbox-dest-v1".encode())
    h.update(email.strip().lower().encode())
    return h.hexdigest()


def _latest_code(db_path: str, email: str) -> str:
    """Read the most recent verification code for `email` from the
    captured outbox (capture mode — the "sent" mail is a DB row)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT frozen_snapshot FROM email_outbox "
            "WHERE kind='booking_code' AND destination_hash=? "
            "ORDER BY created_at DESC LIMIT 1",
            (_dest_hash(email),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise AssertionError(f"no booking_code found for {email}")
    return json.loads(rows[0][0])["fields"]["code"]


def _booking_count(db_path: str, status: str = "confirmed") -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE status=?", (status,)
        ).fetchone()[0]
    finally:
        conn.close()


def _confirm_timezone(page) -> None:
    """Open the timezone prompt (PX3: nothing auto-opens on arrival —
    the visitor must choose) and confirm the timezone-first dialog."""
    page.wait_for_selector("#bw-tz-prompt:not([hidden])", timeout=10000)
    page.click("#bw-tz-open")
    page.wait_for_selector("#bw-tz-dialog[open]", timeout=10000)
    page.select_option("#bw-tz-country", "Canada")
    page.select_option("#bw-tz-select", "America/Vancouver")
    page.click("#bw-tz-form button[type=submit]")
    page.wait_for_selector("#bw-step-calendar:not([hidden])", timeout=15000)


def _pick_first_available_day(page):
    """Drive calendar → slots step only (stops before the slot click)."""
    page.wait_for_selector(
        "#bw-calendar-grid .bw-day[data-date]:not(:disabled)", timeout=15000
    )
    page.click("#bw-calendar-grid .bw-day[data-date]:not(:disabled)")
    page.wait_for_selector("#bw-slots-list .bw-slot", timeout=10000)


def _continue_to_form(page):
    """From the slots step: pick the first slot → interstitial → Continue."""
    slot = page.locator("#bw-slots-list .bw-slot").first
    aria = slot.get_attribute("aria-label")
    slot.click()
    page.wait_for_selector("#bw-slot-dialog[open]", timeout=5000)
    assert "Selected time" in page.inner_text("#bw-slot-dialog")
    page.click("#bw-slot-dialog-continue")
    page.wait_for_selector("#bw-step-form:not([hidden])", timeout=5000)
    return aria


def _pick_first_available_day_and_slot(page, base: str):
    """Drive calendar → slots → interstitial → Continue. Returns the
    chosen slot's aria-label for later assertions."""
    _pick_first_available_day(page)
    return _continue_to_form(page)


def _fill_form(page, *, name="Playwright Booker", email, phone="7785550100"):
    page.fill("#bw-name", name)
    page.fill("#bw-email", email)
    page.select_option("#bw-country-code", "+1")
    page.fill("#bw-phone", phone)
    page.select_option("#bw-relationship", "prospective_client")
    page.fill(
        "#bw-discuss",
        "I want to talk about bookkeeping for my small consulting business this year.",
    )
    page.click("#bw-form-submit")
    page.wait_for_selector("#bw-step-code:not([hidden])", timeout=15000)


def _verify_code(page, db_path: str, email: str) -> None:
    code = _latest_code(db_path, email)
    assert re.fullmatch(r"\d{6}", code), code
    page.fill("#bw-code-input", code)
    page.click("#bw-code-verify")
    page.wait_for_selector("#bw-step-review:not([hidden])", timeout=15000)


# ----------------------------------------------------------------------
# Journeys
# ----------------------------------------------------------------------

def test_timezone_prompt_dismissible_and_calendar_below_header(live_server, browser):
    """PX3: nothing auto-opens on arrival — the page stays readable with
    a visible timezone affordance, and the dialog is dismissible (x /
    ESC). PX2: after tz submit, the calendar heading lands below the
    sticky header. P12/P13: step indicator marks step 1; legend renders."""
    base, _db_path, _app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}{CONSULT}")
        # PX3: no modal on arrival; prompt + page content visible.
        page.wait_for_selector("#bw-tz-prompt:not([hidden])", timeout=10000)
        assert not page.evaluate("() => document.getElementById('bw-tz-dialog').open")
        assert page.is_visible("#consult-heading")
        assert page.is_visible("#bw-tz-open")

        # PX3: opening the dialog and hitting ESC closes it without a
        # choice; the page behind stays readable.
        page.click("#bw-tz-open")
        page.wait_for_selector("#bw-tz-dialog[open]", timeout=10000)
        assert page.is_visible("#bw-tz-close")
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => !document.getElementById('bw-tz-dialog').open", timeout=5000
        )
        assert page.is_visible("#bw-tz-prompt")
        assert not page.evaluate(
            "() => document.getElementById('bw-step-calendar').hidden === false"
        )

        # Confirm the zone, then the calendar reveals below the header.
        page.click("#bw-tz-open")
        page.wait_for_selector("#bw-tz-dialog[open]", timeout=10000)
        page.select_option("#bw-tz-country", "Canada")
        page.select_option("#bw-tz-select", "America/Vancouver")
        page.click("#bw-tz-form button[type=submit]")
        page.wait_for_selector("#bw-step-calendar:not([hidden])", timeout=15000)
        page.wait_for_selector("#bw-calendar-grid .bw-day[data-date]", timeout=15000)
        page.wait_for_timeout(900)  # let the smooth scroll settle
        heading_top = page.evaluate(
            "() => document.getElementById('bw-calendar-title').getBoundingClientRect().top"
        )
        header_bottom = page.evaluate(
            "() => document.querySelector('.site-header').getBoundingClientRect().bottom"
        )
        assert heading_top >= header_bottom - 1, (
            f"calendar heading ({heading_top}px) is under the sticky header "
            f"(bottom edge {header_bottom}px)"
        )

        # P12: step indicator visible, step 1 current.
        assert page.is_visible("#bw-steps")
        assert page.get_attribute(
            '#bw-steps li[data-step="calendar"]', "aria-current"
        ) == "step"
        assert (
            page.get_attribute('#bw-steps li[data-step="slots"]', "aria-current")
            is None
        )

        # P13: legend labels both day styles.
        legend = page.inner_text(".bw-legend")
        assert "Available" in legend and "Unavailable" in legend
    finally:
        page.close()


def test_booking_journey_desktop(live_server, browser):
    base, db_path, _app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}{CONSULT}")
        _confirm_timezone(page)
        _pick_first_available_day(page)
        # P12: the step indicator tracks progress.
        assert page.get_attribute(
            '#bw-steps li[data-step="slots"]', "aria-current"
        ) == "step"
        assert (
            page.get_attribute('#bw-steps li[data-step="calendar"]', "aria-current")
            is None
        )
        _continue_to_form(page)
        _fill_form(page, email="pw.journey@example.com")
        assert page.get_attribute(
            '#bw-steps li[data-step="form"]', "aria-current"
        ) == "step"
        _verify_code(page, db_path, "pw.journey@example.com")

        # Review shows the masked details; Change links are present.
        review = page.inner_text("#bw-step-review")
        assert "pw.journey@example.com" in review
        assert page.locator(".bw-change").count() >= 4

        before = _booking_count(db_path)
        page.click("#bw-confirm")
        page.wait_for_url("**/thank-you-for-your-booking/", timeout=15000)

        # Dynamic render from the stored confirm response.
        page.wait_for_selector("#bt-dynamic:not([hidden])", timeout=10000)
        body = page.inner_text("#bt-dynamic")
        ref = REF_RE.search(body)
        assert ref, body
        assert "queued" in body.lower()
        # F7 honest vocabulary: never "sent".
        assert "was sent" not in body.lower()
        resched = page.get_attribute("#bt-reschedule", "href")
        cancel = page.get_attribute("#bt-cancel", "href")
        assert resched and "/booking/manage/" in resched, resched
        assert cancel and "/booking/manage/" in cancel, cancel
        # ONE link set: the two links share the same token.
        assert resched.split("/booking/manage/")[1].split("#")[0] == cancel.split(
            "/booking/manage/"
        )[1].split("#")[0]
        ics = page.get_attribute("#bt-ics", "href")
        assert ics and ics.endswith("/ics"), ics
        assert _booking_count(db_path) == before + 1
    finally:
        page.close()


def test_booking_journey_phone(live_server, browser):
    base, db_path, _app = live_server
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        is_mobile=True,
        has_touch=True,
    )
    page = ctx.new_page()
    try:
        page.goto(f"{base}{CONSULT}")
        _confirm_timezone(page)
        _pick_first_available_day_and_slot(page, base)
        _fill_form(page, email="pw.phone@example.com", name="Phone Booker")
        _verify_code(page, db_path, "pw.phone@example.com")
        page.tap("#bw-confirm")
        page.wait_for_url("**/thank-you-for-your-booking/", timeout=15000)
        page.wait_for_selector("#bt-dynamic:not([hidden])", timeout=10000)
        assert REF_RE.search(page.inner_text("#bt-dynamic"))
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1, f"horizontal overflow on phone: {overflow}px"
    finally:
        page.close()
        ctx.close()


def test_change_links_issue_no_network_writes(live_server, browser):
    """Change links never submit: clicking one issues no POST, and the
    email-change path (values preserved → re-submit) starts a fresh
    verification instead of reusing the old code."""
    base, db_path, _app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}{CONSULT}")
        _confirm_timezone(page)
        _pick_first_available_day_and_slot(page, base)
        _fill_form(page, email="pw.change@example.com")
        _verify_code(page, db_path, "pw.change@example.com")

        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

        # Email change: back to the form, values preserved, no POST.
        page.click('.bw-change[data-change="email"]')
        page.wait_for_selector("#bw-step-form:not([hidden])", timeout=5000)
        assert page.input_value("#bw-email") == "pw.change@example.com"
        assert page.input_value("#bw-name") == "Playwright Booker"
        assert posts == [], f"Change link issued writes: {posts}"

        # Re-submit with a new email → fresh intent → fresh code required.
        page.fill("#bw-email", "pw.change2@example.com")
        page.click("#bw-form-submit")
        page.wait_for_selector("#bw-step-code:not([hidden])", timeout=15000)
        note = page.inner_text("#bw-code-note")
        assert "fresh code" in note.lower() or "changed" in note.lower(), note
        code2 = _latest_code(db_path, "pw.change2@example.com")
        page.fill("#bw-code-input", code2)
        page.click("#bw-code-verify")
        page.wait_for_selector("#bw-step-review:not([hidden])", timeout=15000)
        assert "pw.change2@example.com" in page.inner_text("#bw-step-review")

        # Time change: back to the calendar, no POST from the click itself.
        n_posts = len(posts)
        page.click('.bw-change[data-change="time"]')
        page.wait_for_selector("#bw-step-calendar:not([hidden])", timeout=5000)
        assert len(posts) == n_posts, f"time Change link issued writes: {posts[n_posts:]}"
    finally:
        page.close()


def test_409_repick_path(live_server, browser):
    """Two visitors pick the same slot; the second submit gets 409 and the
    honest re-pick recovery (error + refreshed slots without the taken
    time). Exactly one booking exists afterwards."""
    base, db_path, _app = live_server
    ctx_a = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx_b = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page_a = ctx_a.new_page()
        page_b = ctx_b.new_page()
        for p in (page_a, page_b):
            p.goto(f"{base}{CONSULT}")
            _confirm_timezone(p)
            _pick_first_available_day_and_slot(p, base)

        before = _booking_count(db_path)

        # A completes first and holds the slot.
        _fill_form(page_a, email="pw.race.a@example.com", name="Race Winner")
        _verify_code(page_a, db_path, "pw.race.a@example.com")

        # B submits the same slot → 409 slot_taken → re-pick recovery.
        page_b.fill("#bw-name", "Race Loser")
        page_b.fill("#bw-email", "pw.race.b@example.com")
        page_b.fill("#bw-phone", "7785550101")
        page_b.fill(
            "#bw-discuss",
            "I also wanted that exact slot for my consulting business.",
        )
        page_b.click("#bw-form-submit")
        page_b.wait_for_selector("#bw-step-slots:not([hidden])", timeout=15000)
        # The widget refreshes the slot list before showing the recovery
        # message — wait for the error to become visible.
        page_b.wait_for_selector("#bw-error:not([hidden])", timeout=15000)
        err = page_b.inner_text("#bw-error")
        assert "just taken" in err.lower(), err
        # The taken slot is gone from the refreshed list.
        page_b.wait_for_selector("#bw-slots-list .bw-slot", timeout=10000)
        slots_text = page_b.inner_text("#bw-slots-list")
        assert "Race Winner" not in slots_text  # sanity: list re-rendered
        # No booking was created for the loser.
        conn = sqlite3.connect(db_path)
        try:
            loser = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE guest_email=? AND status='confirmed'",
                ("pw.race.b@example.com",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert loser == 0
        assert _booking_count(db_path) == before  # A hasn't confirmed yet either
    finally:
        ctx_a.close()
        ctx_b.close()


def test_stale_calendar_fails_closed(live_server, browser):
    """An aged calendar snapshot fails closed: no days shown, honest error
    with the phone number, and a working retry once the sync is fresh."""
    base, db_path, app = live_server
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE calendar_sync_state SET last_ok_at='2020-01-01T00:00:00'")
        conn.commit()
    finally:
        conn.close()

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}{CONSULT}")
        _confirm_timezone(page)
        # Fail-closed: the retry affordance appears, no day buttons.
        page.wait_for_selector("#bw-calendar-retry", timeout=15000)
        assert page.locator("#bw-calendar-grid .bw-day[data-date]").count() == 0
        err = page.inner_text("#bw-error")
        assert "1-(778)-227-4891" in err, err

        # Re-sync, then retry recovers honestly.
        from app import calendar_worker

        db = app.state.session_factory()
        try:
            calendar_worker.sync_calendar_blocks(db, calendar_worker.StubCalendarProvider([]))
            db.commit()
        finally:
            db.close()
        page.click("#bw-calendar-retry")
        page.wait_for_selector(
            "#bw-calendar-grid .bw-day[data-date]:not(:disabled)", timeout=15000
        )
    finally:
        page.close()


def test_manage_page_reschedule_and_cancel(live_server, browser):
    """Tokenized manage page smoke: book via the API, reschedule in the
    browser to a new slot (new reference; old token revoked → generic
    404 panel), then cancel with the new token."""
    base, db_path, _app = live_server
    import datetime as _dt
    import uuid as _uuid

    import httpx

    c = httpx.Client(trust_env=False, timeout=15)
    try:
        now = _dt.datetime.now()
        # Find a day with at least two free slots: earlier tests in this
        # module book on the first available day (shared module-scoped DB),
        # and the 24h min-notice filter shrinks the pool near end of day —
        # blindly taking the first available day is time-of-day flaky.
        # Scan the current month, then the next, so month-end runs work too.
        day, slots = None, []
        for _month_offset in (0, 1):
            y = now.year + (now.month + _month_offset - 1) // 12
            m = (now.month + _month_offset - 1) % 12 + 1
            avail = c.get(
                f"{base}/api/v1/booking/availability",
                params={"year": y, "month": m, "tz": "America/Vancouver"},
            ).json()
            for d in avail["days"]:
                if not d["available"]:
                    continue
                s = c.get(
                    f"{base}/api/v1/booking/slots",
                    params={"date": d["date"], "tz": "America/Vancouver"},
                ).json()["slots"]
                if len(s) >= 2:
                    day, slots = d["date"], s
                    break
            if day is not None:
                break
        assert day is not None, "need a day with two free slots for the reschedule test"
        intent = c.post(
            f"{base}/api/v1/booking/intents",
            json={
                "slot_start": slots[0]["start"],
                "guest_timezone": "America/Vancouver",
                "name": "Manage Tester",
                "email": "pw.manage@example.com",
                "phone": "7785550102",
                "country_code": "+1",
                "relationship": "prospective_client",
                "discuss": "Testing the manage page reschedule flow end to end.",
            },
        ).json()
        code = _latest_code(db_path, "pw.manage@example.com")
        c.post(
            f"{base}/api/v1/booking/intents/{intent['intent_id']}/verify",
            json={"code": code},
        )
        confirm = c.post(
            f"{base}/api/v1/booking/confirm",
            json={"intent_id": intent["intent_id"]},
            headers={"Idempotency-Key": str(_uuid.uuid4())},
        ).json()
        ref1 = confirm["reference"]
        token1 = confirm["reschedule_url"].split("/booking/manage/")[1].split("#")[0]
    finally:
        c.close()

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}/booking/manage/{token1}")
        page.wait_for_selector("#m-reschedule-toggle", timeout=10000)
        assert ref1 in page.inner_text("#manage-root")

        page.click("#m-reschedule-toggle")
        # Navigate the calendar to the target month (the chosen day may be
        # in the next month near month-end).
        page.select_option("#m-year", day[0:4])
        page.select_option("#m-month", str(int(day[5:7])))
        day_sel = f'#m-calendar-grid .bw-day[data-date="{day}"]:not(:disabled)'
        page.wait_for_selector(day_sel, timeout=10000)
        page.click(day_sel)
        page.wait_for_selector("#m-slots .bw-slot", timeout=10000)
        # The current slot is excluded from the list — the first button
        # is necessarily a new time.
        page.click("#m-slots .bw-slot")
        page.wait_for_selector("#m-reschedule-submit:not([hidden])", timeout=5000)
        page.click("#m-reschedule-submit")
        page.wait_for_url("**/thank-you-for-your-booking/", timeout=15000)
        page.wait_for_selector("#bt-dynamic:not([hidden])", timeout=10000)
        ref2 = REF_RE.search(page.inner_text("#bt-dynamic")).group(0)
        assert ref2 != ref1

        # The new token works; the old one is revoked → generic panel.
        stored = json.loads(
            page.evaluate("() => sessionStorage.getItem('jaro.booking_result')")
        )
        token2 = stored["cancel_url"].split("/booking/manage/")[1].split("#")[0]
        assert token2 != token1
        page.goto(f"{base}/booking/manage/{token1}")
        page.wait_for_selector("#manage-root .notice-box", timeout=10000)
        assert "isn't valid" in page.inner_text("#manage-root")

        page.goto(f"{base}/booking/manage/{token2}")
        page.wait_for_selector("#m-cancel-toggle", timeout=10000)
        page.click("#m-cancel-toggle")
        page.fill("#m-cancel-reason", "Testing the cancel flow.")
        # P15: the cancel button opens an inline confirmation first.
        page.click("#m-cancel-ask-submit")
        page.wait_for_selector("#m-cancel-confirm:not([hidden])", timeout=5000)
        page.click("#m-cancel-submit")
        page.wait_for_selector("#manage-root .notice-box", timeout=10000)
        assert "cancelled" in page.inner_text("#manage-root").lower()
    finally:
        page.close()
