"""Generate a GST-correct PDF invoice for a finalized bill."""
from __future__ import annotations

import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from db.models import get_conn
from agent.tools import get_preference

OUTPUT_DIR = os.environ.get("INVOICE_OUTPUT_DIR", "invoices")


def generate_invoice_pdf(bill_id: int, chat_id: str | None = None) -> dict:
    with get_conn() as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill:
            return {"ok": False, "error": "Bill not found."}
        if bill["status"] != "finalized":
            return {"ok": False, "error": "Only finalized bills can be invoiced. Finalize the bill first."}

        items = conn.execute(
            """SELECT bi.*, p.name, p.unit FROM bill_items bi
               JOIN products p ON p.sku_id = bi.sku_id WHERE bi.bill_id = ?""",
            (bill_id,),
        ).fetchall()
        customer = None
        if bill["customer_id"]:
            customer = conn.execute("SELECT name FROM customers WHERE id = ?", (bill["customer_id"],)).fetchone()

    shop_name = "Kirana Store"
    gstin = None
    if chat_id:
        shop_name = get_preference(chat_id, "shop_name").get("value") or shop_name
        gstin = get_preference(chat_id, "gstin").get("value")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"invoice_{bill_id}.pdf")

    doc = SimpleDocTemplate(path, pagesize=A5, topMargin=10 * mm, bottomMargin=10 * mm,
                             leftMargin=10 * mm, rightMargin=10 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("shop", parent=styles["Heading2"], alignment=1)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8)

    elements = [Paragraph(shop_name, title_style)]
    if gstin:
        elements.append(Paragraph(f"GSTIN: {gstin}", ParagraphStyle("gstin", parent=styles["Normal"], alignment=1, fontSize=8)))
    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(
        f"Invoice #{bill_id} &nbsp;&nbsp;|&nbsp;&nbsp; {bill['finalized_at']}", small))
    if customer:
        elements.append(Paragraph(f"Customer: {customer['name']}", small))
    elements.append(Paragraph(f"Payment: {bill['payment_mode'].upper()}" + (f" ({bill['payment_ref']})" if bill["payment_ref"] else ""), small))
    elements.append(Spacer(1, 4 * mm))

    table_data = [["Item", "HSN", "Qty", "Rate", "GST%", "CGST", "SGST", "Total"]]
    for it in items:
        table_data.append([
            it["name"], it["hsn_code"] or "-", f"{it['qty']:g} {it['unit']}",
            f"{it['unit_price']:.2f}", f"{it['gst_rate']:g}%",
            f"{it['cgst_amt']:.2f}", f"{it['sgst_amt']:.2f}", f"{it['line_total']:.2f}",
        ])
    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d2d2d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 4 * mm))

    totals_data = [
        ["Subtotal", f"Rs. {bill['subtotal']:.2f}"],
        ["CGST", f"Rs. {bill['cgst_total']:.2f}"],
        ["SGST", f"Rs. {bill['sgst_total']:.2f}"],
        ["Grand Total", f"Rs. {bill['grand_total']:.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=[80 * mm, 40 * mm])
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("LINEABOVE", (0, 3), (-1, 3), 0.8, colors.black),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 6 * mm))
    elements.append(Paragraph("Thank you for shopping with us!", small))

    doc.build(elements)
    return {"ok": True, "bill_id": bill_id, "file_path": path}
