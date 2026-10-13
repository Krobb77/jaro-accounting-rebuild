/* /portal/filings/ — filing list + five-step timeline (read-only). */
import {
  portalApi, ApiRequestError, startSessionWatch, requireSession,
  esc, vancouverDateTime,
} from "./portal-api.js";

const root = document.getElementById("filings-root");
const errorBox = document.getElementById("portal-error");
const banner = document.getElementById("session-banner");
const extendBtn = document.getElementById("session-extend");

const STATUS_LABELS = {
  collecting: "Collecting documents",
  awaiting_info: "Awaiting info",
  preparing: "Preparing",
  filed: "Filed with CRA",
  confirmed: "Confirmed by CRA",
};

// The five filing states in machine order — the timeline always shows all
// five, with reached steps marked from the append-only history.
const STATUS_ORDER = ["collecting", "awaiting_info", "preparing", "filed", "confirmed"];

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("portal-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = "/portal/login/";
});

async function loadFilings() {
  const res = await portalApi.get("/filings");
  const items = res.items || [];
  if (!items.length) {
    root.innerHTML = "<p class=\"portal-empty\">No filings yet. When Jared starts a return for you, its progress will appear here.</p>";
    return;
  }
  let html = "";
  for (const f of items) {
    const detail = await portalApi.get(`/filings/${encodeURIComponent(f.id)}`);
    // Map the append-only history onto the five machine states.
    const reachedAt = {};
    for (const s of detail.timeline || []) {
      if (!(s.status in reachedAt)) reachedAt[s.status] = s.at;
    }
    html += `<section class="portal-card-mini" aria-label="${esc(f.kind_label)} ${esc(f.tax_year)}">
      <h2>${esc(f.kind_label)} — ${esc(f.tax_year)}</h2>
      <p>Status: <strong>${esc(STATUS_LABELS[f.status] || f.status)}</strong></p>
      ${f.note ? `<p class="filing-note">From Jared: ${esc(f.note)}</p>` : ""}
      <ol class="timeline">`;
    for (const status of STATUS_ORDER) {
      const isCurrent = status === f.status;
      const reached = status in reachedAt;
      const cls = isCurrent ? " timeline__step--current" : (reached ? " timeline__step--done" : "");
      html += `<li class="timeline__step${cls}">
        <span class="timeline__label">${esc(STATUS_LABELS[status])}</span>
        <span class="timeline__time">${reached ? esc(vancouverDateTime(reachedAt[status])) : "—"}</span>
      </li>`;
    }
    html += `</ol></section>`;
  }
  root.innerHTML = html;
}

(async function init() {
  await requireSession();
  startSessionWatch({ bannerEl: banner, extendBtn });
  try {
    await loadFilings();
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = "/portal/login?reason=session-expired";
      return;
    }
    showError(err.message || "Couldn't load your filings.");
    root.innerHTML = "";
  }
})();
