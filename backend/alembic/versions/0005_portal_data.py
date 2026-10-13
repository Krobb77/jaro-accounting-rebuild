"""0005 — portal v1 data: documents, filings, invoices.

Blast radius: creates document_types, portal_documents, filing_kinds,
filings, invoices, invoice_lines. No data touched (fresh tables).
Downgrade policy: plain drop in reverse dependency order.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0005"
down_revision = "0004"

import sqlalchemy as sa
from alembic import op

from migration_helpers import create_table_idempotent, drop_table_if_exists, ensure_stub
from app.db import UTCDateTime
from app.models import StrList

UUID = sa.String(36)


def _tables():
    metadata = sa.MetaData()
    ensure_stub(metadata, "portal_users")  # FK target from 0001
    document_types = sa.Table(
        "document_types",
        metadata,
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("allowed_mime", StrList, nullable=False),
        sa.Column("max_bytes", sa.Integer, nullable=False, server_default="26214400"),
        sa.Column("retention_note", sa.Text),
    )
    portal_documents = sa.Table(
        "portal_documents",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("type_code", sa.String(64), sa.ForeignKey("document_types.code"), nullable=False),
        sa.Column("original_filename", sa.Text, nullable=False),
        sa.Column("object_key", sa.Text, nullable=False, unique=True),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("uploaded_by", sa.String(16), nullable=False),
        sa.Column("label", sa.Text),
        sa.Column("scan_status", sa.String(16), nullable=False, server_default="unscanned"),
        sa.Column("deleted_at", UTCDateTime()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("uploaded_by IN ('client', 'owner')", name="ck_portal_documents_uploader"),
        sa.CheckConstraint(
            "scan_status IN ('unscanned', 'clean', 'quarantined')", name="ck_portal_documents_scan"
        ),
    )
    filing_kinds = sa.Table(
        "filing_kinds",
        metadata,
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("allowed_statuses", StrList, nullable=False),
    )
    filings = sa.Table(
        "filings",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("kind_code", sa.String(64), sa.ForeignKey("filing_kinds.code"), nullable=False),
        sa.Column("tax_year", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="collecting"),
        sa.Column("status_changed_at", UTCDateTime(), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("tax_year BETWEEN 2000 AND 2100", name="ck_filings_tax_year"),
        sa.CheckConstraint(
            "status IN ('collecting', 'awaiting_info', 'preparing', 'filed', 'confirmed')",
            name="ck_filings_status",
        ),
    )
    invoices = sa.Table(
        "invoices",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("portal_users.id"), nullable=False, index=True),
        sa.Column("invoice_number", sa.String(64), nullable=False, unique=True),
        sa.Column("issued_at", UTCDateTime(), nullable=False),
        sa.Column("due_at", UTCDateTime(), nullable=False),
        sa.Column("subtotal_cents", sa.Integer, nullable=False),
        sa.Column("tax_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CAD"),
        sa.Column("status", sa.String(16), nullable=False, server_default="issued"),
        sa.Column("issued_pdf_object_key", sa.Text),
        sa.Column("paid_at", UTCDateTime()),
        sa.Column("pay_method_note", sa.Text),
        sa.Column("void_reason", sa.Text),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("status IN ('issued', 'paid', 'void')", name="ck_invoices_status"),
        sa.CheckConstraint("total_cents = subtotal_cents + tax_cents", name="ck_invoices_totals"),
        sa.CheckConstraint("total_cents >= 0", name="ck_invoices_nonnegative"),
    )
    invoice_lines = sa.Table(
        "invoice_lines",
        metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("invoice_id", UUID, sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("unit_cents", sa.Integer, nullable=False),
        sa.Column("line_total_cents", sa.Integer, nullable=False),
    )
    return document_types, portal_documents, filing_kinds, filings, invoices, invoice_lines


def upgrade() -> None:
    for table in _tables():
        create_table_idempotent(op, table)


def downgrade() -> None:
    for table in ("invoice_lines", "invoices", "filings", "filing_kinds", "portal_documents", "document_types"):
        drop_table_if_exists(op, table)
