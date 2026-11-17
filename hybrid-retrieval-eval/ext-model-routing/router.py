"""Cheap model first: a small model labels each request, app code picks the model that answers it.

The label is the router's only job, so its reply is capped at a few tokens. App code, not the model, maps the
label to a model: "simple" goes to the cheaper model; "hard" and anything unexpected go to the stronger one, so a
router mistake costs money rather than a wrong answer to a hard request (ext-model-routing/README.md).
"""

from __future__ import annotations

from main.answer import SYSTEM as ANSWER_SYSTEM
from main.answer import judge, user_message
from main.config import CHEAP_MODEL, STRONG_MODEL, answer_max_tokens

ROUTER_MODEL = CHEAP_MODEL
ROUTER_SYSTEM = """Label the shopper's request with one word.
simple: it asks about one named product (its price, whether it is in stock, whether the store sells it).
hard: it compares products, sets a budget, or has several conditions.
Reply with exactly one word: simple or hard."""

ROUTE = {"simple": CHEAP_MODEL, "hard": STRONG_MODEL}


def label_of(reply_text: str) -> str:
    word = reply_text.strip().lower().strip(".")
    return word if word in ROUTE else "hard"


def route(router_chat, text: str) -> tuple[str, str, float]:
    """Returns (label, model, router cost in CAD)."""
    r = router_chat.chat(ROUTER_MODEL, ROUTER_SYSTEM, text, max_tokens=8)  # 8: room for one word (README, Decisions)
    label = label_of(r.text)
    return label, ROUTE[label], r.cost_cad


def run_policy(policy: str, requests: list[dict], search, router_chat, answer_chat, k: int = 5) -> dict:
    """policy: "all-cheap", "all-strong" or "routed". Returns accuracy, cost, and one row per request."""
    rows = []
    for req in requests:
        router_cost, label = 0.0, None
        if policy == "routed":
            label, model, router_cost = route(router_chat, req["text"])
        else:
            model = CHEAP_MODEL if policy == "all-cheap" else STRONG_MODEL
        top = [i for i, _ in search.hybrid(req["text"], k)]
        # Decision (reply cap per model, main/config.py): 150 tokens for the cheaper model, 600 for the stronger one.
        r = answer_chat.chat(model, ANSWER_SYSTEM, user_message(req["text"], [search.products[i] for i in top]),
                             max_tokens=answer_max_tokens(model))
        # Decision (an empty reply is an error, not a wrong answer): a reply with no text says nothing about what
        # the model knows. It happens when a model that reasons first spends max_tokens on the reasoning
        # (finish_reason "length"). Such a row is reported and left out of the accuracy; its tokens are still paid.
        if r.text.strip():
            correct, error = judge(r.text, top, set(req["relevant"]))["correct"], None
        else:
            correct, error = None, f"empty reply, finish_reason {r.finish_reason or 'not given'}"
        rows.append({"id": req["id"], "gold": req["label"], "label": label, "model": model,
                     "correct": correct, "error": error, "cost_cad": r.cost_cad + router_cost,
                     "router_cost_cad": router_cost})
    n = len(rows)
    routed = [r for r in rows if r["label"] is not None]
    scored = [r for r in rows if r["error"] is None]
    return {
        "policy": policy,
        "accuracy": (sum(r["correct"] for r in scored) / len(scored)) if scored else None,
        "scored": len(scored),
        "errors": [{"id": r["id"], "model": r["model"], "error": r["error"]} for r in rows if r["error"]],
        "cost_cad": sum(r["cost_cad"] for r in rows),
        "router_cost_cad": sum(r["router_cost_cad"] for r in rows),
        "strong_share": sum(r["model"] == STRONG_MODEL for r in rows) / n,
        "router_agrees": (sum(r["label"] == r["gold"] for r in routed) / len(routed)) if routed else None,
        "rows": rows,
    }
