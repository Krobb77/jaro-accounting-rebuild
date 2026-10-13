"""Phase-0 test fixtures.

G1 safety gate: every database used here is a disposable SQLite file under
pytest's tmp_path. If JARO_DATABASE_URL is set in the environment to anything
that is not such a disposable target, the session refuses to start.
"""

from __future__ import annotations

import os
import sys

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND_DIR)

from app import main as app_main  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.routes import get_db  # noqa: E402

BASE_TEST_ENV = {
    "JARO_MODE": "test",
    "JARO_WORKLOAD": "web",
    "DB_ROLE": "jaro_web",
    "SECRET_KEY": "test-secret-key-0123456789abcdef",
    "TOTP_KEK": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "MAIL_ALLOW_SEND": "false",
    "MAIL_BACKEND": "capture",
}


def _g1_check():
    url = os.environ.get("DATABASE_URL", "")
    if url and not url.startswith("sqlite"):
        pytest.exit(
            f"G1 safety gate: refusing to run tests with DATABASE_URL={url!r}; "
            "tests only run against disposable sqlite files",
            returncode=2,
        )


_g1_check()


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture(scope="session")
def db_url(tmp_path_factory):
    """Session-scoped disposable database, migrated to head once."""
    path = tmp_path_factory.mktemp("jaro-test-db") / "test.db"
    url = f"sqlite:///{path}"
    alembic_command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture()
def db(db_url):
    """Per-test transactional session; rolled back at teardown.

    NOTE: ``db.commit()`` inside a test does NOT release the outer
    transaction (the session joins it); committed data is NOT visible to
    other connections. Tests needing cross-connection visibility (session
    middleware, rate limiter, worker) must use ``committed_db`` or
    ``db_admin`` and clean up their own rows.
    """
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    conn = engine.connect()
    tx = conn.begin()
    Session = sessionmaker(bind=conn, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    tx.rollback()
    conn.close()
    engine.dispose()


@pytest.fixture()
def db_admin(db_url):
    """Direct engine access for committed SQL (setup/teardown that other
    connections must see)."""
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


@pytest.fixture()
def committed_db(db_url):
    """Per-test session that commits (for cross-connection visibility, e.g.
    the session middleware). Tests must clean up their own rows."""
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def app(db_url, tmp_path):
    settings = load_settings(
        {
            **BASE_TEST_ENV,
            "DATABASE_URL": db_url,
            "MAIL_CAPTURE_DIR": str(tmp_path / "mail-capture"),
            "STORAGE_DIR": str(tmp_path / "storage"),
        }
    )
    application = app_main.create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture()
def client(app, db):
    """TestClient with the route DB dependency bound to the test transaction."""
    from fastapi.testclient import TestClient

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def scratch_db_url(tmp_path):
    """A second disposable DB file for tests that need their own database
    (migration rehearsal, store-failure injection)."""
    path = tmp_path / "scratch.db"
    return f"sqlite:///{path}"
