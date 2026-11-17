"""Offline stand-ins for the task model and the reflection model, so the whole loop runs without a key.

Decision (README, Decisions: "Offline stub"): the stub task model writes fixed SQL per question
(main/stub_replies.yml) and has habits that the system prompt can stop, the way a real model's failures
change with its instructions. The stub reflection model reads GEPA's feedback and adds one rule per failure
type it sees. Neither is a language model: they show that the loop, the metric and the split discipline work,
not how much a real prompt improves. The measured numbers of a real run are in the L15 demo (README).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import yaml

REPLIES = {r["question"]: r for r in yaml.safe_load((Path(__file__).parent / "stub_replies.yml").read_text())}

# Each habit, and the words in the system prompt that stop it. The seed instructions contain none of these.
STOPPED_BY = {
    "chatty": "no text after it",
    "share": "0 to 100",
    "seconds": "divide by 60",
}

# Which habits each stub model has. "stub-small" stands for the cheaper task model, "stub-large" for the larger.
HABITS = {
    "stub-small": {"chatty", "share", "seconds"},
    "stub-large": {"share"},
}

# The rule the stub reflection model writes for each failure type it finds in the feedback.
RULES = {
    "FAILURE (SQL error)": "Return only the SQL: one SELECT statement, with no semicolon and no text after it.",
    "a share from 0 to 1 where a percentage was asked":
        "A question that asks for a percentage wants a number on the 0 to 100 scale: multiply the share by 100.0.",
    "seconds where minutes were asked":
        "A wait asked in minutes: take epoch(ready_at - placed_at), which is in seconds, and divide by 60.0.",
}
# The rules must contain the words that stop the habits; checked here so an edit cannot break the stub silently.
assert "no text after it" in RULES["FAILURE (SQL error)"]
assert "0 to 100" in RULES["a share from 0 to 1 where a percentage was asked"]
assert "divide by 60" in RULES["seconds where minutes were asked"]


def task_reply(model: str, system: str, user: str) -> str:
    """The SQL the stub task model writes for the question in `user`, given the instructions in `system`."""
    habits = {h for h in HABITS[model] if STOPPED_BY[h] not in _norm(system)}
    reply = next((r for q, r in REPLIES.items() if q in user), None)
    if reply is None:
        return "SELECT NULL"
    sql = reply["sql"]
    if "share" in habits and "share_sql" in reply:
        sql = reply["share_sql"]
    if "seconds" in habits and "seconds_sql" in reply:
        sql = reply["seconds_sql"]
    if "chatty" in habits and reply.get("chatty"):
        sql += "\nThis query answers the question."
    return sql


def reflect_reply(prompt: str) -> str:
    """A new instruction text: the current one plus a rule for every failure type in the feedback.

    Two prompt shapes: DSPy's (ChatAdapter field markers, main/) and the gepa library's (``` blocks,
    ext-gepa-direct/). The reply comes back in the same shape.
    """
    m = re.search(r"\[\[ ## current_instruction ## \]\]\n(.*?)\n\n\[\[ ## ", prompt, flags=re.S)
    dspy_shape = m is not None
    if not dspy_shape:
        m = re.search(r"```\n?(.*?)```", prompt, flags=re.S)
    current = m.group(1).strip() if m else ""
    added = [rule for marker, rule in RULES.items() if marker in prompt and rule not in current]
    new = "\n".join([current, *added])
    if dspy_shape:
        return f"[[ ## new_instruction ## ]]\n{new}\n\n[[ ## completed ## ]]"
    return f"```\n{new}\n```"


def tokens(text: str) -> int:
    """A rough token count for the stub's usage numbers: about 4 characters a token (assumed)."""
    return math.ceil(len(text) / 4)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())
