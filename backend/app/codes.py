"""6-digit verification-code service (F5).

CSPRNG codes, 10-minute default TTL, SHA-256 digest storage, atomic
single-use consume under an advisory lock, 5 attempts then dead. One table
(``sms_codes``) serves booking verification, SMS 2FA fallback, and password
reset — the scope column + CHECK matrix keeps the shapes separate (B4).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from . import advisory
from .db import constant_time_compare, sha256_hex, utcnow, new_uuid7
from .models import SmsCode

CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5


def new_code() -> str:
    """Mint a 6-digit code from a CSPRNG (no modulo bias)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def code_digest(raw: str) -> str:
    return sha256_hex("jaro-code-v1", raw)


def target_hash(target: str) -> str:
    """PII-safe target reference (A6): the email/phone never sits raw in the row."""
    return sha256_hex("jaro-code-target-v1", target.strip().lower())


@dataclass
class CodeResult:
    ok: bool
    reason: str  # ok | invalid_code | code_expired | code_attempts_exceeded | code_consumed


def issue_code(
    db,
    *,
    scope: str,
    target: str,
    booking_id: str | None = None,
    user_id: str | None = None,
    ttl_minutes: int = CODE_TTL_MINUTES,
    max_attempts: int = CODE_MAX_ATTEMPTS,
    lock_backend: advisory.AdvisoryLockBackend | None = None,
    supersede_prior: bool = True,
) -> str:
    """Issue a code; returns the RAW code (the caller delivers it).

    Prior live codes for the same scope+target are superseded so only the
    newest code is valid — resend never leaves two live codes.
    """
    thash = target_hash(target)
    raw = new_code()
    now = utcnow()
    if supersede_prior:
        prior = db.execute(
            select(SmsCode).where(
                SmsCode.scope == scope,
                SmsCode.target_hash == thash,
                SmsCode.consumed_at.is_(None),
            )
        ).scalars()
        for row in prior:
            row.consumed_at = now
    db.add(
        SmsCode(
            id=new_uuid7(),
            scope=scope,
            target_hash=thash,
            booking_id=booking_id,
            user_id=user_id,
            code_hash=code_digest(raw),
            expires_at=now + timedelta(minutes=ttl_minutes),
            max_attempts=max_attempts,
        )
    )
    db.flush()
    return raw


def verify_code(
    db,
    *,
    scope: str,
    target: str,
    code: str,
    lock_backend: advisory.AdvisoryLockBackend,
) -> CodeResult:
    """Verify a code with advisory-lock atomic consume (F5).

    The lock key covers scope+target so concurrent guesses serialize; the
    newest live code is the only candidate. Wrong guesses increment
    attempts; the 5th wrong guess kills the code.
    """
    thash = target_hash(target)
    key = advisory.advisory_key("sms-code", scope, thash)
    now = utcnow()
    with lock_backend.acquire(db, key):
        row = (
            db.execute(
                select(SmsCode)
                .where(
                    SmsCode.scope == scope,
                    SmsCode.target_hash == thash,
                    SmsCode.consumed_at.is_(None),
                )
                .order_by(SmsCode.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return CodeResult(False, "code_consumed")
        if row.expires_at <= now:
            row.consumed_at = now
            db.flush()
            return CodeResult(False, "code_expired")
        if not constant_time_compare(code_digest(code.strip()), row.code_hash):
            row.attempts += 1
            if row.attempts >= row.max_attempts:
                row.consumed_at = now
                db.flush()
                # Security-critical: persist the kill even though the caller
                # will fail the request (see _consume_login_challenge for the
                # rationale — brute-force protection must survive rollback).
                db.commit()
                return CodeResult(False, "code_attempts_exceeded")
            db.flush()
            # Same rationale: the attempt counter must survive the caller's
            # rollback, or wrong guesses are free.
            db.commit()
            return CodeResult(False, "invalid_code")
        row.consumed_at = now
        db.flush()
        return CodeResult(True, "ok")
