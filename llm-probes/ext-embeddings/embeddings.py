"""Extension ext-embeddings: does an embedding model place two similar sentences closer than an unrelated one?

README.md in this folder says what it adds to main/ and why.
"""

import math

from endpoint import failure, row


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def probe_embeddings(ep, model):
    # Decision: a relation (the similar pair is closer than the unrelated pair), not a fixed score, so it holds
    # on any embedding model.
    texts = ["How do I reset my password?", "I forgot my password and cannot log in.", "Parquet stores data by column."]
    status, reply, seconds = ep.request("POST", "/embeddings", {"model": model, "input": texts})
    if status != 200:
        return [row("embeddings", model, "no", seconds, failure(status, reply))]
    vectors = [item["embedding"] for item in sorted(reply["data"], key=lambda item: item["index"])]
    near, far = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
    note = f"{len(vectors[0])} dimensions; similar pair {near:.2f}, unrelated pair {far:.2f}"
    return [row("embeddings", model, "yes" if near > far else "no", seconds, note)]


PROBES = [probe_embeddings]
