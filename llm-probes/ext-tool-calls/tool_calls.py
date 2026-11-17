"""Extension ext-tool-calls: does the model call a tool, and does it use the tool result in its reply?

README.md in this folder says what it adds to main/ and why.
"""

import json

from endpoint import failure, row, text_of

# Decision: one read-only tool with one string argument, the shape of the course's run_sql tool (L15).
SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run one read-only SQL query against the trips database and return the rows.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["query"],
        },
    },
}


def probe_tools(ep, model):
    """A tool call, then the tool result sent back for the final reply."""
    messages = [{"role": "user", "content": "How many rows does the trips table have? Use the tool."}]
    body = {"model": model, "messages": messages, "tools": [SQL_TOOL], "max_tokens": 400, "temperature": 0}
    status, reply, seconds = ep.chat(body)
    if status != 200:
        return [row("tool call", model, "no", seconds, failure(status, reply))]
    message = reply["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    try:
        arguments = json.loads(calls[0]["function"]["arguments"])
        query = arguments["query"]
    except (IndexError, KeyError, TypeError, ValueError):
        return [row("tool call", model, "no", seconds, f"no usable tool call; text was {text_of(reply)[:60]!r}")]
    rows = [row("tool call", model, "yes", seconds, f"run_sql({query[:60]!r})")]

    # Decision: the tool result is a made-up number (48213) that the model cannot guess, so using it shows.
    messages += [message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps({"rows": [[48213]]})}]
    status, reply, seconds = ep.chat(body | {"messages": messages})
    if status != 200:
        return rows + [row("tool result used", model, "no", seconds, failure(status, reply))]
    text = text_of(reply)
    used = "48213" in text.replace(",", "").replace(" ", "")
    return rows + [row("tool result used", model, "yes" if used else "no", seconds, f"replied {text[:60]!r}")]


PROBES = [probe_tools]
