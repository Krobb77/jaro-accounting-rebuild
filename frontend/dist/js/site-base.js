/* Site base path for client-side navigation.
 *
 * The static site can be served from the domain root (production) or from a
 * subpath (e.g. GitHub Pages project sites at /jaro-accounting-rebuild).
 * HTML/CSS URLs are rewritten at build time, but JavaScript navigation
 * (window.location.href = "/portal/...") must resolve the base path at
 * runtime so one script bundle works under any base path.
 *
 * Every app script lives under /js/, so the base path is whatever precedes
 * "/js/" in this module's own URL. No build-time rewriting of JS is needed
 * (and none is done -- /api/* URLs are left alone because the stub backend
 * intercepts them before network dispatch).
 */
const _base = (() => {
  try {
    const path = new URL(import.meta.url).pathname;
    const i = path.lastIndexOf("/js/");
    return i > 0 ? path.slice(0, i) : "";
  } catch {
    return "";
  }
})();

export const SITE_BASE = _base;

/** Prefix a root-absolute internal path with the deployment base path. */
export function withBase(path) {
  return SITE_BASE + path;
}
