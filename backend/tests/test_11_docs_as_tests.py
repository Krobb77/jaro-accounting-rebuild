"""DONE 11 — docs-as-tests (G3).

- README setup steps execute (alembic upgrade head; create_app boots; /healthz 200).
- .env.example parses and every key is consumed by the config loader.
- No *register*/*signup* route or identifier exists in code or OpenAPI
  (the no-public-signup invariant as a regression gate).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import KNOWN_KEYS  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKEND_DIR, ".."))


def _parse_env_example(path: str) -> dict:
    values = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_readme_exists_and_documents_setup():
    readme = open(os.path.join(BACKEND_DIR, "README.md"), encoding="utf-8").read()
    assert "alembic" in readme and "upgrade head" in readme
    assert "uvicorn" in readme and "create_app" in readme
    assert ".env.example" in readme


def test_readme_setup_steps_execute(scratch_db_url, tmp_path):
    """Execute the substance of the README setup: migrate, boot, probe."""
    env = dict(os.environ)
    env["DATABASE_URL"] = scratch_db_url
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]

    from tests.conftest import BASE_TEST_ENV
    from app.config import load_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    settings = load_settings({**BASE_TEST_ENV, "DATABASE_URL": scratch_db_url,
                              "MAIL_CAPTURE_DIR": str(tmp_path / "mc")})
    application = create_app(settings)
    try:
        with TestClient(application) as c:
            assert c.get("/healthz").status_code == 200
            assert c.get("/readyz").status_code == 200
    finally:
        application.state.engine.dispose()


def test_env_example_keys_all_consumed():
    example = _parse_env_example(os.path.join(BACKEND_DIR, ".env.example"))
    assert example, ".env.example parsed to nothing"
    orphans = [k for k in example if k not in KNOWN_KEYS]
    assert not orphans, f".env.example keys not consumed by config: {orphans}"
    # And the critical gates are present in the example.
    for key in ("JARO_MODE", "DATABASE_URL", "SECRET_KEY", "MAIL_ALLOW_SEND"):
        assert key in example


def test_no_public_signup_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    blob = "\n".join(paths)
    for pattern in ("register", "signup", "sign-up", "public-signup"):
        assert pattern not in blob.lower(), f"public-signup route pattern {pattern!r} in OpenAPI"


def test_no_public_signup_in_source():
    """The no-public-signup invariant: no route or identifier that would let
    a visitor self-register. (StorageRegistry.register is a false positive
    and is excluded by the patterns below.)"""
    hits = []
    for root, _dirs, files in os.walk(os.path.join(BACKEND_DIR, "app")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            for i, line in enumerate(open(path, encoding="utf-8"), 1):
                low = line.lower()
                if "no-public-signup" in low or "no_public_signup" in low:
                    continue
                if re.search(r"/register|\bsignup\b|\bsign_up\b", low):
                    hits.append(f"{path}:{i}: {line.strip()[:100]}")
                elif re.search(r"\bregister\b", low) and not re.search(
                    r"def register|\.register\(|registered", low
                ):
                    hits.append(f"{path}:{i}: {line.strip()[:100]}")
    assert not hits, "public-signup identifiers in source:\n" + "\n".join(hits)


def test_root_readme_documents_setup():
    """G3: the root README is parsed as assertions — it must document the
    same setup substance the backend README does (venv, env, migrations,
    uvicorn factory, worker -m entry point, frontend build)."""
    readme = open(os.path.join(ROOT_DIR, "README.md"), encoding="utf-8").read()
    for needle in (".venv", ".env.example", "alembic upgrade head",
                   "uvicorn", "create_app --factory",
                   "python -m app.worker_main", "build.py",
                   "BUILD_REPORT.md", "DECISIONS.md"):
        assert needle in readme, f"root README missing setup element: {needle!r}"


def test_root_readme_setup_steps_execute(scratch_db_url, tmp_path):
    """Execute the root README's backend setup substance end to end:
    migrate -> boot API -> worker --once -> frontend build into a scratch
    dir (the real dist/ is never touched)."""
    env = dict(os.environ)
    env["DATABASE_URL"] = scratch_db_url

    # alembic upgrade head (README step 2)
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]

    from tests.conftest import BASE_TEST_ENV
    from app.config import load_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    settings = load_settings({**BASE_TEST_ENV, "DATABASE_URL": scratch_db_url,
                              "MAIL_CAPTURE_DIR": str(tmp_path / "mc")})
    application = create_app(settings)
    try:
        with TestClient(application) as c:
            assert c.get("/healthz").status_code == 200
    finally:
        application.state.engine.dispose()

    # worker --once (README step 4)
    wenv = dict(env)
    wenv.update({**BASE_TEST_ENV, "JARO_WORKLOAD": "worker",
                 "DB_ROLE": "jaro_worker",
                 "MAIL_CAPTURE_DIR": str(tmp_path / "mc2")})
    r = subprocess.run(
        [sys.executable, "-m", "app.worker_main", "--once"],
        cwd=BACKEND_DIR, env=wenv, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "worker cycle complete" in r.stderr

    # frontend build (README step 5) into a scratch dir
    frontend_dir = os.path.join(ROOT_DIR, "frontend")
    scratch_out = os.path.join(str(tmp_path), "dist")
    r = subprocess.run(
        [sys.executable, "build.py", "--out", scratch_out],
        cwd=frontend_dir, env=env, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert os.path.isfile(os.path.join(scratch_out, "index.html"))
    assert os.path.isfile(os.path.join(scratch_out, "portal-login.html"))


def test_openapi_covers_full_route_table(app, client):
    """G4: the OpenAPI export (from the real app) contains every in-schema
    endpoint. The expected set is derived from the app's own route table,
    so a new route that forgets its schema entry fails here."""
    from fastapi.routing import APIRoute

    spec_paths = client.get("/openapi.json").json()["paths"]
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.include_in_schema:
            # Deliberate: the POST session-revoke alias documents itself as
            # "not in the inventory".
            continue
        for method in route.methods:
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            ops = spec_paths.get(route.path, {})
            if method.lower() not in ops:
                missing.append(f"{method} {route.path}")
    assert not missing, "routes missing from OpenAPI export:\n" + "\n".join(missing)


def test_error_code_catalog_complete():
    """G3: every error code raised anywhere in app/ is registered in
    ERROR_CATALOG (status + default message). An unregistered code would
    surface to clients as an untyped 500."""
    import re

    from app import errors as errors_lib

    raised = set()
    for root, _dirs, files in os.walk(os.path.join(BACKEND_DIR, "app")):
        for name in files:
            if not name.endswith(".py"):
                continue
            text = open(os.path.join(root, name), encoding="utf-8").read()
            raised.update(re.findall(r'ApiError\(\s*"([a-z_0-9]+)"', text))
    assert raised, "no ApiError codes found — the regex drifted"
    missing = [c for c in sorted(raised) if c not in errors_lib.ERROR_CATALOG]
    assert not missing, "error codes missing from ERROR_CATALOG:\n" + "\n".join(missing)
    # Every catalog entry carries a status and a default message.
    for code, entry in errors_lib.ERROR_CATALOG.items():
        assert isinstance(entry[0], int) and entry[1], f"malformed catalog entry: {code}"
