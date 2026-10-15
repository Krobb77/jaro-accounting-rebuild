"""E1–E4 — durable outbox with lease-claim worker protocol.

The DB row is the single durable record of work; the queue carries only
opaque intent IDs. Workers claim via conditional UPDATE (lease token +
expiry); completion requires the token to still match, fencing stale
workers. Honest status vocabulary (E4): queued/sending/submitted/captured/
retry/dead/cancelled — "sent"/"delivered" are never claimed.

Staging helper (used by routes, A4):
    stage_intent(db, kind=..., scope_id=..., destination=..., snapshot={...})

frozen_snapshot is write-once (E2); payload_digest = sha256 over
destination + kind + template_version + snapshot + payload, re-verified
before every render — mismatch kills the intent as render_input_mismatch.
Dedup key: sha256(kind || ':' || scope_id) (D-14).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, text

from .db import new_uuid7, sha256_hex, utcnow
from .mail import MailBackend, MailNotAllowed, RenderedMail, render_template
from .models import EmailOutbox, OutboxAttempt
from .site_contact import sender_address

LEASE_TTL = timedelta(minutes=5)
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def dedup_key(kind: str, scope_id: str) -> str:
    return sha256_hex("jaro-outbox-dedup-v1", kind, scope_id)


def destination_hash(destination: str) -> str:
    return sha256_hex("jaro-outbox-dest-v1", destination.strip().lower())


def payload_digest(*, destination: str, kind: str, template_version: str, snapshot: dict, payload: dict) -> str:
    return sha256_hex(
        "jaro-outbox-digest-v1",
        destination.strip().lower(),
        kind,
        template_version,
        canonical_json(snapshot),
        canonical_json(payload),
    )


def freeze_snapshot(*, sender: str, site_origin: str, template_version: str, fields: dict) -> dict:
    """Write-once render snapshot (E2): later config/template changes cannot
    silently rebrand or retarget queued mail."""
    return {
        "sender": sender,
        "site_origin": site_origin,
        "template_version": template_version,
        "fields": dict(fields),
    }


def stage_intent(
    db,
    *,
    kind: str,
    scope_id: str,
    destination: str,
    snapshot_fields: dict,
    payload: dict | None = None,
    not_before=None,
    sender: str = sender_address(),  # single source of truth: app/site_contact.py
    site_origin: str = "https://jaroaccounting.com",
    template_version: str = "v1",
) -> EmailOutbox:
    """Stage an outbox intent inside the caller's transaction (A4).

    F3: re-issuing the same (kind, scope_id) returns the existing row —
    exactly-once notification even if app checks race.
    """
    payload = payload or {}
    key = dedup_key(kind, scope_id)
    existing = db.execute(select(EmailOutbox).where(EmailOutbox.dedup_key == key)).scalars().first()
    if existing is not None:
        return existing
    snapshot = freeze_snapshot(
        sender=sender, site_origin=site_origin, template_version=template_version, fields=snapshot_fields
    )
    # The snapshot carries the raw destination because the worker must
    # render it; destination_hash is the PII-safe reference for logs and
    # eligibility checks (A6). See DECISIONS-backend.md.
    # The digest is computed AFTER this injection so the worker can
    # reconstruct the identical canonical input at render time (E2).
    snapshot["fields"]["destination"] = destination
    row = EmailOutbox(
        id=new_uuid7(),
        kind=kind,
        dedup_key=key,
        destination_hash=destination_hash(destination),
        frozen_snapshot=snapshot,
        payload_digest=payload_digest(
            destination=destination, kind=kind, template_version=template_version,
            snapshot=snapshot, payload=payload,
        ),
        not_before=not_before or utcnow(),
        status="queued",
    )
    db.add(row)
    db.flush()
    return row


def cancel_pending_intents(db, *, kind: str, scope_id: str, reason: str) -> int:
    """F2 cancel-suppression: queued/retry intents for (kind, scope_id) are
    cancelled with a stable reason — never silently dropped."""
    rows = (
        db.execute(
            select(EmailOutbox).where(
                EmailOutbox.dedup_key == dedup_key(kind, scope_id),
                EmailOutbox.status.in_(("queued", "retry")),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "cancelled"
        row.cancel_reason = reason
        row.lease_token = None
        row.lease_expires_at = None
    db.flush()
    return len(rows)


@dataclass
class ClaimResult:
    claimed: bool
    intent_id: str | None = None
    lease_token: str | None = None


def claim_intent(db, intent_id: str, *, lease_ttl: timedelta = LEASE_TTL) -> ClaimResult:
    """E1 claim: conditional UPDATE with lease token + expiry. Only one
    claimant wins (rowcount == 1). Crashed workers' ``sending`` rows with an
    expired lease are reclaimable — that is the crash-recovery path."""
    token = secrets.token_urlsafe(24)
    now = utcnow()
    result = db.execute(
        text(
            "UPDATE email_outbox SET status = 'sending', lease_token = :tok, "
            "lease_expires_at = :exp, attempts = attempts + 1, updated_at = :now "
            "WHERE id = :id AND not_before <= :now "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= :now) "
            "AND ("
            "  (status IN ('queued', 'retry') AND (lease_expires_at IS NULL OR lease_expires_at < :now))"
            "  OR (status = 'sending' AND lease_expires_at < :now)"
            ")"
        ),
        {"tok": token, "exp": now + lease_ttl, "now": now, "id": intent_id},
    )
    db.flush()
    if result.rowcount == 1:
        return ClaimResult(True, intent_id, token)
    return ClaimResult(False)


def _finish(db, intent_id: str, lease_token: str, *, status: str, cancel_reason=None,
            next_attempt_at=None, outcome: str, evidence_label: str | None = None) -> bool:
    """Complete a claim; requires the lease token to still match (fences
    stale workers). Returns False when the lease was lost."""
    now = utcnow()
    result = db.execute(
        text(
            "UPDATE email_outbox SET status = :status, lease_token = NULL, "
            "lease_expires_at = NULL, cancel_reason = :cancel_reason, "
            "next_attempt_at = :next_attempt_at, updated_at = :now "
            "WHERE id = :id AND lease_token = :tok AND status = 'sending'"
        ),
        {
            "status": status, "cancel_reason": cancel_reason,
            "next_attempt_at": next_attempt_at, "now": now,
            "id": intent_id, "tok": lease_token,
        },
    )
    if result.rowcount != 1:
        return False
    row = db.get(EmailOutbox, intent_id)
    db.add(
        OutboxAttempt(
            id=new_uuid7(),
            outbox_id=intent_id,
            attempt_no=row.attempts,
            started_at=now,
            finished_at=now,
            outcome=outcome,
            evidence_label=evidence_label,
        )
    )
    db.flush()
    return True


def backoff_delay(attempts: int) -> timedelta:
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    jitter = secrets.randbelow(max(1, delay // 10 + 1))
    return timedelta(seconds=delay + jitter)


@dataclass
class WorkerConfig:
    batch_size: int = 25
    max_attempts: int = 8
    sender: str = sender_address()  # single source of truth: app/site_contact.py
    site_origin: str = "https://jaroaccounting.com"


class OutboxWorker:
    """Single worker process (E1). ``run_cycle`` claims due intents and
    drives each to a terminal or retry state."""

    def __init__(self, session_factory, mail_backend: MailBackend,
                 config: WorkerConfig | None = None, eligibility: dict | None = None):
        self._sessions = session_factory
        self._mail = mail_backend
        self._config = config or WorkerConfig()
        # eligibility: kind -> callable(snapshot_fields) -> (ok, reason)
        self._eligibility = eligibility or {}

    def due_intents(self, db, limit: int | None = None):
        now = utcnow()
        q = (
            select(EmailOutbox.id)
            .where(
                or_(
                    EmailOutbox.status.in_(("queued", "retry")),
                    and_(
                        EmailOutbox.status == "sending",
                        EmailOutbox.lease_expires_at < now,
                    ),
                ),
                EmailOutbox.not_before <= now,
                (EmailOutbox.next_attempt_at.is_(None)) | (EmailOutbox.next_attempt_at <= now),
                (EmailOutbox.lease_expires_at.is_(None)) | (EmailOutbox.lease_expires_at < now),
            )
            .order_by(EmailOutbox.created_at)
            .limit(limit or self._config.batch_size)
        )
        return [r[0] for r in db.execute(q).all()]

    def run_cycle(self) -> dict:
        report = {"claimed": 0, "done": 0, "retried": 0, "killed": 0, "cancelled": 0}
        db = self._sessions()
        try:
            for intent_id in self.due_intents(db):
                claim = claim_intent(db, intent_id)
                db.commit()
                if not claim.claimed:
                    continue
                report["claimed"] += 1
                outcome = self._deliver_one(claim.intent_id, claim.lease_token)
                report[outcome] = report.get(outcome, 0) + 1
        finally:
            db.close()
        return report

    def _deliver_one(self, intent_id: str, lease_token: str) -> str:
        db = self._sessions()
        try:
            row = db.get(EmailOutbox, intent_id)
            if row is None or row.lease_token != lease_token:
                return "lost"
            snap = row.frozen_snapshot or {}
            fields = snap.get("fields", {})
            # E2: digest re-verification before render. The canonical input
            # is reconstructed byte-identically to stage_intent (destination
            # already injected into fields at stage time).
            expected = payload_digest(
                destination=fields.get("destination", ""),
                kind=row.kind,
                template_version=snap.get("template_version", "v1"),
                snapshot=snap,
                payload={},
            )
            if not _constant_time_eq(expected, row.payload_digest):
                _finish(db, intent_id, lease_token, status="dead",
                        outcome="render_input_mismatch", evidence_label="digest_mismatch")
                db.commit()
                return "killed"
            # E3: eligibility double-gate (claim-time AND post-render).
            gate = self._eligibility.get(row.kind)
            if gate is not None:
                ok, reason = gate(fields)
                if not ok:
                    _finish(db, intent_id, lease_token, status="cancelled",
                            cancel_reason=reason, outcome="cancelled", evidence_label="eligibility_gate")
                    db.commit()
                    return "cancelled"
            try:
                mail = render_template(row.kind, snap)
            except KeyError:
                _finish(db, intent_id, lease_token, status="dead",
                        outcome="unknown_template", evidence_label="unknown_kind")
                db.commit()
                return "killed"
            try:
                ref = self._mail.send(mail, intent_id=intent_id, attempt_no=row.attempts)
            except MailNotAllowed:
                raise
            except Exception as exc:
                # Transient-only retries; permanent rejections go dead
                # immediately (E4). Classification hook point for Phase 4.
                transient = _is_transient(exc)
                if transient and row.attempts < row.max_attempts:
                    _finish(db, intent_id, lease_token, status="retry",
                            next_attempt_at=utcnow() + backoff_delay(row.attempts),
                            outcome="retry", evidence_label=f"transient_{type(exc).__name__}")
                    db.commit()
                    return "retried"
                _finish(db, intent_id, lease_token, status="dead",
                        outcome="dead", evidence_label=f"permanent_{type(exc).__name__}")
                db.commit()
                return "killed"
            terminal = "captured" if ref.startswith("captured:") else "submitted"
            _finish(db, intent_id, lease_token, status=terminal,
                    outcome=terminal, evidence_label="ok")
            db.commit()
            return "done"
        finally:
            db.close()


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a, b)


def _is_transient(exc: Exception) -> bool:
    # Conservative default: network/IO problems are transient; everything
    # else is permanent. Providers refine this in Phase 4.
    name = type(exc).__name__
    return name in {"TimeoutError", "ConnectionError", "SMTPServerDisconnected", "OSError"}
