"""Tokens for the keyword index: lower case, product codes kept whole ("hx-200"), a light plural strip."""

from __future__ import annotations

import re

STOP = {"a", "an", "and", "the", "for", "of", "to", "in", "on", "with", "my", "is", "it", "at", "by", "or", "that",
        "into", "onto", "up", "same", "time"}
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        if t in STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out
