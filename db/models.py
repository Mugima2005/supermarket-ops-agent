"""
Database layer for the Supermarket Ops Agent.

Design notes
------------
- SQLite in WAL mode: readers don't block writers, and we use
  `BEGIN IMMEDIATE` for any write that must be atomic (stock decrement,
  bill finalize, khata updates) so two concurrent writers serialize
  instead of corrupting state.
- The oversell guard is enforced at the SQL layer: stock decrements use
  `UPDATE products SET qty = qty - ? WHERE sku_id = ? AND qty >= ?`
  and we check `cursor.rowcount`. This makes it impossible to bypass
  the guard from the tool layer, the prompt, or a bug in the model's
  reasoning.
- Idempotency: `idempotency_keys` records every finalize attempt's key
  before doing the work. A retried Telegram update (or a retried
  `finalize_bill` tool call) with the same key short-circuits and
  returns the original result instead of double-billing.
"""
from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("STORE_DB_PATH", str(Path(__file__).parent.parent / "store.sqlite3"))

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    sku_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    category        TEXT,
    unit            TEXT NOT NULL CHECK (unit IN ('kg','g','l','ml','packet','dozen','piece')),
    is_loose        INTEGER NOT NULL DEFAULT 0,
    hsn_code        TEXT,
    gst_rate        REAL NOT NULL DEFAULT 0,      -- e.g. 0, 5, 12, 18
    cost_price      REAL NOT NULL,
    sell_price      REAL NOT NULL,                -- pre-tax unit sell price
    mrp             REAL,
    qty             REAL NOT NULL DEFAULT 0,
    reorder_level   REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, unit)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku_id      INTEGER NOT NULL REFERENCES products(sku_id),
    delta       REAL NOT NULL,           -- positive = stock-in, negative = sale/adjustment
    reason      TEXT NOT NULL,           -- 'receive', 'sale', 'adjustment'
    ref_id      TEXT,                    -- e.g. bill id
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone       TEXT,
    balance     REAL NOT NULL DEFAULT 0,   -- positive = customer owes the shop
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS khata_transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    amount        REAL NOT NULL,            -- always positive
    txn_type      TEXT NOT NULL CHECK (txn_type IN ('credit','payment')),
    note          TEXT,
    bill_id       INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','finalized','cancelled')),
    customer_id     INTEGER REFERENCES customers(id),
    payment_mode    TEXT CHECK (payment_mode IN ('cash','upi','card','khata')),
    payment_ref     TEXT,
    subtotal        REAL NOT NULL DEFAULT 0,
    cgst_total      REAL NOT NULL DEFAULT 0,
    sgst_total      REAL NOT NULL DEFAULT 0,
    grand_total     REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finalized_at    TEXT
);

CREATE TABLE IF NOT EXISTS bill_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id       INTEGER NOT NULL REFERENCES bills(id),
    sku_id        INTEGER NOT NULL REFERENCES products(sku_id),
    qty           REAL NOT NULL,
    unit_price    REAL NOT NULL,     -- pre-tax, snapshotted at add-time
    gst_rate      REAL NOT NULL,
    hsn_code      TEXT,
    line_subtotal REAL NOT NULL,
    cgst_amt      REAL NOT NULL,
    sgst_amt      REAL NOT NULL,
    line_total    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    chat_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, key)
);

-- One row per (chat_id) tracking the currently-open draft bill, so the
-- agent doesn't need the model to remember a numeric bill_id across turns.
CREATE TABLE IF NOT EXISTS active_bills (
    chat_id     TEXT PRIMARY KEY,
    bill_id     INTEGER NOT NULL REFERENCES bills(id)
);

-- Idempotency ledger. `key` is caller-supplied (Telegram update_id for
-- inbound messages; a stable hash of chat_id+bill_id+action for tool
-- calls like finalize_bill). Storing the JSON result lets a retry
-- return the exact same answer without re-running side effects.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    result_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stock_movements_sku ON stock_movements(sku_id);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);
CREATE INDEX IF NOT EXISTS idx_khata_customer ON khata_transactions(customer_id);
"""


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@contextmanager
def get_conn(db_path: str = DB_PATH, immediate: bool = False):
    """
    Context manager returning a sqlite3 connection with row access by name.
    `immediate=True` opens a BEGIN IMMEDIATE transaction — use this for
    any write path that must not race with another writer (stock
    decrement, bill finalize, khata credit/payment).
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if immediate:
        conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_idempotency(conn: sqlite3.Connection, key: str):
    """Returns the stored JSON result if this key was already processed, else None."""
    if key is None:
        return None
    row = conn.execute("SELECT result_json FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
    return row["result_json"] if row else None


def store_idempotency(conn: sqlite3.Connection, key: str, result_json: str) -> None:
    if key is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys (key, result_json) VALUES (?, ?)",
        (key, result_json),
    )
