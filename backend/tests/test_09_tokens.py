"""DONE 9 — token service.

- Raw token verifies once; replayed consume rejected.
- Digest-only storage: a DB dump contains no raw token.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import advisory, tokens  # noqa: E402
from app.db import utcnow  # noqa: E402
from app.models import PortalInvitation  # noqa: E402


def test_token_verifies_once(db):
    backend = advisory.ThreadingAdvisoryLockBackend()
    store = tokens.SingleUseTokenStore(backend, "test-invite")
    raw = tokens.new_token()
    assert len(raw) == 43  # 32 bytes, urlsafe base64, no padding

    digest = tokens.token_digest(raw)
    assert digest != raw
    assert len(digest) == 64

    consumed = []

    def lookup(d):
        return {"digest": digest} if d == digest and not consumed else None

    def mark_used(row):
        consumed.append(row)
        return True

    ok, _row = store.consume(db, raw, lookup, mark_used)
    assert ok is True
    # Replay rejected: the row reports itself used.
    ok2, _ = store.consume(db, raw, lookup, mark_used)
    assert ok2 is False
    # Wrong token never verifies.
    ok3, _ = store.consume(db, tokens.new_token(), lookup, mark_used)
    assert ok3 is False


def test_digest_only_storage(db):
    """A DB dump contains digests, never raw tokens (B6)."""
    raw = tokens.new_token()
    row = PortalInvitation(
        email="tokentest@example.com",
        client_name="Token Test",
        token_hash=tokens.token_digest(raw),
        status="created",
        created_at=utcnow(),
        expires_at=utcnow(),
    )
    db.add(row)
    db.flush()
    # Simulate a dump: every text value in the row.
    values = []
    for col in PortalInvitation.__table__.columns:
        values.append(str(getattr(row, col.name)))
    blob = "\n".join(values)
    assert raw not in blob
    assert tokens.token_digest(raw) in blob


def test_constant_time_compare():
    assert tokens.verify_digest("abc", tokens.token_digest("abc")) is True
    assert tokens.verify_digest("abd", tokens.token_digest("abc")) is False
