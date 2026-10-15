"""Outbound mail — H1 capture mode, never real sending unless gated.

``MAIL_ALLOW_SEND`` must be exactly ``"true"`` AND ``MAIL_BACKEND`` set to a
real backend before anything leaves the process. In every other mode the
capture backend writes inspectable artifacts (atomic temp-write + fsync +
rename) and tests assert on those artifacts — proving the full pipeline
while making accidental live sends structurally impossible.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import tempfile
from abc import ABC, abstractmethod
from email.message import EmailMessage

from .db import new_uuid7, utcnow
from .site_contact import SITE_CONTACT, address_1line


class MailNotAllowed(Exception):
    """Real sending was attempted without the explicit opt-in gate."""


class RenderedMail:
    def __init__(self, to: str, subject: str, text_body: str, headers: dict | None = None,
                 html_body: str | None = None):
        self.to = to
        self.subject = subject
        self.text_body = text_body
        self.headers = headers or {}
        # Phase 5: the invitation email renders the production HTML
        # (EMAIL_TEMPLATES §2 + the frozen asset) as a multipart
        # alternative. Every other template stays text-only (Phase-2/3
        # precedent) until its HTML sibling ships.
        self.html_body = html_body


class MailBackend(ABC):
    @abstractmethod
    def send(self, mail: RenderedMail, *, intent_id: str, attempt_no: int) -> str:
        """Submit one message. Returns a provider/artifact reference."""


class CaptureMailBackend(MailBackend):
    """H1: capture to inspectable JSON artifacts. One artifact per
    (intent_id, attempt_no) — a retried attempt never overwrites evidence."""

    def __init__(self, capture_dir: str):
        self.capture_dir = capture_dir
        os.makedirs(capture_dir, exist_ok=True)

    def send(self, mail: RenderedMail, *, intent_id: str, attempt_no: int) -> str:
        artifact = {
            "intent_id": intent_id,
            "attempt_no": attempt_no,
            "captured_at": utcnow().isoformat(),
            "to": mail.to,
            "subject": mail.subject,
            "text_body": mail.text_body,
            "headers": mail.headers,
        }
        if mail.html_body is not None:
            artifact["html_body"] = mail.html_body
        name = f"{intent_id}-attempt{attempt_no}.json"
        path = os.path.join(self.capture_dir, name)
        fd, tmp = tempfile.mkstemp(dir=self.capture_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(artifact, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return f"captured:{name}"


class SmtpMailBackend(MailBackend):
    """Real SMTP. The gate is enforced here AND at settings validation —
    defense in depth, not just convention."""

    def __init__(self, settings):
        self._settings = settings

    def send(self, mail: RenderedMail, *, intent_id: str, attempt_no: int) -> str:
        if not self._settings.mail_allow_send:
            raise MailNotAllowed(
                "refusing real send: MAIL_ALLOW_SEND is not exactly 'true'"
            )
        cfg = self._settings.smtp
        msg = EmailMessage()
        msg["From"] = cfg.from_address
        msg["To"] = mail.to
        msg["Subject"] = mail.subject
        for k, v in mail.headers.items():
            msg[k] = v
        msg.set_content(mail.text_body)
        if mail.html_body is not None:
            msg.add_alternative(mail.html_body, subtype="html")
        with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as smtp:
            if cfg.use_tls:
                smtp.starttls()
            if cfg.username:
                smtp.login(cfg.username, cfg.password or "")
            smtp.send_message(msg)
        return f"smtp:{new_uuid7()}"


def create_backend(settings) -> MailBackend:
    if settings.mail_backend == "capture":
        return CaptureMailBackend(settings.mail_capture_dir)
    if settings.mail_backend == "smtp":
        return SmtpMailBackend(settings)
    raise ValueError(f"unknown MAIL_BACKEND: {settings.mail_backend}")


# Minimal Phase-0 templates ----------------------------------------------------
# Real production templates (invitation HTML, booking confirmation, contact
# receipt) are Phase 2/3/5 work. These placeholders keep the outbox
# round-trip testable end-to-end in Phase 0 with honest, clearly-marked
# copy. Each template renders ONLY from the frozen snapshot (E2).
# NOTE: "invite" is intentionally absent from TEMPLATES — it is rendered
# by _render_invite() from the production HTML asset (frozen snapshot),
# not from a text template. render_template() special-cases it above.
TEMPLATES = {
# --- Phase-5 portal templates — text-only renderings of the
    # docs/EMAIL_TEMPLATES.md + AUTH_AND_INVITATIONS.md copy blocks (the
    # shared render pipeline is text-only for these kinds; the §1 HTML
    # skeleton is carried as text structure, per the Phase-2/3 precedent).
    # The invitation kind is special-cased in render_template below: it
    # renders the production HTML asset (frozen snapshot).
    "password_reset": (
        "Reset your Jaro Accounting portal password",
        "Reset your Jaro Accounting portal password.\n"
        "\n"
        "Reset your password\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "A password reset was requested for your Jaro Accounting client portal account.\n"
        "Set a new password using the link below. PLEASE NOTE THE LINK WILL\n"
        "EXPIRE AFTER {expiry_hours} HOURS.\n"
        "\n"
        "Reset your password:\n"
        "{reset_url}\n"
        "\n"
        "Your username is: {client_email}\n"
        "\n"
        "If you have any questions or concerns, please contact Jared Robbins at\n"
        f"{SITE_CONTACT['email']} or call {SITE_CONTACT['phone_display']}.\n"
        "\n"
        "If you didn't request this, you can safely ignore this email — your\n"
        "password won't change unless the link is used. We'll never ask for\n"
        "your password by email or phone.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or\n"
        "entity to which it is addressed and may contain confidential and/or\n"
        "privileged material. Any review, re-transmission, dissemination or\n"
        "other use of, or taking of any action in reliance upon this\n"
        "information by persons or entities other than the intended recipient\n"
        "is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material.",
    ),
    # --- Phase-3 booking templates — text-only renderings of the
    # docs/EMAIL_TEMPLATES.md copy blocks (the shared render pipeline is
    # text-only; the §1 HTML skeleton — charcoal header, red accents,
    # confidentiality footer — is carried here as text structure, per the
    # Phase-2 precedent; DECISIONS-backend.md Phase-0 item 17). Every §3
    # testable element is present verbatim: subject, preheader (first body
    # line), guest-zone times + timezone echo, reference, reschedule/cancel
    # tokenized links (one link set), "didn't book this?" line.
    "booking_code": (
        "Your Jaro Accounting booking code: {code}",
        "Your 6-digit verification code for your Jaro Accounting booking.\n"
        "\n"
        "Your verification code\n"
        "\n"
        "Your code is: {code}\n"
        "\n"
        "Enter it on the booking page to prove this email address is yours.\n"
        "The code expires in {expires_minutes} minutes.\n"
        "\n"
        "Didn't request a booking code? No action needed — without the code,\n"
        "no booking can be made with your address.\n"
        "\n"
        "— Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or\n"
        "entity to which it is addressed and may contain confidential and/or\n"
        "privileged material. Any review, re-transmission, dissemination or\n"
        "other use of, or taking of any action in reliance upon this\n"
        "information by persons or entities other than the intended recipient\n"
        "is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material.",
    ),
    "booking_confirmation": (
        "Your Jaro Accounting appointment is booked — {date_long}",
        "Your {service_name} on {date_short} at {time_start_guest_tz}. Details, what to prepare, and reschedule options inside.\n"
        "\n"
        "Your appointment is booked\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Your {service_name} with Jaro Accounting is confirmed:\n"
        "\n"
        "- Date: {date_long}\n"
        "- Time: {time_start_guest_tz} – {time_end_guest_tz}\n"
        "- Timezone: {guest_timezone} (all times above are in your timezone)\n"
        "- Also: {firm_time}\n"
        "- Where: {meeting_location}\n"
        "- Reference: {booking_reference}\n"
        "{rescheduled_from_block}"
        "\n"
        "What to prepare\n"
        "\n"
        "{preparation_bullets}\n"
        "\n"
        "Need to change it?\n"
        "\n"
        "You can reschedule or cancel without an account using the links\n"
        "below. If you reschedule, you'll get a fresh confirmation like\n"
        "this one.\n"
        "\n"
        "Reschedule: {reschedule_url}\n"
        "Cancel: {cancel_url}\n"
        "\n"
        "If you have any questions, please contact Jared Robbins at\n"
        "{contact_email} or call {contact_phone}.\n"
        "\n"
        "This email was triggered by a booking made on jaroaccounting.com.\n"
        "If you didn't book this, you can safely ignore it — or cancel it\n"
        "with the link above.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or\n"
        "entity to which it is addressed and may contain confidential and/or\n"
        "privileged material. Any review, re-transmission, dissemination or\n"
        "other use of, or taking of any action in reliance upon this\n"
        "information by persons or entities other than the intended recipient\n"
        "is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material.",
    ),
    "booking_owner_notification": (
        "New booking: {booking_reference}",
        "Jaro Accounting — booking notification (for Jared only)\n"
        "\n"
        "{headline}\n"
        "\n"
        "When: {slot_human}\n"
        "Reference: {booking_reference}\n"
        "\n"
        "Guest: {guest_name} <{guest_email}>\n"
        "Phone: {guest_phone}\n"
        "Relationship: {guest_relationship}\n"
        "\n"
        "What they want to discuss:\n"
        "{discuss}\n"
        "\n"
        "---\n"
        "This notice is for Jared only.\n"
        "{contact_business_line}",
    ),
    "booking_reminder": (
        "Reminder: your consultation on {date_short}",
        "A reminder about your upcoming consultation with Jaro Accounting.\n"
        "\n"
        "Your consultation is coming up\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "This is a reminder about your {service_name} with Jaro Accounting:\n"
        "\n"
        "- Date: {date_long}\n"
        "- Time: {time_range}\n"
        "- Timezone: {guest_timezone} (all times above are in your timezone)\n"
        "- Where: Phone call — Jared will call you\n"
        "- Reference: {booking_reference}\n"
        "\n"
        "Need to change it? You can still reschedule or cancel without an\n"
        "account (up to 24 hours before the appointment):\n"
        "\n"
        "Reschedule: {reschedule_url}\n"
        "Cancel: {cancel_url}\n"
        "\n"
        "If you have any questions, please contact Jared Robbins at\n"
        "{contact_email} or call {contact_phone}.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 Jaro Accounting. All rights reserved.",
    ),
    # EMAIL_TEMPLATES §5 item 8 (out of scope for that document): the
    # booking workstream's cancellation notice, following the §1 skeleton —
    # one link (the rebook URL for the guest; none for the owner),
    # phishing-safe (typed-out contact details), honest vocabulary
    # ("cancelled", never "sent"/"confirmed").
    "booking_cancellation_notice": (
        "Booking cancelled — {booking_reference}",
        "Your Jaro Accounting appointment has been cancelled.\n"
        "\n"
        "Your appointment is cancelled\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Your appointment with Jaro Accounting has been cancelled:\n"
        "\n"
        "- Was: {slot_human}\n"
        "- Reference: {booking_reference}\n"
        "{cancel_reason_block}"
        "\n"
        "{detail_block}\n"
        "\n"
        "{action_line}"
        "\n"
        "If you have any questions, please contact Jared Robbins at\n"
        "{contact_email} or call {contact_phone}.\n"
        "\n"
        "This email was triggered by a cancellation on jaroaccounting.com.\n"
        "If you didn't cancel this, please call {contact_phone} so we can\n"
        "look into it.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or\n"
        "entity to which it is addressed and may contain confidential and/or\n"
        "privileged material. Any review, re-transmission, dissemination or\n"
        "other use of, or taking of any action in reliance upon this\n"
        "information by persons or entities other than the intended recipient\n"
        "is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material.",
    ),
    # Phase-2 contact pipeline — real copy per docs/EMAIL_TEMPLATES.md §4
    # (receipt) and the §1 skeleton (owner notice). These are text-only
    # renderings of the doc's copy blocks: the shared render pipeline is
    # text-only in Phase 0 (DECISIONS-backend.md Phase-0 item 17), so the
    # §1 HTML skeleton (charcoal header, red accents, confidentiality
    # footer) is carried here as text structure — branded header line,
    # body, gray-footer fine print. The full HTML render upgrade is a
    # documented Phase-5 seam alongside the production invitation template.
    "inquiry_owner_notice": (
        "New website inquiry — {request_id}",
        "Jaro Accounting — new contact-form inquiry\n"
        "\n"
        "A new message was submitted on jaroaccounting.com/contact-us/.\n"
        "\n"
        "From: {guest_name} <{guest_email}>\n"
        "Reference: {request_id}\n"
        "Submitted: {submitted_at}\n"
        "\n"
        "Message:\n"
        "{message}\n"
        "\n"
        "---\n"
        "This notice is for Jared only. Reply to the guest at {guest_email}\n"
        "(set as Reply-To).\n"
        "{contact_business_line}",
    ),
    "inquiry_receipt": (
        "We received your message — ref {request_id}",
        # The first line is the §4 preheader: in a text email the first
        # body line is what inbox clients show as preview text, which is
        # exactly the preheader's job.
        "Thanks, {first_name} — your message is with Jared. "
        "Your reference: {request_id}.\n"
        "\n"
        "Thanks for reaching out\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "We've received your message and will get back to you as soon as we\n"
        "can. There's no need to send it again.\n"
        "\n"
        "What happens next\n"
        "\n"
        "1. Jared will review your message — normally within {response_sla}.\n"
        "2. We'll reply from {contact_email}, so it's worth checking\n"
        "   your spam folder if you don't hear from us.\n"
        "3. If it's urgent, call {contact_phone}.\n"
        "\n"
        "Your reference: {request_id}\n"
        "Please mention this reference if you contact us about this message.\n"
        "\n"
        "Your message, as received:\n"
        "{message_excerpt}\n"
        "\n"
        "Didn't submit this form? No action needed — this receipt only goes\n"
        "to the email address entered on our contact page, so you can safely\n"
        "delete it.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or\n"
        "entity to which it is addressed and may contain confidential and/or\n"
        "privileged material. Any review, re-transmission, dissemination or\n"
        "other use of, or taking of any action in reliance upon this\n"
        "information by persons or entities other than the intended recipient\n"
        "is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material.",
    ),
    "document_received": (
        "New document from {client_name} — {filename}",
        "Jaro Accounting — document received (for Jared only)\n"
        "\n"
        "{client_name} <{client_email}> uploaded a document to the client portal:\n"
        "\n"
        "- File: {filename}\n"
        "- Category: {category}\n"
        "{note_block}"
        "- Uploaded: {uploaded_at}\n"
        "\n"
        "View it in the owner console under this client's Documents tab.\n"
        "\n"
        "---\n"
        "This notice is for Jared only.\n"
        "{contact_business_line}",
    ),
    "document_shared": (
        "Jaro Accounting shared a document with you — {filename}",
        "A new document from Jaro Accounting.\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Jared Robbins shared a document with you in the client portal:\n"
        "\n"
        "- File: {filename}\n"
        "- Category: {category}\n"
        "\n"
        "Sign in to the portal to view and download it.\n"
        "\n"
        "If you have any questions, please contact Jared Robbins at\n"
        "{contact_email} or call {contact_phone}.\n"
        "\n"
        "If you weren't expecting this email, please don't click any links —\n"
        "contact Jared Robbins directly at {contact_email}.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 Jaro Accounting. All rights reserved.",
    ),
    "filing_status_changed": (
        "Your {tax_year} {kind_label} — {status_plain}",
        "Your filing status changed.\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Your {tax_year} {kind_label} {status_plain}.\n"
        "{note_block}"
        "\n"
        "Sign in to the portal to see the full filing timeline.\n"
        "\n"
        "If you have any questions, please contact Jared Robbins at\n"
        "{contact_email} or call {contact_phone}.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 Jaro Accounting. All rights reserved.",
    ),
    "invoice_issued": (
        "New invoice {invoice_number} from Jaro Accounting",
        "A new invoice is available in your client portal.\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Jaro Accounting issued invoice {invoice_number}:\n"
        "\n"
        "- Amount due: {total}\n"
        "- Due: {due_date}\n"
        "\n"
        "Sign in to the portal to view the invoice and download the PDF.\n"
        "\n"
        # "To pay" block mirrors portal.INVOICE_PAYMENT_INSTRUCTIONS (D-27;
        # §3.5 #5, OWNER-REVIEWABLE) — keep the wording identical; contact
        # details stay in sync via the {contact_*} placeholders.
        "To pay: e-transfer to {contact_email} — please include your\n"
        "invoice number with your payment. Cheque also accepted; contact Jared\n"
        "Robbins at {contact_phone} for details.\n"
        "\n"
        "If you weren't expecting this email, please contact Jared Robbins\n"
        "directly at {contact_email}.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 Jaro Accounting. All rights reserved.",
    ),
    "mfa_sms_fallback": (
        "Your Jaro Accounting sign-in code: {code}",
        "Your Jaro Accounting sign-in code.\n"
        "\n"
        "Hi {first_name},\n"
        "\n"
        "Your sign-in code is: {code}\n"
        "\n"
        "Enter it on the sign-in page. The code expires in {expires_minutes} minutes.\n"
        "\n"
        "Didn't try to sign in? No action needed — without the code, no one\n"
        "can sign in as you. If you're concerned, contact Jared Robbins at\n"
        "{contact_email} or {contact_phone}.\n"
        "\n"
        "— Jaro Accounting",
    ),
}


# Per-kind message headers, rendered from the frozen snapshot like the
# body (placeholders are interpolated). Kept separate from TEMPLATES so the
# Phase-0 (subject, body) shape — and its tests — stay untouched.
TEMPLATE_HEADERS: dict[str, dict[str, str]] = {
    # §4.3: a reply to the receipt lands with Jared, not in a no-reply void.
    "inquiry_receipt": {"Reply-To": "{contact_business_name} <{contact_email}>"},
    # Owner notice: Reply-To the guest so Jared can answer in one click.
    # The guest address passed email validation (no whitespace/control
    # chars), so it is header-safe; it is the address the guest entered,
    # which is exactly who Jared needs to reach.
    "inquiry_owner_notice": {"Reply-To": "{guest_name} <{guest_email}>"},
    # Booking guest mail: a reply lands with Jared, not in a no-reply void
    # (§1.4 consistent From/Reply-To). The owner notice needs no Reply-To
    # override (it goes to Jared himself).
    "booking_confirmation": {"Reply-To": "{contact_business_name} <{contact_email}>"},
    "booking_reminder": {"Reply-To": "{contact_business_name} <{contact_email}>"},
    "booking_cancellation_notice": {"Reply-To": "{contact_business_name} <{contact_email}>"},
}


def _contact_template_vars() -> dict:
    """Contact placeholders available in every template (single source of
    truth: app/site_contact.py). Update the phone / email / address there to
    change every email footer at once."""
    c = SITE_CONTACT
    return {
        "contact_business_name": c["business_name"],
        "contact_legal_name": c["legal_name"],
        "contact_business_line": f"{c['business_name']} \u00b7 {c['legal_name']}",
        "contact_email": c["email"],
        "contact_phone": c["phone_display"],
        "contact_address_1line": address_1line(),
        "contact_website": c["website"],
    }


def render_template(kind: str, snapshot: dict) -> RenderedMail:
    """Render a notification kind from its frozen snapshot only."""
    fields = snapshot.get("fields", {})
    if kind == "invite":
        return _render_invite(fields)
    subject_tpl, body_tpl = TEMPLATES[kind]  # KeyError on unknown kind: fail loudly
    safe = _SafeDict(fields)
    safe.update(_contact_template_vars())
    headers = {k: v.format_map(safe) for k, v in TEMPLATE_HEADERS.get(kind, {}).items()}
    return RenderedMail(
        to=fields.get("destination", ""),
        subject=subject_tpl.format_map(safe),
        text_body=body_tpl.format_map(safe),
        headers=headers,
    )


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


# --- Invitation email: the production HTML asset (Phase 5) -----------------------
# The source file is a FROZEN snapshot of
# jaroaccounting-replication/assets/email/jaro-invitation-email.html
# (EMAIL_TEMPLATES §2): it is copied byte-identical and never edited in
# place. A test asserts the bytes still match the source asset. The worker
# renders it from the frozen outbox snapshot (E2): only the four
# placeholders below are substituted, user input is HTML-escaped, and the
# token URL must pass the origin allowlist or the render fails loudly
# (the intent goes dead — never a half-rendered credential email).

_INVITE_HTML_PATH = os.path.join(
    os.path.dirname(__file__), "mail_templates", "jaro-invitation-email.html"
)
with open(_INVITE_HTML_PATH, "r", encoding="utf-8") as _fh:
    INVITE_HTML = _fh.read()

# The token URL is the ONLY credential/action link in the email
# (EMAIL_TEMPLATES §1.3). It must be exactly the portal accept URL on the
# production origin — anything else is a render refusal.
_INVITE_URL_RE = re.compile(
    r"https://jaroaccounting\.com/portal/accept\?token=[A-Za-z0-9_-]{43}"
)


def render_invite_html(fields: dict) -> str:
    """Render the production invitation HTML from frozen snapshot fields."""
    url = fields.get("set_password_url", "")
    if not _INVITE_URL_RE.fullmatch(url):
        raise ValueError("refusing to render invite: set_password_url failed the origin allowlist")
    first_name = (fields.get("first_name") or "").strip() or "there"
    portal_name = fields.get("portal_name") or "myJaro"
    out = INVITE_HTML
    out = out.replace("{{FIRST_NAME}}", html.escape(first_name))
    # §2.1: the client email is plain text, never a mailto link.
    out = out.replace("{{CLIENT_EMAIL}}", html.escape(fields.get("client_email", "")))
    out = out.replace("{{SET_PASSWORD_URL}}", url)
    # Cross-workstream contract (IMPLEMENTATION_PLAN §5): the copy lifetime
    # and the invite row lifetime are the same constant.
    out = out.replace("{{EXPIRY_DAYS}}", html.escape(str(fields.get("expiry_days", "7"))))
    # D-29: the portal name is parameterized — a rename is one change.
    out = out.replace("myJaro", html.escape(portal_name))
    # Contact details are parameterized too (single source of truth:
    # app/site_contact.py) — a phone / email / address change is one edit.
    c = SITE_CONTACT
    out = out.replace("{{CONTACT_BUSINESS_NAME}}", html.escape(c["business_name"]))
    out = out.replace("{{CONTACT_LEGAL_NAME}}", html.escape(c["legal_name"]))
    out = out.replace("{{CONTACT_EMAIL}}", html.escape(c["email"]))
    out = out.replace("{{CONTACT_PHONE}}", html.escape(c["phone_display"]))
    out = out.replace("{{CONTACT_PHONE_TEL}}", html.escape(c["phone_tel"]))
    out = out.replace("{{CONTACT_STREET}}", html.escape(c["street"]))
    out = out.replace("{{CONTACT_CITY}}", html.escape(c["city"]))
    out = out.replace("{{CONTACT_REGION}}", html.escape(c["region"]))
    out = out.replace("{{CONTACT_POSTAL}}", html.escape(c["postal"]))
    out = out.replace("{{CONTACT_WEBSITE}}", html.escape(c["website"]))
    return out


def _render_invite_text(fields: dict) -> str:
    """Plain-text alternative per EMAIL_TEMPLATES §2.3."""
    first_name = (fields.get("first_name") or "").strip() or "there"
    portal_name = fields.get("portal_name") or "myJaro"
    return (
        f"Welcome to {portal_name}\n"
        "\n"
        f"Hi {first_name},\n"
        "\n"
        f"You now have access to {portal_name}: your secure client portal where you can view\n"
        "your invoices, upload and download tax documents, and check your filing status.\n"
        "\n"
        "To sign in, set your password using the link below. PLEASE NOTE THE LINK WILL\n"
        f"EXPIRE AFTER {fields.get('expiry_days', '7')} DAYS.\n"
        "\n"
        "Set your password:\n"
        f"{fields.get('set_password_url', '')}\n"
        "\n"
        f"Your username is: {fields.get('client_email', '')}\n"
        "\n"
        "If you have any questions or concerns, please contact Jared Robbins at\n"
        f"{SITE_CONTACT['email']} or call {SITE_CONTACT['phone_display']}.\n"
        "\n"
        "If you weren't expecting this email, please don't click the link -- contact\n"
        f"Jared Robbins directly at {SITE_CONTACT['email']} and let him know. We'll\n"
        "never ask for your password by email or phone.\n"
        "\n"
        "Sincerely,\n"
        "Jaro Accounting\n"
        "\n"
        "---\n"
        "{contact_business_line}\n"
        "{contact_address_1line}\n"
        "{contact_website}\n"
        "Copyright © 2026 {contact_business_name}. All rights reserved.\n"
        "\n"
        "The information transmitted is intended only for the person or entity to which\n"
        "it is addressed and may contain confidential and/or privileged material. Any\n"
        "review, re-transmission, dissemination or other use of, or taking of any action\n"
        "in reliance upon this information by persons or entities other than the intended\n"
        "recipient is prohibited. If you received this in error, please contact the\n"
        "sender and delete the material."
    )


def _render_invite(fields: dict) -> RenderedMail:
    portal_name = fields.get("portal_name") or "myJaro"
    return RenderedMail(
        to=fields.get("destination", ""),
        subject=f"You're invited to {portal_name} — set your password",
        text_body=_render_invite_text(fields),
        headers={"Reply-To": f"{SITE_CONTACT['business_name']} <{SITE_CONTACT['email']}>",},
        html_body=render_invite_html(fields),
    )
