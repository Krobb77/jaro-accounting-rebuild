"""Single source of truth for Jaro Accounting's public contact information.

Mirrors ``frontend/src/data/site-contact.json`` (the frontend build injects
that file's values wherever a ``[[contact.*]]`` token appears). Update the
values here to change the phone / email / address everywhere at once:
email footers, invoice headers, error copy, and API responses all read from
``SITE_CONTACT`` — nothing hardcodes contact details.

The street address is intentionally a placeholder until Jared provides the
real one (2026-10-15: physical street address removed from the site).
"""

from __future__ import annotations

SITE_CONTACT: dict[str, str] = {
    "business_name": "Jaro Accounting",
    "legal_name": "1238855 BC LTD",
    "email": "jared@jaroaccounting.com",
    "phone_display": "1-(778)-227-4891",
    "phone_tel": "+17782274891",
    "street": "[Street Address]",
    "city": "New Westminster",
    "region": "BC",
    "postal": "[Postal Code]",
    "country": "CA",
    "website": "jaroaccounting.com",
}


def address_1line() -> str:
    """One-line mailing address, e.g. for email footers and invoice headers."""
    c = SITE_CONTACT
    return f"{c['street']}, {c['city']}, {c['region']} {c['postal']}"


def sender_address() -> str:
    """RFC-style sender, e.g. ``Jaro Accounting <jared@jaroaccounting.com>``."""
    return f"{SITE_CONTACT['business_name']} <{SITE_CONTACT['email']}>"
