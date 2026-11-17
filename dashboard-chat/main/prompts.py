"""The system prompt and the one tool the model may call.

App code generates the system prompt from the live schema. It reaches the LLM as the system prompt. The question
reaches the LLM as the user message. The rows DuckDB returns reach the LLM as a tool result.
"""

from __future__ import annotations

import hashlib
import json

SYSTEM_TEMPLATE = """You answer questions about a small shop's sales by writing DuckDB SQL.

Tables and columns:
{schema}

Rules:
- To answer, call the tool run_sql with one SELECT query over these tables only.
- The user asking has customer_id '{customer_id}'. "My" or "I" in a question means that customer.
- After the tool result, reply in one or two sentences. Take every number from the tool result; do not compute
  numbers yourself.
- If the question is not about these tables, reply that you can only answer questions about the shop's sales."""

RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run one read-only DuckDB SELECT over the shop's tables and return the rows (at most 50).",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One SELECT statement"}},
            "required": ["sql"],
        },
    },
}


def prompt_version() -> str:
    """A short hash of the system prompt template and the tool definition: it changes when either one changes.

    The answer cache keys on it (ext-answer-cache), so an answer written under old rules is not served under new ones.
    """
    text = SYSTEM_TEMPLATE + json.dumps(RUN_SQL_TOOL, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def system_prompt(schema: dict[str, list[tuple[str, str]]], customer_id: str) -> str:
    lines = [f"- {t}({', '.join(f'{c} {ty}' for c, ty in cols)})" for t, cols in schema.items()]
    return SYSTEM_TEMPLATE.format(schema="\n".join(lines), customer_id=customer_id)
