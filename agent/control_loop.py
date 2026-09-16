"""
The control loop is intentionally small: it is NOT a state machine with a
node per command. It's a single observe -> reason -> act -> observe cycle
that keeps calling the model and feeding back tool results until the model
stops asking for tools. All store logic lives in agent/tools.py; this file
only knows how to talk to an LLM API and dispatch tool_use blocks.

Two backends are supported, chosen by the LLM_PROVIDER env var:

- "anthropic" (default) — Claude via the native Anthropic Messages API.
  Requires ANTHROPIC_API_KEY and billing/credits on the account.
- "gemini" — Google Gemini via its OpenAI-compatible endpoint
  (https://ai.google.dev/gemini-api/docs/openai), which has a genuinely
  free tier (no credit card) at modest rate limits. Requires only
  GEMINI_API_KEY from https://aistudio.google.com/apikey. Tool schemas
  and dispatch logic are identical either way — only the request/response
  shape differs, which is handled entirely inside this file.
"""
from __future__ import annotations

import json
import logging
import os

from agent.tool_schemas import TOOLS
from agent.system_prompt import SYSTEM_PROMPT
from agent.tools import TOOL_REGISTRY

logger = logging.getLogger("agent.control_loop")

PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
MAX_TOOL_HOPS = 12  # safety cap on chained tool calls within a single turn


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool schema -> OpenAI/Gemini function-calling schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


OPENAI_TOOLS = _to_openai_tools(TOOLS)


class StoreAgent:
    """
    One instance per Telegram chat. Holds that chat's rolling message
    history (kept short — the DB, not the transcript, is the source of
    truth for store state) and a registry of tools it can additionally
    dispatch to (e.g. document-generation tools registered by bot.py,
    which need a chat-scoped output path).
    """

    def __init__(self, chat_id: str, extra_tools: dict | None = None, max_history_turns: int = 20):
        self.chat_id = chat_id
        self.provider = PROVIDER
        self.history: list[dict] = []
        self.max_history_turns = max_history_turns
        self.registry = dict(TOOL_REGISTRY)
        if extra_tools:
            self.registry.update(extra_tools)

        if self.provider == "gemini":
            from openai import OpenAI
            self.client = OpenAI(api_key=os.environ["GEMINI_API_KEY"], base_url=GEMINI_BASE_URL)
        else:
            import anthropic
            self.client = anthropic.Anthropic()

    def _trim_history(self):
        # Keep the tail; store state (not chat transcript) is authoritative,
        # so trimming old turns never loses money-affecting facts.
        if len(self.history) > self.max_history_turns:
            self.history = self.history[-self.max_history_turns:]

    def _dispatch(self, name: str, tool_input: dict) -> dict:
        fn = self.registry.get(name)
        if fn is None:
            return {"ok": False, "error": f"Unknown tool '{name}'."}
        # Inject chat_id for tools that are scoped per-owner and where the
        # model might omit it despite the system prompt's instruction.
        if "chat_id" in fn.__code__.co_varnames and "chat_id" not in tool_input:
            tool_input = {**tool_input, "chat_id": self.chat_id}
        try:
            logger.info("tool_call chat=%s name=%s input=%s", self.chat_id, name, tool_input)
            result = fn(**tool_input)
            logger.info("tool_result chat=%s name=%s result=%s", self.chat_id, name, result)
            return result
        except TypeError as e:
            return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
        except Exception as e:  # noqa: BLE001 — surface any tool failure to the model, don't crash the bot
            logger.exception("tool_error chat=%s name=%s", self.chat_id, name)
            return {"ok": False, "error": f"Internal error running {name}: {e}"}

    def handle_message(self, user_text: str) -> str:
        self.history.append({
            "role": "user",
            "content": f"[chat_id={self.chat_id}] {user_text}",
        })
        if self.provider == "gemini":
            reply = self._loop_openai_style()
        else:
            reply = self._loop_anthropic()
        self._trim_history()
        return reply

    # ------------------------------------------------------------------
    # Anthropic backend
    # ------------------------------------------------------------------
    def _loop_anthropic(self) -> str:
        final_text_parts: list[str] = []
        for _hop in range(MAX_TOOL_HOPS):
            response = self.client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.history,
            )

            assistant_content = response.content
            self.history.append({"role": "assistant", "content": assistant_content})

            tool_uses = [b for b in assistant_content if b.type == "tool_use"]
            text_blocks = [b.text for b in assistant_content if b.type == "text"]
            final_text_parts.extend(text_blocks)

            if not tool_uses:
                break

            tool_results = []
            for block in tool_uses:
                result = self._dispatch(block.name, block.input or {})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
            self.history.append({"role": "user", "content": tool_results})

            if response.stop_reason != "tool_use":
                break

        return "\n".join(p for p in final_text_parts if p).strip() or "(done)"

    # ------------------------------------------------------------------
    # Gemini backend (OpenAI-compatible function calling)
    # ------------------------------------------------------------------
    def _loop_openai_style(self) -> str:
        final_text_parts: list[str] = []
        for _hop in range(MAX_TOOL_HOPS):
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *self.history]
            response = self.client.chat.completions.create(
                model=GEMINI_MODEL,
                messages=messages,
                tools=OPENAI_TOOLS,
                tool_choice="auto",
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            # Dump the raw message instead of hand-picking fields: Gemini 3.x
            # ("thinking") models attach an extra_content.google.thought_signature
            # to each tool call that MUST be echoed back verbatim on the next
            # turn, or the API 400s. Reconstructing the dict by hand (as before)
            # silently dropped it.
            assistant_entry = message.model_dump(exclude_none=True)
            assistant_entry["role"] = "assistant"
            self.history.append(assistant_entry)

            if message.content:
                final_text_parts.append(message.content)

            if not tool_calls:
                break

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch(tc.function.name, args)
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

        return "\n".join(p for p in final_text_parts if p).strip() or "(done)"