"""
Declarative data invariants. Each is (name, description, sql); the SQL returns the
violating rows, so an empty result means the invariant holds. Checked after every
guarded write and by the eval runner after every run. This is detection; the
enforcement lives in the DB (trigger) and in the tools' preconditions.
"""

INVARIANTS = [
    (
        "returns_not_exceed_sold",
        "Total returned quantity per order/sku must not exceed quantity sold",
        """
        SELECT r.order_id, r.sku, SUM(r.quantity) AS returned,
               (SELECT COALESCE(SUM(ol.quantity), 0) FROM order_lines ol
                WHERE ol.order_id = r.order_id AND ol.sku = r.sku) AS sold
        FROM returns r
        GROUP BY r.order_id, r.sku
        HAVING returned > sold
        """,
    ),
    (
        "refund_not_exceed_paid",
        "Total refunds per order/sku must not exceed what the customer paid",
        """
        SELECT r.order_id, r.sku, ROUND(SUM(r.refund_amount), 2) AS refunded,
               (SELECT ROUND(COALESCE(SUM(ol.quantity * ol.unit_price * (1 - o.order_discount_pct / 100.0)), 0), 2)
                FROM order_lines ol JOIN orders o ON o.order_id = ol.order_id
                WHERE ol.order_id = r.order_id AND ol.sku = r.sku) AS paid
        FROM returns r
        GROUP BY r.order_id, r.sku
        HAVING refunded > paid + 0.01
        """,
    ),
    (
        "inventory_non_negative",
        "On-hand quantity must never be negative",
        "SELECT sku, on_hand_qty FROM inventory WHERE on_hand_qty < 0",
    ),
    (
        "one_open_po_per_product",
        "A product may have at most one open purchase order",
        """
        SELECT product_id, COUNT(*) AS open_pos, GROUP_CONCAT(po_id) AS po_ids
        FROM purchase_orders WHERE status = 'open'
        GROUP BY product_id HAVING COUNT(*) > 1
        """,
    ),
]


def check_all(conn):
    """Return [{"name", "description", "rows": [dict, ...]}] for violated invariants only."""
    violations = []
    for name, description, sql in INVARIANTS:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
        if rows:
            violations.append({"name": name, "description": description, "rows": rows})
    return violations
