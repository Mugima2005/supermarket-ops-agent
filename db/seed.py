"""Seed the store with realistic Indian kirana SKUs, HSN codes and GST slabs."""
from db.models import get_conn, init_db, DB_PATH

# (name, category, unit, is_loose, hsn_code, gst_rate, cost_price, sell_price, mrp, qty, reorder_level)
SEED_PRODUCTS = [
    ("Aashirvaad Atta 5kg", "staples", "packet", 0, "1101", 0,  215.0, 249.0, 259.0, 40, 10),
    ("Tata Salt 1kg",       "staples", "packet", 0, "2501", 5,  20.0,  25.0,  28.0,  60, 15),
    ("Amul Butter 100g",    "dairy",   "packet", 0, "0405", 12, 52.0,  58.0,  62.0,  30, 10),
    ("Fortune Sunflower Oil 1L", "staples", "packet", 0, "1512", 5, 135.0, 149.0, 159.0, 25, 8),
    ("Maggi 70g",           "fmcg",    "packet", 0, "1902", 12, 10.5,  13.0,  14.0,  120, 30),
    ("Parle-G",             "fmcg",    "packet", 0, "1905", 18, 8.0,   10.0,  10.0,  150, 40),
    ("Surf Excel 1kg",      "fmcg",    "packet", 0, "3402", 18, 105.0, 128.0, 135.0, 20, 6),
    ("Sugar", "loose", "kg", 1, "1701", 0, 38.0, 45.0, None, 80, 20),
    ("Rice (Sona Masoori)", "loose", "kg", 1, "1006", 0, 40.0, 48.0, None, 100, 25),
    ("Toor Dal", "loose", "kg", 1, "0713", 0, 110.0, 128.0, None, 50, 15),
]


def seed(db_path: str = DB_PATH) -> None:
    init_db(db_path)
    with get_conn(db_path) as conn:
        for (name, category, unit, is_loose, hsn, gst, cost, sell, mrp, qty, reorder) in SEED_PRODUCTS:
            conn.execute(
                """INSERT OR IGNORE INTO products
                   (name, category, unit, is_loose, hsn_code, gst_rate, cost_price, sell_price, mrp, qty, reorder_level)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (name, category, unit, is_loose, hsn, gst, cost, sell, mrp, qty, reorder),
            )
    print(f"Seeded {len(SEED_PRODUCTS)} products into {db_path}")


if __name__ == "__main__":
    seed()
