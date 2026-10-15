"""A1 — uniform safe error envelope.

Every failure path normalizes to ``{"code", "message", "request_id", "fields"}``
with ``X-Request-ID`` echoed on the response and ``Cache-Control: no-store``.
Client-supplied request IDs are never trusted; the ID is server-minted per
request by the RequestIDMiddleware.
"""

from __future__ import annotations

from fastapi import Request

from .site_contact import SITE_CONTACT
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# OWNER-REVIEWABLE (D-22; IMPLEMENTATION_PLAN §3.5 #8): guest-facing
# copy for the no-show hold (2 no-shows in 12 months → 422 booking_hold).
# Professional, firm wording: booking temporarily restricted + the phone
# number to resolve it. This constant is the single source of truth for the
# hold wording — the ERROR_CATALOG entry below references it.
BOOKING_HOLD_MESSAGE = (
    "We couldn't complete this booking automatically — "
    f"please call {SITE_CONTACT['phone_display']}."
)

# code -> (http status, default message). This catalog is the single source
# of truth shared by server, docs, and (later) typed clients.
ERROR_CATALOG: dict[str, tuple[int, str]] = {
    "not_implemented": (501, "This endpoint is not implemented yet."),
    "not_found": (404, "The requested resource was not found."),
    "unauthorized": (401, "Authentication failed."),
    "session_required": (401, "Authentication is required."),
    "session_expired": (401, "Your session has expired. Please sign in again."),
    "forbidden": (403, "You do not have permission to perform this action."),
    "terms_not_accepted": (403, "Engagement terms must be accepted first."),
    "validation_failed": (422, "The request could not be validated."),
    "idempotency_key_required": (
        422,
        "The Idempotency-Key header is required for this request.",
    ),
    "idempotency_conflict": (
        409,
        "A previous request with this idempotency key had a different payload. "
        "Generate a fresh key and submit again.",
    ),
    "rate_limited": (429, "Too many requests. Please slow down and try again."),
    "budget_unavailable": (503, "Service temporarily unavailable. Please try again."),
    "not_ready": (503, "Service is not ready."),
    "calendar_unavailable": (
        503,
        "Live availability is temporarily unavailable. Please try again shortly "
        f"or call {SITE_CONTACT['phone_display']}.",
    ),
    "conflict": (409, "The request conflicts with the current state."),
    "gone": (410, "The requested resource is no longer available."),
    # --- booking (Phase 3) -------------------------------------------------
    "slot_taken": (
        409,
        "That time was just taken — please pick another time.",
    ),
    "invalid_code": (
        422,
        "That code isn't right. Check the code and try again.",
    ),
    "code_expired": (410, "That code expired — request a new one."),
    "code_attempts_exceeded": (
        429,
        "Too many wrong attempts — request a new code.",
    ),
    "code_sends_exhausted": (
        429,
        "You've requested the maximum number of codes for this booking. "
        "Please start over.",
    ),
    "resend_cooldown": (
        429,
        "Please wait a moment before requesting another code.",
    ),
    "email_not_verified": (
        422,
        "Please verify your email address with the 6-digit code first.",
    ),
    "intent_expired": (
        410,
        "Your booking session expired — please start over.",
    ),
    "cancel_too_late": (
        422,
        "It's too late to cancel this booking online — "
        f"please call {SITE_CONTACT['phone_display']} and we'll help.",
    ),
    "reschedule_too_late": (
        422,
        "It's too late to reschedule this booking online — "
        f"please call {SITE_CONTACT['phone_display']} and we'll help.",
    ),
    "manage_token_expired": (
        410,
        f"This link has expired — please call {SITE_CONTACT['phone_display']} to rebook.",
    ),
    "booking_not_found": (404, "The requested booking could not be found."),
    "booking_hold": (422, BOOKING_HOLD_MESSAGE),
    # --- portal / invitations / auth (Phase 5) -------------------------------
    "bootstrap_consumed": (410, "Owner bootstrap has already been completed."),
    "bootstrap_unavailable": (
        503,
        "Owner bootstrap is not available in this environment.",
    ),
    "invitation_invalid": (
        410,
        "This invitation link is invalid or has expired. Please contact "
        f"Jared Robbins at {SITE_CONTACT['email']} or {SITE_CONTACT['phone_display']} "
        "for a new invitation.",
    ),
    "invitation_active": (
        409,
        "An active invitation already exists for this email. "
        "Resend or revoke it first.",
    ),
    "invitation_not_found": (404, "The requested invitation could not be found."),
    "invitation_not_active": (
        409,
        "This invitation is no longer active. Create a fresh invitation instead.",
    ),
    "invitation_not_revocable": (
        409,
        "This invitation can no longer be revoked (it is already terminal).",
    ),
    "client_exists": (
        409,
        "A client account already exists for this email.",
    ),
    "client_not_found": (404, "The requested client could not be found."),
    "invalid_credentials": (401, "Incorrect email or password."),
    "2fa_invalid": (401, "That code didn't work. Check your authenticator app and try again."),
    "challenge_consumed": (
        410,
        "This sign-in attempt has expired or was already used. "
        "Please sign in again.",
    ),
    "totp_required": (
        403,
        "Two-step verification setup is required before you can continue.",
    ),
    "password_change_required": (
        403,
        "You need to set your own password before you can continue.",
    ),
    "totp_already_enrolled": (409, "Two-step verification is already set up."),
    "reset_invalid": (
        410,
        "This reset link is invalid or has expired. Request a new one below.",
    ),
    "reset_not_found": (404, "The requested password reset could not be found."),
    "version_mismatch": (
        409,
        "The terms have changed since this page loaded. "
        "Please review the current terms and accept again.",
    ),
    "filing_not_found": (404, "The requested filing could not be found."),
    "filing_transition_invalid": (
        422,
        "This filing cannot move to the requested status from its current status.",
    ),
    "invoice_not_found": (404, "The requested invoice could not be found."),
    "invoice_not_payable": (409, "Only issued invoices can be marked paid."),
    "invoice_not_voidable": (409, "Only issued invoices can be voided."),
    "document_not_found": (404, "The requested document could not be found."),
    "document_rejected": (
        422,
        "This file can't be accepted. Check the file type and try again.",
    ),
    "file_too_large": (413, "This file is larger than the 25 MB limit."),
    "session_not_found": (404, "The requested session could not be found."),
    "cannot_revoke_current": (409, "Sign out to end your current session."),
    "internal_error": (500, "An unexpected error occurred."),
}


class ApiError(Exception):
    """Application error carrying a machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        fields: list[dict] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        status, default_message = ERROR_CATALOG.get(code, (500, "An unexpected error occurred."))
        super().__init__(message or default_message)
        self.code = code
        self.status = status
        self.message = message or default_message
        self.fields = fields or []
        self.headers = headers or {}


def error_body(
    code: str, message: str, request_id: str, fields: list[dict] | None = None
) -> dict:
    return {
        "code": code,
        "message": message,
        "request_id": request_id,
        "fields": fields or [],
    }


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _respond(request: Request, code: str, message: str, status: int, fields=None, headers=None):
    rid = _request_id(request)
    merged = {"X-Request-ID": rid, "Cache-Control": "no-store"}
    merged.update(headers or {})
    return JSONResponse(
        status_code=status,
        content=error_body(code, message, rid, fields),
        headers=merged,
    )


async def api_error_handler(request: Request, exc: ApiError):
    return _respond(request, exc.code, exc.message, exc.status, exc.fields, exc.headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Normalize framework HTTP exceptions (404s, 405s, ...) into the envelope.
    code = "not_found" if exc.status_code == 404 else "internal_error"
    if exc.status_code == 404:
        message = ERROR_CATALOG["not_found"][1]
    elif exc.status_code == 405:
        code, message = "not_found", "Method not allowed."
    else:
        message = str(exc.detail) if exc.detail else "An unexpected error occurred."
        if exc.status_code >= 500:
            code, message = "internal_error", ERROR_CATALOG["internal_error"][1]
    return _respond(request, code, message, exc.status_code)


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    fields = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        fields.append({"field": loc or "body", "message": err.get("msg", "invalid")})
    status, message = ERROR_CATALOG["validation_failed"]
    # Never echo raw input values: err dicts may contain them; we keep only
    # field names and messages.
    return _respond(request, "validation_failed", message, status, fields)


async def unhandled_exception_handler(request: Request, exc: Exception):
    # The traceback is logged server-side by the logging config; the wire
    # response stays generic (A6).
    import logging

    logging.getLogger("jaro.app").exception(
        "unhandled exception rid=%s type=%s", _request_id(request), type(exc).__name__
    )
    status, message = ERROR_CATALOG["internal_error"]
    return _respond(request, "internal_error", message, status)
