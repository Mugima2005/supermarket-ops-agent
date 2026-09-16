# Supermarket Ops Agent

An agent that runs a small Indian kirana store end-to-end from Telegram — receiving stock,
cutting GST-correct bills, running khata (credit), closing the day, and generating invoices
and analysis decks. No admin panel, no command router. The chat is the product.

**Telegram bot:** `@My Kirana Store`

## Harness

A hand-rolled `observe → reason → act (tool call) → feed result back → continue` loop
(`agent/control_loop.py`) on top of an LLM's native tool-calling API — Claude's Messages API
or Gemini's OpenAI-compatible endpoint, switchable via `LLM_PROVIDER` in `.env` with zero
other code changes. No graph framework: the brief explicitly rules out a node-per-command
state machine, and at this scale (22 tools, single agent) a thin loop I can log and reason
about beats a heavier abstraction. All business logic — GST math, oversell guard,
idempotency, khata rules — lives in `agent/tools.py` and the SQL in `db/models.py`, never in
the prompt. The loop's only job is dispatch.

## Skills & tools

22 thin tools across 5 skills (`agent/tool_schemas.py`):
- **Inventory** — `add_product`, `receive_stock`, `search_products`, `get_stock`, `list_low_stock`
- **Billing** — `start_bill`, `add_bill_item`, `remove_bill_item`, `update_bill_item_qty`, `get_bill_draft`, `cancel_bill`, `finalize_bill`
- **Khata** — `add_khata_charge`, `record_khata_payment`, `get_khata_balance`, `list_khata_customers`
- **Analytics** — `get_daily_close`, `get_sales_summary`, `get_top_items`, `get_stock_health`
- **Documents** — `generate_invoice_pdf`, `generate_analysis_deck`
- **Preferences** — `set_preference`, `get_preference`, `get_all_preferences`

No tool handles "the billing message" — the model composes `start_bill` → `add_bill_item` × N
→ edits → `finalize_bill` itself, across however many turns the owner takes. `search_products`
exists so the model can check real ambiguity before asking a clarifying question.

## The hard parts

| Hard part | How |
|---|---|
| Grounding | Every price/GST/stock fact comes from SQLite via a tool call — nothing hardcoded in the prompt. |
| Oversell guard | `UPDATE products SET qty = qty - ? WHERE qty >= ?`; `rowcount == 0` means refused. Enforced in SQL, not the prompt. |
| GST correctness | Each line stores its HSN + slab; CGST/SGST split 50/50, rounded, shown in full on the PDF invoice. |
| Multi-turn bills | `bills`/`bill_items` accumulate a draft across messages; `active_bills` maps `chat_id → bill_id`. Stock untouched until `finalize_bill`. |
| Idempotency | `idempotency_keys` table: Telegram `update_id`s are deduped in `bot.py`; `finalize_bill` takes a caller idempotency key and returns the cached result on retry instead of re-running side effects. |
| Concurrency | Every stock/khata write opens `BEGIN IMMEDIATE`, serializing concurrent writers. Tested with 4 threads racing for limited stock — exactly the right number succeed. |
| Guardrails | Refuses below-cost sales unless explicitly overridden; refuses to settle a nonexistent/zero khata balance; no delete-stock tool exists at all. |
| Real artifacts | ReportLab PDF invoice with a full GST breakup table; python-pptx deck with real line/bar/pie charts from live query data — no screenshots. |
| Memory across sessions | `preferences` table keyed by `chat_id`. `/new` clears only in-memory chat history, never SQLite. |

## Data model

SQLite, WAL mode. `products`, `stock_movements`, `customers`, `khata_transactions`,
`bills`/`bill_items`, `preferences`, `active_bills`, `idempotency_keys`. Survives a restart.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN + an LLM key
python -c "from db.seed import seed; seed()"   # optional sample SKUs
python bot.py
```

`LLM_PROVIDER=gemini` (default) uses Google's free tier — no card, get a key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey). `LLM_PROVIDER=anthropic`
uses Claude and needs a funded account. Switch in `.env`, no code changes.

## Tests

```bash
python -m pytest tests/ -v
```

8 tests exercise the tool layer directly: GST math, oversell guard, idempotent finalize,
khata flow + refusal, below-cost guard, concurrent-race prevention, multi-turn bill edits.
## Deployment

The Telegram bot is deployed on **Railway** and runs continuously using Telegram long polling.

### Railway Deployment

1. Push the project to GitHub.
2. Create a new project on Railway.
3. Connect the GitHub repository.
4. Add the following environment variables in Railway:

```env
LLM_PROVIDER=gemini
TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
GEMINI_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-flash-lite-latest
STORE_DB_PATH=./store.sqlite3
INVOICE_OUTPUT_DIR=./invoices
DECK_OUTPUT_DIR=./decks
Railway installs the dependencies from requirements.txt.
Start the bot with:
python bot.py

## **Production Flow**
Telegram User
      │
      ▼
Telegram Bot API
      │
      ▼
Railway
      │
      ▼
bot.py
      │
      ▼
Agent Control Loop
(observe → reason → tool call → result)
      │
      ├── Inventory Tools
      ├── Billing Tools
      ├── Khata Tools
      ├── Analytics Tools
      ├── Document Tools
      └── Preference Tools
      │
      ▼
SQLite Database

The application uses Telegram long polling, so no separate frontend or webhook server is required. Railway keeps the bot process running and provides the environment variables required by the application.

**## Deployment Notes**
Secrets are stored as Railway environment variables and are not committed to Git.
SQLite persistence is used for products, bills, stock movements, khata transactions, preferences, and idempotency records.
Generated invoices and analysis decks are created by the running application.
The bot can be restarted without losing business data stored in SQLite.

