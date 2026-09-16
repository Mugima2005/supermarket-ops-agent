"""Generate a business-analysis PPTX deck: sales trend, top items, stock health, GST collected."""
from __future__ import annotations

import os
from datetime import date, timedelta

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.dml.color import RGBColor

from db.models import get_conn

OUTPUT_DIR = os.environ.get("DECK_OUTPUT_DIR", "decks")

NAVY = RGBColor(0x1F, 0x2A, 0x44)
ACCENT = RGBColor(0xE0, 0x7A, 0x1F)


def _title_slide(prs, title, subtitle):
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = title
    slide.placeholders[1].text = subtitle
    return slide


def _bullet_slide(prs, title, bullets):
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = title
    body = slide.placeholders[1].text_frame
    body.clear()
    for i, b in enumerate(bullets):
        p = body.paragraphs[0] if i == 0 else body.add_paragraph()
        p.text = b
        p.font.size = Pt(18)
    return slide


def _chart_slide(prs, title, categories, series_name, values, chart_type=XL_CHART_TYPE.COLUMN_CLUSTERED):
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    chart_data = CategoryChartData()
    chart_data.categories = categories
    chart_data.add_series(series_name, values)
    x, y, cx, cy = Inches(0.7), Inches(1.5), Inches(8.6), Inches(5.0)
    slide.shapes.add_chart(chart_type, x, y, cx, cy, chart_data)
    return slide


def generate_analysis_deck(start_date: str, end_date: str, title: str | None = None) -> dict:
    with get_conn() as conn:
        bills = conn.execute(
            "SELECT * FROM bills WHERE status='finalized' AND date(finalized_at) BETWEEN date(?) AND date(?) ORDER BY finalized_at",
            (start_date, end_date),
        ).fetchall()
        top_items = conn.execute(
            """SELECT p.name, SUM(bi.qty) as qty_sold, SUM(bi.line_total) as revenue
               FROM bill_items bi JOIN bills b ON b.id = bi.bill_id JOIN products p ON p.sku_id = bi.sku_id
               WHERE b.status='finalized' AND date(b.finalized_at) BETWEEN date(?) AND date(?)
               GROUP BY bi.sku_id ORDER BY revenue DESC LIMIT 8""",
            (start_date, end_date),
        ).fetchall()
        stock = conn.execute(
            "SELECT name, qty, reorder_level FROM products ORDER BY (qty - reorder_level) ASC LIMIT 8"
        ).fetchall()

    # Daily sales trend
    daily_totals: dict[str, float] = {}
    daily_tax: dict[str, float] = {}
    for b in bills:
        d = (b["finalized_at"] or "")[:10]
        daily_totals[d] = daily_totals.get(d, 0.0) + b["grand_total"]
        daily_tax[d] = daily_tax.get(d, 0.0) + b["cgst_total"] + b["sgst_total"]
    days = sorted(daily_totals.keys()) or [start_date]

    total_sales = sum(daily_totals.values())
    total_tax = sum(daily_tax.values())
    bill_count = len(bills)
    avg_bill = (total_sales / bill_count) if bill_count else 0.0

    by_mode: dict[str, float] = {}
    for b in bills:
        by_mode[b["payment_mode"] or "unknown"] = by_mode.get(b["payment_mode"] or "unknown", 0.0) + b["grand_total"]

    prs = Presentation()
    deck_title = title or "Sales Analysis"
    _title_slide(prs, deck_title, f"{start_date} to {end_date}")

    _bullet_slide(prs, "Headline numbers", [
        f"Total sales: Rs. {total_sales:,.2f}",
        f"GST collected: Rs. {total_tax:,.2f}",
        f"Bills finalized: {bill_count}",
        f"Average bill value: Rs. {avg_bill:,.2f}",
    ])

    if days:
        _chart_slide(prs, "Daily sales trend", days, "Sales (Rs.)", [round(daily_totals.get(d, 0.0), 2) for d in days], XL_CHART_TYPE.LINE_MARKERS)

    if top_items:
        _chart_slide(
            prs, "Top items by revenue",
            [r["name"] for r in top_items], "Revenue (Rs.)", [round(r["revenue"], 2) for r in top_items],
        )

    if by_mode:
        _chart_slide(
            prs, "Sales by payment mode",
            list(by_mode.keys()), "Rs.", [round(v, 2) for v in by_mode.values()], XL_CHART_TYPE.PIE,
        )

    if stock:
        _chart_slide(
            prs, "Stock health (closest to reorder level)",
            [r["name"] for r in stock], "Current qty", [r["qty"] for r in stock],
        )
        low = [r["name"] for r in stock if r["qty"] <= r["reorder_level"]]
        if low:
            _bullet_slide(prs, "Reorder now", low)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_name = f"analysis_{start_date}_{end_date}".replace(":", "-")
    path = os.path.join(OUTPUT_DIR, f"{safe_name}.pptx")
    prs.save(path)
    return {"ok": True, "file_path": path, "start_date": start_date, "end_date": end_date}
