import { withBase } from "./site-base.js";
/* Shared portal API client — imported by every portal page script.
 * No side effects on import.
 *
 * Conventions:
 * - All portal/admin calls are same-origin fetch with credentials included
 *   (the __Host- session cookie). CSRF: cookie is SameSite=Lax + the API is
 *   same-origin only; state-changing calls use POST/PUT/PATCH/DELETE.
 * - ApiRequestError carries the backend's A1 envelope code.
 * - Session polling: pollSession() hits GET /api/v1/portal/session (the
 *   authoritative expires_in_seconds — the frontend never computes expiry).
 *   startSessionWatch() wires the ≤120s warning banner + one-click extend.
 */

export const PORTAL_API = "/api/v1/portal";
export const ADMIN_API = "/api/v1/admin";

/** API error carrying the backend's A1 envelope code. */
export class ApiRequestError extends Error {
  constructor(status, code, message, fields = []) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

async function request(base, path, { method = "GET", json, form, headers = {} } = {}) {
  const init = { method, headers: { ...headers }, credentials: "include" };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form !== undefined) {
    init.body = form; // FormData: browser sets the multipart boundary
  }
  let res;
  try {
    res = await fetch(base + path, init);
  } catch (err) {
    throw new ApiRequestError(0, "network_error",
      "Couldn't reach the portal. Check your connection and try again.");
  }
  const ctype = res.headers.get("content-type") || "";
  let body = null;
  if (ctype.includes("application/json")) {
    try { body = await res.json(); } catch { body = null; }
  }
  if (!res.ok) {
    throw new ApiRequestError(
      res.status,
      (body && body.code) || "request_failed",
      (body && body.message) || `The portal returned ${res.status}.`,
      (body && body.fields) || []
    );
  }
  return body;
}

export const portalApi = {
  get: (path, opts) => request(PORTAL_API, path, { ...opts, method: "GET" }),
  post: (path, opts) => request(PORTAL_API, path, { ...opts, method: "POST" }),
  patch: (path, opts) => request(PORTAL_API, path, { ...opts, method: "PATCH" }),
  put: (path, opts) => request(PORTAL_API, path, { ...opts, method: "PUT" }),
  del: (path, opts) => request(PORTAL_API, path, { ...opts, method: "DELETE" }),
};

export const adminApi = {
  get: (path, opts) => request(ADMIN_API, path, { ...opts, method: "GET" }),
  post: (path, opts) => request(ADMIN_API, path, { ...opts, method: "POST" }),
  patch: (path, opts) => request(ADMIN_API, path, { ...opts, method: "PATCH" }),
  put: (path, opts) => request(ADMIN_API, path, { ...opts, method: "PUT" }),
  del: (path, opts) => request(ADMIN_API, path, { ...opts, method: "DELETE" }),
};

/** A fresh Idempotency-Key for each mutating call (A3). */
export function idempotencyKey() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Authoritative session status. Throws ApiRequestError(401) when expired. */
export async function pollSession() {
  return portalApi.get("/session");
}

/**
 * Session watchdog for portal pages.
 * - Polls the authoritative expires_in_seconds every 30s.
 * - At ≤120s shows the warning banner (no auto-extend; the user clicks).
 * - One-click extend is idempotent (≤1 effective extend/60s server-side).
 * - On 401 → redirect to /portal/login?reason=session-expired.
 * - On 403 terms_not_accepted → show the terms modal hook (if provided).
 *
 * opts: { bannerEl, extendBtn, onTermsRequired, onExpired }
 */
export function startSessionWatch(opts = {}) {
  const { bannerEl, extendBtn, onTermsRequired, onExpired } = opts;
  let timer = null;
  let stopped = false;

  async function tick() {
    if (stopped) return;
    try {
      const s = await pollSession();
      const secs = s.expires_in_seconds;
      if (bannerEl) {
        const show = secs <= 120;
        bannerEl.hidden = !show;
        if (show) {
          const mm = Math.floor(secs / 60);
          const ss = String(secs % 60).padStart(2, "0");
          const label = bannerEl.querySelector("[data-countdown]");
          if (label) label.textContent = `${mm}:${ss}`;
        }
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        stop();
        if (onExpired) onExpired(err);
        else window.location.href = withBase("/portal/login?reason=session-expired");
      }
      // Network blips: keep the last known state, retry next tick.
    }
    if (!stopped) timer = setTimeout(tick, 30000);
  }

  async function extendOnce() {
    try {
      await portalApi.post("/sessions/extend");
      if (bannerEl) bannerEl.hidden = true;
      tick(); // refresh immediately
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        stop();
        window.location.href = withBase("/portal/login?reason=session-expired");
      }
      // Otherwise the banner stays up; the user can retry.
    }
  }

  function stop() {
    stopped = true;
    if (timer) clearTimeout(timer);
  }

  if (extendBtn) extendBtn.addEventListener("click", extendOnce);
  tick();
  return { stop, extendOnce, tick };
}

/** Route-guard helper for session-gated pages. */
export async function requireSession({ onTermsRequired } = {}) {
  try {
    const s = await pollSession();
    return s;
  } catch (err) {
    if (err instanceof ApiRequestError) {
      if (err.status === 401) {
        window.location.href = withBase("/portal/login?reason=session-expired");
        throw err;
      }
      if (err.code === "terms_not_accepted" && onTermsRequired) {
        onTermsRequired();
        throw err;
      }
      if (err.code === "totp_required") {
        window.location.href = withBase("/portal/login?reason=2fa-required");
        throw err;
      }
    }
    throw err;
  }
}

/** Escape HTML for interpolated strings. */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/**
 * Authenticated chrome (P19): a quiet "Signed in as …" indicator plus the
 * owner-only Admin link, inside the portal nav. Idempotent; safe to call
 * without a live session (the page's own guard redirects on 401).
 */
export async function initPortalChrome() {
  const nav = document.querySelector(".portal-nav");
  if (!nav || nav.dataset.chromeInit) return;
  nav.dataset.chromeInit = "1";
  let session = null;
  try {
    session = await pollSession();
  } catch {
    return;
  }
  if (!session) return;
  if (session.role === "owner" && !nav.querySelector('a[href="/portal/admin/"]')) {
    const adminLink = document.createElement("a");
    adminLink.href = "/portal/admin/";
    adminLink.textContent = "Admin";
    const signout = nav.querySelector(".link-button");
    nav.insertBefore(adminLink, signout);
  }
  const who = document.createElement("span");
  who.className = "portal-nav__who";
  who.textContent = `Signed in as ${session.email || "you"}`;
  const signout = nav.querySelector(".link-button");
  nav.insertBefore(who, signout);
}

/** Format an ISO timestamp in America/Vancouver. */
export function vancouverDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-CA", {
      timeZone: "America/Vancouver",
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
