/* /portal/login — password-only sign in (Mawer-informed, 2026-10-15).
 *
 * Interaction patterns borrowed from Mawer's public login (behavior only,
 * Jaro's look kept):
 * - single-step email + password form;
 * - inline per-field errors: red border + "!" icon via aria-invalid, short
 *   message under the field, no navigation on validation failure;
 * - show/hide password toggle that appears only once the field is non-empty;
 * - submit button shows a busy spinner and double-submit is disabled;
 * - server credential errors stay generic (anti-enumeration);
 * - a forced password-change screen for temporary-password onboarding.
 */
import { portalApi, ApiRequestError, pollSession } from "./portal-api.js";

const loginForm = document.getElementById("login-form");
const changeForm = document.getElementById("change-password-form");
const errorBox = document.getElementById("login-error");
const expiredNote = document.getElementById("login-expired-note");

function setFieldError(input, errorEl, message) {
  if (message) {
    input.setAttribute("aria-invalid", "true");
    errorEl.textContent = message;
    errorEl.hidden = false;
  } else {
    input.removeAttribute("aria-invalid");
    errorEl.textContent = "";
    errorEl.hidden = true;
  }
}

function clearFieldErrorOnInput(input, errorEl) {
  input.addEventListener("input", () => setFieldError(input, errorEl, ""));
  input.addEventListener("blur", () => {
    // Re-validate on blur so a corrected field clears its error promptly,
    // matching the login page's submit-time rules.
    if (input.value.trim() !== "") setFieldError(input, errorEl, "");
  });
}

function showError(err) {
  errorBox.hidden = false;
  if (err instanceof ApiRequestError && err.code === "invalid_credentials") {
    // Anti-enumeration: the backend returns identical 401s; keep the copy generic.
    errorBox.textContent = "That email and password didn't match our records. Try again.";
  } else if (err instanceof ApiRequestError && err.code === "rate_limited") {
    errorBox.textContent = "Too many attempts. Please wait a moment and try again.";
  } else if (err instanceof ApiRequestError && err.code === "password_change_required") {
    showChangeForm();
    return;
  } else {
    errorBox.textContent = err.message || "Something went wrong. Try again.";
  }
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function setBusy(button, busy) {
  button.disabled = busy;
  if (busy) {
    button.setAttribute("aria-busy", "true");
  } else {
    button.removeAttribute("aria-busy");
  }
}

// Show/hide password toggle: visible only while the field is non-empty.
function wirePasswordToggle(inputId, toggleId) {
  const input = document.getElementById(inputId);
  const toggle = document.getElementById(toggleId);
  const sync = () => { toggle.hidden = input.value.length === 0; };
  input.addEventListener("input", sync);
  sync();
  toggle.addEventListener("click", () => {
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    toggle.textContent = showing ? "Show" : "Hide";
    toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    toggle.setAttribute("aria-pressed", String(!showing));
    input.focus();
  });
}

// Already signed in? Bounce to the dashboard — unless the session is a
// temporary-password one, in which case show the forced-change form here.
pollSession().then(
  (session) => {
    if (session && session.must_change_password) {
      showChangeForm();
    } else {
      window.location.href = "/jaro-accounting-rebuild/portal/";
    }
  },
  () => {}
);

const params = new URLSearchParams(window.location.search);
if (params.get("reason") === "session-expired") {
  expiredNote.hidden = false;
}

const emailInput = document.getElementById("login-email");
const passwordInput = document.getElementById("login-password");
const emailError = document.getElementById("login-email-error");
const passwordError = document.getElementById("login-password-error");
clearFieldErrorOnInput(emailInput, emailError);
clearFieldErrorOnInput(passwordInput, passwordError);
wirePasswordToggle("login-password", "login-password-toggle");

function validateLogin() {
  let ok = true;
  const email = emailInput.value.trim();
  const password = passwordInput.value;
  if (!email) {
    setFieldError(emailInput, emailError, "Enter your email address.");
    ok = false;
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    setFieldError(emailInput, emailError, "Enter a valid email address.");
    ok = false;
  }
  if (!password) {
    setFieldError(passwordInput, passwordError, "Enter your password.");
    ok = false;
  }
  return ok;
}

function showChangeForm() {
  hideError();
  loginForm.hidden = true;
  changeForm.hidden = false;
  document.getElementById("change-password-heading").scrollIntoView({ block: "nearest" });
  document.getElementById("new-password").focus();
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError();
  if (!validateLogin()) {
    const firstBad = document.querySelector("#login-form [aria-invalid]");
    if (firstBad) firstBad.focus();
    return;
  }
  const submit = document.getElementById("login-submit");
  setBusy(submit, true);
  try {
    const res = await portalApi.post("/login", {
      json: { email: emailInput.value.trim(), password: passwordInput.value },
    });
    // The backend sets the session cookie; follow the next step.
    if (res.next === "change_password") {
      showChangeForm();
    } else if (res.next === "terms") {
      window.location.href = "/jaro-accounting-rebuild/portal/?first=1";
    } else {
      window.location.href = "/jaro-accounting-rebuild/portal/";
    }
  } catch (err) {
    showError(err);
  } finally {
    setBusy(submit, false);
  }
});

// --- forced password change (temporary-password onboarding) -----------------

const newPasswordInput = document.getElementById("new-password");
const newPasswordConfirm = document.getElementById("new-password-confirm");
const newPasswordError = document.getElementById("new-password-error");
const newPasswordConfirmError = document.getElementById("new-password-confirm-error");
clearFieldErrorOnInput(newPasswordInput, newPasswordError);
clearFieldErrorOnInput(newPasswordConfirm, newPasswordConfirmError);
wirePasswordToggle("new-password", "new-password-toggle");

changeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError();
  let ok = true;
  const pw = newPasswordInput.value;
  const confirm = newPasswordConfirm.value;
  if (pw.length < 12) {
    setFieldError(newPasswordInput, newPasswordError, "Use at least 12 characters.");
    ok = false;
  }
  if (confirm !== pw) {
    setFieldError(newPasswordConfirm, newPasswordConfirmError, "Passwords don't match.");
    ok = false;
  }
  if (!ok) {
    const firstBad = document.querySelector("#change-password-form [aria-invalid]");
    if (firstBad) firstBad.focus();
    return;
  }
  const submit = document.getElementById("change-password-submit");
  setBusy(submit, true);
  try {
    const res = await portalApi.post("/password/change", { json: { new_password: pw } });
    if (res.next === "terms") {
      window.location.href = "/jaro-accounting-rebuild/portal/?first=1";
    } else {
      window.location.href = "/jaro-accounting-rebuild/portal/";
    }
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "validation_failed" && err.fields) {
      for (const f of err.fields) {
        if (f.field === "new_password") setFieldError(newPasswordInput, newPasswordError, f.message);
        else showError(err);
      }
      const firstBad = document.querySelector("#change-password-form [aria-invalid]");
      if (firstBad) firstBad.focus();
    } else {
      showError(err);
    }
  } finally {
    setBusy(submit, false);
  }
});
