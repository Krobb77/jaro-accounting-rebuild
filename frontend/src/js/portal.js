/* /portal/ — dashboard, terms gate, TOTP enrollment, session watch. */
import {
  portalApi, ApiRequestError, startSessionWatch,
  esc, vancouverDateTime,
} from "./portal-api.js";

const root = document.getElementById("portal-root");
const errorBox = document.getElementById("portal-error");
const banner = document.getElementById("session-banner");
const extendBtn = document.getElementById("session-extend");

const termsModal = document.getElementById("terms-modal");
const termsBody = document.getElementById("terms-body");
const termsError = document.getElementById("terms-error");
let termsHash = null;

const totpModal = document.getElementById("totp-modal");
const totpError = document.getElementById("totp-error");

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("portal-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = "/portal/login/";
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
  window.location.href = "/portal/login/";
});

// --- TOTP enrollment -------------------------------------------------------------
async function ensureTotp() {
  const status = await portalApi.get("/2fa/status").catch(() => null);
  if (!status || status.enrolled) return;
  totpModal.hidden = false;
  const res = await portalApi.post("/2fa/enroll");
  document.getElementById("totp-manual-key").textContent = res.manual_key;
  document.getElementById("totp-uri-link").href = res.provisioning_uri;
}

document.getElementById("totp-confirm").addEventListener("click", async () => {
  totpError.hidden = true;
  const code = document.getElementById("totp-code1").value.trim();
  const code2 = document.getElementById("totp-code2").value.trim();
  try {
    const res = await portalApi.post("/2fa/confirm", { json: { code, code2 } });
    document.getElementById("totp-step-key").hidden = true;
    const list = document.getElementById("totp-recovery-list");
    list.innerHTML = "";
    for (const c of res.recovery_codes) {
      const li = document.createElement("li");
      li.innerHTML = "<code></code>";
      li.firstChild.textContent = c;
      list.appendChild(li);
    }
    document.getElementById("totp-step-recovery").hidden = false;
  } catch (err) {
    totpError.hidden = false;
    totpError.textContent = err.message || "Those codes didn't work. Try the next two.";
  }
});

document.getElementById("totp-recovery-saved").addEventListener("change", (e) => {
  document.getElementById("totp-done").disabled = !e.target.checked;
});

document.getElementById("totp-done").addEventListener("click", async () => {
  totpModal.hidden = true;
  await boot();
});

// --- dashboard ---------------------------------------------------------------------
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
    const s = d.snapshots;
    html += `<section class="portal-cards" aria-label="Snapshot">`;
    html += `<div class="portal-card-mini"><h2>Filings</h2><p>${s.filings.length ? `${esc(s.filings[0].kind_label)} ${esc(s.filings[0].tax_year)} — ${esc(s.filings[0].status)}` : "No filings yet"}</p></div>`;
    html += `<div class="portal-card-mini"><h2>Documents</h2><p>${s.documents.count} on file${s.documents.new_count ? `, ${s.documents.new_count} new` : ""}</p></div>`;
    html += `<div class="portal-card-mini"><h2>Invoices</h2><p>${s.invoices.open_count} open — $${(s.invoices.total_owing_cents / 100).toFixed(2)} owing</p></div>`;
    html += `</section>`;
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
    if (err instanceof ApiRequestError && err.code === "totp_required") {
      root.innerHTML = "<p>Two-step verification setup is required. Opening setup&hellip;</p>";
      await ensureTotp();
      return;
    }
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = "/portal/login?reason=session-expired";
      return;
    }
    showError(err.message || "Couldn't load your dashboard.");
    root.innerHTML = "";
  }
}

(async function init() {
  // Terms first (the gate), then TOTP, then content — matches the backend order.
  const termsOk = await ensureTerms().catch((err) => {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = "/portal/login?reason=session-expired";
    } else showError(err.message);
    return false;
  });
  if (!termsOk) return;
  startSessionWatch({ bannerEl: banner, extendBtn });
  await boot();
  // TOTP check happens after first content load (backend returns totp_required).
})();
