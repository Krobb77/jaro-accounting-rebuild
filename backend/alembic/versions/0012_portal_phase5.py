"""0012 — Phase-5 portal columns, invoice series, one-shot markers.

Blast radius: adds nullable columns to portal_users (last_portal_visit_at,
internal_note, last_login_at), mfa_challenges (attempts), invoices (memo), invoice_lines
(tax_rate_bps, tax_cents); creates invoice_series (+ seeds the default
series) and one_shot_markers. No existing rows are modified.
Downgrade policy: FAIL-CLOSED (B1) — refuses when any portal table holds
live rows; otherwise drops the new tables and columns.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0012"
down_revision = "0011"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_table_idempotent,
    drop_table_if_exists,
    refuse_if_live_rows,
    table_names,
)
from app.db import UTCDateTime

UUID = sa.String(36)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add_column_idempotent(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    if table in table_names(bind) and not _has_column(bind, table, column.name):
        op.add_column(table, column)


def _drop_column_if_exists(table: str, column: str) -> None:
    bind = op.get_bind()
    if table in table_names(bind) and _has_column(bind, table, column):
        op.drop_column(table, column)


def _tables():
    metadata = sa.MetaData()
    invoice_series = sa.Table(
        "invoice_series",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("prefix", sa.String(32), nullable=False),
        sa.Column("next_number", sa.Integer, nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    one_shot_markers = sa.Table(
        "one_shot_markers",
        metadata,
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("consumed_at", UTCDateTime()),
    )
    return invoice_series, one_shot_markers


def upgrade() -> None:
    # Nullable portal columns — existing rows untouched.
    _add_column_idempotent("portal_users", sa.Column("last_portal_visit_at", UTCDateTime()))
    _add_column_idempotent("portal_users", sa.Column("internal_note", sa.Text()))
    _add_column_idempotent("portal_users", sa.Column("last_login_at", UTCDateTime()))
    _add_column_idempotent("sessions", sa.Column("user_agent_summary", sa.String(80)))
    _add_column_idempotent(
        "mfa_challenges", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    _add_column_idempotent("invoices", sa.Column("memo", sa.Text()))
    _add_column_idempotent(
        "invoice_lines",
        sa.Column("tax_rate_bps", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_idempotent(
        "invoice_lines", sa.Column("tax_cents", sa.Integer(), nullable=False, server_default="0")
    )

    invoice_series, one_shot_markers = _tables()
    create_table_idempotent(op, invoice_series)
    create_table_idempotent(op, one_shot_markers)

    # Seed the default invoice series (idempotent — one row, fixed PK).
    bind = op.get_bind()
    exists = bind.execute(
        sa.select(sa.literal(1)).where(invoice_series.c.id == "default")
    ).first()
    if not exists:
        bind.execute(
            invoice_series.insert().values(
                id="default",
                prefix="JARO-INV-",
                next_number=1,
                updated_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    # B1: never silently destroy portal data. The seeded default series row
    # (id='default') is inert config, not live data: it is deleted first, but
    # only after the data tables already passed the check above — any issued
    # invoice would have refused the downgrade via `invoices` first.
    refuse_if_live_rows(
        op,
        "portal_users",
        "portal_invitations",
        "portal_documents",
        "filings",
        "invoices",
    )
    op.execute(sa.text("DELETE FROM invoice_series WHERE id = 'default'"))
    refuse_if_live_rows(op, "invoice_series", "one_shot_markers")
    drop_table_if_exists(op, "one_shot_markers")
    drop_table_if_exists(op, "invoice_series")
    for table, column in (
        ("invoice_lines", "tax_cents"),
        ("invoice_lines", "tax_rate_bps"),
        ("invoices", "memo"),
        ("mfa_challenges", "attempts"),
        ("portal_users", "internal_note"),
        ("portal_users", "last_portal_visit_at"),
        ("portal_users", "last_login_at"),
        ("sessions", "user_agent_summary"),
    ):
        _drop_column_if_exists(table, column)
