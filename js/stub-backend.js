/* ============================================================================
 * stub-backend.js — realistic client-side backend stub for the Jaro
 * Accounting static preview.
 *
 * This is a TEST DOUBLE, not the real backend. It intercepts same-origin
 * fetch() calls to /api/* and answers them with realistic, stateful data
 * so every flow (contact, booking, portal, admin) can be exercised on a
 * purely static host (e.g. GitHub Pages) that has no real API.
 *
 * Activation (any one):
 *   - URL has ?stub=1            (persists for the tab via sessionStorage)
 *   - URL has ?stub=0            (forces OFF, clears the flag)
 *   - hostname ends with .github.io (the public preview deployment)
 * Otherwise the script is completely inert: fetch is untouched and the
 * real backend (if any) serves the page normally.
 *
 * While active a floating "STUB MODE" badge shows the demo credentials,
 * the latest booking code —
 * everything a tester needs, without touching the app's own UI.
 *
 * State persists in localStorage (key below) so bookings, uploads and
 * cancellations survive reloads. The badge panel has a "Reset demo data"
 * button to start over.
 * ========================================================================== */
(function () {
  "use strict";

  /* ---------------- activation ---------------- */

  var qs = new URLSearchParams(window.location.search);
  var FLAG_KEY = "jaro.stub.enabled";
  var STATE_KEY = "jaro.stub.state.v1";

  var enabled;
  if (qs.get("stub") === "0") {
    enabled = false;
    try { sessionStorage.removeItem(FLAG_KEY); } catch (e) {}
  } else if (qs.get("stub") === "1") {
    enabled = true;
    try { sessionStorage.setItem(FLAG_KEY, "1"); } catch (e) {}
  } else {
    try {
      enabled = sessionStorage.getItem(FLAG_KEY) === "1" ||
        /\.github\.io$/.test(window.location.hostname);
    } catch (e) {
      enabled = /\.github\.io$/.test(window.location.hostname);
    }
  }
  if (!enabled) return; // inert: the real backend owns /api/*

  /* ---------------- subpath awareness ---------------- */
  // On project-page hosts (e.g. GitHub Pages) the site lives under a
  // subpath like /jaro-accounting-rebuild. Derive it from this script's
  // own URL so generated links and request routing stay correct there;
  // empty on root deployments.
  var STUB_BASE = (function () {
    try {
      var src = (document.currentScript && document.currentScript.src) || "";
      var m = src.match(/^(?:https?:\/\/[^/]+)?(\/.*?)\/js\/stub-backend\.js$/);
      return m ? m[1] : "";
    } catch (e) { return ""; }
  })();
  function stripBase(path) {
    if (STUB_BASE && path.indexOf(STUB_BASE + "/") === 0) return path.slice(STUB_BASE.length);
    return path;
  }

  /* ---------------- tiny utils ---------------- */

  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hashStr(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function rid() {
    var b = new Uint8Array(12);
    crypto.getRandomValues(b);
    return Array.from(b, function (x) { return x.toString(16).padStart(2, "0"); }).join("");
  }
  function uid(prefix) {
    return (prefix || "id") + "_" + rid().slice(0, 16);
  }
  function pick(rng, arr) { return arr[Math.floor(rng() * arr.length)]; }
  function maskEmail(email) {
    var parts = String(email).split("@");
    if (parts.length !== 2) return "***";
    var local = parts[0];
    return (local.length <= 1 ? "*" : local[0] + "***") + "@" + parts[1];
  }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function isoDate(y, m, d) { return y + "-" + pad2(m) + "-" + pad2(d); }
  function nowISO() { return new Date().toISOString(); }
  function daysFromNow(n) {
    return new Date(Date.now() + n * 86400000).toISOString();
  }
  function errBody(code, message, fields) {
    return { code: code, message: message, request_id: rid(), fields: fields || [] };
  }
  function latency() { return 120 + Math.random() * 320; }

  /* Vancouver wall-clock <-> UTC helpers (firm zone America/Vancouver). */
  var VAN = "America/Vancouver";
  function vancouverOffsetMs(utcDate) {
    try {
      var dtf = new Intl.DateTimeFormat("en-US", {
        timeZone: VAN, hour12: false, year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
      var parts = {};
      dtf.formatToParts(utcDate).forEach(function (p) { parts[p.type] = p.value; });
      var asVan = Date.UTC(+parts.year, +parts.month - 1, +parts.day,
        (+parts.hour) % 24, +parts.minute, +parts.second);
      return asVan - utcDate.getTime();
    } catch (e) { return -7 * 3600000; }
  }
  /** Vancouver wall time (y, m 1-12, d, H, M) -> UTC Date. */
  function vanWallToUtc(y, m, d, H, M) {
    var wall = Date.UTC(y, m - 1, d, H, M, 0);
    var guess = wall;
    for (var i = 0; i < 2; i++) guess = wall - vancouverOffsetMs(new Date(guess));
    return new Date(guess);
  }

  /* ---------------- seed data ---------------- */

  // Official 10 BC statutory holidays (matches the backend's bc_stat_holidays).
  var HOLIDAYS = {
    2026: ["2026-01-01", "2026-02-16", "2026-04-03", "2026-05-18", "2026-07-01",
           "2026-08-03", "2026-09-07", "2026-10-12", "2026-11-11", "2026-12-25"],
    2027: ["2027-01-01", "2027-02-15", "2027-03-26", "2027-05-24", "2027-07-01",
           "2027-08-02", "2027-09-06", "2027-10-11", "2027-11-11", "2027-12-25"],
  };
  var FIRM_TZ = "America/Vancouver";
  // Mon–Fri 09:00–17:00, Sat 10:00–15:00 (mirrors GET /config working_hours).
  var DAY_HOURS = { 0: null, 1: [9, 17], 2: [9, 17], 3: [9, 17], 4: [9, 17], 5: [9, 17], 6: [10, 15] };

  var DEMO_CLIENT = {
    id: "client_amelia_hart",
    display_name: "Amelia Hart",
    email: "amelia.hart@example.com",
    password: "demo-client-1",
    phone_e164: "+16045550137",
    status: "active",
  };
  // Temp-password onboarding demo: signs in, then must choose her own password.
  var DEMO_TEMP_CLIENT = {
    id: "client_new_temp",
    display_name: "New Client",
    email: "new.client@example.com",
    password: "temp-password-1",
    phone_e164: null,
    status: "active",
    must_change_password: true,
  };
  var DEMO_OWNER = {
    id: "owner_jared",
    display_name: "Jared Robbins",
    email: "jared@jaroaccounting.com",
    password: "demo-owner-1",
    status: "active",
  };
  var DEMO_INVITE_TOKEN = "demo-invite-token-1234567890abcdef";
  var DEMO_RESET_TOKEN = "demo-reset-token-1234567890abcdef";

  function seedState() {
    return {
      v: 1,
      // booking
      intents: {},          // intent_id -> intent
      bookings: {},         // booking_id -> booking
      manageTokens: {},     // token -> booking_id (revoked on cancel/reschedule)
      takenSlots: {},       // slot_start ISO -> true (seeded busy times)
      idempotency: {},      // Idempotency-Key -> {payload_hash, response}
      // portal
      session: null,        // {email, role, display_name, expires_at, terms_accepted}
      totpEnrolled: false,
      inquiries: [],
      inquiryRate: [],      // timestamps of recent inquiry submits
      documents: [
        { id: "doc_t1_2025", label: "2025 T1 Return — final", original_filename: "Hart_2025_T1_Final.pdf", type_code: "deliverables", type_label: "Deliverables", byte_size: 184320, created_at: "2026-04-28T16:02:11Z", uploaded_by: "owner" },
        { id: "doc_noa_2024", label: "2024 Notice of Assessment", original_filename: "NOA_2024.pdf", type_code: "correspondence", type_label: "Correspondence", byte_size: 96512, created_at: "2026-06-02T14:40:00Z", uploaded_by: "owner" },
        { id: "doc_t4_2025", label: null, original_filename: "T4_2025.pdf", type_code: "other", type_label: "Other", byte_size: 41208, created_at: "2026-03-14T10:12:44Z", uploaded_by: "client" },
      ],
      filings: [
        { id: "fil_t1_2025", kind_code: "personal_t1", kind_label: "Personal T1", tax_year: "2025", status: "preparing", note: "Waiting on your T4A slip — upload it under Documents when it arrives.",
          timeline: [
            { status: "collecting", at: "2026-03-02T15:00:00Z" },
            { status: "awaiting_info", at: "2026-03-20T17:30:00Z" },
            { status: "preparing", at: "2026-04-11T13:05:00Z" },
          ] },
        { id: "fil_gst_2025", kind_code: "gst_hst", kind_label: "GST/HST", tax_year: "2025", status: "filed", note: null,
          timeline: [
            { status: "collecting", at: "2026-01-12T14:00:00Z" },
            { status: "awaiting_info", at: "2026-01-19T16:00:00Z" },
            { status: "preparing", at: "2026-02-03T11:00:00Z" },
            { status: "filed", at: "2026-02-24T09:15:00Z" },
          ] },
      ],
      invoices: [
        { id: "inv_0041", invoice_number: "JARO-2026-0041", issued_at: "2026-09-15T09:00:00Z", due_at: "2026-10-15T23:59:59Z", status: "issued",
          memo: "E-transfer to payments@jaroaccounting.com — please include the invoice number.",
          lines: [
            { description: "2025 T1 personal tax preparation", quantity: 1, unit_cents: 45000, tax_rate_bps: 500, line_total_cents: 47250 },
          ],
          subtotal_cents: 45000, tax_cents: 2250, total_cents: 47250 },
        { id: "inv_0038", invoice_number: "JARO-2026-0038", issued_at: "2026-06-10T09:00:00Z", due_at: "2026-07-10T23:59:59Z", status: "paid", memo: null,
          lines: [
            { description: "Q2 bookkeeping — monthly package", quantity: 1, unit_cents: 32000, tax_rate_bps: 500, line_total_cents: 33600 },
          ],
          subtotal_cents: 32000, tax_cents: 1600, total_cents: 33600 },
      ],
      invoiceSeq: 43,
      // admin
      clients: [
        { id: DEMO_CLIENT.id, display_name: DEMO_CLIENT.display_name, email: DEMO_CLIENT.email, status: "active",
          phone_e164: DEMO_CLIENT.phone_e164, phone_verified_at: "2026-03-01T12:00:00Z",
          totp_enrolled_at: null, terms_accepted_at: null, last_login_at: "2026-10-12T18:22:00Z",
          internal_note: "Prefers email over phone. T4A still outstanding for 2025 T1." },
        { id: "client_ben_okafor", display_name: "Ben Okafor", email: "ben.okafor@example.com", status: "active",
          phone_e164: "+17785550192", phone_verified_at: "2026-05-11T12:00:00Z",
          totp_enrolled_at: "2026-05-12T09:00:00Z", terms_accepted_at: "2026-05-11T12:05:00Z",
          last_login_at: "2026-10-10T14:02:00Z", internal_note: "Corporate T2 + GST. Year-end is Dec 31." },
        { id: "client_priya_nair", display_name: "Priya Nair", email: "priya.nair@example.com", status: "invited",
          phone_e164: null, phone_verified_at: null, totp_enrolled_at: null,
          terms_accepted_at: null, last_login_at: null, internal_note: "Invited Oct 9 — hasn't set a password yet." },
      ],
      invitations: [
        { id: "inv_0007", client_name: "Priya Nair", email: "priya.nair@example.com", status: "emailed", expires_at: daysFromNow(4), created_at: daysFromNow(-3) },
        { id: "inv_0006", client_name: "Tom Becker", email: "tom.becker@example.com", status: "created", expires_at: daysFromNow(6), created_at: daysFromNow(-1) },
      ],
      inviteSeq: 8,
      audit: [
        { id: uid("audit"), occurred_at: daysFromNow(-0.2), action: "invoice.issued", actor: "Jared Robbins", entity_type: "invoice" },
        { id: uid("audit"), occurred_at: daysFromNow(-1.1), action: "document.shared", actor: "Jared Robbins", entity_type: "document" },
        { id: uid("audit"), occurred_at: daysFromNow(-2.4), action: "filing.status_changed", actor: "Jared Robbins", entity_type: "filing" },
        { id: uid("audit"), occurred_at: daysFromNow(-3.0), action: "invitation.emailed", actor: "Jared Robbins", entity_type: "invitation" },
      ],
      // transient (not persisted meaningfully, but kept for the session)
      challenges: {},       // challenge_id -> {email, code, sms_code}
      bookingCodes: {},      // intent_id -> current 6-digit code (shown in badge)
      lastBookingCode: null, // {masked, code} for the badge
    };
  }

  function loadState() {
    try {
      var raw = localStorage.getItem(STATE_KEY);
      if (raw) {
        var s = JSON.parse(raw);
        if (s && s.v === 1) return s;
      }
    } catch (e) {}
    return seedState();
  }
  var S = loadState();
  function save() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(S)); } catch (e) {}
  }

  /* ---------------- response helpers ---------------- */

  function jsonResponse(data, status, headers) {
    var h = { "Content-Type": "application/json" };
    if (headers) for (var k in headers) h[k] = headers[k];
    return new Response(JSON.stringify(data), { status: status || 200, headers: h });
  }
  function errResponse(code, message, status, fields, headers) {
    return jsonResponse(errBody(code, message, fields), status, headers);
  }
  function notFound() {
    return errResponse("not_found", "That wasn't found.", 404);
  }

  /* ---------------- availability engine ---------------- */

  /** Deterministic per-month availability, mirroring firm rules. */
  function monthAvailability(year, month) {
    var rng = mulberry32(hashStr("avail:" + year + "-" + month));
    var days = [];
    var dim = new Date(Date.UTC(year, month, 0)).getUTCDate();
    var today = new Date();
    var todayUtc = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
    var holidays = HOLIDAYS[year] || [];
    for (var d = 1; d <= dim; d++) {
      var dateStr = isoDate(year, month, d);
      var dayDate = new Date(Date.UTC(year, month - 1, d));
      var dow = dayDate.getUTCDay();
      var hours = DAY_HOURS[dow];
      var dayMs = dayDate.getTime();
      var available = true;
      if (!hours) available = false;                              // Sunday
      else if (holidays.indexOf(dateStr) !== -1) available = false;
      else if (dayMs < todayUtc) available = false;               // past
      else if (dayMs - todayUtc > 90 * 86400000) available = false; // horizon
      else if (dayMs - todayUtc < 86400000 && !hasSlotAfter(dayDate, hours, 24)) available = false;
      else if (rng() < 0.12 && dow !== 6) available = false;      // seeded fully-booked days
      days.push({ date: dateStr, available: available });
    }
    return days;
  }
  function hasSlotAfter(dayDate, hours, noticeHours) {
    // Same seeded pattern as slotsForDate so the answer always agrees
    // with what the slot list will show.
    var dateStr = isoDate(dayDate.getUTCFullYear(), dayDate.getUTCMonth() + 1, dayDate.getUTCDate());
    var slots = daySlots(dayDate, hours, mulberry32(hashStr("slots:" + dateStr)));
    var cutoff = Date.now() + noticeHours * 3600000;
    return slots.some(function (s) { return Date.parse(s.start) >= cutoff && !s.taken; });
  }
  /** 30-min slots for a day (UTC Date at 00:00), firm wall-clock hours. */
  function daySlots(dayDate, hours, rng) {
    var y = dayDate.getUTCFullYear(), m = dayDate.getUTCMonth() + 1, d = dayDate.getUTCDate();
    var out = [];
    for (var H = hours[0]; H < hours[1]; H++) {
      for (var M = 0; M < 60; M += 30) {
        var lastStart = (hours[1] - hours[0]) * 60 - 30; // mins after open
        var minsAfterOpen = (H - hours[0]) * 60 + M;
        if (minsAfterOpen > lastStart) continue; // last start 16:30 / 14:30 Sat
        var start = vanWallToUtc(y, m, d, H, M);
        var end = new Date(start.getTime() + 30 * 60000);
        var key = start.toISOString();
        out.push({
          start: key,
          end: end.toISOString(),
          taken: !!S.takenSlots[key] || rng() < 0.22,
        });
      }
    }
    return out;
  }
  function slotsForDate(dateStr, guestTz) {
    var p = dateStr.split("-").map(Number);
    var dayDate = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    var dow = dayDate.getUTCDay();
    var hours = DAY_HOURS[dow];
    if (!hours) return [];
    var holidays = HOLIDAYS[p[0]] || [];
    if (holidays.indexOf(dateStr) !== -1) return [];
    var rng = mulberry32(hashStr("slots:" + dateStr));
    var cutoff = Date.now() + 24 * 3600000; // 24h minimum notice
    return daySlots(dayDate, hours, rng)
      .filter(function (s) { return !s.taken && Date.parse(s.start) >= cutoff; })
      .map(function (s) { return { start: s.start, end: s.end }; });
  }
  function seedTakenSlots() {
    // A few plausibly-busy slots near today so the calendar feels lived-in.
    var rng = mulberry32(hashStr("taken-seed"));
    for (var i = 1; i <= 20; i++) {
      var day = new Date(Date.now() + i * 86400000);
      var p = [day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate()];
      var dow = new Date(Date.UTC(p[0], p[1] - 1, p[2])).getUTCDay();
      var hours = DAY_HOURS[dow];
      if (!hours) continue;
      var n = 1 + Math.floor(rng() * 3);
      for (var k = 0; k < n; k++) {
        var H = hours[0] + Math.floor(rng() * (hours[1] - hours[0]));
        var M = rng() < 0.5 ? 0 : 30;
        S.takenSlots[vanWallToUtc(p[0], p[1], p[2], H, M).toISOString()] = true;
      }
    }
  }
  if (!Object.keys(S.takenSlots).length) { seedTakenSlots(); save(); }

  function bookingConfig() {
    var year = new Date().getUTCFullYear();
    var holidays = [];
    [year, year + 1].forEach(function (y) {
      (HOLIDAYS[y] || []).forEach(function (d) { holidays.push(d); });
    });
    holidays.sort();
    return {
      firm_timezone: FIRM_TZ,
      working_hours: {
        mon_fri: { open: "09:00", close: "17:00" },
        sat: { open: "10:00", close: "15:00" },
      },
      event_type: "Free 30-Min Consultation — Phone Call",
      min_notice_hours: 24,
      horizon_days: 90,
      holidays: holidays,
      verification: { code_ttl_minutes: 10, max_attempts: 5, resend_cooldown_seconds: 60, max_sends: 5 },
      cutoffs: { cancel_hours: 24, reschedule_hours: 24 },
    };
  }

  /* ---------------- booking helpers ---------------- */

  function newRef(prefix) {
    var chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
    var rng = Math.random, s = "";
    for (var i = 0; i < 6; i++) s += chars[Math.floor(rng() * chars.length)];
    var year = new Date().getUTCFullYear();
    return (prefix || "JARO") + "-" + year + "-" + s;
  }
  function newToken() {
    var chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    var s = "";
    for (var i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
    return s;
  }
  function sixDigit() { return String(100000 + Math.floor(Math.random() * 900000)); }
  function emailValid(e) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(e); }

  function findFreeSlot(slotStart) {
    // Confirm the requested start is a genuine bookable slot.
    var t = Date.parse(slotStart);
    if (isNaN(t)) return null;
    var d = new Date(t);
    var dateStr = d.toISOString().slice(0, 10);
    var list = slotsForDate(dateStr, FIRM_TZ);
    for (var i = 0; i < list.length; i++) {
      if (list[i].start === new Date(t).toISOString()) return list[i];
    }
    return null;
  }

  /* ================= booking routes ================= */

  function bookingRoutes(method, parts, query, body, headers) {
    var p0 = parts[0] || "";

    if (method === "GET" && p0 === "config") {
      return jsonResponse(bookingConfig());
    }

    if (method === "GET" && p0 === "availability") {
      var year = parseInt(query.get("year") || "0", 10);
      var month = parseInt(query.get("month") || "0", 10);
      var tz = (query.get("tz") || "").trim() || FIRM_TZ;
      if (!(year >= 1900 && year <= 2200) || !(month >= 1 && month <= 12)) {
        return errResponse("validation_failed", "The request could not be validated.", 422,
          [{ field: "month", message: "Please choose a valid year and month." }]);
      }
      try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); }
      catch (e) {
        return errResponse("validation_failed", "The request could not be validated.", 422,
          [{ field: "tz", message: "Unknown time zone." }]);
      }
      return jsonResponse({
        year: year, month: month,
        days: monthAvailability(year, month),
        as_of: nowISO(),
        guest_timezone_echo: tz,
      });
    }

    if (method === "GET" && p0 === "slots") {
      var dateStr = query.get("date") || "";
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
        return errResponse("validation_failed", "The request could not be validated.", 422,
          [{ field: "date", message: "Please choose a valid date." }]);
      }
      var gtz = (query.get("tz") || "").trim() || FIRM_TZ;
      return jsonResponse({ date: dateStr, slots: slotsForDate(dateStr, gtz) });
    }

    if (method === "POST" && p0 === "intents" && parts.length === 1) {
      return bookingCreateIntent(body || {});
    }

    if (parts[0] === "intents" && parts.length === 3 && parts[2] === "verify" && method === "POST") {
      return bookingVerifyCode(parts[1], body || {});
    }

    if (parts[0] === "intents" && parts.length === 3 && parts[2] === "resend-code" && method === "POST") {
      return bookingResendCode(parts[1]);
    }

    if (method === "POST" && p0 === "confirm" && parts.length === 1) {
      return bookingConfirm(body || {}, headers);
    }

    if (parts[0] === "manage" && parts.length >= 2) {
      var token = parts[1];
      var bookingId = S.manageTokens[token];
      var booking = bookingId && S.bookings[bookingId];
      if (!booking || booking.status !== "confirmed") {
        // A5: never distinguish invalid / expired / revoked.
        return errResponse("not_found", "That link isn't valid.", 404);
      }
      if (parts.length === 2 && method === "GET") {
        return jsonResponse(manageProjection(booking));
      }
      if (parts.length === 3 && parts[2] === "reschedule" && method === "POST") {
        return bookingReschedule(booking, token, body || {}, headers);
      }
      if (parts.length === 3 && parts[2] === "cancel" && method === "POST") {
        return bookingCancel(booking, token, body || {});
      }
    }

    return notFound();
  }

  function intentFieldErrors(v) {
    var fields = [];
    var name = (v.name || "").trim();
    if (name.length < 2 || name.length > 100)
      fields.push({ field: "name", message: "Please enter your name (2–100 characters)." });
    if (!emailValid((v.email || "").trim()))
      fields.push({ field: "email", message: "Please enter a valid email address." });
    var digits = (v.phone || "").replace(/\D/g, "");
    if (digits.length < 7)
      fields.push({ field: "phone", message: "Please enter a valid phone number." });
    var discuss = (v.discuss || "").trim();
    if (discuss.length < 10 || discuss.length > 1000)
      fields.push({ field: "discuss", message: "Tell us a little about what you'd like to discuss (10–1000 characters)." });
    if (!v.slot_start)
      fields.push({ field: "slot_start", message: "Please pick a time first." });
    return fields;
  }

  function bookingCreateIntent(v) {
    var fields = intentFieldErrors(v);
    if (fields.length) return errResponse("validation_failed", "The request could not be validated.", 422, fields);

    var slot = findFreeSlot(v.slot_start);
    if (!slot) {
      return errResponse("slot_taken", "That time was just taken — please pick another time.", 409);
    }
    // Supersede: a re-submit expires the caller's previous pending intent.
    if (v.supersedes_intent_id && S.intents[v.supersedes_intent_id]) {
      var old = S.intents[v.supersedes_intent_id];
      if (old.status === "pending_verification") {
        old.status = "superseded";
        delete S.takenSlots[old.slot_start];
      }
    }
    var id = uid("intent");
    var code = sixDigit();
    var email = v.email.trim();
    var intent = {
      id: id,
      reference: newRef("JARO"),
      slot_start: slot.start,
      slot_end: slot.end,
      guest_timezone: v.guest_timezone || FIRM_TZ,
      name: v.name.trim(), email: email,
      phone: v.phone.trim(), country_code: v.country_code || "+1",
      relationship: v.relationship || "prospective_client",
      discuss: v.discuss.trim(),
      code: code, code_attempts: 0, sends_used: 1,
      last_sent_at: Date.now(), verified: false,
      status: "pending_verification", created_at: nowISO(),
    };
    S.intents[id] = intent;
    S.takenSlots[slot.start] = true; // hold the slot while verifying
    S.bookingCodes[id] = code;
    S.lastBookingCode = { masked: maskEmail(email), code: code };
    save();
    badgeRefresh();
    return jsonResponse({
      intent_id: id,
      reference: intent.reference,
      code_sent_to_masked: maskEmail(email),
      expires_in_seconds: 600,
      sends_used: 1,
      remaining_sends: 4,
    });
  }

  function bookingVerifyCode(intentId, v) {
    var intent = S.intents[intentId];
    if (!intent || intent.status !== "pending_verification") {
      return errResponse("intent_expired", "That code (or this booking attempt) expired. Start again with a fresh time.", 410);
    }
    if (Date.now() - intent.last_sent_at > 10 * 60000) {
      intent.status = "expired";
      delete S.takenSlots[intent.slot_start];
      save();
      return errResponse("code_expired", "That code expired. Request a new one to continue.", 410);
    }
    if (intent.code_attempts >= 5) {
      return errResponse("code_attempts_exceeded", "Too many wrong tries.", 429, [], { "Retry-After": "60" });
    }
    if ((v.code || "").trim() !== intent.code) {
      intent.code_attempts += 1;
      save();
      if (intent.code_attempts >= 5) {
        return errResponse("code_attempts_exceeded", "Too many wrong tries — request a new code below to continue.", 429, [], { "Retry-After": "60" });
      }
      return errResponse("invalid_code", "That code isn't right.", 401,
        [{ field: "code", message: "That code isn't right. Check the code and try again." }]);
    }
    intent.verified = true;
    intent.status = "verified";
    save();
    return jsonResponse({ verified: true, email_masked: maskEmail(intent.email) });
  }

  function bookingResendCode(intentId) {
    var intent = S.intents[intentId];
    if (!intent || intent.status !== "pending_verification") {
      return errResponse("intent_expired", "This booking attempt expired. Start again with a fresh time.", 410);
    }
    if (Date.now() - intent.last_sent_at < 60000) {
      var wait = Math.ceil((60000 - (Date.now() - intent.last_sent_at)) / 1000);
      return errResponse("resend_cooldown", "Please wait " + wait + "s before requesting a new code.", 429, [], { "Retry-After": String(wait) });
    }
    if (intent.sends_used >= 5) {
      return errResponse("code_sends_exhausted", "No more codes can be sent for this attempt. Start again with a fresh time.", 429);
    }
    var code = sixDigit();
    intent.code = code;
    intent.code_attempts = 0;
    intent.sends_used += 1;
    intent.last_sent_at = Date.now();
    S.bookingCodes[intentId] = code;
    S.lastBookingCode = { masked: maskEmail(intent.email), code: code };
    save();
    badgeRefresh();
    return jsonResponse({
      code_sent_to_masked: maskEmail(intent.email),
      sends_remaining: 5 - intent.sends_used,
    });
  }

  function confirmProjection(booking, replayed) {
    return {
      reference: booking.reference,
      slot_start: booking.slot_start,
      slot_end: booking.slot_end,
      guest_timezone: booking.guest_timezone,
      email_masked: maskEmail(booking.email),
      queued_count: 3,
      reschedule_url: STUB_BASE + "/booking-manage.html?token=" + booking.manage_token,
      cancel_url: STUB_BASE + "/booking-manage.html?token=" + booking.manage_token,
      ics_url: STUB_BASE + "/api/v1/booking/manage/" + booking.manage_token + "/ics",
      replayed: !!replayed,
    };
  }

  function bookingConfirm(v, headers) {
    // Idempotency first: a replayed confirm must succeed even though the
    // intent is already consumed (mirrors the backend's idempotency store).
    var key = headers["idempotency-key"] || headers["Idempotency-Key"];
    var payloadHash = hashStr(JSON.stringify({ intent_id: v.intent_id }));
    if (key) {
      var seen = S.idempotency["confirm:" + key];
      if (seen) {
        if (seen.payload_hash === payloadHash) {
          var rb = S.bookings[seen.booking_id];
          if (rb) return jsonResponse(confirmProjection(rb, true));
        } else {
          return errResponse("idempotency_conflict", "Your details changed while confirming — please review and confirm again.", 409);
        }
      }
    }
    var intent = S.intents[v.intent_id];
    if (!intent || (intent.status !== "verified" && intent.status !== "pending_verification")) {
      return errResponse("intent_expired", "This booking attempt expired. Start again with a fresh time.", 410);
    }
    if (!intent.verified) {
      return errResponse("email_not_verified", "Your email isn't verified yet — please enter the code we sent you.", 403);
    }
    if (S.takenSlots[intent.slot_start] && !intentSlotHeldBy(intent)) {
      // Slot genuinely taken by someone else (not our own hold).
    }
    var booking = {
      id: uid("booking"),
      reference: newRef("JARO"),
      slot_start: intent.slot_start,
      slot_end: intent.slot_end,
      guest_timezone: intent.guest_timezone,
      name: intent.name, email: intent.email, phone: intent.phone,
      country_code: intent.country_code, relationship: intent.relationship,
      discuss: intent.discuss,
      status: "confirmed",
      manage_token: newToken(),
      created_at: nowISO(),
    };
    intent.status = "confirmed";
    S.bookings[booking.id] = booking;
    S.manageTokens[booking.manage_token] = booking.id;
    if (key) S.idempotency["confirm:" + key] = { payload_hash: payloadHash, booking_id: booking.id };
    save();
    return jsonResponse(confirmProjection(booking, false));
  }
  function intentSlotHeldBy() { return true; } // our own hold is always legitimate

  function manageProjection(booking) {
    var msLeft = Date.parse(booking.slot_start) - Date.now();
    var actions = (booking.status === "confirmed" && msLeft > 24 * 3600000)
      ? ["reschedule", "cancel"] : [];
    return {
      reference: booking.reference,
      status: booking.status,
      guest_name: booking.name,
      guest_email: booking.email,
      guest_phone: booking.phone,
      guest_relationship: booking.relationship,
      slot_start: booking.slot_start,
      slot_end: booking.slot_end,
      guest_timezone: booking.guest_timezone,
      allowed_actions: actions,
      cutoffs: { cancel_hours: 24, reschedule_hours: 24 },
    };
  }

  function bookingReschedule(booking, oldToken, v, headers) {
    var slot = findFreeSlot(v.new_slot_start);
    if (!slot) {
      return errResponse("slot_taken", "That time was just taken — please pick another.", 409);
    }
    delete S.takenSlots[booking.slot_start];
    S.takenSlots[slot.start] = true;
    booking.slot_start = slot.start;
    booking.slot_end = slot.end;
    booking.reference = newRef("JARO"); // each change issues a new reference
    delete S.manageTokens[oldToken];
    booking.manage_token = newToken();
    S.manageTokens[booking.manage_token] = booking.id;
    save();
    return jsonResponse(confirmProjection(booking, false));
  }

  function bookingCancel(booking, token) {
    booking.status = "cancelled";
    delete S.manageTokens[token]; // revoked: the link stops working
    delete S.takenSlots[booking.slot_start]; // slot released
    save();
    return jsonResponse({ reference: booking.reference });
  }

  /* ================= inquiries ================= */

  function inquiriesRoutes(method, parts, body, headers) {
    if (method === "POST" && parts.length === 0) {
      var v = body || {};
      var fields = [];
      var name = (v.name || "").trim();
      var email = (v.email || "").trim();
      var message = (v.message || "").trim();
      if (name.length < 2 || name.length > 100)
        fields.push({ field: "name", message: "Please enter your name (2–100 characters)." });
      if (!emailValid(email))
        fields.push({ field: "email", message: "Please enter a valid email address." });
      if (message.length < 10 || message.length > 2000)
        fields.push({ field: "message", message: "Please enter a message between 10 and 2000 characters." });
      if (fields.length)
        return errResponse("validation_failed", "Please fix the following:", 422, fields);

      // Gentle rate budget: 10 inquiries / minute (expressive, not punishing).
      var now = Date.now();
      S.inquiryRate = (S.inquiryRate || []).filter(function (t) { return now - t < 60000; });
      if (S.inquiryRate.length >= 10) {
        return errResponse("rate_limited", "We've received a lot of messages just now. Please wait a little while and try again.", 429, [], { "Retry-After": "45" });
      }

      var key = headers["idempotency-key"] || headers["Idempotency-Key"];
      if (!key) return errResponse("idempotency_key_required", "We couldn't prepare your message. Please reload the page and try again.", 400);
      var payloadHash = hashStr(JSON.stringify({ name: name, email: email, message: message }));
      var seen = S.idempotency["inquiry:" + key];
      if (seen) {
        if (seen.payload_hash === payloadHash) {
          var prev = S.inquiries.find(function (i) { return i.id === seen.inquiry_id; });
          if (prev) return jsonResponse({ reference: prev.reference, response_sla: "1–2 business days" }, 202);
        }
        return errResponse("idempotency_conflict", "This looks like a message that was already received with different details.", 409);
      }

      var inquiry = {
        id: uid("inquiry"),
        reference: newRef("INQ"),
        name: name, email: email, message: message,
        created_at: nowISO(),
      };
      S.inquiries.push(inquiry);
      S.inquiryRate.push(now);
      S.idempotency["inquiry:" + key] = { payload_hash: payloadHash, inquiry_id: inquiry.id };
      save();
      return jsonResponse({ reference: inquiry.reference, response_sla: "1–2 business days" }, 202);
    }
    return notFound();
  }

  /* ================= portal: auth & session ================= */

  var SESSION_TTL_MS = 15 * 60000;

  function sessionAlive() {
    return S.session && S.session.expires_at > Date.now();
  }
  function touchSession() {
    if (sessionAlive()) S.session.expires_at = Date.now() + SESSION_TTL_MS;
  }
  function requireAuth() {
    if (!sessionAlive()) {
      return errResponse("session_expired", "Your session has expired. Please sign in again.", 401);
    }
    touchSession();
    return null;
  }

  function portalAuthRoutes(method, parts, query, body) {
    var p0 = parts[0] || "";

    if (method === "GET" && p0 === "session") {
      if (!sessionAlive())
        return errResponse("session_expired", "Your session has expired. Please sign in again.", 401);
      touchSession(); save();
      return jsonResponse({
        expires_in_seconds: Math.max(1, Math.round((S.session.expires_at - Date.now()) / 1000)),
        email: S.session.email, role: S.session.role,
        must_change_password: !!S.session.must_change_password,
        terms_pending: !S.session.terms_accepted,
      });
    }

    if (method === "POST" && p0 === "login" && parts.length === 1) {
      var v = body || {};
      var acct = null, role = null;
      if (v.email === DEMO_CLIENT.email && v.password === DEMO_CLIENT.password) { acct = DEMO_CLIENT; role = "client"; }
      else if (v.email === DEMO_TEMP_CLIENT.email && v.password === DEMO_TEMP_CLIENT.password) { acct = DEMO_TEMP_CLIENT; role = "client"; }
      else if (v.email === DEMO_OWNER.email && v.password === DEMO_OWNER.password) { acct = DEMO_OWNER; role = "owner"; }
      if (!acct) {
        // Anti-enumeration: identical 401 either way.
        return errResponse("invalid_credentials", "That email and password didn't match our records.", 401);
      }
      // Password-only login (no authenticator): session immediately, with a
      // forced password-change step for temporary-password onboarding.
      var termsAccepted = role === "owner" ? true : !!S.clientTermsAccepted;
      var mustChange = !!acct.must_change_password && !S.tempPasswordChanged;
      S.session = {
        email: acct.email, role: role, display_name: acct.display_name,
        expires_at: Date.now() + SESSION_TTL_MS,
        terms_accepted: termsAccepted,
        must_change_password: mustChange,
      };
      save(); badgeRefresh();
      return jsonResponse({
        ok: true,
        next: mustChange ? "change_password" : (termsAccepted ? "portal" : "terms"),
        session_expires_in_seconds: Math.round(SESSION_TTL_MS / 1000),
      });
    }

    if (method === "POST" && p0 === "password" && parts[1] === "change") {
      if (!S.session) return errResponse("unauthorized", "Please sign in again.", 401);
      var vpw = body || {};
      if (!vpw.new_password || vpw.new_password.length < 12)
        return errResponse("validation_failed", "Please choose a stronger password.", 422,
          [{ field: "new_password", message: "Use at least 12 characters." }]);
      S.tempPasswordChanged = true;
      if (S.session) S.session.must_change_password = false;
      var termsOk = S.session.role === "owner" ? true : !!S.clientTermsAccepted;
      save(); badgeRefresh();
      return jsonResponse({ ok: true, next: termsOk ? "portal" : "terms" });
    }

    
    
    
    if (method === "POST" && p0 === "logout") {
      S.session = null; save();
      return jsonResponse({ ok: true });
    }

    if (method === "POST" && p0 === "sessions" && parts[1] === "extend") {
      var authErr = requireAuth();
      if (authErr) return authErr;
      S.session.expires_at = Date.now() + SESSION_TTL_MS;
      save();
      return jsonResponse({ expires_in_seconds: Math.round(SESSION_TTL_MS / 1000) });
    }

    if (method === "GET" && p0 === "terms" && parts[1] === "current") {
      return jsonResponse({
        version_hash: "stub-terms-v3",
        body_html: "<h2>myJaro Client Portal — Terms of Use (demo)</h2>" +
          "<p>This is a <strong>stub preview</strong>: the terms below illustrate the real " +
          "document's shape. Nothing here is a legal agreement.</p>" +
          "<ol><li>The portal is for you and Jaro Accounting to exchange tax documents securely.</li>" +
          "<li>Keep your password private; tell us immediately if you suspect misuse.</li>" +
          "<li>Documents you upload should be true copies of your records.</li>" +
          "<li>Either side may close portal access with written notice.</li></ol>",
      });
    }

    if (method === "POST" && p0 === "terms" && parts[1] === "accept") {
      var a2 = requireAuth();
      if (a2) return a2;
      S.session.terms_accepted = true;
      if (S.session.role === "client") S.clientTermsAccepted = true;
      var c = S.clients.find(function (x) { return x.email === S.session.email; });
      if (c) c.terms_accepted_at = nowISO();
      save();
      return jsonResponse({ accepted_at: nowISO() });
    }

    return null; // not an auth route
  }


  /* ================= portal: client ================= */

  function portalClientRoutes(method, parts, query, body, headers) {
    var p0 = parts[0] || "";

    // Pre-login public routes: no session required.
    if (method === "POST" && p0 === "password-reset" && parts[1] === "request") {
      return jsonResponse({ ok: true }); // always 200, same message either way
    }
    if (method === "POST" && p0 === "password-reset" && parts[1] === "verify") {
      if ((body || {}).token === DEMO_RESET_TOKEN) return jsonResponse({ ok: true });
      return errResponse("reset_invalid", "This reset link isn't valid anymore.", 410);
    }
    if (method === "POST" && p0 === "password-reset" && parts[1] === "complete") {
      var v = body || {};
      if (v.token !== DEMO_RESET_TOKEN)
        return errResponse("reset_invalid", "This reset link isn't valid anymore.", 410);
      if (!v.new_password || v.new_password.length < 12)
        return errResponse("validation_failed", "Please choose a stronger password.", 422,
          [{ field: "new_password", message: "Use at least 12 characters." }]);
      return jsonResponse({ ok: true });
    }
    var authErr = requireAuth();
    if (authErr) return authErr;

    if (method === "GET" && p0 === "dashboard") {
      if (!S.session.terms_accepted) {
        return errResponse("terms_not_accepted", "Please accept the terms of use to continue.", 403);
      }
      if (S.session.must_change_password) {
        return errResponse("password_change_required", "You need to set your own password before you can continue.", 403);
      }
      return jsonResponse(dashboardProjection());
    }

    if (method === "GET" && p0 === "documents" && parts.length === 1) {
      return jsonResponse({ items: S.documents.filter(function (d) { return !d.deleted_at; }) });
    }

    if (method === "POST" && p0 === "documents" && parts.length === 1) {
      // FormData upload: body arrives as a parsed field map from the router.
      var f = body || {};
      var filename = f.__filename || "upload.bin";
      var doc = {
        id: uid("doc"),
        label: f.label || null,
        original_filename: filename,
        type_code: f.type_code || "other",
        type_label: { deliverables: "Deliverables", correspondence: "Correspondence", other: "Other" }[f.type_code] || "Other",
        byte_size: parseInt(f.__size || "0", 10) || 1024,
        created_at: nowISO(),
        uploaded_by: S.session.role === "owner" ? "owner" : "client",
      };
      S.documents.unshift(doc);
      save();
      return jsonResponse({ id: doc.id }, 201);
    }

    if (method === "GET" && p0 === "filings" && parts.length === 1) {
      return jsonResponse({
        items: S.filings.map(function (x) {
          return { id: x.id, kind_label: x.kind_label, tax_year: x.tax_year, status: x.status, note: x.note };
        }),
      });
    }

    if (method === "GET" && p0 === "filings" && parts.length === 2) {
      var fl = S.filings.find(function (x) { return x.id === parts[1]; });
      if (!fl) return notFound();
      return jsonResponse({ id: fl.id, timeline: fl.timeline });
    }

    if (method === "GET" && p0 === "invoices" && parts.length === 1) {
      return jsonResponse({
        items: S.invoices.map(function (x) {
          return { id: x.id, invoice_number: x.invoice_number, total_cents: x.total_cents, due_at: x.due_at, issued_at: x.issued_at, status: x.status };
        }),
      });
    }

    if (method === "GET" && p0 === "invoices" && parts.length === 2) {
      var inv = S.invoices.find(function (x) { return x.id === parts[1]; });
      if (!inv) return notFound();
      return jsonResponse(inv);
    }

    return null;
  }

  function greeting() {
    var h;
    try {
      h = parseInt(new Intl.DateTimeFormat("en-CA", { timeZone: VAN, hour: "numeric", hour12: false }).format(new Date()), 10);
    } catch (e) { h = new Date().getHours(); }
    var part = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
    return part + ", " + (S.session.display_name || "there");
  }

  function dashboardProjection() {
    var open = S.invoices.filter(function (i) { return i.status === "issued"; });
    var owing = open.reduce(function (a, i) { return a + i.total_cents; }, 0);
    var newDocs = S.documents.filter(function (d) { return d.uploaded_by === "owner"; }).length;
    var filingsAttention = S.filings.filter(function (f) { return f.status === "awaiting_info"; })
      .map(function (f) { return { kind_label: f.kind_label, tax_year: f.tax_year, note: f.note }; });
    var invoicesAttention = open.filter(function (i) { return Date.parse(i.due_at) < Date.now(); })
      .map(function (i) { return { invoice_number: i.invoice_number, due_at: i.due_at }; });
    var docsAttention = S.documents.filter(function (d) { return d.uploaded_by === "owner"; }).slice(0, 3)
      .map(function (d) { return { original_filename: d.original_filename }; });
    var firstFiling = S.filings[0];
    return {
      greeting: greeting(),
      needs_attention: { filings: filingsAttention, invoices: invoicesAttention, documents: docsAttention },
      snapshots: {
        filings: firstFiling ? [{ kind_label: firstFiling.kind_label, tax_year: firstFiling.tax_year, status: firstFiling.status }] : [],
        documents: { count: S.documents.filter(function (d) { return !d.deleted_at; }).length, new_count: newDocs },
        invoices: { open_count: open.length, total_owing_cents: owing },
      },
      activity: [
        { at: daysFromNow(-0.4), summary: "Jared shared 2025 T1 Return — final" },
        { at: daysFromNow(-2.1), summary: "Your GST/HST 2025 filing moved to Filed with CRA" },
        { at: daysFromNow(-6.8), summary: "Invoice JARO-2026-0041 issued" },
        { at: daysFromNow(-9.3), summary: "You uploaded T4_2025.pdf" },
      ],
    };
  }

  /* ================= admin routes ================= */

  var FILING_LABELS = {
    personal_t1: "Personal T1", corporate_t2: "Corporate T2",
    nonprofit: "Non-profit", nil_return: "Nil return", gst_hst: "GST/HST",
  };

  function auditAction(action, entityType) {
    S.audit.unshift({
      id: uid("audit"), occurred_at: nowISO(),
      action: action, actor: "Jared Robbins (stub)", entity_type: entityType || "",
    });
  }

  function requireOwner() {
    var a = requireAuth();
    if (a) return a;
    if (S.session.role !== "owner") {
      return errResponse("forbidden", "That area is for Jared only.", 403);
    }
    return null;
  }

  function adminRoutes(method, parts, query, body) {
    var denied = requireOwner();
    if (denied) return denied;
    var p0 = parts[0] || "";

    if (p0 === "clients" && parts.length === 1 && method === "GET") {
      var search = (query.get("search") || "").toLowerCase();
      var items = S.clients
        .filter(function (c) {
          return !search || (c.display_name + " " + c.email).toLowerCase().indexOf(search) !== -1;
        })
        .map(function (c) { return { id: c.id, display_name: c.display_name, email: c.email, status: c.status }; });
      return jsonResponse({ items: items, page: 1, per_page: 50, total: items.length });
    }

    if (p0 === "clients" && parts.length === 2 && method === "GET") {
      var cl = S.clients.find(function (x) { return x.id === parts[1]; });
      if (!cl) return notFound();
      return jsonResponse({
        id: cl.id, display_name: cl.display_name, email: cl.email, status: cl.status,
        phone_e164: cl.phone_e164, phone_verified_at: cl.phone_verified_at,
        must_change_password: !!cl.must_change_password, terms_accepted_at: cl.terms_accepted_at,
        last_login_at: cl.last_login_at, internal_note: cl.internal_note,
        documents: S.documents.filter(function (d) { return !d.deleted_at; }),
        filings: S.filings.map(function (f) { return { id: f.id, kind_label: f.kind_label, tax_year: f.tax_year, status: f.status, note: f.note }; }),
        invoices: S.invoices.map(function (i) { return { id: i.id, invoice_number: i.invoice_number, total_cents: i.total_cents, status: i.status }; }),
        activity: S.audit.slice(0, 20),
      });
    }

    if (p0 === "clients" && parts.length === 2 && method === "PATCH") {
      var cl2 = S.clients.find(function (x) { return x.id === parts[1]; });
      if (!cl2) return notFound();
      if (body && typeof body.internal_note === "string") {
        cl2.internal_note = body.internal_note.slice(0, 5000);
        auditAction("dossier.note_saved", "client");
        save();
      }
      return jsonResponse({ ok: true });
    }

    if (p0 === "clients" && parts[2] === "sessions" && parts[3] === "revoke-all" && method === "POST") {
      auditAction("client.sessions_revoked", "client");
      save();
      return jsonResponse({ revoked_count: 1 });
    }

    if (p0 === "clients" && parts[2] === "2fa" && parts[3] === "disable" && method === "POST") {
      if (!(body && body.reason)) {
        return errResponse("validation_failed", "A reason is required.", 422,
          [{ field: "reason", message: "Tell us why (it's audited)." }]);
      }
      S.totpEnrolled = false;
      auditAction("client.2fa_disabled", "client");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "clients" && parts[2] === "documents" && parts[4] === "retire" && method === "POST") {
      var doc = S.documents.find(function (d) { return d.id === parts[3]; });
      if (!doc) return notFound();
      doc.deleted_at = (body && body.retired === false) ? null : nowISO();
      auditAction("document.retired", "document");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "clients" && parts[2] === "documents" && parts.length === 3 && method === "POST") {
      var f = body || {};
      var up = {
        id: uid("doc"),
        label: null,
        original_filename: f.__filename || "upload.bin",
        type_code: f.type_code || "other",
        type_label: "Other",
        byte_size: parseInt(f.__size || "0", 10) || 1024,
        created_at: nowISO(),
        uploaded_by: "owner",
      };
      S.documents.unshift(up);
      auditAction("document.shared", "document");
      save();
      return jsonResponse({ id: up.id }, 201);
    }

    if (p0 === "filings" && parts.length === 1 && method === "POST") {
      var fv = body || {};
      var kindLabel = FILING_LABELS[fv.kind_code] || "Personal T1";
      var filing = {
        id: uid("fil"), kind_code: fv.kind_code || "personal_t1",
        kind_label: kindLabel, tax_year: String(fv.tax_year || new Date().getUTCFullYear() - 1),
        status: "collecting", note: null,
        timeline: [{ status: "collecting", at: nowISO() }],
      };
      S.filings.unshift(filing);
      auditAction("filing.created", "filing");
      save();
      return jsonResponse({ id: filing.id }, 201);
    }

    if (p0 === "filings" && parts[2] === "status" && method === "PATCH") {
      var fl = S.filings.find(function (x) { return x.id === parts[1]; });
      if (!fl) return notFound();
      var st = (body || {}).status;
      if (["collecting", "awaiting_info", "preparing", "filed", "confirmed"].indexOf(st) === -1) {
        return errResponse("validation_failed", "Unknown status.", 422,
          [{ field: "status", message: "Pick a valid filing status." }]);
      }
      fl.status = st;
      if (body && body.note) fl.note = String(body.note).slice(0, 2000);
      fl.timeline.push({ status: st, at: nowISO() });
      auditAction("filing.status_changed", "filing");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "invoices" && parts.length === 1 && method === "POST") {
      var iv = body || {};
      var lines = (iv.lines || []).map(function (l) {
        var qty = parseFloat(l.quantity) || 0;
        var unit = parseInt(l.unit_cents, 10) || 0;
        var bps = parseInt(l.tax_rate_bps, 10) || 0;
        var pre = Math.round(qty * unit);
        var tax = Math.round(pre * bps / 10000);
        return {
          description: l.description || "Service",
          quantity: qty, unit_cents: unit, tax_rate_bps: bps,
          line_total_cents: pre + tax,
        };
      });
      if (!lines.length) {
        return errResponse("validation_failed", "Add at least one line item.", 422,
          [{ field: "lines", message: "Add at least one line item." }]);
      }
      var sub = lines.reduce(function (a, l) { return a + Math.round(l.quantity * l.unit_cents); }, 0);
      var tax = lines.reduce(function (a, l) { return a + (l.line_total_cents - Math.round(l.quantity * l.unit_cents)); }, 0);
      var inv = {
        id: uid("inv"),
        invoice_number: "JARO-" + new Date().getUTCFullYear() + "-" + String(S.invoiceSeq++).padStart(4, "0"),
        issued_at: nowISO(),
        due_at: new Date(Date.now() + 30 * 86400000).toISOString(),
        status: "issued",
        memo: (iv.memo || "").slice(0, 2000) || null,
        lines: lines,
        subtotal_cents: sub, tax_cents: tax, total_cents: sub + tax,
      };
      S.invoices.unshift(inv);
      auditAction("invoice.issued", "invoice");
      save();
      return jsonResponse({ id: inv.id, invoice_number: inv.invoice_number }, 201);
    }

    if (p0 === "invoices" && parts[2] === "pay" && method === "POST") {
      var inv2 = S.invoices.find(function (x) { return x.id === parts[1]; });
      if (!inv2) return notFound();
      inv2.status = "paid";
      inv2.pay_method_note = (body || {}).pay_method_note || null;
      auditAction("invoice.paid", "invoice");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "invoices" && parts[2] === "void" && method === "POST") {
      var inv3 = S.invoices.find(function (x) { return x.id === parts[1]; });
      if (!inv3) return notFound();
      if (!(body && body.reason)) {
        return errResponse("validation_failed", "A reason is required.", 422,
          [{ field: "reason", message: "Void reason is required (audited)." }]);
      }
      inv3.status = "void";
      auditAction("invoice.voided", "invoice");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "invitations" && parts.length === 1 && method === "GET") {
      return jsonResponse({ items: S.invitations, page: 1, per_page: 25, total: S.invitations.length });
    }

    if (p0 === "invitations" && parts.length === 1 && method === "POST") {
      var nv = body || {};
      if (!nv.client_name || !emailValid(nv.email || "")) {
        return errResponse("validation_failed", "Please fix the following:", 422, [
          { field: "client_name", message: "Client name is required." },
          { field: "email", message: "Please enter a valid email address." },
        ].filter(function (x) { return x.field === "client_name" ? !nv.client_name : !emailValid(nv.email || ""); }));
      }
      if (!nv.temp_password || nv.temp_password.length < 12)
        return errResponse("validation_failed", "Please fix the following:", 422,
          [{ field: "temp_password", message: "Use at least 12 characters." }]);
      var invite = {
        id: "inv_" + String(S.inviteSeq++).padStart(4, "0"),
        client_name: nv.client_name, email: nv.email,
        status: "accepted", expires_at: daysFromNow(7), created_at: nowISO(),
      };
      S.invitations.unshift(invite);
      auditAction("invitation.created", "invitation");
      save();
      return jsonResponse({ id: invite.id, status: "accepted" }, 201);
    }

    if (p0 === "invitations" && parts[2] === "revoke" && method === "POST") {
      var inv5 = S.invitations.find(function (x) { return x.id === parts[1]; });
      if (!inv5) return notFound();
      inv5.status = "revoked";
      auditAction("invitation.revoked", "invitation");
      save();
      return jsonResponse({ ok: true });
    }

    if (p0 === "audit" && method === "GET") {
      return jsonResponse({ items: S.audit.slice(0, 50), page: 1, per_page: 50, total: S.audit.length });
    }

    return notFound();
  }

  /* ================= downloads (anchor interception) ================= */

  function pdfEscape(s) {
    return String(s).replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)").slice(0, 140);
  }
  function makePdf(title, lines) {
    var content = "BT /F1 14 Tf 72 740 Td 18 TL (" + pdfEscape(title) + ") Tj T* /F1 11 Tf ";
    lines.forEach(function (l) { content += "(" + pdfEscape(l) + ") Tj T* "; });
    content += "ET";
    var objs = [
      "<< /Type /Catalog /Pages 2 0 R >>",
      "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
      "<< /Length " + content.length + " >>\nstream\n" + content + "\nendstream",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ];
    var pdf = "%PDF-1.4\n", offsets = [];
    objs.forEach(function (o, i) {
      offsets.push(pdf.length);
      pdf += (i + 1) + " 0 obj\n" + o + "\nendobj\n";
    });
    var xref = pdf.length;
    pdf += "xref\n0 " + (objs.length + 1) + "\n0000000000 65535 f \n";
    offsets.forEach(function (off) { pdf += String(off).padStart(10, "0") + " 00000 n \n"; });
    pdf += "trailer\n<< /Size " + (objs.length + 1) + " /Root 1 0 R >>\nstartxref\n" + xref + "\n%%EOF";
    return new Blob([pdf], { type: "application/pdf" });
  }
  function fmtUtc(iso) {
    return iso.replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
  }
  function bookingIcs(booking) {
    var ics = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jaro Accounting//Stub//EN", "BEGIN:VEVENT",
      "UID:" + booking.id + "@stub.jaroaccounting.com",
      "DTSTAMP:" + fmtUtc(nowISO()),
      "DTSTART:" + fmtUtc(booking.slot_start),
      "DTEND:" + fmtUtc(booking.slot_end),
      "SUMMARY:Free 30-Min Consultation \\u2014 Phone Call (Jaro Accounting)",
      "DESCRIPTION:Reference " + booking.reference,
      "END:VEVENT", "END:VCALENDAR", ""].join("\r\n");
    return new Blob([ics], { type: "text/calendar" });
  }
  function downloadBlob(blob, filename) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }

  /** Handle <a href=".../api/v1/..."> navigations (downloads) inside stub mode. */
  function downloadForApiPath(path) {
    path = stripBase(path);
    var m;
    if ((m = path.match(/^\/api\/v1\/booking\/manage\/([^/]+)\/ics$/))) {
      var bookingId = S.manageTokens[m[1]];
      var booking = bookingId && S.bookings[bookingId];
      if (!booking || booking.status !== "confirmed") return false;
      downloadBlob(bookingIcs(booking), "booking-" + booking.reference + ".ics");
      return true;
    }
    if ((m = path.match(/^\/api\/v1\/portal\/invoices\/([^/]+)\/pdf$/))) {
      var inv = S.invoices.find(function (x) { return x.id === m[1]; });
      if (!inv) return false;
      var lines = [
        "Issued: " + inv.issued_at.slice(0, 10) + "    Due: " + inv.due_at.slice(0, 10) + "    Status: " + inv.status.toUpperCase(),
        "",
      ];
      inv.lines.forEach(function (l) {
        lines.push(l.description + "  x" + l.quantity + " @ $" + (l.unit_cents / 100).toFixed(2) +
          (l.tax_rate_bps ? " + " + (l.tax_rate_bps / 100).toFixed(2) + "% GST" : "") +
          "  = $" + (l.line_total_cents / 100).toFixed(2));
      });
      lines.push("", "Subtotal: $" + (inv.subtotal_cents / 100).toFixed(2),
        "GST: $" + (inv.tax_cents / 100).toFixed(2),
        "TOTAL DUE: $" + (inv.total_cents / 100).toFixed(2));
      if (inv.memo) { lines.push("", "Memo: " + inv.memo); }
      lines.push("", "(Stub preview — illustrative document, not a real invoice.)");
      downloadBlob(makePdf("Jaro Accounting — Invoice " + inv.invoice_number, lines), inv.invoice_number + ".pdf");
      return true;
    }
    if ((m = path.match(/^\/api\/v1\/(portal|admin)\/.*\/documents\/([^/]+)\/download$/) ||
             path.match(/^\/api\/v1\/portal\/documents\/([^/]+)\/download$/))) {
      var docId = m[2] || m[1];
      var doc = S.documents.find(function (d) { return d.id === docId && !d.deleted_at; });
      if (!doc) return false;
      var name = doc.original_filename;
      if (/\.pdf$/i.test(name)) {
        downloadBlob(makePdf("Jaro Accounting — " + (doc.label || name),
          ["File: " + name, "Shared: " + doc.created_at.slice(0, 10),
           "", "(Stub preview — illustrative document, not the real file.)"]), name);
      } else {
        downloadBlob(new Blob(["Stub preview of " + name + "\n"], { type: "text/plain" }), name);
      }
      return true;
    }
    return false;
  }

  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href*="/api/v1/"]') : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (href.indexOf("/api/v1/") === -1) return;
    var path;
    try { path = new URL(href, window.location.origin).pathname; }
    catch (err) { return; }
    if (downloadForApiPath(path)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

  /* ================= fetch interception ================= */

  var origFetch = window.fetch.bind(window);

  function normHeaders(h) {
    var out = {};
    if (!h) return out;
    if (typeof h.forEach === "function") {
      h.forEach(function (v, k) { out[String(k).toLowerCase()] = v; });
    } else {
      for (var k in h) out[String(k).toLowerCase()] = h[k];
    }
    return out;
  }

  function parseBody(bodyInit) {
    if (bodyInit == null) return {};
    if (typeof bodyInit === "string") {
      try { return JSON.parse(bodyInit); } catch (e) { return {}; }
    }
    if (typeof FormData !== "undefined" && bodyInit instanceof FormData) {
      var form = {};
      bodyInit.forEach(function (value, key) {
        if (value && typeof value === "object" && typeof value.name === "string") {
          form.__filename = value.name;
          form.__size = String(value.size || 0);
        } else {
          form[key] = value;
        }
      });
      return form;
    }
    return {};
  }

  function route(method, urlStr, headers, body) {
    var u, path, query;
    try {
      u = new URL(urlStr, window.location.origin);
      path = stripBase(u.pathname);
      query = u.searchParams;
    } catch (e) { return null; }
    if (path.indexOf("/api/") !== 0) return null;
    var segs = path.split("/").filter(Boolean);
    if (segs[0] !== "api" || segs[1] !== "v1") return null;
    var rest = segs.slice(2);
    var base = rest[0] || "";
    var sub = rest.slice(1);
    var res = null;
    if (base === "booking") res = bookingRoutes(method, sub, query, body, headers);
    else if (base === "inquiries") res = inquiriesRoutes(method, sub, body, headers);
    else if (base === "portal") {
      res = portalAuthRoutes(method, sub, query, body) ||
            portalClientRoutes(method, sub, query, body, headers);
    }
    else if (base === "admin") res = adminRoutes(method, sub, query, body);
    return res || notFound();
  }

  window.fetch = function (input, init) {
    init = init || {};
    if (typeof input !== "string") return origFetch(input, init); // app never does this
    var method = (init.method || "GET").toUpperCase();
    var headers = normHeaders(init.headers);
    var path;
    try { path = new URL(input, window.location.origin).pathname; }
    catch (e) { return origFetch(input, init); }
    if (stripBase(path).indexOf("/api/") !== 0) return origFetch(input, init); // non-/api: real network
    var res;
    try {
      res = route(method, input, headers, parseBody(init.body));
    } catch (err) {
      res = errResponse("stub_error", "Stub backend error: " + (err && err.message), 500);
    }
    if (!res) res = notFound();
    var delay = latency();
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(res); }, delay);
    });
  };

  /* ================= stub badge ================= */

  var badgeEl = null;

  var BADGE_CSS = "position:fixed;right:12px;bottom:12px;z-index:2147483647;" +
    "font:12px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
    "color:#1a1a1a;";
  var BADGE_BTN_CSS = "background:#CD2220;color:#fff;border:0;border-radius:999px;" +
    "padding:8px 14px;font-weight:700;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25);";
  var BADGE_PANEL_CSS = "margin-top:8px;background:#fff;border:1px solid #ddd;border-radius:10px;" +
    "padding:12px 14px;max-width:320px;max-height:60vh;overflow:auto;" +
    "box-shadow:0 4px 20px rgba(0,0,0,.2);";

  function ensureBadge() {
    if (badgeEl || !document.body) return;
    badgeEl = document.createElement("div");
    badgeEl.id = "jaro-stub-badge";
    badgeEl.setAttribute("style", BADGE_CSS);
    badgeEl.innerHTML =
      '<button type="button" id="jaro-stub-toggle" style="' + BADGE_BTN_CSS + '">STUB MODE</button>' +
      '<div id="jaro-stub-panel" hidden style="' + BADGE_PANEL_CSS + '"></div>';
    document.body.appendChild(badgeEl);
    document.getElementById("jaro-stub-toggle").addEventListener("click", function () {
      var p = document.getElementById("jaro-stub-panel");
      p.hidden = !p.hidden;
      if (!p.hidden) badgeRefresh();
    });
  }

  function kv(k, v) {
    return '<div><strong>' + k + ':</strong> <code>' + v + "</code></div>";
  }

  function badgeRefresh() {
    var p = document.getElementById("jaro-stub-panel");
    if (!p || p.hidden) return;
    var html = "<p style='margin:0 0 8px'><strong>Demo backend</strong> — no real data moves. " +
      "Flows behave like production against realistic fixtures.</p>";
    html += "<p style='margin:0 0 4px'><strong>Sign in</strong></p>";
    html += kv("Client", DEMO_CLIENT.email + " / " + DEMO_CLIENT.password);
    html += kv("Temp-password demo", DEMO_TEMP_CLIENT.email + " / " + DEMO_TEMP_CLIENT.password + " (must choose own password)");
    html += kv("Jared (owner)", DEMO_OWNER.email + " / " + DEMO_OWNER.password);
    if (S.lastBookingCode) {
      html += "<p style='margin:8px 0 4px'><strong>Latest booking code</strong></p>";
      html += kv(S.lastBookingCode.masked, S.lastBookingCode.code);
    }
    html += "<p style='margin:8px 0 4px'><strong>Deep links</strong></p>";
    html += "<div><a href='" + STUB_BASE + "/portal/reset-password/?token=" + DEMO_RESET_TOKEN + "'>Password reset</a></div>";
    html += "<p style='margin:10px 0 0'>";
    html += "<button type='button' id='jaro-stub-reset' style='margin-right:8px'>Reset demo data</button>";
    html += "<button type='button' id='jaro-stub-off'>Disable stubs</button></p>";
    p.innerHTML = html;
    document.getElementById("jaro-stub-reset").addEventListener("click", function () {
      if (!confirm("Reset all stub demo data?")) return;
      S = seedState();
      seedTakenSlotsIfEmpty();
      save();
      badgeRefresh();
      alert("Demo data reset.");
    });
    document.getElementById("jaro-stub-off").addEventListener("click", function () {
      try { sessionStorage.removeItem(FLAG_KEY); } catch (e) {}
      var u = new URL(window.location.href);
      u.searchParams.set("stub", "0");
      window.location.href = u.toString();
    });
  }

  function seedTakenSlotsIfEmpty() {
    if (!Object.keys(S.takenSlots).length) { seedTakenSlots(); }
  }

  /* ================= init ================= */

  window.JaroStub = {
    reset: function () { S = seedState(); seedTakenSlotsIfEmpty(); save(); badgeRefresh(); },
    state: function () { return S; },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureBadge);
  } else {
    ensureBadge();
  }
  setInterval(function () {
    var p = document.getElementById("jaro-stub-panel");
    if (p && !p.hidden) badgeRefresh();
  }, 5000);
})();
