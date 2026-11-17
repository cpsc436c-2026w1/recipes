# Embeddings: is a similar pair closer than an unrelated one?

Extend the [llm-probes recipe](../README.md) with group `embeddings`, in [embeddings.py](embeddings.py).

## What it adds to main/

For each model with `embed` in its name, the group sends one `POST /embeddings` instead of the chat probes. The request sends three sentences to produce embeddings, vectors that represent the sentences:
two about a forgotten password and one about Parquet. One row, `embeddings`: `yes` when the cosine similarity of the
password pair is higher than that of the unrelated pair. The note gives the number of dimensions and both
similarities.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| check (R90) | a relation (similar pair closer than unrelated pair), not a fixed score | scores differ between models; the relation holds on any useful one | retrieval (L17) |
| dimensions (R91) | printed | an index you build must use the same number | L17 |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R90** (optional) Where the group `embeddings` is run, the recipe shall report `embeddings` as `yes` only when
  the cosine similarity of the two similar sentences is higher than that of the unrelated pair.
- **R91** (optional) Where the group `embeddings` is run, the recipe shall give the number of dimensions and both
  similarities in the note.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,embeddings
```
