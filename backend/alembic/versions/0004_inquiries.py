"""0004 — contact inquiries (lead pipeline table).

Blast radius: creates inquiries. No data touched (fresh table).
Downgrade policy: plain drop (no B1-protected table here).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0004"
down_revision = "0003"

import sqlalchemy as sa
from alembic import op

from migration_helpers import create_table_idempotent, drop_table_if_exists
from app.db import UTCDateTime

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    inquiries = sa.Table(
        "inquiries",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'owner_notified', 'receipt_sent', 'archived')",
            name="ck_inquiries_status",
        ),
    )
    return (inquiries,)


def upgrade() -> None:
    for table in _tables():
        create_table_idempotent(op, table)


def downgrade() -> None:
    drop_table_if_exists(op, "inquiries")
