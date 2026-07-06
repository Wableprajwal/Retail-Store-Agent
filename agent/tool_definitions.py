TOOLS = [
    {
        "name": "lookup_product",
        "description": (
            "Search for products/SKUs by name, color, size, category, or SKU. "
            "Use this to resolve vague references like 'hoodie in medium' or "
            "'blue tee' before ringing up a sale. Returns matching SKUs with "
            "current inventory levels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search e.g. 'hoodie medium' or 'canvas tote'"},
                "color": {"type": "string", "description": "Optional color filter e.g. 'Blue', 'Navy', 'Gray'"},
                "size": {"type": "string", "description": "Optional size filter e.g. 'S', 'M', 'L'"},
                "category": {"type": "string", "description": "Optional: 'apparel' or 'goods'"}
            },
        }
    },
    {
        "name": "lookup_customer",
        "description": "Find a customer by name or customer_id. Returns customer_id needed for creating orders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer name to search"},
                "customer_id": {"type": "string", "description": "Exact customer_id like C-001"}
            }
        }
    },
    {
        "name": "create_order",
        "description": (
            "Ring up a sale — create an order with one or more line items. "
            "Automatically applies any active promotions on the sale date. "
            "Decrements inventory. For walk-ins, omit customer_id. "
            "Returns order_id, itemized receipt, and total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Line items to sell",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "integer"}
                        },
                        "required": ["sku", "quantity"]
                    }
                },
                "payment_method": {"type": "string", "enum": ["cash", "card"]},
                "customer_id": {"type": "string", "description": "Omit for walk-in"},
                "order_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today (2026-06-19)"},
                "order_discount_pct": {"type": "number", "description": "Whole-order % discount, default 0"}
            },
            "required": ["items", "payment_method"]
        }
    },
    {
        "name": "get_order",
        "description": "Retrieve a specific order with all its lines, customer info, and any returns against it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"}
            },
            "required": ["order_id"]
        }
    },
    {
        "name": "create_return",
        "description": (
            "Process a return against an existing order. Refunds the price actually "
            "paid (after order discount). 'good' condition restores inventory; "
            "'damaged' does not. Returns refund amount."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "sku": {"type": "string"},
                "quantity": {"type": "integer"},
                "condition": {"type": "string", "enum": ["good", "damaged"]},
                "return_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"}
            },
            "required": ["order_id", "sku", "quantity", "condition"]
        }
    },
    {
        "name": "check_inventory",
        "description": "Check current stock levels for one or all SKUs. Shows on_hand_qty, reorder_point, reorder_qty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Specific SKU, or omit for all"},
                "product_id": {"type": "string", "description": "Filter by product e.g. P-HOOD"}
            }
        }
    },
    {
        "name": "create_restock_orders",
        "description": (
            "USE THIS when asked to 'reorder', 'restock', or 'place purchase orders'. "
            "Finds all SKUs at or below their reorder point and creates purchase orders "
            "automatically, picking the best supplier (lowest cost with lead_time <= 10 days). "
            "Do NOT use check_inventory for this — call create_restock_orders directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today (2026-06-19)"}
            }
        }
    },
    {
        "name": "receive_purchase_order",
        "description": (
            "Record that ordered goods have physically arrived. "
            "Increments inventory by the number received. Closes the PO if fully received. "
            "IMPORTANT: use 'qty_received' (not received_qty or received_amount) for how many arrived."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_name": {"type": "string", "description": "Supplier name e.g. 'Northwind'"},
                "product_name": {"type": "string", "description": "Product name e.g. 'Canvas Tote'"},
                "qty_ordered": {"type": "integer", "description": "The original PO size e.g. 50, used to find the right open PO"},
                "qty_received": {"type": "integer", "description": "How many units actually arrived e.g. 40"},
                "receive_date": {"type": "string", "description": "YYYY-MM-DD date goods arrived, defaults to today"}
            },
            "required": ["qty_received"]
        }
    },
    {
        "name": "create_promotion",
        "description": (
            "Create a new promotion (e.g. 20% off all hoodies for a date range). "
            "scope_type='product' targets one product by product_id; scope_type='category' targets all apparel or all goods. "
            "IMPORTANT: scope_ref must be a product_id or category — use 'P-HOOD' for hoodies, 'P-TEE' for tees, "
            "'P-TOTE' for totes, 'P-MUG' for mugs, 'P-SOCK' for socks, 'apparel' or 'goods' for categories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Human-readable promo name e.g. 'Hoodie 20% off'"},
                "type": {"type": "string", "enum": ["percent_off"]},
                "value": {"type": "number", "description": "Discount percentage e.g. 20 means 20% off"},
                "scope_type": {"type": "string", "enum": ["product", "category"]},
                "scope_ref": {"type": "string", "description": "product_id: P-TEE, P-HOOD, P-TOTE, P-MUG, P-SOCK — or category: apparel, goods"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD inclusive start"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD inclusive end"}
            },
            "required": ["description", "type", "value", "scope_type", "scope_ref", "start_date", "end_date"]
        }
    },
    {
        "name": "get_sales_report",
        "description": (
            "Report on sales performance: revenue, cost, margin by product. "
            "Filters by date range. Use month='2026-05' for 'last month'. "
            "Returns products ranked by the requested metric."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "month": {"type": "string", "description": "YYYY-MM shorthand e.g. '2026-05'"},
                "rank_by": {"type": "string", "enum": ["margin", "revenue", "units"], "description": "default: margin"},
                "limit": {"type": "integer", "description": "Top N results, default 5"}
            }
        }
    },
    {
        "name": "get_stockout_risk",
        "description": (
            "Identify products at risk of stocking out. A product is flagged if it is "
            "below its reorder point OR has fewer than 14 days of cover "
            "(days_of_cover = on_hand / (units_sold_last_30_days / 30)). "
            "Uses May 2026 sales as the velocity window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "list_promotions",
        "description": "List all promotions (active, past, and future).",
        "input_schema": {
            "type": "object",
            "properties": {
                "active_on": {"type": "string", "description": "YYYY-MM-DD to filter to promos active on that date"}
            }
        }
    },
    {
        "name": "list_orders",
        "description": (
            "List orders with optional filters. Use to answer 'what has Sarah bought?', "
            "'show me today's orders', 'what orders came in last week?'. "
            "Returns order summaries with totals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Filter by customer name e.g. 'Sarah Chen'"},
                "customer_id": {"type": "string", "description": "Filter by exact customer_id e.g. 'C-001'"},
                "order_date": {"type": "string", "description": "Exact date YYYY-MM-DD"},
                "start_date": {"type": "string", "description": "Range start YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Range end YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Max rows to return, default 20"}
            }
        }
    },
    {
        "name": "list_purchase_orders",
        "description": "List purchase orders, optionally filtered by status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "closed"], "description": "Omit for all"}
            }
        }
    }
]