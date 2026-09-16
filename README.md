# Supermarket Ops Agent

An agent that runs a small Indian kirana store end-to-end from Telegram — receiving stock,
cutting GST-correct bills, running khata (credit), closing the day, and generating invoices
and analysis decks — with no admin panel and no command router. The chat is the product.

**Telegram bot:** `@My Kirana Store` *(fill in after deploying — see below)*

## Harness: hand-rolled control loop on the Anthropic Messages API

I built a small, explicit `observe → reason → act (tool call) → feed result back → continue`
loop directly on `anthropic.Anthropic().messages.create(...)` (see `agent/control_loop.py`)
rather than pulling in a heavier graph-based framework. Reasons:

- The task explicitly rules out a node-per-command state machine. A loop that just keeps
  calling the model with the growing tool-result history *is* the Claude Agent SDK's core
  pattern, at a scale (22 tools, single-agent, no subagent handoff needed) where a thin
  wrapper is more debuggable than a framework — every hop is one `messages.create` call I can
  log and reason about.
- All business logic (GST math, oversell guard, idempotency, khata rules) lives in
  `agent/tools.py` and the SQL in `db/models.py` — never in the system prompt. The loop's only
  job is dispatch: take a `tool_use` block, call the matching Python function, hand the JSON
  result back. This is what "the model orchestrates, the tools own correctness" concretely
  means, and it's independent of which harness sits above the dispatch loop.

## Skill / tool surface

Five skills, twenty-two thin tools (`agent/tool_schemas.py`), each doing one thing:

- **Inventory** — `add_product`, `receive_stock`, `search_products`, `get_stock`, `list_low_stock`
- **Billing** — `start_bill`, `add_bill_item`, `remove_bill_item`, `update_bill_item_qty`,
  `get_bill_draft`, `cancel_bill`, `finalize_bill`
- **Khata** — `add_khata_charge`, `record_khata_payment`, `get_khata_balance`, `list_khata_customers`
- **Analytics** — `get_daily_close`, `get_sales_summary`, `get_top_items`, `get_stock_health`
- **Documents** — `generate_invoice_pdf`, `generate_analysis_deck`
- **Preferences** — `set_preference`, `get_preference`, `get_all_preferences`

No tool does "handle the billing message" — the model composes `start_bill` →
`add_bill_item` × N → (edits via `update_bill_item_qty` / `remove_bill_item`) →
`get_bill_draft` → `finalize_bill` itself, across as many turns as the owner takes to
finish reading off items. `search_products` exists specifically so the model can check
whether "add atta" is genuinely ambiguous before asking a clarifying question, instead of
guessing or always asking.

## How each hard part is solved

| Hard part | Where | How |
|---|---|---|
| **Grounding** | every tool | Prices/GST/stock only ever come from SQLite via a tool call; nothing is hardcoded in the prompt. |
| **Oversell guard** | `db/models.py`, `tools.finalize_bill` | Stock decrement is `UPDATE products SET qty = qty - ? WHERE sku_id = ? AND qty >= ?`; `cursor.rowcount == 0` means refused. Can't be bypassed by the model — it's SQL, not a prompt rule. Also checked (softly) at `add_bill_item` time so the owner gets fast feedback. |
| **GST correctness** | `tools._gst_split`, `documents/invoice_pdf.py` | Each line stores its own HSN + slab; CGST/SGST computed as a 50/50 intra-state split, rounded to paise, summed for the bill total. The PDF invoice shows the full per-line breakup. |
| **Multi-turn bills** | `bills` table, `active_bills` table | A bill is a `draft` row that accumulates `bill_items` across messages; `active_bills` maps `chat_id → bill_id` so the model doesn't have to keep a numeric id in its head. Stock is untouched until `finalize_bill`. |
| **Idempotency** | `idempotency_keys` table | Two layers: (1) `bot.py` records every Telegram `update_id` before processing and skips replays; (2) `finalize_bill` requires a caller-supplied `idempotency_key` and stores its JSON result the first time — a retry returns the cached result instead of re-running side effects. Tested directly in `tests/test_tools.py::test_finalize_is_idempotent`. |
| **Concurrency** | `db/models.get_conn(immediate=True)` | Every write path that touches stock/khata opens `BEGIN IMMEDIATE`, so SQLite serializes concurrent writers instead of interleaving them. `tests/test_tools.py::test_concurrent_sales_do_not_oversell` fires 4 threads each trying to sell 5 units against 10 in stock and asserts exactly 2 succeed and stock lands at exactly 0. |
| **Guardrails** | `tools.finalize_bill`, `tools.record_khata_payment` | Refuses to finalize below cost unless `allow_below_cost=true` is explicitly passed (the model is instructed to only do this on explicit owner confirmation); refuses to settle a khata account that doesn't exist or has no balance; there is no `delete_stock`/`delete_product` tool at all. |
| **Real artifacts** | `documents/invoice_pdf.py`, `documents/analysis_pptx.py` | ReportLab PDF with a real HSN/GST-breakup table (only on `finalized` bills); python-pptx deck with an actual line chart (daily sales), bar charts (top items, stock health) and a pie chart (payment mode split) built from live query results, not screenshots. |
| **Memory across sessions** | `preferences` table, keyed by `chat_id` | `/new` only clears `StoreAgent.history` in `bot.py` — it never touches SQLite. The system prompt instructs the model to call `get_all_preferences` at the start of a session and apply defaults (payment mode, preferred brand, shop name/GSTIN) without the owner repeating them. |

## Data model

SQLite (`db/models.py`), WAL mode. Core tables: `products`, `stock_movements` (audit trail),
`customers`, `khata_transactions`, `bills` / `bill_items`, `preferences`, `active_bills`
(per-chat draft pointer), `idempotency_keys`. Survives a restart — it's a file on disk, not
in-memory state.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and an LLM key — see below
python -c "from db.seed import seed; seed()"   # optional: load sample kirana SKUs
python bot.py
```

Get a `TELEGRAM_BOT_TOKEN` from **@BotFather** on Telegram (`/newbot`). Deploy anywhere that
can run a long-lived Python process (Railway, Render, a small VPS, `screen`/`tmux` on any box);
`app.run_polling()` needs no public URL or webhook.

### Choosing an LLM backend

`agent/control_loop.py` supports two interchangeable backends, picked by `LLM_PROVIDER` in
`.env` — the tool schemas and all business logic are identical either way, only the API call
shape differs:

- **`gemini`** (default in `.env.example`) — Google Gemini via its OpenAI-compatible endpoint.
  Free, no credit card: get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  and set `GEMINI_API_KEY`. Rate-limited (roughly 10 req/min, 250/day on `gemini-2.5-flash` as of
  writing), which is plenty for demoing this project but not for production load.
- **`anthropic`** — Claude via the native Messages API. Requires `ANTHROPIC_API_KEY` and a funded
  account at [console.anthropic.com](https://console.anthropic.com) (Settings → Billing).

Switch by changing `LLM_PROVIDER` in `.env` — no code changes needed.

## Tests

```bash
python -m pytest tests/ -v
```

`tests/test_tools.py` exercises the tool layer directly (no API key needed): GST math,
oversell guard, idempotent finalize, khata flow + refusal, below-cost guard, concurrent
oversell prevention, and multi-turn bill edits. All 8 pass.

## What's stubbed / left for stretch

Branded invoice templates, scheduled weekly decks, reorder-from-velocity suggestions,
FEFO/expiry batch tracking, voice-note orders, Hindi/Tamil, and barcode/photo lookup are not
implemented — the brief marks these as stretch goals and the 5-day scope went to the core
control loop, GST/khata correctness, and the two document generators instead.
