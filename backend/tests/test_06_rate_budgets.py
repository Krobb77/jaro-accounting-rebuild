"""DONE 6 — rate budgets (A5).

- Exceeding any of the 4 dimensions -> 429 + Retry-After.
- Budget-store failure -> 503 fail-closed (never open).
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import advisory, rate_limits  # noqa: E402
from app.rate_limits import BUDGETS, BudgetStoreError, RateLimited, RateLimiter  # noqa: E402
from starlette.requests import Request  # noqa: E402


@pytest.fixture()
def limiter():
    return RateLimiter(b"test-hmac-key-1234567890", advisory.ThreadingAdvisoryLockBackend())


def _budgets(monkeypatch, spec):
    monkeypatch.setattr(rate_limits, "BUDGETS", {"t": spec})


def test_source_dimension_429(db, limiter, monkeypatch):
    _budgets(monkeypatch, {"source": (2, 3600), "route_source": (100, 3600), "global": (1000, 3600)})
    limiter.check(db, budget="t", source="1.2.3.4")
    limiter.check(db, budget="t", source="1.2.3.4")
    with pytest.raises(RateLimited) as exc:
        limiter.check(db, budget="t", source="1.2.3.4")
    assert exc.value.retry_after >= 1
    db.rollback()


def test_route_source_dimension_429(db, limiter, monkeypatch):
    _budgets(monkeypatch, {"source": (100, 3600), "route_source": (1, 3600), "global": (1000, 3600)})
    limiter.check(db, budget="t", source="9.9.9.9")
    with pytest.raises(RateLimited):
        limiter.check(db, budget="t", source="9.9.9.9")
    db.rollback()


def test_target_dimension_429(db, limiter, monkeypatch):
    _budgets(monkeypatch, {"source": (100, 3600), "route_source": (100, 3600),
                           "target": (1, 3600), "global": (1000, 3600)})
    limiter.check(db, budget="t", source="9.9.9.9", target="victim@example.com")
    # same target, different source -> still limited (per-target budget)
    with pytest.raises(RateLimited):
        limiter.check(db, budget="t", source="8.8.8.8", target="victim@example.com")
    # different target is fine
    limiter.check(db, budget="t", source="8.8.8.8", target="other@example.com")
    db.rollback()


def test_global_dimension_429(db, limiter, monkeypatch):
    _budgets(monkeypatch, {"source": (100, 3600), "route_source": (100, 3600), "global": (2, 3600)})
    limiter.check(db, budget="t", source="1.1.1.1")
    limiter.check(db, budget="t", source="2.2.2.2")
    with pytest.raises(RateLimited):
        limiter.check(db, budget="t", source="3.3.3.3")
    db.rollback()


def test_keys_are_pseudonymized(db, limiter, monkeypatch):
    _budgets(monkeypatch, {"source": (100, 3600), "route_source": (100, 3600), "global": (1000, 3600)})
    limiter.check(db, budget="t", source="5.6.7.8")
    db.flush()
    rows = db.execute(text("SELECT key_hash FROM rate_windows")).fetchall()
    assert rows
    blob = " ".join(r[0] for r in rows)
    assert "5.6.7.8" not in blob  # raw IP never touches the table
    # No manual cleanup: the fixture rolls back the outer transaction.


def test_store_failure_fails_closed():
    engine = create_engine("sqlite://")  # no rate_windows table
    Session = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(bind=engine)
    db = Session()
    limiter = RateLimiter(b"test-hmac-key-1234567890", advisory.ThreadingAdvisoryLockBackend())
    with pytest.raises(BudgetStoreError):
        limiter.check(db, budget="portal.login", source="1.2.3.4")
    db.close()
    engine.dispose()


def test_route_429_with_retry_after_header(app, db_admin, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.routes import get_db

    monkeypatch.setattr(
        rate_limits, "BUDGETS",
        {"portal.login": {"source": (100, 3600), "route_source": (2, 3600),
                          "target": (100, 3600), "global": (1000, 3600)}},
    )
    # Production-faithful override: one real session per request, committed
    # like app.routes.get_db does (new A4 contract 2026-10-06: stashed on
    # request.state.db_session; TransactionMiddleware commits BEFORE the
    # response is sent). (The transactional `db` fixture would
    # leave the failed-login audit row uncommitted, holding a SQLite write
    # lock that the next request's limiter session trips over — a harness
    # artifact, not app behavior.)
    Session = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False)

    def _override(request: Request):
        db = Session()
        request.state.db_session = db
        try:
            yield db
        finally:
            db.close()

    # route_source budget is 2: two attempts pass the limiter (then 401 bad
    # credentials — Phase 5 implements the route for real)...
    with db_admin.begin() as conn:
        conn.execute(text("DELETE FROM rate_windows"))
        conn.execute(text("DELETE FROM audit_log"))

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            bodies = [
                {"email": "ratelimit-case@example.com", "password": "x"},
                {"email": "ratelimit-case@example.com", "password": "x"},
                {"email": "ratelimit-case@example.com", "password": "x"},
            ]
            statuses = [c.post("/api/v1/portal/login", json=b).status_code for b in bodies]
            assert statuses[0] == 401 and statuses[1] == 401, statuses
            r = c.post("/api/v1/portal/login", json=bodies[2])
            assert r.status_code == 429, r.status_code
            assert r.json()["code"] == "rate_limited"
            assert int(r.headers["Retry-After"]) >= 1
    finally:
        app.dependency_overrides.clear()
        with db_admin.begin() as conn:
            conn.execute(text("DELETE FROM rate_windows"))
            conn.execute(text("DELETE FROM audit_log"))


def test_route_503_when_budget_store_dead(scratch_db_url, tmp_path):
    """Kill the budget store -> 503 fail-closed on a budget-enforced route."""
    import sqlalchemy as sa
    from alembic.config import Config
    from alembic import command as alembic_command
    from fastapi.testclient import TestClient
    from app.config import load_settings
    from app.main import create_app
    from tests.conftest import BASE_TEST_ENV

    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url)
    alembic_command.upgrade(cfg, "head")

    settings = load_settings({**BASE_TEST_ENV, "DATABASE_URL": scratch_db_url,
                              "MAIL_CAPTURE_DIR": str(tmp_path / "mc")})
    application = create_app(settings)
    try:
        # Kill the store: drop the table with a committed connection.
        engine = sa.create_engine(scratch_db_url)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE rate_windows"))
        engine.dispose()
        with TestClient(application, raise_server_exceptions=False) as c:
            r = c.post("/api/v1/portal/login", json={"email": "a@b.com", "password": "x"})
        assert r.status_code == 503, r.status_code
        assert r.json()["code"] == "budget_unavailable"
    finally:
        application.state.engine.dispose()


def test_source_dimension_is_per_budget(db, limiter, monkeypatch):
    """Regression: the `source` counter must be namespaced by budget.

    Before the fix, the source-dimension key omitted the budget, so every
    rate-limited request from one IP shared a single counter — browsing
    availability (booking.slots) burned through the confirm budget's
    per-IP allowance and produced spurious 429s on /confirm and
    /manage/{token}/reschedule (caught by the Playwright booking journey).
    """
    monkeypatch.setattr(
        rate_limits,
        "BUDGETS",
        {
            "a": {"source": (2, 3600), "route_source": (100, 3600), "global": (1000, 3600)},
            "b": {"source": (2, 3600), "route_source": (100, 3600), "global": (1000, 3600)},
        },
    )
    limiter.check(db, budget="a", source="1.2.3.4")
    limiter.check(db, budget="a", source="1.2.3.4")
    # Budget "a" is now exhausted for this source, but budget "b" is untouched.
    limiter.check(db, budget="b", source="1.2.3.4")
    limiter.check(db, budget="b", source="1.2.3.4")
    with pytest.raises(RateLimited):
        limiter.check(db, budget="b", source="1.2.3.4")
    db.rollback()
