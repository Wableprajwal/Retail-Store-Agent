import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "store.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL   -- 'apparel' or 'goods'
);

-- A SKU is one sellable variant (e.g. TEE-BLU-M)
CREATE TABLE IF NOT EXISTS skus (
    sku          TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL REFERENCES products(product_id),
    color        TEXT,           -- NULL for goods
    size         TEXT,           -- NULL for goods
    retail_price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT,
    joined_date TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id   TEXT PRIMARY KEY,
    supplier_name TEXT NOT NULL
);

-- Who sells us what product at what cost
CREATE TABLE IF NOT EXISTS supplier_catalog (
    supplier_id    TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id     TEXT NOT NULL REFERENCES products(product_id),
    unit_cost      REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    PRIMARY KEY (supplier_id, product_id)
);

-- Current stock levels per SKU
CREATE TABLE IF NOT EXISTS inventory (
    sku          TEXT PRIMARY KEY REFERENCES skus(sku),
    on_hand_qty  INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 0,
    reorder_qty  INTEGER NOT NULL DEFAULT 0
);

-- Sales headers
CREATE TABLE IF NOT EXISTS orders (
    order_id           TEXT PRIMARY KEY,
    order_date         TEXT NOT NULL,
    customer_id        TEXT REFERENCES customers(customer_id), -- NULL = walk-in
    order_discount_pct REAL NOT NULL DEFAULT 0,
    payment_method     TEXT NOT NULL
);

-- Sales lines
CREATE TABLE IF NOT EXISTS order_lines (
    order_id   TEXT NOT NULL REFERENCES orders(order_id),
    line_no    INTEGER NOT NULL,
    sku        TEXT NOT NULL REFERENCES skus(sku),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL,  -- pre-order-discount, post-promo price
    PRIMARY KEY (order_id, line_no)
);

-- Returns
CREATE TABLE IF NOT EXISTS returns (
    return_id     TEXT PRIMARY KEY,
    return_date   TEXT NOT NULL,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    sku           TEXT NOT NULL REFERENCES skus(sku),
    quantity      INTEGER NOT NULL,
    condition     TEXT NOT NULL,  -- 'good' or 'damaged'
    refund_amount REAL NOT NULL
);

-- Promotions
CREATE TABLE IF NOT EXISTS promotions (
    promo_id    TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    type        TEXT NOT NULL,       -- 'percent_off'
    value       REAL NOT NULL,       -- e.g. 20 means 20%
    scope_type  TEXT NOT NULL,       -- 'product' or 'category'
    scope_ref   TEXT NOT NULL,       -- product_id or category
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL
);

-- Purchase orders (restocking)
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id       TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id  TEXT NOT NULL REFERENCES products(product_id),
    sku         TEXT REFERENCES skus(sku),  -- NULL if product-level PO
    order_date  TEXT NOT NULL,
    qty_ordered INTEGER NOT NULL,
    qty_received INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open'  -- 'open' or 'closed'
);
"""

def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
