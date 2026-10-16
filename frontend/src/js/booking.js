/* Booking widget — free 30-minute consultation.
 *
 * Steps: timezone (modal) → day (calendar) → time (slots) → interstitial
 * <dialog> → your details (form) → verification code → review → confirm →
 * thank-you page (rendered from the stored confirm response).
 *
 * The backend owns all availability truth; this script never invents
 * slots and re-fetches after any 409 so a taken slot can be re-picked.
 */

import {
  ApiRequestError, apiFetch, COUNTRIES, RELATIONSHIPS, STORAGE,
  allTimezones, formatSlot, parseISODate,
} from "./booking-api.js";
import { withBase } from "./site-base.js";

const $ = (id) => document.getElementById(id);
const widget = $("booking-widget");
if (!widget) {
  throw new Error("booking widget: #booking-widget not found");
}

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

const state = {
  config: null,
  timezone: null,
  year: null,
  month: null, // 1-12
  day: null, // "YYYY-MM-DD"
  slot: null, // {start, end} ISO strings
  intentId: null,
  formValues: {},
  idempotencyKey: null,
  resendAvailableAt: 0,
  resendTimer: null,
};

const STEPS = ["calendar", "slots", "form", "code", "review"];

/* ------------------------------------------------------------------ */
/* Error summary (route-level: 503 / network / unexpected)             */
/* ------------------------------------------------------------------ */

function showError(message) {
  const el = $("bw-error");
  el.textContent = message;
  el.hidden = false;
  el.scrollIntoView({ block: "nearest" });
}

function clearError() {
  const el = $("bw-error");
  el.hidden = true;
  el.textContent = "";
}

function handleRouteError(err, context) {
  if (err instanceof ApiRequestError && err.code === "calendar_unavailable") {
    // Fail-closed upstream: be explicit, offer retry + the human path.
    showCalendarError();
    showError(
      "We couldn't check Jared's live calendar just now, so no times are shown. " +
      "This is deliberate — we'd rather show nothing than a time that isn't real. " +
      "Call [[contact.phone]] and we'll book you in directly."
    );
    return;
  }
  if (err instanceof ApiRequestError) {
    showError(`${context}: ${err.message}`);
    return;
  }
  showError(`${context}: couldn't reach the booking service. Check your connection and try again.`);
}

/* ------------------------------------------------------------------ */
/* Step navigation                                                     */
/* ------------------------------------------------------------------ */

// P12: the visible step indicator tracks "calendar → slots → form"
// while the script's internal steps are finer-grained (code + review
// are still "Your details" from the visitor's point of view).
const STEP_INDICES = { calendar: 0, slots: 1, form: 2, code: 2, review: 2 };

function updateStepIndicator(name) {
  const nav = $("bw-steps");
  if (!nav) return;
  const idx = STEP_INDICES[name] ?? 0;
  nav.hidden = false;
  const items = [...nav.querySelectorAll("li")];
  items.forEach((li, i) => {
    if (i === idx) {
      li.setAttribute("aria-current", "step");
      li.classList.remove("is-done");
    } else {
      li.removeAttribute("aria-current");
      li.classList.toggle("is-done", i < idx);
    }
  });
}

function showStep(name) {
  clearError();
  for (const s of STEPS) {
    const el = $(`bw-step-${s}`);
    el.hidden = s !== name;
  }
  updateStepIndicator(name);
  const heading = $(`bw-${name}-title`);
  if (heading) heading.focus({ preventScroll: true });
  document.getElementById(`bw-step-${name}`).scrollIntoView({ block: "start", behavior: "smooth" });
}

/* ------------------------------------------------------------------ */
/* Timezone modal (timezone-first)                                     */
/* ------------------------------------------------------------------ */

const tzDialog = $("bw-tz-dialog");
const tzCountry = $("bw-tz-country");
const tzSelect = $("bw-tz-select");

function buildTimezoneOptions(countryName) {
  const country = COUNTRIES.find((c) => c.name === countryName);
  const priority = country ? country.zones : [];
  const rest = allTimezones().filter((z) => !priority.includes(z));
  tzSelect.innerHTML = "";
  if (priority.length) {
    const g = document.createElement("optgroup");
    g.label = countryName;
    for (const z of priority) g.appendChild(new Option(z.replace(/_/g, " "), z));
    tzSelect.appendChild(g);
  }
  const g2 = document.createElement("optgroup");
  g2.label = "All time zones";
  for (const z of rest) g2.appendChild(new Option(z.replace(/_/g, " "), z));
  tzSelect.appendChild(g2);
  const stored = localStorage.getItem(STORAGE.timezone);
  tzSelect.value = stored && [...priority, ...rest].includes(stored)
    ? stored
    : priority[0] || "America/Vancouver";
}

function openTimezoneDialog() {
  tzCountry.innerHTML = "";
  for (const c of COUNTRIES) {
    tzCountry.appendChild(new Option(`${c.name} (${c.dial})`, c.name));
  }
  tzCountry.value = localStorage.getItem(STORAGE.country) || "Canada";
  buildTimezoneOptions(tzCountry.value);
  tzCountry.onchange = () => buildTimezoneOptions(tzCountry.value);
  if (typeof tzDialog.showModal === "function") {
    tzDialog.showModal();
  }
}

function closeTimezoneDialog(confirmed) {
  if (confirmed) {
    state.timezone = tzSelect.value;
    localStorage.setItem(STORAGE.timezone, state.timezone);
    localStorage.setItem(STORAGE.country, tzCountry.value);
  }
  if (tzDialog.open) tzDialog.close();
  if (confirmed) {
    $("bw-tz-prompt").hidden = true;
    updateTimezoneEchoes();
    // A zone change invalidates any picked day/slot (they were rendered
    // in the old zone) — start over at the calendar, honestly.
    state.day = null;
    state.slot = null;
    showStep("calendar");
    void loadCalendar();
  } else if (!state.timezone) {
    // Dismissed without a choice: the page stays readable, the prompt
    // (and nothing else) stays on screen.
    for (const s of STEPS) $(`bw-step-${s}`).hidden = true;
    const nav = $("bw-steps");
    if (nav) nav.hidden = true;
    $("bw-tz-prompt").hidden = false;
  }
}

function updateTimezoneEchoes() {
  for (const id of ["bw-tz-echo", "bw-slots-tz"]) {
    const el = $(id);
    if (el) el.textContent = state.timezone;
  }
}

/* ------------------------------------------------------------------ */
/* Calendar (available-days-only)                                      */
/* ------------------------------------------------------------------ */

function buildYearOptions() {
  const yearSel = $("bw-year");
  yearSel.innerHTML = "";
  const now = new Date();
  const horizonDays = state.config?.horizon_days ?? 90;
  const lastYear = new Date(now.getTime() + horizonDays * 86400000).getFullYear();
  for (let y = now.getFullYear(); y <= lastYear; y++) {
    yearSel.appendChild(new Option(String(y), String(y)));
  }
  return yearSel;
}

function shiftMonth(delta) {
  let y = state.year, m = state.month + delta;
  if (m < 1) { m = 12; y -= 1; }
  if (m > 12) { m = 1; y += 1; }
  const yearSel = $("bw-year");
  if (![...yearSel.options].some((o) => Number(o.value) === y)) return; // past horizon edge
  state.year = y;
  state.month = m;
  yearSel.value = String(y);
  $("bw-month").value = String(m);
  void loadCalendar();
}

function showCalendarError() {
  const grid = $("bw-calendar-grid");
  grid.innerHTML = "";
  const status = $("bw-calendar-status");
  status.innerHTML =
    "We couldn't reach the live calendar, so no days are shown. " +
    '<button type="button" class="linklike" id="bw-calendar-retry">Try again</button> ' +
    "or call [[contact.phone]].";
  const retry = $("bw-calendar-retry");
  if (retry) retry.onclick = () => loadCalendar();
}

async function loadCalendar() {
  const grid = $("bw-calendar-grid");
  const status = $("bw-calendar-status");
  clearError();
  // Loading skeleton.
  grid.innerHTML = "";
  status.textContent = "Loading available days…";
  for (let i = 0; i < 14; i++) {
    const sk = document.createElement("span");
    sk.className = "bw-skeleton";
    sk.setAttribute("aria-hidden", "true");
    grid.appendChild(sk);
  }
  try {
    const data = await apiFetch(
      `/availability?year=${state.year}&month=${state.month}&tz=${encodeURIComponent(state.timezone)}`
    );
    renderCalendar(data.days || []);
    const n = (data.days || []).filter((d) => d.available).length;
    status.textContent = n === 0
      ? "No bookable days this month — try another month."
      : `${n} day${n === 1 ? "" : "s"} with openings this month.`;
  } catch (err) {
    handleRouteError(err, "Couldn't load the calendar");
  }
}

function renderCalendar(days) {
  const grid = $("bw-calendar-grid");
  grid.innerHTML = "";
  const frag = document.createDocumentFragment();
  const weekdayHead = document.createElement("div");
  weekdayHead.className = "bw-weekdays";
  weekdayHead.setAttribute("aria-hidden", "true");
  for (const w of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
    const s = document.createElement("span");
    s.textContent = w;
    weekdayHead.appendChild(s);
  }
  frag.appendChild(weekdayHead);

  const first = parseISODate(`${state.year}-${String(state.month).padStart(2, "0")}-01`);
  // Monday-first offset.
  const leadBlanks = (first.getUTCDay() + 6) % 7;
  for (let i = 0; i < leadBlanks; i++) {
    const b = document.createElement("span");
    b.className = "bw-day bw-day-blank";
    b.setAttribute("aria-hidden", "true");
    frag.appendChild(b);
  }

  const todayStr = new Date().toISOString().slice(0, 10);
  for (const d of days) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "bw-day" + (d.date === state.day ? " is-selected" : "");
    btn.dataset.date = d.date;
    const label = parseISODate(d.date).toLocaleDateString("en-US", {
      weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
    });
    btn.textContent = String(Number(d.date.slice(8, 10)));
    if (d.available) {
      btn.setAttribute("aria-label", `${label} — times available`);
      btn.onclick = () => selectDay(d.date);
    } else {
      btn.disabled = true;
      btn.classList.add("is-disabled");
      btn.setAttribute("aria-label", `${label} — no times available`);
    }
    if (d.date === todayStr) {
      btn.classList.add("is-today");
      btn.setAttribute("aria-label", btn.getAttribute("aria-label") + " (today)");
    }
    frag.appendChild(btn);
  }
  grid.appendChild(frag);
}

async function selectDay(date) {
  state.day = date;
  state.slot = null;
  // Re-mark selection without a refetch.
  for (const b of document.querySelectorAll("#bw-calendar-grid .bw-day[data-date]")) {
    b.classList.toggle("is-selected", b.dataset.date === date);
  }
  showStep("slots");
  await loadSlots();
}

/* ------------------------------------------------------------------ */
/* Slots                                                               */
/* ------------------------------------------------------------------ */

async function loadSlots() {
  const list = $("bw-slots-list");
  const dayLabel = parseISODate(state.day).toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
  });
  $("bw-slots-day").textContent = dayLabel;
  updateTimezoneEchoes();
  list.innerHTML = '<p class="bw-status" role="status">Loading times…</p>';
  try {
    const data = await apiFetch(
      `/slots?date=${state.day}&tz=${encodeURIComponent(state.timezone)}`
    );
    const slots = data.slots || [];
    list.innerHTML = "";
    if (!slots.length) {
      list.innerHTML = "<p>That day just filled up — please pick another day.</p>";
      return;
    }
    for (const s of slots) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "bw-slot";
      const f = formatSlot(s.start, s.end, state.timezone);
      b.innerHTML = `<span class="bw-slot-time">${f.timeRange}</span>` +
        `<span class="bw-slot-firm">${f.firmLine}</span>`;
      b.setAttribute("aria-label", s.aria_label || `Select ${f.timeRange} on ${f.dateLong}`);
      b.onclick = () => openSlotDialog(s);
      list.appendChild(b);
    }
  } catch (err) {
    handleRouteError(err, "Couldn't load times");
  }
}

/* ------------------------------------------------------------------ */
/* Interstitial dialog                                                 */
/* ------------------------------------------------------------------ */

const slotDialog = $("bw-slot-dialog");

function openSlotDialog(s) {
  state.slot = { start: s.start, end: s.end };
  const f = formatSlot(s.start, s.end, state.timezone);
  $("bw-slot-dialog-when").textContent = `${f.dateLong} · ${f.timeRange} (${f.tzLabel})`;
  $("bw-slot-dialog-firm").textContent = f.firmLine;
  if (typeof slotDialog.showModal === "function") {
    slotDialog.showModal();
  }
}

function closeSlotDialog() {
  if (slotDialog.open) slotDialog.close();
}

/* ------------------------------------------------------------------ */
/* Details form                                                        */
/* ------------------------------------------------------------------ */

function buildFormControls() {
  const rel = $("bw-relationship");
  rel.innerHTML = "";
  for (const r of RELATIONSHIPS) rel.appendChild(new Option(r.label, r.value));
  rel.value = "prospective_client";
  const cc = $("bw-country-code");
  cc.innerHTML = "";
  for (const c of COUNTRIES) cc.appendChild(new Option(`${c.name} (${c.dial})`, c.dial));
  cc.value = "+1";
}

function setFieldError(input, message) {
  const err = document.getElementById(`${input.id}-error`);
  input.setAttribute("aria-invalid", "true");
  if (err) {
    err.textContent = message;
    err.hidden = false;
  }
}

function clearFieldErrors() {
  for (const id of ["bw-name", "bw-email", "bw-phone", "bw-discuss"]) {
    const input = $(id);
    input.removeAttribute("aria-invalid");
    const err = document.getElementById(`${id}-error`);
    if (err) {
      err.textContent = "";
      err.hidden = true;
    }
  }
}

function validateForm() {
  clearFieldErrors();
  const v = {
    name: $("bw-name").value.trim(),
    email: $("bw-email").value.trim(),
    phone: $("bw-phone").value.trim(),
    country_code: $("bw-country-code").value,
    relationship: $("bw-relationship").value,
    discuss: $("bw-discuss").value.trim(),
  };
  let firstBad = null;
  const bad = (id, msg) => {
    setFieldError($(id), msg);
    if (!firstBad) firstBad = $(id);
  };
  if (v.name.length < 2 || v.name.length > 100) bad("bw-name", "Please enter your name (2–100 characters).");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v.email)) bad("bw-email", "Please enter a valid email address.");
  if (v.phone.replace(/\D/g, "").length < 7) bad("bw-phone", "Please enter a valid phone number.");
  if (v.discuss.length < 10 || v.discuss.length > 1000) {
    bad("bw-discuss", "Tell us a little about what you'd like to discuss (10–1000 characters).");
  }
  if (firstBad) {
    firstBad.focus();
    return null;
  }
  return v;
}

function restoreFormValues() {
  const v = state.formValues;
  if (!v || !Object.keys(v).length) return;
  $("bw-name").value = v.name || "";
  $("bw-email").value = v.email || "";
  $("bw-phone").value = v.phone || "";
  $("bw-country-code").value = v.country_code || "+1";
  $("bw-relationship").value = v.relationship || "prospective_client";
  $("bw-discuss").value = v.discuss || "";
}

async function submitForm() {
  const values = validateForm();
  if (!values) return;
  const emailChanged = state.formValues.email && state.formValues.email !== values.email;
  state.formValues = values;
  const btn = $("bw-form-submit");
  btn.disabled = true;
  btn.textContent = "Sending code…";
  try {
    // Any re-submit invalidates the previous verification: the backend
    // has no email-change primitive, so a changed (or re-entered) email
    // starts a fresh intent and a fresh code. Passing supersedes_intent_id
    // expires our own previous hold atomically — otherwise our earlier
    // attempt would 409 this re-submit on the same slot.
    const body = await apiFetch("/intents", {
      method: "POST",
      json: {
        slot_start: state.slot.start,
        guest_timezone: state.timezone,
        name: values.name,
        email: values.email,
        phone: values.phone,
        country_code: values.country_code,
        relationship: values.relationship,
        discuss: values.discuss,
        ...(state.intentId ? { supersedes_intent_id: state.intentId } : {}),
      },
    });
    state.intentId = body.intent_id;
    state.resendAvailableAt = Date.now() + 60000;
    startResendCountdown();
    $("bw-code-masked").textContent = body.code_sent_to_masked;
    $("bw-code-input").value = "";
    $("bw-code-error").hidden = true;
    showStep("code");
    if (emailChanged) {
      const note = $("bw-code-note");
      if (note) {
        note.textContent = "Your email changed, so we sent a fresh code to the new address.";
        note.hidden = false;
      }
    }
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "slot_taken") {
      // The slot was taken between listing and submit — re-pick, honestly.
      state.intentId = null;
      showStep("slots");
      await loadSlots();
      showError("That time was just taken — please pick another time.");
      return;
    }
    if (err instanceof ApiRequestError && err.status === 422 && err.fields.length) {
      for (const f of err.fields) {
        const map = { name: "bw-name", email: "bw-email", phone: "bw-phone", discuss: "bw-discuss" };
        if (map[f.field]) setFieldError($(map[f.field]), f.message);
      }
      const first = document.querySelector("#bw-step-form [aria-invalid]");
      if (first) first.focus();
      return;
    }
    handleRouteError(err, "Couldn't start your booking");
  } finally {
    btn.disabled = false;
    btn.textContent = "Send verification code";
  }
}

/* ------------------------------------------------------------------ */
/* Verification code                                                   */
/* ------------------------------------------------------------------ */

function startResendCountdown() {
  const btn = $("bw-code-resend");
  const status = $("bw-code-resend-status");
  clearInterval(state.resendTimer);
  const tick = () => {
    const left = Math.max(0, Math.ceil((state.resendAvailableAt - Date.now()) / 1000));
    if (left <= 0) {
      btn.disabled = false;
      status.textContent = "";
      clearInterval(state.resendTimer);
    } else {
      btn.disabled = true;
      status.textContent = `You can request a new code in ${left}s.`;
    }
  };
  tick();
  state.resendTimer = setInterval(tick, 1000);
}

async function verifyCode() {
  const input = $("bw-code-input");
  const err = $("bw-code-error");
  const code = input.value.trim();
  if (!/^\d{6}$/.test(code)) {
    err.textContent = "Enter the 6-digit code from your email.";
    err.hidden = false;
    input.focus();
    return;
  }
  const btn = $("bw-code-verify");
  btn.disabled = true;
  try {
    const body = await apiFetch(`/intents/${encodeURIComponent(state.intentId)}/verify`, {
      method: "POST",
      json: { code },
    });
    err.hidden = true;
    $("bw-review-verified-note").textContent =
      `Email verified (${body.email_masked}). We'll send your confirmation there.`;
    fillReview();
    showStep("review");
  } catch (e) {
    if (e instanceof ApiRequestError) {
      if (e.code === "invalid_code") {
        err.textContent = `${e.message} Try again, or request a new code below.`;
        err.hidden = false;
        input.select();
        return;
      }
      if (e.code === "code_attempts_exceeded") {
        err.textContent = "Too many wrong tries — request a new code below to continue.";
        err.hidden = false;
        return;
      }
      if (e.code === "code_expired" || e.code === "intent_expired") {
        err.textContent = "That code (or this booking attempt) expired. Start again with a fresh time.";
        err.hidden = false;
        state.intentId = null;
        return;
      }
    }
    handleRouteError(e, "Couldn't verify the code");
  } finally {
    btn.disabled = false;
  }
}

async function resendCode() {
  const btn = $("bw-code-resend");
  const status = $("bw-code-resend-status");
  btn.disabled = true;
  try {
    const body = await apiFetch(`/intents/${encodeURIComponent(state.intentId)}/resend-code`, {
      method: "POST",
    });
    $("bw-code-masked").textContent = body.code_sent_to_masked;
    state.resendAvailableAt = Date.now() + 60000;
    startResendCountdown();
    status.textContent = `New code sent (${body.sends_remaining} of 5 remaining).`;
  } catch (e) {
    if (e instanceof ApiRequestError && (e.code === "resend_cooldown" || e.code === "code_sends_exhausted")) {
      status.textContent = e.message;
      return;
    }
    handleRouteError(e, "Couldn't resend the code");
  } finally {
    if (!state.resendTimer) btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Review + Change links (never submit)                                */
/* ------------------------------------------------------------------ */

function fillReview() {
  const f = formatSlot(state.slot.start, state.slot.end, state.timezone);
  $("bw-review-time").textContent = `${f.dateLong} · ${f.timeRange}`;
  $("bw-review-firm").textContent = f.firmLine;
  $("bw-review-tz").textContent = state.timezone;
  $("bw-review-name").textContent = state.formValues.name;
  $("bw-review-email").textContent = state.formValues.email;
  $("bw-review-phone").textContent = `${state.formValues.country_code} ${state.formValues.phone}`;
  const rel = RELATIONSHIPS.find((r) => r.value === state.formValues.relationship);
  $("bw-review-relationship").textContent = rel ? rel.label : state.formValues.relationship;
  $("bw-review-discuss").textContent = state.formValues.discuss;
  // Fresh idempotency key per review visit (Phase-0 D-11).
  state.idempotencyKey = crypto.randomUUID();
}

// Change links are plain step switches — they issue no network requests.
// data-change="time"      → back to the day picker (fresh availability on confirm)
// data-change="timezone"  → re-open the timezone modal, then back to days
// data-change="email"     → back to the form with values kept (new code on submit)
// data-change="details"   → back to the form with values kept (new code on submit)
function wireChangeLinks() {
  for (const link of document.querySelectorAll(".bw-change")) {
    link.addEventListener("click", () => {
      const kind = link.dataset.change;
      if (kind === "time") {
        state.slot = null;
        showStep("calendar");
        void loadCalendar();
      } else if (kind === "timezone") {
        openTimezoneDialog();
      } else {
        restoreFormValues();
        showStep("form");
      }
    });
  }
}

/* ------------------------------------------------------------------ */
/* Confirm                                                             */
/* ------------------------------------------------------------------ */

async function confirmBooking() {
  const btn = $("bw-confirm");
  btn.disabled = true;
  btn.textContent = "Confirming…";
  try {
    // The key survives network-error retries within this review visit so
    // a double submit can never double-book (Phase-0 D-11).
    const body = await apiFetch("/confirm", {
      method: "POST",
      json: { intent_id: state.intentId },
      headers: { "Idempotency-Key": state.idempotencyKey },
    });
    sessionStorage.setItem(STORAGE.result, JSON.stringify(body));
    window.location.href = withBase("/thank-you-for-your-booking/");
  } catch (e) {
    if (e instanceof ApiRequestError && e.code === "slot_taken") {
      // The intent is still pending (confirm didn't consume it): keep
      // state.intentId so the re-pick's re-submit supersedes it and frees
      // the dead hold instead of stranding it until the sweep.
      state.slot = null;
      showStep("slots");
      await loadSlots();
      showError("That time was just taken — please pick another time.");
      return;
    }
    if (e instanceof ApiRequestError && e.code === "idempotency_conflict") {
      showError("Your details changed while confirming — please review and confirm again.");
      state.idempotencyKey = crypto.randomUUID();
      return;
    }
    if (e instanceof ApiRequestError && e.code === "email_not_verified") {
      showError("Your email isn't verified yet — please enter the code we sent you.");
      showStep("code");
      return;
    }
    if (e instanceof ApiRequestError && e.code === "booking_hold") {
      showError(e.message);
      return;
    }
    handleRouteError(e, "Couldn't confirm your booking");
  } finally {
    btn.disabled = false;
    btn.textContent = "Confirm booking";
  }
}

/* ------------------------------------------------------------------ */
/* Wiring + init                                                       */
/* ------------------------------------------------------------------ */

function wire() {
  $("bw-tz-form").addEventListener("submit", (e) => {
    e.preventDefault();
    closeTimezoneDialog(true);
  });
  // PX3: the dialog is always dismissible — ×, ESC, and backdrop click
  // all close it without a choice. Nothing auto-opens on arrival, so
  // there's no arrival-time trap to guard anymore.
  $("bw-tz-close").onclick = () => closeTimezoneDialog(false);
  $("bw-tz-open").onclick = openTimezoneDialog;
  tzDialog.addEventListener("cancel", () => closeTimezoneDialog(false));
  tzDialog.addEventListener("click", (e) => {
    if (e.target === tzDialog) closeTimezoneDialog(false); // backdrop click dismisses
  });

  $("bw-tz-change").onclick = openTimezoneDialog;

  $("bw-prev-month").onclick = () => shiftMonth(-1);
  $("bw-next-month").onclick = () => shiftMonth(1);
  $("bw-year").onchange = (e) => { state.year = Number(e.target.value); void loadCalendar(); };
  $("bw-month").onchange = (e) => { state.month = Number(e.target.value); void loadCalendar(); };

  $("bw-back-to-calendar").onclick = () => { showStep("calendar"); void loadCalendar(); };

  $("bw-slot-dialog-continue").onclick = () => { closeSlotDialog(); restoreFormValues(); showStep("form"); };
  $("bw-slot-dialog-clear").onclick = () => { closeSlotDialog(); state.slot = null; void loadSlots(); };
  slotDialog.addEventListener("click", (e) => {
    if (e.target === slotDialog) closeSlotDialog(); // backdrop click dismisses
  });

  $("bw-form").addEventListener("submit", (e) => { e.preventDefault(); void submitForm(); });
  $("bw-form-back").onclick = () => { state.slot = null; showStep("slots"); void loadSlots(); };

  $("bw-code-verify").onclick = verifyCode;
  $("bw-code-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); void verifyCode(); }
  });
  $("bw-code-resend").onclick = resendCode;
  $("bw-code-back").onclick = () => { restoreFormValues(); showStep("form"); };

  wireChangeLinks();
  $("bw-confirm").onclick = confirmBooking;
}

async function init() {
  try {
    state.config = await apiFetch("/config");
  } catch (err) {
    handleRouteError(err, "Couldn't load booking settings");
    return;
  }
  wire();
  buildFormControls();

  const now = new Date();
  state.year = now.getFullYear();
  state.month = now.getMonth() + 1;
  const yearSel = buildYearOptions();
  yearSel.value = String(state.year);
  const monthSel = $("bw-month");
  const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  monthNames.forEach((name, i) => monthSel.appendChild(new Option(name, String(i + 1))));
  monthSel.value = String(state.month);

  const storedTz = localStorage.getItem(STORAGE.timezone);
  if (storedTz) {
    state.timezone = storedTz;
    updateTimezoneEchoes();
    showStep("calendar");
    await loadCalendar();
  } else {
    // PX3: never auto-open the modal on arrival. The page content stays
    // readable; the visitor picks their zone via the prompt when ready.
    for (const s of STEPS) $(`bw-step-${s}`).hidden = true;
    const nav = $("bw-steps");
    if (nav) nav.hidden = true;
    $("bw-tz-prompt").hidden = false;
  }
}

void init();
