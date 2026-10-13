"""DONE 12 — packaging: no dev-only packages, junk-class scan clean."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DEV_PACKAGES = {
    "pytest", "pytest-asyncio", "pytest-cov", "coverage", "tox", "nox",
    "mypy", "ruff", "black", "flake8", "pylint", "ipython", "jupyter",
    "debugpy", "playwright", "selenium",
}

JUNK_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache"}
JUNK_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".egg")


def test_requirements_pinned_and_runtime_only():
    lines = [
        line.strip()
        for line in open(os.path.join(BACKEND_DIR, "requirements.txt"), encoding="utf-8")
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements.txt is empty"
    for line in lines:
        assert "==" in line, f"unpinned requirement: {line}"
        name = line.split("==")[0].split("[")[0].strip().lower().replace("-", "_")
        assert name not in DEV_PACKAGES, f"dev-only package in requirements.txt: {line}"


def test_junk_class_scan_clean():
    hits = []
    for root, dirs, files in os.walk(BACKEND_DIR):
        # Never descend into these (build/test artifacts, not shipped code).
        dirs[:] = [d for d in dirs if d not in JUNK_NAMES and not d.endswith(".egg-info")]
        for name in files:
            if name in JUNK_NAMES or name.endswith(JUNK_SUFFIXES):
                hits.append(os.path.join(root, name))
    assert not hits, f"junk files in backend/:\n" + "\n".join(hits)
