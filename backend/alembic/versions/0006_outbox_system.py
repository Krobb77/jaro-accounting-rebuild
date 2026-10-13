"""0006 — outbox, idempotency, audit, rate budgets.

Blast radius: creates notification_kinds, email_outbox, outbox_attempts,
idempotency_replays, audit_log, domain_events, rate_windows. No data
touched (fresh tables).
Downgrade policy: plain drop in reverse dependency order. Queued intents
are durable work — downgrading with non-terminal intents is refused via
refuse_if_live_rows as a conservative guard.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0006"
down_revision = "0005"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_index_idempotent,
    create_table_idempotent,
    drop_index_if_exists,
    drop_table_if_exists,
    refuse_if_live_rows,
)
from app.db import UTCDateTime
from app.models import JSONBCol

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    notification_kinds = sa.Table(
        "notification_kinds",
        metadata,
        sa.Column("kind", sa.String(64), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="email"),
        sa.Column("template_version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("channel IN ('email', 'sms', 'portal_inbox')", name="ck_notification_kinds_channel"),
    )
    email_outbox = sa.Table(
        "email_outbox",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(64), sa.ForeignKey("notification_kinds.kind"), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False, unique=True),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("frozen_snapshot", JSONBCol, nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("not_before", UTCDateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="8"),
        sa.Column("next_attempt_at", UTCDateTime()),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", UTCDateTime()),
        sa.Column("cancel_reason", sa.Text),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'submitted', 'captured', 'retry', 'dead', 'cancelled')",
            name="ck_email_outbox_status",
        ),
    )
    outbox_attempts = sa.Table(
        "outbox_attempts",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("outbox_id", UUID, sa.ForeignKey("email_outbox.id"), nullable=False, index=True),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime()),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("evidence_label", sa.String(128)),
        sa.UniqueConstraint("outbox_id", "attempt_no", name="uq_outbox_attempts_no"),
    )
    idempotency_replays = sa.Table(
        "idempotency_replays",
        metadata,
        sa.Column("scope_hash", sa.String(64), primary_key=True),
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("client_key", sa.Text, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("result", JSONBCol, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    audit_log = sa.Table(
        "audit_log",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False, index=True),
        sa.Column("entity_type", sa.String(64), index=True),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("detail", JSONBCol),
    )
    domain_events = sa.Table(
        "domain_events",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", UTCDateTime(), nullable=False),
        sa.Column("event", sa.String(128), nullable=False, index=True),
        sa.Column("entity_type", sa.String(64)),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("payload", JSONBCol),
    )
    rate_windows = sa.Table(
        "rate_windows",
        metadata,
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("window_start", UTCDateTime(), nullable=False),
        sa.Column("window_seconds", sa.Integer, nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
    )
    return (
        notification_kinds, email_outbox, outbox_attempts, idempotency_replays,
        audit_log, domain_events, rate_windows,
    )


def upgrade() -> None:
    for table in _tables():
        create_table_idempotent(op, table)
    # Worker claim path (E1): due intents first.
    where = sa.text("status IN ('queued', 'retry')")
    create_index_idempotent(
        op, "ix_email_outbox_claim", "email_outbox",
        ["status", "not_before", "next_attempt_at"],
        postgresql_where=where, sqlite_where=where,
    )
    create_index_idempotent(op, "ix_audit_log_entity", "audit_log",
                            ["entity_type", "entity_id", "occurred_at"])


def downgrade() -> None:
    refuse_if_live_rows(op, "email_outbox")
    drop_index_if_exists(op, "ix_audit_log_entity", "audit_log")
    drop_index_if_exists(op, "ix_email_outbox_claim", "email_outbox")
    for table in ("rate_windows", "domain_events", "audit_log", "idempotency_replays",
                  "outbox_attempts", "email_outbox", "notification_kinds"):
        drop_table_if_exists(op, table)
