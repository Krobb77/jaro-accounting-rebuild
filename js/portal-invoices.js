/* /portal/invoices/ — invoice list + detail + PDF download. */
import {
  portalApi, ApiRequestError, startSessionWatch, requireSession,
  initPortalChrome, esc, vancouverDateTime,
} from "./portal-api.js";
import { withBase } from "./site-base.js";

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
  window.location.href = withBase("/portal/login/");
});

function money(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

/* P27: financial data in aligned rows — numbers right-aligned, status badges. */
const STATUS_BADGE_CLASS = {
  issued: "badge--issued",
  paid: "badge--paid",
  void: "badge--void",
};

function statusBadge(inv, overdue) {
  if (overdue) return `<span class="badge badge--overdue">Overdue</span>`;
  const cls = STATUS_BADGE_CLASS[inv.status] || "badge--void";
  const label = inv.status.charAt(0).toUpperCase() + inv.status.slice(1);
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

async function loadInvoices() {
  const res = await portalApi.get("/invoices");
  const items = res.items || [];
  if (!items.length) {
    root.innerHTML = `<p class="portal-empty">No invoices yet. Invoices Jared sends you will appear here.</p>
      <p class="portal-empty">Payment happens outside the portal — each invoice lists how to pay.</p>`;
    return;
  }
  let html = `<table class="data-table">
    <thead><tr><th scope="col">Invoice</th><th scope="col">Due</th>
    <th scope="col" class="num">Amount</th><th scope="col">Status</th>
    <th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
    <tbody>`;
  for (const inv of items) {
    const overdue = inv.status === "issued" && inv.due_at && new Date(inv.due_at) < new Date();
    html += `<tr>
      <td><strong>${esc(inv.invoice_number)}</strong></td>
      <td>${esc(vancouverDateTime(inv.due_at))}</td>
      <td class="num">${money(inv.total_cents)}</td>
      <td>${statusBadge(inv, overdue)}</td>
      <td><button type="button" class="btn btn--small" data-invoice="${esc(inv.id)}">View</button></td>
    </tr>`;
  }
  root.innerHTML = html + `</tbody></table><div id="invoice-detail"></div>`;
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
        <thead><tr><th scope="col">Description</th><th scope="col" class="num">Qty</th><th scope="col" class="num">Unit</th><th scope="col" class="num">Tax</th><th scope="col" class="num">Total</th></tr></thead>
        <tbody>`;
    for (const ln of inv.lines) {
      html += `<tr><td>${esc(ln.description)}</td><td class="num">${esc(ln.quantity)}</td>
        <td class="num">${money(ln.unit_cents)}</td>
        <td class="num">${ln.tax_rate_bps ? `${(ln.tax_rate_bps / 100).toFixed(2)}%` : "—"}</td>
        <td class="num">${money(ln.line_total_cents)}</td></tr>`;
    }
    html += `</tbody></table>
      <p>Subtotal: ${money(inv.subtotal_cents)} &middot; Tax: ${money(inv.tax_cents)}</p>
      <p><strong>Total due: ${money(inv.total_cents)}</strong></p>
      ${inv.memo ? `<p>Memo: ${esc(inv.memo)}</p>` : ""}
      <p><a class="btn btn--small" href="/api/v1/portal/invoices/${encodeURIComponent(inv.id)}/pdf">Download PDF</a></p>
    </section>`;
    detailEl.innerHTML = html;
    detailEl.scrollIntoView({ block: "nearest" });
  } catch (err) {
    detailEl.innerHTML = `<p class="error-summary">${esc(err.message || "Couldn't load the invoice.")}</p>`;
  }
}

(async function init() {
  initPortalChrome();
  await requireSession();
  startSessionWatch({ bannerEl: banner, extendBtn });
  try {
    await loadInvoices();
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = withBase("/portal/login?reason=session-expired");
      return;
    }
    showError(err.message || "Couldn't load your invoices.");
    root.innerHTML = "";
  }
})();
