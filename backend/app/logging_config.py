"""A6 — PII-safe logging.

Sanitization happens at the filter layer (process entry point), not by
developer discipline at each call site:

- ``PIIRedactingFilter`` strips emails, phone numbers, bearer tokens,
  cookie values, and password/code JSON fields from every log record.
- The access log middleware logs ``METHOD path -> status`` with the query
  string stripped — never params.
- Health probes collapse driver exceptions to booleans (drivers embed
  connection strings and SQL params).
"""

from __future__ import annotations

import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_PATTERNS = [
    # email addresses
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED-EMAIL]"),
    # phone-ish: +1-778-227-4891, (778) 227-4891, 7782274891 (7+ digits w/ separators)
    (re.compile(r"\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"), "[REDACTED-PHONE]"),
    # bearer tokens / basic auth
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]+"), r"\1 [REDACTED]"),
    # session cookie value
    (re.compile(r"__Host-jaro_session=[^;\s]+"), "__Host-jaro_session=[REDACTED]"),
    # token/code/password in query strings or free text
    (re.compile(r"(?i)\b(token|idempotency-key|set_password_url)=([^&\s]+)"), r"\1=[REDACTED]"),
    # JSON fields that carry secrets
    (re.compile(r'(?i)("(?:password|new_password|code|token|secret|totp)"\s*:\s*")[^"]*(")'), r"\1[REDACTED]\2"),
]


class PIIRedactingFilter(logging.Filter):
    """Process-entry-point redaction. Applied to handlers once in
    ``configure_logging``; safe to apply twice (idempotent-ish)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for pattern, replacement in _PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Install the PII filter on the root handler chain + jaro loggers."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    filt = PIIRedactingFilter()
    for handler in root.handlers:
        if not any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
            handler.addFilter(filt)
    # Attach to named loggers too (they may have their own handlers).
    for name in ("jaro.app", "jaro.worker", "jaro.outbox", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if not any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
                handler.addFilter(filt)


# A6: manage tokens ride in the URL path (/booking/manage/{token}); the
# path segment is redacted so bearer tokens never land in access logs.
_MANAGE_TOKEN_PATH = re.compile(r"(/api/v1/booking/manage/)[^/]+")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One-line access logs. Path only — the query string is stripped (A6),
    and the booking manage-token path segment is redacted."""

    def __init__(self, app, logger_name: str = "jaro.app.access"):
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        rid = getattr(request.state, "request_id", "-")
        # request.url.path — never the query string; manage-token segment
        # redacted (A6: tokens never appear in logs).
        path = _MANAGE_TOKEN_PATH.sub(r"\1[REDACTED]", request.url.path)
        self._logger.info(
            "%s %s -> %s %sms rid=%s", request.method, path, response.status_code, elapsed_ms, rid
        )
        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Server-minted request ID (A1). Client-supplied IDs are never trusted."""

    HEADER = "X-Request-ID"

    async def dispatch(self, request: Request, call_next):
        from .db import new_uuid7

        request.state.request_id = new_uuid7().replace("-", "")
        response = await call_next(request)
        response.headers.setdefault(self.HEADER, request.state.request_id)
        return response
