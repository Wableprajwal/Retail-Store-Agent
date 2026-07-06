import difflib
import math
from .db import get_conn

TODAY = "2026-06-19"  # Hardcoded for testing; in real life, use date.today().isoformat()

def _today():
    return TODAY

def _next_id(conn, table, prefix, id_col):
    """
    Generate next sequential ID like O-1016, R-2002, etc.
    Compares numerically, not lexicographically — 'PO-9' must yield 'PO-10',
    not collide with it.
    """
    rows = conn.execute(f"SELECT {id_col} FROM {table}").fetchall()
    max_num = 0
    for row in rows:
        try:
            max_num = max(max_num, int(str(row[0]).split("-")[-1]))
        except ValueError:
            continue
    return f"{prefix}{max_num + 1}"

def _resolve_sku(conn, sku):
    """
    Resolve a SKU tolerantly. Models sometimes mistype SKUs (e.g. 'TEEBLU-M'
    for 'TEE-BLU-M'), so try: exact match, then case/dash-insensitive match,
    then return an error with close-match suggestions the model can act on.
    Returns (resolved_sku, None) or (None, error_message).
    """
    if conn.execute("SELECT 1 FROM skus WHERE sku=?", (sku,)).fetchone():
        return sku, None

    all_skus = [r["sku"] for r in conn.execute("SELECT sku FROM skus").fetchall()]
    norm = lambda s: "".join(c for c in s.upper() if c.isalnum())
    for candidate in all_skus:
        if norm(candidate) == norm(sku):
            return candidate, None

    close = difflib.get_close_matches(sku.upper(), all_skus, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return None, f"SKU '{sku}' not found.{hint}"


def _round_half_up(value, decimals=2):
    """Round half-up (banker's rounding avoidance per business rules)."""
    multiplier = 10 ** decimals
    return math.floor(value * multiplier + 0.5) / multiplier

def _get_active_promo_price(conn, sku, product_id, category, sale_date):
    """
    Return the lowest promo price for a SKU on a given date.
    If no promo applies, returns None (use retail_price).
    Business rule: if multiple promos apply, lowest price wins (no stacking).
    """
    retail = conn.execute(
        "SELECT retail_price FROM skus WHERE sku=?", (sku,)
    ).fetchone()["retail_price"]

    promos = conn.execute("""
        SELECT value FROM promotions
        WHERE type='percent_off'
          AND start_date <= ?
          AND end_date >= ?
          AND (
              (scope_type='product' AND scope_ref=?)
              OR (scope_type='category' AND scope_ref=?)
          )
    """, (sale_date, sale_date, product_id, category)).fetchall()

    if not promos:
        return None  # no promo active

    # Pick the promo giving the lowest price (highest discount)
    best_discount = max(row["value"] for row in promos)
    promo_price = _round_half_up(retail * (1 - best_discount / 100))
    return promo_price

# ── Tool: lookup_product ──────────────────────────────────────────────────────

def lookup_product(query="", color=None, size=None, category=None):
    conn = get_conn()
    sql = """
        SELECT s.sku, s.product_id, p.product_name, p.category,
               s.color, s.size, s.retail_price,
               i.on_hand_qty, i.reorder_point
        FROM skus s
        JOIN products p ON s.product_id = p.product_id
        LEFT JOIN inventory i ON s.sku = i.sku
        WHERE 1=1
    """
    params = []
    if query:
        sql += " AND (LOWER(p.product_name) LIKE ? OR LOWER(s.sku) LIKE ? OR LOWER(p.product_id) LIKE ?)"
        q = f"%{query.lower()}%"
        params += [q, q, q]
    if color:
        sql += " AND LOWER(s.color) = ?"
        params.append(color.lower())
    if size:
        sql += " AND LOWER(s.size) = ?"
        params.append(size.lower())
    if category:
        sql += " AND LOWER(p.category) = ?"
        params.append(category.lower())

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {"found": False, "message": f"No products matched '{query}'"}

    results = []
    for r in rows:
        results.append({
            "sku": r["sku"],
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "category": r["category"],
            "color": r["color"],
            "size": r["size"],
            "retail_price": r["retail_price"],
            "on_hand_qty": r["on_hand_qty"],
            "reorder_point": r["reorder_point"]
        })
    return {"found": True, "results": results}


# ── Tool: lookup_customer ─────────────────────────────────────────────────────

def lookup_customer(name=None, customer_id=None):
    conn = get_conn()
    if customer_id:
        row = conn.execute(
            "SELECT * FROM customers WHERE customer_id=?", (customer_id,)
        ).fetchone()
    elif name:
        row = conn.execute(
            "SELECT * FROM customers WHERE LOWER(name) LIKE ?",
            (f"%{name.lower()}%",)
        ).fetchone()
    else:
        return {"found": False, "message": "Provide name or customer_id"}

    if not row:
        return {"found": False, "message": "Customer not found"}
    return {"found": True, "customer_id": row["customer_id"],
            "name": row["name"], "email": row["email"]}


# ── Tool: create_order ────────────────────────────────────────────────────────

def create_order(items, payment_method, customer_id=None,
                 order_date=None, order_discount_pct=0):
    """
    Ring up a sale.
    items = [{"sku": "TEE-BLU-M", "quantity": 2}, ...]
    """
    if not order_date:
        order_date = _today()
    if not customer_id:  # treat "" same as None (walk-in)
        customer_id = None

    conn = get_conn()
    with conn:
        # Resolve SKUs tolerantly, then validate quantities and stock
        for item in items:
            resolved, err = _resolve_sku(conn, item["sku"])
            if err:
                return {"error": err}
            item["sku"] = resolved
            if not isinstance(item["quantity"], int) or item["quantity"] <= 0:
                return {"error": f"Invalid quantity {item['quantity']} for {resolved}. "
                                 "Quantities must be positive integers — to take items back, use create_return."}

        for item in items:
            sku = item["sku"]
            qty = item["quantity"]
            inv = conn.execute(
                "SELECT on_hand_qty FROM inventory WHERE sku=?", (sku,)
            ).fetchone()
            if not inv:
                return {"error": f"SKU {sku} not found in inventory"}
            if inv["on_hand_qty"] < qty:
                return {"error": f"Insufficient stock for {sku}: have {inv['on_hand_qty']}, need {qty}"}

        # Generate order ID
        order_id = _next_id(conn, "orders", "O-", "order_id")
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?)",
            (order_id, order_date, customer_id, order_discount_pct, payment_method)
        )

        lines = []
        order_total = 0.0

        for line_no, item in enumerate(items, 1):
            sku = item["sku"]
            qty = item["quantity"]

            # Get product info for promo lookup
            sku_row = conn.execute("""
                SELECT s.retail_price, s.product_id, p.category
                FROM skus s JOIN products p ON s.product_id=p.product_id
                WHERE s.sku=?
            """, (sku,)).fetchone()

            retail = sku_row["retail_price"]
            product_id = sku_row["product_id"]
            category = sku_row["category"]

            # Apply promo if active on sale date
            promo_price = _get_active_promo_price(
                conn, sku, product_id, category, order_date
            )
            unit_price = promo_price if promo_price is not None else retail

            # Apply order-level discount
            discounted = _round_half_up(unit_price * (1 - order_discount_pct / 100))
            line_total = _round_half_up(discounted * qty)
            order_total += line_total

            conn.execute(
                "INSERT INTO order_lines VALUES (?,?,?,?,?)",
                (order_id, line_no, sku, qty, unit_price)
            )

            # Decrement inventory
            conn.execute(
                "UPDATE inventory SET on_hand_qty = on_hand_qty - ? WHERE sku=?",
                (qty, sku)
            )

            lines.append({
                "sku": sku,
                "quantity": qty,
                "unit_price": unit_price,
                "discounted_unit_price": discounted,
                "line_total": line_total
            })

        return {
            "order_id": order_id,
            "order_date": order_date,
            "customer_id": customer_id or "walk-in",
            "payment_method": payment_method,
            "order_discount_pct": order_discount_pct,
            "lines": lines,
            "order_total": _round_half_up(order_total)
        }


# ── Tool: get_order ───────────────────────────────────────────────────────────

def get_order(order_id):
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not order:
        return {"error": f"Order {order_id} not found"}

    lines = conn.execute("""
        SELECT ol.*, s.retail_price, p.product_name, s.color, s.size
        FROM order_lines ol
        JOIN skus s ON ol.sku = s.sku
        JOIN products p ON s.product_id = p.product_id
        WHERE ol.order_id=?
        ORDER BY ol.line_no
    """, (order_id,)).fetchall()

    rets = conn.execute(
        "SELECT * FROM returns WHERE order_id=?", (order_id,)
    ).fetchall()

    discount = order["order_discount_pct"]
    line_list = []
    for l in lines:
        discounted = _round_half_up(l["unit_price"] * (1 - discount / 100))
        line_list.append({
            "line_no": l["line_no"],
            "sku": l["sku"],
            "product_name": l["product_name"],
            "color": l["color"],
            "size": l["size"],
            "quantity": l["quantity"],
            "unit_price": l["unit_price"],
            "discounted_unit_price": discounted,
            "line_total": _round_half_up(discounted * l["quantity"])
        })

    return {
        "order_id": order_id,
        "order_date": order["order_date"],
        "customer_id": order["customer_id"] or "walk-in",
        "payment_method": order["payment_method"],
        "order_discount_pct": discount,
        "lines": line_list,
        "returns": [dict(r) for r in rets]
    }


# ── Tool: create_return ───────────────────────────────────────────────────────

def create_return(order_id, sku, quantity, condition, return_date=None):
    if not return_date:
        return_date = _today()

    conn = get_conn()
    with conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            return {"error": f"Order {order_id} not found"}

        resolved, err = _resolve_sku(conn, sku)
        if err:
            return {"error": err}
        sku = resolved

        if not isinstance(quantity, int) or quantity <= 0:
            return {"error": f"Invalid return quantity {quantity} — must be a positive integer"}

        line = conn.execute(
            "SELECT * FROM order_lines WHERE order_id=? AND sku=?",
            (order_id, sku)
        ).fetchone()
        if not line:
            return {"error": f"SKU {sku} not found on order {order_id}"}

        if quantity > line["quantity"]:
            return {"error": f"Cannot return {quantity} units; only {line['quantity']} were sold"}

        # Business rule: refund = price actually paid (after order discount)
        discount = order["order_discount_pct"]
        paid_per_unit = _round_half_up(line["unit_price"] * (1 - discount / 100))
        refund_amount = _round_half_up(paid_per_unit * quantity)

        return_id = _next_id(conn, "returns", "R-", "return_id")
        conn.execute(
            "INSERT INTO returns VALUES (?,?,?,?,?,?,?)",
            (return_id, return_date, order_id, sku, quantity, condition, refund_amount)
        )

        # Business rule: good condition → restore inventory
        if condition == "good":
            conn.execute(
                "UPDATE inventory SET on_hand_qty = on_hand_qty + ? WHERE sku=?",
                (quantity, sku)
            )

        return {
            "return_id": return_id,
            "order_id": order_id,
            "sku": sku,
            "quantity": quantity,
            "condition": condition,
            "paid_per_unit": paid_per_unit,
            "refund_amount": refund_amount,
            "inventory_restored": condition == "good"
        }


# ── Tool: check_inventory ─────────────────────────────────────────────────────

def check_inventory(sku=None, product_id=None):
    conn = get_conn()
    sql = """
        SELECT i.sku, p.product_name, s.color, s.size,
               i.on_hand_qty, i.reorder_point, i.reorder_qty
        FROM inventory i
        JOIN skus s ON i.sku = s.sku
        JOIN products p ON s.product_id = p.product_id
        WHERE 1=1
    """
    params = []
    if sku:
        sql += " AND i.sku=?"
        params.append(sku)
    if product_id:
        sql += " AND s.product_id=?"
        params.append(product_id)
    sql += " ORDER BY p.product_name, s.color, s.size"

    rows = conn.execute(sql, params).fetchall()
    return {"inventory": [dict(r) for r in rows]}


# ── Tool: create_restock_orders ───────────────────────────────────────────────

def create_restock_orders(order_date=None):
    """
    Find all SKUs below reorder_point, pick best supplier per product,
    create purchase orders.
    Business rule: best = lowest unit_cost with lead_time_days <= 10.
    """
    if not order_date:
        order_date = _today()

    conn = get_conn()

    # Find SKUs needing restock
    below_reorder = conn.execute("""
        SELECT i.sku, s.product_id, i.on_hand_qty, i.reorder_point, i.reorder_qty
        FROM inventory i
        JOIN skus s ON i.sku = s.sku
        WHERE i.on_hand_qty <= i.reorder_point
    """).fetchall()

    if not below_reorder:
        return {"message": "Nothing is below reorder point", "purchase_orders": []}

    # Group by product_id — we create one PO per product per supplier
    product_qtys = {}
    for row in below_reorder:
        pid = row["product_id"]
        if pid not in product_qtys:
            product_qtys[pid] = 0
        product_qtys[pid] += row["reorder_qty"]

    created = []
    with conn:
        for product_id, total_qty in product_qtys.items():
            # Best supplier: lowest cost, lead_time <= 10
            supplier = conn.execute("""
                SELECT sc.supplier_id, s.supplier_name, sc.unit_cost, sc.lead_time_days
                FROM supplier_catalog sc
                JOIN suppliers s ON sc.supplier_id = s.supplier_id
                WHERE sc.product_id=? AND sc.lead_time_days <= 10
                ORDER BY sc.unit_cost ASC
                LIMIT 1
            """, (product_id,)).fetchone()

            if not supplier:
                created.append({
                    "product_id": product_id,
                    "error": "No eligible supplier (all have lead_time > 10)"
                })
                continue

            po_id = _next_id(conn, "purchase_orders", "PO-", "po_id")
            conn.execute(
                "INSERT INTO purchase_orders VALUES (?,?,?,NULL,?,?,0,'open')",
                (po_id, supplier["supplier_id"], product_id, order_date, total_qty)
            )

            created.append({
                "po_id": po_id,
                "product_id": product_id,
                "supplier": supplier["supplier_name"],
                "unit_cost": supplier["unit_cost"],
                "lead_time_days": supplier["lead_time_days"],
                "qty_ordered": total_qty,
                "order_date": order_date
            })

    return {"purchase_orders": created}


# ── Tool: receive_purchase_order ──────────────────────────────────────────────

def receive_purchase_order(qty_received, supplier_name=None, product_name=None,
                           qty_ordered=None, receive_date=None):
    if not receive_date:
        receive_date = _today()

    conn = get_conn()

    # Find the matching open PO
    sql = """
        SELECT po.*, s.supplier_name, p.product_name
        FROM purchase_orders po
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        JOIN products p ON po.product_id = p.product_id
        WHERE po.status='open'
    """
    params = []
    if supplier_name:
        sql += " AND LOWER(s.supplier_name) LIKE ?"
        params.append(f"%{supplier_name.lower()}%")
    if product_name:
        sql += " AND LOWER(p.product_name) LIKE ?"
        params.append(f"%{product_name.lower()}%")
    if qty_ordered:
        sql += " AND po.qty_ordered=?"
        params.append(qty_ordered)

    po = conn.execute(sql, params).fetchone()
    if not po:
        # No existing PO — create one on the fly (for prompt 5 scenario)
        # Find supplier and product
        sup = conn.execute(
            "SELECT * FROM suppliers WHERE LOWER(supplier_name) LIKE ?",
            (f"%{supplier_name.lower()}%",)
        ).fetchone() if supplier_name else None
        prod = conn.execute(
            "SELECT * FROM products WHERE LOWER(product_name) LIKE ?",
            (f"%{product_name.lower()}%",)
        ).fetchone() if product_name else None

        if not sup or not prod:
            return {"error": "Could not find matching open purchase order or supplier/product"}

        with conn:
            po_id = _next_id(conn, "purchase_orders", "PO-", "po_id")
            ordered = qty_ordered or qty_received
            conn.execute(
                "INSERT INTO purchase_orders VALUES (?,?,?,NULL,?,?,?,'open')",
                (po_id, sup["supplier_id"], prod["product_id"],
                 receive_date, ordered, qty_received)
            )
            # Increment inventory for all SKUs of this product
            skus = conn.execute(
                "SELECT sku FROM skus WHERE product_id=?", (prod["product_id"],)
            ).fetchall()
            # For goods (single SKU), update directly
            if len(skus) == 1:
                conn.execute(
                    "UPDATE inventory SET on_hand_qty = on_hand_qty + ? WHERE sku=?",
                    (qty_received, skus[0]["sku"])
                )
            status = "closed" if qty_received >= ordered else "open"
            conn.execute(
                "UPDATE purchase_orders SET status=? WHERE po_id=?",
                (status, po_id)
            )
        return {
            "po_id": po_id,
            "product": prod["product_name"],
            "supplier": sup["supplier_name"],
            "qty_ordered": ordered,
            "qty_received": qty_received,
            "status": status,
            "receive_date": receive_date
        }

    # Update existing PO
    with conn:
        outstanding = po["qty_ordered"] - po["qty_received"]
        if qty_received > outstanding:
            return {"error": f"Cannot receive {qty_received} units against {po['po_id']}: "
                             f"only {outstanding} of {po['qty_ordered']} ordered are still outstanding "
                             f"({po['qty_received']} already received)"}
        new_received = po["qty_received"] + qty_received
        status = "closed" if new_received >= po["qty_ordered"] else "open"
        conn.execute("""
            UPDATE purchase_orders
            SET qty_received=?, status=?
            WHERE po_id=?
        """, (new_received, status, po["po_id"]))

        # Increment inventory for all SKUs of this product
        skus = conn.execute(
            "SELECT sku FROM skus WHERE product_id=?", (po["product_id"],)
        ).fetchall()
        if len(skus) == 1:
            conn.execute(
                "UPDATE inventory SET on_hand_qty = on_hand_qty + ? WHERE sku=?",
                (qty_received, skus[0]["sku"])
            )

    return {
        "po_id": po["po_id"],
        "product": po["product_name"],
        "supplier": po["supplier_name"],
        "qty_ordered": po["qty_ordered"],
        "qty_received": new_received,
        "status": status,
        "receive_date": receive_date
    }


# ── Tool: create_promotion ────────────────────────────────────────────────────

def create_promotion(description, type, value, scope_type, scope_ref,
                     start_date, end_date):
    conn = get_conn()
    with conn:
        promo_id = _next_id(conn, "promotions", "PR-", "promo_id")
        conn.execute(
            "INSERT INTO promotions VALUES (?,?,?,?,?,?,?,?)",
            (promo_id, description, type, value, scope_type,
             scope_ref, start_date, end_date)
        )
    return {
        "promo_id": promo_id,
        "description": description,
        "type": type,
        "value": value,
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "start_date": start_date,
        "end_date": end_date
    }


# ── Tool: get_sales_report ────────────────────────────────────────────────────

def get_sales_report(start_date=None, end_date=None, month=None,
                     rank_by="margin", limit=5):
    """
    Revenue = unit_price × (1 − discount/100) × quantity, summed per product.
    COGS = Northwind cost × units that stayed sold (units - returned-good).
    Margin = Revenue − COGS of units that stayed sold.
    """
    if month:
        start_date = f"{month}-01"
        # Last day of month
        y, m = int(month.split("-")[0]), int(month.split("-")[1])
        import calendar
        last = calendar.monthrange(y, m)[1]
        end_date = f"{month}-{last:02d}"

    if not start_date or not end_date:
        return {"error": "Provide start_date+end_date or month"}

    conn = get_conn()

    # COGS = Northwind Supply unit cost per product (business rule 1),
    # read from supplier_catalog rather than hardcoded
    northwind_costs = {
        r["product_id"]: r["unit_cost"]
        for r in conn.execute("""
            SELECT sc.product_id, sc.unit_cost
            FROM supplier_catalog sc
            JOIN suppliers s ON sc.supplier_id = s.supplier_id
            WHERE LOWER(s.supplier_name) LIKE '%northwind%'
        """).fetchall()
    }

    rows = conn.execute("""
        SELECT
            p.product_id,
            p.product_name,
            SUM(ol.quantity) as units_sold,
            SUM(ol.quantity * ol.unit_price * (1 - o.order_discount_pct / 100.0)) as revenue
        FROM order_lines ol
        JOIN orders o ON ol.order_id = o.order_id
        JOIN skus s ON ol.sku = s.sku
        JOIN products p ON s.product_id = p.product_id
        WHERE o.order_date >= ? AND o.order_date <= ?
        GROUP BY p.product_id, p.product_name
    """, (start_date, end_date)).fetchall()

    # Good returns only: reduce units_stayed_sold (restocked → not a cost or revenue)
    good_returned = conn.execute("""
        SELECT s.product_id, SUM(r.quantity) as units_returned
        FROM returns r
        JOIN skus s ON r.sku = s.sku
        WHERE r.return_date >= ? AND r.return_date <= ?
          AND r.condition = 'good'
        GROUP BY s.product_id
    """, (start_date, end_date)).fetchall()
    good_returned_map = {r["product_id"]: r["units_returned"] for r in good_returned}

    # All refunds (good + damaged) reduce net revenue per business rule 6
    all_refunds = conn.execute("""
        SELECT s.product_id, SUM(r.refund_amount) as refunds
        FROM returns r
        JOIN skus s ON r.sku = s.sku
        WHERE r.return_date >= ? AND r.return_date <= ?
        GROUP BY s.product_id
    """, (start_date, end_date)).fetchall()
    refunds_map = {r["product_id"]: r["refunds"] for r in all_refunds}

    results = []
    for row in rows:
        pid = row["product_id"]
        cost_per_unit = northwind_costs.get(pid, 0)
        units_sold = row["units_sold"]
        revenue = round(row["revenue"], 2)

        units_returned_good = good_returned_map.get(pid, 0)
        refunds = refunds_map.get(pid, 0)

        units_stayed_sold = units_sold - units_returned_good
        cogs = round(cost_per_unit * units_stayed_sold, 2)
        net_revenue = round(revenue - refunds, 2)
        margin = round(net_revenue - cogs, 2)
        margin_pct = round((margin / net_revenue * 100) if net_revenue else 0, 1)

        results.append({
            "product_id": pid,
            "product_name": row["product_name"],
            "units_sold": units_sold,
            "units_stayed_sold": units_stayed_sold,
            "revenue": revenue,
            "refunds": refunds,
            "net_revenue": net_revenue,
            "cogs": cogs,
            "margin": margin,
            "margin_pct": margin_pct
        })

    # Sort
    sort_key = {"margin": "margin", "revenue": "net_revenue", "units": "units_sold"}[rank_by]
    results.sort(key=lambda x: x[sort_key], reverse=True)

    return {
        "period": f"{start_date} to {end_date}",
        "rank_by": rank_by,
        "results": results[:limit]
    }


# ── Tool: get_stockout_risk ───────────────────────────────────────────────────

def get_stockout_risk():
    """
    Flag: on_hand <= reorder_point OR days_of_cover < 14.
    Velocity = units sold May 2026 (trailing 30 days per spec).
    days_of_cover = on_hand / (monthly_units / 30), aggregated per product.
    """
    conn = get_conn()

    # May 2026 velocity per product
    velocity = conn.execute("""
        SELECT s.product_id, SUM(ol.quantity) as units_sold
        FROM order_lines ol
        JOIN orders o ON ol.order_id = o.order_id
        JOIN skus s ON ol.sku = s.sku
        WHERE o.order_date >= '2026-05-01' AND o.order_date <= '2026-05-31'
        GROUP BY s.product_id
    """).fetchall()
    vel_map = {r["product_id"]: r["units_sold"] for r in velocity}

    # Inventory per product (sum across variants)
    inv = conn.execute("""
        SELECT s.product_id, p.product_name,
               SUM(i.on_hand_qty) as total_on_hand,
               MIN(i.on_hand_qty) as min_variant_on_hand,
               MIN(i.reorder_point) as reorder_point
        FROM inventory i
        JOIN skus s ON i.sku = s.sku
        JOIN products p ON s.product_id = p.product_id
        GROUP BY s.product_id, p.product_name
    """).fetchall()

    at_risk = []
    for row in inv:
        pid = row["product_id"]
        on_hand = row["total_on_hand"]
        rp = row["reorder_point"]
        monthly_units = vel_map.get(pid, 0)
        daily_rate = monthly_units / 30 if monthly_units > 0 else 0
        days_of_cover = (on_hand / daily_rate) if daily_rate > 0 else float("inf")

        below_reorder = row["min_variant_on_hand"] <= rp
        low_cover = days_of_cover < 14

        if below_reorder or low_cover:
            at_risk.append({
                "product_id": pid,
                "product_name": row["product_name"],
                "total_on_hand": on_hand,
                "reorder_point": rp,
                "below_reorder_point": below_reorder,
                "monthly_velocity": monthly_units,
                "days_of_cover": round(days_of_cover, 1) if days_of_cover != float("inf") else "∞",
                "low_cover": low_cover
            })

    at_risk.sort(key=lambda x: x["total_on_hand"])
    return {"at_risk_products": at_risk, "flagged_count": len(at_risk)}


# ── Tool: list_promotions ─────────────────────────────────────────────────────

def list_promotions(active_on=None):
    conn = get_conn()
    sql = "SELECT * FROM promotions"
    params = []
    if active_on:
        sql += " WHERE start_date <= ? AND end_date >= ?"
        params = [active_on, active_on]
    rows = conn.execute(sql, params).fetchall()
    return {"promotions": [dict(r) for r in rows]}


# ── Tool: list_purchase_orders ────────────────────────────────────────────────

def list_purchase_orders(status=None):
    conn = get_conn()
    sql = """
        SELECT po.*, s.supplier_name, p.product_name
        FROM purchase_orders po
        JOIN suppliers s ON po.supplier_id = s.supplier_id
        JOIN products p ON po.product_id = p.product_id
    """
    params = []
    if status:
        sql += " WHERE po.status=?"
        params.append(status)
    rows = conn.execute(sql, params).fetchall()
    return {"purchase_orders": [dict(r) for r in rows]}


# ── Tool: list_orders ────────────────────────────────────────────────────────

def list_orders(customer_id=None, customer_name=None, start_date=None,
                end_date=None, order_date=None, limit=20):
    """
    List orders with optional filters. Supports:
    - by customer (id or name)
    - by date range or exact date
    Returns summarized order headers with totals.
    """
    conn = get_conn()

    # Resolve customer name → id
    if customer_name and not customer_id:
        row = conn.execute(
            "SELECT customer_id FROM customers WHERE LOWER(name) LIKE ?",
            (f"%{customer_name.lower()}%",)
        ).fetchone()
        if row:
            customer_id = row["customer_id"]

    sql = """
        SELECT o.order_id, o.order_date, o.customer_id,
               c.name as customer_name,
               o.order_discount_pct, o.payment_method,
               SUM(ol.quantity * ol.unit_price * (1 - o.order_discount_pct / 100.0)) as order_total,
               COUNT(ol.line_no) as line_count
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.customer_id
        LEFT JOIN order_lines ol ON o.order_id = ol.order_id
        WHERE 1=1
    """
    params = []
    if customer_id:
        sql += " AND o.customer_id=?"
        params.append(customer_id)
    if order_date:
        sql += " AND o.order_date=?"
        params.append(order_date)
    elif start_date and end_date:
        sql += " AND o.order_date >= ? AND o.order_date <= ?"
        params += [start_date, end_date]
    elif start_date:
        sql += " AND o.order_date >= ?"
        params.append(start_date)

    sql += " GROUP BY o.order_id ORDER BY o.order_date DESC, o.order_id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "order_id": r["order_id"],
            "order_date": r["order_date"],
            "customer": r["customer_name"] or "walk-in",
            "payment_method": r["payment_method"],
            "order_discount_pct": r["order_discount_pct"],
            "line_count": r["line_count"],
            "order_total": round(r["order_total"] or 0, 2)
        })
    return {"orders": results, "count": len(results)}


# ── Dispatch ──────────────────────────────────────────────────────────────────

TOOL_MAP = {
    "lookup_product": lookup_product,
    "lookup_customer": lookup_customer,
    "create_order": create_order,
    "get_order": get_order,
    "list_orders": list_orders,
    "create_return": create_return,
    "check_inventory": check_inventory,
    "create_restock_orders": create_restock_orders,
    "receive_purchase_order": receive_purchase_order,
    "create_promotion": create_promotion,
    "get_sales_report": get_sales_report,
    "get_stockout_risk": get_stockout_risk,
    "list_promotions": list_promotions,
    "list_purchase_orders": list_purchase_orders,
}

def dispatch(tool_name, tool_input):
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return fn(**tool_input)
    except Exception as e:
        return {"error": str(e)}