#!/usr/bin/env python3
"""Base-path support for the Jaro Accounting static build.

When the site is served from a subpath (e.g. GitHub Pages project sites at
``/jaro-accounting-rebuild``), every root-absolute internal URL must be
prefixed with that subpath. This module does it *inside the real build*
with a proper HTML parser and a CSS scanner -- never with regexes over
built files, which has corrupted output twice before (literal ``\\1``
leaking into srcset attributes).

Public API:
    apply_base_path_to_html(html_text, base) -> str
    apply_base_path_to_css(css_text, base) -> str

``base`` is ``""`` for root serving or ``"/subpath"`` (leading slash, no
trailing slash). An empty base is a no-op.

Rules:
  - Only root-absolute URLs are rewritten: exactly one leading ``/``.
  - Skipped: ``//host/...`` (protocol-relative), absolute http(s) URLs,
    ``data:``, ``mailto:``, ``tel:``, ``#fragments``.
  - ``srcset`` is parsed per the WHATWG microsyntax (comma-separated
    candidates; each candidate's URL is its leading token).
  - JavaScript is NOT rewritten here: app scripts resolve the base path
    at runtime via ``src/js/site-base.js`` (``import.meta.url``), and
    ``stub-backend.js`` is already runtime-aware via ``STUB_BASE``.
    ``/api/*`` URLs are never rewritten -- the stub intercepts them
    before network dispatch on static hosts.
"""

from __future__ import annotations

from html.parser import HTMLParser
from io import StringIO

# Attributes whose value is a single URL.
_URL_ATTRS = {"href", "src", "action", "poster", "data-src"}
# Attributes whose value is a srcset list.
_SRCSET_ATTRS = {"srcset"}


def _is_rewritable(url: str) -> bool:
    """True for root-absolute internal URLs (exactly one leading slash)."""
    return (
        isinstance(url, str)
        and url.startswith("/")
        and not url.startswith("//")
    )


def prefix_url(url: str, base: str) -> str:
    """Prefix one URL with the base path if it is a rewritable root-absolute URL."""
    if not base or not _is_rewritable(url):
        return url
    return base + url


def prefix_srcset(srcset: str, base: str) -> str:
    """Prefix every candidate URL in a srcset attribute value."""
    if not base:
        return srcset
    out: list[str] = []
    for candidate in srcset.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        # The URL is the first whitespace-separated token; the rest is the
        # descriptor (e.g. "800w", "2x").
        parts = candidate.split(None, 1)
        url = parts[0]
        rest = (" " + parts[1]) if len(parts) > 1 else ""
        out.append(prefix_url(url, base) + rest)
    return ", ".join(out)


def _rewrite_css_urls(css: str, base: str) -> str:
    """Rewrite url(...) references in CSS using a small scanner (no regex)."""
    if not base:
        return css
    out = StringIO()
    i, n = 0, len(css)
    while i < n:
        # Look for "url(" case-insensitively, not inside strings/comments.
        # A full CSS tokenizer is overkill; url( is distinctive enough and we
        # validate the result in tests.
        j = css.lower().find("url(", i)
        if j == -1:
            out.write(css[i:])
            break
        out.write(css[i:j])
        k = j + 4
        # Skip whitespace.
        while k < n and css[k] in " \t\n\r\f":
            k += 1
        quote = ""
        if k < n and css[k] in "\"'":
            quote = css[k]
            k += 1
        start = k
        if quote:
            while k < n and css[k] != quote:
                k += 1
            url = css[start:k]
            k += 1  # closing quote
        else:
            while k < n and css[k] not in " \t\n\r\f)":
                k += 1
            url = css[start:k]
        # Skip trailing whitespace before ")".
        while k < n and css[k] in " \t\n\r\f":
            k += 1
        if k < n and css[k] == ")":
            k += 1
        new_url = prefix_url(url, base)
        out.write(f"url({quote}{new_url}{quote})")
        i = k
    return out.getvalue()


def apply_base_path_to_css(css_text: str, base: str) -> str:
    """Rewrite root-absolute url(...) references in a CSS file."""
    return _rewrite_css_urls(css_text, base)


class _HTMLRewriter(HTMLParser):
    """HTMLParser that rewrites URL attributes, preserving everything else."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=False)
        self.base = base
        self.out = StringIO()

    def _rewrite_attr(self, name: str, value: str | None) -> str:
        lname = name.lower()
        if value is None:
            return f" {name}"
        if lname in _SRCSET_ATTRS:
            value = prefix_srcset(value, self.base)
        elif lname in _URL_ATTRS:
            value = prefix_url(value, self.base)
        elif lname == "style":
            value = _rewrite_css_urls(value, self.base)
        # The parser hands us the raw attribute text (entities intact, since
        # convert_charrefs=False). Re-serialize double-quoted: escape only a
        # literal double quote; everything else goes back verbatim so we
        # never double-escape &amp; and friends.
        escaped = value.replace('"', "&quot;")
        return f' {name}="{escaped}"'

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.write("<" + tag)
        for name, value in attrs:
            self.out.write(self._rewrite_attr(name, value))
        self.out.write(">")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.write("<" + tag)
        for name, value in attrs:
            self.out.write(self._rewrite_attr(name, value))
        self.out.write(" />")

    def handle_endtag(self, tag: str) -> None:
        self.out.write(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.write(data)

    def handle_comment(self, data: str) -> None:
        self.out.write(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.write(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.out.write(f"<?{data}>")

    def handle_entityref(self, name: str) -> None:
        self.out.write(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.write(f"&#{name};")


def apply_base_path_to_html(html_text: str, base: str) -> str:
    """Rewrite root-absolute internal URLs in a fully-rendered HTML page."""
    if not base:
        return html_text
    rw = _HTMLRewriter(base)
    rw.feed(html_text)
    rw.close()
    return rw.out.getvalue()
