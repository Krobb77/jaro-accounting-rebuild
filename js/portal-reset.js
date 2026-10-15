/* /portal/reset-password — request + complete (token in query string). */
import { portalApi, ApiRequestError, idempotencyKey } from "./portal-api.js";

const errorBox = document.getElementById("reset-error");
const requestForm = document.getElementById("reset-request-form");
const sentNote = document.getElementById("reset-sent");
const completeForm = document.getElementById("reset-complete-form");

const token = new URLSearchParams(window.location.search).get("token");

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

if (token) {
  // Verify first so an expired link shows a clear message before typing.
  requestForm.hidden = true;
  portalApi.post("/password-reset/verify", { json: { token } }).then(
    () => { completeForm.hidden = false; },
    () => {
      showError("This reset link isn't valid anymore. Request a new one below.");
      requestForm.hidden = false;
    }
  );
}

requestForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const email = document.getElementById("reset-email").value.trim();
  const submit = document.getElementById("reset-request-submit");
  submit.disabled = true;
  try {
    // Always 200 — the message stays the same whether or not the email exists.
    await portalApi.post("/password-reset/request", { json: { email } });
    requestForm.hidden = true;
    sentNote.hidden = false;
  } catch (err) {
    showError(err.message || "Something went wrong. Try again.");
  } finally {
    submit.disabled = false;
  }
});

completeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const pw = document.getElementById("reset-password").value;
  const pw2 = document.getElementById("reset-password2").value;
  if (pw !== pw2) {
    showError("The two passwords don't match.");
    return;
  }
  const submit = document.getElementById("reset-complete-submit");
  submit.disabled = true;
  try {
    await portalApi.post("/password-reset/complete", {
      json: { token, new_password: pw },
      headers: { "Idempotency-Key": idempotencyKey() },
    });
    window.location.href = "/jaro-accounting-rebuild/portal/login?reason=reset-complete";
  } catch (err) {
    if (err instanceof ApiRequestError && err.code === "validation_failed") {
      showError(err.fields.map((f) => f.message).join(" ") || err.message);
    } else {
      showError(err.message || "Something went wrong. Try again.");
    }
  } finally {
    submit.disabled = false;
  }
});
