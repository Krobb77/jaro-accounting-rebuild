"""DONE 7 — PII discipline (A6).

- Forced-error logs contain no email/phone/token/body.
- Access logs strip query strings.
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.logging_config import PIIRedactingFilter  # noqa: E402


def test_filter_redacts_pii():
    filt = PIIRedactingFilter()

    def run(msg, *args):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)
        filt.filter(record)
        return record.getMessage()

    assert "@" not in run("login failed for user jane.doe@example.com")
    assert "jane.doe@example.com" not in run("login failed for user jane.doe@example.com")
    assert "+1-778-227-4891" not in run("sms to +1-778-227-4891 failed")
    assert "token=abc123XYZ" not in run("verify token=abc123XYZ failed")
    assert "s3cret" not in run('body {"password": "s3cret", "code": "123456"}')
    assert "__Host-jaro_session=rawvalue" not in run("cookie __Host-jaro_session=rawvalue seen")
    assert "Bearer abc.def.ghi" not in run("auth Bearer abc.def.ghi rejected")
    # non-PII survives
    assert "rid=abc123" in run("request failed rid=abc123")


def test_forced_error_logs_have_no_pii(client, caplog):
    # Attach a capturing handler WITH the production filter to the app loggers.
    records = []

    class Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Cap()
    handler.addFilter(PIIRedactingFilter())
    for name in ("jaro.app", "jaro.app.access", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)  # pytest leaves root at WARNING; be explicit
    try:
        # Forced errors carrying PII in body and query string.
        client.post("/api/v1/portal/login",
                    json={"email": "jane.doe@example.com", "password": "s3cret-pw"})
        client.get("/api/v1/portal/dashboard?email=jane.doe@example.com&token=abc123")
        client.post("/api/v1/portal/login?phone=%2B17782274891", json={"email": "a@b.com"})
    finally:
        for name in ("jaro.app", "jaro.app.access", "uvicorn.access"):
            logging.getLogger(name).removeHandler(handler)

    blob = "\n".join(records)
    assert "jane.doe@example.com" not in blob
    assert "s3cret-pw" not in blob
    assert "17782274891" not in blob
    assert "token=abc123" not in blob


def test_access_log_strips_query_string(client):
    records = []

    class Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Cap()
    handler.addFilter(PIIRedactingFilter())
    access_logger = logging.getLogger("jaro.app.access")
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)  # pytest leaves root at WARNING; be explicit
    try:
        client.get("/api/v1/booking/availability?year=2026&month=10&email=jane.doe@example.com")
    finally:
        access_logger.removeHandler(handler)

    assert records, "expected an access log record"
    line = records[-1]
    assert "/api/v1/booking/availability" in line
    assert "year=2026" not in line
    assert "jane.doe@example.com" not in line
