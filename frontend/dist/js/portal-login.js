/* /portal/login — branded login + TOTP/SMS second step. */
import { portalApi, ApiRequestError, pollSession } from "./portal-api.js";

const loginForm = document.getElementById("login-form");
const mfaForm = document.getElementById("mfa-form");
const errorBox = document.getElementById("login-error");
const expiredNote = document.getElementById("login-expired-note");
const smsBlock = document.getElementById("sms-block");
const smsCodeField = document.getElementById("sms-code-field");

let challengeId = null;
let smsMode = false;

function showError(err) {
  errorBox.hidden = false;
  if (err instanceof ApiRequestError && err.code === "invalid_credentials") {
    // Anti-enumeration: the backend returns identical 401s; keep the copy generic.
    errorBox.textContent = "That email and password didn't match our records. Try again.";
  } else if (err instanceof ApiRequestError && err.code === "rate_limited") {
    errorBox.textContent = "Too many attempts. Please wait a moment and try again.";
  } else {
    errorBox.textContent = err.message || "Something went wrong. Try again.";
  }
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

// Already signed in? Bounce to the dashboard.
pollSession().then(
  () => { window.location.href = "/portal/"; },
  () => {}
);

const params = new URLSearchParams(window.location.search);
if (params.get("reason") === "session-expired") {
  expiredNote.hidden = false;
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError();
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  if (!email || !password) {
    errorBox.hidden = false;
    errorBox.textContent = "Enter your email address and password.";
    return;
  }
  const submit = document.getElementById("login-submit");
  submit.disabled = true;
  try {
    const res = await portalApi.post("/login", { json: { email, password } });
    challengeId = res.challenge_id;
    smsMode = false;
    loginForm.hidden = true;
    mfaForm.hidden = false;
    smsBlock.hidden = !res.sms_available;
    document.getElementById("mfa-code").focus();
  } catch (err) {
    showError(err);
  } finally {
    submit.disabled = false;
  }
});

document.getElementById("mfa-back").addEventListener("click", () => {
  challengeId = null;
  mfaForm.hidden = true;
  loginForm.hidden = false;
  hideError();
});

document.getElementById("sms-request").addEventListener("click", async () => {
  hideError();
  try {
    await portalApi.post("/login/sms/request", { json: { challenge_id: challengeId } });
    smsMode = true;
    smsCodeField.hidden = false;
    document.getElementById("sms-code").focus();
  } catch (err) {
    showError(err);
  }
});

mfaForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError();
  const submit = document.getElementById("mfa-submit");
  submit.disabled = true;
  try {
    let res;
    if (smsMode) {
      const code = document.getElementById("sms-code").value.trim();
      res = await portalApi.post("/login/sms/verify", {
        json: { challenge_id: challengeId, code },
      });
    } else {
      const code = document.getElementById("mfa-code").value.trim();
      res = await portalApi.post("/login/2fa", {
        json: { challenge_id: challengeId, code },
      });
    }
    // The backend sets the session cookie; follow the next step.
    if (res.next === "terms") {
      window.location.href = "/portal/?first=1";
    } else {
      window.location.href = "/portal/";
    }
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "challenge_consumed") {
      errorBox.hidden = false;
      errorBox.textContent = "That verification session expired. Please sign in again from the start.";
      document.getElementById("mfa-back").click();
    } else {
      showError(err);
    }
  } finally {
    submit.disabled = false;
  }
});
