/* /portal/admin/ — Jared's owner console. Owner role only (403 otherwise). */
import {
  adminApi, portalApi, ApiRequestError,
  initPortalChrome, idempotencyKey, esc, vancouverDateTime,
} from "./portal-api.js";

const errorBox = document.getElementById("admin-error");
const clientsRoot = document.getElementById("clients-root");
const statsRoot = document.getElementById("admin-stats");
const dossierSection = document.getElementById("dossier-section");
const dossierRoot = document.getElementById("dossier-root");
const auditRoot = document.getElementById("audit-root");

let currentClientId = null;
let currentTab = "overview";

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("admin-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = "/jaro-accounting-rebuild/portal/login/";
});

// --- at-a-glance (P20) ---------------------------------------------------------------
async function loadStats() {
  try {
    const res = await adminApi.get("/clients?page=1&per_page=50");
    const clients = res.items || [];
    const active = clients.filter((c) => c.status === "active").length;
    const dossiers = await Promise.all(
      clients.map((c) => adminApi.get(`/clients/${encodeURIComponent(c.id)}`).catch(() => null))
    );
    let pendingFilings = 0, outstanding = 0, outstandingCents = 0;
    for (const d of dossiers) {
      if (!d) continue;
      for (const f of d.filings || []) {
        if (["collecting", "awaiting_info", "preparing"].indexOf(f.status) !== -1) pendingFilings++;
      }
      for (const i of d.invoices || []) {
        if (i.status === "issued") { outstanding++; outstandingCents += i.total_cents || 0; }
      }
    }
    statsRoot.innerHTML =
      `<div class="portal-stat"><p class="portal-stat__number">${active}</p><p class="portal-stat__label">Active clients</p></div>` +
      `<div class="portal-stat"><p class="portal-stat__number">${pendingFilings}</p><p class="portal-stat__label">Pending filings</p></div>` +
      `<div class="portal-stat"><p class="portal-stat__number">${outstanding}</p>` +
      `<p class="portal-stat__label">Outstanding invoices${outstanding ? ` &middot; $${(outstandingCents / 100).toFixed(2)}` : ""}</p></div>`;
  } catch {
    statsRoot.innerHTML = "";
  }
}

// --- clients -------------------------------------------------------------------
/* P27: client list as rows (name, email, status, action) with status badges. */
function clientStatusBadge(c) {
  if (c.must_change_password) {
    return `<span class="badge badge--needs-password-change">Needs password change</span>`;
  }
  if (c.status === "active") return `<span class="badge badge--active">Active</span>`;
  return `<span class="badge badge--void">${esc(c.status || "unknown")}</span>`;
}

function clientName(c) {
  return c.display_name || c.name || c.email;
}

async function loadClients(search = "") {
  const params = new URLSearchParams({ page: "1", per_page: "50" });
  if (search) params.set("search", search);
  const res = await adminApi.get(`/clients?${params}`);
  const items = res.items || [];
  if (!items.length) {
    clientsRoot.innerHTML = `<p class="portal-empty">No clients yet. Client accounts you create below will appear here.</p>
      <p class="portal-empty-action"><button type="button" class="btn btn--small" id="empty-create-client">Create the first client account</button></p>`;
    document.getElementById("empty-create-client").addEventListener("click", () => {
      document.getElementById("create-client").scrollIntoView({ block: "start" });
      document.getElementById("invite-name").focus();
    });
    return;
  }
  let html = `<table class="data-table">
    <thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Status</th>
    <th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
    <tbody>`;
  for (const c of items) {
    html += `<tr>
      <td><strong>${esc(clientName(c))}</strong></td>
      <td>${esc(c.email)}</td>
      <td>${clientStatusBadge(c)}</td>
      <td><button type="button" class="btn btn--small" data-client="${esc(c.id)}">Open dossier</button></td>
    </tr>`;
  }
  clientsRoot.innerHTML = html + `</tbody></table>`;
  clientsRoot.querySelectorAll("[data-client]").forEach((btn) => {
    btn.addEventListener("click", () => openDossier(btn.dataset.client));
  });
}

let searchTimer = null;
document.getElementById("client-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadClients(e.target.value.trim()).catch((err) => showError(err.message)), 300);
});

// --- dossier ---------------------------------------------------------------------
async function openDossier(clientId) {
  currentClientId = clientId;
  currentTab = "overview";
  dossierSection.hidden = false;
  dossierSection.scrollIntoView({ block: "start" });
  await renderDossier();
}

document.querySelectorAll(".dossier-tabs [role=tab]").forEach((tab) => {
  tab.addEventListener("click", async () => {
    currentTab = tab.dataset.tab;
    document.querySelectorAll(".dossier-tabs [role=tab]").forEach((t) =>
      t.setAttribute("aria-selected", t === tab ? "true" : "false"));
    await renderDossier();
  });
});

async function renderDossier() {
  if (!currentClientId) return;
  dossierRoot.innerHTML = "<p>Loading dossier&hellip;</p>";
  const d = await adminApi.get(`/clients/${encodeURIComponent(currentClientId)}`);
  let html = `<h3>${esc(d.display_name || d.name || d.email)} <span class="portal-list__meta">${esc(d.email)}</span></h3>`;
  if (currentTab === "overview") {
    html += `<dl class="dossier-facts">
      <dt>Status</dt><dd>${esc(d.status)}${d.must_change_password ? " — must choose own password" : ""}</dd>
      <dt>Phone</dt><dd>${esc(d.phone_e164 || "—")}${d.phone_verified_at ? " (verified)" : ""}</dd>
      <dt>2FA</dt><dd>${d.totp_enrolled_at ? "enrolled" : "not enrolled"}</dd>
      <dt>Terms</dt><dd>${d.terms_accepted_at ? "accepted" : "pending"}</dd>
      <dt>Last login</dt><dd>${esc(vancouverDateTime(d.last_login_at))}</dd>
    </dl>
    <div class="form-field">
      <label for="internal-note">Internal note (only you see this)</label>
      <textarea id="internal-note" rows="3" maxlength="5000">${esc(d.internal_note || "")}</textarea>
    </div>
    <button type="button" class="btn btn--small" id="save-note">Save note</button>
    <div class="danger-zone">
      <h4>Security</h4>
      <p class="form-hint">These sign the client out everywhere and are audited. The temporary password is never emailed by the portal — you send it yourself.</p>
      <button type="button" class="btn btn--small" id="revoke-all">Revoke all sessions</button>
      <button type="button" class="btn btn--small btn--danger" id="reset-password">Set temporary password</button>
    </div>`;
  } else if (currentTab === "documents") {
    html += `<ul class="portal-list">` + (d.documents || []).map((doc) =>
      `<li><strong>${esc(doc.original_filename)}</strong>
       <span class="portal-list__meta">${esc(vancouverDateTime(doc.created_at))}${doc.deleted_at ? " · retired" : ""}</span>
       <a class="btn btn--small" href="/jaro-accounting-rebuild/api/v1/admin/clients/${encodeURIComponent(currentClientId)}/documents/${encodeURIComponent(doc.id)}/download">Download</a>
       ${doc.deleted_at ? "" : `<button type="button" class="btn btn--small" data-retire="${esc(doc.id)}">Retire</button>`}
      </li>`).join("") + `</ul>
      <h4>Upload for this client</h4>
      <form id="owner-upload-form">
        <input type="file" id="owner-upload-file" required>
        <select id="owner-upload-category">
          <option value="deliverables">Deliverables</option>
          <option value="correspondence">Correspondence</option>
          <option value="other">Other</option>
        </select>
        <button class="btn btn--small" type="submit">Upload &amp; share</button>
      </form>`;
  } else if (currentTab === "filings") {
    html += `<ul class="portal-list">` + (d.filings || []).map((f) =>
      `<li><strong>${esc(f.kind_label)} ${esc(f.tax_year)}</strong>
       <span class="portal-list__meta">${esc(f.status)}${f.note ? ` — ${esc(f.note)}` : ""}</span>
       <button type="button" class="btn btn--small" data-advance="${esc(f.id)}" data-status="${esc(f.status)}">Advance</button>
      </li>`).join("") + `</ul>
      <h4>New filing</h4>
      <form id="filing-form">
        <select id="filing-kind">
          <option value="personal_t1">Personal T1</option>
          <option value="corporate_t2">Corporate T2</option>
          <option value="nonprofit">Non-profit</option>
          <option value="nil_return">Nil return</option>
          <option value="gst_hst">GST/HST</option>
        </select>
        <input id="filing-year" type="number" value="2025" min="2000" max="2100">
        <button class="btn btn--small" type="submit">Create filing</button>
      </form>`;
  } else if (currentTab === "invoices") {
    html += `<ul class="portal-list">` + (d.invoices || []).map((i) =>
      `<li><strong>${esc(i.invoice_number)}</strong>
       <span class="portal-list__meta">$${(i.total_cents / 100).toFixed(2)} · ${esc(i.status)}</span>
       ${i.status === "issued" ? `<button type="button" class="btn btn--small" data-pay="${esc(i.id)}">Mark paid</button>
       <button type="button" class="btn btn--small" data-void="${esc(i.id)}">Void</button>` : ""}
      </li>`).join("") + `</ul>
      <h4>Issue invoice</h4>
      <form id="invoice-form">
        <div class="form-field"><label>Line items (description | qty | unit cents | tax bps, one per line)</label>
        <textarea id="invoice-lines" rows="3">Tax preparation | 1 | 45000 | 500</textarea></div>
        <div class="form-field"><label for="invoice-memo">Memo</label><input id="invoice-memo" maxlength="2000"></div>
        <button class="btn btn--small" type="submit">Issue invoice</button>
      </form>`;
  } else if (currentTab === "activity") {
    html += `<ul class="portal-list">` + (d.activity || []).map((a) =>
      `<li><span class="portal-list__time">${esc(vancouverDateTime(a.occurred_at || a.at))}</span> ${esc(a.summary || a.action)}</li>`).join("") + `</ul>`;
  }
  dossierRoot.innerHTML = html;
  wireDossier();
}

function wireDossier() {
  const saveNote = document.getElementById("save-note");
  if (saveNote) saveNote.addEventListener("click", async () => {
    const note = document.getElementById("internal-note").value;
    try {
      await adminApi.patch(`/clients/${encodeURIComponent(currentClientId)}`, { json: { internal_note: note } });
      showError(""); errorBox.hidden = true;
    } catch (err) { showError(err.message); }
  });
  const revokeAll = document.getElementById("revoke-all");
  if (revokeAll) revokeAll.addEventListener("click", async () => {
    if (!confirm("Revoke every session for this client? They'll need to sign in again.")) return;
    try {
      const res = await adminApi.post(`/clients/${encodeURIComponent(currentClientId)}/sessions/revoke-all`);
      alert(`Revoked ${res.revoked_count} session(s).`);
    } catch (err) { showError(err.message); }
  });
  const resetPassword = document.getElementById("reset-password");
  if (resetPassword) resetPassword.addEventListener("click", async () => {
    const temp = prompt("New temporary password for this client (min 12 characters).\nYou will email it to them yourself — the portal never sends passwords.");
    if (!temp) return;
    const reason = prompt("Reason for the reset (required, audited):");
    if (!reason) return;
    if (!confirm("This sets a new temporary password and signs the client out everywhere. Continue?")) return;
    try {
      await adminApi.post(`/clients/${encodeURIComponent(currentClientId)}/password/reset`, {
        json: { temp_password: temp, reason },
      });
      alert("Temporary password set. Email it to the client yourself — they must choose their own password on next sign-in.");
      await renderDossier();
    } catch (err) { showError(err.message); }
  });
  dossierRoot.querySelectorAll("[data-retire]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await adminApi.post(`/clients/${encodeURIComponent(currentClientId)}/documents/${encodeURIComponent(btn.dataset.retire)}/retire`, { json: { retired: true } });
        await renderDossier();
      } catch (err) { showError(err.message); }
    });
  });
  const uploadForm = document.getElementById("owner-upload-form");
  if (uploadForm) uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = document.getElementById("owner-upload-file").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("type_code", document.getElementById("owner-upload-category").value);
    try {
      const res = await fetch(`/api/v1/admin/clients/${encodeURIComponent(currentClientId)}/documents`, {
        method: "POST", body: form, credentials: "include",
        headers: { "Idempotency-Key": idempotencyKey() },
      });
      if (!res.ok) throw new Error(`Upload failed (${res.status}).`);
      await renderDossier();
    } catch (err) { showError(err.message); }
  });
  const filingForm = document.getElementById("filing-form");
  if (filingForm) filingForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await adminApi.post("/filings", {
        json: {
          client_id: currentClientId,
          kind_code: document.getElementById("filing-kind").value,
          tax_year: parseInt(document.getElementById("filing-year").value, 10),
        },
        headers: { "Idempotency-Key": idempotencyKey() },
      });
      await renderDossier();
    } catch (err) { showError(err.message); }
  });
  dossierRoot.querySelectorAll("[data-advance]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const next = prompt("New status (collecting, awaiting_info, preparing, filed, confirmed):", btn.dataset.status);
      if (!next) return;
      const note = prompt("Message to client (optional):") || undefined;
      try {
        await adminApi.patch(`/filings/${encodeURIComponent(btn.dataset.advance)}/status`, { json: { status: next, note } });
        await renderDossier();
      } catch (err) { showError(err.message); }
    });
  });
  const invoiceForm = document.getElementById("invoice-form");
  if (invoiceForm) invoiceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const lines = document.getElementById("invoice-lines").value.split("\n")
      .map((l) => l.trim()).filter(Boolean).map((l) => {
        const [description, qty, unit, tax] = l.split("|").map((s) => s.trim());
        return { description, quantity: parseFloat(qty), unit_cents: parseInt(unit, 10), tax_rate_bps: parseInt(tax || "0", 10) };
      });
    try {
      await adminApi.post("/invoices", {
        json: { client_id: currentClientId, lines, memo: document.getElementById("invoice-memo").value || undefined },
        headers: { "Idempotency-Key": idempotencyKey() },
      });
      await renderDossier();
    } catch (err) { showError(err.message); }
  });
  dossierRoot.querySelectorAll("[data-pay]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const note = prompt("Payment method note (e.g. e-transfer received 2026-10-06):");
      if (!note) return;
      try {
        await adminApi.post(`/invoices/${encodeURIComponent(btn.dataset.pay)}/pay`, { json: { pay_method_note: note } });
        await renderDossier();
      } catch (err) { showError(err.message); }
    });
  });
  dossierRoot.querySelectorAll("[data-void]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const reason = prompt("Void reason (required, audited):");
      if (!reason) return;
      try {
        await adminApi.post(`/invoices/${encodeURIComponent(btn.dataset.void)}/void`, { json: { reason } });
        await renderDossier();
      } catch (err) { showError(err.message); }
    });
  });
}

// --- create client account -----------------------------------------------------------
// Direct provisioning (no invitation emails): Jared sets a temporary password
// and emails it to the client personally. POST /invitations is the product's
// direct-provisioning endpoint — the invitation row is only the creation
// record / audit trail, marked accepted immediately.
document.getElementById("invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const createdNote = document.getElementById("client-created-note");
  createdNote.hidden = true;
  const body = {
    client_name: document.getElementById("invite-name").value.trim(),
    email: document.getElementById("invite-email").value.trim(),
  };
  const phone = document.getElementById("invite-phone").value.trim();
  if (phone) body.phone_e164 = phone;
  const tempPassword = document.getElementById("invite-temp-password").value;
  if (!tempPassword || tempPassword.length < 12) {
    showError("Enter a temporary password of at least 12 characters.");
    return;
  }
  body.temp_password = tempPassword;
  try {
    await adminApi.post("/invitations", { json: body, headers: { "Idempotency-Key": idempotencyKey() } });
    e.target.reset();
    createdNote.hidden = false;
    createdNote.textContent = `Account created for ${body.client_name} (${body.email}). Email them the temporary password yourself — they'll choose their own on first sign-in.`;
    await loadClients();
    await loadStats();
  } catch (err) { showError(err.message); }
});

// --- audit -------------------------------------------------------------------------
async function loadAudit() {
  const res = await adminApi.get("/audit?page=1&per_page=50");
  const items = res.items || [];
  auditRoot.innerHTML = `<ul class="portal-list">` + items.map((a) =>
    `<li><span class="portal-list__time">${esc(vancouverDateTime(a.occurred_at))}</span>
     <strong>${esc(a.action)}</strong> <span class="portal-list__meta">${esc(a.actor || "")} ${esc(a.entity_type || "")}</span></li>`
  ).join("") + `</ul>`;
}

// --- init ----------------------------------------------------------------------------
(async function init() {
  initPortalChrome();
  try {
    await loadStats();
    await loadClients();
    await loadAudit();
  } catch (err) {
    if (err instanceof ApiRequestError && (err.status === 401 || err.status === 403)) {
      window.location.href = "/jaro-accounting-rebuild/portal/login/";
      return;
    }
    showError(err.message || "Couldn't load the console.");
  }
})();
