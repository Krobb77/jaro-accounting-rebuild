"""0015 — client temporary-password flag: ``portal_users.must_change_password``.

2026-10-15 auth refactor: Jared provisions each client with a temporary
password (communicated by Jared himself, never by the portal); the client
must replace it on first login before any portal use. The mandatory
authenticator-app (TOTP) requirement was removed in the same refactor.

Blast radius: one new NOT NULL boolean column, default FALSE — existing
rows (including any already-provisioned clients) are unaffected.

Downgrade: drops the column.
"""

from __future__ import annotations

revision = "0015"
down_revision = "0014"

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column(
        "portal_users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("portal_users", "must_change_password")
