"""DONE 2 — verify_runtime refuses to boot on every misconfiguration.

Cases: bad/missing secrets, wrong DB role, unpinned Alembic head,
MAIL_ALLOW_SEND unset/misspelled. Plus the happy path.
"""

from __future__ import annotations

import os
import sys

import pytest
from alembic import command as alembic_command
from alembic.config import Config

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import SecretError, load_settings  # noqa: E402
from app.runtime_check import RuntimeRefused, verify_runtime  # noqa: E402

BASE = {
    "JARO_MODE": "test",
    "JARO_WORKLOAD": "web",
    "DB_ROLE": "jaro_web",
    "SECRET_KEY": "test-secret-key-0123456789abcdef",
    "TOTP_KEK": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "MAIL_ALLOW_SEND": "false",
    "MAIL_BACKEND": "capture",
}


def _settings(db_url: str, **overrides):
    env = {**BASE, "DATABASE_URL": db_url, **overrides}
    return load_settings(env)


def test_happy_path(db_url):
    verify_runtime(_settings(db_url))  # must not raise


def test_missing_secret_refuses():
    env = {k: v for k, v in BASE.items() if k != "SECRET_KEY"}
    env["DATABASE_URL"] = "sqlite:////tmp/x.db"
    with pytest.raises(SecretError, match="SECRET_KEY"):
        load_settings(env)


def test_both_secret_forms_refuses():
    env = {**BASE, "DATABASE_URL": "sqlite:////tmp/x.db",
           "SECRET_KEY_FILE": "/tmp/some-key-file"}
    with pytest.raises(SecretError, match="exactly-one-wins"):
        load_settings(env)


def test_short_secret_refuses_at_verify(db_url):
    settings = _settings(db_url, SECRET_KEY="short")
    with pytest.raises(RuntimeRefused, match="too short"):
        verify_runtime(settings)


def test_bad_mode_refuses(db_url):
    settings = _settings(db_url, JARO_MODE="preview")
    with pytest.raises(RuntimeRefused, match="allowlist"):
        verify_runtime(settings)


def test_wrong_db_role_refuses(db_url):
    settings = _settings(db_url, DB_ROLE="jaro_worker")
    with pytest.raises(RuntimeRefused, match="does not match workload"):
        verify_runtime(settings)


def test_unknown_db_role_refuses(db_url):
    settings = _settings(db_url, DB_ROLE="postgres")
    with pytest.raises(RuntimeRefused, match="not a known role"):
        verify_runtime(settings)


def test_unpinned_head_refuses(scratch_db_url):
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic")),
    )
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "0003")  # deliberately behind head
    with pytest.raises(RuntimeRefused, match="pinned head"):
        verify_runtime(_settings(scratch_db_url))


def test_unmigrated_db_refuses(scratch_db_url):
    with pytest.raises(RuntimeRefused, match="pinned head"):
        verify_runtime(_settings(scratch_db_url))


def test_mail_allow_send_unset_refuses(db_url):
    env = {k: v for k, v in BASE.items() if k != "MAIL_ALLOW_SEND"}
    settings = load_settings({**env, "DATABASE_URL": db_url})
    with pytest.raises(RuntimeRefused, match="MAIL_ALLOW_SEND must be exactly"):
        verify_runtime(settings)


def test_mail_allow_send_misspelled_refuses(db_url):
    settings = _settings(db_url, MAIL_ALLOW_SEND="yes")
    with pytest.raises(RuntimeRefused, match="MAIL_ALLOW_SEND must be exactly"):
        verify_runtime(settings)


def test_unknown_mail_backend_refuses(db_url):
    settings = _settings(db_url, MAIL_BACKEND="sendgrid")
    with pytest.raises(RuntimeRefused, match="MAIL_BACKEND"):
        verify_runtime(settings)
