"""0011 — calendar sync state.

Blast radius: creates calendar_sync_state (one row per calendar source).
No data touched.
Downgrade policy: drops the table — it holds derived worker state (the
last successful poll time), which the worker rebuilds on its next poll.
No live user data.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0011"
down_revision = "0010"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_table_idempotent,
    drop_table_if_exists,
)
from app.db import UTCDateTime


def upgrade() -> None:
    metadata = sa.MetaData()
    create_table_idempotent(
        op,
        sa.Table(
            "calendar_sync_state",
            metadata,
            sa.Column("source", sa.String(64), primary_key=True),
            sa.Column("last_ok_at", UTCDateTime(), nullable=True),
            sa.Column("last_error_at", UTCDateTime(), nullable=True),
            sa.Column("last_error", sa.String(256), nullable=True),
        ),
    )


def downgrade() -> None:
    drop_table_if_exists(op, "calendar_sync_state")
