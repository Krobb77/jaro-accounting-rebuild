/* /portal/invoices/ — invoice list + detail + PDF download. */
import {
  portalApi, ApiRequestError, startSessionWatch, requireSession,
  esc, vancouverDateTime,
} from "./portal-api.js";

const root = document.getElementById("invoices-root");
const errorBox = document.getElementById("portal-error");
const banner = document.getElementById("session-banner");
const extendBtn = document.getElementById("session-extend");

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("portal-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = "/jaro-accounting-rebuild/portal/login/";
});

function money(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

async function loadInvoices() {
  const res = await portalApi.get("/invoices");
  const items = res.items || [];
  if (!items.length) {
    root.innerHTML = "<p class=\"portal-empty\">No invoices yet.</p>";
    return;
  }
  let html = `<ul class="portal-list">`;
  for (const inv of items) {
    const overdue = inv.status === "issued" && inv.due_at && new Date(inv.due_at) < new Date();
    html += `<li>
      <strong>${esc(inv.invoice_number)}</strong>
      <span class="portal-list__meta">${money(inv.total_cents)} &middot;
      due ${esc(vancouverDateTime(inv.due_at))} &middot; ${esc(inv.status)}${overdue ? " &middot; overdue" : ""}</span>
      <button type="button" class="btn btn--small" data-invoice="${esc(inv.id)}">View</button>
    </li>`;
  }
  root.innerHTML = html + `</ul><div id="invoice-detail"></div>`;
  root.querySelectorAll("[data-invoice]").forEach((btn) => {
    btn.addEventListener("click", () => showDetail(btn.dataset.invoice));
  });
}

async function showDetail(id) {
  const detailEl = document.getElementById("invoice-detail");
  detailEl.innerHTML = "<p>Loading invoice&hellip;</p>";
  try {
    const inv = await portalApi.get(`/invoices/${encodeURIComponent(id)}`);
    let html = `<section class="portal-card-mini" aria-label="Invoice ${esc(inv.invoice_number)}">
      <h2>Invoice ${esc(inv.invoice_number)}</h2>
      <p>Issued ${esc(vancouverDateTime(inv.issued_at))} &middot; Due ${esc(vancouverDateTime(inv.due_at))} &middot; Status: ${esc(inv.status)}</p>
      <table class="invoice-table">
        <thead><tr><th scope="col">Description</th><th scope="col">Qty</th><th scope="col">Unit</th><th scope="col">Tax</th><th scope="col">Total</th></tr></thead>
        <tbody>`;
    for (const ln of inv.lines) {
      html += `<tr><td>${esc(ln.description)}</td><td>${esc(ln.quantity)}</td>
        <td>${money(ln.unit_cents)}</td>
        <td>${ln.tax_rate_bps ? `${(ln.tax_rate_bps / 100).toFixed(2)}%` : "—"}</td>
        <td>${money(ln.line_total_cents)}</td></tr>`;
    }
    html += `</tbody></table>
      <p>Subtotal: ${money(inv.subtotal_cents)} &middot; Tax: ${money(inv.tax_cents)}</p>
      <p><strong>Total due: ${money(inv.total_cents)}</strong></p>
      ${inv.memo ? `<p>Memo: ${esc(inv.memo)}</p>` : ""}
      <p><a class="btn btn--small" href="/jaro-accounting-rebuild/api/v1/portal/invoices/${encodeURIComponent(inv.id)}/pdf">Download PDF</a></p>
    </section>`;
    detailEl.innerHTML = html;
    detailEl.scrollIntoView({ block: "nearest" });
  } catch (err) {
    detailEl.innerHTML = `<p class="error-summary">${esc(err.message || "Couldn't load the invoice.")}</p>`;
  }
}

(async function init() {
  await requireSession();
  startSessionWatch({ bannerEl: banner, extendBtn });
  try {
    await loadInvoices();
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = "/jaro-accounting-rebuild/portal/login?reason=session-expired";
      return;
    }
    showError(err.message || "Couldn't load your invoices.");
    root.innerHTML = "";
  }
})();
