"""
Telegram entrypoint for the Supermarket Ops Agent.

- One StoreAgent per chat_id, kept in memory for the process lifetime
  (conversation context only — actual store state lives in SQLite and
  survives a restart).
- Update-level idempotency: Telegram guarantees at-least-once delivery,
  so we record each update_id before processing and skip anything we've
  already seen, on top of the finalize_bill-level idempotency_key.
- Documents (invoice PDF / analysis PPTX) are registered as extra tools
  scoped to the current chat_id so filenames/preferences resolve
  correctly, then sent back to Telegram as file uploads.
"""
from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
load_dotenv()  # reads .env in the current directory into os.environ before anything else runs

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

from db.models import init_db, get_conn
from agent.control_loop import StoreAgent
from documents.invoice_pdf import generate_invoice_pdf
from documents.analysis_pptx import generate_analysis_deck

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

SESSIONS: dict[str, StoreAgent] = {}


def _get_agent(chat_id: str) -> StoreAgent:
    if chat_id not in SESSIONS:
        chat_id_str = str(chat_id)

        def _invoice_tool(bill_id: int):
            return generate_invoice_pdf(bill_id, chat_id=chat_id_str)

        def _deck_tool(start_date: str, end_date: str, title: str | None = None):
            return generate_analysis_deck(start_date, end_date, title)

        SESSIONS[chat_id] = StoreAgent(
            chat_id_str,
            extra_tools={
                "generate_invoice_pdf": _invoice_tool,
                "generate_analysis_deck": _deck_tool,
            },
        )
    return SESSIONS[chat_id]


def _already_processed(update_id: int) -> bool:
    key = f"telegram_update:{update_id}"
    with get_conn(immediate=True) as conn:
        row = conn.execute("SELECT 1 FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
        if row:
            return True
        conn.execute("INSERT INTO idempotency_keys (key) VALUES (?)", (key,))
        return False


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if _already_processed(update.update_id):
        logger.info("Skipping already-processed update %s", update.update_id)
        return

    chat_id = str(update.effective_chat.id)
    agent = _get_agent(chat_id)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    reply = agent.handle_message(update.message.text)

    # If the agent's tool calls produced a document this turn, send it as a file.
    file_path = _extract_last_generated_file(agent)
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f:
            if file_path.endswith(".pdf"):
                await update.message.reply_document(document=f, filename=os.path.basename(file_path))
            elif file_path.endswith(".pptx"):
                await update.message.reply_document(document=f, filename=os.path.basename(file_path))
    await update.message.reply_text(reply)


def _extract_last_generated_file(agent: StoreAgent) -> str | None:
    """Scan the most recent tool_result blocks in this turn's history for a file_path."""
    for msg in reversed(agent.history[-4:]):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                import json
                try:
                    parsed = json.loads(block["content"])
                except Exception:
                    continue
                if isinstance(parsed, dict) and parsed.get("file_path"):
                    return parsed["file_path"]
    return None


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/new clears conversation context only — store state and preferences persist."""
    chat_id = str(update.effective_chat.id)
    SESSIONS.pop(chat_id, None)
    await update.message.reply_text("Fresh chat started. Store data and your saved preferences carry over.")


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    init_db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Supermarket Ops Agent bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
