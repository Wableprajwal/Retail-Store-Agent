"""
Deterministic tests for the tool/business-rules layer. No LLM, no API key.

Run:  python tests/test_tools.py     (or: python -m pytest tests/ -v)

Each test seeds a fresh temp database from data/*.csv, so tests verify the
exact business rules from DATA_DICTIONARY.md against the real seed data.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.db as db


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.DB_PATH = path
    from agent.seed import seed
    seed()


def _on_hand(sku):
    conn = db.get_conn()
    return conn.execute("SELECT on_hand_qty FROM inventory WHERE sku=?", (sku,)).fetchone()[0]


# ── Sales ─────────────────────────────────────────────────────────────────────

def test_walk_in_order_total():
    """Prompt 1: two Blue-M tees + one tote, cash, today -> $68.00, no promo."""
    fresh_db()
    from agent.tools import create_order
    result = create_order(
        items=[{"sku": "TEE-BLU-M", "quantity": 2}, {"sku": "TOTE", "quantity": 1}],
        payment_method="cash", order_date="2026-06-19",
    )
    assert "error" not in result, result
    assert result["order_total"] == 68.00, result
    assert result["customer_id"] == "walk-in"


def test_insufficient_stock_rejected():
    """Prompt 2: ten totes but only 4 on hand -> clean error, nothing written."""
    fresh_db()
    from agent.tools import create_order
    before = _on_hand("TOTE")
    result = create_order(items=[{"sku": "TOTE", "quantity": 10}],
                          payment_method="cash")
    assert "error" in result, result
    assert _on_hand("TOTE") == before


def test_order_decrements_inventory():
    fresh_db()
    from agent.tools import create_order
    before = _on_hand("MUG")
    create_order(items=[{"sku": "MUG", "quantity": 3}], payment_method="card")
    assert _on_hand("MUG") == before - 3


# ── Promotions ────────────────────────────────────────────────────────────────

def test_promo_applies_within_window():
    """Prompt 8: 20% off hoodies 06-20..06-22; sale on 06-21 -> $48.00."""
    fresh_db()
    from agent.tools import create_promotion, create_order
    create_promotion(description="Hoodie 20% off", type="percent_off", value=20,
                     scope_type="product", scope_ref="P-HOOD",
                     start_date="2026-06-20", end_date="2026-06-22")
    result = create_order(items=[{"sku": "HOOD-GRY-M", "quantity": 1}],
                          payment_method="cash", order_date="2026-06-21")
    assert result["lines"][0]["unit_price"] == 48.00, result


def test_promo_not_applied_outside_window():
    fresh_db()
    from agent.tools import create_promotion, create_order
    create_promotion(description="Hoodie 20% off", type="percent_off", value=20,
                     scope_type="product", scope_ref="P-HOOD",
                     start_date="2026-06-20", end_date="2026-06-22")
    result = create_order(items=[{"sku": "HOOD-GRY-M", "quantity": 1}],
                          payment_method="cash", order_date="2026-06-19")
    assert result["lines"][0]["unit_price"] == 60.00, result


# ── Returns ───────────────────────────────────────────────────────────────────

def test_return_refunds_discounted_price():
    """Prompt 6: O-1006 has 10% order discount -> hoodie refund $54, restocked."""
    fresh_db()
    from agent.tools import create_return
    before = _on_hand("HOOD-NVY-L")
    result = create_return(order_id="O-1006", sku="HOOD-NVY-L",
                           quantity=1, condition="good")
    assert result["refund_amount"] == 54.00, result
    assert _on_hand("HOOD-NVY-L") == before + 1


def test_damaged_return_no_restock():
    """Prompt 7: damaged tote from O-1006 -> refund $16.20, NOT restocked."""
    fresh_db()
    from agent.tools import create_return
    before = _on_hand("TOTE")
    result = create_return(order_id="O-1006", sku="TOTE",
                           quantity=1, condition="damaged")
    assert result["refund_amount"] == 16.20, result
    assert _on_hand("TOTE") == before


# ── Purchasing ────────────────────────────────────────────────────────────────

def test_restock_picks_eligible_cheapest_supplier():
    """Prompt 4: tote below reorder point. Pioneer is cheaper ($6.50) but 14-day
    lead time makes it ineligible -> Northwind at $7.00 wins."""
    fresh_db()
    from agent.tools import create_restock_orders
    result = create_restock_orders(order_date="2026-06-19")
    tote_po = next(po for po in result["purchase_orders"]
                   if po.get("product_id") == "P-TOTE")
    assert "Northwind" in tote_po["supplier"], tote_po
    assert tote_po["unit_cost"] == 7.00, tote_po


def test_receive_partial_po_stays_open():
    """Prompt 5: 40 of 50 totes arrive -> inventory +40, PO stays open."""
    fresh_db()
    from agent.tools import create_restock_orders, receive_purchase_order
    create_restock_orders(order_date="2026-06-19")
    before = _on_hand("TOTE")
    result = receive_purchase_order(qty_received=40, supplier_name="Northwind",
                                    product_name="Canvas Tote", qty_ordered=50)
    assert result["status"] == "open", result
    assert _on_hand("TOTE") == before + 40


def test_over_receipt_rejected():
    """Regression: receiving the same 40-unit delivery twice pushed a 50-unit
    PO to 80 received and inflated inventory. Over-receipt must be rejected."""
    fresh_db()
    from agent.tools import create_restock_orders, receive_purchase_order
    create_restock_orders(order_date="2026-06-19")
    receive_purchase_order(qty_received=40, supplier_name="Northwind",
                           product_name="Canvas Tote", qty_ordered=50)
    stock_after_first = _on_hand("TOTE")
    result = receive_purchase_order(qty_received=40, supplier_name="Northwind",
                                    product_name="Canvas Tote", qty_ordered=50)
    assert "error" in result, result
    assert "10" in result["error"], result  # tells the model what IS outstanding
    assert _on_hand("TOTE") == stock_after_first


# ── Reporting ─────────────────────────────────────────────────────────────────

def test_sales_report_may_margins():
    """Prompt 9: May 2026 top product by margin is the Classic Tee at $420."""
    fresh_db()
    from agent.tools import get_sales_report
    result = get_sales_report(month="2026-05", rank_by="margin", limit=5)
    top = result["results"][0]
    assert top["product_id"] == "P-TEE", top
    assert top["margin"] == 420.00, top


def test_stockout_flags_tote():
    """Prompt 10: tote (4 on hand, reorder point 10) must be flagged."""
    fresh_db()
    from agent.tools import get_stockout_risk
    result = get_stockout_risk()
    flagged = {p["product_id"] for p in result["at_risk_products"]}
    assert "P-TOTE" in flagged, result


def test_mistyped_sku_resolves():
    """Regression: qwen2:7b emitted 'TEEBLU-M' for 'TEE-BLU-M'. Dash/case slips
    must resolve instead of failing the sale."""
    fresh_db()
    from agent.tools import create_order
    result = create_order(items=[{"sku": "TEEBLU-M", "quantity": 2},
                                 {"sku": "tote", "quantity": 1}],
                          payment_method="cash", order_date="2026-06-19")
    assert "error" not in result, result
    assert result["order_total"] == 68.00, result
    assert result["lines"][0]["sku"] == "TEE-BLU-M", result


def test_negative_quantity_rejected():
    """Regression: qwen2.5 tried create_order with quantity -1 to fake a
    return, which would have INCREASED inventory. Must be rejected."""
    fresh_db()
    from agent.tools import create_order, create_return
    before = _on_hand("HOOD-NVY-L")
    result = create_order(items=[{"sku": "HOOD-NVY-L", "quantity": -1}],
                          payment_method="cash")
    assert "error" in result, result
    assert _on_hand("HOOD-NVY-L") == before
    result = create_return(order_id="O-1006", sku="HOOD-NVY-L",
                           quantity=-1, condition="good")
    assert "error" in result, result


def test_unknown_sku_suggests_closest():
    """A genuinely wrong SKU returns a 'did you mean' hint the model can act on."""
    fresh_db()
    from agent.tools import create_order
    result = create_order(items=[{"sku": "HOOD-GRY-XL", "quantity": 1}],
                          payment_method="cash")
    assert "error" in result, result
    assert "Did you mean" in result["error"], result


# ── Infrastructure ────────────────────────────────────────────────────────────

def test_id_generation_past_ten_records():
    """Regression: lexicographic ordering would collide at the 10th ID
    (PO-9 > PO-10 as strings). IDs must stay unique numerically."""
    fresh_db()
    conn = db.get_conn()
    from agent.tools import _next_id
    ids = set()
    with conn:
        for _ in range(12):
            po_id = _next_id(conn, "purchase_orders", "PO-", "po_id")
            assert po_id not in ids, f"duplicate id {po_id}"
            ids.add(po_id)
            conn.execute(
                "INSERT INTO purchase_orders VALUES (?,?,?,NULL,?,?,0,'open')",
                (po_id, "SUP-NW", "P-TOTE", "2026-06-19", 10),
            )


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
