"""Phase-5 portal domain: invitations, auth/2FA, terms, sessions helpers,
dashboard, documents, filings, invoices.

Route-owned transactions (A4): every function takes the caller's session
and never commits, rolls back, or touches the network — it stages rows,
outbox intents, and audit rows. Side effects become durable outbox rows
claimed by the background worker (E1).

Conventions reused unchanged: outbox (F2/F3), idempotency (A3, scope hash
``sha256(route || ':' || client_key)``), token service (256-bit CSPRNG,
SHA-256 digest storage), codes (F5), advisory locks, TOTP (D-36),
AES-256-GCM secretbox (D5), StorageBackend, session middleware.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from . import (
    advisory,
    codes,
    idempotency as idem,
    invoice_pdf,
    outbox as outbox_lib,
    passwords,
    secretbox,
    sessions,
    tokens,
    totp,
)
from .db import constant_time_compare, hmac_hex, new_uuid7, sha256_hex, utcnow
from .site_contact import SITE_CONTACT
from .errors import ApiError
from .models import (
    AuditLog,
    Filing,
    FilingKind,
    Invoice,
    InvoiceLine,
    InvoiceSeries,
    InvitationEvent,
    MfaChallenge,
    MfaRecoveryCode,
    OneShotMarker,
    PasswordReset,
    PortalDocument,
    PortalInvitation,
    PortalUser,
    Session,
    TermsAcceptance,
    TermsVersion,
)

# --- canonical constants (IMPLEMENTATION_PLAN §3.1 / AUTH) --------------------

INVITE_TTL_DAYS = 7  # coupled to {{EXPIRY_DAYS}} in the invitation email
RESET_TTL_HOURS = 24  # AUTH D4
MFA_CHALLENGE_TTL = timedelta(minutes=5)
MFA_MAX_ATTEMPTS = 5  # wrong TOTP guesses kill the challenge
SMS_CODE_TTL_MINUTES = 10
RECOVERY_CODE_COUNT = 10  # AUTH D41
PORTAL_NAME = "myJaro"  # D-29 default — parameterized, rename is one change
SITE_ORIGIN = "https://jaroaccounting.com"
FIRM_TZ = ZoneInfo("America/Vancouver")
SESSION_IDLE_SECONDS = 900
# Contact details: single source of truth in app/site_contact.py — update
# the phone / email / address there to change them everywhere at once.
OWNER_EMAIL = SITE_CONTACT["email"]
OWNER_PHONE_DISPLAY = SITE_CONTACT["phone_display"]

BOOTSTRAP_MARKER = "owner_bootstrap"

INVITE_LIVE = ("created", "emailed")
INVITE_TERMINAL = ("accepted", "expired", "revoked")

# --- env-overridable owner-question defaults ---------------------------------
# Small helpers so the owner-question constants below can be overridden
# per-deployment (JARO_* env vars) without a code edit. Malformed values
# fall back to the default — these are business defaults, not secrets, so
# fail-open-to-default is the right posture (contrast config.py secrets).


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# Owner-question decisions (D-27; IMPLEMENTATION_PLAN §3.5 #5): invoice
# defaults. Every value is env-overridable (JARO_INVOICE_*); the
# invoice_series DB row remains the runtime sequence (owner-editable via
# PUT /api/v1/admin/invoice-series), and this block documents the decided
# defaults it is seeded with.
INVOICE_NUMBER_PREFIX = _env_str("JARO_INVOICE_NUMBER_PREFIX", "JARO")
INVOICE_TERMS_DAYS = _env_int("JARO_INVOICE_TERMS_DAYS", 30)  # Net 30
INVOICE_TAX_RATE = _env_float("JARO_INVOICE_TAX_RATE", 0.05)  # BC: 5% GST on accounting services
INVOICE_TAX_RATE_BPS = int(round(INVOICE_TAX_RATE * 10000))  # 500 bps
INVOICE_TAX_LABEL = _env_str("JARO_INVOICE_TAX_LABEL", "GST")  # GST only — no PST is invented

# OWNER-REVIEWABLE (D-27; §3.5 #5): payment-instructions text shown on the
# invoice detail + PDF. Deliberately free of invented account numbers:
# e-transfer/cheque is the grounded current practice (PORTAL_V1 §6
# non-goal 3); Jared supplies specifics directly.
INVOICE_PAYMENT_INSTRUCTIONS = _env_str(
    "JARO_INVOICE_PAYMENT_INSTRUCTIONS",
    "Pay by e-transfer to " + SITE_CONTACT["email"] + " — please include your "
    "invoice number with your payment. Cheque also accepted; contact Jared "
    "Robbins at " + SITE_CONTACT["phone_display"] + " for details.",
)

# Owner-question decision (D-26; IMPLEMENTATION_PLAN §3.5 #14):
# Jared-supplied invite phones stay pre-marked verified (default False =
# keep the pre-marked path). Set JARO_INVITE_PHONE_REQUIRE_SMS_VERIFY=true
# to force the always-SMS-verify path instead. Rationale: personally-
# onboarded roster; owner input trusted.
INVITE_PHONE_REQUIRE_SMS_VERIFY = (
    os.environ.get("JARO_INVITE_PHONE_REQUIRE_SMS_VERIFY", "false") == "true"
)

# Conservative stdlib email check — the same shape as the contact pipeline's
# (routes/public.py); no email-validator in the venv. Defined here because
# no shared email-validation primitive exists in the repo (custom, noted).
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
)
_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")

# Precomputed dummy Argon2 hash for anti-enumeration (D-38): unknown emails
# burn the same hash cost as a real check, so timing is indistinguishable.
_DUMMY_PASSWORD_HASH = passwords.hash_password("dummy-" + secrets.token_hex(16))


# --- small helpers ------------------------------------------------------------


def normalize_email(value: str) -> str:
    """Lowercase + trim; raises 422 validation_failed on malformed input."""
    v = (value or "").strip().lower()
    if len(v) < 3 or len(v) > 254 or v.count("@") != 1 or not _EMAIL_RE.match(v):
        raise ApiError(
            "validation_failed",
            fields=[{"field": "email", "message": "Please enter a valid email address."}],
        )
    return v


def mask_email(email: str) -> str:
    """A6: unauthenticated surfaces show only first char + domain."""
    local, _, domain = email.partition("@")
    return f"{(local[:1] if local else '')}\u2022\u2022\u2022@{domain}"


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return f"\u2022\u2022\u2022\u2022{phone[-4:]}"


def validate_password_policy(password: str) -> list[dict]:
    """Length-first + blocklist (no composition rules). Returns A1 fields."""
    fields = []
    if len(password) < 12:
        fields.append(
            {
                "field": "password",
                "message": "password_too_short: use at least 12 characters.",
            }
        )
    if len(password) > 256:
        fields.append(
            {
                "field": "password",
                "message": "password_too_long: keep it to 256 characters or fewer.",
            }
        )
    if passwords.is_blocklisted(password):
        fields.append(
            {
                "field": "password",
                "message": "password_blocklisted: this password is too common. "
                "Please choose a less predictable one.",
            }
        )
    return fields


def check_password_policy(password: str) -> None:
    fields = validate_password_policy(password)
    if fields:
        raise ApiError("validation_failed", fields=fields)


def audit(
    db,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    request_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one audit row (B6: never updated/deleted by runtime roles).

    A6: ``detail`` must carry opaque IDs and counts only — never emails,
    token values, passwords, or message bodies.
    """
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=request_id,
            detail=detail or {},
        )
    )
    db.flush()


def ip_hash(secret_key: bytes, ip: str | None) -> str:
    """HMAC-pseudonymized client IP for audit rows (A5/A6)."""
    return hmac_hex(secret_key, "jaro-ip-v1", ip or "unknown")


def user_agent_summary(ua: str | None) -> str:
    """Short UA fingerprint for the client's own session inventory — enough
    to recognize a device, never the full string (A6)."""
    ua = (ua or "").strip()
    if not ua:
        return "unknown device"
    return ua[:80]


def _new_session(
    db,
    *,
    user_id: str,
    actor: str,
    request_id: str | None,
    ip: str | None,
    user_agent: str | None,
    secret_key: bytes,
) -> tuple[str, Session]:
    """Create a session (device summary stored); audit D-24 evictions.

    ``sessions.create_session`` enforces the 5-session cap with
    evict-oldest; when the cap was already full, the eviction is an audit
    event (DONE-6) — counted here from the pre-create live total.
    """
    live_before = (
        db.execute(
            select(func.count())
            .select_from(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        ).scalar()
        or 0
    )
    raw, row = sessions.create_session(
        db,
        user_id=user_id,
        ip_hash=ip_hash(secret_key, ip),
        user_agent_hash=hmac_hex(secret_key, "jaro-ua-v1", user_agent or "unknown"),
        user_agent_summary=user_agent_summary(user_agent),
    )
    if live_before >= sessions.MAX_CONCURRENT_SESSIONS:
        audit(
            db,
            actor=actor,
            action="session.evicted",
            entity_type="portal_user",
            entity_id=user_id,
            request_id=request_id,
            detail={
                "reason": "concurrent_cap",
                "live_before": live_before,
                "evicted": live_before + 1 - sessions.MAX_CONCURRENT_SESSIONS,
            },
        )
    return raw, row


def utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def vancouver_iso(dt: datetime | None) -> str | None:
    """Server-rendered America/Vancouver timestamp for portal surfaces."""
    if dt is None:
        return None
    return dt.astimezone(FIRM_TZ).isoformat()


def _find_user_by_email(db, email: str) -> PortalUser | None:
    return (
        db.execute(
            select(PortalUser).where(func.lower(PortalUser.email) == email.strip().lower())
        )
        .scalars()
        .first()
    )


# --- invitations --------------------------------------------------------------


def create_invitation(
    db,
    *,
    owner: PortalUser,
    client_name: str,
    email: str,
    phone_e164: str | None,
    note: str | None,
    temp_password: str,
    idem_key: str,
    request_id: str,
) -> dict:
    """Owner creates an invitation: row + event + outbox intent, one
    transaction (A4). The raw token is returned ONCE in the response and
    never persisted (B6) — the idempotency replay record deliberately
    excludes ``accept_url`` (unlike booking's manage URLs, a leaked invite
    replay record would mint a live credential)."""
    email = normalize_email(email)
    client_name = (client_name or "").strip()
    if not client_name:
        raise ApiError(
            "validation_failed",
            fields=[{"field": "client_name", "message": "Please enter the client's name."}],
        )
    # Jared sets the temporary password (communicated by Jared himself);
    # the client must replace it on first login.
    check_password_policy(temp_password)
    if phone_e164 and not _E164_RE.match(phone_e164.strip()):
        raise ApiError(
            "validation_failed",
            fields=[{"field": "phone_e164", "message": "Phone must be E.164, e.g. +17785550134."}],
        )
    payload = {
        "client_name": client_name,
        "email": email,
        "phone_e164": phone_e164 or "",
        "note": note or "",
    }
    payload_hash = idem.begin(db, route="admin.invitations.create", client_key=idem_key, payload=payload)

    existing = _find_user_by_email(db, email)
    if existing is not None and existing.role == "client":
        raise ApiError("client_exists")
    now = utcnow()
    user = PortalUser(
        id=new_uuid7(),
        role="client",
        email=email,
        display_name=client_name,
        password_hash=passwords.hash_password(temp_password),
        phone_e164=(phone_e164 or "").strip() or None,
        # No SMS verification anymore (authenticator removed); the number is
        # contact info only.
        phone_verified_at=None,
        status="active",
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    # The invitation row is kept as the creation record / audit trail and is
    # marked accepted immediately — there is no token accept flow anymore.
    # No outbox intent is staged: Jared emails the temporary password himself.
    row = PortalInvitation(
        id=new_uuid7(),
        email=email,
        client_name=client_name,
        phone_e164=(phone_e164 or "").strip() or None,
        note=(note or "").strip() or None,
        token_hash=tokens.token_digest(tokens.new_token(32)),
        status="accepted",
        created_by=owner.id,
        created_at=now,
        expires_at=now + timedelta(days=INVITE_TTL_DAYS),
        used_at=now,
    )
    db.add(row)
    db.flush()
    db.add(InvitationEvent(invite_id=row.id, event="created", actor="owner", at=now))
    db.add(InvitationEvent(invite_id=row.id, event="accepted", actor="owner", at=now))
    audit(
        db,
        actor=f"owner:{owner.id}",
        action="invite.created",
        entity_type="portal_invitation",
        entity_id=row.id,
        request_id=request_id,
        detail={
            "email_domain": email.split("@")[-1],
            "user_id": user.id,
            "idempotency_scope": idem.scope_hash("admin.invitations.create", idem_key),
        },
    )
    db.flush()
    stored = {
        "id": row.id,
        "email": email,
        "client_name": row.client_name,
        "status": "accepted",
        "user_id": user.id,
        "expires_at": utc_iso(row.expires_at),
    }
    idem.store(
        db,
        route="admin.invitations.create",
        client_key=idem_key,
        payload_hash=payload_hash,
        result=stored,
        status_code=201,
    )
    return stored


def db_get_invitation(db, invite_id: str) -> PortalInvitation:
    row = db.get(PortalInvitation, invite_id)
    if row is None:
        raise ApiError("invitation_not_found")
    return row


def db_get_client(db, client_id: str) -> PortalUser:
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    return user


def invite_projection(row: PortalInvitation) -> dict:    return {
        "id": row.id,
        "client_name": row.client_name,
        "email": row.email,
        "status": row.status,
        "created_at": utc_iso(row.created_at),
        "emailed_at": utc_iso(row.emailed_at),
        "accepted_at": utc_iso(row.used_at),
        "expires_at": utc_iso(row.expires_at),
        "revoked_at": utc_iso(row.revoked_at),
        "revoke_reason": row.revoke_reason,
        "superseded_by": row.superseded_by,
    }


def email_status_for_invite(db, invite_id: str) -> str:
    """Honest E4 vocabulary for the linked outbox intent (never 'sent')."""
    row = (
        db.execute(
            select(outbox_lib.EmailOutbox).where(
                outbox_lib.EmailOutbox.dedup_key == outbox_lib.dedup_key("invite", invite_id)
            )
        )
        .scalars()
        .first()
    )
    return row.status if row is not None else "none"


def invite_events_trail(db, invite_id: str) -> list[dict]:
    rows = (
        db.execute(
            select(InvitationEvent)
            .where(InvitationEvent.invite_id == invite_id)
            .order_by(InvitationEvent.at.asc())
        )
        .scalars()
        .all()
    )
    return [
        {"event": r.event, "actor": r.actor, "at": utc_iso(r.at), "note": r.note} for r in rows
    ]


def expire_invite_if_due(db, invite: PortalInvitation, *, actor: str = "system", request_id: str | None = None) -> bool:
    """Lazy expiry (AUTH §1.3): live + past expires_at → expired + event."""
    now = utcnow()
    if invite.status in INVITE_LIVE and invite.expires_at <= now:
        invite.status = "expired"
        db.add(InvitationEvent(invite_id=invite.id, event="expired", actor=actor, at=now, note="expired"))
        audit(
            db,
            actor=f"{actor}:{invite.id}",
            action="invite.expired",
            entity_type="portal_invitation",
            entity_id=invite.id,
            request_id=request_id,
        )
        db.flush()
        # The expiry transition must survive the caller's 410 (the route
        # rollbacks on ApiError). This is a terminal state transition, not
        # speculative work — persisting it is correct.
        db.commit()
        return True
    return False




def revoke_invitation(
    db, *, owner: PortalUser, invite_id: str, note: str | None, idem_key: str, request_id: str
) -> dict:
    """Revoke from created/emailed only. Revoking from created cancels the
    queued email intent (F2) — no stale invitation goes out."""
    row = db.get(PortalInvitation, invite_id)
    if row is None:
        raise ApiError("invitation_not_found")
    payload = {"invite_id": invite_id, "note": note or ""}
    payload_hash = idem.begin(db, route="admin.invitations.revoke", client_key=idem_key, payload=payload)
    if row.status not in INVITE_LIVE:
        raise ApiError("invitation_not_revocable")
    now = utcnow()
    row.status = "revoked"
    row.revoked_at = now
    row.revoke_reason = (note or "").strip()[:280] or None
    db.add(
        InvitationEvent(
            invite_id=row.id, event="revoked", actor="owner", at=now,
            note=row.revoke_reason,
        )
    )
    cancelled = outbox_lib.cancel_pending_intents(
        db, kind="invite", scope_id=row.id, reason="invitation_revoked"
    )
    audit(
        db,
        actor=f"owner:{owner.id}",
        action="invite.revoked",
        entity_type="portal_invitation",
        entity_id=row.id,
        request_id=request_id,
        detail={"intents_cancelled": cancelled},
    )
    db.flush()
    result = {"id": row.id, "status": "revoked", "revoked_at": utc_iso(row.revoked_at)}
    idem.store(
        db, route="admin.invitations.revoke", client_key=idem_key,
        payload_hash=payload_hash, result=result, status_code=200,
    )
    return result


def list_invitations(
    db, *, status: str | None, search: str | None, created_from: datetime | None,
    created_to: datetime | None, expiring_before: datetime | None,
    page: int, per_page: int,
) -> dict:
    q = select(PortalInvitation)
    if status:
        if status not in ("created", "emailed", "accepted", "expired", "revoked"):
            raise ApiError(
                "validation_failed",
                fields=[{"field": "status", "message": "Unknown invitation status."}],
            )
        q = q.where(PortalInvitation.status == status)
    if search:
        like = f"%{search.strip().lower()}%"
        q = q.where(
            or_(
                func.lower(PortalInvitation.email).like(like),
                func.lower(PortalInvitation.client_name).like(like),
            )
        )
    if created_from:
        q = q.where(PortalInvitation.created_at >= created_from)
    if created_to:
        q = q.where(PortalInvitation.created_at <= created_to)
    if expiring_before:
        q = q.where(
            PortalInvitation.expires_at <= expiring_before,
            PortalInvitation.status.in_(INVITE_LIVE),
        )
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = (
        db.execute(q.order_by(PortalInvitation.created_at.desc()).offset((page - 1) * per_page).limit(per_page))
        .scalars()
        .all()
    )
    return {
        "items": [invite_projection(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
    }


# --- advisory-lock helper (per-request backend from app.state) -----------------
# The threading backend is NOT cross-process on SQLite (documented); the
# route passes the shared app.state.lock_backend.

_advisory_backends: dict[int, object] = {}


def advisory_lock(db, key: int):
    """Advisory lock bound to the route-owned transaction.

    The backend instance is stashed on the session by the route layer
    (``portal.set_lock_backend``); Postgres uses pg_advisory_xact_lock
    (xact-scoped, released on commit), SQLite uses the shared threading
    registry from app.state.
    """
    backend = getattr(db, "_jaro_lock_backend", None)
    if backend is None:
        raise RuntimeError("lock backend not bound to session (route bug)")
    return backend.acquire(db, key)


def set_lock_backend(db, backend) -> None:
    setattr(db, "_jaro_lock_backend", backend)


# --- owner bootstrap + owner auth ---------------------------------------------

def bootstrap_owner(db, *, settings, email: str, initial_password: str, request_id: str) -> str:
    """One-shot owner bootstrap (D-03): the env secret is consumed exactly
    once. Second call → 410; absent/malformed secret → 503 (fail-closed)."""
    if db.get(OneShotMarker, BOOTSTRAP_MARKER) is not None:
        raise ApiError("bootstrap_consumed")
    secret = settings.owner_initial_password
    if not secret:
        raise ApiError("bootstrap_unavailable")
    email = normalize_email(email)
    if not constant_time_compare(initial_password, secret):
        # Generic auth failure (A6): no oracle on which check failed.
        audit(
            db, actor="system", action="bootstrap.failure",
            entity_type="portal_user", request_id=request_id, detail={},
        )
        raise ApiError("invalid_credentials")
    if (
        db.execute(select(PortalUser).where(PortalUser.role == "owner")).scalars().first()
        is not None
    ):
        raise ApiError("bootstrap_consumed")
    user = PortalUser(
        id=new_uuid7(),
        role="owner",
        email=email,
        display_name="Jared Robbins",
        password_hash=passwords.hash_password(initial_password),
        status="active",
    )
    db.add(user)
    db.add(OneShotMarker(name=BOOTSTRAP_MARKER, consumed_at=utcnow()))
    db.flush()
    audit(
        db, actor="system", action="bootstrap.completed",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={},
    )
    return user.id


def _issue_mfa_challenge(db, *, user: PortalUser, request_id: str) -> str:
    raw = tokens.new_token(32)
    db.add(
        MfaChallenge(
            id=tokens.token_digest(raw),
            user_id=user.id,
            expires_at=utcnow() + MFA_CHALLENGE_TTL,
            attempts=0,
        )
    )
    db.flush()
    audit(
        db, actor=f"owner:{user.id}" if user.role == "owner" else f"client:{user.id}",
        action="mfa.challenge_issued", entity_type="portal_user", entity_id=user.id,
        request_id=request_id, detail={},
    )
    return raw


def owner_login(
    db, *, email: str, password: str, request_id: str, ip: str | None,
    user_agent: str | None, secret_key: bytes,
) -> dict:
    """Owner login: email+password (anti-enumeration) → TOTP challenge
    MANDATORY, no SMS fallback (AUTH D1)."""
    user = _find_user_by_email(db, (email or "").strip().lower())
    user = user if (user is not None and user.role == "owner") else None
    ok = passwords.verify_password(user.password_hash, password) if user else False
    if user is None:
        # D-38: dummy hash keeps timing indistinguishable.
        passwords.verify_password(_DUMMY_PASSWORD_HASH, password or "x")
    if not ok or user is None or user.status != "active":
        audit(
            db, actor="system", action="login.failure", entity_type="portal_user",
            entity_id=user.id if user else None, request_id=request_id,
            detail={"ip_hash": ip_hash(secret_key, ip), "role": "owner"},
        )
        raise ApiError("invalid_credentials")
    user.failed_logins = 0
    user.last_login_at = utcnow()
    audit(
        db, actor=f"owner:{user.id}", action="login.success",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={"ip_hash": ip_hash(secret_key, ip)},
    )
    if user.totp_enrolled_at is None:
        # Provisional owner session: valid ONLY for TOTP enrollment until
        # the owner enrolls (require_owner enforces the gate).
        raw_session, _ = _new_session(
            db, user_id=user.id, actor=f"owner:{user.id}", request_id=request_id,
            ip=ip, user_agent=user_agent, secret_key=secret_key,
        )
        return {
            "next": "enroll_totp", "methods": ["totp"],
            "expires_in_seconds": int(MFA_CHALLENGE_TTL.total_seconds()),
            "_session_raw": raw_session,
        }
    challenge = _issue_mfa_challenge(db, user=user, request_id=request_id)
    return {
        "next": "verify_totp", "methods": ["totp"], "challenge_id": challenge,
        "expires_in_seconds": int(MFA_CHALLENGE_TTL.total_seconds()),
    }


def owner_verify_totp(
    db, *, challenge_id: str, code: str, request_id: str, ip: str | None,
    user_agent: str | None, secret_key: bytes, settings,
) -> dict:
    user = _consume_login_challenge(
        db, challenge_id=challenge_id, code=code, expected_role="owner",
        request_id=request_id, settings=settings,
    )
    raw_session, _ = _new_session(
        db, user_id=user.id, actor=f"owner:{user.id}", request_id=request_id,
        ip=ip, user_agent=user_agent, secret_key=secret_key,
    )
    audit(
        db, actor=f"owner:{user.id}", action="login.success",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={"via": "totp"},
    )
    return {"ok": True, "next": "admin", "session_expires_in_seconds": SESSION_IDLE_SECONDS,
            "_session_raw": raw_session}


# --- client login + 2FA ---------------------------------------------------------


def client_login(db, *, email: str, password: str, request_id: str, ip: str | None,
                 user_agent: str | None, secret_key: bytes) -> dict:
    """Password-only login (2026-10-15 refactor: the authenticator-app / SMS
    second step was removed — passwords only, per the owner's direction).

    Anti-enumeration credential check (D-38): unknown emails burn a dummy
    Argon2 verify; both failures return the identical 401.

    A client provisioned with a temporary password (``must_change_password``)
    gets a session but ``next: "change_password"`` — the ``client()``
    dependency 403s every portal route until the password is replaced.
    """
    email_n = (email or "").strip().lower()
    user = _find_user_by_email(db, email_n)
    user = user if (user is not None and user.role == "client") else None
    if user is None:
        passwords.verify_password(_DUMMY_PASSWORD_HASH, password or "x")
        audit(
            db, actor="system", action="login.failure", entity_type="portal_user",
            request_id=request_id, detail={"ip_hash": ip_hash(secret_key, ip)},
        )
        raise ApiError("invalid_credentials")
    if user.status in ("locked", "disabled") or not passwords.verify_password(
        user.password_hash, password or "x"
    ):
        user.failed_logins = (user.failed_logins or 0) + 1
        audit(
            db, actor=f"client:{user.id}", action="login.failure",
            entity_type="portal_user", entity_id=user.id, request_id=request_id,
            detail={"ip_hash": ip_hash(secret_key, ip)},
        )
        raise ApiError("invalid_credentials")
    user.failed_logins = 0
    user.last_login_at = utcnow()
    raw_session, _session_row = _new_session(
        db, user_id=user.id, actor=f"client:{user.id}", request_id=request_id,
        ip=ip, user_agent=user_agent, secret_key=secret_key,
    )
    audit(
        db, actor=f"client:{user.id}", action="login.success",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={},
    )
    db.flush()
    if user.must_change_password:
        next_step = "change_password"
    elif user.terms_accepted_at is None:
        next_step = "terms"
    else:
        next_step = "portal"
    return {
        "ok": True,
        "next": next_step,
        "session_expires_in_seconds": SESSION_IDLE_SECONDS,
        "_session_raw": raw_session,
    }


def change_client_password(db, *, user: PortalUser, new_password: str,
                           current_password: str | None, session_id: str,
                           request_id: str, ip: str | None,
                           user_agent: str | None, secret_key: bytes) -> dict:
    """Password change, two modes in one endpoint:

    - Forced (``must_change_password``): temporary-password onboarding or an
      owner password reset. The session itself proves the caller knew the
      previous password, so no current password is required.
    - Voluntary (in-settings): the current password is required.

    Both clear ``must_change_password``, rotate the session, and revoke all
    other sessions. Wrong current password → generic 401 (anti-enumeration).
    """
    forced = user.must_change_password
    if not forced:
        if not passwords.verify_password(user.password_hash, current_password or "x"):
            audit(db, actor=f"client:{user.id}", action="password.change_failure",
                  entity_type="portal_user", entity_id=user.id, request_id=request_id, detail={})
            raise ApiError("invalid_credentials")
    check_password_policy(new_password)
    if passwords.verify_password(user.password_hash, new_password):
        raise ApiError(
            "validation_failed",
            fields=[{"field": "new_password",
                     "message": "Choose a password different from your current one."}],
        )
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    user.failed_logins = 0
    # Revoke every session except the current one, then rotate the current.
    others = (
        db.execute(
            select(Session).where(Session.user_id == user.id,
                                 Session.revoked_at.is_(None),
                                 Session.id != session_id)
        ).scalars().all()
    )
    now = utcnow()
    for s in others:
        s.revoked_at = now
    current = db.get(Session, session_id)
    if current is not None:
        current.revoked_at = now
    raw_session, _ = _new_session(
        db, user_id=user.id, actor=f"client:{user.id}", request_id=request_id,
        ip=ip, user_agent=user_agent, secret_key=secret_key,
    )
    audit(
        db, actor=f"client:{user.id}", action="password.changed",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={"forced": forced, "other_sessions_revoked": len(others)},
    )
    db.flush()
    if user.terms_accepted_at is None:
        next_step = "terms"
    else:
        next_step = "portal"
    return {"ok": True, "next": next_step, "_session_raw": raw_session}


def _consume_login_challenge(db, *, challenge_id: str, code: str, expected_role: str,
                             request_id: str, settings) -> PortalUser:
    """Verify a TOTP code (or recovery code) against a challenge; consumes
    the challenge atomically (F5). 5 wrong codes kill the challenge."""
    digest = tokens.token_digest(challenge_id)
    with advisory_lock(db, advisory.advisory_key("mfa-challenge", digest)):
        ch = db.get(MfaChallenge, digest)
        if ch is None or not tokens.verify_digest(challenge_id, digest):
            raise ApiError("challenge_consumed")
        now = utcnow()
        if ch.consumed_at is not None:
            raise ApiError("challenge_consumed")
        if ch.expires_at <= now:
            ch.consumed_at = now
            db.flush()
            raise ApiError("challenge_consumed")
        user = db.get(PortalUser, ch.user_id)
        if user is None or user.role != expected_role or user.status not in ("active", "pending_first_login"):
            ch.consumed_at = now
            db.flush()
            raise ApiError("challenge_consumed")
        ok = False
        via = "totp"
        if user.totp_secret_enc:
            try:
                secret = secretbox.unseal(settings.totp_kek, bytes(user.totp_secret_enc)).decode("ascii")
            except secretbox.DecryptionFailed:
                secret = None
            if secret and totp.verify(secret, code or ""):
                ok = True
        if not ok and expected_role == "client":
            # Recovery codes (D-41): SHA-256 stored, single-use, shown once.
            for rc in (
                db.execute(
                    select(MfaRecoveryCode).where(
                        MfaRecoveryCode.user_id == user.id,
                        MfaRecoveryCode.used_at.is_(None),
                    )
                )
                .scalars()
                .all()
            ):
                if constant_time_compare(sha256_hex("jaro-recovery-v1", (code or "").strip()), rc.code_hash):
                    rc.used_at = now
                    ok = True
                    via = "recovery_code"
                    break
        if not ok:
            ch.attempts = (ch.attempts or 0) + 1
            if ch.attempts >= MFA_MAX_ATTEMPTS:
                ch.consumed_at = now
                audit(
                    db, actor=f"{expected_role}:{user.id}", action="mfa.challenge_consumed",
                    entity_type="portal_user", entity_id=user.id, request_id=request_id,
                    detail={"reason": "attempts_exceeded"},
                )
            else:
                audit(
                    db, actor=f"{expected_role}:{user.id}", action="mfa.failure",
                    entity_type="portal_user", entity_id=user.id, request_id=request_id,
                    detail={"via": "totp"},
                )
            db.flush()
            # Security-critical: the attempt counter MUST persist even though
            # this request fails (the route will rollback on the ApiError
            # below). Without this commit, brute-force protection is void —
            # an attacker would get unlimited guesses. This is a deliberate,
            # documented A4 exception: the challenge row is a security
            # counter, not business data.
            db.commit()
            raise ApiError("2fa_invalid")
        ch.consumed_at = now
        audit(
            db, actor=f"{expected_role}:{user.id}", action="mfa.success",
            entity_type="portal_user", entity_id=user.id, request_id=request_id,
            detail={"via": via},
        )
        db.flush()
        return user


def owner_totp_enroll(db, *, owner: PortalUser, settings, request_id: str) -> dict:
    if owner.totp_enrolled_at is not None:
        raise ApiError("totp_already_enrolled")
    secret = totp.generate_secret()
    owner.totp_secret_enc = secretbox.seal(settings.totp_kek, secret.encode("ascii"))
    db.flush()
    audit(db, actor=f"owner:{owner.id}", action="totp.enroll_started",
          entity_type="portal_user", entity_id=owner.id, request_id=request_id, detail={})
    return {
        "provisioning_uri": totp.provisioning_uri(secret, account=owner.email, issuer="Jaro Accounting (owner)"),
        "manual_key": secret,
    }


def owner_totp_confirm(db, *, owner: PortalUser, code: str, code2: str, settings, request_id: str) -> dict:
    if owner.totp_enrolled_at is not None:
        raise ApiError("totp_already_enrolled")
    if not owner.totp_secret_enc:
        raise ApiError("validation_failed",
                       fields=[{"field": "code", "message": "Start enrollment first."}])
    try:
        secret = secretbox.unseal(settings.totp_kek, bytes(owner.totp_secret_enc)).decode("ascii")
    except secretbox.DecryptionFailed:
        raise ApiError("2fa_invalid")
    if not totp.verify_enrollment_pair(secret, code or "", code2 or ""):
        raise ApiError("2fa_invalid")
    owner.totp_enrolled_at = utcnow()
    audit(db, actor=f"owner:{owner.id}", action="totp.enrolled",
          entity_type="portal_user", entity_id=owner.id, request_id=request_id, detail={})
    db.flush()
    return {"ok": True, "enrolled": True}


def reset_client_password(db, *, owner: PortalUser, client_id: str,
                          temp_password: str, reason: str, request_id: str) -> dict:
    """Owner-only password reset (2026-10-15 refactor; replaces the old TOTP
    reset). Jared sets a new temporary password — communicated by Jared
    himself — and the client must replace it on next login. All of the
    client's sessions are revoked so the old password stops working
    everywhere at once."""
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    reason = (reason or "").strip()
    if not reason:
        raise ApiError("validation_failed",
                       fields=[{"field": "reason", "message": "A reason is required."}])
    check_password_policy(temp_password)
    user.password_hash = passwords.hash_password(temp_password)
    user.must_change_password = True
    user.failed_logins = 0
    # The old password stops working everywhere at once: revoke sessions
    # and any live password-reset links (cf. revoke_all_client_sessions).
    sessions.revoke_all_sessions(db, user.id)
    for r in (
        db.execute(select(PasswordReset).where(
            PasswordReset.user_id == user.id, PasswordReset.status == "live"))
        .scalars().all()
    ):
        r.status = "revoked"
    audit(
        db, actor=f"owner:{owner.id}", action="password.reset_by_owner",
        entity_type="portal_user", entity_id=user.id, request_id=request_id,
        detail={"reason": reason[:280]},
    )
    db.flush()
    return {"ok": True, "must_change_password": True}


# --- phone enrollment (client) ---------------------------------------------------


def password_reset_request(db, *, email: str, request_id: str, ip: str | None, secret_key: bytes) -> dict:
    """Always 200 — no account-existence oracle (AUTH §4.5)."""
    email_n = (email or "").strip().lower()
    user = _find_user_by_email(db, email_n)
    user = user if (user is not None and user.role == "client") else None
    if user is not None and user.status in ("pending_first_login", "active"):
        raw = tokens.new_token(32)
        now = utcnow()
        row = PasswordReset(
            id=new_uuid7(), user_id=user.id, token_hash=tokens.token_digest(raw),
            status="live", created_at=now, expires_at=now + timedelta(hours=RESET_TTL_HOURS),
        )
        db.add(row)
        db.flush()
        outbox_lib.stage_intent(
            db, kind="password_reset", scope_id=row.id, destination=user.email,
            snapshot_fields={
                "first_name": (user.display_name or "").split()[0] if user.display_name else "",
                "client_email": user.email,
                "reset_url": f"{SITE_ORIGIN}/portal/reset-password?token={raw}",
                "expiry_hours": str(RESET_TTL_HOURS),
            },
        )
        audit(
            db, actor=f"client:{user.id}", action="password.reset_requested",
            entity_type="password_reset", entity_id=row.id, request_id=request_id,
            detail={"ip_hash": ip_hash(secret_key, ip)},
        )
        db.flush()
    else:
        # Unknown email: burn equivalent work, reveal nothing (A6).
        tokens.new_token(32)
        audit(
            db, actor="system", action="password.reset_requested",
            entity_type="password_reset", request_id=request_id,
            detail={"ip_hash": ip_hash(secret_key, ip), "unknown_account": True},
        )
        db.flush()
    return {"ok": True}


def _live_reset(db, raw_token: str) -> PasswordReset | None:
    digest = tokens.token_digest(raw_token)
    row = (
        db.execute(select(PasswordReset).where(PasswordReset.token_hash == digest))
        .scalars().first()
    )
    if row is None or not tokens.verify_digest(raw_token, row.token_hash):
        return None
    if row.status == "live" and row.expires_at <= utcnow():
        row.status = "expired"
        db.flush()
        return None
    return row if row.status == "live" else None


def password_reset_verify(db, *, raw_token: str) -> dict:
    row = _live_reset(db, raw_token)
    if row is None:
        raise ApiError("reset_invalid")
    user = db.get(PortalUser, row.user_id)
    return {"ok": True, "email_masked": mask_email(user.email if user else "")}


def password_reset_complete(
    db, *, raw_token: str, password: str, idem_key: str, request_id: str,
    ip: str | None, secret_key: bytes,
) -> dict:
    """Atomic consume → new password → ALL sessions revoked (compromise-safe).
    No session is minted: the client logs in fresh with full 2FA."""
    check_password_policy(password)
    digest = tokens.token_digest(raw_token)
    with advisory_lock(db, advisory.advisory_key("reset-consume", digest)):
        row = _live_reset(db, raw_token)
        if row is None:
            raise ApiError("reset_invalid")
        payload = {"token_digest": digest, "password_sha": sha256_hex("jaro-pw-v1", password)}
        payload_hash = idem.begin(
            db, route="portal.password_reset.complete", client_key=idem_key, payload=payload
        )
        user = db.get(PortalUser, row.user_id)
        if user is None or user.status not in ("pending_first_login", "active"):
            raise ApiError("reset_invalid")
        now = utcnow()
        user.password_hash = passwords.hash_password(password)
        user.failed_logins = 0
        row.status = "used"
        row.used_at = now
        revoked = sessions.revoke_all_sessions(db, user.id)
        audit(
            db, actor=f"client:{user.id}", action="password.reset_completed",
            entity_type="password_reset", entity_id=row.id, request_id=request_id,
            detail={"sessions_revoked": revoked, "ip_hash": ip_hash(secret_key, ip)},
        )
        db.flush()
        result = {"ok": True, "next": "login"}
        idem.store(
            db, route="portal.password_reset.complete", client_key=idem_key,
            payload_hash=payload_hash, result=result, status_code=200,
        )
        return result




def list_password_resets(db, *, status: str | None, page: int, per_page: int) -> dict:
    q = select(PasswordReset)
    if status:
        if status not in ("live", "used", "expired", "revoked"):
            raise ApiError("validation_failed",
                           fields=[{"field": "status", "message": "Unknown reset status."}])
        q = q.where(PasswordReset.status == status)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = (
        db.execute(q.order_by(PasswordReset.created_at.desc())
                   .offset((page - 1) * per_page).limit(per_page)).scalars().all()
    )
    items = []
    for r in rows:
        user = db.get(PortalUser, r.user_id)
        items.append({
            "id": r.id, "client_id": r.user_id,
            "email": user.email if user else None,
            "requested_at": utc_iso(r.created_at), "status": r.status,
            "completed_at": utc_iso(r.used_at),
        })
    return {"items": items, "page": page, "per_page": per_page, "total": total}


# --- terms gate ----------------------------------------------------------------------


def terms_current(db) -> dict:
    row = (
        db.execute(select(TermsVersion).order_by(TermsVersion.published_at.desc()))
        .scalars().first()
    )
    if row is None:
        raise ApiError("not_found")
    return {"version": row.version, "version_hash": row.version_hash,
            "title": row.title, "body_html": row.body_html}


def terms_accept(db, *, user: PortalUser, version_hash: str, request_id: str) -> dict:
    current = terms_current(db)
    if not constant_time_compare(version_hash or "", current["version_hash"]):
        raise ApiError("version_mismatch")
    now = utcnow()
    existing = db.get(TermsAcceptance, {"user_id": user.id, "version": current["version"]})
    if existing is None:
        db.add(TermsAcceptance(user_id=user.id, version=current["version"],
                               version_hash=current["version_hash"], accepted_at=now))
    user.terms_accepted_at = now
    user.terms_version_hash = current["version_hash"]
    audit(db, actor=f"client:{user.id}", action="terms.accepted",
          entity_type="portal_user", entity_id=user.id, request_id=request_id,
          detail={"version": current["version"], "version_hash": current["version_hash"]})
    db.flush()
    return {"ok": True, "accepted_at": utc_iso(now)}


# --- sessions (client self-service) -----------------------------------------------------


def session_status_payload(db, view: sessions.SessionView, user: PortalUser) -> dict:
    return {
        "ok": True,
        "email": user.email,
        "name": user.display_name,
        "expires_in_seconds": sessions.expires_in_ms(view) // 1000,
        "absolute_expires_in_seconds": max(
            0, int((view.absolute_expires_at - utcnow()).total_seconds())
        ),
        "terms_pending": user.terms_accepted_at is None,
        "must_change_password": user.must_change_password,
    }


def extend_session(db, *, view: sessions.SessionView, user: PortalUser) -> dict:
    """Idempotent one-click extend (D-44): at most one effective extend per
    60s (the touch-throttle interval doubles as the extend throttle — no new
    column); cannot resurrect (the middleware never attaches an expired
    session, and we re-check here)."""
    row = db.get(Session, view.id)
    now = utcnow()
    if row is None or row.revoked_at is not None or now > row.absolute_expires_at:
        raise ApiError("session_expired")
    if now - row.last_seen_at > sessions.SESSION_IDLE:
        raise ApiError("session_expired")
    if now - row.last_seen_at >= sessions.TOUCH_THROTTLE:
        row.last_seen_at = now
        db.flush()
    fresh = sessions.SessionView(
        id=row.id, user_id=row.user_id, mfa_verified_at=row.mfa_verified_at,
        created_at=row.created_at, last_seen_at=row.last_seen_at,
        absolute_expires_at=row.absolute_expires_at,
    )
    return {"ok": True, "expires_in_seconds": sessions.expires_in_ms(fresh) // 1000}


def session_inventory(db, *, user: PortalUser, current_session_id: str) -> dict:
    rows = (
        db.execute(
            select(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None))
            .order_by(Session.created_at.desc())
        ).scalars().all()
    )
    items = []
    for r in rows:
        items.append({
            "id": r.id[:12] + "\u2026",  # A6: the client UI never sees full session IDs
            "created_at": utc_iso(r.created_at),
            "last_seen_at": utc_iso(r.last_seen_at),
            "user_agent_summary": r.user_agent_summary or "unknown device",
            "current": r.id == current_session_id,
        })
    return {"items": items}


def revoke_own_session(db, *, user: PortalUser, session_id: str, current_session_id: str,
                       request_id: str) -> None:
    if session_id == current_session_id:
        raise ApiError("cannot_revoke_current")
    row = db.get(Session, session_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise ApiError("session_not_found")
    row.revoked_at = utcnow()
    audit(db, actor=f"client:{user.id}", action="session.revoked",
          entity_type="session", entity_id=row.id, request_id=request_id, detail={})
    db.flush()


def logout(db, *, view: sessions.SessionView, user: PortalUser, request_id: str) -> None:
    row = db.get(Session, view.id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
    audit(db, actor=f"client:{user.id}", action="session.revoked",
          entity_type="session", entity_id=view.id, request_id=request_id,
          detail={"reason": "logout"})
    db.flush()


def revoke_all_client_sessions(db, *, owner: PortalUser, client_id: str, request_id: str) -> dict:
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    count = sessions.revoke_all_sessions(db, user.id)
    # A revoke-all also kills live password-reset links (AUTH §4.5).
    resets = (
        db.execute(select(PasswordReset).where(
            PasswordReset.user_id == user.id, PasswordReset.status == "live"))
        .scalars().all()
    )
    for r in resets:
        r.status = "revoked"
    audit(db, actor=f"owner:{owner.id}", action="session.revoked_all",
          entity_type="portal_user", entity_id=user.id, request_id=request_id,
          detail={"sessions_revoked": count, "resets_revoked": len(resets)})
    db.flush()
    return {"revoked_count": count}


# --- F1 dashboard --------------------------------------------------------------------------


def _user_entity_ids(db, user_id: str) -> dict[str, set[str]]:
    doc_ids = {r[0] for r in db.execute(
        select(PortalDocument.id).where(PortalDocument.user_id == user_id)).all()}
    filing_ids = {r[0] for r in db.execute(
        select(Filing.id).where(Filing.user_id == user_id)).all()}
    invoice_ids = {r[0] for r in db.execute(
        select(Invoice.id).where(Invoice.user_id == user_id)).all()}
    return {"document": doc_ids, "filing": filing_ids, "invoice": invoice_ids}


_ACTIVITY_SUMMARIES = {
    "invite.accepted": "Invitation accepted — account created.",
    "terms.accepted": "Engagement terms accepted.",
    "totp.enrolled": "Two-step verification set up.",
    "password.changed": "Password changed.",
    "password.reset_completed": "Password reset completed.",
    "document.uploaded": "Document uploaded.",
    "document.shared": "Jaro Accounting shared a document with you.",
    "document.retired": "A document was retired.",
    "document.unretired": "A document was restored.",
    "filing.created": "A new filing was opened.",
    "filing.status_changed": "Filing status changed.",
    "invoice.issued": "A new invoice was issued.",
    "invoice.paid": "An invoice was marked paid.",
    "invoice.voided": "An invoice was voided.",
    "login.success": "Signed in.",
    "session.evicted": "An older sign-in was replaced by a newer one.",
}


def _actor_label(actor: str, user_id: str) -> str:
    if actor == f"client:{user_id}":
        return "You"
    if actor.startswith("owner:"):
        return "Jared Robbins"
    return "System"


def dashboard(db, *, user: PortalUser, request_id: str) -> dict:
    now = utcnow()
    last_visit = user.last_portal_visit_at
    kind_labels = {r.code: r.label for r in db.execute(select(FilingKind)).scalars().all()}

    filings = (
        db.execute(select(Filing).where(Filing.user_id == user.id)
                   .order_by(Filing.tax_year.desc(), Filing.created_at.desc()))
        .scalars().all()
    )
    invoices = (
        db.execute(select(Invoice).where(Invoice.user_id == user.id)
                   .order_by(Invoice.issued_at.desc())).scalars().all()
    )
    documents = (
        db.execute(select(PortalDocument).where(
            PortalDocument.user_id == user.id, PortalDocument.deleted_at.is_(None))
            .order_by(PortalDocument.created_at.desc())).scalars().all()
    )
    new_docs = [d for d in documents
                if d.uploaded_by == "owner" and (last_visit is None or d.created_at > last_visit)]

    needs_attention = {
        "filings": [
            {"id": f.id, "kind_code": f.kind_code,
             "kind_label": kind_labels.get(f.kind_code, f.kind_code),
             "tax_year": f.tax_year, "note": f.note,
             "status_changed_at": vancouver_iso(f.status_changed_at)}
            for f in filings if f.status == "awaiting_info"
        ],
        "invoices": [
            {"id": i.id, "invoice_number": i.invoice_number,
             "total_cents": i.total_cents, "due_at": vancouver_iso(i.due_at)}
            for i in invoices if i.status == "issued" and i.due_at < now
        ],
        "documents": [
            {"id": d.id, "original_filename": d.original_filename,
             "created_at": vancouver_iso(d.created_at)}
            for d in new_docs
        ],
    }
    open_invoices = [i for i in invoices if i.status == "issued"]
    snapshots = {
        "filings": [
            {"kind_label": kind_labels.get(f.kind_code, f.kind_code),
             "tax_year": f.tax_year, "status": f.status}
            for f in filings[:5]
        ],
        "documents": {"count": len(documents), "new_count": len(new_docs)},
        "invoices": {
            "open_count": len(open_invoices),
            "total_owing_cents": sum(i.total_cents for i in open_invoices),
        },
    }

    entity_ids = _user_entity_ids(db, user.id)
    wanted = []
    for etype, ids in entity_ids.items():
        if ids:
            wanted.append((AuditLog.entity_type == etype) & (AuditLog.entity_id.in_(ids)))
    wanted.append((AuditLog.entity_type == "portal_user") & (AuditLog.entity_id == user.id))
    feed_rows = (
        db.execute(select(AuditLog).where(or_(*wanted))
                   .order_by(AuditLog.occurred_at.desc()).limit(20)).scalars().all()
    )
    activity = [
        {"action": r.action, "at": vancouver_iso(r.occurred_at),
         "actor": _actor_label(r.actor, user.id),
         "summary": _ACTIVITY_SUMMARIES.get(r.action, r.action.replace(".", " ").replace("_", " ") + ".")}
        for r in feed_rows
    ]

    # PORTAL_V1 §3: the "new since last visit" marker clears on view.
    user.last_portal_visit_at = now
    db.flush()

    return {
        "greeting": f"Welcome back, {user.display_name or 'there'}",
        "entity_name": user.display_name,
        "needs_attention": needs_attention,
        "snapshots": snapshots,
        "activity": activity,
    }


# --- F2 document vault ---------------------------------------------------------------

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # PORTAL_V1 §7 #2

# Owner-question decision (D-57; IMPLEMENTATION_PLAN §3.5 #17): the upload
# extension allowlist. Env-overridable (JARO_UPLOAD_ALLOWED_EXTENSIONS, a
# comma-separated list) so Jared can extend it without a code edit;
# magic-byte verification below still applies to every added extension.
UPLOAD_ALLOWED_EXTENSIONS = frozenset(
    ext.strip().lower()
    for ext in os.environ.get(
        "JARO_UPLOAD_ALLOWED_EXTENSIONS",
        "pdf,jpg,jpeg,png,doc,docx,xls,xlsx,csv,zip",
    ).split(",")
    if ext.strip()
)

# Magic-byte signatures (extension is NEVER trusted — PORTAL_V1 F2).
_MAGIC = {
    "pdf": (b"%PDF-",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "zip": (b"PK\x03\x04",),
}
_EXT_MIME = {
    "pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv", "zip": "application/zip",
}


def validate_upload(filename: str, data: bytes) -> tuple[str, str]:
    """Extension allowlist + magic-byte verification. Returns (ext, mime).

    HTML/SVG/JS are never allowed (no extension for them exists in the
    allowlist, and the magic check would reject them anyway).
    """
    name = (filename or "").strip()
    if "." not in name:
        raise ApiError("document_rejected")
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in UPLOAD_ALLOWED_EXTENSIONS:
        raise ApiError(
            "document_rejected",
            fields=[{"field": "file", "message": f"Files of type .{ext} can't be uploaded."}],
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError("file_too_large")
    if not data:
        raise ApiError("document_rejected",
                       fields=[{"field": "file", "message": "The file is empty."}])
    sigs = _MAGIC.get(ext)
    if sigs is not None:
        if not any(data.startswith(s) for s in sigs):
            # A renamed executable (e.g. malware.pdf with an MZ header)
            # fails here — the extension is never trusted.
            raise ApiError(
                "document_rejected",
                fields=[{"field": "file",
                         "message": "This file's contents don't match its type."}],
            )
    else:  # csv: must be genuine text, never markup or binary
        if b"\x00" in data:
            raise ApiError("document_rejected",
                           fields=[{"field": "file", "message": "This file's contents don't match its type."}])
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError("document_rejected",
                           fields=[{"field": "file", "message": "This file's contents don't match its type."}])
        head = text.lstrip()[:64].lower()
        if head.startswith("<!doctype") or head.startswith("<html") or "<script" in head[:400].lower():
            raise ApiError("document_rejected",
                           fields=[{"field": "file", "message": "HTML files can't be uploaded."}])
    return ext, _EXT_MIME[ext]


def document_projection(row: PortalDocument, kind_label: str | None = None) -> dict:
    return {
        "id": row.id,
        "original_filename": row.original_filename,
        "byte_size": row.byte_size,
        "mime": row.mime,
        "type_code": row.type_code,
        "type_label": kind_label,
        "uploaded_by": row.uploaded_by,
        "label": row.label,
        # D-46: no AV in v1 — the UI must never claim "scanned".
        "scan_status": row.scan_status,
        "created_at": utc_iso(row.created_at),
    }


def _document_type_label(db, type_code: str) -> str:
    from .models import DocumentType

    row = db.get(DocumentType, type_code)
    if row is None:
        raise ApiError(
            "validation_failed",
            fields=[{"field": "type_code", "message": "Unknown document category."}],
        )
    return row.label


def store_document(
    db, *, user: PortalUser, uploaded_by: str, filename: str, data: bytes,
    type_code: str, label: str | None, idem_key: str, request_id: str,
    storage, actor: PortalUser,
) -> dict:
    ext, mime = validate_upload(filename, data)
    type_label = _document_type_label(db, type_code)
    if label is not None and len(label) > 500:
        raise ApiError("validation_failed",
                       fields=[{"field": "label", "message": "Keep the note to 500 characters or fewer."}])
    sha = hashlib.sha256(data).hexdigest()
    payload = {
        "user_id": user.id, "filename": (filename or "").strip(), "sha256": sha,
        "byte_size": len(data), "type_code": type_code, "label": label or "",
        "uploaded_by": uploaded_by,
    }
    route = "portal.documents.upload" if uploaded_by == "client" else "admin.documents.upload"
    payload_hash = idem.begin(db, route=route, client_key=idem_key, payload=payload)
    # Per-client isolation: bytes live under the client's own directory;
    # the name is a UUID (unguessable); the row re-checks ownership.
    object_key = storage.put(data, subdir=user.id, suggested_name=f"{ext}")
    row = PortalDocument(
        id=new_uuid7(), user_id=user.id, type_code=type_code,
        original_filename=(filename or "").strip()[:255],
        object_key=object_key, byte_size=len(data), sha256=sha, mime=mime,
        uploaded_by=uploaded_by, label=(label or "").strip() or None,
        scan_status="unscanned",
    )
    db.add(row)
    db.flush()
    now = utcnow()
    if uploaded_by == "client":
        outbox_lib.stage_intent(
            db, kind="document_received", scope_id=row.id, destination=OWNER_EMAIL,
            snapshot_fields={
                "client_name": user.display_name or "",
                "client_email": user.email,
                "filename": row.original_filename,
                "category": type_label,
                "note_block": f"- Note: {row.label}\n" if row.label else "",
                "uploaded_at": vancouver_iso(now) or "",
            },
        )
        action = "document.uploaded"
    else:
        outbox_lib.stage_intent(
            db, kind="document_shared", scope_id=row.id, destination=user.email,
            snapshot_fields={
                "first_name": (user.display_name or "").split()[0] if user.display_name else "",
                "filename": row.original_filename,
                "category": type_label,
            },
        )
        action = "document.shared"
    audit(
        db, actor=f"{'owner' if actor.role == 'owner' else 'client'}:{actor.id}",
        action=action, entity_type="document", entity_id=row.id,
        request_id=request_id,
        detail={"byte_size": len(data), "uploaded_by": uploaded_by,
                "idempotency_scope": idem.scope_hash(route, idem_key)},
    )
    db.flush()
    result = document_projection(row, type_label)
    idem.store(db, route=route, client_key=idem_key, payload_hash=payload_hash,
               result=result, status_code=201)
    return result


def list_documents(db, *, user: PortalUser, include_retired: bool = False) -> dict:
    from .models import DocumentType

    labels = {r.code: r.label for r in db.execute(select(DocumentType)).scalars().all()}
    q = select(PortalDocument).where(PortalDocument.user_id == user.id)
    if not include_retired:
        q = q.where(PortalDocument.deleted_at.is_(None))
    rows = db.execute(q.order_by(PortalDocument.created_at.desc())).scalars().all()
    return {"items": [document_projection(r, labels.get(r.type_code)) for r in rows]}


def get_document_bytes(db, *, user: PortalUser, document_id: str, storage,
                       request_id: str, actor: PortalUser) -> tuple[bytes, PortalDocument]:
    """Session-gated download: ownership re-checked (not just list-filtered);
    SHA-256 re-verified on every read."""
    row = db.get(PortalDocument, document_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        # Owner console may fetch retired docs via the admin path; the
        # client path never serves retired rows.
        if row is None or row.user_id != user.id:
            audit(
                db, actor=f"{'owner' if actor.role == 'owner' else 'client'}:{actor.id}",
                action="document.download_denied", entity_type="document",
                entity_id=document_id, request_id=request_id, detail={},
            )
            # The denial audit must survive the route's rollback on the 403
            # below (security trail); the session holds only this row.
            db.commit()
            # 403 (not 404): the criterion requires an explicit denial signal
            # for cross-client attempts; the UUID is unguessable so the
            # existence oracle is negligible.
            raise ApiError("forbidden")
        raise ApiError("document_not_found")
    try:
        data = storage.get(row.object_key)
    except Exception:
        raise ApiError("internal_error")
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise ApiError("internal_error")
    return data, row


def get_document_bytes_owner(db, *, user: PortalUser, document_id: str, storage,
                             owner: PortalUser, request_id: str) -> tuple[bytes, PortalDocument]:
    """Owner download: any client doc incl. retired (PORTAL_V1 §3)."""
    row = db.get(PortalDocument, document_id)
    if row is None or row.user_id != user.id:
        raise ApiError("document_not_found")
    try:
        data = storage.get(row.object_key)
    except Exception:
        raise ApiError("internal_error")
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise ApiError("internal_error")
    audit(db, actor=f"owner:{owner.id}", action="document.downloaded",
          entity_type="document", entity_id=row.id, request_id=request_id,
          detail={"for_client": user.id})
    return data, row


def retire_document(db, *, owner: PortalUser, user: PortalUser, document_id: str,
                    retired: bool, request_id: str) -> dict:
    """Soft retire (PORTAL_V1 F2): hidden from the client, retained in
    storage, audit-logged. No hard delete in v1."""
    row = db.get(PortalDocument, document_id)
    if row is None or row.user_id != user.id:
        raise ApiError("document_not_found")
    now = utcnow()
    row.deleted_at = now if retired else None
    audit(
        db, actor=f"owner:{owner.id}",
        action="document.retired" if retired else "document.unretired",
        entity_type="document", entity_id=row.id, request_id=request_id, detail={},
    )
    db.flush()
    return {"id": row.id, "retired": retired}


# --- F3 filings ------------------------------------------------------------------


FILING_TRANSITIONS = {
    "collecting": ("awaiting_info",),
    "awaiting_info": ("preparing",),
    "preparing": ("awaiting_info", "filed"),
    "filed": ("confirmed",),
    "confirmed": (),
}
FILING_STEPS = ("collecting", "awaiting_info", "preparing", "filed", "confirmed")
# Plain-language status lines for the filing_status_changed email —
# filed (transmitted to CRA) is never conflated with confirmed (CRA ack).
FILING_STATUS_PLAIN = {
    "collecting": "we're collecting your documents",
    "awaiting_info": "we need some information from you",
    "preparing": "being prepared",
    "filed": "has been filed",
    "confirmed": "confirmed — the CRA has acknowledged it",
}


def _filing_kind_label(db, kind_code: str) -> str:
    row = db.get(FilingKind, kind_code)
    if row is None:
        raise ApiError("validation_failed",
                       fields=[{"field": "kind_code", "message": "Unknown filing type."}])
    return row.label


def filing_projection(db, row: Filing) -> dict:
    return {
        "id": row.id,
        "kind_code": row.kind_code,
        "kind_label": _filing_kind_label(db, row.kind_code),
        "tax_year": row.tax_year,
        "status": row.status,
        "status_changed_at": utc_iso(row.status_changed_at),
        "note": row.note,
        "created_at": utc_iso(row.created_at),
    }


def create_filing(db, *, owner: PortalUser, client_id: str, kind_code: str,
                  tax_year: int, request_id: str, idem_key: str) -> dict:
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    kind_label = _filing_kind_label(db, kind_code)
    if not (2000 <= tax_year <= 2100):
        raise ApiError("validation_failed",
                       fields=[{"field": "tax_year", "message": "Enter a valid tax year."}])
    payload = {"client_id": client_id, "kind_code": kind_code, "tax_year": tax_year}
    payload_hash = idem.begin(db, route="admin.filings.create", client_key=idem_key, payload=payload)
    now = utcnow()
    row = Filing(id=new_uuid7(), user_id=user.id, kind_code=kind_code, tax_year=tax_year,
                 status="collecting", status_changed_at=now)
    db.add(row)
    db.flush()
    outbox_lib.stage_intent(
        db, kind="filing_status_changed", scope_id=f"{row.id}:created", destination=user.email,
        snapshot_fields={
            "first_name": (user.display_name or "").split()[0] if user.display_name else "",
            "kind_label": kind_label, "tax_year": str(tax_year),
            "status_plain": FILING_STATUS_PLAIN["collecting"], "note": "",
        },
    )
    audit(db, actor=f"owner:{owner.id}", action="filing.created",
          entity_type="filing", entity_id=row.id, request_id=request_id,
          detail={"kind": kind_code, "tax_year": tax_year,
                  "idempotency_scope": idem.scope_hash("admin.filings.create", idem_key)})
    db.flush()
    result = filing_projection(db, row)
    idem.store(db, route="admin.filings.create", client_key=idem_key,
               payload_hash=payload_hash, result=result, status_code=201)
    return result


def advance_filing(db, *, owner: PortalUser, filing_id: str, status: str,
                   note: str | None, request_id: str) -> dict:
    row = db.get(Filing, filing_id)
    if row is None:
        raise ApiError("filing_not_found")
    if status not in FILING_TRANSITIONS.get(row.status, ()):
        raise ApiError(
            "filing_transition_invalid",
            fields=[{"field": "status",
                     "message": f"A filing can't move from {row.status} to {status}."}],
        )
    now = utcnow()
    old = row.status
    row.status = status
    row.status_changed_at = now
    # The note is what the client sees — the UI labels it "Message to client".
    row.note = (note or "").strip()[:2000] or None
    user = db.get(PortalUser, row.user_id)
    kind_label = _filing_kind_label(db, row.kind_code)
    outbox_lib.stage_intent(
        db, kind="filing_status_changed", scope_id=f"{row.id}:{status}:{int(now.timestamp())}",
        destination=user.email if user else OWNER_EMAIL,
        snapshot_fields={
            "first_name": (user.display_name or "").split()[0] if (user and user.display_name) else "",
            "kind_label": kind_label, "tax_year": str(row.tax_year),
            "status_plain": FILING_STATUS_PLAIN[status],
            "note_block": f"\nJared's note: {row.note}\n" if row.note else "",
        },
    )
    audit(db, actor=f"owner:{owner.id}", action="filing.status_changed",
          entity_type="filing", entity_id=row.id, request_id=request_id,
          detail={"from": old, "to": status, "note_present": bool(row.note)})
    db.flush()
    return filing_projection(db, row)


def filing_timeline(db, *, user: PortalUser, filing_id: str) -> dict:
    row = db.get(Filing, filing_id)
    if row is None or row.user_id != user.id:
        raise ApiError("filing_not_found")
    events = (
        db.execute(
            select(AuditLog).where(AuditLog.entity_type == "filing",
                                   AuditLog.entity_id == filing_id,
                                   AuditLog.action.in_(("filing.created", "filing.status_changed")))
            .order_by(AuditLog.occurred_at.asc())
        ).scalars().all()
    )
    steps = []
    for e in events:
        detail = e.detail or {}
        if e.action == "filing.created":
            steps.append({"status": "collecting", "at": vancouver_iso(e.occurred_at),
                          "note": None, "actor": _actor_label(e.actor, user.id)})
        else:
            steps.append({"status": detail.get("to"), "at": vancouver_iso(e.occurred_at),
                          "note": None, "actor": _actor_label(e.actor, user.id)})
    # The current awaiting_info note rides on the filing row (Jared's latest
    # "Message to client"); history notes are audit detail (not stored —
    # the row note is the live one).
    proj = filing_projection(db, row)
    proj["timeline"] = steps
    return proj


def list_filings(db, *, user: PortalUser) -> dict:
    rows = (
        db.execute(select(Filing).where(Filing.user_id == user.id)
                   .order_by(Filing.tax_year.desc(), Filing.created_at.desc()))
        .scalars().all()
    )
    return {"items": [filing_projection(db, r) for r in rows]}


# --- F4 invoices ------------------------------------------------------------------


def _parse_due_at(value: str | None, now: datetime) -> datetime:
    """Due-date default +INVOICE_TERMS_DAYS (Net 30; D-27). Accepts ISO date
    or datetime; a date-only value means end-of-day in the firm timezone."""
    if not value:
        return now + timedelta(days=INVOICE_TERMS_DAYS)
    v = value.strip()
    try:
        if len(v) == 10:
            y, m, d = (int(p) for p in v.split("-"))
            return datetime(y, m, d, 23, 59, 59, tzinfo=FIRM_TZ).astimezone(ZoneInfo("UTC"))
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("UTC"))
    except (ValueError, TypeError):
        raise ApiError("validation_failed",
                       fields=[{"field": "due_at", "message": "Use YYYY-MM-DD or a full ISO date-time."}])


def _validate_invoice_lines(lines: list[dict]) -> list[dict]:
    if not lines or len(lines) > 50:
        raise ApiError("validation_failed",
                       fields=[{"field": "lines", "message": "Provide 1–50 line items."}])
    out = []
    for i, ln in enumerate(lines):
        desc = (ln.get("description") or "").strip()
        if not desc:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].description", "message": "A description is required."}])
        if len(desc) > 500:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].description",
                                    "message": "Keep the description to 500 characters or fewer."}])
        try:
            qty = Decimal(str(ln.get("quantity", 1)))
        except Exception:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].quantity", "message": "Quantity must be a number."}])
        if qty <= 0 or qty > 1_000_000:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].quantity", "message": "Quantity must be positive."}])
        unit = ln.get("unit_cents")
        if not isinstance(unit, int) or unit < 0 or unit > 100_000_000:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].unit_cents",
                                    "message": "Rate must be a non-negative whole-cent amount."}])
        bps = ln.get("tax_rate_bps")
        if bps is None:
            # D-27 default: GST 5% on accounting services (BC) unless the
            # owner sets an explicit per-line rate (0 = exempt).
            bps = INVOICE_TAX_RATE_BPS
        if not isinstance(bps, int) or bps < 0 or bps > 10000:
            raise ApiError("validation_failed",
                           fields=[{"field": f"lines[{i}].tax_rate_bps",
                                    "message": "Tax rate must be 0–10000 basis points."}])
        line_total = int((qty * unit).to_integral_value(rounding=ROUND_HALF_UP))
        tax_cents = int((Decimal(line_total) * bps / 10000).to_integral_value(rounding=ROUND_HALF_UP))
        out.append({
            "description": desc, "quantity": str(qty.normalize()), "unit_cents": unit,
            "tax_rate_bps": bps, "line_total_cents": line_total, "tax_cents": tax_cents,
        })
    return out


def _mint_invoice_number(db, series_id: str = "default") -> str:
    """Sequential, gap-free numbering (B2): the advisory lock is held by
    the caller across mint + commit (booking-confirm precedent).

    Numbering scheme (D-27; §3.5 #5): ``{prefix}-{YYYY}-{NNNN}`` — e.g.
    ``JARO-2026-0001``. The prefix defaults to INVOICE_NUMBER_PREFIX
    (seeded by migration 0014; Jared can change it at runtime via PUT
    /invoice-series). The sequence never resets and voided numbers are
    never recycled, so numbers stay unique and gap-free across years.
    """
    series = db.get(InvoiceSeries, series_id)
    if series is None:
        raise ApiError("internal_error")
    year = utcnow().astimezone(FIRM_TZ).year
    number = f"{series.prefix}-{year}-{series.next_number:04d}"
    series.next_number += 1
    db.flush()
    return number


def issue_invoice(
    db, *, owner: PortalUser, client_id: str, due_at: str | None, lines: list[dict],
    memo: str | None, idem_key: str, request_id: str, storage,
) -> dict:
    """Issue: mint number → compute totals → render + store PDF → stage
    invoice_issued. The preview IS the stored PDF (PORTAL_V1 F4): there is
    no draft state — what the owner sees after issuing is the record."""
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    validated = _validate_invoice_lines(lines)
    if memo is not None and len(memo) > 2000:
        raise ApiError("validation_failed",
                       fields=[{"field": "memo", "message": "Keep the memo to 2000 characters or fewer."}])
    payload = {"client_id": client_id, "due_at": due_at or "",
               "lines": validated, "memo": memo or ""}
    payload_hash = idem.begin(db, route="admin.invoices.issue", client_key=idem_key, payload=payload)
    now = utcnow()
    due = _parse_due_at(due_at, now)
    subtotal = sum(l["line_total_cents"] for l in validated)
    tax_total = sum(l["tax_cents"] for l in validated)
    total = subtotal + tax_total
    # The advisory lock covers mint + row + PDF + commit (route-owned
    # commit happens inside the lock — booking-confirm precedent).
    with advisory_lock(db, advisory.advisory_key("invoice-series", "default")):
        db.rollback()  # fresh snapshot on SQLite (no-op on Postgres)
        number = _mint_invoice_number(db)
        row = Invoice(
            id=new_uuid7(), user_id=user.id, invoice_number=number,
            issued_at=now, due_at=due, subtotal_cents=subtotal,
            tax_cents=tax_total, total_cents=total, currency="CAD",
            status="issued", memo=(memo or "").strip() or None,
        )
        db.add(row)
        db.flush()
        for ln in validated:
            db.add(InvoiceLine(
                id=new_uuid7(), invoice_id=row.id, description=ln["description"],
                quantity=Decimal(ln["quantity"]), unit_cents=ln["unit_cents"],
                line_total_cents=ln["line_total_cents"],
                tax_rate_bps=ln["tax_rate_bps"], tax_cents=ln["tax_cents"],
            ))
        pdf_bytes = invoice_pdf.render_invoice_pdf(
            invoice_number=number,
            issued_date=now.astimezone(FIRM_TZ).strftime("%B %d, %Y"),
            due_date=due.astimezone(FIRM_TZ).strftime("%B %d, %Y"),
            bill_to_name=user.display_name or "",
            bill_to_email=user.email,
            lines=validated,
            subtotal_cents=subtotal, tax_cents=tax_total, total_cents=total,
            memo=row.memo, payment_instructions=INVOICE_PAYMENT_INSTRUCTIONS,
            tax_label=INVOICE_TAX_LABEL,
        )
        row.issued_pdf_object_key = storage.put(
            pdf_bytes, subdir=f"invoices", suggested_name=f"{number}.pdf")
        outbox_lib.stage_intent(
            db, kind="invoice_issued", scope_id=row.id, destination=user.email,
            snapshot_fields={
                "first_name": (user.display_name or "").split()[0] if user.display_name else "",
                "invoice_number": number,
                "total": f"${total / 100:,.2f}",
                "due_date": due.astimezone(FIRM_TZ).strftime("%B %d, %Y"),
            },
        )
        audit(db, actor=f"owner:{owner.id}", action="invoice.issued",
              entity_type="invoice", entity_id=row.id, request_id=request_id,
              detail={"invoice_number": number, "total_cents": total,
                      "idempotency_scope": idem.scope_hash("admin.invoices.issue", idem_key)})
        db.flush()
        result = invoice_projection(db, row)
        idem.store(db, route="admin.invoices.issue", client_key=idem_key,
                   payload_hash=payload_hash, result=result, status_code=201)
        db.commit()
        return result


def invoice_projection(db, row: Invoice) -> dict:
    lines = (
        db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == row.id)
                   .order_by(InvoiceLine.id)).scalars().all()
    )
    now = utcnow()
    return {
        "id": row.id,
        "invoice_number": row.invoice_number,
        "issued_at": utc_iso(row.issued_at),
        "due_at": utc_iso(row.due_at),
        "lines": [
            {"description": l.description, "quantity": str(l.quantity),
             "unit_cents": l.unit_cents, "tax_rate_bps": l.tax_rate_bps,
             "line_total_cents": l.line_total_cents, "tax_cents": l.tax_cents}
            for l in lines
        ],
        "subtotal_cents": row.subtotal_cents,
        "tax_cents": row.tax_cents,
        "total_cents": row.total_cents,
        "currency": row.currency,
        "status": row.status,
        # overdue is a COMPUTED display state, never stored (D-55).
        "overdue": row.status == "issued" and row.due_at < now,
        "memo": row.memo,
        "paid_at": utc_iso(row.paid_at),
        "pay_method_note": row.pay_method_note,
        "void_reason": row.void_reason,
        "payment_instructions": INVOICE_PAYMENT_INSTRUCTIONS,
    }


def invoice_history(db, invoice_id: str) -> list[dict]:
    rows = (
        db.execute(
            select(AuditLog).where(AuditLog.entity_type == "invoice",
                                   AuditLog.entity_id == invoice_id,
                                   AuditLog.action.in_(("invoice.issued", "invoice.paid", "invoice.voided")))
            .order_by(AuditLog.occurred_at.asc())
        ).scalars().all()
    )
    return [{"action": r.action, "at": vancouver_iso(r.occurred_at),
             "actor": r.actor, "detail": r.detail or {}} for r in rows]


def pay_invoice(db, *, owner: PortalUser, invoice_id: str, pay_method_note: str,
                request_id: str) -> dict:
    row = db.get(Invoice, invoice_id)
    if row is None:
        raise ApiError("invoice_not_found")
    if row.status != "issued":
        raise ApiError("invoice_not_payable")
    note = (pay_method_note or "").strip()
    if not note:
        raise ApiError("validation_failed",
                       fields=[{"field": "pay_method_note", "message": "Record how the client paid."}])
    row.status = "paid"
    row.paid_at = utcnow()
    row.pay_method_note = note[:500]
    audit(db, actor=f"owner:{owner.id}", action="invoice.paid",
          entity_type="invoice", entity_id=row.id, request_id=request_id,
          detail={"invoice_number": row.invoice_number, "total_cents": row.total_cents})
    db.flush()
    return invoice_projection(db, row)


def void_invoice(db, *, owner: PortalUser, invoice_id: str, reason: str, request_id: str) -> dict:
    row = db.get(Invoice, invoice_id)
    if row is None:
        raise ApiError("invoice_not_found")
    if row.status != "issued":
        raise ApiError("invoice_not_voidable")
    reason = (reason or "").strip()
    if not reason:
        raise ApiError("validation_failed",
                       fields=[{"field": "reason", "message": "A void reason is required."}])
    row.status = "void"
    row.void_reason = reason[:500]
    # The number is NEVER recycled (B2 sequentiality): next_number only moves
    # forward; the voided row keeps its number as history.
    audit(db, actor=f"owner:{owner.id}", action="invoice.voided",
          entity_type="invoice", entity_id=row.id, request_id=request_id,
          detail={"invoice_number": row.invoice_number, "reason": row.void_reason})
    db.flush()
    return invoice_projection(db, row)


def get_invoice_series(db) -> dict:
    row = db.get(InvoiceSeries, "default")
    return {"id": "default", "prefix": row.prefix, "next_number": row.next_number}


def update_invoice_series(db, *, owner: PortalUser, prefix: str | None,
                          next_number: int | None, request_id: str) -> dict:
    row = db.get(InvoiceSeries, "default")
    if row is None:
        raise ApiError("internal_error")
    if prefix is not None:
        prefix = prefix.strip()
        if not prefix or len(prefix) > 32:
            raise ApiError("validation_failed",
                           fields=[{"field": "prefix", "message": "Prefix must be 1–32 characters."}])
        row.prefix = prefix
    if next_number is not None:
        if not isinstance(next_number, int) or next_number < 1:
            raise ApiError("validation_failed",
                           fields=[{"field": "next_number", "message": "Next number must be ≥ 1."}])
        if next_number < row.next_number:
            # Monotonic: rewinding could re-issue an existing number.
            raise ApiError("validation_failed",
                           fields=[{"field": "next_number",
                                    "message": "Next number can't move backwards — issued numbers are never reused."}])
        row.next_number = next_number
    audit(db, actor=f"owner:{owner.id}", action="invoice.series_updated",
          entity_type="invoice_series", entity_id="default", request_id=request_id,
          detail={"prefix": row.prefix, "next_number": row.next_number})
    db.flush()
    return get_invoice_series(db)


def list_invoices(db, *, user: PortalUser) -> dict:
    rows = (
        db.execute(select(Invoice).where(Invoice.user_id == user.id)
                   .order_by(Invoice.issued_at.desc())).scalars().all()
    )
    return {"items": [invoice_projection(db, r) for r in rows]}


def get_client_invoice(db, *, user: PortalUser, invoice_id: str) -> dict:
    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id:
        raise ApiError("invoice_not_found")
    proj = invoice_projection(db, row)
    proj["history"] = invoice_history(db, invoice_id)
    return proj


def get_invoice_pdf_bytes(db, *, user: PortalUser, invoice_id: str, storage,
                          request_id: str, actor: PortalUser) -> tuple[bytes, Invoice]:
    """Byte-identical issuance PDF (the stored bytes are the record)."""
    import hashlib as _hl

    row = db.get(Invoice, invoice_id)
    if row is None or row.user_id != user.id or not row.issued_pdf_object_key:
        if row is not None and row.user_id == user.id:
            raise ApiError("invoice_not_found")
        audit(db, actor=f"{'owner' if actor.role == 'owner' else 'client'}:{actor.id}",
              action="invoice.download_denied", entity_type="invoice",
              entity_id=invoice_id, request_id=request_id, detail={})
        raise ApiError("invoice_not_found")
    try:
        data = storage.get(row.issued_pdf_object_key)
    except Exception:
        raise ApiError("internal_error")
    if not data.startswith(b"%PDF-"):
        raise ApiError("internal_error")
    return data, row


# --- owner console ---------------------------------------------------------------


def list_clients(db, *, search: str | None, page: int, per_page: int) -> dict:
    q = select(PortalUser).where(PortalUser.role == "client")
    if search:
        like = f"%{search.strip().lower()}%"
        q = q.where(or_(func.lower(PortalUser.email).like(like),
                        func.lower(PortalUser.display_name).like(like)))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = (
        db.execute(q.order_by(PortalUser.created_at.desc())
                   .offset((page - 1) * per_page).limit(per_page)).scalars().all()
    )
    return {
        "items": [client_projection(r) for r in rows],
        "page": page, "per_page": per_page, "total": total,
    }


def client_projection(user: PortalUser) -> dict:
    return {
        "id": user.id, "name": user.display_name, "email": user.email,
        "phone_masked": mask_phone(user.phone_e164),
        "status": user.status, "created_at": utc_iso(user.created_at),
        "last_login_at": utc_iso(user.last_login_at),
        "must_change_password": user.must_change_password,
        "terms_accepted_at": utc_iso(user.terms_accepted_at),
    }


def client_dossier(db, *, client_id: str) -> dict:
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    from .models import DocumentType

    labels = {r.code: r.label for r in db.execute(select(DocumentType)).scalars().all()}
    docs = (
        db.execute(select(PortalDocument).where(PortalDocument.user_id == user.id)
                   .order_by(PortalDocument.created_at.desc())).scalars().all()
    )
    filings = (
        db.execute(select(Filing).where(Filing.user_id == user.id)
                   .order_by(Filing.tax_year.desc())).scalars().all()
    )
    invoices = (
        db.execute(select(Invoice).where(Invoice.user_id == user.id)
                   .order_by(Invoice.issued_at.desc())).scalars().all()
    )
    entity_ids = _user_entity_ids(db, user.id)
    wanted = []
    for etype, ids in entity_ids.items():
        if ids:
            wanted.append((AuditLog.entity_type == etype) & (AuditLog.entity_id.in_(ids)))
    wanted.append((AuditLog.entity_type == "portal_user") & (AuditLog.entity_id == user.id))
    feed = (
        db.execute(select(AuditLog).where(or_(*wanted))
                   .order_by(AuditLog.occurred_at.desc()).limit(50)).scalars().all()
    )
    overview = client_projection(user)
    # D-60: internal notes live on the overview tab — never shown to the client.
    overview["internal_note"] = user.internal_note
    return {
        "overview": overview,
        "documents": [{**document_projection(d, labels.get(d.type_code)),
                       "retired": d.deleted_at is not None} for d in docs],
        "filings": [filing_projection(db, f) for f in filings],
        "invoices": [invoice_projection(db, i) for i in invoices],
        "activity": [
            {"action": r.action, "at": vancouver_iso(r.occurred_at), "actor": r.actor,
             "entity_type": r.entity_type, "detail": r.detail or {}}
            for r in feed
        ],
    }


def patch_client(db, *, owner: PortalUser, client_id: str, display_name: str | None,
                 status: str | None, internal_note: str | None, request_id: str) -> dict:
    user = db.get(PortalUser, client_id)
    if user is None or user.role != "client":
        raise ApiError("client_not_found")
    changes: dict = {}
    if display_name is not None:
        display_name = display_name.strip()
        if not display_name or len(display_name) > 200:
            raise ApiError("validation_failed",
                           fields=[{"field": "display_name", "message": "Enter a name of 1–200 characters."}])
        if display_name != user.display_name:
            changes["display_name"] = display_name
            user.display_name = display_name
    if status is not None:
        if status not in ("pending_first_login", "active", "locked", "disabled"):
            raise ApiError("validation_failed",
                           fields=[{"field": "status", "message": "Unknown client status."}])
        if status != user.status:
            changes["status"] = status
            user.status = status
    if internal_note is not None:
        note = internal_note.strip()[:5000] or None
        if note != user.internal_note:
            changes["internal_note"] = bool(note)
            user.internal_note = note
    if changes:
        audit(db, actor=f"owner:{owner.id}", action="client.profile_updated",
              entity_type="portal_user", entity_id=user.id, request_id=request_id,
              detail={"fields": sorted(changes)})
        db.flush()
    proj = client_projection(user)
    proj["internal_note"] = user.internal_note
    return proj


def audit_feed(db, *, entity_type: str | None, action: str | None,
               entity_id: str | None, page: int, per_page: int) -> dict:
    q = select(AuditLog)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if action:
        q = q.where(AuditLog.action == action)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = (
        db.execute(q.order_by(AuditLog.occurred_at.desc())
                   .offset((page - 1) * per_page).limit(per_page)).scalars().all()
    )
    return {
        "items": [
            {"id": r.id, "at": utc_iso(r.occurred_at), "actor": r.actor,
             "action": r.action, "entity_type": r.entity_type,
             "entity_id": r.entity_id, "detail": r.detail or {}}
            for r in rows
        ],
        "page": page, "per_page": per_page, "total": total,
    }
