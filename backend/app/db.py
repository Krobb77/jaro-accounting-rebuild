"""Database primitives: engine factory, sessions, time/UUID helpers.

Sync SQLAlchemy is the Phase-0 choice: the venv ships no async DB driver
(no aiosqlite/asyncpg), and the same sync code runs against Postgres with a
DBAPI driver such as psycopg2. Async remains a documented future option.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """TIMESTAMPTZ that round-trips as tz-aware UTC on every dialect.

    SQLite stores naive datetimes; without normalization, naive/aware
    comparisons explode. This type stores naive UTC and always returns
    aware UTC, so Python-side comparisons are consistent on SQLite and
    Postgres alike.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def new_uuid7() -> str:
    """Generate a UUIDv7 (time-ordered) as a canonical 8-4-4-4-12 string."""
    ts_ms = int(time.time() * 1000)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    val = (ts_ms << 80) | (7 << 76) | (rand_a << 62) | (0b10 << 60) | rand_b
    h = f"{val:032x}"
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def sha256_hex(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def hmac_hex(key: bytes, *parts: str) -> str:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(b"\x00")
        mac.update(part.encode("utf-8"))
    return mac.hexdigest()


def create_app_engine(database_url: str, *, pool_size: int = 5):
    """Create the SQLAlchemy engine for the given URL.

    SQLite (local/dev/test) uses a file database; Postgres is the deploy
    target. ``check_same_thread=False`` is required because the ASGI server
    serves requests from a thread pool.
    """
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # Keep the default QueuePool for file-backed SQLite; it is safe for
        # the single-process web/worker topology of Phase 0.
    else:
        kwargs["pool_size"] = pool_size
        kwargs["pool_pre_ping"] = True
    return create_engine(database_url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
