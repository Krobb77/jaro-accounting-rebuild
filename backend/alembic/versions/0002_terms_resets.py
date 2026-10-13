"""0002 — terms registry + password resets (AUTH_AND_INVITATIONS §9).

Blast radius: creates terms_versions, terms_acceptances, password_resets.
No data touched (fresh tables).
Downgrade policy: plain drop in reverse order (no B1-protected tables here;
terms acceptances are append-only audit but carry no PII beyond user ids —
a downgrade with rows still refuses via refuse_if_live_rows on
terms_acceptances as a conservative guard).
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0002"
down_revision = "0001"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_table_idempotent,
    drop_table_if_exists,
    ensure_stub,
    refuse_if_live_rows,
)
from app.db import UTCDateTime

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    ensure_stub(metadata, "portal_users")  # FK target from 0001
    terms_versions = sa.Table(
        "terms_versions",
        metadata,
        sa.Column("version", sa.String(16), primary_key=True),
        sa.Column("version_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body_html", sa.Text, nullable=False),
        sa.Column("published_at", UTCDateTime(), nullable=False),
    )
    terms_acceptances = sa.Table(
        "terms_acceptances",
        metadata,
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), primary_key=True),
        sa.Column("version", sa.String(16), sa.ForeignKey("terms_versions.version"), primary_key=True),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", UTCDateTime(), nullable=False),
    )
    password_resets = sa.Table(
        "password_resets",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="live"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("used_at", UTCDateTime()),
        sa.CheckConstraint("status IN ('live', 'used', 'expired', 'revoked')", name="ck_password_resets_status"),
    )
    return terms_versions, terms_acceptances, password_resets


def upgrade() -> None:
    for table in _tables():
        create_table_idempotent(op, table)


def downgrade() -> None:
    refuse_if_live_rows(op, "terms_acceptances", "password_resets")
    for table in ("password_resets", "terms_acceptances", "terms_versions"):
        drop_table_if_exists(op, table)
