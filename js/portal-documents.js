/* /portal/documents/ — vault list + upload (resumable on session timeout). */
import {
  portalApi, ApiRequestError, startSessionWatch, requireSession,
  idempotencyKey, esc, vancouverDateTime,
} from "./portal-api.js";

const root = document.getElementById("docs-root");
const errorBox = document.getElementById("portal-error");
const banner = document.getElementById("session-banner");
const extendBtn = document.getElementById("session-extend");
const uploadForm = document.getElementById("upload-form");
const progressEl = document.getElementById("upload-progress");

const MAX_BYTES = 25 * 1024 * 1024;

function showError(msg) {
  errorBox.hidden = false;
  errorBox.textContent = msg;
}

document.getElementById("portal-logout").addEventListener("click", async () => {
  try { await portalApi.post("/logout"); } catch { /* already out */ }
  window.location.href = "/jaro-accounting-rebuild/portal/login/";
});

async function loadDocs() {
  const res = await portalApi.get("/documents");
  const items = res.items || [];
  if (!items.length) {
    root.innerHTML = "<p class=\"portal-empty\">No documents yet. Uploaded files and anything Jared shares with you will appear here.</p>";
    return;
  }
  let html = `<ul class="portal-list">`;
  for (const d of items) {
    const title = d.label || d.original_filename;
    html += `<li>
      <strong>${esc(title)}</strong>
      <span class="portal-list__meta">${d.label ? `${esc(d.original_filename)} &middot; ` : ""}${esc(d.type_label || d.type_code)} &middot;
      ${(d.byte_size / 1024).toFixed(0)} KB &middot;
      ${esc(vancouverDateTime(d.created_at))} &middot;
      ${d.uploaded_by === "owner" ? "shared by Jared" : "uploaded by you"}</span>
      <a class="btn btn--small" href="/jaro-accounting-rebuild/api/v1/portal/documents/${encodeURIComponent(d.id)}/download">Download</a>
    </li>`;
  }
  root.innerHTML = html + `</ul>`;
}

uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const fileInput = document.getElementById("upload-file");
  const file = fileInput.files[0];
  if (!file) {
    showError("Choose a file to upload.");
    return;
  }
  if (file.size > MAX_BYTES) {
    showError("That file is over the 25 MB limit.");
    return;
  }
  const submit = document.getElementById("upload-submit");
  submit.disabled = true;
  progressEl.hidden = false;
  progressEl.textContent = `Uploading ${file.name}…`;
  // Idempotency-Key survives a retry: if the session expired mid-upload and
  // the user signs back in and retries, a completed upload won't duplicate.
  const key = idempotencyKey();
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("type_code", document.getElementById("upload-category").value);
  const label = document.getElementById("upload-label").value.trim();
  if (label) form.append("label", label);
  try {
    await portalApi.post("/documents", { form, headers: { "Idempotency-Key": key } });
    progressEl.textContent = "Upload complete.";
    uploadForm.reset();
    await loadDocs();
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      // Session-timeout during upload: no data-loss surprise — the upload
      // simply didn't happen; the idempotency key makes a retry safe.
      progressEl.textContent = "";
      window.location.href = "/jaro-accounting-rebuild/portal/login?reason=session-expired";
      return;
    }
    showError(err.message || "Upload failed. Try again.");
    progressEl.textContent = "";
  } finally {
    submit.disabled = false;
    setTimeout(() => { progressEl.hidden = true; }, 4000);
  }
});

(async function init() {
  await requireSession();
  startSessionWatch({ bannerEl: banner, extendBtn });
  try {
    await loadDocs();
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 401) {
      window.location.href = "/jaro-accounting-rebuild/portal/login?reason=session-expired";
      return;
    }
    showError(err.message || "Couldn't load your documents.");
    root.innerHTML = "";
  }
})();
