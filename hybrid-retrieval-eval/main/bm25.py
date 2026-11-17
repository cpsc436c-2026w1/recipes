"""Keyword search: BM25 in pure Python, the local stand-in for an OpenSearch text field.

score(q, d) = sum over query terms t of  idf(t) * tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))
idf(t)      = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))

k1 = 1.2 and b = 0.75: Lucene's BM25 defaults, which OpenSearch text fields use (read in Lucene's
BM25Similarity.java, `this(1.2f, 0.75f, ...)`, 2026-11-13; README, "Decisions"). The idf above is Lucene's too. A term that appears in no document does not add to any score: that is why a typo or a word the catalogue does not
use ("couch" for "sofa") misses here, and why the embedding index is there.
"""

from __future__ import annotations

import math
from collections import Counter

from main.text import tokens

K1 = 1.2
B = 0.75


class BM25Index:
    def __init__(self, docs: dict[str, str]):
        self.ids = list(docs)
        self.tf = {i: Counter(tokens(t)) for i, t in docs.items()}
        self.len = {i: sum(c.values()) for i, c in self.tf.items()}
        self.avgdl = sum(self.len.values()) / max(len(self.len), 1)
        df = Counter(t for c in self.tf.values() for t in c)
        n = len(self.ids)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = 10, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """Top k (id, score), best first; only documents with a score above zero. `allowed` limits the candidates
        before ranking (ext-filters)."""
        q = tokens(query)
        scores = {}
        for i in self.ids:
            if allowed is not None and i not in allowed:
                continue
            tf, dl = self.tf[i], self.len[i]
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / self.avgdl))
            if s > 0:
                scores[i] = s
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]
