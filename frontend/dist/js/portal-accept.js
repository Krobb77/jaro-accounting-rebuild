/* /portal/accept?token= — invitation verify → set-password. */
import { portalApi, ApiRequestError, idempotencyKey } from "./portal-api.js";

const statusEl = document.getElementById("accept-status");
const errorBox = document.getElementById("accept-error");
const form = document.getElementById("accept-form");
const welcomeEl = document.getElementById("accept-welcome");

const token = new URLSearchParams(window.location.search).get("token");

function fail(message) {
  statusEl.hidden = true;
  errorBox.hidden = false;
  errorBox.innerHTML = "";
  const p = document.createElement("p");
  p.textContent = message;
  errorBox.appendChild(p);
}

if (!token) {
  fail("This invitation link is incomplete. Please use the full link from your invitation email, or ask Jared for a new invitation.");
} else {
  portalApi.post("/invitations/verify", { json: { token } }).then(
    (res) => {
      statusEl.hidden = true;
      form.hidden = false;
      welcomeEl.textContent =
        `Welcome, ${res.client_name}. You're signing in as ${res.email_masked}.`;
      document.getElementById("accept-password").focus();
    },
    (err) => {
      // Single safe 410 for every bad token — keep the copy generic.
      fail("This invitation link isn't valid anymore. It may have expired, already been used, or been replaced. Ask Jared for a new invitation if you need one.");
    }
  );
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const pw = document.getElementById("accept-password").value;
  const pw2 = document.getElementById("accept-password2").value;
  if (pw !== pw2) {
    errorBox.hidden = false;
    errorBox.textContent = "The two passwords don't match.";
    return;
  }
  const submit = document.getElementById("accept-submit");
  submit.disabled = true;
  try {
    await portalApi.post("/invitations/set-password", {
      json: { token, password: pw },
      headers: { "Idempotency-Key": idempotencyKey() },
    });
    window.location.href = "/portal/?first=1";
  } catch (err) {
    errorBox.hidden = false;
    if (err instanceof ApiRequestError && err.code === "validation_failed") {
      const msgs = err.fields.map((f) => f.message).join(" ");
      errorBox.textContent = msgs || err.message;
    } else if (err instanceof ApiRequestError && err.code === "invitation_invalid") {
      fail("This invitation link isn't valid anymore. Ask Jared for a new invitation if you need one.");
    } else {
      errorBox.textContent = err.message || "Something went wrong. Try again.";
    }
  } finally {
    submit.disabled = false;
  }
});
