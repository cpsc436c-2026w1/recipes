"""One fixed path per label. App code owns each path: which data it reads, which model it calls, if any.

| label        | what runs                                             | model call         |
|--------------|-------------------------------------------------------|--------------------|
| lookup       | find the order number in the text, read the order     | none               |
| search       | keyword match over the catalog, small model replies   | small model, once  |
|              | an empty reply moves the request to the hard path      | then large, once   |
| hard         | the whole catalog and the policy, large model replies | large model, once  |
| out_of_scope | stock reply                                           | none               |

A path that cannot do its job (a lookup without an order number) does not guess: it returns `misroute` with the
reason, and app code sends the request to the hard path and writes a line to the misroute log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from main.data import CATALOG, ORDERS, POLICY
from main.model import Reply

STOCK_REPLY = ("I can help with your orders and with our products. For anything else, please contact us at the "
               "store.")

SEARCH_SYSTEM = ("You help customers of an outdoor gear shop. Answer in at most three sentences, using only the "
                 "products listed in the user message, with their prices in CAD.")
HARD_SYSTEM = ("You help customers of an outdoor gear shop. Use only the catalog and the policy below; say so when "
               "they do not answer the question. Answer in at most five sentences, with prices in CAD.\n\n"
               "Catalog (JSON):\n" + json.dumps(CATALOG) + "\n\nPolicy: " + POLICY)

SEARCH_MAX_TOKENS = 200     # assumed: three sentences
HARD_MAX_TOKENS = 600       # assumed: five sentences, and room for a model that reasons before it answers


@dataclass
class PathResult:
    reply: str
    calls: list[Reply] = field(default_factory=list)
    misroute: str = ""                 # non-empty: this path could not handle the request; the reason


def lookup(text: str, models) -> PathResult:
    found = re.search(r"\b(\d{4})\b", text)
    if not found:
        return PathResult("", misroute="lookup without an order number")
    order_id = int(found.group(1))
    order = ORDERS.get(order_id)
    if order is None:
        return PathResult(f"I could not find order {order_id}. Please check the number on your receipt.")
    return PathResult(f"Order {order_id} is {order['status']}: {order['items']} item(s), "
                      f"{order['total_cad']:.2f} CAD.")


def _matches(text: str, k: int = 3) -> list[dict]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    scored = []
    for p in CATALOG:
        vocab = set(re.findall(r"[a-z]+", f"{p['name']} {p['kind']} {p['tags']}".lower()))
        score = len(words & vocab)
        if score:
            scored.append((score, p["id"], p))
    return [p for _, _, p in sorted(scored, key=lambda s: (-s[0], s[1]))[:k]]


def search(text: str, models) -> PathResult:
    hits = _matches(text)
    if not hits:
        return PathResult("", misroute="search found no matching product")
    listing = "\n".join(f"- {p['name']}: {p['price_cad']:.2f} CAD" for p in hits)
    r = models.chat(models.small, SEARCH_SYSTEM, f"Products:\n{listing}\n\nQuestion: {text}", SEARCH_MAX_TOKENS)
    if not r.text.strip():
        # The small model answered with nothing (a model that reasons first can spend the whole cap on it). The
        # call is paid for and stays in the request's cost; the request moves up to the hard path.
        return PathResult("", calls=[r], misroute="search reply was empty")
    return PathResult(r.text, calls=[r])


def hard(text: str, models) -> PathResult:
    r = models.chat(models.large, HARD_SYSTEM, f"Question: {text}", HARD_MAX_TOKENS)
    return PathResult(r.text, calls=[r])


def out_of_scope(text: str, models) -> PathResult:
    return PathResult(STOCK_REPLY)


PATHS = {"lookup": lookup, "search": search, "hard": hard, "out_of_scope": out_of_scope}
