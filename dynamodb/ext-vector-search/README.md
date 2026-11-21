# Vector search over the riders' messages, in the trips table

> ⚠️ Run dry runs on your laptop and AWS commands in a sandbox terminal. The scripts are in your clone; DynamoDB is
> a service AWS runs.

`bash ext-vector-search/run.sh` ends by deleting the whole recipe (`clean_up.sh`), its tables and the vector index
with them. With `KEEP=1` it keeps everything, and you run `bash clean_up.sh` yourself when you are done.

Extension of the [dynamodb recipe](../README.md). Riders write short messages about their trips: a lost phone, a
driver at the wrong corner, a fare above the quote. Support staff want the messages that mean the same as a new one,
whatever words they use, and often only for some trips (a fare range, a few pickup zones). The trips are already in
DynamoDB, so the vectors and the search stay there too.

## What it changes in main/

- Run `main/run.sh` first with `KEEP=1`. The extension does not change it.
- Each of the 50 trips gets two attributes: `message`, the rider's message, and `message_vector`, its vector.
- `$PREFIX-trips` gets a vector index, `trip-messages`, added with `update-table`. The table and its other index
  stay as `main/` made them, and `clean_up.sh` removes the index with the table.

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
| filters (R74) | `pickup_zone` (String) and `fare_quote` (Number) as inline filters; no vector index partition key | a search can filter only on the attributes the index declares as `INLINE_FILTER` in its `SearchSchema` (and in `AttributeDefinitions`), at most 18, chosen when the index is created: decide them before you build it. On them, `=`, `<>` and `IN` work on String, Number and Binary attributes, `<`, `<=`, `>`, `>=` and `BETWEEN` on Number attributes only, with `attribute_exists`, `attribute_not_exists`, `AND`, `OR` and `NOT`. A partition key accepts only `=`, so a search with a partition key covers one zone, and a question over two zones would be two searches. A partition key fits a value every search names, such as a team or a tenant. It is not an access boundary: anyone allowed `SearchVectors` on the index can search every value. The filter applies during the search, so the search still returns up to 5 matching trips; a filter applied to the results afterwards can return fewer | [SearchConditionExpression reference](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchFilterReference.html); [quotas, vector indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html) |
| embeddings (R72) | `qwen3-embedding-0.6b` through the course LLM gateway with your own course token; the same vectors shipped in `vectors.json` | DynamoDB stores and searches vectors; it does not make them. The query and the stored vectors must come from the same model and have the dimensions of the index (the dimensions are fixed when the index is created, at most 4,096). The shipped file lets the extension run in CloudShell without a token | course constraint (model calls through the course LLM gateway, `KB_MCP_TOKEN`); developer guide, "Generating vector embeddings" |
| distance and projection | cosine distance; projection `ALL`; each search returns `trip_id`, `pickup_zone`, `fare_quote` and `message`, not the vector | the gateway's vectors have length 1 (measured on 2026-11-21), and cosine ignores length. A search does not return the vector unless asked; returning it, or a larger projection, adds bytes. For a large table, project only what the app reads from the results | developer guide, "Generating vector embeddings" and [Storage considerations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchStorage.html) |
| writes (R73) | one `update-item` per trip with `--return-consumed-capacity INDEXES` | `INDEXES` is the level at which a write reports `VectorWriteRequestBytes`. An update keeps the status, driver and fare that `main/` gave the trip; a batch Put would replace the item | [Monitoring vector index capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchMonitoring.html) |
| cost (R76) | the run prints the bytes DynamoDB reported and multiplies them by the ca-central-1 prices, each request billed at 1 KB at least, with 1 GB = 2^30 bytes | vector writes and searches are billed in bytes, separately from read and write units. AWS says not to estimate the bill from the number of dimensions: measure the bytes of a sample of your own data. "All size measurements in DynamoDB" are binary, so a GB is 2^30 bytes | same page, "Per-request metering minimum"; [quotas](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ServiceQuotas.html); AWS bulk price list, ca-central-1 |
| AWS CLI (R75) | the run stops before it changes anything when the CLI does not know `search-vectors` | the command needs AWS CLI v2 2.36.16 or later. The pip package `awscli` (version 1, 1.46.3 on 2026-11-21) does not have it | developer guide, "Filtering with SearchConditionExpression" |
| throughput | the table's maximum of 100 write units per second (`MAX_UNITS`) also bounds the vector writes; the vector index does not take a throughput setting of its own | every vector write is a write to the table. Each search is billed: a loop that searches without end bills about 30 KB per search, about 0.06 USD a million (30,208 bytes per search measured on an index of 50 items, 2026-11-21) | [on-demand maximum throughput](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode-max-throughput.html); the vector index parameters of `update-table` do not include a throughput field |

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

Run by us on 2026-11-21 in a course sandbox, with a course staff role (`../output/ext-vector-search.txt`); a run with
the student role is still to be done (not yet run with the student role). The extension calls `dynamodb:UpdateTable`,
`dynamodb:DescribeTable`, `dynamodb:UpdateItem` and `dynamodb:SearchVectors`; `main/` adds `dynamodb:CreateTable` and
`dynamodb:BatchWriteItem`, and the clean-up `dynamodb:DeleteTable` and `dynamodb:ListTables` (the recipe README,
[AWS permissions](../README.md#aws-permissions), has the full list): check that your team's role allows them before
you run. The vectors in `vectors.json` were made through the course LLM gateway the same
day. From your sandbox terminal, sign in and check that the account is the one you expect. The recipe README explains
what a sandbox terminal is:

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

Captured: [../output/ext-vector-search.txt](../output/ext-vector-search.txt) (2026-11-21, run with `KEEP=1`). Step 1
prints `dimensions: 1024`. In step 2 the index was `CREATING`, then backfilling from 33 s, and `ACTIVE` after 514 s,
for a table of 50 small items. The first search succeeded at once. In step 3 each write used 5.0 write units and
4,203 to 4,263 vector write bytes.

| Step | Nearest 5 (trip, zone, fare quote), cosine distance | Search bytes |
|---|---|---|
| 4, lost item | t-0047 headphones (Astoria, 29) 0.265, t-0007 phone (Astoria, 19), t-0032 wallet (Astoria, 14), t-0025 laptop bag (Midtown, 37), t-0038 sunglasses (SoHo, 20) 0.298 | 30,197 |
| 5, fare question | t-0000 (Midtown, 12) 0.278, t-0039 (JFK, 21), t-0013 (SoHo, 25), t-0044 (JFK, 26), t-0018 (SoHo, 30) 0.335 | 30,213 |
| 6, same, fare 20 to 30, JFK or SoHo | t-0039 0.299, t-0013, t-0044, t-0018, t-0009 (JFK, 21: a waiting fee) 0.436 | 30,213 |

These relations held in our run. A vector written a moment ago can be missing from a search: if a step shows fewer
or other trips, run the search again. Step 6 returns only trips from JFK or SoHo with a fare quote from 20 to 30, and
still returns 5: t-0000 (Midtown, 12) drops out and the next matching trip comes in. The fifth match is about a fee,
not the quote: a search returns the K nearest that match, however far they are, so an app shows the score or
sets a cut-off. The filter did not change the search bytes. The index's item count and size printed 0 in step 7:
DynamoDB updates them from time to time, not after every write.

## Cost

Measured on 2026-11-21 (step 7), ca-central-1 prices from the AWS bulk price list, 1 GB = 2^30 bytes:

| Request | Bytes | Price | USD |
|---|---|---|---|
| 50 vector writes | 211,387 (4,228 per write) | 0.572 per GB | 0.00011 |
| 3 searches | 90,623 (30,208 per search) | 0.0022 per GB | 0.0000002 |
| 50 table writes for the messages and vectors | 250 write units (5 per write) | 0.6875 per million | 0.00017 |

The run is well under one cent. The table writes cost more than the vector writes. The 52 texts of step 1 are 694
tokens on the course LLM gateway. A search's bytes are what DynamoDB examined and returned, not the 4 KB query vector:
here about 7 times the size of one stored vector.

The captured run printed its step 7 with a GB of 10^9 bytes: 0.0001209 USD for the writes and 1.994e-07 USD for the
searches. With 1 GB = 2^30 bytes, as DynamoDB measures sizes, the same bytes give 0.0001126 and 1.857e-07;
`vectors.py` now uses 2^30.

Index storage: "A vector index consumes storage separately from its base table" ([storage
considerations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchStorage.html)), and the
[on-demand pricing page](https://aws.amazon.com/dynamodb/pricing/on-demand/) says "You are charged for the data stored
in the vector index per GB-month", "in addition to the standard charges for the underlying DynamoDB table". The
ca-central-1 price list does not have a line of its own for it, and the pricing page's example bills index storage at
the us-east-1 table storage price (0.25 USD per GB-month), so we assume the ca-central-1 table storage price, 0.275 USD
per GB-month. The free tier lists "25 GB of data storage" without saying whether index storage counts: check the
pricing page, and count none until it says so.

Estimate it for your data. Run these steps on a sample of your own items, with your model, dimensions, projection and
TopK; step 7 prints your bytes and write units. Then:

| Part | Rule | Example: 100,000 items like ours, 1 million searches |
|---|---|---|
| table writes | items × your write units per write × 0.6875 USD per million | 100,000 × 5 → 0.34 USD |
| vector writes | items × your bytes per write × 0.572 USD per GB | 100,000 × 4,228 bytes → 0.23 USD |
| searches | searches × your bytes per search × 0.0022 USD per GB | 1 million × 30,208 bytes → 0.06 USD |
| storage | index size and table size (`IndexSizeBytes`, `TableSizeBytes`) × 0.275 USD per GB-month each (index rate assumed, above) | not measured: the index size printed 0 |
| embeddings | tokens of all your texts and questions × the course LLM gateway price (CAD, course page) | 52 texts were 694 tokens |

The examples are an extrapolation from the recipe's 50-item index, not a bill. Table and vector writes are billed
again for each changed item or vector. Search bytes were measured on 50 items: AWS says the logarithmic growth of a
search's bytes with the number of items "is not a formula for estimating your bill" ([monitoring vector index
capacity](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/VectorSearchMonitoring.html)), so measure
again with an index of your own size, your TopK, filters and projection. The prices are for the Standard table class;
for vector search, the pricing page says "the Standard-IA tier prices on the same per-byte model at 40% of Standard on
storage and 125% on requests", and the table's own Standard-IA prices are on the same page.

## Clean up

The captured run used `KEEP=1`; the clean-up was run separately the same day with `bash clean_up.sh` and its output is
at the end of [../output/ext-vector-search.txt](../output/ext-vector-search.txt), under "== Clean up". To check that the
table and its index are gone:

```bash
: "${PREFIX:?set the prefix used for this run, e.g. PREFIX=recipe-vec}"
aws dynamodb describe-table --table-name "${PREFIX}-trips"   # ResourceNotFoundException: gone
```
