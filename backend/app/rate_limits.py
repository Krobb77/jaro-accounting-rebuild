"""A5 — tiered rate budgets over Postgres (no Redis).

Four dimensions per check, evaluated atomically under an advisory lock per
key (the no-burst guarantee comes from atomicity, not the store):
  source        — per client IP
  route_source  — per route per IP
  target        — per victim resource (email, slot, ...), lower for
                  recovery-style flows
  global        — one shared budget

Keys are HMAC-pseudonymized (never raw IPs/emails). All keys are checked
before any increment (no burst slipping). Store failure fails CLOSED
(503), never open.

Budgets below are starting values (D-21); Jared tunes them with load data
in Phase 4.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from . import advisory
from .db import hmac_hex, utcnow
from .errors import ApiError
from .models import RateWindow

# route-name -> dimension -> (limit, window_seconds)
BUDGETS: dict[str, dict[str, tuple[int, int]]] = {
    "portal.login": {
        "source": (20, 3600),
        "route_source": (20, 3600),
        "target": (10, 3600),  # per email — credential-stuffing defense
        "global": (1000, 3600),
    },
    "booking.intents": {
        "source": (30, 3600),
        "route_source": (10, 3600),
        "target": (5, 3600),  # per email — code-send budget
        "global": (1000, 3600),
    },
    "booking.confirm": {
        "source": (30, 3600),
        "route_source": (20, 3600),
        "target": (10, 3600),  # per slot
        "global": (1000, 3600),
    },
    "booking.slots": {
        "source": (300, 3600),
        "route_source": (120, 3600),
        "global": (1000, 3600),
    },
    "inquiries.submit": {
        "source": (30, 3600),
        "route_source": (30, 3600),
        # Per recipient email: blocks form-abuse receipt mail loops (each
        # submit queues a receipt to the form's address). Starting value
        # (D-21); Jared tunes with load data in Phase 4.
        "target": (5, 3600),
        "global": (1000, 3600),
    },
    # --- portal / auth (Phase 5) -------------------------------------------------
    # Starting values (D-21); recovery-style flows get a tight target
    # dimension so one account/token can't be hammered (AUTH A5).
    "portal.invite_verify": {
        "source": (30, 3600),
        "route_source": (30, 3600),
        "target": (20, 3600),  # per token
        "global": (1000, 3600),
    },
    "portal.set_password": {
        "source": (30, 3600),
        "route_source": (20, 3600),
        "target": (10, 3600),  # per token
        "global": (1000, 3600),
    },
    "portal.mfa": {
        "source": (60, 3600),
        "route_source": (30, 3600),
        "target": (20, 3600),  # per challenge
        "global": (1000, 3600),
    },
    "portal.password_reset": {
        "source": (30, 3600),
        "route_source": (20, 3600),
        "target": (5, 3600),  # per email — reset-mail harassment defense
        "global": (1000, 3600),
    },
    "portal.upload": {
        "source": (60, 3600),
        "route_source": (60, 3600),
        "target": (30, 3600),  # per client — upload abuse surface
        "global": (1000, 3600),
    },
    "admin.login": {
        "source": (20, 3600),
        "route_source": (10, 3600),
        "target": (5, 3600),  # per email
        "global": (1000, 3600),
    },
    "admin.bootstrap": {
        "source": (20, 3600),
        "route_source": (10, 3600),
        "global": (1000, 3600),
    },
    "admin.invitations": {
        "source": (60, 3600),
        "route_source": (30, 3600),
        "global": (1000, 3600),
    },
}


class BudgetStoreError(Exception):
    """The rate_windows store failed — callers must fail closed (503)."""


class RateLimited(Exception):
    """Raised when a budget is exceeded; carries Retry-After seconds."""

    def __init__(self, retry_after: int):
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


class RateLimiter:
    def __init__(self, hmac_key: bytes, lock_backend: advisory.AdvisoryLockBackend):
        if not hmac_key:
            raise ValueError("hmac_key is required")
        self._hmac_key = hmac_key
        self._locks = lock_backend

    def _key(self, dimension: str, value: str) -> str:
        # HMAC-pseudonymized: raw IPs/emails never touch the table (A5/A6).
        return hmac_hex(self._hmac_key, "jaro-ratelimit-v1", dimension, value)

    def check(
        self,
        db,
        *,
        budget: str,
        source: str,
        target: str | None = None,
    ) -> None:
        """Evaluate the four dimensions for ``budget``. Raises RateLimited
        ( -> 429 + Retry-After) or BudgetStoreError ( -> 503 fail-closed)."""
        spec = BUDGETS[budget]
        # Every dimension is namespaced by budget: the spec lists per-budget
        # limits, so the `source` counter must not be shared across budgets
        # (it was: the key omitted the budget, so availability browsing
        # burned through the confirm budget's per-IP allowance → spurious
        # 429s). The dimension name keeps `source` and `route_source` rows
        # distinct even though both are per (budget, source).
        dims: list[tuple[str, str, int, int]] = [
            ("source", f"{budget}:{source}", *spec["source"]),
            ("route_source", f"{budget}:{source}", *spec["route_source"]),
        ]
        if target is not None and "target" in spec:
            dims.append(("target", f"{budget}:{target}", *spec["target"]))
        dims.append(("global", budget, *spec["global"]))

        keys = [(dim, self._key(dim, value), limit, window) for dim, value, limit, window in dims]
        # Lock ordering is fixed (sorted) to avoid deadlock between concurrent
        # checkers; locks are advisory + transaction-scoped on Postgres.
        try:
            with _multi_lock(self._locks, db, sorted(k for _, k, _, _ in keys)):
                now = utcnow()
                states = []
                for dim, key, limit, window in keys:
                    row = db.get(RateWindow, key)
                    if row is None or (now - row.window_start).total_seconds() >= row.window_seconds:
                        states.append((key, limit, window, now, 0))
                    else:
                        states.append((key, limit, row.window_seconds, row.window_start, row.count))
                # Check ALL before incrementing ANY (no burst slipping).
                for key, limit, window, window_start, count in states:
                    if count >= limit:
                        retry_after = max(
                            1,
                            int(window - (now - window_start).total_seconds()),
                        )
                        raise RateLimited(retry_after)
                for key, limit, window, window_start, count in states:
                    row = db.get(RateWindow, key)
                    if row is None or (now - row.window_start).total_seconds() >= row.window_seconds:
                        db.merge(
                            RateWindow(
                                key_hash=key,
                                window_start=now,
                                window_seconds=window,
                                count=1,
                            )
                        )
                    else:
                        row.count = count + 1
                db.flush()
        except RateLimited:
            raise
        except SQLAlchemyError as exc:
            raise BudgetStoreError("rate budget store unavailable") from exc


class _multi_lock:
    def __init__(self, backend, db, keys: list[str]):
        self._backend = backend
        self._db = db
        self._keys = keys
        self._held = []

    def __enter__(self):
        for k in self._keys:
            cm = self._backend.acquire(self._db, advisory.advisory_key("ratelimit", k))
            cm.__enter__()
            self._held.append(cm)
        return self

    def __exit__(self, *exc):
        for cm in reversed(self._held):
            cm.__exit__(*exc)
        return False


def rate_limit_dependency(budget: str, *, target_from: str | None = None):
    """FastAPI dependency factory: enforce ``budget`` before the route.

    ``target_from`` names the JSON body field used as the target dimension
    (e.g. "email"); when absent the target dimension is skipped. The
    check-and-increment runs in its own short-lived transaction so a route
    rollback cannot refund the attempt.
    """

    async def _dep(request: Request):
        limiter: RateLimiter = request.app.state.rate_limiter
        db = request.app.state.session_factory()
        try:
            source = request.client.host if request.client else "unknown"
            target = None
            if target_from:
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        value = body.get(target_from)
                        target = str(value).strip() if value else None
                except Exception:
                    target = None
            try:
                limiter.check(db, budget=budget, source=source, target=target)
                db.commit()
            except RateLimited as exc:
                db.rollback()
                raise ApiError("rate_limited", headers={"Retry-After": str(exc.retry_after)})
            except BudgetStoreError:
                db.rollback()
                raise ApiError("budget_unavailable")
        finally:
            db.close()

    return _dep
