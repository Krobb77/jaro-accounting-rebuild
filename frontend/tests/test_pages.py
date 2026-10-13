"""Phase-1 frontend tests: all static pages for the Jaro Accounting rebuild.

Maps 1:1 to IMPLEMENTATION_PLAN.md Phase-1 DONE criteria:
  1. pages render with shared chrome (+ branded 404)
  2. every nav/footer link resolves; no bare-homepage social links
  3. copy audit (hero typo fixed, prices verbatim, consulting copy real,
     bio figures never quoted)
  4. images (alt/srcset/aspect-ratio/lazy, hero eager, headline is HTML,
     onerror hides broken images)
  5. contrast recomputed in code (hero scrim >= 4.5:1; AA grays; links underlined)
  6. a11y (focus-visible, reduced-motion, 44px targets, sr-only, aria-hidden)
  7. SEO (OG/canonical/JSON-LD valid, sitemap complete, robots.txt)
  8. header/footer contents (utility lane, pinned CTA, trust strip)
  9. mobile nav / skip-link / no-overflow — static assertions in this file
     (nav-toggle markup, skip-link-first-child, overflow-x: clip) PLUS a live
     Chromium smoke run (see DECISIONS-frontend.md §6): nav toggles open/closed
     at 360px, skip link moves focus to #main, no horizontal overflow on
     7 pages at 360px, desktop hero/CTA render.
 10. no #007AB7 in UI; single hover token #B41E1C (#A91D1B absent)
     (test_build.py also greps dist/ for both banned colors)

Run: /home/hatch/workspace/jaroaccounting-rebuild/.venv/bin/python -m pytest frontend/tests/
"""

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

FRONTEND = Path(__file__).resolve().parents[1]
DIST = FRONTEND / "dist"
SRC = FRONTEND / "src"
BUILD = FRONTEND / "build.py"

sys.path.insert(0, str(FRONTEND))
import build as build_mod  # noqa: E402

# DONE 1 — the full Phase-1 page set (7 spec pages minus the deleted blog
# placeholder, plus the 5 new trust pages), and the branded 404.
EXPECTED_PAGES = [
    "index.html",
    "about-us.html",
    "our-services.html",
    "contact-us.html",
    "free-30-min-consultation.html",
    "thank-you-for-your-booking.html",
    "privacy.html",
    "terms.html",
    "legal.html",
    "engagement-terms.html",
    "faq.html",
    "404.html",
]

# Internal hrefs that legitimately have no dist/ target yet: the Phase-5
# portal shells (same-domain, per the canonical reconciliations).
FUTURE_ROUTES = ("/portal/login", "/portal/accept")

BARE_SOCIAL_HOMEPAGES = {
    "https://facebook.com", "https://www.facebook.com",
    "https://twitter.com", "https://www.twitter.com",
    "https://instagram.com", "https://www.instagram.com",
    "https://youtube.com", "https://www.youtube.com",
}


@pytest.fixture(scope="module")
def built():
    result = subprocess.run(
        [sys.executable, str(BUILD)], cwd=FRONTEND,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    return DIST


def _page(dist: Path, name: str) -> str:
    return (dist / name).read_text(encoding="utf-8")


def _css() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((DIST / "css").glob("*.css"))
    )


# ---------------------------------------------------------------------------
# DONE 1 — pages render with the shared chrome; branded 404
# ---------------------------------------------------------------------------

def test_all_phase1_pages_exist(built):
    for name in EXPECTED_PAGES:
        assert (built / name).is_file(), f"missing page: {name}"


def test_blog_placeholder_is_gone(built):
    assert not (built / "blog.html").exists()
    assert "blog/" not in (built / "sitemap.xml").read_text()


def test_every_page_has_chrome(built):
    for name in EXPECTED_PAGES:
        text = _page(built, name)
        assert 'class="skip-link"' in text and "Skip to content" in text, name
        assert 'class="site-header"' in text, name
        assert 'class="site-footer"' in text, name
        assert text.count('id="main"') == 1, name
        assert "<main" in text and "</main>" in text, name


def test_404_is_branded(built):
    text = _page(built, "404.html")
    assert "We can&rsquo;t find that page" in text or "We can't find that page" in text
    assert 'href="/"' in text
    assert 'href="/our-services/"' in text
    assert 'href="/contact-us/"' in text
    assert "1-(778)-227-4891" in text


@pytest.mark.parametrize("page,href,label", [
    ("about-us.html", "/about-us/", "About Us"),
    ("our-services.html", "/our-services/", "Our Services"),
    ("contact-us.html", "/contact-us/", "Contact Us"),
    ("free-30-min-consultation.html", "/free-30-min-consultation/", "FREE 30 Min Consultation!"),
    ("index.html", "/", "Home"),
])
def test_nav_marks_current_page(built, page, href, label):
    text = _page(built, page)
    assert f'<a href="{href}" aria-current="page">{label}</a>' in text, page


# ---------------------------------------------------------------------------
# DONE 2 — every nav/footer link resolves; no bare-homepage social links
# ---------------------------------------------------------------------------

def _internal_hrefs(text: str) -> list[str]:
    hrefs = re.findall(r'href="(/[^"]*)"', text)
    # Asset links (css/js/images) are not pages; the build already
    # fail-closes on missing assets.
    return [h for h in hrefs
            if not h.startswith(("/assets/", "/css/", "/js/"))]


def _resolve(dist: Path, href: str) -> bool:
    if href.startswith(FUTURE_ROUTES):
        return True
    if href == "/":
        return (dist / "index.html").is_file()
    name = href.strip("/").split("?")[0] + ".html"
    return (dist / name).is_file()


def test_all_internal_links_resolve(built):
    dead = []
    for name in EXPECTED_PAGES:
        for href in _internal_hrefs(_page(built, name)):
            if not _resolve(built, href):
                dead.append(f"{name}: {href}")
    assert not dead, "dead internal links:\n" + "\n".join(dead)


def test_no_bare_homepage_social_links(built):
    offenders = []
    for name in EXPECTED_PAGES:
        text = _page(built, name)
        for href in re.findall(r'href="(https?://[^"]*)"', text):
            canon = href.rstrip("/")
            if canon in BARE_SOCIAL_HOMEPAGES:
                offenders.append(f"{name}: {href}")
        for label in ("Facebook", "Twitter", "Instagram", "YouTube"):
            if f'aria-label="{label}"' in text:
                offenders.append(f"{name}: social icon {label}")
    assert not offenders, "bare social homepage links/icons:\n" + "\n".join(offenders)


def test_footer_trust_strip_links(built):
    text = _page(built, "index.html")
    for href in ("/privacy/", "/terms/", "/legal/"):
        assert f'href="{href}"' in text, href
        assert _resolve(built, href), href


def test_whatsapp_link_is_verbatim(built):
    text = _page(built, "contact-us.html")
    assert "api.whatsapp.com/send?phone=17782274891" in text


# ---------------------------------------------------------------------------
# DONE 3 — copy audit
# ---------------------------------------------------------------------------

def test_hero_typo_is_fixed(built):
    text = _page(built, "index.html")
    assert "Focus on your business, let us focus on it." in text
    assert "focus in it" not in text  # the original's typo, deliberately fixed


def test_prices_are_verbatim(built):
    text = _page(built, "our-services.html")
    for price in ("$149.99", "$249.99", "$299.99",
                  "$99.99", "$999.99", "$499.99", "$349.99"):
        assert price in text, price


def test_consulting_services_has_real_body_copy(built):
    text = _page(built, "our-services.html")
    m = re.search(r'id="consulting-heading".*', text, re.DOTALL)
    assert m
    section = m.group(0)
    assert "<p>" in section
    for keyword in ("incorporation", "holding company", "pay myself"):
        assert keyword in section, keyword


def test_bio_figures_are_facts_not_quotes(built):
    text = _page(built, "about-us.html")
    assert "nearly a decade" in text
    assert "300" in text
    assert "<q>" not in text
    assert "\u201c300" not in text and '"300' not in text  # never presented as a quote


def test_pricing_honesty_note_exists(built):
    text = _page(built, "our-services.html")
    assert "not included" in text.lower()
    assert "quoted in writing" in text


def test_tax_savings_qualifier_exists(built):
    text = _page(built, "our-services.html")
    assert "starting at" in text.lower() or "starting points" in text.lower()


def test_legal_page_makes_no_credential_claims(built):
    text = _page(built, "legal.html")
    assert "1238855 BC LTD" in text
    assert "215 Mowat Street" in text
    # The credential section is explicitly marked pending owner confirmation.
    assert "pending owner confirmation" in text
    assert "Not yet published" in text
    assert "Chartered Professional Accountant" not in text


def test_testimonials_ship_empty_and_hidden(built):
    data = json.loads((SRC / "data" / "testimonials.json").read_text())
    assert data == []
    # Zero entries -> the [[testimonials]] directive renders nothing.
    assert 'class="testimonials"' not in _page(built, "index.html")


def test_contact_pii_microcopy_and_crosslink(built):
    text = _page(built, "contact-us.html")
    assert "SINs" in text
    assert "bank account numbers" in text
    assert "Book a free consultation" in text


def test_contact_form_shell(built):
    text = _page(built, "contact-us.html")
    for field in ('name="name"', 'name="email"', 'name="message"'):
        assert field in text, field
    assert text.count("required") >= 3
    assert 'type="submit"' in text


def test_hours_table(built):
    text = _page(built, "contact-us.html")
    assert "09:00" in text and "17:00" in text
    assert "10:00" in text and "15:00" in text
    assert "Closed" in text
    assert 'class="hours-table"' in text


def test_consultation_shell_microcopy(built):
    text = _page(built, "free-30-min-consultation.html")
    assert "not tax advice" in text
    assert "general information" in text


def test_thank_you_honest_vocabulary(built):
    text = _page(built, "thank-you-for-your-booking.html")
    assert "queued" in text
    assert "JARO-2026-XXXXXX" in text
    # One link set (the original duplicated every link).
    assert text.count("Back to Home Page") == 1
    assert text.count("Back to Our Services") == 1


def test_faq_deep_links_and_categories(built):
    text = _page(built, "faq.html")
    for anchor in ("#hours-contact", "#services-pricing", "#booking", "#about-firm"):
        assert f'href="{anchor}"' in text, anchor
    for qid in ("faq-hours-1", "faq-pricing-2", "faq-pricing-3",
                "faq-booking-1", "faq-booking-2", "faq-about-1"):
        assert f'id="{qid}"' in text, qid


# ---------------------------------------------------------------------------
# DONE 4 — images
# ---------------------------------------------------------------------------

def test_every_img_has_srcset_aspect_loading(built):
    for name in EXPECTED_PAGES:
        for m in re.finditer(r"<img\b[^>]*>", _page(built, name)):
            tag = m.group(0)
            assert 'alt="' in tag and re.search(r'alt="([^"]+)"', tag).group(1).strip(), (name, tag)
            assert 'srcset="' in tag, (name, tag)
            assert "aspect-ratio" in tag, (name, tag)
            assert 'loading="' in tag, (name, tag)
            assert 'onerror=' in tag, (name, tag)  # broken images hide themselves


def test_hero_eager_others_lazy(built):
    text = _page(built, "index.html")
    hero = re.search(r'<img\b[^>]*src="/assets/hero\.webp"[^>]*>', text)
    assert hero and 'loading="eager"' in hero.group(0) and 'fetchpriority="high"' in hero.group(0)
    for m in re.finditer(r'<img\b[^>]*src="/assets/(?!hero\.webp)[^"]*"[^>]*>', text):
        assert 'loading="lazy"' in m.group(0), m.group(0)


def test_hero_headline_is_html_not_baked_in(built):
    text = _page(built, "index.html")
    assert "<h1>JARO ACCOUNTING</h1>" in text
    hero = re.search(r'<img\b[^>]*src="/assets/hero\.webp"[^>]*>', text)
    assert "JARO ACCOUNTING" not in hero.group(0)


# ---------------------------------------------------------------------------
# DONE 5 — contrast recomputed in code
# ---------------------------------------------------------------------------

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_to_linear(v / 255) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_math_sanity():
    white, red, gray = _luminance((255, 255, 255)), _luminance((205, 34, 32)), _luminance((118, 118, 118))
    assert abs(contrast(white, red) - 5.46) < 0.05   # #CD2220 on white
    assert abs(contrast(white, gray) - 4.54) < 0.05  # #767676 on white (A11 fix)


def _gradient_layers(block: str) -> list[str]:
    """Split a `background:` value into its top-level gradient layers.

    Uses the LAST background declaration in the block — the sculpted
    gradients override the flat-overlay fallback for color-mix engines.
    """
    decls = [m.start() for m in re.finditer(r"background\s*:", block)]
    assert decls, "no background declaration found"
    seg_start = block.find(":", decls[-1]) + 1
    layers, depth = [], 0
    for j in range(seg_start, len(block)):
        ch = block[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            layers.append(block[seg_start:j])
            seg_start = j + 1
        elif ch == ";" and depth == 0:
            layers.append(block[seg_start:j])
            break
    return [l.strip() for l in layers if l.strip()]


def _stop_list(layer: str) -> list[tuple[float, float]]:
    """[(mix_alpha, position)] for a gradient layer; transparent stops -> 0."""
    stops = []
    for m in re.finditer(
            r"(?:color-mix\(in srgb, var\(--color-foreground-dark\)\s+(\d+)%, transparent\)|transparent)\s+(\d+)%",
            layer):
        mix = int(m.group(1)) / 100 if m.group(1) else 0.0
        stops.append((mix, int(m.group(2))))
    return stops


def _layer_alpha_at_center(layer: str) -> float:
    """Effective #101010 alpha of one gradient layer at the text center."""
    stops = _stop_list(layer)
    assert stops, f"no stops parsed in layer: {layer[:60]}"
    if layer.lstrip().startswith("radial-gradient"):
        # The text sits at the radial's center (50% 44%); gradients extend
        # the first stop's color backward, so position 0 sees the first stop.
        return stops[0][0]
    # linear-gradient(180deg): interpolate the stops at y = 44%.
    pts = sorted(stops, key=lambda s: s[1])
    y = 44
    if y <= pts[0][1]:
        return pts[0][0]
    for (m0, p0), (m1, p1) in zip(pts, pts[1:]):
        if p0 <= y <= p1:
            t = (y - p0) / (p1 - p0) if p1 != p0 else 0
            return m0 + t * (m1 - m0)
    return pts[-1][0]


def _scrim_alphas(css_text: str) -> tuple[float, float]:
    """Effective central scrim alpha for (desktop, mobile), parsed from CSS.

    The sculpted scrim stacks translucent #101010 layers; the alpha behind the
    centered text is 1 - Π(1 - a_i) over the layers' center values. Mobile
    uses the strengthened stops inside the max-width: 640px override.
    """
    desktop_m = re.search(r"\.hero::before \{(.*?)\n  \}", css_text, re.DOTALL)
    assert desktop_m, ".hero::before rule missing"
    mobile_m = re.search(
        r"@media \(max-width: 640px\) \{.*?\.hero::before \{(.*?)\n    \}",
        css_text, re.DOTALL)
    assert mobile_m, "mobile .hero::before override missing"

    def composite(block: str) -> float:
        total = 1.0
        for layer in _gradient_layers(block):
            total *= (1 - _layer_alpha_at_center(layer))
        return 1 - total

    return composite(desktop_m.group(1)), composite(mobile_m.group(1))


def test_hero_scrim_matches_corrected_design():
    desktop, mobile = _scrim_alphas(_css())
    print(f"\nparsed scrim alpha — desktop: {desktop:.2%}, mobile: {mobile:.2%}")
    assert desktop >= 0.55, "documented minimum is ≥55% scrim behind text"
    assert mobile > desktop, "mobile scrim must be the strengthened variant"


# The H2 text-shadow halo, evaluated at the glyph edge (where WCAG measures):
#   0 1px 14px rgb(16 16 16 / 0.55)  +  0 2px 3px rgb(16 16 16 / 0.60)
# Combined peak alpha at the edge of a letterform:
HALO_ALPHA = 1 - (1 - 0.55) * (1 - 0.60)

# hero.webp is 2384x1008; object-position: 50% 35%.
_IMG_W, _IMG_H = 2384, 1008


def _brightest_in_text_region(vp_w: int, vp_h: int,
                              text_rect: tuple[float, float, float, float]) -> float:
    """Brightest pixel luminance inside the headline's rect, cover-crop aware.

    text_rect is (x0, y0, x1, y1) as viewport fractions. Maps through the
    object-fit: cover transform (with object-position 50% 35%) back to image
    pixels, then returns the max luminance found there.
    """
    scale = max(vp_w / _IMG_W, vp_h / _IMG_H)
    dw, dh = _IMG_W * scale, _IMG_H * scale
    ox, oy = (dw - vp_w) * 0.5, (dh - vp_h) * 0.35
    img = Image.open(SRC / "assets" / "hero.webp").convert("RGB")
    x0 = int((text_rect[0] * vp_w + ox) / scale)
    y0 = int((text_rect[1] * vp_h + oy) / scale)
    x1 = int((text_rect[2] * vp_w + ox) / scale)
    y1 = int((text_rect[3] * vp_h + oy) / scale)
    region = img.crop((max(0, x0), max(0, y0), min(_IMG_W, x1), min(_IMG_H, y1)))
    return max(_luminance(px) for px in list(region.get_flattened_data())[::11])


def _hero_text_contrast(scrim_alpha: float, vp: tuple[int, int],
                        text_rect: tuple[float, float, float, float]) -> tuple[float, float]:
    """(scrim-only contrast, full-stack contrast) for white headline text.

    Full stack = image -> sculpted scrim -> text-shadow halo (at glyph edge).
    Returns both so the record shows what the halo contributes.
    """
    brightest = _brightest_in_text_region(*vp, text_rect)
    scrim_lum = _luminance((16, 16, 16))  # #101010
    comp = lambda a: a * scrim_lum + (1 - a) * brightest  # noqa: E731
    scrim_only = contrast(1.0, comp(scrim_alpha))
    total = 1 - (1 - scrim_alpha) * (1 - HALO_ALPHA)
    full = contrast(1.0, comp(total))
    return scrim_only, full


def test_hero_contrast_desktop():
    scrim, _ = _scrim_alphas(_css())
    scrim_only, full = _hero_text_contrast(scrim, (1600, 900), (0.25, 0.30, 0.75, 0.46))
    print(f"\ndesktop: scrim-only {scrim_only:.2f}:1, with halo {full:.2f}:1")
    assert full >= 4.5, f"desktop hero text contrast {full:.2f}:1 < 4.5:1"


def test_hero_contrast_mobile():
    _, mobile_scrim = _scrim_alphas(_css())
    scrim_only, full = _hero_text_contrast(mobile_scrim, (390, 844), (0.0, 0.28, 1.0, 0.50))
    print(f"\nmobile: scrim-only {scrim_only:.2f}:1, with halo {full:.2f}:1")
    assert full >= 4.5, f"mobile hero text contrast {full:.2f}:1 < 4.5:1"


def test_muted_text_uses_aa_gray(built):
    assert "--color-neutral-500-text: #767676" in _css()


def test_links_always_underlined(built):
    css_text = _css()
    m = re.search(r"\.site-main a:not\(\.btn\)\s*\{([^}]*)\}", css_text)
    assert m, "in-content link rule missing"
    assert "text-decoration: underline" in m.group(1)


# ---------------------------------------------------------------------------
# DONE 6 — a11y
# ---------------------------------------------------------------------------

def test_focus_visible_ring(built):
    assert ":focus-visible" in _css()
    assert "outline" in _css()


def test_reduced_motion(built):
    assert "prefers-reduced-motion: reduce" in _css()


def test_44px_targets(built):
    css_text = _css()
    for selector in (".btn", ".learn-more", ".nav-toggle-label"):
        m = re.search(r"(?m)^  " + re.escape(selector) + r" \{(.*?)\n  \}",
                      css_text, re.DOTALL)
        assert m and "min-height: 44px" in m.group(1), selector


def test_sr_only_utility(built):
    assert ".sr-only" in _css()


def test_decorative_wrappers_hidden(built):
    text = _page(built, "index.html")
    # SVG icons and separators are decorative: hidden from assistive tech.
    assert text.count('aria-hidden="true"') >= 3


# ---------------------------------------------------------------------------
# DONE 7 — SEO
# ---------------------------------------------------------------------------

def test_og_tags_and_canonical_on_every_page(built):
    for name in EXPECTED_PAGES:
        text = _page(built, name)
        for tag in ("og:title", "og:description", "og:image", "og:url",
                    'rel="canonical"', "twitter:card"):
            assert tag in text, (name, tag)


def test_jsonld_localbusiness_valid(built):
    for name in EXPECTED_PAGES:
        text = _page(built, name)
        blocks = re.findall(
            r'<script type="application/ld\+json">\n(.*?)\n</script>', text, re.DOTALL)
        assert blocks, name
        found = False
        for raw in blocks:
            data = json.loads(raw)
            if data.get("@type") == "AccountingService":
                found = True
                for field in ("name", "telephone", "address",
                              "openingHoursSpecification", "url"):
                    assert field in data, (name, field)
        assert found, f"{name}: no AccountingService JSON-LD"


def test_jsonld_faqpage_valid(built):
    text = _page(built, "faq.html")
    blocks = re.findall(
        r'<script type="application/ld\+json">\n(.*?)\n</script>', text, re.DOTALL)
    faq = [json.loads(raw) for raw in blocks
           if json.loads(raw).get("@type") == "FAQPage"]
    assert faq, "no FAQPage JSON-LD on /faq/"
    entities = faq[0]["mainEntity"]
    assert len(entities) >= 10
    for e in entities:
        assert e["name"] and e["acceptedAnswer"]["text"]
    # Every visible question is represented in the structured data.
    for qid in re.findall(r'<details class="faq-item" id="([^"]+)"', text):
        summary = re.search(rf'id="{qid}"[^>]*>\s*<summary>(.*?)</summary>', text, re.DOTALL)
        assert summary and summary.group(1).strip() in [e["name"] for e in entities], qid


def test_sitemap_complete(built):
    root = ET.parse(built / "sitemap.xml").getroot()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {u.find("s:loc", ns).text for u in root.findall("s:url", ns)}
    expected = {
        "https://jaroaccounting.com/",
        "https://jaroaccounting.com/about-us/",
        "https://jaroaccounting.com/our-services/",
        "https://jaroaccounting.com/contact-us/",
        "https://jaroaccounting.com/free-30-min-consultation/",
        "https://jaroaccounting.com/thank-you-for-your-booking/",
        "https://jaroaccounting.com/privacy/",
        "https://jaroaccounting.com/terms/",
        "https://jaroaccounting.com/legal/",
        "https://jaroaccounting.com/engagement-terms/",
        "https://jaroaccounting.com/faq/",
    }
    assert expected <= locs, f"missing from sitemap: {expected - locs}"
    assert not any("/blog" in loc for loc in locs)


def test_robots_txt(built):
    text = (built / "robots.txt").read_text()
    assert "Sitemap: https://jaroaccounting.com/sitemap.xml" in text


# ---------------------------------------------------------------------------
# DONE 8 — header/footer contents
# ---------------------------------------------------------------------------

def test_utility_lane(built):
    text = _page(built, "index.html")
    assert "Client Login" in text
    assert 'href="/portal/login"' in text
    assert "<svg" in text  # lock icon
    assert "1-(778)-227-4891" in text
    assert "Mon" in text and "Fri" in text  # hours snippet


def test_booking_cta_pinned_in_nav(built):
    text = _page(built, "index.html")
    assert "Book FREE 30 Min Consultation" in text
    assert 'href="/free-30-min-consultation/"' in text


def test_footer_client_login_and_trust_strip(built):
    text = _page(built, "index.html")
    assert text.count('href="/portal/login"') >= 2  # utility lane + footer
    for href in ("/privacy/", "/terms/", "/legal/"):
        assert f'href="{href}"' in text


# ---------------------------------------------------------------------------
# DONE 9 — mobile nav / skip-link / no-overflow (STATIC assertions;
#          Playwright browser check deferred — see DECISIONS-frontend.md)
# ---------------------------------------------------------------------------

def test_mobile_nav_toggle_markup(built):
    text = _page(built, "index.html")
    assert 'id="nav-toggle"' in text
    assert 'aria-label="Toggle navigation menu"' in text
    assert "Menu" in text


def test_skip_link_targets_main(built):
    for name in EXPECTED_PAGES:
        text = _page(built, name)
        assert '<a class="skip-link" href="#main">Skip to content</a>' in text, name
        assert text.index('class="skip-link"') < text.index('class="site-header"'), name
        # tabindex="-1" lets the skip link MOVE focus (not just scroll).
        assert '<main id="main" tabindex="-1"' in text, name


def test_no_horizontal_overflow_rule(built):
    assert "overflow-x: clip" in _css()


# ---------------------------------------------------------------------------
# DONE 10 — color discipline: no #007AB7; single hover #B41E1C
# ---------------------------------------------------------------------------

def test_single_hover_token_and_no_blue(built):
    css_text = _css().lower()
    assert "#b41e1c" in css_text
    assert "#a91d1b" not in css_text
    assert "#007ab7" not in css_text


def test_build_helpers_still_fail_closed():
    with pytest.raises(ValueError):
        build_mod.img_tag("hero.webp", "")
    with pytest.raises(FileNotFoundError):
        build_mod.img_tag("nope.webp", "alt")
    assert build_mod.load_testimonials() == []
    with pytest.raises(FileNotFoundError):  # missing JSON-LD also fails the build
        build_mod.load_jsonld("does-not-exist.json")
