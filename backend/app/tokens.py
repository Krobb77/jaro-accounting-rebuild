"""Token service (F1/D-14, B6 digest-only).

256-bit CSPRNG tokens, URL-safe base64 (43 chars for 32 bytes), stored as
SHA-256 digests only. Verification uses constant-time comparison. A token
verifies at most once: consumption is atomic under an advisory lock so a
replayed token is rejected even under concurrency.
"""

from __future__ import annotations

import base64
import secrets

from . import advisory
from .db import constant_time_compare, sha256_hex


def new_token(nbytes: int = 32) -> str:
    """Mint a raw token. The raw value is shown/stored by the caller once."""
    return base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).rstrip(b"=").decode("ascii")


def token_digest(raw: str) -> str:
    """SHA-256 hex digest — the only form persisted server-side (B6)."""
    return sha256_hex("jaro-token-v1", raw)


def verify_digest(raw: str, digest: str) -> bool:
    return constant_time_compare(token_digest(raw), digest)


class SingleUseTokenStore:
    """Advisory-locked single-use consume over a caller-supplied row type.

    The caller provides ``lookup(digest) -> row | None`` and
    ``mark_used(row)``; this class adds the atomicity (F5 mechanism).
    """

    def __init__(self, lock_backend: advisory.AdvisoryLockBackend, namespace: str):
        self._locks = lock_backend
        self._namespace = namespace

    def consume(self, db, raw: str, lookup, mark_used) -> tuple[bool, object | None]:
        """Atomically consume ``raw``. Returns (ok, row)."""
        digest = token_digest(raw)
        key = advisory.advisory_key(self._namespace, digest)
        with self._locks.acquire(db, key):
            row = lookup(digest)
            if row is None:
                return False, None
            if not verify_digest(raw, digest):
                return False, None
            if not mark_used(row):
                # Already used/expired/revoked — the row told us.
                return False, None
            db.flush()
            return True, row
