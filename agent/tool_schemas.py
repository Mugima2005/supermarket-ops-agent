"""
Tool schemas passed to the Anthropic API's `tools` parameter.

Design principle: tools are thin and single-purpose. The model composes
them (e.g. start_bill -> add_bill_item x N -> get_bill_draft -> finalize_bill)
rather than one tool doing "handle_billing_message". This is what keeps
the intent logic in the model instead of in an if/elif router.
"""

TOOLS = [
    # ---------------- Inventory ----------------
    {
        "name": "add_product",
        "description": "Register a brand-new SKU that does not yet exist in the store. Use this only for products the owner is introducing for the first time, not for restocking an existing item (use receive_stock for that).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact product name, e.g. 'Amul Butter 100g'."},
                "unit": {"type": "string", "enum": ["kg", "g", "l", "ml", "packet", "dozen", "piece"]},
                "gst_rate": {"type": "number", "description": "GST % slab: 0, 5, 12, or 18."},
                "cost_price": {"type": "number", "description": "Purchase cost per unit."},
                "sell_price": {"type": "number", "description": "Pre-tax selling price per unit."},
                "mrp": {"type": "number", "description": "Printed MRP, if applicable."},
                "category": {"type": "string"},
                "hsn_code": {"type": "string", "description": "HSN/SAC code for GST filing."},
                "initial_qty": {"type": "number", "default": 0},
                "reorder_level": {"type": "number", "default": 0},
                "is_loose": {"type": "boolean", "default": False, "description": "True for loose commodities sold by weight (rice, dal, sugar)."},
            },
            "required": ["name", "unit", "gst_rate", "cost_price", "sell_price"],
        },
    },
    {
        "name": "receive_stock",
        "description": "Record incoming stock for an EXISTING product (a delivery arriving). Optionally update cost/MRP/sell price if the supplier changed them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product name or sku_id."},
                "qty": {"type": "number"},
                "cost_price": {"type": "number"},
                "mrp": {"type": "number"},
                "sell_price": {"type": "number"},
            },
            "required": ["product", "qty"],
        },
    },
    {
        "name": "search_products",
        "description": "Fuzzy-search products by partial name. Use this to disambiguate when the owner's wording could match more than one SKU (e.g. 'atta' could be Aashirvaad 5kg or a loose variant) before asking a clarifying question.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_stock",
        "description": "Look up current quantity, unit and sell price for one product.",
        "input_schema": {
            "type": "object",
            "properties": {"product": {"type": "string", "description": "Product name or sku_id."}},
            "required": ["product"],
        },
    },
    {
        "name": "list_low_stock",
        "description": "List all products at or below their reorder level — answers 'what's running out?'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ---------------- Billing ----------------
    {
        "name": "start_bill",
        "description": "Open a new draft bill for this chat (or resume one already open). Call this once at the start of 'make a bill' before adding items. A bill only affects stock once finalize_bill is called.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "customer_name": {"type": "string", "description": "Only if this bill may go on khata / credit."},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "add_bill_item",
        "description": "Add a line item to a draft bill. Refused at the tool layer if requested qty exceeds current stock (oversell guard) — surface that refusal to the owner rather than adding anyway.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "product": {"type": "string"},
                "qty": {"type": "number"},
            },
            "required": ["bill_id", "product", "qty"],
        },
    },
    {
        "name": "remove_bill_item",
        "description": "Drop a product entirely from a draft bill (e.g. 'drop the butter').",
        "input_schema": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}, "product": {"type": "string"}},
            "required": ["bill_id", "product"],
        },
    },
    {
        "name": "update_bill_item_qty",
        "description": "Change the quantity of a product already on a draft bill (e.g. 'make it 6 Maggi'). Setting qty to 0 removes it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "product": {"type": "string"},
                "qty": {"type": "number"},
            },
            "required": ["bill_id", "product", "qty"],
        },
    },
    {
        "name": "get_bill_draft",
        "description": "Fetch the current state of a bill (items, subtotal, GST breakup, total) — use before reading it back to the owner or before finalizing.",
        "input_schema": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": ["bill_id"],
        },
    },
    {
        "name": "cancel_bill",
        "description": "Abandon a draft bill without affecting stock.",
        "input_schema": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": ["bill_id"],
        },
    },
    {
        "name": "finalize_bill",
        "description": (
            "Close out a draft bill: atomically re-checks stock, decrements it, records the sale, "
            "and (for khata) adds the total to the customer's balance. This is the ONLY tool that "
            "changes stock for a sale. Always pass idempotency_key as a stable string you derive from "
            "the conversation turn (e.g. 'finalize:{bill_id}:{turn_id}') so a retried message can't double-bill. "
            "If it returns ok=false with an oversell or below-cost error, explain it to the owner — do not retry blindly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "payment_mode": {"type": "string", "enum": ["cash", "upi", "card", "khata"]},
                "payment_ref": {"type": "string", "description": "UPI/card reference, if given."},
                "idempotency_key": {"type": "string"},
                "allow_below_cost": {"type": "boolean", "default": False, "description": "Only set true if the owner explicitly confirms selling below cost."},
            },
            "required": ["bill_id", "payment_mode", "idempotency_key"],
        },
    },
    # ---------------- Khata ----------------
    {
        "name": "add_khata_charge",
        "description": "Put an amount on a customer's credit tab directly (not tied to a bill), e.g. 'put ₹500 on Ramesh's credit'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "amount": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["customer_name", "amount"],
        },
    },
    {
        "name": "record_khata_payment",
        "description": "Record a customer settling (part of) their credit balance, e.g. 'Ramesh paid ₹300'. Refuses if the customer has no khata account or no outstanding balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "amount": {"type": "number"},
                "note": {"type": "string"},
            },
            "required": ["customer_name", "amount"],
        },
    },
    {
        "name": "get_khata_balance",
        "description": "Get one customer's current outstanding credit balance.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_name": {"type": "string"}},
            "required": ["customer_name"],
        },
    },
    {
        "name": "list_khata_customers",
        "description": "List every customer with an outstanding khata balance, highest first.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ---------------- Analytics ----------------
    {
        "name": "get_daily_close",
        "description": "Answers 'today's sales?' / 'close the day' — totals, tax collected, cash vs UPI vs card breakdown, and top items for a given date (defaults to today).",
        "input_schema": {
            "type": "object",
            "properties": {"for_date": {"type": "string", "description": "YYYY-MM-DD, defaults to today."}},
        },
    },
    {
        "name": "get_sales_summary",
        "description": "Aggregate sales and tax collected between two dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_top_items",
        "description": "Top-selling products by revenue in a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_stock_health",
        "description": "Full stock snapshot sorted by how close each product is to its reorder level. Use before building the analysis deck.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ---------------- Documents ----------------
    {
        "name": "generate_invoice_pdf",
        "description": "Produce a GST-correct PDF invoice for a finalized bill. Only works on bills with status 'finalized'.",
        "input_schema": {
            "type": "object",
            "properties": {"bill_id": {"type": "integer"}},
            "required": ["bill_id"],
        },
    },
    {
        "name": "generate_analysis_deck",
        "description": "Produce a PPTX business-analysis deck (sales trend, top items, stock health, GST collected) for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "title": {"type": "string", "description": "Deck title, e.g. 'This Week's Sales Analysis'."},
            },
            "required": ["start_date", "end_date"],
        },
    },
    # ---------------- Preferences (cross-session memory) ----------------
    {
        "name": "set_preference",
        "description": (
            "Persist a standing owner preference that must survive a /new chat — default payment mode, "
            "a preferred brand default (e.g. 'default atta = Aashirvaad 5kg'), shop name, or GSTIN for invoices. "
            "Call this whenever the owner states a standing rule with words like 'always', 'default', 'from now on'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "key": {"type": "string", "description": "e.g. 'default_payment_mode', 'default_atta', 'shop_name', 'gstin'."},
                "value": {"type": "string"},
            },
            "required": ["chat_id", "key", "value"],
        },
    },
    {
        "name": "get_preference",
        "description": "Read one standing preference for this chat/owner.",
        "input_schema": {
            "type": "object",
            "properties": {"chat_id": {"type": "string"}, "key": {"type": "string"}},
            "required": ["chat_id", "key"],
        },
    },
    {
        "name": "get_all_preferences",
        "description": "Read every standing preference for this chat/owner. Call this at the start of a session (or a fresh /new chat) so defaults like payment mode or preferred brand are applied without the owner repeating them.",
        "input_schema": {
            "type": "object",
            "properties": {"chat_id": {"type": "string"}},
            "required": ["chat_id"],
        },
    },
]
