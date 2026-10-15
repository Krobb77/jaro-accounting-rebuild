"""Canonical data model — 30 tables.

Reconciliation applied (IMPLEMENTATION_PLAN §3.1):
- ARCHITECTURE §2.8's 26 tables as baseline
- + invitation_events, password_resets, terms_versions, terms_acceptances
  (AUTH_AND_INVITATIONS §9)
- login_events folded into audit_log (action vocabulary ``login.*``)
- clients -> portal_users (roles owner/client), login_challenges -> mfa_challenges,
  booking_intents/booking_codes -> bookings(status='pending_verification') +
  sms_codes(scope='booking_verify'), document_categories -> document_types,
  filing_types -> filing_kinds

Note on the count: the plan says "31 tables" but 26 + 4 = 30 and
login_events folds into audit_log rather than adding a table. The honest
count is 30; see backend/DECISIONS-backend.md.

Portability: migrations target Postgres DDL but must stay SQLite-runnable
for local verification. Postgres-only constructs are abstracted:
- citext  -> String with CITEXT variant on postgres; case-insensitive
  uniqueness via a functional lower() unique index on both dialects.
- text[]  -> JSON with JSONB variant on postgres; validated as string
  lists in application code.
- partial unique indexes -> postgresql_where + sqlite_where.
- advisory locks -> never in DDL; app-level abstraction (app/advisory.py).
- UUIDv7 PKs generated in Python (app/db.py new_uuid7).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, new_uuid7, utcnow

# Portable column types -----------------------------------------------------
# Case-insensitive text: CITEXT on Postgres, plain String on SQLite.
# Case-insensitive uniqueness is enforced by functional lower() indexes
# defined after each mapped class (works on both dialects).
CIText = String(320).with_variant(postgresql.CITEXT(), "postgresql")

# String-list registries (document_types.allowed_mime,
# filing_kinds.allowed_statuses): JSONB on Postgres, JSON on SQLite.
StrList = JSON().with_variant(postgresql.JSONB(), "postgresql")
JSONBCol = JSON().with_variant(postgresql.JSONB(), "postgresql")

# Partial-index WHERE clauses shared by models and migrations.
INVITE_LIVE_WHERE = text("status IN ('created', 'emailed')")
BOOKING_ACTIVE_WHERE = text("status IN ('pending_verification', 'confirmed')")


def _uuid_pk():
    # UUIDv7 as a canonical 8-4-4-4-12 string: time-ordered and portable
    # (SQLAlchemy's Uuid type demands uuid.UUID objects; String(36) keeps the
    # same values without ORM type friction on any dialect.)
    return mapped_column(String(36), primary_key=True, default=new_uuid7)


def _created():
    return mapped_column(UTCDateTime(), nullable=False, default=utcnow)


# 2.1 Identity, invitations, sessions ---------------------------------------

USER_ROLES = ("owner", "client")
USER_STATUSES = ("pending_first_login", "active", "locked", "disabled")
INVITE_STATUSES = ("created", "emailed", "accepted", "expired", "revoked")
INVITE_EVENTS = ("created", "emailed", "accepted", "expired", "revoked", "resent")
INVITE_ACTORS = ("owner", "system", "client")


class PortalUser(Base):
    __tablename__ = "portal_users"

    id: Mapped[str] = _uuid_pk()
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    email: Mapped[str] = mapped_column(CIText, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(32))
    phone_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enrolled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_first_login")
    terms_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    terms_version_hash: Mapped[str | None] = mapped_column(String(64))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # 2026-10-15: Jared provisions clients with a temporary password he
    # communicates himself; the client must replace it on first login before
    # any portal use (the authenticator-app requirement was removed).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Phase 5: "new documents since last visit" marker (F1 needs-attention /
    # "N new" snapshot). Advanced when the client views the dashboard —
    # PORTAL_V1 §3 clears the marker on view.
    last_portal_visit_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Phase 5: owner-only dossier note (D-60) — plain text, never shown to
    # the client.
    internal_note: Mapped[str | None] = mapped_column(Text)
    # Phase 5: owner-visible client list shows last login (AUTH §2.6).
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime | None] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'client')", name="ck_portal_users_role"),
        CheckConstraint("status IN ('pending_first_login', 'active', 'locked', 'disabled')", name="ck_portal_users_status"),
    )


class Capability(Base):
    __tablename__ = "capabilities"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class UserCapability(Base):
    __tablename__ = "user_capabilities"

    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), primary_key=True)
    capability_code: Mapped[str] = mapped_column(ForeignKey("capabilities.code"), primary_key=True)
    granted_by: Mapped[str | None] = mapped_column(ForeignKey("portal_users.id"))
    granted_at: Mapped[datetime | None] = _created()


class PortalInvitation(Base):
    __tablename__ = "portal_invitations"

    id: Mapped[str] = _uuid_pk()
    email: Mapped[str] = mapped_column(CIText, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("portal_users.id"))
    created_at: Mapped[datetime | None] = _created()
    emailed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(ForeignKey("portal_invitations.id"))
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("status IN ('created', 'emailed', 'accepted', 'expired', 'revoked')", name="ck_portal_invitations_status"),
        CheckConstraint("(used_at IS NULL) OR (revoked_at IS NULL)", name="ck_portal_invitations_not_both"),
    )


class InvitationEvent(Base):
    __tablename__ = "invitation_events"

    id: Mapped[str] = _uuid_pk()
    invite_id: Mapped[str] = mapped_column(ForeignKey("portal_invitations.id"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[datetime | None] = _created()
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("event IN ('created', 'emailed', 'accepted', 'expired', 'revoked', 'resent')", name="ck_invitation_events_event"),
        CheckConstraint("actor IN ('owner', 'system', 'client')", name="ck_invitation_events_actor"),
    )


class Session(Base):
    """Server-side session. The cookie holds the raw token; ``id`` is its SHA-256."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    mfa_verified_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime | None] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    absolute_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    # Phase 5: short device fingerprint for the client's own session
    # inventory (AUTH §7 #33) — never the full UA string (A6).
    user_agent_summary: Mapped[str | None] = mapped_column(String(80))


class MfaChallenge(Base):
    __tablename__ = "mfa_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    created_at: Mapped[datetime | None] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Phase 5: 5 wrong TOTP guesses kill the challenge (AUTH §4.3).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MfaRecoveryCode(Base):
    __tablename__ = "mfa_recovery_codes"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime | None] = _created()


# AUTH §9 extra tables --------------------------------------------------------

class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    created_at: Mapped[datetime | None] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint("status IN ('live', 'used', 'expired', 'revoked')", name="ck_password_resets_status"),
    )


class TermsVersion(Base):
    __tablename__ = "terms_versions"

    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = _created()


class TermsAcceptance(Base):
    __tablename__ = "terms_acceptances"

    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), primary_key=True)
    version: Mapped[str] = mapped_column(ForeignKey("terms_versions.version"), primary_key=True)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime | None] = _created()


# 2.2 Verification codes -------------------------------------------------------

SMS_SCOPES = ("booking_verify", "mfa_sms", "password_reset")


class SmsCode(Base):
    __tablename__ = "sms_codes"

    id: Mapped[str] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_id: Mapped[str | None] = mapped_column(ForeignKey("bookings.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("portal_users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime | None] = _created()

    __table_args__ = (
        CheckConstraint("scope IN ('booking_verify', 'mfa_sms', 'password_reset')", name="ck_sms_codes_scope"),
        CheckConstraint(
            "(scope = 'booking_verify' AND booking_id IS NOT NULL AND user_id IS NULL) OR "
            "(scope IN ('mfa_sms', 'password_reset') AND user_id IS NOT NULL AND booking_id IS NULL)",
            name="ck_sms_codes_scope_shape",
        ),
    )


# 2.3 Bookings ------------------------------------------------------------------

class EventType(Base):
    __tablename__ = "event_types"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_after_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_mailbox_code: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AvailabilityRule(Base):
    __tablename__ = "availability_rules"

    id: Mapped[str] = _uuid_pk()
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_local: Mapped[datetime] = mapped_column(Time(), nullable=False)
    end_local: Mapped[datetime] = mapped_column(Time(), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(Date())
    valid_until: Mapped[datetime | None] = mapped_column(Date())
    event_code: Mapped[str] = mapped_column(ForeignKey("event_types.code"), nullable=False)

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_availability_rules_weekday"),
    )


class CalendarBlock(Base):
    __tablename__ = "calendar_blocks"

    id: Mapped[str] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="jared_calendar")
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    tentative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synced_at: Mapped[datetime | None] = _created()

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_calendar_blocks_order"),
    )


class CalendarSyncState(Base):
    """Worker-observed calendar health (D-15/D-31).

    ``last_ok_at`` is the freshness signal for the availability
    fail-closed gate — it advances on every successful poll even when the
    calendar is empty (an empty-but-fresh sync must not read as
    "never synced", which would 503 availability forever on a genuinely
    empty calendar). ``last_error*`` is observability for the read-error
    path; the gate itself stays staleness-based.
    """

    __tablename__ = "calendar_sync_state"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(256))


BOOKING_STATUSES = (
    "pending_verification",
    "confirmed",
    "cancelled",
    "completed",
    "no_show",
    "rescheduled",
)
GUEST_RELATIONSHIPS = (
    "prospective_client",
    "current_client",
    "referral_professional",
    "other",
)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = _uuid_pk()
    reference_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    event_code: Mapped[str] = mapped_column(ForeignKey("event_types.code"), nullable=False)
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    slot_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    guest_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    guest_name: Mapped[str] = mapped_column(Text, nullable=False)
    guest_email: Mapped[str] = mapped_column(Text, nullable=False)
    guest_phone_e164: Mapped[str | None] = mapped_column(String(32))
    guest_relationship: Mapped[str] = mapped_column(String(32), nullable=False, default="prospective_client")
    discuss_notes: Mapped[str | None] = mapped_column(Text)
    staff_id: Mapped[str] = mapped_column(String(64), nullable=False, default="jared")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_verification")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # Manage-link auth: SHA-256 digest of the 32-byte CSPRNG token (B6 —
    # the raw token is shown once at confirm and in the confirmation
    # email; the DB keeps only the digest). Revoked (NULL) on cancel and
    # on reschedule (the new booking row gets its own token).
    manage_token_hash: Mapped[str | None] = mapped_column(String(64))
    # No-show marker set by the owner lever; the 12-month hold window for
    # the booking_hold rule (D-22) is computed from this timestamp.
    no_show_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    supersedes: Mapped[str | None] = mapped_column(ForeignKey("bookings.id"))
    created_at: Mapped[datetime | None] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_verification', 'confirmed', 'cancelled', 'completed', 'no_show', 'rescheduled')",
            name="ck_bookings_status",
        ),
        CheckConstraint(
            "guest_relationship IN ('prospective_client', 'current_client', 'referral_professional', 'other')",
            name="ck_bookings_relationship",
        ),
        CheckConstraint("slot_end > slot_start", name="ck_bookings_slot_order"),
        CheckConstraint(
            "(status <> 'confirmed') OR (code_verified_at IS NOT NULL)",
            name="ck_bookings_confirmed_verified",
        ),
        CheckConstraint(
            "(status <> 'cancelled') OR (cancelled_at IS NOT NULL)",
            name="ck_bookings_cancelled_at",
        ),
    )


# 2.4 Contact inquiries ----------------------------------------------------------

class Inquiry(Base):
    __tablename__ = "inquiries"

    id: Mapped[str] = _uuid_pk()
    reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime | None] = _created()

    __table_args__ = (
        CheckConstraint("status IN ('new', 'owner_notified', 'receipt_sent', 'archived')", name="ck_inquiries_status"),
    )


# 2.5 Portal v1: documents, filings, invoices -------------------------------------

DOCUMENT_UPLOADERS = ("client", "owner")
SCAN_STATUSES = ("unscanned", "clean", "quarantined")
FILING_STATUSES = ("collecting", "awaiting_info", "preparing", "filed", "confirmed")
INVOICE_STATUSES = ("issued", "paid", "void")


class DocumentType(Base):
    __tablename__ = "document_types"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_mime: Mapped[datetime] = mapped_column(StrList, nullable=False, default=list)
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=26214400)
    retention_note: Mapped[str | None] = mapped_column(Text)


class PortalDocument(Base):
    __tablename__ = "portal_documents"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    type_code: Mapped[str] = mapped_column(ForeignKey("document_types.code"), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    scan_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unscanned")
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime | None] = _created()

    __table_args__ = (
        CheckConstraint("uploaded_by IN ('client', 'owner')", name="ck_portal_documents_uploader"),
        CheckConstraint("scan_status IN ('unscanned', 'clean', 'quarantined')", name="ck_portal_documents_scan"),
    )


class FilingKind(Base):
    __tablename__ = "filing_kinds"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_statuses: Mapped[datetime] = mapped_column(StrList, nullable=False, default=list)


class Filing(Base):
    __tablename__ = "filings"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    kind_code: Mapped[str] = mapped_column(ForeignKey("filing_kinds.code"), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("tax_year BETWEEN 2000 AND 2100", name="ck_filings_tax_year"),
        CheckConstraint(
            "status IN ('collecting', 'awaiting_info', 'preparing', 'filed', 'confirmed')",
            name="ck_filings_status",
        ),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("portal_users.id"), nullable=False, index=True)
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CAD")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    issued_pdf_object_key: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    pay_method_note: Mapped[str | None] = mapped_column(Text)
    void_reason: Mapped[str | None] = mapped_column(Text)
    # Phase 5: owner memo shown on the invoice detail + rendered into the PDF.
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = _created()

    __table_args__ = (
        CheckConstraint("status IN ('issued', 'paid', 'void')", name="ck_invoices_status"),
        CheckConstraint("total_cents = subtotal_cents + tax_cents", name="ck_invoices_totals"),
        CheckConstraint("total_cents >= 0", name="ck_invoices_nonnegative"),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = _uuid_pk()
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[datetime] = mapped_column(Numeric(12, 2), nullable=False, default=1)
    unit_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Phase 5: per-line tax (PORTAL_V1 F4). Basis points, e.g. 500 = 5%.
    # tax_cents is frozen at issuance — the stored line items + PDF are the
    # invoice of record.
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class InvoiceSeries(Base):
    """Phase 5: owner-configurable invoice numbering (PORTAL_V1 §3).

    One row per series (v1 ships a single ``default`` series). Numbers are
    minted as ``prefix + zero-padded next_number``; next_number only moves
    forward — voided numbers are never recycled. Minting holds an advisory
    lock across the read+increment+commit so concurrent issuance can never
    duplicate a number.
    """

    __tablename__ = "invoice_series"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    next_number: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow)


class OneShotMarker(Base):
    """Phase 5: one-shot operation markers (B6) — bootstrap consumed, etc."""

    __tablename__ = "one_shot_markers"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumed_at: Mapped[datetime | None] = _created()


# 2.6 Email outbox + idempotency + audit ------------------------------------------

OUTBOX_STATUSES = ("queued", "sending", "submitted", "captured", "retry", "dead", "cancelled")


class NotificationKind(Base):
    __tablename__ = "notification_kinds"

    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    template_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("channel IN ('email', 'sms', 'portal_inbox')", name="ck_notification_kinds_channel"),
    )


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id: Mapped[str] = _uuid_pk()
    kind: Mapped[str] = mapped_column(ForeignKey("notification_kinds.kind"), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    destination_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_snapshot: Mapped[datetime] = mapped_column(JSONBCol, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'sending', 'submitted', 'captured', 'retry', 'dead', 'cancelled')",
            name="ck_email_outbox_status",
        ),
    )


class OutboxAttempt(Base):
    __tablename__ = "outbox_attempts"

    id: Mapped[str] = _uuid_pk()
    outbox_id: Mapped[str] = mapped_column(ForeignKey("email_outbox.id"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_label: Mapped[str | None] = mapped_column(String(128))


class IdempotencyReplay(Base):
    __tablename__ = "idempotency_replays"

    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    route: Mapped[str] = mapped_column(Text, nullable=False)
    client_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[datetime] = mapped_column(JSONBCol, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime | None] = _created()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime | None] = _created()
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[datetime | None] = mapped_column(JSONBCol)


class DomainEvent(Base):
    __tablename__ = "domain_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime | None] = _created()
    event: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[datetime | None] = mapped_column(JSONBCol)


# 2.9 Rate-budget store -------------------------------------------------------------

class RateWindow(Base):
    __tablename__ = "rate_windows"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# Functional / partial indexes (defined after the classes so columns exist).
# Case-insensitive uniqueness for emails (CITEXT on Postgres, lower() index
# on SQLite — same semantics, portable DDL).
Index("ix_portal_users_email_ci", func.lower(PortalUser.email), unique=True)
# One live invite per email (B2). Note: there is deliberately NO full unique
# index on invitation email — resend mints a new row for the same address
# after revoking the old (the old row is retained for audit).
Index(
    "ix_portal_invitations_live_email",
    func.lower(PortalInvitation.email),
    unique=True,
    postgresql_where=INVITE_LIVE_WHERE,
    sqlite_where=INVITE_LIVE_WHERE,
)
# The unraceable slot backstop (F6/B2): at most one active booking per slot.
Index(
    "ix_bookings_slot_active",
    Booking.slot_start,
    unique=True,
    postgresql_where=BOOKING_ACTIVE_WHERE,
    sqlite_where=BOOKING_ACTIVE_WHERE,
)
# Worker claim path (E1): due intents first.
Index(
    "ix_email_outbox_claim",
    EmailOutbox.status,
    EmailOutbox.not_before,
    EmailOutbox.next_attempt_at,
    postgresql_where=text("status IN ('queued', 'retry')"),
    sqlite_where=text("status IN ('queued', 'retry')"),
)
Index("ix_calendar_blocks_source", CalendarBlock.source, CalendarBlock.external_id, unique=True)
Index("ix_sessions_user", Session.user_id, Session.created_at)
Index("ix_audit_log_entity", AuditLog.entity_type, AuditLog.entity_id, AuditLog.occurred_at)

#: Canonical table inventory — the docs-as-tests gate asserts the migrated
#: database matches this set exactly.
TABLE_NAMES = sorted(Base.metadata.tables.keys())
