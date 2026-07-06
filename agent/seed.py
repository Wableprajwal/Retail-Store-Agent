import csv
import os
from .db import get_conn, init_db

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def _csv(name):
    path = os.path.join(DATA_DIR, name)
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def seed():
    init_db()
    conn = get_conn()
    with conn:
        # Skip if already seeded
        if conn.execute("SELECT COUNT(*) FROM skus").fetchone()[0] > 0: return
        
        # Products
        seen_products = {}
        for row in _csv("products.csv"):
            pid = row["product_id"]
            if pid not in seen_products:
                seen_products[pid] = (row["product_name"], row["category"])
                conn.execute(
                    "INSERT OR IGNORE INTO products VALUES (?, ?, ?)",
                    (pid, row["product_name"], row["category"])
                )
            conn.execute(
                "INSERT OR IGNORE INTO skus VALUES (?, ?, ?, ?, ?)",
                (row["sku"], pid, row["color"] or None, row["size"] or None, float(row["retail_price"]))
            )
        
        # Customers
        for row in _csv("customers.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?)",
                (row["customer_id"], row["name"], row["email"], row["joined_date"])
            )
        
        # Suppliers
        for row in _csv("suppliers.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO suppliers VALUES (?, ?)",
                (row["supplier_id"], row["supplier_name"])
            )
        
        # Supplier Catalog
        for row in _csv("supplier_catalog.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO supplier_catalog VALUES (?, ?, ?, ?)",
                (row["supplier_id"], row["product_id"], float(row["unit_cost"]), int(row["lead_time_days"]))
            )
        
        # Inventory
        for row in _csv("inventory.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, ?)",
                (row["sku"], int(row["on_hand_qty"]), int(row["reorder_point"]), int(row["reorder_qty"]))
            )
        
        # Orders
        for row in _csv("orders.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?)",
                (row["order_id"], row["order_date"],
                 row["customer_id"] or None,
                 float(row["order_discount_pct"]),
                 row["payment_method"])
            )

        # Order lines
        for row in _csv("order_lines.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO order_lines VALUES (?,?,?,?,?)",
                (row["order_id"], int(row["line_no"]), row["sku"],
                 int(row["quantity"]), float(row["unit_price"]))
            )

        # Returns
        for row in _csv("returns.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO returns VALUES (?,?,?,?,?,?,?)",
                (row["return_id"], row["return_date"], row["order_id"],
                 row["sku"], int(row["quantity"]),
                 row["condition"], float(row["refund_amount"]))
            )

        # Promotions
        for row in _csv("promotions.csv"):
            conn.execute(
                "INSERT OR IGNORE INTO promotions VALUES (?,?,?,?,?,?,?,?)",
                (row["promo_id"], row["description"], row["type"],
                 float(row["value"]), row["scope_type"], row["scope_ref"],
                 row["start_date"], row["end_date"])
            )
        