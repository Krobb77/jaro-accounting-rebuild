"""0013 — drop the full unique index on portal_invitations.email.

The full unique index ix_portal_invitations_email_ci blocked resend (which
mints a new invitation row for the same address after revoking the old).
The correct invariant is "one LIVE invite per email", enforced by the
partial unique index ix_portal_invitations_live_email (B2). The old row is
retained for audit; only the live-email uniqueness matters.
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0013"
down_revision = "0012"

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.drop_index("ix_portal_invitations_email_ci", table_name="portal_invitations")


def downgrade() -> None:
    # Re-create the full unique index (fail-closed: only if no duplicate
    # emails exist, otherwise the index creation itself fails).
    op.create_index(
        "ix_portal_invitations_email_ci",
        "portal_invitations",
        [sa.func.lower(sa.column("email"))],
        unique=True,
    )
