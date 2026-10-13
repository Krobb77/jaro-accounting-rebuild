"""0008 — inquiries.reference: short human-readable request id.

Phase 2 (contact lead pipeline): the submitter receipt and the owner
notice carry a short reference (``MSG-2026-XXXXXX``, EMAIL_TEMPLATES §4.2)
that guests quote in follow-ups, and the outbox dedup scopes are keyed on
it. The reference must therefore be a persisted, unique column — not
derived at render time.

Blast radius: adds one column + one unique index on ``inquiries``. No data
touched: any rows predating this column (none expected pre-launch) get a
derived backfill, after which NOT NULL is enforced.

Downgrade policy: B1 fail-closed — refuses when ``inquiries`` holds live
rows, because the reference is guest-facing (printed in receipts); dropping
the column would orphan support lookups. With an empty table the index and
column are dropped.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0008"
down_revision = "0007"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_index_idempotent,
    drop_index_if_exists,
    refuse_if_live_rows,
    table_names,
)

COLUMN = "reference"


def _has_column(bind, table: str, column: str) -> bool:
    if table not in table_names(bind):
        return False
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "inquiries", COLUMN):
        op.add_column("inquiries", sa.Column(COLUMN, sa.String(32), nullable=True))
    # Backfill rows that predate the column from the random tail of the
    # uuid7 id (62 random bits — unique for any realistic row count). The
    # app always writes a real reference for new rows, so after this no
    # NULLs remain and the constraint can be enforced.
    op.execute(
        sa.text(
            "UPDATE inquiries SET reference = 'MSG-BACKFILL-' || substr(id, 21, 16) "
            "WHERE reference IS NULL"
        )
    )
    # SQLite has no ALTER COLUMN ... SET NOT NULL; batch mode rebuilds the
    # table there and emits plain ALTER on Postgres.
    with op.batch_alter_table("inquiries") as batch_op:
        batch_op.alter_column(COLUMN, existing_type=sa.String(32), nullable=False)
    create_index_idempotent(
        op, "ix_inquiries_reference", "inquiries", [COLUMN], unique=True
    )


def downgrade() -> None:
    refuse_if_live_rows(op, "inquiries")
    drop_index_if_exists(op, "ix_inquiries_reference", "inquiries")
    bind = op.get_bind()
    if _has_column(bind, "inquiries", COLUMN):
        with op.batch_alter_table("inquiries") as batch_op:
            batch_op.drop_column(COLUMN)
