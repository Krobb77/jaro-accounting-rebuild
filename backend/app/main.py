"""Application factory — H2 fail-closed startup, middleware, routers.

``create_app`` is the uvicorn entry point (``uvicorn app.main:create_app
--factory``). There is deliberately NO module-level ``app`` object: import
must never boot the service, because boot is fail-closed (H2) and import
time has no validated configuration.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, advisory, mail, sessions
from . import storage as storage_lib
from .config import Settings, load_settings
from .db import create_app_engine, make_session_factory
from .transactions import TransactionMiddleware
from .errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from .logging_config import (
    AccessLogMiddleware,
    RequestIDMiddleware,
    configure_logging,
)
from .rate_limits import RateLimiter
from .routes import admin, booking, portal, public
from .runtime_check import verify_runtime


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # H2: refuse to boot on any misconfiguration — before binding anything.
    verify_runtime(settings)
    configure_logging()

    app = FastAPI(
        title="Jaro Accounting API",
        version=__version__,
        # Docs are served; the OpenAPI export is the G4 contract artifact.
    )
    app.state.settings = settings
    engine = create_app_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.rate_limiter = RateLimiter(
        settings.secret_key, advisory.get_backend(engine)
    )
    # Shared advisory-lock backend (F5/F6). On SQLite the threading
    # registry must be a single instance per app — a per-request backend
    # would not serialize concurrent requests (see advisory.py).
    app.state.lock_backend = advisory.get_backend(engine)
    app.state.mail_backend = mail.create_backend(settings)
    # Document vault + invoice PDFs: local-filesystem driver v1 (D-19).
    # Bytes live outside the web root, under STORAGE_DIR, in per-client
    # directories; the DB stores backend-qualified keys (local:…).
    _storage_registry = storage_lib.StorageRegistry()
    _storage_registry.register(storage_lib.LocalStorageBackend(settings.storage_dir))
    app.state.storage = _storage_registry

    # Exception handlers: every failure path normalizes to the A1 envelope.
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Middleware: first added = outermost. RequestID must wrap everything so
    # the header survives error paths.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        sessions.SessionMiddleware, session_factory=app.state.session_factory
    )
    # A4 request transaction: commit after the route returns the response
    # object, before the bytes are sent (yield-dependency teardown runs
    # after send — committing there races immediate client follow-ups).
    # Innermost: the transaction boundary hugs the route as tightly as
    # possible.
    app.add_middleware(TransactionMiddleware)

    app.include_router(public.router)
    app.include_router(booking.router)
    app.include_router(portal.router)
    app.include_router(admin.router)
    return app
