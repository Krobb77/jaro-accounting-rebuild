/* Manage booking — tokenized reschedule / cancel page.
 *
 * The token comes from the URL path /booking/manage/{token} (a static
 * page served for that prefix; needs a `try_files /booking-manage.html`
 * rewrite at the web server — see Phase-3 frontend decisions).
 *
 * Invalid/expired/revoked tokens render the same generic 404 panel, so
 * the page never distinguishes "no such token" from "cancelled" (A5).
 *
 * Token sources: the /booking/manage/{token} path (production rewrite)
 * or the ?token= query parameter (static hosts such as the Pages preview).
 */

import { ApiRequestError, apiFetch, formatSlot } from "./booking-api.js";

const MANAGE_BASE = "/manage"; // apiFetch() prepends /api/v1/booking

const token = new URLSearchParams(window.location.search).get("token") ||
  window.location.pathname.split("/").filter(Boolean).pop() || "";
const root = document.getElementById("manage-root");
const errorBox = document.getElementById("manage-error");

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  errorBox.scrollIntoView({ block: "nearest" });
}
function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function genericNotFound() {
  root.innerHTML = `
    <div class="notice-box">
      <p class="notice-title">This link isn't valid</p>
      <p class="notice-text">The link may have expired, or the booking may have been
      cancelled or rescheduled (each change issues a new link). Check your latest
      confirmation email, or call <a href="tel:+17782274891">1-(778)-227-4891</a>
      and we'll sort it out.</p>
    </div>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

let booking = null;
let newSlot = null;
let rescheduleKey = null;
let viewYear, viewMonth;

function renderBooking() {
  clearError();
  const f = formatSlot(booking.slot_start, booking.slot_end, booking.guest_timezone);
  const actions = booking.allowed_actions || [];
  const canReschedule = actions.includes("reschedule");
  const canCancel = actions.includes("cancel");
  const cutoffLine = (canReschedule || canCancel)
    ? `<p class="microcopy">Changes are free until 24 hours before your appointment.</p>`
    : `<p class="microcopy">This appointment is within 24 hours — to change it, ` +
      `please call <a href="tel:+17782274891">1-(778)-227-4891</a>.</p>`;
  root.innerHTML = `
    <dl class="bw-review">
      <div><dt>Reference</dt><dd><strong>${esc(booking.reference)}</strong></dd></div>
      <div><dt>When</dt><dd>${esc(f.dateLong)} · ${esc(f.timeRange)} (${esc(f.tzLabel)})<br>
        <span class="microcopy">${esc(f.firmLine)}</span></dd></div>
      <div><dt>Email</dt><dd>${esc(booking.guest_email)}</dd></div>
      <div><dt>Status</dt><dd>${esc(booking.status)}</dd></div>
    </dl>
    ${cutoffLine}
    <div class="bw-manage-actions">
      ${canReschedule ? `<button type="button" class="btn" id="m-reschedule-toggle">Reschedule</button>` : ""}
      ${canCancel ? `<button type="button" class="btn btn-ghost" id="m-cancel-toggle">Cancel booking</button>` : ""}
    </div>
    <div id="m-reschedule-panel" hidden>
      <h2 tabindex="-1" id="m-reschedule-title">Pick a new time</h2>
      <div class="bw-calendar-nav">
        <button type="button" id="m-prev-month" aria-label="Previous month">&larr;</button>
        <div><select id="m-year" aria-label="Year"></select>
        <select id="m-month" aria-label="Month"></select></div>
        <button type="button" id="m-next-month" aria-label="Next month">&rarr;</button>
      </div>
      <div id="m-calendar-status" class="bw-status" role="status"></div>
      <div id="m-calendar-grid" class="bw-calendar"></div>
      <div id="m-slots" class="bw-slots"></div>
      <p class="microcopy" id="m-new-slot-line" hidden></p>
      <button type="button" class="btn" id="m-reschedule-submit" hidden>Confirm new time</button>
    </div>
    <div id="m-cancel-panel" hidden>
      <h2 tabindex="-1">Cancel this booking</h2>
      <p>Your slot will be released and we'll send a cancellation notice to your email.</p>
      <label class="form-field">Reason for cancelling (optional)
        <textarea id="m-cancel-reason" rows="3" maxlength="500"></textarea>
      </label>
      <button type="button" class="btn btn-danger" id="m-cancel-submit">Cancel booking</button>
    </div>`;
  const rt = document.getElementById("m-reschedule-toggle");
  if (rt) rt.onclick = () => togglePanel("reschedule");
  const ct = document.getElementById("m-cancel-toggle");
  if (ct) ct.onclick = () => togglePanel("cancel");
}

function togglePanel(which) {
  const rp = document.getElementById("m-reschedule-panel");
  const cp = document.getElementById("m-cancel-panel");
  if (which === "reschedule") {
    const show = rp.hidden;
    rp.hidden = !show;
    cp.hidden = true;
    if (show) initReschedulePicker();
  } else {
    const show = cp.hidden;
    cp.hidden = !show;
    rp.hidden = true;
  }
}

/* ---------------- reschedule picker (compact) ---------------- */

function initReschedulePicker() {
  const now = new Date();
  viewYear = now.getFullYear();
  viewMonth = now.getMonth() + 1;
  const ys = document.getElementById("m-year");
  const ms = document.getElementById("m-month");
  ys.innerHTML = "";
  for (let y = now.getFullYear(); y <= now.getFullYear() + 1; y++) ys.appendChild(new Option(String(y), String(y)));
  ms.innerHTML = "";
  ["January","February","March","April","May","June","July","August","September","October","November","December"]
    .forEach((n, i) => ms.appendChild(new Option(n, String(i + 1))));
  ys.value = String(viewYear);
  ms.value = String(viewMonth);
  ys.onchange = (e) => { viewYear = Number(e.target.value); void loadDays(); };
  ms.onchange = (e) => { viewMonth = Number(e.target.value); void loadDays(); };
  document.getElementById("m-prev-month").onclick = () => shift(-1);
  document.getElementById("m-next-month").onclick = () => shift(1);
  document.getElementById("m-reschedule-submit").onclick = submitReschedule;
  void loadDays();
}

function shift(delta) {
  let y = viewYear, m = viewMonth + delta;
  if (m < 1) { m = 12; y -= 1; }
  if (m > 12) { m = 1; y += 1; }
  viewYear = y; viewMonth = m;
  document.getElementById("m-year").value = String(y);
  document.getElementById("m-month").value = String(m);
  void loadDays();
}

async function loadDays() {
  const grid = document.getElementById("m-calendar-grid");
  const status = document.getElementById("m-calendar-status");
  grid.innerHTML = "";
  status.textContent = "Loading available days…";
  try {
    const data = await apiFetch(
      `/availability?year=${viewYear}&month=${viewMonth}&tz=${encodeURIComponent(booking.guest_timezone)}`
    );
    status.textContent = "";
    for (const d of data.days || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "bw-day";
      b.dataset.date = d.date;
      b.textContent = String(Number(d.date.slice(8, 10)));
      b.disabled = !d.available;
      b.setAttribute("aria-label", d.date + (d.available ? " — times available" : " — no times available"));
      if (d.available) b.onclick = () => loadNewSlots(d.date);
      grid.appendChild(b);
    }
    if (!(data.days || []).some((d) => d.available)) {
      status.textContent = "No bookable days this month — try another month.";
    }
  } catch (err) {
    status.textContent = "";
    showError(err instanceof ApiRequestError ? err.message : "Couldn't load the calendar.");
  }
}

async function loadNewSlots(date) {
  const box = document.getElementById("m-slots");
  box.innerHTML = '<p class="bw-status" role="status">Loading times…</p>';
  try {
    const data = await apiFetch(
      `/slots?date=${date}&tz=${encodeURIComponent(booking.guest_timezone)}`
    );
    box.innerHTML = "";
    for (const s of data.slots || []) {
      if (s.start === booking.slot_start) continue; // the current slot isn't "new"
      const b = document.createElement("button");
      b.type = "button";
      b.className = "bw-slot";
      const f = formatSlot(s.start, s.end, booking.guest_timezone);
      b.innerHTML = `<span class="bw-slot-time">${esc(f.timeRange)}</span>` +
        `<span class="bw-slot-firm">${esc(f.firmLine)}</span>`;
      b.setAttribute("aria-label", s.aria_label || `Select ${f.timeRange}`);
      b.onclick = () => {
        newSlot = s;
        // Fresh idempotency key per reschedule attempt; kept across
        // network-error retries of the same submission (Phase-0 D-11).
        rescheduleKey = crypto.randomUUID();
        document.querySelectorAll("#m-slots .bw-slot").forEach((x) => x.classList.remove("is-selected"));
        b.classList.add("is-selected");
        const line = document.getElementById("m-new-slot-line");
        line.hidden = false;
        line.textContent = `New time: ${f.dateLong} · ${f.timeRange} (${f.tzLabel})`;
        const sub = document.getElementById("m-reschedule-submit");
        sub.hidden = false;
        sub.focus();
      };
      box.appendChild(b);
    }
    if (!box.children.length) box.innerHTML = "<p>No other times that day.</p>";
  } catch (err) {
    showError(err instanceof ApiRequestError ? err.message : "Couldn't load times.");
  }
}

async function submitReschedule() {
  if (!newSlot) return;
  const btn = document.getElementById("m-reschedule-submit");
  btn.disabled = true;
  try {
    const body = await apiFetch(`${MANAGE_BASE}/${token}/reschedule`, {
      method: "POST",
      json: { new_slot_start: newSlot.start },
      headers: { "Idempotency-Key": rescheduleKey || crypto.randomUUID() },
    });
    // New reference, new links — hand off to the thank-you page.
    sessionStorage.setItem("jaro.booking_result", JSON.stringify(body));
    window.location.href = "/thank-you-for-your-booking/";
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "slot_taken") {
      showError("That time was just taken — please pick another.");
      newSlot = null;
      document.getElementById("m-reschedule-submit").hidden = true;
      document.getElementById("m-new-slot-line").hidden = true;
      return;
    }
    showError(err instanceof ApiRequestError ? err.message : "Couldn't reschedule.");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- cancel ---------------- */

async function submitCancel() {
  const btn = document.getElementById("m-cancel-submit");
  btn.disabled = true;
  try {
    const body = await apiFetch(`${MANAGE_BASE}/${token}/cancel`, {
      method: "POST",
      json: { reason: document.getElementById("m-cancel-reason").value.trim() || null },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    root.innerHTML = `
      <div class="notice-box">
        <p class="notice-title">Booking cancelled</p>
        <p class="notice-text">Reference <strong>${esc(body.reference)}</strong> has been cancelled.
        A cancellation notice is queued to your email. We'd still love to talk — call
        <a href="tel:+17782274891">1-(778)-227-4891</a> to rebook whenever you're ready.</p>
      </div>`;
  } catch (err) {
    showError(err instanceof ApiRequestError ? err.message : "Couldn't cancel the booking.");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- init ---------------- */

async function init() {
  if (!/^[A-Za-z0-9_-]{20,}$/.test(token)) {
    genericNotFound();
    return;
  }
  try {
    booking = await apiFetch(`${MANAGE_BASE}/${token}`);
  } catch (err) {
    genericNotFound(); // A5: never distinguish invalid / expired / revoked
    return;
  }
  renderBooking();
  document.getElementById("m-cancel-submit").onclick = submitCancel;
}

void init();
