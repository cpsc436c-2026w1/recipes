"""Reciprocal rank fusion: each list votes 1 / (k + rank) for each document it holds; the votes are summed.

Ranks start at 1. Only ranks are used, never the scores: a BM25 score (any positive number) and a cosine (at most 1)
are on different scales, and adding them would let one list drown the other. k = 60 is the typical value AI Engineering
ch. 6 gives for the method of Cormack, Clarke and Buettcher (SIGIR 2009), and the one L17 uses; a larger k flattens
the difference between rank 1 and rank 10.
"""

from __future__ import annotations

RRF_K = 60


def rrf(lists: list[list[tuple[str, float]]], k: int = RRF_K, top: int = 10) -> list[tuple[str, float]]:
    votes: dict[str, float] = {}
    for ranked in lists:
        for rank, (doc_id, _score) in enumerate(ranked, start=1):
            votes[doc_id] = votes.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(votes.items(), key=lambda x: (-x[1], x[0]))[:top]
