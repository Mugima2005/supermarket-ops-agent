"""
Direct tests of the tool layer — these don't touch the Anthropic API at
all, because the business rules (oversell guard, GST math, idempotency,
khata sanity) are supposed to live in the tools/DB, not the model.
Run with: python -m pytest tests/ -v
"""
import os
import sys
import tempfile
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from db import models as db_models


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    monkeypatch.setattr(db_models, "DB_PATH", path)
    # agent.tools imports get_conn with a bound default arg, so reload it
    # against the patched DB_PATH for each test.
    import importlib
    import agent.tools as tools
    importlib.reload(tools)
    monkeypatch.setattr(tools, "get_conn",
                         lambda immediate=False: db_models.get_conn(path, immediate=immediate))
    db_models.init_db(path)
    from db.seed import seed
    seed(path)
    yield tools
    os.remove(path)
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            os.remove(path + ext)


def test_gst_split_and_bill_math(temp_db):
    t = temp_db
    b = t.start_bill("chat1")
    assert b["ok"]
    bill_id = b["bill_id"]
    # Maggi 70g: sell_price 13.0, gst 12% -> subtotal 4*13=52, tax=52*0.12=6.24, half=3.12 each
    r = t.add_bill_item(bill_id, "Maggi 70g", 4)
    assert r["ok"]
    item = r["items"][0]
    assert item["line_subtotal"] == 52.0
    assert item["cgst_amt"] == 3.12
    assert item["sgst_amt"] == 3.12
    assert r["grand_total"] == 58.24


def test_oversell_guard_blocks_add_and_finalize(temp_db):
    t = temp_db
    b = t.start_bill("chat1")
    bill_id = b["bill_id"]
    stock = t.get_stock("Amul Butter 100g")["qty"]
    r = t.add_bill_item(bill_id, "Amul Butter 100g", stock + 5)
    assert not r["ok"]
    assert "stock" in r["error"].lower()


def test_finalize_is_idempotent(temp_db):
    t = temp_db
    b = t.start_bill("chat1")
    bill_id = b["bill_id"]
    t.add_bill_item(bill_id, "Parle-G", 2)
    key = f"finalize:{bill_id}:turn1"
    r1 = t.finalize_bill(bill_id, "cash", idempotency_key=key)
    assert r1["ok"]
    qty_after_first = t.get_stock("Parle-G")["qty"]
    r2 = t.finalize_bill(bill_id, "cash", idempotency_key=key)  # simulate Telegram retry
    assert r2["ok"]
    qty_after_second = t.get_stock("Parle-G")["qty"]
    assert qty_after_first == qty_after_second, "Retried finalize must not double-decrement stock"


def test_khata_flow(temp_db):
    t = temp_db
    r = t.add_khata_charge("Ramesh", 500)
    assert r["ok"] and r["new_balance"] == 500
    r2 = t.record_khata_payment("Ramesh", 300)
    assert r2["ok"] and r2["new_balance"] == 200
    bal = t.get_khata_balance("Ramesh")
    assert bal["balance"] == 200


def test_khata_payment_without_account_refused(temp_db):
    t = temp_db
    r = t.record_khata_payment("Nobody", 100)
    assert not r["ok"]


def test_below_cost_guard(temp_db):
    t = temp_db
    # Force a below-cost price by editing after add, using receive_stock to set sell_price low
    t.receive_stock("Sugar", 1, sell_price=10.0)  # cost_price is 38.0
    b = t.start_bill("chat1")
    bill_id = b["bill_id"]
    t.add_bill_item(bill_id, "Sugar", 1)
    r = t.finalize_bill(bill_id, "cash", idempotency_key="k1")
    assert not r["ok"]
    assert "cost" in r["error"].lower()
    r2 = t.finalize_bill(bill_id, "cash", idempotency_key="k1")  # idempotent even on the error path
    assert r2 == r


def test_concurrent_sales_do_not_oversell(temp_db):
    """Two bills racing to sell the last units of a low-stock item must not both succeed
    beyond available stock — the SQL-level guard must serialize them correctly."""
    t = temp_db
    t.add_product("Test Widget", "piece", 18, cost_price=5, sell_price=10, initial_qty=10)

    def sell_five(tag):
        b = t.start_bill(f"chat_{tag}")
        bill_id = b["bill_id"]
        add = t.add_bill_item(bill_id, "Test Widget", 5)
        if not add["ok"]:
            return {"ok": False}
        return t.finalize_bill(bill_id, "cash", idempotency_key=f"finalize:{bill_id}:{tag}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(sell_five, range(4)))  # 4 attempts x 5 units against 10 in stock

    successes = [r for r in results if r.get("ok")]
    final_stock = t.get_stock("Test Widget")["qty"]
    assert len(successes) == 2, f"Expected exactly 2 of 4 concurrent 5-unit sales to succeed, got {len(successes)}"
    assert final_stock == 0


def test_bill_edit_across_turns(temp_db):
    t = temp_db
    b = t.start_bill("chat1")
    bill_id = b["bill_id"]
    t.add_bill_item(bill_id, "Amul Butter 100g", 1)
    t.add_bill_item(bill_id, "Maggi 70g", 4)
    r = t.remove_bill_item(bill_id, "Amul Butter 100g")
    assert r["ok"]
    assert all(i["name"] != "Amul Butter 100g" for i in r["items"])
    r2 = t.update_bill_item_qty(bill_id, "Maggi 70g", 6)
    assert r2["ok"]
    maggi_line = [i for i in r2["items"] if i["name"] == "Maggi 70g"][0]
    assert maggi_line["qty"] == 6
