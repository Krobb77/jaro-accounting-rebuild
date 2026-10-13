"""DONE 1 — migration rehearsal, single head, B1 fail-closed downgrades.

1. `alembic upgrade head -> downgrade base -> upgrade head` green on a
   disposable database; single head.
2. Downgrade with live portal_invitations/bookings rows refuses (B1) with a
   clear error.
"""

from __future__ import annotations

import os
import sys

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import alembic_heads, models  # noqa: E402
from app.db import new_uuid7, utcnow  # noqa: E402


def _cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic")),
    )
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _tables(db_url: str) -> set:
    engine = sa.create_engine(db_url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_single_head_and_linear_history():
    assert alembic_heads.head_revision() == "0014"
    history = alembic_heads.linear_history()
    assert history == ["0001", "0002", "0003", "0004", "0005", "0006", "0007",
                       "0008", "0009", "0010", "0011", "0012", "0013", "0014"]


def test_upgrade_downgrade_upgrade_cycle(scratch_db_url):
    cfg = _cfg(scratch_db_url)
    alembic_command.upgrade(cfg, "head")
    assert "portal_users" in _tables(scratch_db_url)

    alembic_command.downgrade(cfg, "base")
    assert _tables(scratch_db_url) == {"alembic_version"}

    alembic_command.upgrade(cfg, "head")
    tables = _tables(scratch_db_url)
    assert set(models.TABLE_NAMES) <= tables


def test_migrated_schema_matches_models(scratch_db_url):
    """Structural parity: every model table exists with the same columns."""
    cfg = _cfg(scratch_db_url)
    alembic_command.upgrade(cfg, "head")
    engine = sa.create_engine(scratch_db_url)
    try:
        insp = sa.inspect(engine)
        for table_name in models.TABLE_NAMES:
            assert table_name in insp.get_table_names(), f"missing table {table_name}"
            db_cols = {c["name"] for c in insp.get_columns(table_name)}
            model_cols = set(models.Base.metadata.tables[table_name].columns.keys())
            assert db_cols == model_cols, f"column drift in {table_name}: {db_cols ^ model_cols}"
    finally:
        engine.dispose()


def _insert_live_rows(db_url: str):
    engine = sa.create_engine(db_url)
    try:
        with engine.begin() as conn:
            now = utcnow().replace(tzinfo=None).isoformat(sep=" ")
            conn.execute(
                sa.text(
                    "INSERT INTO portal_invitations (id, email, client_name, token_hash, status,"
                    " created_at, expires_at) VALUES (:id, :email, :name, :th, 'emailed', :now,"
                    " datetime(:now, '+7 days'))"
                ),
                {"id": new_uuid7(), "email": "client@example.com", "name": "Test Client",
                 "th": "ab" * 32, "now": now},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO bookings (id, reference_number, event_code, slot_start, slot_end,"
                    " guest_timezone, guest_name, guest_email, status, payload_hash, created_at,"
                    " updated_at) VALUES (:id, 'JARO-2026-000001', 'free-30-min-consultation',"
                    " :now, datetime(:now, '+30 minutes'), 'America/Vancouver', 'Guest',"
                    " 'guest@example.com', 'pending_verification', :ph, :now, :now)"
                ),
                {"id": new_uuid7(), "ph": "cd" * 32, "now": now},
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ["base", "0002"])
def test_downgrade_refuses_with_live_rows(scratch_db_url, target):
    """B1: downgrade with live portal_invitations/bookings rows refuses."""
    cfg = _cfg(scratch_db_url)
    alembic_command.upgrade(cfg, "head")
    _insert_live_rows(scratch_db_url)
    with pytest.raises(RuntimeError, match="B1 fail-closed downgrade refused"):
        alembic_command.downgrade(cfg, target)


def test_downgrade_succeeds_without_live_rows(scratch_db_url):
    cfg = _cfg(scratch_db_url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")  # must not raise
    assert _tables(scratch_db_url) == {"alembic_version"}


def test_reference_number_format():
    """D-11: booking references look like JARO-2026-XXXXXX."""
    from app import refnums

    ref = refnums.new_reference_number()
    assert refnums.is_valid_reference_number(ref)
    assert ref.startswith("JARO-")
    assert len(ref) == len("JARO-2026-XXXXXX")
    assert len({refnums.new_reference_number() for _ in range(100)}) == 100
    assert not refnums.is_valid_reference_number("JA-123456")
    assert not refnums.is_valid_reference_number("JARO-2026-XXXXXX-extra")
