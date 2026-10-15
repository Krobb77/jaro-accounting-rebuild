/* /portal/admin/ — Jared's owner console. Owner role only (403 otherwise). */
import {
  adminApi, portalApi, ApiRequestError,
  idempotencyKey, esc, vancouverDateTime,
} from "./portal-api.js";

const errorBox = document.getElementById("admin-error");
const clientsRoot = document.getElementById("clients-root");
const dossierSection = document.getElementById("dossier-section");
const dossierRoot = document.getElementById("dossier-root");
const invitesRoot = document.getElementById("invites-root");
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

// --- clients -------------------------------------------------------------------
async function loadClients(search = "") {
  const params = new URLSearchParams({ page: "1", per_page: "50" });
  if (search) params.set("search", search);
  const res = await adminApi.get(`/clients?${params}`);
  const items = res.items || [];
  if (!items.length) {
    clientsRoot.innerHTML = "<p class=\"portal-empty\">No clients yet. Send an invitation below.</p>";
    return;
  }
  let html = `<ul class="portal-list">`;
  for (const c of items) {
    html += `<li><strong>${esc(c.display_name || c.email)}</strong>
      <span class="portal-list__meta">${esc(c.email)} &middot; ${esc(c.status)}</span>
      <button type="button" class="btn btn--small" data-client="${esc(c.id)}">Open dossier</button>
    </li>`;
  }
  clientsRoot.innerHTML = html + `</ul>`;
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
  let html = `<h3>${esc(d.display_name || d.email)} <span class="portal-list__meta">${esc(d.email)}</span></h3>`;
  if (currentTab === "overview") {
    html += `<dl class="dossier-facts">
      <dt>Status</dt><dd>${esc(d.status)}</dd>
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
    <hr>
    <h4>Security</h4>
    <button type="button" class="btn btn--small" id="revoke-all">Revoke all sessions</button>
    ${d.totp_enrolled_at ? `<button type="button" class="btn btn--small" id="disable-totp">Disable two-step verification</button>` : ""}`;
  } else if (currentTab === "documents") {
    html += `<ul class="portal-list">` + (d.documents || []).map((doc) =>
      `<li><strong>${esc(doc.original_filename)}</strong>
       <span class="portal-list__meta">${esc(vancouverDateTime(doc.created_at))}${doc.deleted_at ? " · retired" : ""}</span>
       <a class="btn btn--small" href="/api/v1/admin/clients/${encodeURIComponent(currentClientId)}/documents/${encodeURIComponent(doc.id)}/download">Download</a>
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
  const disableTotp = document.getElementById("disable-totp");
  if (disableTotp) disableTotp.addEventListener("click", async () => {
    const reason = prompt("Reason for disabling two-step verification (required, audited):");
    if (!reason) return;
    try {
      await adminApi.post(`/clients/${encodeURIComponent(currentClientId)}/2fa/disable`, { json: { reason } });
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

// --- invitations -------------------------------------------------------------------
async function loadInvites() {
  const res = await adminApi.get("/invitations?page=1&per_page=25");
  const items = res.items || [];
  if (!items.length) {
    invitesRoot.innerHTML = "<p class=\"portal-empty\">No invitations yet.</p>";
    return;
  }
  let html = `<ul class="portal-list">`;
  for (const inv of items) {
    html += `<li><strong>${esc(inv.client_name)}</strong>
      <span class="portal-list__meta">${esc(inv.email)} &middot; ${esc(inv.status)} &middot; expires ${esc(vancouverDateTime(inv.expires_at))}</span>
      ${(inv.status === "created" || inv.status === "emailed") ? `
      <button type="button" class="btn btn--small" data-resend="${esc(inv.id)}">Resend</button>
      <button type="button" class="btn btn--small" data-revoke="${esc(inv.id)}">Revoke</button>` : ""}
    </li>`;
  }
  invitesRoot.innerHTML = html + `</ul>`;
  invitesRoot.querySelectorAll("[data-resend]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await adminApi.post(`/invitations/${encodeURIComponent(btn.dataset.resend)}/resend`, { headers: { "Idempotency-Key": idempotencyKey() } });
        await loadInvites();
      } catch (err) { showError(err.message); }
    });
  });
  invitesRoot.querySelectorAll("[data-revoke]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Revoke this invitation? The link will stop working.")) return;
      try {
        await adminApi.post(`/invitations/${encodeURIComponent(btn.dataset.revoke)}/revoke`, {
          json: {}, headers: { "Idempotency-Key": idempotencyKey() },
        });
        await loadInvites();
      } catch (err) { showError(err.message); }
    });
  });
}

document.getElementById("invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const body = {
    client_name: document.getElementById("invite-name").value.trim(),
    email: document.getElementById("invite-email").value.trim(),
  };
  const phone = document.getElementById("invite-phone").value.trim();
  if (phone) body.phone_e164 = phone;
  try {
    await adminApi.post("/invitations", { json: body, headers: { "Idempotency-Key": idempotencyKey() } });
    e.target.reset();
    await loadInvites();
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
  try {
    await loadClients();
    await loadInvites();
    await loadAudit();
  } catch (err) {
    if (err instanceof ApiRequestError && (err.status === 401 || err.status === 403)) {
      window.location.href = "/jaro-accounting-rebuild/portal/login/";
      return;
    }
    showError(err.message || "Couldn't load the console.");
  }
})();
