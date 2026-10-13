"""Workload configuration — H3/H4 secret binding, H2 explicit mode allowlist.

Secrets follow exactly-one-wins semantics: ``NAME`` xor ``NAME_FILE``.
Both set is a hard error; neither set (for a required secret) is a hard
error. File values are read as UTF-8 with trailing newlines stripped;
empty, NUL-containing, or oversized values are rejected. Secret values
never appear in error messages.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

MODES = ("development", "staging", "production", "test")
WORKLOADS = ("web", "worker", "migrate")
EXPECTED_ROLES = {"web": "jaro_web", "worker": "jaro_worker", "migrate": "jaro_migrate"}
DB_ROLES = ("jaro_web", "jaro_worker", "jaro_migrate")
MAIL_BACKENDS = ("capture", "smtp")

# Every key .env.example may contain. The docs-as-tests gate asserts that
# every key in .env.example is consumed here (no orphaned example config).
KNOWN_KEYS = frozenset(
    {
        "JARO_MODE",
        "JARO_WORKLOAD",
        "DB_ROLE",
        "DATABASE_URL",
        "SECRET_KEY",
        "SECRET_KEY_FILE",
        "TOTP_KEK",
        "TOTP_KEK_FILE",
        "OWNER_INITIAL_PASSWORD",
        "OWNER_INITIAL_PASSWORD_FILE",
        "MAIL_ALLOW_SEND",
        "MAIL_BACKEND",
        "MAIL_CAPTURE_DIR",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_USE_TLS",
        "SMTP_FROM",
        "STORAGE_DIR",
    }
)


class SecretError(ValueError):
    """Raised when a secret binding is ambiguous, missing, or malformed."""


def resolve_secret(
    env: dict,
    name: str,
    *,
    required: bool = True,
    max_len: int = 4096,
) -> str | None:
    """Resolve ``NAME`` xor ``NAME_FILE`` to a string value.

    Value-free errors: the secret's name appears, never its value or path
    contents.
    """
    raw = env.get(name)
    path = env.get(name + "_FILE")
    if raw and path:
        raise SecretError(f"{name} and {name}_FILE are both set (exactly-one-wins)")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read()
        except OSError as exc:
            raise SecretError(f"could not read {name}_FILE") from exc
        value = value.rstrip("\r\n")
    elif raw is not None:
        value = raw
    elif required:
        raise SecretError(f"neither {name} nor {name}_FILE is set")
    else:
        return None
    if not value:
        raise SecretError(f"{name} resolved to an empty value")
    if "\x00" in value:
        raise SecretError(f"{name} contains a NUL byte")
    if len(value) > max_len:
        raise SecretError(f"{name} exceeds the maximum length")
    return value


def normalize_kek(value: str) -> bytes:
    """Normalize the TOTP KEK to exactly 32 bytes.

    Accepts base64 (standard or urlsafe, padded or not) encoding 32 bytes,
    or a raw string of at least 32 characters (first 32 UTF-8 bytes used).
    """
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded, validate=True)
        except Exception:
            continue
        if len(raw) == 32:
            return raw
    raw = value.encode("utf-8")
    if len(raw) >= 32:
        return raw[:32]
    raise SecretError("TOTP_KEK must be base64 of 32 bytes or a raw string of >= 32 chars")


@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str | None = None
    use_tls: bool = True
    from_address: str = "Jaro Accounting <jared@jaroaccounting.com>"


@dataclass
class Settings:
    mode: str = ""
    workload: str = "web"
    db_role: str = ""
    database_url: str = ""
    secret_key: bytes = b""
    totp_kek: bytes = b""
    owner_initial_password: str | None = None
    mail_allow_send_raw: str | None = None
    mail_backend: str = "capture"
    mail_capture_dir: str = "/tmp/jaro-mail-capture"
    storage_dir: str = "./var/storage"
    smtp: SmtpSettings = field(default_factory=SmtpSettings)

    @property
    def mail_allow_send(self) -> bool:
        """Boolean gate. Only the exact string "true" enables real sending."""
        return self.mail_allow_send_raw == "true"


def load_settings(env: dict | None = None) -> Settings:
    """Build Settings from a mapping (defaults to os.environ)."""
    env = dict(os.environ) if env is None else dict(env)
    smtp = SmtpSettings(
        host=env.get("SMTP_HOST", ""),
        port=int(env.get("SMTP_PORT", "587") or 587),
        username=env.get("SMTP_USERNAME", ""),
        password=resolve_secret(env, "SMTP_PASSWORD", required=False),
        use_tls=(env.get("SMTP_USE_TLS", "true") == "true"),
        from_address=env.get("SMTP_FROM", SmtpSettings.from_address),
    )
    return Settings(
        mode=env.get("JARO_MODE", ""),
        workload=env.get("JARO_WORKLOAD", "web"),
        db_role=env.get("DB_ROLE", ""),
        database_url=env.get("DATABASE_URL", ""),
        secret_key=resolve_secret(env, "SECRET_KEY", required=True).encode("utf-8"),
        totp_kek=normalize_kek(resolve_secret(env, "TOTP_KEK", required=True)),
        owner_initial_password=resolve_secret(
            env, "OWNER_INITIAL_PASSWORD", required=False
        ),
        mail_allow_send_raw=env.get("MAIL_ALLOW_SEND"),
        mail_backend=env.get("MAIL_BACKEND", "capture"),
        mail_capture_dir=env.get("MAIL_CAPTURE_DIR", "/tmp/jaro-mail-capture"),
        storage_dir=env.get("STORAGE_DIR", "./var/storage"),
        smtp=smtp,
    )
