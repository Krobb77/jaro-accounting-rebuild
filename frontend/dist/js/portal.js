/* /portal/ — dashboard, onboarding gates, session watch. */
import {
  portalApi, ApiRequestError, startSessionWatch, initPortalChrome,
  esc, vancouverDateTime,
} from "./portal-api.js";
import { withBase } from "./site-base.js";

const root = document.getElementById("portal-root");
const errorBox = document.getElementById("portal-error");
const banner = document.getElementById("session-banner");
const extendBtn = document.getElementById("session-extend");

const termsModal = document.getElementById("terms-modal");
const termsBody = document.getElementById("terms-body");
const termsError = document.getElementById("terms-error");
let termsHash = null;

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("portal-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = withBase("/portal/login/");
});

// --- terms gate (no dismiss: Accept or Sign out) -------------------------------
async function ensureTerms() {
  try {
    await portalApi.get("/dashboard");
    return true;
  } catch (err) {
    if (!(err instanceof ApiRequestError) || err.code !== "terms_not_accepted") throw err;
  }
  const current = await portalApi.get("/terms/current");
  termsHash = current.version_hash;
  termsBody.innerHTML = current.body_html; // server-rendered firm copy
  termsModal.hidden = false;
  root.innerHTML = ""; // keep "Loading your dashboard…" from sitting behind the modal
  document.getElementById("terms-accept").focus();
  return false;
}

document.getElementById("terms-accept").addEventListener("click", async () => {
  termsError.hidden = true;
  try {
    await portalApi.post("/terms/accept", { json: { version_hash: termsHash } });
    termsModal.hidden = true;
    await boot();
  } catch (err) {
    termsError.hidden = false;
    termsError.textContent = err.message || "Couldn't record your acceptance. Try again.";
  }
});

document.getElementById("terms-signout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = withBase("/portal/login/");
});

// --- dashboard ---------------------------------------------------------------------
const FILING_STATUS_LABELS = {
  collecting: "Collecting documents",
  awaiting_info: "Awaiting info",
  preparing: "Preparing",
  filed: "Filed with CRA",
  confirmed: "Confirmed by CRA",
};

function money(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

/* P18: key-facts summary strip — "what needs me" first. Values come from the
   API (demo figures under the stub); labels stay demo-plausible, never
   invented facts. */
function factsStrip(d) {
  const s = d.snapshots;
  const filing = s.filings[0];
  const open = s.invoices.open_count;
  const owing = s.invoices.total_owing_cents;
  const docs = s.documents;
  return `<section class="portal-facts" aria-label="Key facts">
    <div class="portal-fact">
      <p class="portal-fact__label">Next filing</p>
      <p class="portal-fact__value">${filing ? esc(`${filing.kind_label} ${filing.tax_year}`) : "None yet"}</p>
      <p class="portal-fact__sub">${filing ? esc(FILING_STATUS_LABELS[filing.status] || filing.status) : "Jared will start one for you"}</p>
    </div>
    <div class="portal-fact">
      <p class="portal-fact__label">Balance owing</p>
      <p class="portal-fact__value">${money(owing)}</p>
      <p class="portal-fact__sub">${open ? `${open} open invoice${open === 1 ? "" : "s"}` : "Nothing owing"}</p>
    </div>
    <div class="portal-fact">
      <p class="portal-fact__label">Documents</p>
      <p class="portal-fact__value">${docs.new_count ? `${docs.new_count} new` : `${docs.count} on file`}</p>
      <p class="portal-fact__sub">${docs.new_count ? `${docs.count} on file — see Needs attention` : "Shared files appear here"}</p>
    </div>
  </section>`;
}

function attentionSection(title, items, renderItem) {
  if (!items.length) return "";
  return `<section class="portal-section" aria-label="${esc(title)}">
    <h2>${esc(title)}</h2>
    <ul class="portal-list">${items.map(renderItem).join("")}</ul>
  </section>`;
}

async function boot() {
  root.innerHTML = "<p>Loading your dashboard&hellip;</p>";
  try {
    const d = await portalApi.get("/dashboard");
    const na = d.needs_attention;
    let html = `<h1>${esc(d.greeting)}</h1>`;
    html += factsStrip(d);
    const hasAttention = na.filings.length || na.invoices.length || na.documents.length;
    if (hasAttention) {
      html += `<section class="portal-attention" aria-label="Needs attention"><h2>Needs attention</h2>`;
      for (const f of na.filings) {
        html += `<p><strong>${esc(f.kind_label)} ${esc(f.tax_year)}</strong> — awaiting info${f.note ? `: ${esc(f.note)}` : ""}</p>`;
      }
      for (const i of na.invoices) {
        html += `<p><strong>Invoice ${esc(i.invoice_number)}</strong> — overdue since ${esc(vancouverDateTime(i.due_at))}</p>`;
      }
      for (const doc of na.documents) {
        html += `<p><strong>${esc(doc.original_filename)}</strong> — newly shared by Jared</p>`;
      }
      html += `</section>`;
    } else {
      html += `<p class="portal-empty">You're all caught up. New documents, filings, and invoices will appear here.</p>`;
    }
    if (d.activity.length) {
      html += `<section class="portal-section" aria-label="Recent activity"><h2>Recent activity</h2><ul class="portal-list">`;
      for (const a of d.activity) {
        html += `<li><span class="portal-list__time">${esc(vancouverDateTime(a.at))}</span> ${esc(a.summary)}</li>`;
      }
      html += `</ul></section>`;
    }
    root.innerHTML = html;
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "terms_not_accepted") {
      await ensureTerms();
      return;
    }
    if (err instanceof ApiRequestError && err.code === "password_change_required") {
      // Temporary-password session landed here directly: the login page
      // shows the forced-change form for sessions in this state.
      window.location.href = withBase("/portal/login/");
      return;
    }
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = withBase("/portal/login?reason=session-expired");
      return;
    }
    showError(err.message || "Couldn't load your dashboard.");
    root.innerHTML = "";
  }
}

(async function init() {
  initPortalChrome();
  const termsOk = await ensureTerms().catch((err) => {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = withBase("/portal/login?reason=session-expired");
    } else showError(err.message);
    return false;
  });
  if (!termsOk) return;
  startSessionWatch({ bannerEl: banner, extendBtn });
  await boot();
})();
