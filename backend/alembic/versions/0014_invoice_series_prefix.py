"""0014 — invoice series default prefix: 'JARO-INV-' -> 'JARO'.

Owner-question decision D-27 (IMPLEMENTATION_PLAN §3.5 #5): invoice numbers
follow the scheme ``{prefix}-{YYYY}-{NNNN}`` (e.g. ``JARO-2026-0001``), with
the default prefix ``JARO`` (portal.INVOICE_NUMBER_PREFIX). The previous
seed ``JARO-INV-`` predates the scheme decision.

Blast radius: one UPDATE on the single ``invoice_series`` default row, and
only when it still carries the old seeded value — a Jared-customized prefix
is never touched. Already-issued invoice numbers are immutable history and
are not rewritten (voided numbers are never recycled, B2).

Downgrade: restores ``JARO-INV-`` under the mirror condition (only when the
row still carries ``JARO``); a customized prefix is likewise never touched.
"""

from __future__ import annotations

revision = "0014"
down_revision = "0013"

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE invoice_series SET prefix = 'JARO' "
            "WHERE id = 'default' AND prefix = 'JARO-INV-'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE invoice_series SET prefix = 'JARO-INV-' "
            "WHERE id = 'default' AND prefix = 'JARO'"
        )
    )
