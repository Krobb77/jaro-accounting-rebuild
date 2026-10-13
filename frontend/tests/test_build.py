"""Phase-0 frontend tests: the build script assembles dist/ correctly.

Run from the repo venv:  /home/hatch/workspace/jaroaccounting-rebuild/.venv/bin/python -m pytest frontend/tests/
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1]
DIST = FRONTEND / "dist"
BUILD = FRONTEND / "build.py"

sys.path.insert(0, str(FRONTEND))
import build as build_mod  # noqa: E402  (build.py, the stdlib-only builder)

EXPECTED_IMAGES = {
    "hero.webp",
    "service-tax.webp",
    "service-bookkeeping.webp",
    "service-consulting.webp",
}

BANNED_COLORS = ("#a91d1b", "#007ab7")


def run_build() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BUILD)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_build_runs_clean():
    result = run_build()
    assert result.returncode == 0, f"build.py failed:\n{result.stdout}\n{result.stderr}"
    assert "build OK" in result.stdout


def test_dist_contains_expected_pages():
    run_build()
    assert (DIST / "index.html").is_file()
    assert (DIST / "404.html").is_file()


def _dist_html_files() -> list[Path]:
    return sorted(DIST.glob("*.html"))


def test_every_page_has_chrome():
    """Every dist HTML file carries the skip link, header, footer, and main landmark."""
    run_build()
    pages = _dist_html_files()
    assert pages, "no HTML files emitted"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert 'class="skip-link"' in text and "Skip to content" in text, page.name
        assert 'class="site-header"' in text, page.name
        assert 'class="site-footer"' in text, page.name
        assert 'id="main"' in text, page.name
        assert "<main" in text and "</main>" in text, page.name


def test_no_banned_colors_in_dist():
    """#A91D1B and #007AB7 must not appear anywhere in dist CSS/HTML (case-insensitive)."""
    run_build()
    offenders = []
    for path in list(DIST.rglob("*.css")) + _dist_html_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for banned in BANNED_COLORS:
            if banned in lowered:
                offenders.append(f"{path.relative_to(DIST)}: {banned}")
    assert not offenders, "banned colors found:\n" + "\n".join(offenders)


def test_all_images_present_in_dist_assets():
    run_build()
    assets = DIST / "assets"
    assert assets.is_dir()
    present = {p.name for p in assets.iterdir() if p.is_file()}
    assert EXPECTED_IMAGES <= present, f"missing: {EXPECTED_IMAGES - present}"


def test_every_dist_image_has_nonempty_alt():
    run_build()
    for page in _dist_html_files():
        for m in re.finditer(r"<img\b[^>]*>", page.read_text(encoding="utf-8")):
            alt = re.search(r'alt="([^"]*)"', m.group(0))
            assert alt is not None and alt.group(1).strip(), f"{page.name}: {m.group(0)}"


def test_hero_image_is_eager_others_lazy():
    run_build()
    text = (DIST / "index.html").read_text(encoding="utf-8")
    hero = re.search(r'<img\b[^>]*src="/assets/hero\.webp"[^>]*>', text)
    assert hero and 'loading="eager"' in hero.group(0)
    for name in ("service-tax.webp", "service-bookkeeping.webp", "service-consulting.webp"):
        tag = re.search(rf'<img\b[^>]*src="/assets/{re.escape(name)}"[^>]*>', text)
        assert tag and 'loading="lazy"' in tag.group(0), name


def test_img_helper_rejects_empty_alt():
    with pytest.raises(ValueError):
        build_mod.img_tag("hero.webp", "")
    with pytest.raises(ValueError):
        build_mod.img_tag("hero.webp", "   ")


def test_img_helper_rejects_missing_asset():
    with pytest.raises(FileNotFoundError):
        build_mod.img_tag("does-not-exist.webp", "Some alt text")


def test_nav_active_state_marks_current_page():
    run_build()
    text = (DIST / "index.html").read_text(encoding="utf-8")
    assert '<a href="/" aria-current="page">Home</a>' in text
    text404 = (DIST / "404.html").read_text(encoding="utf-8")
    assert "aria-current" not in text404  # 404 marks no nav item current


def test_dist_is_generated_not_handwritten():
    """dist/ must be fully reproducible: wipe it, rebuild, byte-identical."""
    run_build()
    before = {p.relative_to(DIST): p.read_bytes() for p in DIST.rglob("*") if p.is_file()}
    subprocess.run([sys.executable, str(BUILD), "--clean"], cwd=FRONTEND, check=True,
                   capture_output=True, timeout=60)
    after = {p.relative_to(DIST): p.read_bytes() for p in DIST.rglob("*") if p.is_file()}
    assert before.keys() == after.keys()
    assert all(before[k] == after[k] for k in before)
