/* Thank-you page — renders the confirmation dynamically from the
 * widget's stored confirm response (sessionStorage). The response is the
 * backend's frozen projection: {reference, slot_start, slot_end,
 * guest_timezone, email_masked, queued_count, reschedule_url, cancel_url,
 * ics_url, replayed}. If the visitor lands here without a stored result
 * (direct visit, new tab, expired session), the honest static copy stays
 * visible instead.
 */

import { STORAGE, formatSlot } from "./booking-api.js";

const raw = sessionStorage.getItem(STORAGE.result);
if (raw) {
  let r = null;
  try {
    r = JSON.parse(raw);
  } catch {
    r = null;
  }
  if (r && r.reference && r.slot_start && r.slot_end && r.guest_timezone) {
    const dynamic = document.getElementById("bt-dynamic");
    const staticEl = document.getElementById("bt-static");
    if (dynamic && staticEl) {
      staticEl.hidden = true;
      dynamic.hidden = false;

      const f = formatSlot(r.slot_start, r.slot_end, r.guest_timezone);
      document.getElementById("bt-reference").textContent = r.reference;
      document.getElementById("bt-when").textContent =
        `${f.dateLong} · ${f.timeRange} (${f.tzLabel})`;
      document.getElementById("bt-firm").textContent = f.firmLine;
      if (r.email_masked) {
        document.getElementById("bt-email").textContent = r.email_masked;
      } else {
        document.getElementById("bt-email-row").hidden = true;
      }
      const queued = Number(r.queued_count) || 0;
      document.getElementById("bt-queued").textContent =
        `Your confirmation email is queued (${queued} message${queued === 1 ? "" : "s"} queued).`;

      const resched = document.getElementById("bt-reschedule");
      const cancel = document.getElementById("bt-cancel");
      if (r.reschedule_url) {
        resched.href = r.reschedule_url;
      } else {
        resched.hidden = true;
      }
      if (r.cancel_url) {
        cancel.href = r.cancel_url;
      } else {
        cancel.hidden = true;
      }
      if (r.ics_url) {
        const ics = document.getElementById("bt-ics");
        ics.href = r.ics_url;
        ics.hidden = false;
      }
      if (r.replayed) {
        const note = document.getElementById("bt-replayed-note");
        if (note) note.hidden = false;
      }
    }
  }
}
