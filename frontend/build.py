#!/usr/bin/env python3
"""Assemble the Jaro Accounting static frontend.

Stdlib-only. This script is the source of truth: it reads
``frontend/src/`` (partials, pages, css, js, assets) and emits generated,
nginx-ready static HTML into ``frontend/dist/``. Never hand-edit dist/.

Usage:
    python build.py [--out dist] [--clean|--no-clean]

Page files (``src/pages/*.html``) carry a tiny frontmatter block::

    ---
    title: Page title
    description: Meta description
    path: /about-us/
    nav: about
    ---

Pages are assembled as: head + body-open (skip link) + header + page body
(which must contain exactly one ``<main id="main">``) + footer + foot.

Image guidance (build-enforced, not advisory):
    Every page image is authored with an ``[[img ...]]`` directive, e.g.::

        [[img name="hero.webp" alt="Bright office with plants" eager aspect="21/9"]]

    The builder expands it to an <img> with:
      - non-empty alt            (missing/blank alt FAILS the build)
      - loading="lazy"           (hero-style images pass ``eager`` instead,
                                  which also sets fetchpriority="high")
      - aspect-ratio             (prevents layout shift while loading)
      - srcset + sizes           (only when ``variants="800:a-800.webp,..."``
                                  is given; multi-width renditions are a
                                  Phase-1 job — single-source images ship
                                  without srcset)
    Referencing an asset that does not exist in src/assets/ FAILS the build.

SEO (Phase 1): head.html carries OG tags (title/description/url from
frontmatter, image = the matched-set hero) plus a {{jsonld}} slot. Every
page gets LocalBusiness JSON-LD from src/partials/jsonld/localbusiness.json;
frontmatter `jsonld: faq` appends extra blocks (src/partials/jsonld/*.json,
validated as JSON at build time). build() also emits sitemap.xml (every page
whose frontmatter doesn't say `sitemap: false`, minus /404.html) and
robots.txt.

Testimonials: a [[testimonials]] directive renders src/data/testimonials.json
as a component section; zero entries renders nothing (no fabricated
testimonials — the owner provides them later).

Site identity tokens: [[portal_name]] and [[event_channel]] are expanded by
build.py from the single PORTAL_NAME / EVENT_CHANNEL_LABEL constants at the
top of the script (owner-question decisions Q2/Q4, IMPLEMENTATION_PLAN §3.5).
Use them in page bodies and frontmatter descriptions instead of hardcoding
the portal brand name or the consultation channel wording; a rename is one
constant edit.

Page scripts (Phase 2+): frontmatter `scripts: contact.js` injects
`<script type="module" src="/js/contact.js"></script>` before </body>.
Comma-separated for several. The name must match ^[a-z0-9][a-z0-9._-]*\\.js$
and exist under src/js/ — otherwise the build FAILS (fail-closed, same as
images). Scripts are ES modules (deferred by default); no inline scripts.
"""

from __future__ import annotations

import argparse

from build_basepath import apply_base_path_to_css, apply_base_path_to_html
import html
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
PAGES_DIR = SRC / "pages"
PARTIALS_DIR = SRC / "partials"
JSONLD_DIR = PARTIALS_DIR / "jsonld"
DATA_DIR = SRC / "data"
CSS_DIR = SRC / "css"
JS_DIR = SRC / "js"
ASSETS_DIR = SRC / "assets"

SITE_ORIGIN = "https://jaroaccounting.com"

# ---------------------------------------------------------------------------
# Site identity tokens (owner-question decisions, IMPLEMENTATION_PLAN §3.5).
# Rename = one change here: the build injects each value wherever its
# [[token]] appears (page bodies AND frontmatter fields such as descriptions),
# so no template or email hardcodes the brand wording.
# ---------------------------------------------------------------------------

# Q2 (§3.5 #2, decided 2026-10-08): KEEP "myJaro" — consistent with the
# myMAWER pattern the invite model was based on.
PORTAL_NAME = "myJaro"

# Q4 (§3.5 #4 / D-32, decided 2026-10-08): keep "Phone Call" as the event-type
# channel wording ("Free 30-Min Consultation — Phone Call") — matches the
# original site's phone-centric contact and the Mawer-observed pattern.
# Jared can switch to "Video Call" later by editing this one constant.
EVENT_CHANNEL_LABEL = "Phone Call"

# ---------------------------------------------------------------------------
# Contact information — single source of truth (2026-10-15).
# All public contact details (phone, email, address, hours) live in
# src/data/site-contact.json. The build injects them wherever a
# [[contact.*]] token appears (page bodies, frontmatter, partials,
# JSON-LD, JS). To update the phone number / email / address site-wide,
# edit that ONE file and rebuild — never hardcode contact details.
# The street address is intentionally a placeholder until Jared provides
# the real one.
# ---------------------------------------------------------------------------

_CONTACT_PATH = DATA_DIR / "site-contact.json"


def _load_site_contact() -> dict[str, str]:
    try:
        raw = json.loads(_CONTACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"site-contact.json is missing or invalid: {exc}") from exc
    contact = {k: v for k, v in raw.items() if not k.startswith("_")}
    required = ("email", "phone_display", "phone_tel", "street", "city",
                "region", "postal", "country")
    missing = [k for k in required if not contact.get(k)]
    if missing:
        raise ValueError(f"site-contact.json missing required keys: {missing}")
    return contact


SITE_CONTACT = _load_site_contact()

_SITE_TOKENS = {
    "[[portal_name]]": PORTAL_NAME,
    "[[event_channel]]": EVENT_CHANNEL_LABEL,
    # Contact tokens — one edit in site-contact.json updates the whole site.
    "[[contact.email]]": SITE_CONTACT["email"],
    "[[contact.phone]]": SITE_CONTACT["phone_display"],
    "[[contact.phone_tel]]": SITE_CONTACT["phone_tel"],
    "[[contact.street]]": SITE_CONTACT["street"],
    "[[contact.city]]": SITE_CONTACT["city"],
    "[[contact.region]]": SITE_CONTACT["region"],
    "[[contact.postal]]": SITE_CONTACT["postal"],
    "[[contact.country]]": SITE_CONTACT["country"],
    "[[contact.website]]": SITE_CONTACT["website"],
    "[[contact.business_name]]": SITE_CONTACT["business_name"],
    "[[contact.legal_name]]": SITE_CONTACT["legal_name"],
    "[[contact.hours_short]]": SITE_CONTACT["hours_short"],
    "[[contact.hours_weekdays]]": SITE_CONTACT["hours_weekdays"],
    "[[contact.hours_saturday]]": SITE_CONTACT["hours_saturday"],
}


def expand_site_tokens(text: str) -> str:
    """Expand [[portal_name]] / [[event_channel]] site-identity tokens.

    Safe to run over frontmatter values (they are html.escape()d later in
    render_page) and over body HTML (the token is plain text in text
    position).
    """
    for token, value in _SITE_TOKENS.items():
        text = text.replace(token, value)
    return text

# ---------------------------------------------------------------------------
# Image helper: the build-enforced <img> contract
# ---------------------------------------------------------------------------


def img_tag(
    name: str,
    alt: str,
    *,
    eager: bool = False,
    aspect: str | None = None,
    variants: str | None = None,
    sizes: str = "100vw",
    cls: str | None = None,
) -> str:
    """Return an <img> tag that satisfies the project's image guidance.

    Raises ValueError on blank alt text (non-empty alt is required), and
    FileNotFoundError when the asset (or a variant) is missing from
    src/assets/.
    """
    if not alt or not alt.strip():
        raise ValueError(f'img "{name}": non-empty alt text is required')
    asset = ASSETS_DIR / name
    if not asset.is_file():
        raise FileNotFoundError(f'img "{name}": no such asset in src/assets/')

    attrs = [f'src="/assets/{html.escape(name, quote=True)}"']
    if variants:
        parts = []
        for chunk in variants.split(","):
            width, _, fname = chunk.strip().partition(":")
            width, fname = width.strip(), fname.strip()
            if not width or not fname:
                raise ValueError(f'img "{name}": bad variant spec "{chunk}"')
            if not (ASSETS_DIR / fname).is_file():
                raise FileNotFoundError(
                    f'img "{name}": variant "{fname}" missing from src/assets/'
                )
            parts.append(f"/assets/{html.escape(fname, quote=True)} {int(width)}w")
        attrs.append(f'srcset="{", ".join(parts)}"')
        attrs.append(f'sizes="{html.escape(sizes, quote=True)}"')
    attrs.append(f'alt="{html.escape(alt.strip(), quote=True)}"')
    attrs.append('loading="eager" fetchpriority="high"' if eager else 'loading="lazy" decoding="async"')
    # Phase-1 DONE #4: broken images hide themselves instead of showing a
    # broken-image icon. (If a strict CSP is ever added, move this to a
    # delegated error listener — inline handlers and CSP don't mix.)
    attrs.append('onerror="this.hidden=true"')
    if aspect:
        if not re.fullmatch(r"\d+\s*/\s*\d+", aspect):
            raise ValueError(f'img "{name}": aspect must look like "16/10", got "{aspect}"')
        attrs.append(f'style="aspect-ratio: {aspect}"')
    if cls:
        attrs.append(f'class="{html.escape(cls, quote=True)}"')
    return "<img " + " ".join(attrs) + ">"


_IMG_DIRECTIVE_RE = re.compile(r"\[\[img\s+([^\]]+)\]\]")


def _parse_kv(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r'(\w+)(?:=(".*?"|\S+))?', spec):
        key, val = m.group(1), m.group(2)
        if val is None:
            out[key] = "true"  # bare flag, e.g. [[img ... eager]]
            continue
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[key] = val
    return out


def expand_img_directives(body: str) -> str:
    """Replace every [[img ...]] directive with its build-enforced <img> tag."""

    def repl(m: re.Match) -> str:
        kv = _parse_kv(m.group(1))
        if "name" not in kv or "alt" not in kv:
            raise ValueError(f"img directive needs name= and alt=: {m.group(0)}")
        return img_tag(
            kv["name"],
            kv["alt"],
            eager=kv.get("eager", "false").lower() in ("1", "true", "yes"),
            aspect=kv.get("aspect"),
            variants=kv.get("variants"),
            sizes=kv.get("sizes", "100vw"),
            cls=kv.get("class"),
        )

    return _IMG_DIRECTIVE_RE.sub(repl, body)


# ---------------------------------------------------------------------------
# Testimonials: component + data file, zero fabricated entries
# ---------------------------------------------------------------------------

_TESTIMONIALS_RE = re.compile(r"\[\[testimonials\]\]")


def load_testimonials() -> list[dict[str, str]]:
    path = DATA_DIR / "testimonials.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"testimonials.json is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("testimonials.json must be a JSON array")
    return data


def expand_testimonials(body: str) -> str:
    """Replace [[testimonials]] with the rendered component.

    Zero entries -> the section is omitted entirely (no fabricated
    testimonials; the owner provides them later).
    """

    def repl(m: re.Match) -> str:
        items = load_testimonials()
        if not items:
            return ""
        parts = ['<section class="testimonials" aria-label="Client testimonials">',
                 "<h2>What Our Clients Say</h2>"]
        for item in items:
            quote = html.escape(str(item.get("quote", "")).strip())
            name = html.escape(str(item.get("name", "")).strip())
            detail = html.escape(str(item.get("detail", "")).strip())
            if not quote or not name:
                raise ValueError("testimonial entries need at least quote= and name=")
            cite = f"<cite>&mdash; {name}" + (f", {detail}" if detail else "") + "</cite>"
            parts.append(f"<blockquote class=\"testimonial\"><p>{quote}</p>{cite}</blockquote>")
        parts.append("</section>")
        return "\n".join(parts)

    return _TESTIMONIALS_RE.sub(repl, body)


# ---------------------------------------------------------------------------
# Page scripts: frontmatter `scripts: contact.js` -> module script tags.
# ---------------------------------------------------------------------------

_SCRIPT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.js$")


def render_scripts(meta: dict[str, str]) -> str:
    """Inject per-page ES module scripts before </body>.

    Fail-closed like images: a bad name or a file missing from src/js/
    aborts the build instead of shipping a page with a dead script tag.
    """
    raw = meta.get("scripts", "").strip()
    if not raw:
        return ""
    tags = []
    for name in [s.strip() for s in raw.split(",") if s.strip()]:
        if not _SCRIPT_NAME_RE.match(name) or "/" in name or name.startswith("."):
            raise ValueError(
                f'page "{meta.get("path", "?")}": scripts: bad script name {name!r}'
            )
        if not (JS_DIR / name).is_file():
            raise ValueError(
                f'page "{meta.get("path", "?")}": scripts: {name} not found under src/js/'
            )
        tags.append(f'<script type="module" src="/js/{name}"></script>')
    return "\n".join(tags)


# ---------------------------------------------------------------------------
# JSON-LD: LocalBusiness on every page, extras via frontmatter `jsonld:`
# ---------------------------------------------------------------------------


def load_jsonld(name: str) -> str:
    """Read src/partials/jsonld/<name>.json, validate it, wrap in a script tag."""
    path = JSONLD_DIR / name
    text = path.read_text(encoding="utf-8")
    # Contact tokens (e.g. [[contact.street]]) expand BEFORE validation —
    # the JSON-LD carries the firm's address/phone and must stay in sync.
    text = expand_site_tokens(text)
    try:
        json.loads(text)  # fail-closed: invalid JSON-LD aborts the build
    except json.JSONDecodeError as exc:
        raise ValueError(f"jsonld/{name} is not valid JSON: {exc}") from exc
    return f'<script type="application/ld+json">\n{text.strip()}\n</script>'


def render_jsonld(meta: dict[str, str]) -> str:
    blocks = [load_jsonld("localbusiness.json")]
    for extra in [e.strip() for e in meta.get("jsonld", "").split(",")]:
        if extra:
            blocks.append(load_jsonld(f"{extra}.json"))
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# sitemap.xml + robots.txt
# ---------------------------------------------------------------------------

SITEMAP_DATE = "2026-10-05"


def render_sitemap(metas: list[dict[str, str]]) -> str:
    urls = []
    for meta in metas:
        path = meta["path"]
        if path == "/404.html" or meta.get("sitemap", "true").lower() == "false":
            continue
        urls.append(f"  <url>\n    <loc>{SITE_ORIGIN}{path}</loc>\n"
                    f"    <lastmod>{SITEMAP_DATE}</lastmod>\n  </url>")
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
            + "\n".join(urls) + "\n</urlset>\n")


ROBOTS_TXT = ("User-agent: *\n"
              "Allow: /\n"
              f"Sitemap: {SITE_ORIGIN}/sitemap.xml\n")


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_NAV_TOKEN_RE = re.compile(r"\[\[nav:([a-z]+)\]\]")


def parse_page(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing frontmatter block (--- ... ---)")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    for required in ("title", "description", "path"):
        if required not in meta:
            raise ValueError(f"{path.name}: frontmatter missing required key '{required}'")
    return meta, m.group(2)


def load_partial(name: str) -> str:
    # Contact/site tokens expand in partials too (footer/header carry the
    # address, phone, and email) — one edit in site-contact.json updates them.
    return expand_site_tokens((PARTIALS_DIR / name).read_text(encoding="utf-8"))


def apply_nav_active(header: str, active: str) -> str:
    def repl(m: re.Match) -> str:
        return ' aria-current="page"' if m.group(1) == active else ""

    return _NAV_TOKEN_RE.sub(repl, header)


def render_page(meta: dict[str, str], body: str) -> str:
    head = load_partial("head.html")
    head = head.replace("{{title}}", html.escape(meta["title"]))
    head = head.replace("{{description}}", html.escape(meta["description"], quote=True))
    head = head.replace("{{canonical}}", SITE_ORIGIN + meta["path"])
    head = head.replace("{{jsonld}}", render_jsonld(meta))

    body_open = load_partial("body-open.html")
    header = apply_nav_active(load_partial("header.html"), meta.get("nav", "none"))
    footer = load_partial("footer.html")
    foot = load_partial("foot.html")
    scripts = render_scripts(meta)
    if scripts:
        foot = scripts + "\n" + foot

    body = expand_img_directives(body)
    body = expand_testimonials(body)
    if body.count('id="main"') != 1:
        raise ValueError(
            f'page "{meta["path"]}": body must contain exactly one id="main" landmark'
        )
    return head + body_open + header + body + footer + foot


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------


def _nested_target(out_dir: Path, page_path: str) -> Path:
    """Map a frontmatter path to a nested index.html target.

    "/portal/login/" -> out/portal/login/index.html
    "/"              -> out/index.html
    "/404.html"      -> out/404.html (file paths pass through unchanged)
    """
    if page_path == "/":
        return out_dir / "index.html"
    if not page_path.endswith("/"):
        return out_dir / page_path.lstrip("/")
    return out_dir / page_path.strip("/") / "index.html"


def build(out_dir: Path, *, clean: bool = True, base_path: str = "") -> list[Path]:
    """Assemble the site.

    ``base_path`` is "" for domain-root serving or "/subpath" when the
    site lives under a subpath (e.g. GitHub Pages project sites). HTML
    URLs and CSS url() references are rewritten with a real parser
    (build_basepath) -- never regexes. JavaScript resolves the base path
    at runtime via site-base.js, so JS is copied verbatim.
    """
    if base_path and (not base_path.startswith("/") or base_path.endswith("/")):
        raise ValueError(f'base_path must be "" or "/subpath", got {base_path!r}')
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    emitted: list[Path] = []
    metas: list[dict[str, str]] = []

    # Pages -> dist (flat; src/pages/<name>.html becomes dist/<name>.html)
    for page_path in sorted(PAGES_DIR.glob("*.html")):
        meta, body = parse_page(page_path)
        # Site-identity tokens (Q2/Q4): expand in frontmatter fields and
        # body alike so no copy hardcodes the portal name or channel wording.
        meta = {k: expand_site_tokens(v) for k, v in meta.items()}
        body = expand_site_tokens(body)
        rendered = render_page(meta, body)
        rendered = apply_base_path_to_html(rendered, base_path)
        if base_path:
            # Subpath hosts (GitHub Pages) need pretty nested URLs:
            # portal-login.html -> portal/login/index.html, using the
            # frontmatter path as the source of truth.
            nested = _nested_target(out_dir, meta["path"])
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text(rendered, encoding="utf-8")
            emitted.append(nested)
        else:
            target = out_dir / page_path.name
            target.write_text(rendered, encoding="utf-8")
            emitted.append(target)
        metas.append(meta)

    if base_path:
        (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    # SEO: sitemap.xml (all sitemap-eligible pages) + robots.txt
    (out_dir / "sitemap.xml").write_text(render_sitemap(metas), encoding="utf-8")
    (out_dir / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8")

    # Static trees: css always; js/assets only if non-empty
    for tree in (CSS_DIR, JS_DIR, ASSETS_DIR):
        if not tree.is_dir():
            continue
        files = [p for p in tree.rglob("*") if p.is_file()]
        if not files:
            continue
        dest = out_dir / tree.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(tree, dest)
        # JS is copied verbatim except for site/contact token expansion —
        # a few scripts embed the phone number in user-facing messages, and
        # those must stay in sync with site-contact.json too. (Base-path
        # handling for JS is runtime via site-base.js; /api/* is never
        # rewritten because the stub intercepts it before network dispatch.)
        if tree is JS_DIR:
            for js_file in sorted(dest.rglob("*.js")):
                js_file.write_text(
                    expand_site_tokens(js_file.read_text(encoding="utf-8")),
                    encoding="utf-8",
                )
        # CSS url() references are rewritten with a real scanner when the
        # site is served from a subpath.
        if tree is CSS_DIR and base_path:
            for css_file in sorted(dest.rglob("*.css")):
                css_file.write_text(
                    apply_base_path_to_css(css_file.read_text(encoding="utf-8"), base_path),
                    encoding="utf-8",
                )
        emitted.extend(sorted(dest.rglob("*")))

    return emitted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble the static frontend.")
    parser.add_argument("--out", default="dist", help="output dir relative to frontend/")
    parser.add_argument("--clean", dest="clean", action="store_true", default=True)
    parser.add_argument("--no-clean", dest="clean", action="store_false")
    parser.add_argument(
        "--base-path", default="",
        help='subpath the site is served from, e.g. "/jaro-accounting-rebuild" '
             '(empty for domain-root serving)',
    )
    args = parser.parse_args(argv)

    out_dir = (ROOT / args.out).resolve()
    emitted = build(out_dir, clean=args.clean, base_path=args.base_path)
    if args.base_path:
        pages = list(out_dir.rglob("index.html")) + [
            p for p in out_dir.glob("*.html") if p.name != "index.html"
        ]
    else:
        pages = list(out_dir.glob("*.html"))
    print(f"build OK: {len(pages)} page(s) -> {out_dir} (base-path={args.base_path or '/'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
