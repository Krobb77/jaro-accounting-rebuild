"""0001 — identity, invitations, sessions.

Blast radius: creates portal_users, capabilities, user_capabilities,
portal_invitations, invitation_events, sessions, mfa_challenges,
mfa_recovery_codes. No data touched (fresh tables).
Downgrade policy: FAIL-CLOSED (B1) — refuses when portal_users or
portal_invitations hold live rows; otherwise drops in reverse order.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0001"
down_revision = None

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
from app.models import CIText

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    portal_users = sa.Table(
        "portal_users",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("email", CIText, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("phone_e164", sa.String(32)),
        sa.Column("phone_verified_at", UTCDateTime()),
        sa.Column("totp_secret_enc", sa.LargeBinary()),
        sa.Column("totp_enrolled_at", UTCDateTime()),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_first_login"),
        sa.Column("terms_accepted_at", UTCDateTime()),
        sa.Column("terms_version_hash", sa.String(64)),
        sa.Column("failed_logins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", UTCDateTime()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'client')", name="ck_portal_users_role"),
        sa.CheckConstraint(
            "status IN ('pending_first_login', 'active', 'locked', 'disabled')",
            name="ck_portal_users_status",
        ),
    )
    capabilities = sa.Table(
        "capabilities",
        metadata,
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
    )
    user_capabilities = sa.Table(
        "user_capabilities",
        metadata,
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), primary_key=True),
        sa.Column("capability_code", sa.String(64), sa.ForeignKey("capabilities.code"), primary_key=True),
        sa.Column("granted_by", UUID, sa.ForeignKey("portal_users.id")),
        sa.Column("granted_at", UTCDateTime(), nullable=False),
    )
    portal_invitations = sa.Table(
        "portal_invitations",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("email", CIText, nullable=False),
        sa.Column("client_name", sa.Text, nullable=False),
        sa.Column("phone_e164", sa.String(32)),
        sa.Column("note", sa.Text),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("created_by", UUID, sa.ForeignKey("portal_users.id")),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("emailed_at", UTCDateTime()),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("used_at", UTCDateTime()),
        sa.Column("revoked_at", UTCDateTime()),
        sa.Column("revoke_reason", sa.Text),
        sa.Column("superseded_by", UUID, sa.ForeignKey("portal_invitations.id")),
        sa.Column("last_sent_at", UTCDateTime()),
        sa.Column("send_count", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('created', 'emailed', 'accepted', 'expired', 'revoked')",
            name="ck_portal_invitations_status",
        ),
        sa.CheckConstraint("(used_at IS NULL) OR (revoked_at IS NULL)", name="ck_portal_invitations_not_both"),
    )
    invitation_events = sa.Table(
        "invitation_events",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("invite_id", UUID, sa.ForeignKey("portal_invitations.id"), nullable=False, index=True),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.Column("ip_hash", sa.String(64)),
        sa.Column("note", sa.Text),
        sa.CheckConstraint(
            "event IN ('created', 'emailed', 'accepted', 'expired', 'revoked', 'resent')",
            name="ck_invitation_events_event",
        ),
        sa.CheckConstraint("actor IN ('owner', 'system', 'client')", name="ck_invitation_events_actor"),
    )
    sessions = sa.Table(
        "sessions",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("mfa_verified_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("absolute_expires_at", UTCDateTime(), nullable=False),
        sa.Column("revoked_at", UTCDateTime()),
        sa.Column("ip_hash", sa.String(64)),
        sa.Column("user_agent_hash", sa.String(64)),
    )
    mfa_challenges = sa.Table(
        "mfa_challenges",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("consumed_at", UTCDateTime()),
    )
    mfa_recovery_codes = sa.Table(
        "mfa_recovery_codes",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", UTCDateTime()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    return (
        portal_users, capabilities, user_capabilities, portal_invitations,
        invitation_events, sessions, mfa_challenges, mfa_recovery_codes,
    )


def upgrade() -> None:
    tables = _tables()
    for table in tables:
        create_table_idempotent(op, table)
    # Case-insensitive email uniqueness (CITEXT on Postgres, lower() index on SQLite).
    create_index_idempotent(op, "ix_portal_users_email_ci", "portal_users", [sa.text("lower(email)")], unique=True)
    create_index_idempotent(op, "ix_portal_invitations_email_ci", "portal_invitations", [sa.text("lower(email)")], unique=True)
    # One live invite per email (B2).
    where = sa.text("status IN ('created', 'emailed')")
    create_index_idempotent(
        op, "ix_portal_invitations_live_email", "portal_invitations", [sa.text("lower(email)")],
        unique=True, postgresql_where=where, sqlite_where=where,
    )
    create_index_idempotent(op, "ix_sessions_user", "sessions", ["user_id", "created_at"])


def downgrade() -> None:
    refuse_if_live_rows(op, "portal_users", "portal_invitations")
    drop_index_if_exists(op, "ix_sessions_user", "sessions")
    drop_index_if_exists(op, "ix_portal_invitations_live_email", "portal_invitations")
    drop_index_if_exists(op, "ix_portal_invitations_email_ci", "portal_invitations")
    drop_index_if_exists(op, "ix_portal_users_email_ci", "portal_users")
    for table in ("mfa_recovery_codes", "mfa_challenges", "sessions", "invitation_events",
                  "portal_invitations", "user_capabilities", "capabilities", "portal_users"):
        drop_table_if_exists(op, table)
