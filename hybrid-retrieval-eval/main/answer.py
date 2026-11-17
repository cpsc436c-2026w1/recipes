"""Answer quality, measured apart from retrieval: the model answers from the top products and cites them by id.

Two checks per answer, both in app code:
- correct:  at least one cited id is a relevant product for the query
- grounded: every cited id was in the retrieved list the model saw (an id from nowhere is made up)

The stub answerer cites the first product it is shown, so offline its "correct" equals hybrid's precision at 1.
"""

from __future__ import annotations

import re

from main.config import answer_max_tokens

SYSTEM = """You help shoppers find products in a store's catalogue.
Answer in one or two sentences using only the products listed in the user message.
Cite each product you recommend by its id in square brackets, for example [P07].
If none fits, say so."""

_ID = re.compile(r"\[(P\d+)\]")


def user_message(query: str, products: list[dict]) -> str:
    lines = [query, "", "Products:"]
    lines += [f"[{p['id']}] {p['name']} ({p['price_cad']:.0f} CAD): {p['description']}" for p in products]
    return "\n".join(lines)


def cited(text: str) -> list[str]:
    return _ID.findall(text)


def judge(text: str, retrieved: list[str], relevant: set[str]) -> dict:
    ids = cited(text)
    return {"cited": ids, "correct": bool(set(ids) & relevant),
            "grounded": bool(ids) and all(i in retrieved for i in ids)}


class FirstProductAnswerer:
    """Offline stand-in for the model: recommends the first product shown."""

    def answer(self, query: str, products: list[dict]) -> str:
        if not products:
            return "None of the products fits."
        return f"Try {products[0]['name']} [{products[0]['id']}]."


class ModelAnswerer:
    def __init__(self, chat, model: str):
        self.chat, self.model = chat, model
        self.spent_cad = 0.0
        self.calls = self.tokens_in = self.tokens_out = 0

    def answer(self, query: str, products: list[dict]) -> str:
        r = self.chat.chat(self.model, SYSTEM, user_message(query, products),
                           max_tokens=answer_max_tokens(self.model))
        self.spent_cad += r.cost_cad
        self.calls += 1
        self.tokens_in += r.tokens_in
        self.tokens_out += r.tokens_out
        return r.text


def evaluate_answers(search, answerer, queries: list[dict], k: int = 5) -> dict:
    rows = []
    for q in queries:
        top = [d for d, _ in search.hybrid(q["query"], k)]
        text = answerer.answer(q["query"], [search.products[i] for i in top])
        rows.append({"id": q["id"], "retrieved": top, "answer": text,
                     "retrieval_hit": bool(set(top) & set(q["relevant"])),
                     **judge(text, top, set(q["relevant"]))})
    n = len(rows)
    return {
        "answer_correct": sum(r["correct"] for r in rows) / n,
        "answer_grounded": sum(r["grounded"] for r in rows) / n,
        # the split that tells you what to fix: hit but wrong answer = prompt or model; miss = retrieval
        "hit_but_wrong": sum(r["retrieval_hit"] and not r["correct"] for r in rows),
        "missed_retrieval": sum(not r["retrieval_hit"] for r in rows),
        "rows": rows,
    }
