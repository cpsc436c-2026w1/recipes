"""The keyword index: settings, mappings and the version record, and an offline check of all three.

The index is created by main/infra.py before the indexing function writes to it, so the mappings are ours and not
guessed by OpenSearch from the first document (dynamic mapping). `dynamic: false` keeps every attribute in `_source` but
indexes only the fields named here: a new DynamoDB attribute cannot add a field to the index on its own.
"""

from __future__ import annotations

import re

from main.config import Settings

# The same field names as recipes/hybrid-retrieval-eval main/opensearch_store.py index_body, so that recipe's
# keyword_query() runs on this index unchanged.
PROPERTIES = {
    "name": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
    "description": {"type": "text"},
    "category": {"type": "keyword"},
    "brand": {"type": "keyword"},
    "price_cad": {"type": "float"},
    "in_stock": {"type": "boolean"},
}
TEXT_FIELDS = [f for f, m in PROPERTIES.items() if m["type"] == "text"]
KNOWN_TYPES = {"text", "keyword", "float", "double", "integer", "long", "boolean", "date", "knn_vector"}


def index_body(s: Settings) -> dict:
    return {
        "mappings": {
            "dynamic": False,
            # The version record travels with the index: GET <index>/_mapping returns it (README, "Decisions").
            "_meta": {"index_version": s.index_version, "source_table": s.table, "writer": s.function,
                      "fields": sorted(PROPERTIES)},
            "properties": PROPERTIES,
        },
    }


def validate_index(name: str, body: dict) -> list[str]:
    """Offline checks a cluster would otherwise make at create time, plus our own rules. Empty list = valid."""
    errors = []
    m = body.get("mappings", {})
    if m.get("dynamic") not in (False, "false", "strict"):
        errors.append("mappings.dynamic must be false or strict: the index takes only the fields we name")
    meta = m.get("_meta", {})
    v = meta.get("index_version")
    if not isinstance(v, int) or v < 1:
        errors.append("_meta.index_version must be a whole number from 1")
    elif not re.fullmatch(rf"[a-z0-9][a-z0-9_-]*-v{v}", name):
        errors.append(f"index name {name!r} must end in -v{v} (the version in _meta)")
    for field, spec in m.get("properties", {}).items():
        if spec.get("type") not in KNOWN_TYPES:
            errors.append(f"{field}: unknown type {spec.get('type')!r}")
        if spec.get("type") == "knn_vector" and not isinstance(spec.get("dimension"), int):
            errors.append(f"{field}: knn_vector needs a whole-number dimension")
        for sub, sub_spec in spec.get("fields", {}).items():
            if sub_spec.get("type") not in KNOWN_TYPES:
                errors.append(f"{field}.{sub}: unknown type {sub_spec.get('type')!r}")
    if set(meta.get("fields", [])) != set(m.get("properties", {})):
        errors.append("_meta.fields must list exactly the mapped fields")
    return errors


def keyword_query(text: str, k: int = 5, filters: list | None = None) -> dict:
    """A keyword query ranked by BM25 (OpenSearch's default scoring): a `match` on each text and keyword field, the
    name counted twice. For hybrid ranking with embeddings, see recipes/hybrid-retrieval-eval."""
    match = {"multi_match": {"query": text, "fields": ["name^2", "description", "category", "brand"]}}
    if filters:
        match = {"bool": {"must": [match], "filter": filters}}
    return {"size": k, "query": match}
