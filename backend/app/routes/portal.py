"""Portal routes — /api/v1/portal/* (D-05 canonical prefix).

Phase 5: full implementation — invitation accept, login + 2FA, password
reset, terms gate, sessions, F1 dashboard, F2 vault, F3 filings, F4
invoices. Route-owned transactions (A4); the domain module never commits.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, Response as RawResponse
from pydantic import Field
from pydantic_core import PydanticCustomError

from .. import idempotency as idem
from .. import portal as portal_lib
from .. import sessions
from ..errors import ApiError
from ..idempotency import ReplayHit, require_idempotency_key
from ..models import PortalUser
from ..rate_limits import (
    BudgetStoreError,
    RateLimited,
    rate_limit_dependency,
)
from . import DB, StrictModel

router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


# --- shared plumbing ------------------------------------------------------------


def _bind_locks(request: Request, db) -> None:
    """Bind the shared advisory-lock backend to the route-owned session."""
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
    # A3 replay: stored result, byte-identical, with replayed:true.
    # JSONResponse bypasses response_model so the replay flag survives.
    return JSONResponse(status_code=hit.status_code, content=hit.result)


_PRE_GATE_EXACT = {
    "/api/v1/portal/session",
    "/api/v1/portal/logout",
    "/api/v1/portal/sessions",
}
_PRE_GATE_PREFIXES = (
    "/api/v1/portal/terms/",
    "/api/v1/portal/password/",
    "/api/v1/portal/sessions/",
)


def _pre_gate_path(path: str) -> bool:
    if path in _PRE_GATE_EXACT:
        return True
    return path.startswith(_PRE_GATE_PREFIXES)


def client(request: Request, db=DB) -> PortalUser:
    """Authenticated client with the onboarding gates.

    Until a temporary password is replaced, every portal route except
    password-change/terms/session returns 403 password_change_required;
    until terms are accepted, the same set returns 403 terms_not_accepted.
    (Password first: the first-run flow is temp-password login →
    forced password change → terms modal → portal.)
    """
    view = sessions.require_session(request)
    user = db.get(PortalUser, view.user_id)
    if (
        user is None
        or user.role != "client"
        or user.status not in ("pending_first_login", "active")
    ):
        raise ApiError("forbidden")
    if not _pre_gate_path(request.url.path):
        if user.must_change_password:
            raise ApiError("password_change_required")
        if user.terms_accepted_at is None:
            raise ApiError("terms_not_accepted")
    return user


def authed(request: Request) -> sessions.SessionView:
    return sessions.require_session(request)


Client = Depends(client)
Authed = Depends(authed)


async def _upload_budget(request: Request):
    """A5 for uploads: source / route-source / per-client target / global.
    The target comes from the session (multipart bodies have no JSON
    field for the shared dependency to read)."""
    limiter = request.app.state.rate_limiter
    db = request.app.state.session_factory()
    try:
        source = request.client.host if request.client else "unknown"
        view = getattr(request.state, "session", None)
        target = view.user_id if view is not None else None
        try:
            limiter.check(db, budget="portal.upload", source=source, target=target)
            db.commit()
        except RateLimited as exc:
            db.rollback()
            raise ApiError("rate_limited", headers={"Retry-After": str(exc.retry_after)})
        except BudgetStoreError:
            db.rollback()
            raise ApiError("budget_unavailable")
    finally:
        db.close()


# --- request models (A2 closed) ---------------------------------------------------


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class PasswordResetRequestRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetVerifyRequest(StrictModel):
    token: str = Field(min_length=20, max_length=128)


class PasswordResetCompleteRequest(StrictModel):
    token: str = Field(min_length=20, max_length=128)
    new_password: str = Field(min_length=12, max_length=256)


class TermsAcceptRequest(StrictModel):
    version_hash: str = Field(min_length=16, max_length=128)


class PasswordChangeRequest(StrictModel):
    # Optional: omitted for forced changes (temporary-password onboarding /
    # owner reset), where the session itself proves prior-password knowledge.
    current_password: str | None = Field(default=None, max_length=1024)
    new_password: str = Field(min_length=12, max_length=256)


# --- login (public; password-only) ---------------------------------------------------------------------------------------------------------------------------


@router.post(
    "/login",
    status_code=202,
    operation_id="portalLogin",
    dependencies=[Depends(rate_limit_dependency("portal.login", target_from="email"))],
)
def portal_login(body: LoginRequest, request: Request, response: Response, db=DB):
    ctx = _ctx(request)
    _bind_locks(request, db)
    result = portal_lib.client_login(
        db, email=body.email, password=body.password,
        request_id=ctx["request_id"], ip=ctx["ip"], user_agent=ctx["user_agent"],
        secret_key=ctx["secret_key"],
    )
    return _set_session_cookie(response, result)


# --- password reset (public) ---------------------------------------------------------------


@router.post(
    "/password-reset/request",
    operation_id="portalPasswordResetRequest",
    dependencies=[Depends(rate_limit_dependency("portal.password_reset", target_from="email"))],
)
def password_reset_request(body: PasswordResetRequestRequest, request: Request, db=DB):
    ctx = _ctx(request)
    # Always 200 — no account-existence oracle.
    return portal_lib.password_reset_request(
        db, email=body.email, request_id=ctx["request_id"],
        ip=ctx["ip"], secret_key=ctx["secret_key"],
    )


@router.post(
    "/password-reset/verify",
    operation_id="portalPasswordResetVerify",
    dependencies=[Depends(rate_limit_dependency("portal.password_reset", target_from="token"))],
)
def password_reset_verify(body: PasswordResetVerifyRequest, request: Request, db=DB):
    return portal_lib.password_reset_verify(db, raw_token=body.token)


@router.post(
    "/password-reset/complete",
    operation_id="portalPasswordResetComplete",
    dependencies=[Depends(rate_limit_dependency("portal.password_reset", target_from="token"))],
)
def password_reset_complete(body: PasswordResetCompleteRequest, request: Request, db=DB):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    try:
        return portal_lib.password_reset_complete(
            db, raw_token=body.token, password=body.new_password, idem_key=key,
            request_id=ctx["request_id"], ip=ctx["ip"], secret_key=ctx["secret_key"],
        )
    except ReplayHit as hit:
        return _replay(hit)


# --- session (authoritative expiry for the warning banner, D-44) --------------------------------------


@router.get("/session", operation_id="portalSessionStatus")
def session_status(db=DB, session: sessions.SessionView = Authed):
    user = db.get(PortalUser, session.user_id)
    if user is None:
        raise ApiError("session_expired")
    return portal_lib.session_status_payload(db, session, user)


@router.post("/logout", operation_id="portalLogout")
def portal_logout(request: Request, response: Response, db=DB, session: sessions.SessionView = Authed):
    ctx = _ctx(request)
    user = db.get(PortalUser, session.user_id)
    if user is not None:
        portal_lib.logout(db, view=session, user=user, request_id=ctx["request_id"])
    else:
        sessions.revoke_all_sessions(db, session.user_id)
    sessions.clear_session_cookie(response)
    return {"ok": True}


@router.delete("/session", operation_id="portalSessionDelete")
def session_delete(request: Request, response: Response, db=DB, session: sessions.SessionView = Authed):
    """Logout alias (AUTH §7 #32)."""
    return portal_logout(request, response, db=db, session=session)


@router.post("/sessions/extend", operation_id="portalSessionExtend")
def session_extend(request: Request, db=DB, session: sessions.SessionView = Authed):
    """One-click extend (D-44): idempotent, ≤1 effective extend/60s, cannot
    resurrect an expired session."""
    user = db.get(PortalUser, session.user_id)
    if user is None:
        raise ApiError("session_expired")
    return portal_lib.extend_session(db, view=session, user=user)


@router.get("/sessions", operation_id="portalSessionList")
def session_list(db=DB, user: PortalUser = Client, session: sessions.SessionView = Authed):
    return portal_lib.session_inventory(db, user=user, current_session_id=session.id)


@router.delete("/sessions/{session_id}", operation_id="portalSessionRevoke")
def session_revoke(session_id: str, request: Request, db=DB, user: PortalUser = Client,
                   session: sessions.SessionView = Authed):
    ctx = _ctx(request)
    # The inventory truncates IDs for display (A6); accept the full digest
    # or the truncated display form.
    full_id = session_id
    if len(session_id) < 64:
        from sqlalchemy import select as _select
        from ..models import Session as SessionRow

        candidates = (
            db.execute(
                _select(SessionRow).where(SessionRow.user_id == user.id,
                                         SessionRow.revoked_at.is_(None))
            ).scalars().all()
        )
        match = next((c for c in candidates if c.id.startswith(session_id.rstrip("…"))), None)
        if match is None:
            raise ApiError("session_not_found")
        full_id = match.id
    portal_lib.revoke_own_session(
        db, user=user, session_id=full_id, current_session_id=session.id,
        request_id=ctx["request_id"],
    )
    return {"ok": True}


@router.post("/sessions/{session_id}/revoke", operation_id="portalSessionRevokePost", include_in_schema=False)
def session_revoke_post(session_id: str, request: Request, db=DB, user: PortalUser = Client,
                        session: sessions.SessionView = Authed):
    """POST alias for the skeleton's original shape (not in the inventory)."""
    return session_revoke(session_id, request, db=db, user=user, session=session)


# --- terms gate ---------------------------------------------------------------------


@router.get("/terms/current", operation_id="portalTermsCurrent")
def terms_current(db=DB, session: sessions.SessionView = Authed):
    return portal_lib.terms_current(db)


@router.post("/terms/accept", operation_id="portalTermsAccept")
def terms_accept(body: TermsAcceptRequest, request: Request, db=DB, session: sessions.SessionView = Authed):
    ctx = _ctx(request)
    user = db.get(PortalUser, session.user_id)
    if user is None or user.role != "client":
        raise ApiError("forbidden")
    return portal_lib.terms_accept(db, user=user, version_hash=body.version_hash,
                                   request_id=ctx["request_id"])


@router.post("/password/change", operation_id="portalPasswordChange")
def password_change(body: PasswordChangeRequest, request: Request, response: Response,
                    db=DB, user: PortalUser = Client, session: sessions.SessionView = Authed):
    """Password change: forced (temporary password — no current password
    needed) or voluntary (in-settings — current password required)."""
    _bind_locks(request, db)
    ctx = _ctx(request)
    result = portal_lib.change_client_password(
        db, user=user, new_password=body.new_password,
        current_password=body.current_password, session_id=session.id,
        request_id=ctx["request_id"], ip=ctx["ip"],
        user_agent=ctx["user_agent"], secret_key=ctx["secret_key"],
    )
    return _set_session_cookie(response, result)


# --- F1 dashboard -----------------------------------------------------------------------


@router.get("/dashboard", operation_id="portalDashboard")
def dashboard(request: Request, db=DB, user: PortalUser = Client):
    ctx = _ctx(request)
    return portal_lib.dashboard(db, user=user, request_id=ctx["request_id"])


# --- F2 document vault -----------------------------------------------------------------------


@router.get("/documents", operation_id="portalDocumentsList")
def documents_list(db=DB, user: PortalUser = Client):
    return portal_lib.list_documents(db, user=user)


@router.post(
    "/documents",
    status_code=201,
    operation_id="portalDocumentsUpload",
    dependencies=[Depends(_upload_budget)],
)
async def documents_upload(
    request: Request,
    db=DB,
    user: PortalUser = Client,
    file: UploadFile = File(...),
    type_code: str = Form("other"),
    label: str | None = Form(None),
):
    key = require_idempotency_key(request)
    _bind_locks(request, db)
    ctx = _ctx(request)
    # Chunked read: reject >25MB with 413 BEFORE storage, never buffer
    # unbounded input.
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
            db, user=user, uploaded_by="client", filename=file.filename or "upload",
            data=data, type_code=type_code, label=label, idem_key=key,
            request_id=ctx["request_id"], storage=ctx["storage"], actor=user,
        )
    except ReplayHit as hit:
        return _replay(hit)


@router.get("/documents/{document_id}/download", operation_id="portalDocumentDownload")
def document_download(document_id: str, request: Request, db=DB, user: PortalUser = Client):
    ctx = _ctx(request)
    data, row = portal_lib.get_document_bytes(
        db, user=user, document_id=document_id, storage=ctx["storage"],
        request_id=ctx["request_id"], actor=user,
    )
    disp = f"attachment; filename*=UTF-8''{quote(row.original_filename)}"
    return RawResponse(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
    )


# --- F3 filings -----------------------------------------------------------------------


@router.get("/filings", operation_id="portalFilingsList")
def filings_list(db=DB, user: PortalUser = Client):
    return portal_lib.list_filings(db, user=user)


@router.get("/filings/{filing_id}", operation_id="portalFilingGet")
def filing_get(filing_id: str, db=DB, user: PortalUser = Client):
    return portal_lib.filing_timeline(db, user=user, filing_id=filing_id)


# --- F4 invoices -----------------------------------------------------------------------


@router.get("/invoices", operation_id="portalInvoicesList")
def invoices_list(db=DB, user: PortalUser = Client):
    return portal_lib.list_invoices(db, user=user)


@router.get("/invoices/{invoice_id}", operation_id="portalInvoiceGet")
def invoice_get(invoice_id: str, db=DB, user: PortalUser = Client):
    return portal_lib.get_client_invoice(db, user=user, invoice_id=invoice_id)


@router.get("/invoices/{invoice_id}/pdf", operation_id="portalInvoicePdf")
def invoice_pdf(invoice_id: str, request: Request, db=DB, user: PortalUser = Client):
    ctx = _ctx(request)
    data, row = portal_lib.get_invoice_pdf_bytes(
        db, user=user, invoice_id=invoice_id, storage=ctx["storage"],
        request_id=ctx["request_id"], actor=user,
    )
    disp = f"attachment; filename*=UTF-8''{quote(row.invoice_number + '.pdf')}"
    return RawResponse(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
    )
