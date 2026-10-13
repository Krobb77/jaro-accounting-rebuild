"""0010 — booking manage tokens + no-show timestamp.

Blast radius: two nullable columns on bookings (manage_token_hash,
no_show_at). No data touched; a nullable ADD COLUMN is instant on both
dialects.
Downgrade policy: FAIL-CLOSED (B1) — refuses when any booking row carries
a manage-token digest or a no-show timestamp, because dropping the
columns would silently destroy live manage links and the 12-month
no-show window used by the booking_hold rule. Otherwise drops the two
columns.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0010"
down_revision = "0009"

import sqlalchemy as sa
from alembic import op

from migration_helpers import table_names
from app.db import UTCDateTime


def upgrade() -> None:
    op.add_column("bookings", sa.Column("manage_token_hash", sa.String(64), nullable=True))
    op.add_column("bookings", sa.Column("no_show_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "bookings" in table_names(bind):
        live = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM bookings "
                "WHERE manage_token_hash IS NOT NULL OR no_show_at IS NOT NULL"
            )
        ).scalar()
        if live:
            raise RuntimeError(
                "B1 fail-closed downgrade refused: bookings has "
                f"{live} row(s) with manage tokens or no-show timestamps. "
                "Dropping these columns would silently destroy live manage "
                "links and the no-show hold window; this requires an "
                "explicit, audited owner decision first."
            )
    op.drop_column("bookings", "no_show_at")
    op.drop_column("bookings", "manage_token_hash")
