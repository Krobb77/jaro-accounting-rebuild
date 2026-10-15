/**
 * Contact form — Phase 2 lead pipeline wiring.
 *
 * Figmentry playbook mapping:
 *  C1 typed transport: one fetch dispatcher; every backend failure — the
 *     typed A1 envelope {code, message, fields[], request_id}, HTTP
 *     statuses, unreachable service — normalizes to ApiRequestError
 *     {message, status, code, requestId, fields, retryAfter}.
 *  C2 error-code recovery: the catch switches on error.code and each known
 *     failure gets a tailored recovery — per-field plain-language errors,
 *     idempotency-conflict guidance, rate-limit wait copy, and a
 *     try-again panel for server/network failures. Unknown errors fall
 *     back to the single error slot. No branch offers an action the
 *     recovery path doesn't support.
 *  C3 safe async: a busy state machine — the submit button is disabled
 *     while a request is in flight and is ALWAYS re-enabled in `finally`
 *     (the success path replaces the form instead). A second submit while
 *     busy is ignored, so double-clicks can't double-queue.
 *  C4 submit contract: crypto.randomUUID() (CSPRNG) generated per submit
 *     ATTEMPT — a retry is a new attempt with a fresh key, never a replay
 *     of a stale one — sent as Idempotency-Key; the error slot is cleared
 *     on submit; native HTML constraints (required, type=email, maxlength)
 *     own client-side validation so the server only sees valid shapes.
 *
 * Honest vocabulary (F7): the success panel says "queued", never "sent".
 */

const form = document.getElementById("contact-form");
if (form) {
  wireContactForm(form);
}

function wireContactForm(form) {
  // data-endpoint is read per submit attempt (not once at wire time) so a
  // test harness — or a future multi-endpoint page — can retarget the form
  // without reloading.
  const getEndpoint = () => form.getAttribute("data-endpoint") || "/api/v1/inquiries";
  const submitBtn = form.querySelector('[type="submit"]');
  const submitLabel = submitBtn.textContent;
  const summary = document.getElementById("contact-error-summary");
  const successPanel = document.getElementById("contact-success");
  const referenceSlot = document.getElementById("contact-reference");
  const inputs = {
    name: document.getElementById("contact-name"),
    email: document.getElementById("contact-email"),
    message: document.getElementById("contact-message"),
  };
  let busy = false;

  // -- C1: typed transport -------------------------------------------------

  class ApiRequestError extends Error {
    constructor({ message, status, code, requestId, fields, retryAfter }) {
      super(message);
      this.name = "ApiRequestError";
      this.status = status;
      this.code = code;
      this.requestId = requestId;
      this.fields = fields || [];
      this.retryAfter = retryAfter;
    }
  }

  async function postInquiry(endpoint, payload, idempotencyKey) {
    let res;
    try {
      res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      throw new ApiRequestError({
        message: "We couldn't reach our server. Check your connection and try again.",
        status: 0,
        code: "network_unreachable",
        requestId: null,
      });
    }
    let body = null;
    try {
      body = await res.json();
    } catch (err) {
      body = null;
    }
    if (!res.ok || !body || typeof body !== "object") {
      const envelope = body && typeof body === "object" ? body : {};
      throw new ApiRequestError({
        message:
          envelope.message ||
          "Something went wrong on our end — your message was not received.",
        status: res.status,
        code: envelope.code || "unknown_error",
        requestId: envelope.request_id || res.headers.get("X-Request-ID"),
        fields: Array.isArray(envelope.fields) ? envelope.fields : [],
        retryAfter: res.headers.get("Retry-After"),
      });
    }
    return body;
  }

  // -- error rendering ------------------------------------------------------

  function clearErrors() {
    summary.hidden = true;
    summary.textContent = "";
    for (const key of Object.keys(inputs)) {
      const input = inputs[key];
      if (!input) continue;
      input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby");
      const err = document.getElementById(`contact-${key}-error`);
      if (err) err.remove();
    }
  }

  function fieldError(input, key, message) {
    const errId = `contact-${key}-error`;
    let err = document.getElementById(errId);
    if (!err) {
      err = document.createElement("p");
      err.className = "field-error";
      err.id = errId;
      input.insertAdjacentElement("afterend", err);
    }
    err.textContent = message;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", errId);
  }

  function showSummary(title, items, { retryable } = {}) {
    summary.textContent = "";
    const h2 = document.createElement("h2");
    h2.textContent = title;
    h2.tabIndex = -1;
    summary.appendChild(h2);
    if (items.length) {
      const ul = document.createElement("ul");
      for (const item of items) {
        const li = document.createElement("li");
        if (item.href) {
          const a = document.createElement("a");
          a.href = item.href;
          a.textContent = item.text;
          li.appendChild(a);
        } else {
          li.textContent = item.text;
        }
        ul.appendChild(li);
      }
      summary.appendChild(ul);
    }
    if (retryable) {
      const p = document.createElement("p");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn";
      btn.id = "contact-retry";
      btn.textContent = "Try again";
      btn.addEventListener("click", () => void handleSubmit());
      p.appendChild(btn);
      summary.appendChild(p);
    }
    summary.hidden = false;
    h2.focus();
  }

  // -- C2: error-code-driven recovery ----------------------------------------

  function recover(err) {
    if (err.code === "validation_failed" && err.fields.length) {
      const items = [];
      for (const f of err.fields) {
        const input = inputs[f.field];
        if (input) {
          fieldError(input, f.field, f.message);
          items.push({ text: f.message, href: `#${input.id}` });
        } else {
          items.push({ text: f.message });
        }
      }
      showSummary("Please fix the following:", items);
      return;
    }
    if (err.code === "idempotency_conflict") {
      // Same key, different payload: the earlier submit may already be
      // saved. A fresh key is minted automatically on the next submit.
      showSummary("Almost there", [
        {
          text: "This looks like a message that was already received with different details. Please review your message and submit again.",
        },
      ]);
      return;
    }
    if (err.code === "idempotency_key_required") {
      showSummary("Something went wrong", [
        { text: "We couldn't prepare your message. Please reload the page and try again." },
      ]);
      return;
    }
    if (err.code === "rate_limited") {
      const wait = err.retryAfter ? ` Please wait about ${err.retryAfter} seconds and try again.` : " Please wait a little while and try again.";
      showSummary("Too many messages", [
        { text: `We've received a lot of messages just now.${wait}` },
      ], { retryable: true });
      return;
    }
    // Server / network / unknown: the message was NOT received; retrying
    // with a fresh key is safe (idempotent), and nothing is lost — the
    // field values stay intact.
    const items = [
      {
        text: "Something went wrong on our end — your message was not received. You can try again, or call us at 1-(778)-227-4891.",
      },
    ];
    if (err.requestId) {
      items.push({
        text: `If this keeps happening, mention this code when you call us: ${err.requestId}.`,
      });
    }
    showSummary("We couldn't send your message", items, { retryable: true });
  }

  // -- success ---------------------------------------------------------------

  function showSuccess(body) {
    if (referenceSlot && body.reference) {
      referenceSlot.textContent = body.reference;
    }
    // Q §3.5 #6: the SLA is rendered from the API response (which carries
    // RESPONSE_SLA_DEFAULT) — the frontend never hardcodes its own literal.
    // textContent keeps the injection XSS-safe.
    const slaWrap = document.getElementById("contact-sla-wrap");
    const slaSlot = document.getElementById("contact-sla");
    if (slaSlot && typeof body.response_sla === "string" && body.response_sla) {
      slaSlot.textContent = body.response_sla;
    } else if (slaWrap) {
      slaWrap.hidden = true; // backend didn't supply it: omit rather than guess
    }
    form.hidden = true;
    successPanel.hidden = false;
    const h = successPanel.querySelector("h3");
    if (h) {
      h.tabIndex = -1;
      h.focus();
    }
  }

  // -- C3/C4: submit ----------------------------------------------------------

  async function handleSubmit() {
    if (busy) return; // double-click / double-tap guard
    busy = true;
    clearErrors();
    submitBtn.disabled = true;
    submitBtn.setAttribute("aria-busy", "true");
    submitBtn.textContent = "Sending…";
    // C4: fresh CSPRNG key per submit ATTEMPT — retries never replay a
    // stale key.
    const idempotencyKey = crypto.randomUUID();
    try {
      const payload = {
        name: inputs.name.value,
        email: inputs.email.value,
        message: inputs.message.value,
      };
      const body = await postInquiry(getEndpoint(), payload, idempotencyKey);
      // A replayed submit (same key) returns the identical stored result.
      showSuccess(body);
    } catch (err) {
      recover(err instanceof ApiRequestError ? err : new ApiRequestError({
        message: "Something went wrong on our end — your message was not received.",
        status: 0,
        code: "unknown_error",
        requestId: null,
      }));
    } finally {
      // C3: the form is ALWAYS re-enabled — unless it was replaced by the
      // success panel, in which case there is nothing to re-enable.
      busy = false;
      if (!form.hidden) {
        submitBtn.disabled = false;
        submitBtn.removeAttribute("aria-busy");
        submitBtn.textContent = submitLabel;
      }
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault(); // JS owns submission; native constraints ran first
    void handleSubmit();
  });
}
