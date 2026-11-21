# Vector search over the riders' messages, in the trips table

> ⚠️ Run dry runs on your laptop and AWS commands in a sandbox terminal. The scripts are in your clone; DynamoDB is
> a service AWS runs.

Extension of the [dynamodb recipe](../README.md). Riders write short messages about their trips: a lost phone, a
driver at the wrong corner, a fare above the quote. Support staff want the messages that mean the same as a new one,
whatever words they use, and often only for some trips (a fare range, a few pickup zones). The trips are already in
DynamoDB, so the vectors and the search stay there too.

## What it changes in main/

- Each of the 50 trips gets two attributes: `message`, the rider's message, and `message_vector`, its vector.
- `$PREFIX-trips` gets a vector index, `trip-messages`, added with `update-table`. The table and its other index
  stay as `main/` made them, and `clean_up.sh` removes the index with the table.
- Run `main/run.sh` first with `KEEP=1`. The extension does not change it.

Words used here. An **embedding model** turns a text into a **vector** (an embedding): a list of numbers, here 1,024,
such that texts with similar meaning get vectors that are close. A **vector index** keeps the vectors of a table's
items so that `SearchVectors` can return the items nearest to a query vector, the **top K**. **Cosine distance**
measures closeness by the angle between two vectors: 0 for the same direction, up to 2 for opposite ones. An
**inline filter** is an attribute the index keeps next to each vector, so that a search can be limited to the items
that match a condition while the index is searched.

## Steps

1. Make a vector for each message and for the two questions, through the course LLM gateway
   (`qwen3-embedding-0.6b`), when `KB_MCP_TOKEN` and `COURSE_GATEWAY_URL` are set. Without them, use the vectors in
   `vectors.json`, made the same way on 2026-11-21. Print the number of dimensions.
2. Add the vector index `trip-messages` to `$PREFIX-trips`, and wait until it is `ACTIVE`.
3. Write each trip's message and vector: 50 `update-item` calls, each printing its write units and the bytes it
   wrote to the vector index.
4. Search for "I think I left something in the car.": the 5 nearest messages.
5. Search for "Why did I pay more than the price I was quoted?", with no filter.
6. The same search, limited to trips with a fare quote from 20 to 30 picked up at JFK or SoHo.
7. Print the bytes of the run's vector writes and searches, their price, and how to estimate yours.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| index on the trips table (R70) | `update-table` adds the vector index to `$PREFIX-trips`, with the vectors written after it is `ACTIVE` | the trips and their messages are already there, so no second table and no copy to keep in agreement. A vector index can be added to an existing table, one index creation at a time per table; it is first `CREATING` and backfilling, then `ACTIVE`, and backfilling "can take a substantial amount of time even when the base table holds very few items". The run waits up to `WAIT_MINUTES` (default 30) | [Creating and searching vector indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchWorkingWith.html), "Adding a vector index to an existing table" |
| wait until searchable (R71) | poll `describe-table` until the index is `ACTIVE` and not backfilling, then try the first search again on `ValidationException` or `ResourceNotFoundException`, up to 20 times, 15 s apart | the search endpoint can start serving a new index a little after `DescribeTable` reports `ACTIVE`, and answers `ValidationException` until then. Results are eventually consistent: a vector written a moment ago can be missing from a search | same page, "A newly ready index is not immediately searchable"; "Searching with SearchVectors" |
| filters (R74) | `pickup_zone` (String) and `fare_quote` (Number) as inline filters; no vector index partition key | `IN` and `BETWEEN` work on inline filters. A partition key accepts only `=`, so a search with a partition key covers one zone, and a question over two zones would be two searches. A partition key fits a value every search names, such as a team or a tenant. It is not an access boundary: anyone allowed `SearchVectors` on the index can search every value. An index can have up to 18 inline filters. The filter applies during the search, so the search still returns up to 5 matching trips; a filter applied to the results afterwards can return fewer | [SearchConditionExpression reference](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchFilterReference.html); [quotas, vector indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html) |
| embeddings (R72) | `qwen3-embedding-0.6b` through the course LLM gateway with your own course token; the same vectors shipped in `vectors.json` | DynamoDB stores and searches vectors; it does not make them. The query and the stored vectors must come from the same model and have the dimensions of the index (the dimensions are fixed when the index is created, at most 4,096). The shipped file lets the extension run in CloudShell without a token | course constraint (model calls through the course LLM gateway, `KB_MCP_TOKEN`); developer guide, "Generating vector embeddings" |
| distance and projection | cosine distance; projection `ALL`; each search returns `trip_id`, `pickup_zone`, `fare_quote` and `message`, not the vector | the gateway's vectors have length 1 (measured on 2026-11-21), and cosine ignores length. A search does not return the vector unless asked; returning it, or a larger projection, adds bytes. For a large table, project only what the app reads from the results | developer guide, "Generating vector embeddings" and [Storage considerations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchStorage.html) |
| writes (R73) | one `update-item` per trip with `--return-consumed-capacity INDEXES` | `INDEXES` is the level at which a write reports `VectorWriteRequestBytes`. An update keeps the status, driver and fare that `main/` gave the trip; a batch Put would replace the item | [Monitoring vector index capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchMonitoring.html) |
| cost (R76) | the run prints the bytes DynamoDB reported and multiplies them by the ca-central-1 prices, each request billed at 1 KB at least | vector writes and searches are billed in bytes, separately from read and write units. AWS says not to estimate the bill from the number of dimensions: measure the bytes of a sample of your own data | same page, "Per-request metering minimum"; AWS bulk price list, ca-central-1 |
| AWS CLI (R75) | the run stops before it changes anything when the CLI does not know `search-vectors` | the command needs AWS CLI v2 2.36.16 or later. The pip package `awscli` (version 1, 1.46.3 on 2026-11-21) does not have it | developer guide, "Filtering with SearchConditionExpression" |
| ceiling | the table's ceiling of 100 write units per second (`MAX_UNITS`) also bounds the vector writes; a vector index has no ceiling of its own | every vector write is a write to the table. Searches do not have a ceiling: a loop that searches without end bills about 2 KB per search, about 0.004 USD a million (assumed: a search of this size) | [on-demand maximum throughput](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode-max-throughput.html); the vector index parameters of `update-table` have no throughput field |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/vector_search.feature`.

- **R70** (complex) Where `ext-vector-search` is used, when the trips table does not have the vector index, the
  recipe shall add the index `trip-messages` to `$PREFIX-trips` with `update-table`, with `pickup_zone` and
  `fare_quote` as inline filters and the dimensions of the vectors it loads.
- **R71** (complex) Where `ext-vector-search` is used, when the vector index is not `ACTIVE`, the recipe shall wait
  for it before it writes a vector or searches.
- **R72** (complex) Where `ext-vector-search` is used, when no course token is set, the recipe shall load the vectors
  of `ext-vector-search/vectors.json` without a call to the course LLM gateway.
- **R73** (complex) Where `ext-vector-search` is used, when it writes a trip's message and vector, the recipe shall
  ask for the consumed capacity of every index and print the vector write bytes.
- **R74** (complex) Where `ext-vector-search` is used, when it searches with a filter, the recipe shall send
  `fare_quote BETWEEN` and `pickup_zone IN` in the search condition.
- **R75** (complex) Where `ext-vector-search` is used, if the AWS CLI does not know `search-vectors`, then the
  recipe shall stop before it changes the table and name the AWS CLI version it needs.
- **R76** (complex) Where `ext-vector-search` is used, when the searches are done, the recipe shall print the vector
  write and search bytes of the run and their price in USD.

## Run it

Not yet run by us in AWS. The dry run and the scenarios pass offline; the vectors in `vectors.json` were made through
the course LLM gateway on 2026-11-21. From your sandbox terminal, sign in and check that the account is the one you
expect. The recipe README explains what a sandbox terminal is:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
aws --version                                 # AWS CLI v2 2.36.16 or later
```

From your sandbox terminal in `dynamodb`, use your own name in place of `ana`. To make the vectors anew, also set
`KB_MCP_TOKEN` and `COURSE_GATEWAY_URL` (course page); without them the run uses `vectors.json`:

```bash
export PREFIX=recipe-ana AWS_REGION=ca-central-1
KEEP=1 bash main/run.sh                       # first, once: the trips table stays
DRY=1 bash ext-vector-search/run.sh           # prints every command, runs nothing
bash ext-vector-search/run.sh                 # runs the seven steps, then cleans up the whole recipe
```

A run without `KEEP=1` ends with clean-up. Otherwise, run `bash clean_up.sh` in the same terminal with the same
`PREFIX`: deleting `$PREFIX-trips` also deletes its vector index. Cost: see below.

## What you should see

Step 1 prints `dimensions: 1024`. Step 2 prints `"CREATING"`, then the index's states until `ACTIVE`. In step 3,
each trip's line has its write units and `vector_write_bytes`.

The nearest messages below were computed offline from `vectors.json`, by exact cosine distance. DynamoDB's search is
approximate, so its order and scores can differ a little:

| Step | Nearest 5 (trip, zone, fare quote) |
|---|---|
| 4, lost item | t-0047 headphones (Astoria, 29), t-0007 phone (Astoria, 19), t-0032 wallet (Astoria, 14), t-0025 laptop bag (Midtown, 37), t-0038 sunglasses (SoHo, 20) |
| 5, fare question | t-0000 (Midtown, 12), t-0039 (JFK, 21), t-0013 (SoHo, 25), t-0044 (JFK, 26), t-0018 (SoHo, 30) |
| 6, same, fare 20 to 30, JFK or SoHo | t-0039, t-0013, t-0044, t-0018, t-0009 (JFK, 21: a waiting fee) |

These relations hold on every run. Step 6 returns only trips from JFK or SoHo with a fare quote from 20 to 30, and
still returns 5: t-0000 (Midtown, 12) drops out and the next matching trip comes in. The fifth match is about a fee,
not the quote: a search returns the K nearest that match, however far they are, so an app shows the score or
sets a cut-off.

## Cost

Estimated from the price list, not yet measured (`assumed`): about 4,100 vector write bytes per trip (1,024 numbers of
4 bytes, plus the projected attributes), about 0.0001 USD for the 50 writes at 0.572 USD per GB; each search at
least 1 KB at 0.0022 USD per GB; the writes to the table, about 6 to 7 write units per trip for an item with its vector (assumed,
from DynamoDB's item-size rule for numbers; 0.6875 USD per million write units). The run is well under one cent. The 52 texts of step 1 are about 700 tokens on
the course LLM gateway. Step 7 prints the measured bytes and price of your run.

The index is stored and billed per GB-month the same way as table storage, in addition to the table
([on-demand pricing](https://aws.amazon.com/dynamodb/pricing/on-demand/)); the ca-central-1 price list has no line of
its own for it, so it is the table storage price, 0.275 USD per GB-month beyond the free 25 GB-months.

Estimate it for your data:

1. Run this extension's steps on a sample of your own items and searches, with your model, dimensions and projection.
2. Load: items × the `vector_write_bytes` of one write × 0.572 USD per GB, again for each changed vector. Searches: searches × the search bytes × 0.0022 USD per GB, 1 KB at least each.
3. Search bytes grow by a small constant each time the items double, not in proportion; storage is the index size × 0.275 USD per GB-month.
