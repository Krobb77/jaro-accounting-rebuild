"""Admin routes — /api/v1/admin/* (D-05 canonical prefix, owner role only).

Phase 5: full implementation — owner bootstrap + login (TOTP mandatory,
no SMS fallback), invitations, clients + dossier, filings, invoices,
document vault ops, audit feed. Every operation writes audit rows (A4).
Non-owner sessions are rejected with 403 at the route layer.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, Response as RawResponse
from pydantic import Field

from .. import idempotency as idem
from .. import portal as portal_lib
from .. import sessions
from ..errors import ApiError
from ..idempotency import ReplayHit, require_idempotency_key
from ..models import Booking, PortalUser
from ..rate_limits import rate_limit_dependency
from . import DB, StrictModel

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _bind_locks(request: Request, db) -> None:
    portal_lib.set_lock_backend(db, request.app.state.lock_backend)


def _ctx(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "request_id": getattr(request.state, "request_id", "unknown"),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "secret_key": settings.secret_key,
        "settings": settings,
        "storage": request.app.state.storage,
        "lock_backend": request.app.state.lock_backend,
    }


def _set_session_cookie(response: Response, result: dict) -> dict:
    raw = result.pop("_session_raw", None)
    if raw is not None:
        sessions.set_session_cookie(response, raw)
    return result


def _replay(hit: ReplayHit) -> JSONResponse:
    return JSONResponse(status_code=hit.status_code, content=hit.result)


def owner(request: Request, db=DB) -> PortalUser:
    """Owner gate: non-owner sessions → 403. Until the owner enrolls TOTP,
    only the enrollment endpoints are reachable (forced enrollment on
    first login, AUTH §2.1)."""
    user = sessions.require_owner(request, db)
    if user.totp_enrolled_at is None and request.url.path not in (
        "/api/v1/admin/2fa/enroll",
        "/api/v1/admin/2fa/confirm",
    ):
        raise ApiError("totp_required")
    return user


Owner = Depends(owner)


def _parse_dt(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ApiError(
            "validation_failed",
            fields=[{"field": field, "message": "Use an ISO-8601 date-time."}],
        )
    return dt


# --- request models (A2 closed) ---------------------------------------------------


class BootstrapRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    initial_password: str = Field(min_length=16, max_length=256)


class AdminLoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class AdminTotpVerifyRequest(StrictModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32)


class TotpConfirmRequest(StrictModel):
    code: str = Field(min_length=6, max_length=6)
    code2: str = Field(min_length=6, max_length=6)


class InvitationCreateRequest(StrictModel):
    client_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone_e164: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=1000)


class RevokeRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


class InvitationRevokeRequest(StrictModel):
    note: str | None = Field(default=None, max_length=280)


class ClientPatchRequest(StrictModel):
    display_name: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=32)
    internal_note: str | None = Field(default=None, max_length=5000)


class FilingCreateRequest(StrictModel):
    client_id: str = Field(min_length=1, max_length=64)
    kind_code: str = Field(min_length=1, max_length=64)
    tax_year: int = Field(ge=2000, le=2100)


class FilingStatusRequest(StrictModel):
    status: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


class InvoiceLineRequest(StrictModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: float = Field(gt=0)
    unit_cents: int = Field(ge=0)
    # Omitted → the INVOICE_TAX_RATE default (GST 5%, D-27) applies in
    # _validate_invoice_lines; explicit 0 = tax-exempt line.
    tax_rate_bps: int | None = Field(default=None, ge=0, le=10000)


class InvoiceCreateRequest(StrictModel):
    client_id: str = Field(min_length=1, max_length=64)
    due_at: str | None = Field(default=None, max_length=64)
    lines: list[InvoiceLineRequest] = Field(min_length=1)
    memo: str | None = Field(default=None, max_length=2000)


class InvoicePayRequest(StrictModel):
    pay_method_note: str = Field(min_length=1, max_length=500)


class InvoiceVoidRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)


class InvoiceSeriesRequest(StrictModel):
    prefix: str | None = Field(default=None, max_length=32)
    next_number: int | None = Field(default=None, ge=1)


class RetireRequest(StrictModel):
    retired: bool = True


# --- bootstrap + owner login ---------------------------------------------------


@router.post(
    "/bootstrap",
    status_code=201,
    operation_id="adminBootstrap",
    dependencies=[Depends(rate_limit_dependency("admin.bootstrap"))],
)
def admin_bootstrap(body: BootstrapRequest, request: Request, db=DB):
    """One-shot owner bootstrap (D-03): second call → 410; absent secret → 503."""
    ctx = _ctx(request)
    owner_id = portal_lib.bootstrap_owner(
        db, settings=ctx["settings"], email=body.email,
        initial_password=body.initial_password, request_id=ctx["request_id"],
    )
    return {"owner_id": owner_id}


@router.post(
    "/login",
    status_code=202,
    operation_id="adminLogin",
    dependencies=[Depends(rate_limit_dependency("admin.login", target_from="email"))],
)
def admin_login(body: AdminLoginRequest, request: Request, response: Response, db=DB):
    """Owner login: email+password (anti-enumeration) → TOTP challenge
    MANDATORY, no SMS fallback (AUTH D1)."""
    _bind_locks(request, db)
    ctx = _ctx(request)
    result = portal_lib.owner_login(
        db, email=body.email, password=body.password,
        request_id=ctx["request_id"], ip=ctx["ip"], user_agent=ctx["user_agent"],
        secret_key=ctx["secret_key"],
    )
    return _set_session_cookie(response, result)


@router.post(
    "/login/2fa",
    operation_id="adminLoginTotp",
    dependencies=[Depends(rate_limit_dependency("admin.login", target_from="challenge_id"))],
)
def admin_login_totp(body: AdminTotpVerifyRequest, request: Request, response: Response, db=DB):
    _bind_locks(request, db)
    ctx = _ctx(request)
    result = portal_lib.owner_verify_totp(
        db, challenge_id=body.challenge_id, code=body.code,
        request_id=ctx["request_id"], ip=ctx["ip"], user_agent=ctx["user_agent"],
        secret_key=ctx["secret_key"], settings=ctx["settings"],
    )
    return _set_session_cookie(response, result)


@router.post("/2fa/enroll", operation_id="adminTotpEnroll")
def admin_totp_enroll(request: Request, db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.owner_totp_enroll(
        db, owner=user, settings=ctx["settings"], request_id=ctx["request_id"])


@router.post("/2fa/confirm", operation_id="adminTotpConfirm")
def admin_totp_confirm(body: TotpConfirmRequest, request: Request, db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.owner_totp_confirm(
        db, owner=user, code=body.code, code2=body.code2,
        settings=ctx["settings"], request_id=ctx["request_id"],
    )


# --- invitations ---------------------------------------------------------------


@router.get("/invitations", operation_id="adminInvitationsList")
def invitations_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    expiring_before: str | None = None,
    page: int = 1,
    per_page: int = 25,
    db=DB,
    user: PortalUser = Owner,
):
    if page < 1 or per_page < 1 or per_page > 100:
        raise ApiError("validation_failed",
                       fields=[{"field": "per_page", "message": "Use page ≥ 1 and 1 ≤ per_page ≤ 100."}])
    return portal_lib.list_invitations(
        db, status=status, search=search,
        created_from=_parse_dt(created_from, "created_from"),
        created_to=_parse_dt(created_to, "created_to"),
        expiring_before=_parse_dt(expiring_before, "expiring_before"),
        page=page, per_page=per_page,
    )


@router.post(
    "/invitations",
    status_code=201,
    operation_id="adminInvitationCreate",
    dependencies=[Depends(rate_limit_dependency("admin.invitations"))],
)
def invitation_create(body: InvitationCreateRequest, request: Request, db=DB, user: PortalUser = Owner):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.create_invitation(
            db, owner=user, client_name=body.client_name, email=body.email,
            phone_e164=body.phone_e164, note=body.note, idem_key=key,
            request_id=ctx["request_id"],
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.get("/invitations/{invitation_id}", operation_id="adminInvitationGet")
def invitation_get(invitation_id: str, db=DB, user: PortalUser = Owner):
    row = portal_lib.db_get_invitation(db, invitation_id)
    proj = portal_lib.invite_projection(row)
    proj["email_status"] = portal_lib.email_status_for_invite(db, row.id)
    proj["events"] = portal_lib.invite_events_trail(db, row.id)
    return proj


@router.post(
    "/invitations/{invitation_id}/resend",
    operation_id="adminInvitationResend",
    dependencies=[Depends(rate_limit_dependency("admin.invitations"))],
)
def invitation_resend(invitation_id: str, request: Request, db=DB, user: PortalUser = Owner):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.resend_invitation(
            db, owner=user, invite_id=invitation_id, idem_key=key,
            request_id=ctx["request_id"],
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.post(
    "/invitations/{invitation_id}/revoke",
    operation_id="adminInvitationRevoke",
    dependencies=[Depends(rate_limit_dependency("admin.invitations"))],
)
def invitation_revoke(invitation_id: str, body: InvitationRevokeRequest, request: Request,
                      db=DB, user: PortalUser = Owner):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.revoke_invitation(
            db, owner=user, invite_id=invitation_id, note=body.note, idem_key=key,
            request_id=ctx["request_id"],
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.get("/password-resets", operation_id="adminPasswordResetsList")
def password_resets_list(
    status: str | None = None, page: int = 1, per_page: int = 25,
    db=DB, user: PortalUser = Owner,
):
    if page < 1 or per_page < 1 or per_page > 100:
        raise ApiError("validation_failed",
                       fields=[{"field": "per_page", "message": "Use page ≥ 1 and 1 ≤ per_page ≤ 100."}])
    return portal_lib.list_password_resets(db, status=status, page=page, per_page=per_page)


# --- clients -------------------------------------------------------------------


@router.get("/clients", operation_id="adminClientsList")
def clients_list(search: str | None = None, page: int = 1, per_page: int = 25,
                 db=DB, user: PortalUser = Owner):
    if page < 1 or per_page < 1 or per_page > 100:
        raise ApiError("validation_failed",
                       fields=[{"field": "per_page", "message": "Use page ≥ 1 and 1 ≤ per_page ≤ 100."}])
    return portal_lib.list_clients(db, search=search, page=page, per_page=per_page)


@router.get("/clients/{client_id}", operation_id="adminClientGet")
def client_get(client_id: str, db=DB, user: PortalUser = Owner):
    return portal_lib.client_dossier(db, client_id=client_id)


@router.patch("/clients/{client_id}", operation_id="adminClientPatch")
def client_patch(client_id: str, body: ClientPatchRequest, request: Request,
                 db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.patch_client(
        db, owner=user, client_id=client_id, display_name=body.display_name,
        status=body.status, internal_note=body.internal_note,
        request_id=ctx["request_id"],
    )


@router.post("/clients/{client_id}/2fa/disable", operation_id="adminClient2faDisable")
def client_2fa_disable(client_id: str, body: RevokeRequest, request: Request,
                       db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.disable_client_totp(
        db, owner=user, client_id=client_id, reason=body.reason,
        request_id=ctx["request_id"],
    )


@router.post("/clients/{client_id}/sessions/revoke-all", operation_id="adminClientSessionsRevokeAll")
def client_sessions_revoke_all(client_id: str, request: Request, db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.revoke_all_client_sessions(
        db, owner=user, client_id=client_id, request_id=ctx["request_id"])


@router.post(
    "/clients/{client_id}/documents",
    status_code=201,
    operation_id="adminClientDocumentUpload",
    dependencies=[Depends(rate_limit_dependency("portal.upload"))],
)
async def client_document_upload(
    client_id: str,
    request: Request,
    db=DB,
    user: PortalUser = Owner,
    file: UploadFile = File(...),
    type_code: str = Form("other"),
    label: str | None = Form(None),
):
    """Jared uploads FOR a client (deliverables): queues document_shared."""
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    client_user = portal_lib.db_get_client(db, client_id)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > portal_lib.MAX_UPLOAD_BYTES:
            raise ApiError("file_too_large")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        return portal_lib.store_document(
            db, user=client_user, uploaded_by="owner", filename=file.filename or "upload",
            data=data, type_code=type_code, label=label, idem_key=key,
            request_id=ctx["request_id"], storage=ctx["storage"], actor=user,
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.get("/clients/{client_id}/documents/{document_id}/download",
            operation_id="adminClientDocumentDownload")
def client_document_download(client_id: str, document_id: str, request: Request,
                             db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    client_user = portal_lib.db_get_client(db, client_id)
    data, row = portal_lib.get_document_bytes_owner(
        db, user=client_user, document_id=document_id, storage=ctx["storage"],
        owner=user, request_id=ctx["request_id"],
    )
    disp = f"attachment; filename*=UTF-8''{quote(row.original_filename)}"
    return RawResponse(
        content=data, media_type="application/octet-stream",
        headers={"Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
    )


@router.post("/clients/{client_id}/documents/{document_id}/retire",
             operation_id="adminClientDocumentRetire")
def client_document_retire(client_id: str, document_id: str, body: RetireRequest,
                           request: Request, db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    client_user = portal_lib.db_get_client(db, client_id)
    return portal_lib.retire_document(
        db, owner=user, user=client_user, document_id=document_id,
        retired=body.retired, request_id=ctx["request_id"],
    )


# --- bookings (owner levers) -------------------------------------------------------------------


@router.get("/bookings", operation_id="adminBookingsList")
def bookings_list(user: PortalUser = Owner):
    raise ApiError("not_implemented")


@router.post("/bookings/{reference}/no-show", operation_id="adminBookingNoShow")
def booking_no_show(reference: str, request: Request, db=DB, user: PortalUser = Owner):
    """Owner lever: mark a booking as a no-show (D-22). 2 no-shows in 12
    months → 422 booking_hold on new submits (enforced at intent creation)."""
    from sqlalchemy import select

    from .. import booking as booking_lib

    booking = (
        db.execute(select(Booking).where(Booking.reference_number == reference))
        .scalars()
        .first()
    )
    if booking is None:
        raise ApiError("booking_not_found")
    rid = getattr(request.state, "request_id", "unknown")
    return booking_lib.mark_no_show(db, booking=booking, request_id=rid)


@router.post("/bookings/{reference}/cancel", operation_id="adminBookingCancel")
def booking_cancel(reference: str, body: RevokeRequest, user: PortalUser = Owner):
    raise ApiError("not_implemented")


# --- inquiries (owner queue — not Phase-5 scope) -------------------------------------------------------------------


@router.get("/inquiries", operation_id="adminInquiriesList")
def inquiries_list(user: PortalUser = Owner):
    raise ApiError("not_implemented")


@router.post("/inquiries/{inquiry_id}/archive", operation_id="adminInquiryArchive")
def inquiry_archive(inquiry_id: str, user: PortalUser = Owner):
    raise ApiError("not_implemented")


# --- filings -------------------------------------------------------------------


@router.post("/filings", status_code=201, operation_id="adminFilingCreate")
def filing_create(body: FilingCreateRequest, request: Request, db=DB, user: PortalUser = Owner):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.create_filing(
            db, owner=user, client_id=body.client_id, kind_code=body.kind_code,
            tax_year=body.tax_year, request_id=ctx["request_id"], idem_key=key,
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.patch("/filings/{filing_id}/status", operation_id="adminFilingStatus")
def filing_status(filing_id: str, body: FilingStatusRequest, request: Request,
                  db=DB, user: PortalUser = Owner):
    _bind_locks(request, db)
    ctx = _ctx(request)
    return portal_lib.advance_filing(
        db, owner=user, filing_id=filing_id, status=body.status,
        note=body.note, request_id=ctx["request_id"],
    )


# --- invoices -------------------------------------------------------------------


@router.post("/invoices", status_code=201, operation_id="adminInvoiceCreate")
def invoice_create(body: InvoiceCreateRequest, request: Request, db=DB, user: PortalUser = Owner):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.issue_invoice(
            db, owner=user, client_id=body.client_id, due_at=body.due_at,
            lines=[ln.model_dump() for ln in body.lines], memo=body.memo,
            idem_key=key, request_id=ctx["request_id"], storage=ctx["storage"],
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.get("/invoices/{invoice_id}", operation_id="adminInvoiceGet")
def invoice_get(invoice_id: str, db=DB, user: PortalUser = Owner):
    from sqlalchemy import select as _select

    from ..models import Invoice as InvoiceRow

    row = db.get(InvoiceRow, invoice_id)
    if row is None:
        raise ApiError("invoice_not_found")
    proj = portal_lib.invoice_projection(db, row)
    proj["history"] = portal_lib.invoice_history(db, invoice_id)
    return proj


@router.get("/invoices/{invoice_id}/pdf", operation_id="adminInvoicePdf")
def invoice_pdf_download(invoice_id: str, request: Request, db=DB, user: PortalUser = Owner):
    from ..models import Invoice as InvoiceRow

    ctx = _ctx(request)
    row = db.get(InvoiceRow, invoice_id)
    if row is None or not row.issued_pdf_object_key:
        raise ApiError("invoice_not_found")
    client_user = portal_lib.db_get_client(db, row.user_id)
    data, _ = portal_lib.get_invoice_pdf_bytes(
        db, user=client_user, invoice_id=invoice_id, storage=ctx["storage"],
        request_id=ctx["request_id"], actor=user,
    )
    disp = f"attachment; filename*=UTF-8''{quote(row.invoice_number + '.pdf')}"
    return RawResponse(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
    )


@router.post("/invoices/{invoice_id}/pay", operation_id="adminInvoicePay")
def invoice_pay(invoice_id: str, body: InvoicePayRequest, request: Request,
                db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.pay_invoice(
        db, owner=user, invoice_id=invoice_id, pay_method_note=body.pay_method_note,
        request_id=ctx["request_id"],
    )


@router.post("/invoices/{invoice_id}/void", operation_id="adminInvoiceVoid")
def invoice_void(invoice_id: str, body: InvoiceVoidRequest, request: Request,
                 db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.void_invoice(
        db, owner=user, invoice_id=invoice_id, reason=body.reason,
        request_id=ctx["request_id"],
    )


@router.get("/invoice-series", operation_id="adminInvoiceSeriesGet")
def invoice_series_get(db=DB, user: PortalUser = Owner):
    return portal_lib.get_invoice_series(db)


@router.put("/invoice-series", operation_id="adminInvoiceSeriesUpdate")
def invoice_series_update(body: InvoiceSeriesRequest, request: Request,
                          db=DB, user: PortalUser = Owner):
    ctx = _ctx(request)
    return portal_lib.update_invoice_series(
        db, owner=user, prefix=body.prefix, next_number=body.next_number,
        request_id=ctx["request_id"],
    )


# --- audit -------------------------------------------------------------------


@router.get("/audit", operation_id="adminAuditList")
def audit_list(
    entity_type: str | None = None, action: str | None = None,
    entity_id: str | None = None, page: int = 1, per_page: int = 50,
    db=DB, user: PortalUser = Owner,
):
    if page < 1 or per_page < 1 or per_page > 200:
        raise ApiError("validation_failed",
                       fields=[{"field": "per_page", "message": "Use page ≥ 1 and 1 ≤ per_page ≤ 200."}])
    return portal_lib.audit_feed(
        db, entity_type=entity_type, action=action, entity_id=entity_id,
        page=page, per_page=per_page,
    )
