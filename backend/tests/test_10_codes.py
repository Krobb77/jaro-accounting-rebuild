"""DONE 10 — 6-digit code service (F5).

- 5 wrong attempts -> code dead (code_attempts_exceeded).
- Replay after consume -> rejected.
- Expired code -> rejected.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text as sa_text

from app import advisory, codes  # noqa: E402
from app.db import utcnow  # noqa: E402


def _locks():
    return advisory.ThreadingAdvisoryLockBackend()


def test_happy_path_and_replay_rejected(db):
    raw = codes.issue_code(db, scope="booking_verify", target="Guest@Example.com",
                           booking_id="b1", lock_backend=_locks())
    assert len(raw) == 6 and raw.isdigit()
    result = codes.verify_code(db, scope="booking_verify", target="guest@example.com",
                               code=raw, lock_backend=_locks())
    assert result.ok and result.reason == "ok"
    # Replay after consume -> rejected.
    again = codes.verify_code(db, scope="booking_verify", target="guest@example.com",
                              code=raw, lock_backend=_locks())
    assert not again.ok and again.reason == "code_consumed"


def test_five_wrong_attempts_kills_code(db):
    raw = codes.issue_code(db, scope="booking_verify", target="victim@example.com",
                           booking_id="b2", lock_backend=_locks())
    wrong = "000000" if raw != "000000" else "111111"
    reasons = []
    for _ in range(5):
        r = codes.verify_code(db, scope="booking_verify", target="victim@example.com",
                              code=wrong, lock_backend=_locks())
        reasons.append(r.reason)
    assert reasons[:4] == ["invalid_code"] * 4
    assert reasons[4] == "code_attempts_exceeded"
    # Even the right code is now dead.
    r = codes.verify_code(db, scope="booking_verify", target="victim@example.com",
                          code=raw, lock_backend=_locks())
    assert not r.ok


def test_expired_code_rejected(db):
    raw = codes.issue_code(db, scope="mfa_sms", target="+17782274891", user_id="u1",
                           ttl_minutes=10, lock_backend=_locks())
    # Backdate past the TTL.
    db.execute(
        sa_text("UPDATE sms_codes SET expires_at = :past"),
        {"past": utcnow() - timedelta(minutes=1)},
    )
    db.flush()
    r = codes.verify_code(db, scope="mfa_sms", target="+17782274891",
                          code=raw, lock_backend=_locks())
    assert not r.ok and r.reason == "code_expired"


def test_code_storage_is_digest_only(db):
    raw = codes.issue_code(db, scope="booking_verify", target="digest@example.com",
                           booking_id="b3", lock_backend=_locks())
    row = db.execute(
        sa_text("SELECT code_hash, target_hash FROM sms_codes")
    ).fetchall()
    blob = " ".join(f"{a} {b}" for a, b in row)
    assert raw not in blob
    assert "digest@example.com" not in blob  # target is hashed too (A6)


def test_resend_supersedes_prior_code(db):
    first = codes.issue_code(db, scope="booking_verify", target="re@example.com",
                             booking_id="b4", lock_backend=_locks())
    second = codes.issue_code(db, scope="booking_verify", target="re@example.com",
                              booking_id="b4", lock_backend=_locks())
    assert first != second
    r = codes.verify_code(db, scope="booking_verify", target="re@example.com",
                          code=first, lock_backend=_locks())
    assert not r.ok  # superseded
    r = codes.verify_code(db, scope="booking_verify", target="re@example.com",
                          code=second, lock_backend=_locks())
    assert r.ok
