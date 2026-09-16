"""
Tool implementations for the Supermarket Ops Agent.

Every tool here is a plain Python function with a JSON-serializable
return value (a dict). `tool_schemas.py` describes these to the model;
`control_loop.py` dispatches model tool_use blocks to the functions
below by name. Business rules (oversell guard, GST math, idempotency,
khata sanity) live HERE, not in the system prompt — the model cannot
talk its way around them because the SQL itself enforces them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, date, timedelta

from db.models import get_conn, check_idempotency, store_idempotency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def _gst_split(subtotal: float, gst_rate: float) -> tuple[float, float]:
    """Intra-state split: half CGST, half SGST."""
    tax = subtotal * (gst_rate / 100.0)
    half = _round2(tax / 2.0)
    return half, half


def _find_product(conn, name_or_id) -> dict | None:
    """Resolve a product by sku_id (int-like) or fuzzy name match."""
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
        row = conn.execute("SELECT * FROM products WHERE sku_id = ?", (int(name_or_id),)).fetchone()
        return dict(row) if row else None
    row = conn.execute(
        "SELECT * FROM products WHERE name = ? COLLATE NOCASE", (name_or_id,)
    ).fetchone()
    if row:
        return dict(row)
    rows = conn.execute(
        "SELECT * FROM products WHERE name LIKE ? COLLATE NOCASE", (f"%{name_or_id}%",)
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def _search_candidates(conn, query: str) -> list[dict]:
    rows = conn.execute(
        "SELECT sku_id, name, unit, sell_price, qty FROM products WHERE name LIKE ? COLLATE NOCASE",
        (f"%{query}%",),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_or_create_customer(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM customers WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO customers (name) VALUES (?)", (name,))
    return cur.lastrowid


def _recalc_bill_totals(conn, bill_id: int) -> dict:
    rows = conn.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,)).fetchall()
    subtotal = _round2(sum(r["line_subtotal"] for r in rows))
    cgst = _round2(sum(r["cgst_amt"] for r in rows))
    sgst = _round2(sum(r["sgst_amt"] for r in rows))
    grand = _round2(subtotal + cgst + sgst)
    conn.execute(
        "UPDATE bills SET subtotal=?, cgst_total=?, sgst_total=?, grand_total=? WHERE id=?",
        (subtotal, cgst, sgst, grand, bill_id),
    )
    return {"subtotal": subtotal, "cgst_total": cgst, "sgst_total": sgst, "grand_total": grand}


def _bill_snapshot(conn, bill_id: int) -> dict:
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    items = conn.execute(
        """SELECT bi.*, p.name FROM bill_items bi JOIN products p ON p.sku_id = bi.sku_id
           WHERE bi.bill_id = ?""",
        (bill_id,),
    ).fetchall()
    return {
        "bill_id": bill_id,
        "status": bill["status"],
        "customer_id": bill["customer_id"],
        "payment_mode": bill["payment_mode"],
        "items": [dict(i) for i in items],
        "subtotal": bill["subtotal"],
        "cgst_total": bill["cgst_total"],
        "sgst_total": bill["sgst_total"],
        "grand_total": bill["grand_total"],
    }


# ---------------------------------------------------------------------------
# Inventory skill
# ---------------------------------------------------------------------------

def add_product(name, unit, gst_rate, cost_price, sell_price, mrp=None,
                 category=None, hsn_code=None, initial_qty=0, reorder_level=0,
                 is_loose=False):
    with get_conn(immediate=True) as conn:
        existing = conn.execute(
            "SELECT sku_id FROM products WHERE name = ? COLLATE NOCASE AND unit = ?", (name, unit)
        ).fetchone()
        if existing:
            return {"ok": False, "error": f"Product '{name}' ({unit}) already exists as SKU {existing['sku_id']}."}
        cur = conn.execute(
            """INSERT INTO products (name, category, unit, is_loose, hsn_code, gst_rate,
                                      cost_price, sell_price, mrp, qty, reorder_level)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name, category, unit, int(is_loose), hsn_code, gst_rate, cost_price, sell_price,
             mrp, initial_qty, reorder_level),
        )
        sku_id = cur.lastrowid
        if initial_qty:
            conn.execute(
                "INSERT INTO stock_movements (sku_id, delta, reason) VALUES (?,?,?)",
                (sku_id, initial_qty, "receive"),
            )
        return {"ok": True, "sku_id": sku_id, "name": name, "qty": initial_qty}


def receive_stock(product, qty, cost_price=None, mrp=None, sell_price=None):
    if qty <= 0:
        return {"ok": False, "error": "Quantity received must be positive."}
    with get_conn(immediate=True) as conn:
        p = _find_product(conn, product)
        if not p:
            candidates = _search_candidates(conn, str(product))
            return {"ok": False, "error": "Product not found.", "candidates": candidates}
        conn.execute("UPDATE products SET qty = qty + ? WHERE sku_id = ?", (qty, p["sku_id"]))
        if cost_price is not None:
            conn.execute("UPDATE products SET cost_price = ? WHERE sku_id = ?", (cost_price, p["sku_id"]))
        if mrp is not None:
            conn.execute("UPDATE products SET mrp = ? WHERE sku_id = ?", (mrp, p["sku_id"]))
        if sell_price is not None:
            conn.execute("UPDATE products SET sell_price = ? WHERE sku_id = ?", (sell_price, p["sku_id"]))
        conn.execute(
            "INSERT INTO stock_movements (sku_id, delta, reason) VALUES (?,?,?)",
            (p["sku_id"], qty, "receive"),
        )
        new_row = conn.execute("SELECT qty FROM products WHERE sku_id = ?", (p["sku_id"],)).fetchone()
        return {"ok": True, "sku_id": p["sku_id"], "name": p["name"], "new_qty": new_row["qty"]}


def search_products(query):
    with get_conn() as conn:
        return {"ok": True, "candidates": _search_candidates(conn, query)}


def get_stock(product):
    with get_conn() as conn:
        p = _find_product(conn, product)
        if not p:
            candidates = _search_candidates(conn, str(product))
            if not candidates:
                return {"ok": False, "error": f"No product matching '{product}'."}
            return {"ok": False, "error": "Ambiguous or partial match.", "candidates": candidates}
        return {"ok": True, "name": p["name"], "unit": p["unit"], "qty": p["qty"],
                "reorder_level": p["reorder_level"], "sell_price": p["sell_price"]}


def list_low_stock():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, unit, qty, reorder_level FROM products WHERE qty <= reorder_level ORDER BY qty ASC"
        ).fetchall()
        return {"ok": True, "low_stock": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Billing skill (multi-turn draft -> finalize)
# ---------------------------------------------------------------------------

def start_bill(chat_id, customer_name=None):
    with get_conn(immediate=True) as conn:
        existing = conn.execute("SELECT bill_id FROM active_bills WHERE chat_id = ?", (chat_id,)).fetchone()
        if existing:
            b = conn.execute("SELECT status FROM bills WHERE id = ?", (existing["bill_id"],)).fetchone()
            if b and b["status"] == "draft":
                return {"ok": True, "bill_id": existing["bill_id"], "note": "Resuming existing draft bill."}
        customer_id = _get_or_create_customer(conn, customer_name) if customer_name else None
        cur = conn.execute(
            "INSERT INTO bills (chat_id, status, customer_id) VALUES (?, 'draft', ?)",
            (chat_id, customer_id),
        )
        bill_id = cur.lastrowid
        conn.execute(
            "INSERT INTO active_bills (chat_id, bill_id) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET bill_id = excluded.bill_id",
            (chat_id, bill_id),
        )
        return {"ok": True, "bill_id": bill_id}


def add_bill_item(bill_id, product, qty):
    if qty <= 0:
        return {"ok": False, "error": "Quantity must be positive."}
    with get_conn(immediate=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill or bill["status"] != "draft":
            return {"ok": False, "error": "No open draft bill with that id."}
        p = _find_product(conn, product)
        if not p:
            candidates = _search_candidates(conn, str(product))
            return {"ok": False, "error": f"Product '{product}' not found or ambiguous.", "candidates": candidates}
        if p["qty"] < qty:
            return {"ok": False, "error": f"Only {p['qty']} {p['unit']} of {p['name']} in stock — cannot add {qty}.",
                     "available": p["qty"]}
        subtotal = _round2(p["sell_price"] * qty)
        cgst, sgst = _gst_split(subtotal, p["gst_rate"])
        line_total = _round2(subtotal + cgst + sgst)
        conn.execute(
            """INSERT INTO bill_items (bill_id, sku_id, qty, unit_price, gst_rate, hsn_code,
                                        line_subtotal, cgst_amt, sgst_amt, line_total)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (bill_id, p["sku_id"], qty, p["sell_price"], p["gst_rate"], p["hsn_code"],
             subtotal, cgst, sgst, line_total),
        )
        totals = _recalc_bill_totals(conn, bill_id)
        return {"ok": True, **_bill_snapshot(conn, bill_id)}


def remove_bill_item(bill_id, product):
    with get_conn(immediate=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill or bill["status"] != "draft":
            return {"ok": False, "error": "No open draft bill with that id."}
        p = _find_product(conn, product)
        if not p:
            return {"ok": False, "error": f"Product '{product}' not found."}
        row = conn.execute(
            "SELECT id FROM bill_items WHERE bill_id = ? AND sku_id = ?", (bill_id, p["sku_id"])
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"{p['name']} is not on this bill."}
        conn.execute("DELETE FROM bill_items WHERE id = ?", (row["id"],))
        _recalc_bill_totals(conn, bill_id)
        return {"ok": True, **_bill_snapshot(conn, bill_id)}


def update_bill_item_qty(bill_id, product, qty):
    with get_conn(immediate=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill or bill["status"] != "draft":
            return {"ok": False, "error": "No open draft bill with that id."}
        p = _find_product(conn, product)
        if not p:
            return {"ok": False, "error": f"Product '{product}' not found."}
        if qty <= 0:
            conn.execute("DELETE FROM bill_items WHERE bill_id = ? AND sku_id = ?", (bill_id, p["sku_id"]))
            _recalc_bill_totals(conn, bill_id)
            return {"ok": True, **_bill_snapshot(conn, bill_id)}
        if p["qty"] < qty:
            return {"ok": False, "error": f"Only {p['qty']} {p['unit']} of {p['name']} in stock.", "available": p["qty"]}
        subtotal = _round2(p["sell_price"] * qty)
        cgst, sgst = _gst_split(subtotal, p["gst_rate"])
        line_total = _round2(subtotal + cgst + sgst)
        row = conn.execute(
            "SELECT id FROM bill_items WHERE bill_id = ? AND sku_id = ?", (bill_id, p["sku_id"])
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE bill_items SET qty=?, line_subtotal=?, cgst_amt=?, sgst_amt=?, line_total=?
                   WHERE id=?""",
                (qty, subtotal, cgst, sgst, line_total, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO bill_items (bill_id, sku_id, qty, unit_price, gst_rate, hsn_code,
                                            line_subtotal, cgst_amt, sgst_amt, line_total)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (bill_id, p["sku_id"], qty, p["sell_price"], p["gst_rate"], p["hsn_code"],
                 subtotal, cgst, sgst, line_total),
            )
        _recalc_bill_totals(conn, bill_id)
        return {"ok": True, **_bill_snapshot(conn, bill_id)}


def get_bill_draft(bill_id):
    with get_conn() as conn:
        bill = conn.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill:
            return {"ok": False, "error": "Bill not found."}
        return {"ok": True, **_bill_snapshot(conn, bill_id)}


def cancel_bill(bill_id):
    with get_conn(immediate=True) as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill or bill["status"] != "draft":
            return {"ok": False, "error": "No open draft bill with that id."}
        conn.execute("UPDATE bills SET status = 'cancelled' WHERE id = ?", (bill_id,))
        conn.execute("DELETE FROM active_bills WHERE bill_id = ?", (bill_id,))
        return {"ok": True, "bill_id": bill_id, "status": "cancelled"}


def finalize_bill(bill_id, payment_mode, payment_ref=None, idempotency_key=None,
                   allow_below_cost=False):
    """
    Atomically: guard oversell per line, decrement stock, mark bill
    finalized, and (if payment_mode == 'khata') push the total onto the
    customer's credit balance. Wrapped in one BEGIN IMMEDIATE transaction
    so a concurrent sale on the same SKUs can't corrupt stock, and keyed
    by idempotency_key so Telegram's at-least-once delivery can't
    double-bill.
    """
    key = idempotency_key or f"finalize:{bill_id}"
    with get_conn(immediate=True) as conn:
        cached = check_idempotency(conn, key)
        if cached:
            return json.loads(cached)

        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if not bill:
            result = {"ok": False, "error": "Bill not found."}
            store_idempotency(conn, key, json.dumps(result))
            return result
        if bill["status"] == "finalized":
            result = {"ok": True, **_bill_snapshot(conn, bill_id), "note": "Already finalized."}
            store_idempotency(conn, key, json.dumps(result))
            return result
        if bill["status"] != "draft":
            result = {"ok": False, "error": f"Bill is {bill['status']}, cannot finalize."}
            store_idempotency(conn, key, json.dumps(result))
            return result

        items = conn.execute("SELECT * FROM bill_items WHERE bill_id = ?", (bill_id,)).fetchall()
        if not items:
            result = {"ok": False, "error": "Cannot finalize an empty bill."}
            store_idempotency(conn, key, json.dumps(result))
            return result

        # Guardrail: refuse to sell below cost unless explicitly overridden.
        if not allow_below_cost:
            for it in items:
                prod = conn.execute("SELECT name, cost_price FROM products WHERE sku_id = ?", (it["sku_id"],)).fetchone()
                if it["unit_price"] < prod["cost_price"]:
                    result = {"ok": False,
                              "error": f"{prod['name']} is priced below cost (₹{it['unit_price']} < ₹{prod['cost_price']}). "
                                       f"Confirm explicitly to override (allow_below_cost=true)."}
                    store_idempotency(conn, key, json.dumps(result))
                    return result

        # Atomic oversell-guarded stock decrement, one row at a time.
        for it in items:
            cur = conn.execute(
                "UPDATE products SET qty = qty - ? WHERE sku_id = ? AND qty >= ?",
                (it["qty"], it["sku_id"], it["qty"]),
            )
            if cur.rowcount == 0:
                # Someone else sold it out from under this draft between add and finalize.
                prod = conn.execute("SELECT name, qty FROM products WHERE sku_id = ?", (it["sku_id"],)).fetchone()
                result = {"ok": False,
                          "error": f"Oversell blocked: only {prod['qty']} of {prod['name']} left "
                                   f"(bill wants {it['qty']}). Adjust the bill and retry."}
                store_idempotency(conn, key, json.dumps(result))
                return result
            conn.execute(
                "INSERT INTO stock_movements (sku_id, delta, reason, ref_id) VALUES (?,?,?,?)",
                (it["sku_id"], -it["qty"], "sale", str(bill_id)),
            )

        if payment_mode == "khata":
            if not bill["customer_id"]:
                result = {"ok": False, "error": "Khata payment mode requires a named customer on the bill."}
                store_idempotency(conn, key, json.dumps(result))
                return result
            conn.execute(
                "UPDATE customers SET balance = balance + ? WHERE id = ?",
                (bill["grand_total"], bill["customer_id"]),
            )
            conn.execute(
                "INSERT INTO khata_transactions (customer_id, amount, txn_type, note, bill_id) VALUES (?,?,?,?,?)",
                (bill["customer_id"], bill["grand_total"], "credit", "Bill on credit", bill_id),
            )

        conn.execute(
            "UPDATE bills SET status='finalized', payment_mode=?, payment_ref=?, finalized_at=datetime('now') WHERE id=?",
            (payment_mode, payment_ref, bill_id),
        )
        conn.execute("DELETE FROM active_bills WHERE bill_id = ?", (bill_id,))

        result = {"ok": True, **_bill_snapshot(conn, bill_id)}
        store_idempotency(conn, key, json.dumps(result))
        return result


# ---------------------------------------------------------------------------
# Khata (credit ledger) skill
# ---------------------------------------------------------------------------

def add_khata_charge(customer_name, amount, note=None):
    if amount <= 0:
        return {"ok": False, "error": "Amount must be positive."}
    with get_conn(immediate=True) as conn:
        cust_id = _get_or_create_customer(conn, customer_name)
        conn.execute("UPDATE customers SET balance = balance + ? WHERE id = ?", (amount, cust_id))
        conn.execute(
            "INSERT INTO khata_transactions (customer_id, amount, txn_type, note) VALUES (?,?,?,?)",
            (cust_id, amount, "credit", note),
        )
        bal = conn.execute("SELECT balance FROM customers WHERE id = ?", (cust_id,)).fetchone()["balance"]
        return {"ok": True, "customer": customer_name, "new_balance": bal}


def record_khata_payment(customer_name, amount, note=None):
    if amount <= 0:
        return {"ok": False, "error": "Amount must be positive."}
    with get_conn(immediate=True) as conn:
        row = conn.execute("SELECT id, balance FROM customers WHERE name = ? COLLATE NOCASE", (customer_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"No khata account exists for '{customer_name}'."}
        if row["balance"] <= 0:
            return {"ok": False, "error": f"{customer_name} has no outstanding balance to settle."}
        conn.execute("UPDATE customers SET balance = balance - ? WHERE id = ?", (amount, row["id"]))
        conn.execute(
            "INSERT INTO khata_transactions (customer_id, amount, txn_type, note) VALUES (?,?,?,?)",
            (row["id"], amount, "payment", note),
        )
        bal = conn.execute("SELECT balance FROM customers WHERE id = ?", (row["id"],)).fetchone()["balance"]
        return {"ok": True, "customer": customer_name, "new_balance": bal}


def get_khata_balance(customer_name):
    with get_conn() as conn:
        row = conn.execute("SELECT balance FROM customers WHERE name = ? COLLATE NOCASE", (customer_name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"No khata account exists for '{customer_name}'."}
        return {"ok": True, "customer": customer_name, "balance": row["balance"]}


def list_khata_customers():
    with get_conn() as conn:
        rows = conn.execute("SELECT name, balance FROM customers WHERE balance > 0 ORDER BY balance DESC").fetchall()
        return {"ok": True, "customers": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Analytics skill
# ---------------------------------------------------------------------------

def get_daily_close(for_date=None):
    d = for_date or date.today().isoformat()
    with get_conn() as conn:
        bills = conn.execute(
            "SELECT * FROM bills WHERE status='finalized' AND date(finalized_at) = date(?)", (d,)
        ).fetchall()
        by_mode = {}
        total = 0.0
        tax = 0.0
        for b in bills:
            by_mode[b["payment_mode"]] = by_mode.get(b["payment_mode"], 0.0) + b["grand_total"]
            total += b["grand_total"]
            tax += b["cgst_total"] + b["sgst_total"]
        top_items = conn.execute(
            """SELECT p.name, SUM(bi.qty) as qty_sold, SUM(bi.line_total) as revenue
               FROM bill_items bi
               JOIN bills b ON b.id = bi.bill_id
               JOIN products p ON p.sku_id = bi.sku_id
               WHERE b.status='finalized' AND date(b.finalized_at) = date(?)
               GROUP BY bi.sku_id ORDER BY revenue DESC LIMIT 5""",
            (d,),
        ).fetchall()
        return {
            "ok": True, "date": d, "bill_count": len(bills), "total_sales": _round2(total),
            "total_tax_collected": _round2(tax), "by_payment_mode": {k: _round2(v) for k, v in by_mode.items()},
            "top_items": [dict(r) for r in top_items],
        }


def get_sales_summary(start_date, end_date):
    with get_conn() as conn:
        bills = conn.execute(
            "SELECT * FROM bills WHERE status='finalized' AND date(finalized_at) BETWEEN date(?) AND date(?)",
            (start_date, end_date),
        ).fetchall()
        total = _round2(sum(b["grand_total"] for b in bills))
        tax = _round2(sum(b["cgst_total"] + b["sgst_total"] for b in bills))
        return {"ok": True, "start_date": start_date, "end_date": end_date,
                "bill_count": len(bills), "total_sales": total, "total_tax_collected": tax}


def get_top_items(start_date, end_date, limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.name, p.category, SUM(bi.qty) as qty_sold, SUM(bi.line_total) as revenue
               FROM bill_items bi
               JOIN bills b ON b.id = bi.bill_id
               JOIN products p ON p.sku_id = bi.sku_id
               WHERE b.status='finalized' AND date(b.finalized_at) BETWEEN date(?) AND date(?)
               GROUP BY bi.sku_id ORDER BY revenue DESC LIMIT ?""",
            (start_date, end_date, limit),
        ).fetchall()
        return {"ok": True, "items": [dict(r) for r in rows]}


def get_stock_health():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, category, qty, reorder_level, unit FROM products ORDER BY (qty - reorder_level) ASC"
        ).fetchall()
        return {"ok": True, "products": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Preferences skill (cross-session memory, keyed by Telegram chat_id)
# ---------------------------------------------------------------------------

def set_preference(chat_id, key, value):
    with get_conn(immediate=True) as conn:
        conn.execute(
            """INSERT INTO preferences (chat_id, key, value) VALUES (?, ?, ?)
               ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
            (chat_id, key, str(value)),
        )
        return {"ok": True, "key": key, "value": value}


def get_preference(chat_id, key):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM preferences WHERE chat_id = ? AND key = ?", (chat_id, key)).fetchone()
        return {"ok": True, "key": key, "value": row["value"] if row else None}


def get_all_preferences(chat_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM preferences WHERE chat_id = ?", (chat_id,)).fetchall()
        return {"ok": True, "preferences": {r["key"]: r["value"] for r in rows}}


# Registry the control loop dispatches against.
TOOL_REGISTRY = {
    "add_product": add_product,
    "receive_stock": receive_stock,
    "search_products": search_products,
    "get_stock": get_stock,
    "list_low_stock": list_low_stock,
    "start_bill": start_bill,
    "add_bill_item": add_bill_item,
    "remove_bill_item": remove_bill_item,
    "update_bill_item_qty": update_bill_item_qty,
    "get_bill_draft": get_bill_draft,
    "cancel_bill": cancel_bill,
    "finalize_bill": finalize_bill,
    "add_khata_charge": add_khata_charge,
    "record_khata_payment": record_khata_payment,
    "get_khata_balance": get_khata_balance,
    "list_khata_customers": list_khata_customers,
    "get_daily_close": get_daily_close,
    "get_sales_summary": get_sales_summary,
    "get_top_items": get_top_items,
    "get_stock_health": get_stock_health,
    "set_preference": set_preference,
    "get_preference": get_preference,
    "get_all_preferences": get_all_preferences,
    # documents.* and generate_invoice_pdf/generate_analysis_deck are
    # registered separately in bot.py because they need a chat_id-scoped
    # output directory and are imported from documents/.
}
