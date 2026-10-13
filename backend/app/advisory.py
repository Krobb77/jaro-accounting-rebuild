"""Advisory-lock abstraction (F5/F6 atomicity without Redis).

Production (Postgres): ``pg_advisory_xact_lock`` — transaction-scoped, so
the lock is held for exactly the route-owned transaction that needs the
atomic check-and-write, and released on commit/rollback.

Local/test (SQLite): a process-local threading lock registry keyed by the
same 64-bit key. This is NOT cross-process; it exists so the SQLite path
used for local verification keeps the same call shape. Documented in
backend/DECISIONS-backend.md.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, contextmanager
from typing import Iterator


def advisory_key(*parts: str) -> int:
    """Derive a signed 64-bit advisory-lock key from opaque parts."""
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class AdvisoryLockBackend(ABC):
    @abstractmethod
    def acquire(self, db, key: int) -> AbstractContextManager[None]:
        """Return a context manager holding the advisory lock for ``key``."""


class PostgresAdvisoryLockBackend(AdvisoryLockBackend):
    def acquire(self, db, key: int) -> AbstractContextManager[None]:
        from sqlalchemy import text

        @contextmanager
        def _held() -> Iterator[None]:
            # xact-scoped: held until the current transaction ends.
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            yield

        return _held()


class ThreadingAdvisoryLockBackend(AdvisoryLockBackend):
    """Process-local fallback for SQLite. Same call shape, no cross-process
    guarantees — local verification only."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}

    def acquire(self, db, key: int) -> AbstractContextManager[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())

        @contextmanager
        def _held() -> Iterator[None]:
            lock.acquire()
            try:
                yield
            finally:
                lock.release()

        return _held()


def get_backend(engine) -> AdvisoryLockBackend:
    if engine.dialect.name == "postgresql":
        return PostgresAdvisoryLockBackend()
    return ThreadingAdvisoryLockBackend()
