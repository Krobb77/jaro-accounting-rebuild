"""0003 — booking domain.

Blast radius: creates sms_codes, event_types, availability_rules,
calendar_blocks, bookings. No data touched (fresh tables).
Downgrade policy: FAIL-CLOSED (B1) — refuses when bookings holds live
rows; otherwise drops in reverse dependency order.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0003"
down_revision = "0002"

import sqlalchemy as sa
from alembic import op

from migration_helpers import (
    create_index_idempotent,
    create_table_idempotent,
    drop_index_if_exists,
    drop_table_if_exists,
    ensure_stub,
    refuse_if_live_rows,
)
from app.db import UTCDateTime

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    ensure_stub(metadata, "portal_users")  # FK target from 0001
    sms_codes = sa.Table(
        "sms_codes",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("booking_id", UUID, sa.ForeignKey("bookings.id"), index=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), index=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("consumed_at", UTCDateTime()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("scope IN ('booking_verify', 'mfa_sms', 'password_reset')", name="ck_sms_codes_scope"),
        sa.CheckConstraint(
            "(scope = 'booking_verify' AND booking_id IS NOT NULL AND user_id IS NULL) OR "
            "(scope IN ('mfa_sms', 'password_reset') AND user_id IS NOT NULL AND booking_id IS NULL)",
            name="ck_sms_codes_scope_shape",
        ),
    )
    event_types = sa.Table(
        "event_types",
        metadata,
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("duration_min", sa.Integer, nullable=False),
        sa.Column("buffer_before_min", sa.Integer, nullable=False, server_default="0"),
        sa.Column("buffer_after_min", sa.Integer, nullable=False, server_default="0"),
        sa.Column("requires_mailbox_code", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    availability_rules = sa.Table(
        "availability_rules",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("weekday", sa.Integer, nullable=False),
        sa.Column("start_local", sa.Time(), nullable=False),
        sa.Column("end_local", sa.Time(), nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_until", sa.Date()),
        sa.Column("event_code", sa.String(64), sa.ForeignKey("event_types.code"), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_availability_rules_weekday"),
    )
    calendar_blocks = sa.Table(
        "calendar_blocks",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source", sa.String(64), nullable=False, server_default="jared_calendar"),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("starts_at", UTCDateTime(), nullable=False),
        sa.Column("ends_at", UTCDateTime(), nullable=False),
        sa.Column("tentative", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("synced_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="ck_calendar_blocks_order"),
    )
    bookings = sa.Table(
        "bookings",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("reference_number", sa.String(32), nullable=False, unique=True),
        sa.Column("event_code", sa.String(64), sa.ForeignKey("event_types.code"), nullable=False),
        sa.Column("slot_start", UTCDateTime(), nullable=False),
        sa.Column("slot_end", UTCDateTime(), nullable=False),
        sa.Column("guest_timezone", sa.String(64), nullable=False),
        sa.Column("guest_name", sa.Text, nullable=False),
        sa.Column("guest_email", sa.Text, nullable=False),
        sa.Column("guest_phone_e164", sa.String(32)),
        sa.Column("guest_relationship", sa.String(32), nullable=False, server_default="prospective_client"),
        sa.Column("discuss_notes", sa.Text),
        sa.Column("staff_id", sa.String(64), nullable=False, server_default="jared"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_verification"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("code_verified_at", UTCDateTime()),
        sa.Column("cancelled_at", UTCDateTime()),
        sa.Column("cancel_reason", sa.Text),
        sa.Column("supersedes", UUID, sa.ForeignKey("bookings.id")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'confirmed', 'cancelled', 'completed', 'no_show', 'rescheduled')",
            name="ck_bookings_status",
        ),
        sa.CheckConstraint(
            "guest_relationship IN ('prospective_client', 'current_client', 'referral_professional', 'other')",
            name="ck_bookings_relationship",
        ),
        sa.CheckConstraint("slot_end > slot_start", name="ck_bookings_slot_order"),
        sa.CheckConstraint(
            "(status <> 'confirmed') OR (code_verified_at IS NOT NULL)",
            name="ck_bookings_confirmed_verified",
        ),
        sa.CheckConstraint(
            "(status <> 'cancelled') OR (cancelled_at IS NOT NULL)",
            name="ck_bookings_cancelled_at",
        ),
    )
    # Creation order: event_types/bookings before their dependents.
    return event_types, bookings, availability_rules, calendar_blocks, sms_codes


def upgrade() -> None:
    for table in _tables():
        create_table_idempotent(op, table)
    # The unraceable slot backstop (F6/B2).
    where = sa.text("status IN ('pending_verification', 'confirmed')")
    create_index_idempotent(
        op, "ix_bookings_slot_active", "bookings", ["slot_start"],
        unique=True, postgresql_where=where, sqlite_where=where,
    )
    create_index_idempotent(op, "ix_calendar_blocks_source", "calendar_blocks",
                            ["source", "external_id"], unique=True)


def downgrade() -> None:
    refuse_if_live_rows(op, "bookings")
    drop_index_if_exists(op, "ix_calendar_blocks_source", "calendar_blocks")
    drop_index_if_exists(op, "ix_bookings_slot_active", "bookings")
    for table in ("bookings", "calendar_blocks", "availability_rules", "event_types", "sms_codes"):
        drop_table_if_exists(op, table)
