"""Base-path build tests: subpath serving must never corrupt URLs.

Regression coverage for two separate image outages caused by regex-based
post-processing of built files (one left later srcset candidates
root-relative -> 404; one leaked a literal "\\1" into srcset attributes).
The build now rewrites URLs with a real HTML parser / CSS scanner inside
build.py (--base-path), and JS resolves the base path at runtime via
site-base.js. These tests pin that behavior.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build.py"
BASE = "/jaro-accounting-rebuild"


@pytest.fixture(scope="module")
def subpath_build(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist-subpath")
    r = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out), "--base-path", BASE],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    return out


@pytest.fixture(scope="module")
def root_build(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist-root")
    r = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    return out


def _pages(out):
    return sorted(out.rglob("index.html"))


def test_nested_structure(subpath_build):
    pages = _pages(subpath_build)
    # 19 nested index.html + 404.html stays flat (file path, not a trailing-slash path)
    assert len(pages) == 19, f"expected 19 nested pages, got {len(pages)}"
    assert (subpath_build / "404.html").is_file()
    assert (subpath_build / "portal" / "login" / "index.html").is_file()
    assert (subpath_build / ".nojekyll").is_file()


def test_no_literal_backslash_one(subpath_build):
    """The \\1-leak regression: replacement strings must never leak."""
    bad = [p for p in subpath_build.rglob("*.html") if "\\1" in p.read_text(encoding="utf-8")]
    assert not bad, f"literal \\1 in: {[str(p) for p in bad]}"


def test_every_srcset_candidate_prefixed(subpath_build):
    """Every URL inside every srcset must carry the base path."""
    for page in _pages(subpath_build):
        text = page.read_text(encoding="utf-8")
        for m in re.finditer(r'srcset="([^"]*)"', text):
            for candidate in m.group(1).split(","):
                url = candidate.strip().split(None, 1)[0]
                assert url.startswith(BASE + "/"), (
                    f"{page.name}: unprefixed srcset URL {url!r}"
                )


def test_no_stray_root_absolute_internal_urls(subpath_build):
    """No root-absolute /assets, /css, /js, /portal href/src may survive."""
    for page in _pages(subpath_build):
        text = page.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:href|src|action|poster)="([^"]*)"', text):
            url = m.group(1)
            if url.startswith("/") and not url.startswith("//") and not url.startswith("/api/"):
                assert url.startswith(BASE + "/"), (
                    f"{page.relative_to(subpath_build)}: stray {url!r}"
                )


def test_api_urls_untouched(subpath_build):
    """The stub intercepts /api/* at runtime; the build must not rewrite it."""
    stub = (subpath_build / "js" / "stub-backend.js").read_text(encoding="utf-8")
    assert "/api/v1/" in stub
    assert BASE + "/api" not in stub


def test_js_uses_runtime_base(subpath_build):
    """App JS must resolve the base path at runtime, not build time."""
    assert (subpath_build / "js" / "site-base.js").is_file()
    api_js = (subpath_build / "js" / "portal-api.js").read_text(encoding="utf-8")
    assert "withBase(" in api_js
    assert BASE + "/portal" not in api_js  # not hardcoded


def test_css_urls_prefixed(subpath_build, root_build):
    """CSS url() references are rewritten for subpath, untouched for root."""
    for css_file in (subpath_build / "css").rglob("*.css"):
        text = css_file.read_text(encoding="utf-8")
        for m in re.finditer(r"url\(([^)]*)\)", text):
            url = m.group(1).strip().strip("\"'")
            if url.startswith("/") and not url.startswith("//"):
                assert url.startswith(BASE + "/"), f"{css_file.name}: stray {url!r}"
    # Root build must not be prefixed.
    for css_file in (root_build / "css").rglob("*.css"):
        text = css_file.read_text(encoding="utf-8")
        assert BASE + "/" not in text


def test_root_build_unprefixed(root_build):
    """Default build serves from domain root: URLs stay root-absolute."""
    pages = list(root_build.glob("*.html"))
    assert len(pages) == 20
    index = (root_build / "index.html").read_text(encoding="utf-8")
    assert 'src="/assets/hero.webp"' in index
    assert BASE + "/" not in index
