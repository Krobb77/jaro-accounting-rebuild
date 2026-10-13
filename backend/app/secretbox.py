"""Authenticated encryption for secrets at rest (AUTH D5).

AES-256-GCM with a 256-bit KEK from the secret store (H3/H4). Used for
``portal_users.totp_secret_enc`` — a DB leak must not yield second factors
(B6 adapted: encryption instead of digest, because the secret must remain
usable by the server).
"""

from __future__ import annotations

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionFailed(Exception):
    pass


def seal(kek: bytes, plaintext: bytes) -> bytes:
    """Encrypt; returns nonce(12) || ciphertext+tag."""
    if len(kek) != 32:
        raise ValueError("KEK must be 32 bytes")
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(kek).encrypt(nonce, plaintext, None)


def unseal(kek: bytes, blob: bytes) -> bytes:
    if len(kek) != 32:
        raise ValueError("KEK must be 32 bytes")
    if len(blob) < 12 + 16:
        raise DecryptionFailed("ciphertext too short")
    try:
        return AESGCM(kek).decrypt(blob[:12], blob[12:], None)
    except InvalidTag as exc:
        raise DecryptionFailed("authentication failed") from exc
