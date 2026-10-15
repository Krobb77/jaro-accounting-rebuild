"""Invoice PDF rendering (Phase 5, F4).

Why fpdf2 instead of the shared patterns: no PDF primitive exists in the
repo's shared machinery (mail is text/HTML, storage is bytes) and a
hand-rolled stdlib PDF writer cannot do tables, text layout, or font
metrics reliably. fpdf2 is the single new dependency, pinned in
requirements.txt; the render is deterministic (fixed metadata, no
timestamps embedded) so the stored bytes are stable and byte-comparable.

The PDF is generated AT ISSUANCE and stored immutably - the stored bytes
are the invoice of record (PORTAL_V1 F4). Regeneration is never offered;
the download endpoint serves the stored bytes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fpdf import FPDF

from .site_contact import SITE_CONTACT, address_1line

# Brand tokens (match the site's CSS tokens).
_RED = (205, 34, 32)
_CHARCOAL = (48, 48, 48)
_GRAY = (102, 102, 102)
_LIGHT = (242, 242, 242)

# fpdf2 embeds a creation date by default, which would make every render
# byte-different. Pinning the metadata keeps issuance deterministic.
_FIXED_CREATION_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class _InvoicePDF(FPDF):
    def header(self):
        # Charcoal band with the wordmark, red invoice title.
        self.set_fill_color(*_CHARCOAL)
        self.rect(0, 0, 210, 34, "F")
        self.set_xy(12, 9)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 15)
        self.cell(0, 8, "JARO ACCOUNTING", new_x="LMARGIN", new_y="NEXT")
        self.set_x(12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(200, 200, 200)
        self.cell(0, 5, address_1line(), new_x="LMARGIN", new_y="NEXT")  # site_contact.py
        self.set_xy(150, 10)
        self.set_text_color(*_RED)
        self.set_font("Helvetica", "B", 20)
        self.cell(48, 10, "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_y(38)

    def footer(self):
        self.set_y(-18)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*_GRAY)
        self.cell(
            0,
            4,
            f"{SITE_CONTACT['business_name']} · {SITE_CONTACT['legal_name']} · "
            f"{SITE_CONTACT['email']} · {SITE_CONTACT['phone_display']}",
            align="C",
        )


def _latin1(text: str) -> str:
    """fpdf2 core fonts are latin-1 only; sanitize client-controlled text.
    (Em-dashes etc. become ASCII equivalents rather than raising.)"""
    return (
        text.replace("—", "-").replace("–", "-").replace("•", "-")
        .encode("latin-1", errors="replace").decode("latin-1")
    )


def render_invoice_pdf(
    *,
    invoice_number: str,
    issued_date: str,
    due_date: str,
    bill_to_name: str,
    bill_to_email: str,
    lines: list[dict],
    subtotal_cents: int,
    tax_cents: int,
    total_cents: int,
    memo: str | None,
    payment_instructions: str,
    tax_label: str,
    currency: str = "CAD",
) -> bytes:
    """Render the invoice PDF. ``lines``: [{description, quantity (str),
    unit_cents, tax_rate_bps, line_total_cents, tax_cents}]."""
    pdf = _InvoicePDF(format="A4")
    pdf.set_creation_date(_FIXED_CREATION_DATE)
    pdf.set_title(f"Invoice {invoice_number} - Jaro Accounting")
    pdf.set_author("Jaro Accounting")
    pdf.set_auto_page_break(True, margin=22)
    pdf.add_page()

    # Meta block.
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_CHARCOAL)
    pdf.cell(0, 6, f"Invoice number: {invoice_number}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Issued: {issued_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Due: {due_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Bill-to.
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Bill to:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _latin1(bill_to_name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_GRAY)
    pdf.cell(0, 6, _latin1(bill_to_email), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_CHARCOAL)
    pdf.ln(4)

    # Line-item table.
    col_w = [86, 18, 26, 20, 28]  # description, qty, rate, tax, amount
    headers = ["Description", "Qty", "Rate", "Tax", "Amount"]
    pdf.set_fill_color(*_LIGHT)
    pdf.set_font("Helvetica", "B", 9)
    for w, h in zip(col_w, headers):
        pdf.cell(w, 8, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for line in lines:
        line_tax = f"{line['tax_rate_bps'] / 100:.2f}%" if line["tax_rate_bps"] else "-"
        # Description may wrap: use multi_cell for the row height.
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.multi_cell(col_w[0], 7, _latin1(line["description"]), border="LR")
        row_h = pdf.get_y() - y0
        pdf.set_xy(x0 + col_w[0], y0)
        pdf.cell(col_w[1], row_h, line["quantity"], border="LR", align="R")
        pdf.cell(col_w[2], row_h, _money(line["unit_cents"]), border="LR", align="R")
        pdf.cell(col_w[3], row_h, line_tax, border="LR", align="R")
        pdf.cell(col_w[4], row_h, _money(line["line_total_cents"]), border="LR", align="R")
        pdf.ln(row_h)
    # Bottom rule under the table.
    pdf.cell(sum(col_w), 0, "", border="T")
    pdf.ln(4)

    # Totals.
    pdf.set_font("Helvetica", "", 10)
    totals = [("Subtotal:", subtotal_cents), (f"{tax_label}:", tax_cents)]
    for label, cents in totals:
        pdf.cell(150, 7, label, align="R")
        pdf.cell(28, 7, _money(cents), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_RED)
    pdf.cell(150, 9, f"Total due ({currency}):", align="R")
    pdf.cell(28, 9, _money(total_cents), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*_CHARCOAL)
    pdf.ln(4)

    if memo:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Memo:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _latin1(memo))
        pdf.ln(2)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "How to pay:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _latin1(payment_instructions))

    return bytes(pdf.output())
