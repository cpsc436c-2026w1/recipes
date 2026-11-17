# Extension: embeddings kept current through a queue

**What it adds.** A Lambda function on the table's stream (`enqueue.py`) queues an item in Amazon Simple Queue Service (SQS) only when its
text used to make the vector (name, description, category, brand) changed. A price or stock change is skipped. A worker
(`worker.py`) reads the queue, reads the item again from DynamoDB, embeds it through the course LLM gateway
(`qwen3-embedding-0.6b`, 1,024 numbers per text) and writes the vector index `item-vectors-v1`. A message that
fails three times moves to the dead-letter queue.

**What changes in the main scenario.** After the main scenario, this extension adds a second function on the same stream, two queues, a
VECTORSEARCH collection in its own collection group (`$PREFIX-search-vectors`) because a collection group holds one type
of collection, the index `item-vectors-v1`. The keyword index and the
indexing function are unchanged. Run `main/run.py --target aws` first: this extension uses its table.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Vector collection (R82) | Its own NextGen collection group, `$PREFIX-search-vectors`, with the same limits as the keyword one (0 to 2 OCU) | A collection group holds collections of one type only, so the VECTORSEARCH collection cannot join the SEARCH collection's group. Each group scales to zero on its own, so the two can bill up to 8 OCU together while both are busy | AWS docs, [Managing capacity limits](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scaling.html) ("Only one type of collection ... can be included in a single collection group") |
| Queue between stream and model (R81) | SQS, with a dead-letter queue after 3 receives | Model calls are slow and can fail because of budget or rate limits. The queue holds work while the worker catches up, and a bad item does not block the rest | L14 (queues, dead-letter queues); AWS docs, SQS dead-letter queues |
| What gets queued (R80) | Only items whose embedded text changed (hash of old and new image) | A price change does not call the model: the vector stands for the text | Course choice; AI Engineering [ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), "Comparing retrieval algorithms" (embedding cost when data changes often) |
| What the worker embeds | The item read again from DynamoDB, not the text in the message | The table is the source of truth; a stale message (the text changed again) is skipped, and the newer message follows | L17; same practice as the main scenario |
| Repeated messages | Skipped when the stored vector has the same text hash and model | SQS standard queues deliver at least once; a repeat does not call the model | AWS docs, SQS standard queues |
| Version | Index name `item-vectors-v1`; `_meta` records model and dimension; each vector records its model | Vectors from two models are not comparable: a model change means a new index filled beside the old one | Course choice |
| Batch the enqueue function cannot queue (R83) | Lambda retries up to 10 times and stops retrying records older than an hour. Lambda then sends a message identifying the batch's position on the stream to the same dead-letter queue | Processing the shard is not held up for a day by one batch. The message points to the records, which the stream keeps 24 hours | AWS docs, [Retain discarded records for a DynamoDB event source](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html) |
| Stream readers | Two: the indexing function and this one | AWS advises at most two readers per stream shard at a time; a third consumer reads from the queue | DynamoDB docs, Streams and Lambda |
| Where the worker runs | Your laptop or CloudShell, with your course Model Context Protocol (MCP) token in the shell | The token is never stored in AWS. A team service would run the worker on Lambda with the token in Secrets Manager | Course constraint (no keys in code; model calls through the course LLM gateway) |
| Visibility timeout | 120 s (course default) | Longer than one embedding call with retries, so two workers do not take the same message | AWS docs, SQS visibility timeout |

## Requirements

The form table and the rules are in the [recipe README](../README.md#requirements). The scenarios are in
[`../features/extensions.feature`](../features/extensions.feature).

- **R80** (optional) Where embeddings are kept current, the recipe shall queue an item only when its name,
  description, category or brand changed.
- **R81** (complex) Where embeddings are kept current, if a message fails three times, then the recipe shall move
  it to the dead-letter queue.
- **R82** (optional) Where embeddings are kept current, the recipe shall create the vector collection in a
  collection group of its own.
- **R83** (optional) Where embeddings are kept current, the recipe shall send a batch the enqueue function gives up
  on, after 10 retries or when its records are an hour old, to the dead-letter queue.

**Run it.** Run by us on 2026-11-14 in a course sandbox, right after the main scenario
(`output/embeddings-aws.txt`). Before `--setup`, sign in to your team sandbox and check the account with
`aws sts get-caller-identity`; run the main scenario's part 2 first, and `clean_up.py` the same day.

`--setup` creates the enqueue function's role (read the table's stream, send to the queue, write its logs). To use a
role you already have instead, set `LAMBDA_ROLE_ARN` to its ARN.

```bash
uv run python ext-embeddings/worker.py                                  # offline: moto, stub embedder
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'   # from the course page, or your team repo on github.students
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN
PREFIX=recipe-yourname uv run python ext-embeddings/worker.py --setup --target aws   # creates $PREFIX-reembed-enqueue-role
PREFIX=recipe-yourname uv run python ext-embeddings/worker.py --target aws  # drains the queue once
```

The older names `GATEWAY_URL` and `COURSE_MCP_TOKEN` still work when the new ones are not set.

**What you should see** (`output/embeddings-local.txt`):

```
initial items                      queued 8, skipped 0; worker {'embedded': 7, 'failed': 3}
price change on I03                queued 0, skipped 1; worker {}
description change on I05          queued 1, skipped 0; worker {'embedded': 1}
vectors in the index: 7 of 8 items; model calls 11
dead-letter queue: ['I08'] (the stub model fails on it; 3 tries, then moved)
```

The stub model is set to fail on one item, so its message is tried three times and then moved to the dead-letter
queue. The other seven are embedded. The price change does not call the model.

In a sandbox (`output/embeddings-aws.txt`, 2026-11-14):

```
collection let you in 71 s after it was ACTIVE
queue https://sqs.ca-central-1.amazonaws.com/111122223333/recipe-reembed
function recipe-reembed-enqueue on arn:aws:dynamodb:ca-central-1:111122223333:table/recipe-items/stream/...
vector index item-vectors-v1: {'embedding_model': 'qwen3-embedding-0.6b', 'index_version': 1, 'dimension': 1024, 'source_table': 'recipe-items'}
endpoint https://<collection-id>.aoss.ca-central-1.on.aws
worker: {'embedded': 10, 'removed': 4, 'stale': 2}; still queued 0; dead letters 0
```

`--setup` took 1 min 40 s (the times are in the capture's third line): the new vector collection refused the caller for its first 71 s, and the script waited,
as the main scenario does. The enqueue function starts at the oldest record of the stream, so the first worker run
found a message for every text change the main scenario had made, 16 of them: 10 embedded, 4 about the two items
the scenario deleted (the worker does not keep a vector for them), 2 skipped as stale because a newer message for the same item followed. A second worker run 45 s later
found the queue empty (`worker: {}`). The worker's counts depend on what the table went through before `--setup`;
what holds is `still queued 0` and `dead letters 0` after a run with a working model.

**Cost.** Course LLM gateway: 8 short texts are a few hundred tokens at the course LLM gateway's embedding price (well under 0.01 CAD,
assumed token count). SQS and Lambda for a few messages: well under 0.01 USD (assumed). The vector collection's own
group adds up to 4 OCU while it is busy, 1.06 USD an hour at most, and nothing after 10 idle minutes (see the main
README). `clean_up.py` removes the function, the queues and the collection.
