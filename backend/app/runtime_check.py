"""H2 — fail-closed startup verification.

``verify_runtime`` refuses to boot unless ALL of these hold:
1. JARO_MODE is on the explicit allowlist (no inferred/preview modes).
2. Every required secret resolves (NAME xor NAME_FILE; value-free errors).
3. DB_ROLE matches the workload's expected role (jaro_web/jaro_worker/jaro_migrate).
4. The database is at the pinned Alembic head (code head == db version).
5. MAIL_ALLOW_SEND is exactly "true" or "false" (unset/misspelled refuses).
6. MAIL_BACKEND is a known backend; DATABASE_URL parses.

Each check is a separate function so tests can assert every refusal case.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from . import alembic_heads
from .config import (
    DB_ROLES,
    EXPECTED_ROLES,
    MAIL_BACKENDS,
    MODES,
    WORKLOADS,
    SecretError,
    Settings,
)


class RuntimeRefused(Exception):
    """verify_runtime refused to boot. The message is actionable and contains
    no secret values."""


def check_mode(settings: Settings) -> None:
    if settings.mode not in MODES:
        raise RuntimeRefused(
            f"JARO_MODE={settings.mode!r} is not on the allowlist {list(MODES)}; "
            "refusing to boot in an unreviewed mode"
        )


def check_workload(settings: Settings) -> None:
    if settings.workload not in WORKLOADS:
        raise RuntimeRefused(f"JARO_WORKLOAD={settings.workload!r} unknown; expected one of {list(WORKLOADS)}")


def check_secrets(settings: Settings) -> None:
    # load_settings already enforces NAME xor NAME_FILE and value shape;
    # here we assert the resolved values meet minimum strength.
    if len(settings.secret_key) < 16:
        raise RuntimeRefused("SECRET_KEY is too short (minimum 16 bytes)")
    if len(settings.totp_kek) != 32:
        raise RuntimeRefused("TOTP_KEK must normalize to exactly 32 bytes")


def check_db_role(settings: Settings) -> None:
    expected = EXPECTED_ROLES.get(settings.workload)
    if settings.db_role not in DB_ROLES:
        raise RuntimeRefused(
            f"DB_ROLE={settings.db_role!r} is not a known role {list(DB_ROLES)}"
        )
    if settings.db_role != expected:
        raise RuntimeRefused(
            f"DB_ROLE={settings.db_role!r} does not match workload "
            f"{settings.workload!r} (expected {expected!r}); refusing to boot"
        )


def check_database_url(settings: Settings) -> None:
    url = settings.database_url
    if not url:
        raise RuntimeRefused("DATABASE_URL is not set")
    scheme = url.split(":", 1)[0]
    if scheme not in {"postgresql", "postgresql+psycopg2", "postgresql+psycopg", "sqlite"}:
        raise RuntimeRefused(f"DATABASE_URL scheme {scheme!r} is not supported")


def check_mail_gate(settings: Settings) -> None:
    raw = settings.mail_allow_send_raw
    if raw not in ("true", "false"):
        raise RuntimeRefused(
            "MAIL_ALLOW_SEND must be exactly 'true' or 'false' "
            f"(got {raw!r}); refusing to boot rather than guessing"
        )
    if settings.mail_backend not in MAIL_BACKENDS:
        raise RuntimeRefused(f"MAIL_BACKEND={settings.mail_backend!r} unknown; expected one of {list(MAIL_BACKENDS)}")


def _db_version(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
                if engine.dialect.name == "sqlite"
                else text(
                    "SELECT tablename FROM pg_tables WHERE tablename = 'alembic_version'"
                ),
            ).first()
            if not exists:
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row else None
    finally:
        engine.dispose()


def check_alembic_head(settings: Settings) -> None:
    expected = alembic_heads.head_revision()
    actual = _db_version(settings.database_url)
    if actual != expected:
        raise RuntimeRefused(
            f"database alembic version {actual!r} != pinned head {expected!r}; "
            "run migrations before booting (H5: migration-first releases)"
        )


def verify_runtime(settings: Settings) -> None:
    """Run every startup check; raise RuntimeRefused on the first failure."""
    check_mode(settings)
    check_workload(settings)
    check_secrets(settings)
    check_db_role(settings)
    check_database_url(settings)
    check_mail_gate(settings)
    check_alembic_head(settings)
