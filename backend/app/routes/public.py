"""Public routes: probes + contact-inquiry lead pipeline (Phase 2)."""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import text

from .. import idempotency as idem
from ..site_contact import SITE_CONTACT
from .. import outbox as outbox_lib
from .. import refnums
from ..db import new_uuid7, utcnow
from ..errors import ApiError
from ..idempotency import ReplayHit, require_idempotency_key
from ..models import AuditLog, Inquiry
from ..rate_limits import rate_limit_dependency
from . import DB, StrictModel

router = APIRouter()

READYZ_TIMEOUT_SECONDS = 5
_readyz_lock = threading.Lock()

# Owner-notice destination (Jared only — the full guest message never goes
# anywhere else).
OWNER_NOTICE_DESTINATION = SITE_CONTACT["email"]  # single source of truth: app/site_contact.py

# EMAIL_TEMPLATES §4.2 {{RESPONSE_SLA}}: suggested default, a business
# commitment — pending Jared's confirmation (D-33, IMPLEMENTATION_PLAN
# §3.5 #6). When he confirms, this constant and the email copy change
# together (cross-workstream contract).
RESPONSE_SLA_DEFAULT = "1–2 business days"

# Owner-question decision (IMPLEMENTATION_PLAN §3.5 #20): the contact
# receipt echoes ~200 chars of the guest's message so they can confirm
# what arrived — never the full message. Rationale: PII minimization (the
# receipt is email, a less-controlled channel; the full text stays in the
# owner notice + the inquiry row).
RECEIPT_EXCERPT_CHARS = 200

# Conservative stdlib email check (the venv ships no email-validator).
# Rejects whitespace/control chars — which also makes the address
# header-safe for the owner-notice Reply-To.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
)


def _is_valid_email(value: str) -> bool:
    if not value or len(value) > 254 or len(value) < 3:
        return False
    if value.count("@") != 1:
        return False
    return _EMAIL_RE.match(value) is not None


class HealthResponse(StrictModel):
    status: str
    version: str


class ReadinessResponse(StrictModel):
    ready: bool
    checks: dict


def _probe(app) -> tuple[bool, dict]:
    """DB + outbox-claim probes. Driver exceptions collapse to booleans —
    never leak connection strings (A6/A7)."""
    checks = {"db": False, "outbox_claim": False}
    try:
        engine = app.state.engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = True
        db = app.state.session_factory()
        try:
            # Claim-path probe: a conditional UPDATE against a nil id proves
            # the E1 claim SQL executes (rowcount 0 is the healthy answer).
            claim = outbox_lib.claim_intent(db, "00000000-0000-0000-0000-000000000000")
            db.rollback()
            checks["outbox_claim"] = not claim.claimed
        finally:
            db.close()
    except Exception:
        return False, checks
    return all(checks.values()), checks


@router.get("/healthz", response_model=HealthResponse, operation_id="healthz", tags=["ops"])
def healthz():
    """Liveness: process alive. No dependencies (A7)."""
    from .. import __version__

    return {"status": "ok", "version": __version__}


@router.get("/readyz", response_model=ReadinessResponse, operation_id="readyz", tags=["ops"])
def readyz(request: Request):
    """Readiness: DB + outbox-claim probe. Single-flight, hard timeout (A7)."""
    with _readyz_lock:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_probe, request.app)
            try:
                ready, checks = future.result(timeout=READYZ_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                ready, checks = False, {"db": False, "outbox_claim": False, "timeout": True}
    if not ready:
        raise ApiError("not_ready")
    return {"ready": ready, "checks": checks}


# --- contact inquiries (Phase 2: the lead pipeline) --------------------------------

# Plain-language messages: the A1 validation handler renders err["msg"]
# verbatim as the per-field message, and PydanticCustomError (unlike a bare
# ValueError) keeps the message free of the "Value error," prefix.


class InquirySubmitRequest(StrictModel):
    """A2 closed write schema: name ≤120, email, message ≤5000,
    extra="forbid" (unknown fields are rejected, not ignored).

    Length bounds live in the field validators (not Field constraints) so
    every failure renders the same plain-language message — a Field-level
    bound would fire first with Pydantic's default wording."""

    name: str
    email: str
    message: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise PydanticCustomError("value_error", "Please enter your name.")
        if len(v) > 120:
            raise PydanticCustomError(
                "value_error", "Please keep your name to 120 characters or fewer."
            )
        return v

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = v.strip()
        if not _is_valid_email(v):
            raise PydanticCustomError(
                "value_error", "Please enter a valid email address, like you@example.com."
            )
        # Normalized for delivery + dedup: the mailbox is the same address.
        return v.lower()

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise PydanticCustomError("value_error", "Please enter your message.")
        if len(v) > 5000:
            raise PydanticCustomError(
                "value_error", "Please keep your message to 5,000 characters or fewer."
            )
        return v


class InquirySubmitResponse(StrictModel):
    """F7 honest vocabulary: the message is *queued* (two outbox intents
    staged), never "sent"."""

    inquiry_id: str
    reference: str
    queued_count: int
    # Q §3.5 #6 (2026-10-08): the contact page's success copy renders the SLA
    # from this field — the backend constant is the single source, never a
    # frontend literal.
    response_sla: str


def _message_excerpt(message: str, limit: int = RECEIPT_EXCERPT_CHARS) -> str:
    """EMAIL_TEMPLATES §4.2 {{MESSAGE_EXCERPT}}: first ~200 chars so the
    sender can confirm what arrived; internal notes are never included
    (there are none at this stage — the receipt carries only guest input)."""
    return message[:limit] + ("…" if len(message) > limit else "")


def submit_inquiry(
    db,
    *,
    name: str,
    email: str,
    message: str,
    idempotency_key: str,
    request_id: str,
) -> dict:
    """A4 domain service: performs every write inside the caller's
    transaction (inquiry row + two outbox intents + audit row) and never
    commits, rolls back, or touches the network. Raises ReplayHit (return
    the stored result) or ApiError 409 on idempotency conflict."""
    payload = {"name": name, "email": email, "message": message}
    payload_hash = idem.begin(
        db, route="inquiries.submit", client_key=idempotency_key, payload=payload
    )

    reference = refnums.new_inquiry_reference()
    row = Inquiry(
        id=new_uuid7(),
        reference=reference,
        name=name,
        email=email,
        message=message,
        status="new",
        payload_hash=payload_hash,
    )
    db.add(row)
    db.flush()

    # The plan's `contact:<request_id>:receipt` key shape maps onto the
    # canonical D-14 composition sha256(kind || ':' || scope_id): the scope
    # is the request/inquiry reference, the kind distinguishes the two
    # intents. F3: re-issuing the same (kind, scope) returns the existing
    # row — a second staging can never double-queue the receipt.
    first_name = name.split()[0]  # EMAIL_TEMPLATES §4.2 {{FIRST_NAME}}
    outbox_lib.stage_intent(
        db,
        kind="inquiry_receipt",
        scope_id=reference,
        destination=email,  # §4.3 recipient discipline: ONLY the form's address
        snapshot_fields={
            "first_name": first_name,
            "request_id": reference,
            "response_sla": RESPONSE_SLA_DEFAULT,
            "message_excerpt": _message_excerpt(message),
        },
    )
    outbox_lib.stage_intent(
        db,
        kind="inquiry_owner_notice",
        scope_id=reference,
        destination=OWNER_NOTICE_DESTINATION,  # Jared only — full message
        snapshot_fields={
            "guest_name": name,
            "guest_email": email,
            "request_id": reference,
            "submitted_at": utcnow().isoformat(),
            "message": message,
        },
    )

    # A6: the audit detail carries no PII — reference, counts, and the
    # idempotency scope hash (the client key is a CSPRNG UUID, no PII).
    db.add(
        AuditLog(
            actor="public",
            action="inquiry.submitted",
            entity_type="inquiry",
            entity_id=row.id,
            request_id=request_id,
            detail={
                "reference": reference,
                "queued_count": 2,
                "idempotency_scope": idem.scope_hash("inquiries.submit", idempotency_key),
            },
        )
    )
    db.flush()

    result = {
        "inquiry_id": row.id,
        "reference": reference,
        "queued_count": 2,
        # Q §3.5 #6: the SLA rides along from RESPONSE_SLA_DEFAULT so the
        # frontend success copy never duplicates the literal (same value the
        # receipt email snapshots above).
        "response_sla": RESPONSE_SLA_DEFAULT,
    }
    idem.store(
        db,
        route="inquiries.submit",
        client_key=idempotency_key,
        payload_hash=payload_hash,
        result=result,
        status_code=201,
    )
    return result


@router.post(
    "/api/v1/inquiries",
    response_model=InquirySubmitResponse,
    status_code=201,
    operation_id="inquirySubmit",
    tags=["public"],
    # A5: the target dimension is the recipient address — each submit
    # queues a receipt to the form's email, so per-recipient budgeting is
    # the mail-loop defense (DONE-7). Increments run in their own
    # transaction (a route rollback never refunds the attempt).
    dependencies=[Depends(rate_limit_dependency("inquiries.submit", target_from="email"))],
)
def inquiry_submit(body: InquirySubmitRequest, request: Request, db=DB):
    key = require_idempotency_key(request)  # A3: missing key fails loudly (422)
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        return submit_inquiry(
            db,
            name=body.name,
            email=body.email,
            message=body.message,
            idempotency_key=key,
            request_id=request_id,
        )
    except ReplayHit as hit:
        # A3 replay: the stored result, byte-identical, with replayed:true.
        # JSONResponse bypasses response_model so the replay flag survives;
        # X-Request-ID is still echoed by the middleware.
        return JSONResponse(status_code=hit.status_code, content=hit.result)
