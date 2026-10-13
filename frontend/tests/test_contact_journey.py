"""Phase-2 DONE-8 — Playwright contact journey (desktop + phone).

- Full journey: fill the wired contact form → submit → the success panel
  shows the reference + "queued" copy (F7 honest vocabulary, never "sent").
- Double-click submit → exactly one inquiry row server-side (C3/C4).
- Server-error recovery: a forced 500 renders the recovery copy with the
  request id + a Try again button; retrying with a fresh key succeeds and
  no field data is lost (C2).

Setup: a live FastAPI app (capture mail, disposable sqlite) serves BOTH
the API and dist/ from one origin — a test-only StaticFiles mount standing
in for the production nginx split (nginx serves dist/, proxies /api).
Chromium is reused from ~/workspace/pw-browsers/chromium-1243 (launched via
executable_path); nothing is re-downloaded.

Run: /home/hatch/workspace/jaroaccounting-rebuild/.venv/bin/python -m pytest frontend/tests/test_contact_journey.py
"""

from __future__ import annotations

import os
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
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.errors import ApiError  # noqa: E402

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

REF_RE = re.compile(r"MSG-\d{4}-[A-Z2-9]{6}")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Live app + dist on one origin. Yields (base_url, db_path, app)."""
    # Hermetic: rebuild dist from src/ so the journey never tests stale output.
    import subprocess

    result = subprocess.run(
        [sys.executable, str(FRONTEND / "build.py")],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"frontend build failed:\n{result.stdout}\n{result.stderr}"
    tmp = tmp_path_factory.mktemp("pw-contact")
    db_path = tmp / "pw.db"
    db_url = f"sqlite:///{db_path}"
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    alembic_command.upgrade(cfg, "head")

    settings = load_settings(
        {**BASE_TEST_ENV, "DATABASE_URL": db_url, "MAIL_CAPTURE_DIR": str(tmp / "mail")}
    )
    app = app_main.create_app(settings)

    @app.post("/api/v1/__test__/boom")
    def _boom():
        # Test-only 500 for the server-error recovery path.
        raise ApiError("internal_error")

    # Test-only stand-in for the nginx split: dist/ + /api on one origin.
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


def _inquiry_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    finally:
        conn.close()


def _fill(page, name="Playwright Tester", email="pw.tester@example.com", message=None):
    page.fill("#contact-name", name)
    page.fill("#contact-email", email)
    page.fill("#contact-message", message or "Hello from the Playwright journey. " * 8)


def test_contact_journey_desktop(live_server, browser):
    base, db_path, app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}/contact-us.html")
        page.wait_for_selector("#contact-form")
        before = _inquiry_count(db_path)
        _fill(page)
        page.click("#contact-form [type=submit]")
        page.wait_for_selector("#contact-success:not([hidden])", timeout=10000)
        panel = page.inner_text("#contact-success")
        assert "queued" in panel.lower()
        assert REF_RE.search(panel), panel
        # F7: the panel never claims the message was "sent".
        assert "was sent" not in panel.lower()
        assert "has been sent" not in panel.lower()
        assert _inquiry_count(db_path) == before + 1

        # End-to-end: run the worker in-process; both intents capture and
        # the receipt copy matches EMAIL_TEMPLATES §4.
        from app import outbox as outbox_lib

        worker = outbox_lib.OutboxWorker(app.state.session_factory, app.state.mail_backend)
        report = worker.run_cycle()
        assert report["done"] == 2
        mail_dir = Path(app.state.settings.mail_capture_dir)
        receipts = list(mail_dir.glob("*-attempt1.json"))
        assert len(receipts) == 2
        import json as _json

        subjects = [_json.loads(p.read_text())["subject"] for p in receipts]
        ref = REF_RE.search(panel).group(0)
        assert f"We received your message — ref {ref}" in subjects
        assert f"New website inquiry — {ref}" in subjects
    finally:
        page.close()


def test_contact_journey_phone(live_server, browser):
    base, db_path, _app = live_server
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        is_mobile=True,
        has_touch=True,
    )
    page = ctx.new_page()
    try:
        page.goto(f"{base}/contact-us.html")
        page.wait_for_selector("#contact-form")
        _fill(page, name="Phone Tester", email="pw.phone@example.com")
        page.tap("#contact-form [type=submit]")
        page.wait_for_selector("#contact-success:not([hidden])", timeout=10000)
        panel = page.inner_text("#contact-success")
        assert REF_RE.search(panel), panel
        assert "queued" in panel.lower()
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1, f"horizontal overflow on phone: {overflow}px"
    finally:
        page.close()
        ctx.close()


def test_double_click_submits_once(live_server, browser):
    base, db_path, _app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}/contact-us.html")
        page.wait_for_selector("#contact-form")
        before = _inquiry_count(db_path)
        _fill(page, name="Double Clicker", email="pw.double@example.com")
        # Two submit events in immediate succession — the realistic
        # double-submit vector (rapid Enters / programmatic submits bypass
        # the disabled button; the C3 busy guard must drop the second).
        page.evaluate(
            "() => { const f = document.getElementById('contact-form');"
            " f.requestSubmit(); f.requestSubmit(); }"
        )
        page.wait_for_selector("#contact-success:not([hidden])", timeout=10000)
        # Give any stray second request time to land, then count.
        page.wait_for_timeout(1500)
        assert _inquiry_count(db_path) == before + 1
    finally:
        page.close()


def test_server_error_recovery(live_server, browser):
    base, db_path, _app = live_server
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.goto(f"{base}/contact-us.html")
        page.wait_for_selector("#contact-form")
        before = _inquiry_count(db_path)
        _fill(page, name="Error Tester", email="pw.error@example.com",
              message="This submit will hit the forced 500 first.")
        # Point the form at the test-only 500 endpoint (data-endpoint is
        # the seam the recovery path is exercised through).
        page.evaluate(
            "() => document.getElementById('contact-form')"
            ".setAttribute('data-endpoint', '/api/v1/__test__/boom')"
        )
        page.click("#contact-form [type=submit]")
        page.wait_for_selector("#contact-error-summary:not([hidden])", timeout=10000)
        summary = page.inner_text("#contact-error-summary")
        assert "couldn't send" in summary
        assert "1-(778)-227-4891" in summary  # recovery copy: the phone number
        assert "mention this code" in summary  # request id for support
        retry = page.locator("#contact-retry")
        assert retry.count() == 1
        # Nothing was stored by the failed attempt; field data is intact.
        assert _inquiry_count(db_path) == before
        assert page.input_value("#contact-name") == "Error Tester"
        # Restore the real endpoint and retry: a fresh key, success.
        page.evaluate(
            "() => document.getElementById('contact-form')"
            ".setAttribute('data-endpoint', '/api/v1/inquiries')"
        )
        retry.click()
        page.wait_for_selector("#contact-success:not([hidden])", timeout=10000)
        panel = page.inner_text("#contact-success")
        assert REF_RE.search(panel), panel
        assert _inquiry_count(db_path) == before + 1
    finally:
        page.close()
