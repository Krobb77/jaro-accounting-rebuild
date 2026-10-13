"""0009 — audit_log/domain_events id: BIGINT -> INTEGER.

Phase-0 latent defect, found by Phase 2 (the inquiry route is the first
writer of audit_log): BigInteger autoincrement PKs are NOT auto-generated
by SQLAlchemy on SQLite (lastrowid retrieval only applies to Integer PKs),
so the first ORM insert into audit_log or domain_events raised
``IntegrityError: NOT NULL constraint failed``. Integer PKs autoincrement
on both dialects (SQLite rowid alias, Postgres SERIAL).

Blast radius: column type change on two append-only log tables. Pre-launch
there are no rows, so the conversion is a formality (the USING clause is
still supplied for Postgres).

Downgrade policy: reverse the type change; every row is preserved (the
tables are append-only, B1 n/a).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0009"
down_revision = "0008"

import sqlalchemy as sa
from alembic import op

_TABLES = ("audit_log", "domain_events")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "id",
                existing_type=sa.BigInteger(),
                type_=sa.Integer(),
                existing_nullable=False,
                postgresql_using="id::integer",
            )


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "id",
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=False,
                postgresql_using="id::bigint",
            )
