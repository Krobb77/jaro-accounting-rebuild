"""Booking reference numbers — format ``JARO-2026-XXXXXX`` (D-11).

The year prefix disambiguates across years for phone support; uniqueness +
stability is the requirement (the DB unique constraint is the backstop).
The 6-char suffix uses an unambiguous alphabet (no 0/O, 1/I/L).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_PATTERN = re.compile(r"^JARO-\d{4}-[A-Z2-9]{6}$")


def new_reference_number(now: datetime | None = None) -> str:
    year = (now or datetime.now(timezone.utc)).year
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"JARO-{year}-{suffix}"


def is_valid_reference_number(value: str) -> bool:
    return _PATTERN.match(value) is not None


_INQUIRY_PATTERN = re.compile(r"^MSG-\d{4}-[A-Z2-9]{6}$")


def new_inquiry_reference(now: datetime | None = None) -> str:
    """Contact-form request id, e.g. ``MSG-2026-X7K2P9`` (EMAIL_TEMPLATES §4.2).

    Mirrors the booking scheme (D-11): year prefix for phone support, the
    same unambiguous alphabet, uniqueness enforced by a DB unique index
    (``ix_inquiries_reference``). Persisted on the inquiry row and used as
    the outbox dedup scope for both the receipt and the owner notice.
    """
    year = (now or datetime.now(timezone.utc)).year
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"MSG-{year}-{suffix}"


def is_valid_inquiry_reference(value: str) -> bool:
    return _INQUIRY_PATTERN.match(value) is not None
