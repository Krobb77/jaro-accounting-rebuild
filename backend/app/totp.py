"""TOTP — SHA-1, 30s period, 6 digits, ±1 step window (D-36).

Implemented on stdlib hmac/hashlib (RFC 6238). Enrollment requires two
consecutive codes; verification accepts ±1 step for clock skew.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def generate_secret(nbytes: int = 20) -> str:
    """New TOTP secret, base32 (no padding) — the provisioning form."""
    return base64.b32encode(secrets.token_bytes(nbytes)).rstrip(b"=").decode("ascii")


def provisioning_uri(secret: str, *, account: str, issuer: str = "Jaro Accounting") -> str:
    padded = secret + "=" * (-len(secret) % 8)
    raw = base64.b32decode(padded)
    _ = raw  # validated decodable
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


def _code_at(secret: str, counter: int) -> str:
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def code_at(secret: str, for_time: float | None = None) -> str:
    counter = int((for_time if for_time is not None else time.time()) // 30)
    return _code_at(secret, counter)


def verify(secret: str, code: str, *, for_time: float | None = None, window: int = 1) -> bool:
    """Verify with ±``window`` steps. Constant-time compare per candidate."""
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False
    now = for_time if for_time is not None else time.time()
    counter = int(now // 30)
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_code_at(secret, counter + delta), code):
            return True
    return False


def verify_enrollment_pair(secret: str, first: str, second: str, *, for_time: float | None = None) -> bool:
    """D-36: enrollment needs two consecutive codes (anti-mis-enrollment).

    Boundary-tolerant: accepts (prev, current) or (current, next). A client
    that reads two codes just before a 30s rollover must not fail when the
    server verifies just after it. The security property is unchanged — the
    caller still proves possession of two consecutive valid codes.
    """
    now = for_time if for_time is not None else time.time()
    c = int(now // 30)
    first, second = first.strip(), second.strip()
    current_pair = hmac.compare_digest(_code_at(secret, c), first) and hmac.compare_digest(
        _code_at(secret, c + 1), second
    )
    previous_pair = hmac.compare_digest(_code_at(secret, c - 1), first) and hmac.compare_digest(
        _code_at(secret, c), second
    )
    return current_pair or previous_pair
