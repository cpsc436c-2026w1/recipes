"""Embedding search: every product vector compared with the query vector (exact nearest neighbours).

30 products need no approximate index; the cosine of unit vectors is their dot product. OpenSearch's knn_vector field
does the same job with an approximate index (HNSW) once the catalogue is too large to compare every row.
A vector index always returns k neighbours, even for nonsense: there is no "no match" as in BM25.
"""

from __future__ import annotations

import numpy as np


class VectorIndex:
    def __init__(self, ids: list[str], vectors: np.ndarray):
        self.ids = ids
        self.vectors = vectors

    def search(self, query_vector: np.ndarray, k: int = 10, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        scores = self.vectors @ query_vector
        order = np.argsort(-scores, kind="stable")
        out = []
        for i in order:
            if allowed is not None and self.ids[i] not in allowed:
                continue
            out.append((self.ids[i], float(scores[i])))
            if len(out) == k:
                break
        return out
