/* Shared booking helpers — imported by booking.js (widget) and
 * booking-manage.js (tokenized manage page). No side effects on import. */

export const API_BASE = "/api/v1/booking";

export const STORAGE = {
  timezone: "jaro.guest_timezone",
  country: "jaro.guest_country",
  result: "jaro.booking_result",
};

/** API error carrying the backend's A1 envelope code. */
export class ApiRequestError extends Error {
  constructor(status, code, message, fields = []) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

/**
 * JSON wrapper for the booking API. Throws ApiRequestError on non-2xx.
 * `json: undefined` means "no body"; callers must never send secrets in
 * query strings (the manage token travels in the URL path, which the
 * access log redacts — A6).
 */
export async function apiFetch(path, { method = "GET", json, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  let res;
  try {
    res = await fetch(API_BASE + path, init);
  } catch (err) {
    throw new ApiRequestError(0, "network_error", "Couldn't reach the booking service. Check your connection and try again.");
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new ApiRequestError(
      res.status,
      (body && body.code) || "request_failed",
      (body && body.message) || `The booking service returned ${res.status}.`,
      (body && body.fields) || []
    );
  }
  return body;
}

/**
 * Country list for the timezone-first modal and the phone country code.
 * `zones` are that country's main IANA zones; the timezone combobox always
 * lists every IANA zone, with the chosen country's zones grouped first
 * (BOOKING §1: country re-sorts, never restricts).
 */
export const COUNTRIES = [
  { name: "Canada", dial: "+1", zones: ["America/Vancouver", "America/Edmonton", "America/Regina", "America/Winnipeg", "America/Toronto", "America/Halifax", "America/St_Johns"] },
  { name: "United States", dial: "+1", zones: ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"] },
  { name: "Mexico", dial: "+52", zones: ["America/Mexico_City", "America/Cancun", "America/Tijuana"] },
  { name: "United Kingdom", dial: "+44", zones: ["Europe/London"] },
  { name: "Ireland", dial: "+353", zones: ["Europe/Dublin"] },
  { name: "France", dial: "+33", zones: ["Europe/Paris"] },
  { name: "Germany", dial: "+49", zones: ["Europe/Berlin"] },
  { name: "Spain", dial: "+34", zones: ["Europe/Madrid"] },
  { name: "Italy", dial: "+39", zones: ["Europe/Rome"] },
  { name: "Netherlands", dial: "+31", zones: ["Europe/Amsterdam"] },
  { name: "Portugal", dial: "+351", zones: ["Europe/Lisbon"] },
  { name: "Switzerland", dial: "+41", zones: ["Europe/Zurich"] },
  { name: "Australia", dial: "+61", zones: ["Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane", "Australia/Perth"] },
  { name: "New Zealand", dial: "+64", zones: ["Pacific/Auckland"] },
  { name: "India", dial: "+91", zones: ["Asia/Kolkata"] },
  { name: "Japan", dial: "+81", zones: ["Asia/Tokyo"] },
  { name: "China", dial: "+86", zones: ["Asia/Shanghai"] },
  { name: "Singapore", dial: "+65", zones: ["Asia/Singapore"] },
  { name: "Philippines", dial: "+63", zones: ["Asia/Manila"] },
  { name: "South Korea", dial: "+82", zones: ["Asia/Seoul"] },
  { name: "Brazil", dial: "+55", zones: ["America/Sao_Paulo"] },
  { name: "Argentina", dial: "+54", zones: ["America/Argentina/Buenos_Aires"] },
  { name: "South Africa", dial: "+27", zones: ["Africa/Johannesburg"] },
  { name: "United Arab Emirates", dial: "+971", zones: ["Asia/Dubai"] },
];

/** All IANA zones supported by the browser, sorted, for the combobox. */
export function allTimezones() {
  if (typeof Intl.supportedValuesOf === "function") {
    return Intl.supportedValuesOf("timeZone").slice().sort();
  }
  return ["America/Vancouver"];
}

export const RELATIONSHIPS = [
  // Labels match BOOKING §7#13 exactly (D-12); values stay snake_case.
  // "Prospective Client" is preselected in booking.js (D-30, §3.5 #3).
  { value: "prospective_client", label: "Prospective Client" },
  { value: "current_client", label: "Current Client" },
  { value: "referral_professional", label: "Referral from another professional" },
  { value: "other", label: "Other" },
];

/**
 * Render a slot in the guest's zone plus the firm zone (America/Vancouver).
 * Returns { dateLong, timeRange, tzLabel, firmLine }.
 */
export function formatSlot(startISO, endISO, guestTz) {
  const start = new Date(startISO);
  const end = new Date(endISO);
  const dateLong = new Intl.DateTimeFormat("en-US", {
    timeZone: guestTz, weekday: "long", month: "long", day: "numeric", year: "numeric",
  }).format(start);
  const timeFmt = new Intl.DateTimeFormat("en-US", {
    timeZone: guestTz, hour: "numeric", minute: "2-digit", hour12: true,
  });
  const timeRange = `${timeFmt.format(start)} – ${timeFmt.format(end)}`;
  const firmFmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Vancouver", hour: "numeric", minute: "2-digit", hour12: true,
  });
  const firmDate = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Vancouver", weekday: "long", month: "long", day: "numeric", year: "numeric",
  }).format(start);
  const firmLine = `${firmDate} · ${firmFmt.format(start)} – ${firmFmt.format(end)} Vancouver time`;
  return { dateLong, timeRange, tzLabel: guestTz, firmLine };
}

/** "2026-10-13" -> Date at noon UTC (parse-only, no timezone shift). */
export function parseISODate(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
}
