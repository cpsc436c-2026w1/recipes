# Filters and facets

Read the recipe README first. This extension builds on `main/`.

## What it changes in the main scenario

A filter is a dict such as `{"category": ["audio"], "in_stock": true, "price_cad": {"lte": 100}}`.
`filters.filtered_hybrid` applies it inside both searches:

- on the laptop, as the set of allowed ids that BM25 and the vector search rank (steps 2 and 3);
- on OpenSearch, as `filter` clauses inside the `match` query and inside the `knn` query.

`filters.facets` counts the matching products by category, brand and price band, so the page can show "audio (2),
accessories (2)" next to the results. `filters_run.py` counts them in app code on both backends, from the keyword
matches. On OpenSearch, `opensearch_facets_body` builds a request body with three aggregations that can produce the same
counts. `filters_run.py` does not send that request (`tests/test_extensions.py` checks its shape only).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Where the filter runs (R70, R71, R73) | Inside the search, before ranking | Taking the top 5 first and dropping what fails the filter can leave fewer than 5 results, or lose the one the user wanted, although enough matching products exist | L17; OpenSearch k-NN search with filters (filter inside the `knn` query) |
| Filter fields (R73) | `keyword`, `float` and `boolean` fields in the index mapping, not text | A filter is an exact test. A text field is split into words | OpenSearch mappings (`main/opensearch_store.py`, `index_body`) |
| Facets (R72) | Counts over the keyword matches; price in three bands, under 50, 50 up to but not including 200, and 200 CAD and up | Counts show users how many keyword matches fall in each category, brand and price band | Course choice |
| Embedding results under a filter | Still up to k results, some weak | A vector search always returns neighbours. A filter makes less relevant results visible | L17 |

## Requirements

The form table and the rules are in the [recipe README](../README.md#requirements). The scenarios are in
[`../features/filters.feature`](../features/filters.feature).

- **R70** (optional) Where filters are used, the recipe shall apply a filter inside both searches, before ranking.
- **R71** (optional) Where filters are used, the recipe shall print, for each filtered query, the number of results
  and recall@5 of filtering inside the search and of filtering after the top 5, and the mean recall@5 of each.
- **R72** (optional) Where filters are used, the recipe shall count the keyword matches by category, by brand and by
  three price bands: under 50, 50 to 200, and 200 CAD and up.
- **R73** (optional) Where filters are used on the OpenSearch backend, the recipe shall send the filter as a clause
  inside the `multi_match` query's `bool` and inside the `knn` query.

## Run it

Run the command offline in `hybrid-retrieval-eval`:

```bash
uv run python ext-filters/filters_run.py
```

Against OpenSearch Serverless and the gateway, after the sign-in and the `export` lines of the recipe's
[part 3](../README.md#3-laptop-against-opensearch-serverless-in-your-team-sandbox), and `clean_up.py` the same day:

```bash
uv run python ext-filters/filters_run.py --backend opensearch --embedder gateway
```

Run by us on 2026-11-14 against OpenSearch Serverless and the course LLM gateway, after the main run of part 3
([`../output/filters-opensearch.txt`](../output/filters-opensearch.txt)).

## What you should see

Each run reports how many results each approach returns and its recall@5 for each filtered query. Each excerpt
contains two lines from the run:

The offline stand-in run in `output/filters-stub.txt` uses the stand-in embedder and laptop indexes, so its numbers
come from the stand-in:

```
| two | {'category': ['outdoor']} | 3 | 1 | 1.00 | 0.00 |
mean recall@5: inside 1.00, after 0.86
```

**Real run, 2026-11-14** (`output/filters-opensearch.txt`, OpenSearch Serverless, embeddings from the course LLM gateway):

```
| two | {'category': ['outdoor']} | 3 | 0 | 1.00 | 0.00 |
mean recall@5: inside 1.00, after 0.86
```

Filtering inside never returns fewer results than filtering after, and for "two" with the
outdoor filter only the inside filter finds the two-person tent.

Against OpenSearch with real embeddings the relation held and the two means were the same (inside 1.00, after
0.86). The "results after" column differed in three rows: 0 for "two" (1 offline), 3 for "wireless" (2) and 4 for
"charger" (3), because the real embeddings put other products in the top results before the filter.

## Adapt it

Add your fields to the products, to `index_body` and to `filter_queries.jsonl`. Keep filters on fields with a fixed
set of values (category, team, status) or a range (price, date).
