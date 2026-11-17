"""The router: the small model labels the request; app code turns the reply into one of four labels.

The model answers one narrow question and nothing else: which kind of request is this. It does not choose a model,
a tool or a path; app code does that from the label (main/app.py, README "Decisions": who decides).
"""

from __future__ import annotations

import string
from dataclasses import dataclass

LABELS = ("lookup", "search", "hard", "out_of_scope")

ROUTER_SYSTEM = """You label a customer's message to an outdoor gear shop. Reply with exactly one word.
lookup: asks about one order by its number (status, items, total).
search: asks which products match a kind, a use or a feature, and wants a list.
hard: compares products, plans a purchase under a budget or several conditions, or asks for an explanation.
out_of_scope: is not about the shop's orders or products.
Reply with one of: lookup, search, hard, out_of_scope."""

# A label is one word: a few tokens are enough. If a model spends its tokens on reasoning first, the reply comes back
# empty, the label is unknown and the request goes to the hard path, and the misroute log shows it (README).
ROUTER_MAX_TOKENS = 8


@dataclass
class Route:
    label: str            # one of LABELS
    raw: str              # what the model returned, for the misroute log
    known: bool           # False when the reply was not a label and app code chose "hard"


def parse_label(reply: str) -> Route:
    # Punctuation and markup around the word are not part of the label: "lookup!", "(lookup)", "**lookup**".
    word = reply.strip().lower().strip(string.punctuation + string.whitespace).replace("-", "_").replace(" ", "_")
    if word in LABELS:
        return Route(word, reply, True)
    # Anything unexpected goes to the hard path: a router mistake then costs money, not a wrong answer.
    return Route("hard", reply, False)
