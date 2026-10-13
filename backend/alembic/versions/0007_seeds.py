"""0007 — registry seeds (data migration).

Blast radius: INSERTs only, into event_types, availability_rules,
document_types, filing_kinds, capabilities, notification_kinds,
terms_versions. Idempotent: rows are inserted only when their PK is
absent, so re-running is safe.
Downgrade policy: deletes exactly the seeded rows by PK (reverse
dependency order). Refuses when non-seed rows reference the registries
would be affected — in practice seeds are inert config, so the delete is
safe; dependent seeded rows (availability_rules) are removed first.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Alembic loads version modules without their directory on sys.path (e.g.
# `alembic heads` never executes env.py); bootstrap it here so the shared
# migration helpers resolve for every command.
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

revision = "0007"
down_revision = "0006"

import hashlib
from datetime import time as dtime

import sqlalchemy as sa
from alembic import op

TERMS_V1_BODY = """<h1>Client Engagement Terms (draft)</h1>
<p>This is the Phase-0 placeholder for the engagement-terms copy. The final
client-facing terms are authored with Jared before launch (Phase 5); the
registry, hashing, and acceptance machinery ship now.</p>"""


def _terms_v1_hash() -> str:
    return hashlib.sha256(TERMS_V1_BODY.encode("utf-8")).hexdigest()


_reflected: dict[str, sa.Table] = {}


def _table(bind, name: str) -> sa.Table:
    if name not in _reflected:
        _reflected[name] = sa.Table(name, sa.MetaData(), autoload_with=bind)
    return _reflected[name]


def _seed(bind, table: str, pk: str, rows: list[dict]) -> None:
    tbl = _table(bind, table)
    for row in rows:
        exists = bind.execute(
            sa.select(sa.literal(1)).where(tbl.c[pk] == row[pk])
        ).first()
        if not exists:
            bind.execute(tbl.insert().values(**row))


def upgrade() -> None:
    bind = op.get_bind()

    _seed(bind, "event_types", "code", [
        {
            "code": "free-30-min-consultation",
            "label": "Free 30-Min Consultation — Phone Call",
            "duration_min": 30,
            "buffer_before_min": 0,
            "buffer_after_min": 0,
            "requires_mailbox_code": True,
            "active": True,
        },
    ])
    # Firm zone America/Vancouver; wall-clock local times (GROUNDED spec §5).
    rules = []
    for weekday in range(0, 5):
        rules.append({
            "id": f"seed-availability-{weekday}", "weekday": weekday,
            "start_local": dtime(9, 0), "end_local": dtime(17, 0),
            "valid_from": None, "valid_until": None,
            "event_code": "free-30-min-consultation",
        })
    rules.append({
        "id": "seed-availability-5", "weekday": 5,
        "start_local": dtime(10, 0), "end_local": dtime(15, 0),
        "valid_from": None, "valid_until": None,
        "event_code": "free-30-min-consultation",
    })
    _seed(bind, "availability_rules", "id", rules)

    _seed(bind, "document_types", "code", [
        {"code": "tax_slips", "label": "Tax slips",
         "allowed_mime": ["application/pdf", "image/jpeg", "image/png"], "max_bytes": 26214400,
         "retention_note": "Retain per CRA record-keeping requirements."},
        {"code": "receipts", "label": "Receipts",
         "allowed_mime": ["application/pdf", "image/jpeg", "image/png"], "max_bytes": 26214400,
         "retention_note": "Retain per CRA record-keeping requirements."},
        {"code": "bank_statements", "label": "Bank statements",
         "allowed_mime": ["application/pdf"], "max_bytes": 26214400,
         "retention_note": "Retain per CRA record-keeping requirements."},
        {"code": "payroll_records", "label": "Payroll records",
         "allowed_mime": ["application/pdf", "text/csv"], "max_bytes": 26214400,
         "retention_note": "Retain per CRA record-keeping requirements."},
        {"code": "correspondence", "label": "Correspondence",
         "allowed_mime": ["application/pdf", "image/jpeg", "image/png"], "max_bytes": 26214400,
         "retention_note": None},
        {"code": "deliverables", "label": "Deliverables",
         "allowed_mime": ["application/pdf", "application/zip"], "max_bytes": 26214400,
         "retention_note": None},
        {"code": "other", "label": "Other",
         "allowed_mime": ["application/pdf", "image/jpeg", "image/png", "text/csv"], "max_bytes": 26214400,
         "retention_note": None},
    ])
    filing_statuses = ["collecting", "awaiting_info", "preparing", "filed", "confirmed"]
    _seed(bind, "filing_kinds", "code", [
        {"code": "personal_t1", "label": "Personal T1 return", "allowed_statuses": filing_statuses},
        {"code": "corporate_t2", "label": "Corporate T2 return", "allowed_statuses": filing_statuses},
        {"code": "nonprofit", "label": "Non-profit return", "allowed_statuses": filing_statuses},
        {"code": "nil_return", "label": "Nil return", "allowed_statuses": filing_statuses},
        {"code": "gst_hst", "label": "GST/HST filing", "allowed_statuses": filing_statuses},
    ])
    _seed(bind, "capabilities", "code", [
        {"code": "documents.read", "description": "Read own documents in the vault."},
        {"code": "documents.write", "description": "Upload documents to the vault."},
        {"code": "filings.read", "description": "View own filings and their status."},
        {"code": "invoices.read", "description": "View own invoices and download PDFs."},
        {"code": "booking.manage_self", "description": "Manage own bookings via manage links."},
    ])
    kinds = [
        ("invite", "email"), ("password_reset", "email"), ("booking_code", "email"),
        ("booking_confirmation", "email"), ("booking_owner_notification", "email"),
        ("booking_reminder", "email"), ("booking_cancellation_notice", "email"),
        ("booking_reschedule_notice", "email"), ("inquiry_owner_notice", "email"),
        ("inquiry_receipt", "email"), ("document_received", "email"),
        ("document_shared", "email"), ("filing_status_changed", "email"),
        ("invoice_issued", "email"), ("mfa_sms_fallback", "sms"),
    ]
    _seed(bind, "notification_kinds", "kind", [
        {"kind": kind, "channel": channel, "template_version": "v1", "enabled": True}
        for kind, channel in kinds
    ])
    _seed(bind, "terms_versions", "version", [
        {"version": "v1", "version_hash": _terms_v1_hash(),
         "title": "Client Engagement Terms",
         "body_html": TERMS_V1_BODY, "published_at": sa.func.now()},
    ])


_SEED_PKS = {
    "availability_rules": [f"seed-availability-{i}" for i in range(6)],
    "event_types": ["free-30-min-consultation"],
    "document_types": ["tax_slips", "receipts", "bank_statements", "payroll_records", "correspondence", "deliverables", "other"],
    "filing_kinds": ["personal_t1", "corporate_t2", "nonprofit", "nil_return", "gst_hst"],
    "capabilities": ["documents.read", "documents.write", "filings.read", "invoices.read", "booking.manage_self"],
    "notification_kinds": ["invite", "password_reset", "booking_code", "booking_confirmation",
                            "booking_owner_notification", "booking_reminder", "booking_cancellation_notice",
                            "booking_reschedule_notice", "inquiry_owner_notice", "inquiry_receipt",
                            "document_received", "document_shared", "filing_status_changed",
                            "invoice_issued", "mfa_sms_fallback"],
    "terms_versions": ["v1"],
}
_SEED_PK_COL = {
    "availability_rules": "id", "event_types": "code", "document_types": "code",
    "filing_kinds": "code", "capabilities": "code", "notification_kinds": "kind",
    "terms_versions": "version",
}


def downgrade() -> None:
    bind = op.get_bind()
    # Reverse dependency order: availability_rules -> event_types last among seeds.
    for table in ("availability_rules", "terms_versions", "notification_kinds",
                  "capabilities", "filing_kinds", "document_types", "event_types"):
        pk = _SEED_PK_COL[table]
        for value in _SEED_PKS[table]:
            bind.execute(sa.text(f"DELETE FROM {table} WHERE {pk} = :v"), {"v": value})
