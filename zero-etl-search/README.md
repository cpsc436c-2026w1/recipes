# Search index that follows a DynamoDB table: Streams, Lambda and OpenSearch Serverless

## What you build

Build a DynamoDB table of items, a Lambda function on the table's stream, and a small OpenSearch Serverless collection
that scales to zero. Every insert, update and delete in the table reaches the keyword index a few seconds later
(median 1.7 s over 7 timed changes in a sandbox run on 2026-11-13). A
record the index refuses is set aside in a queue without holding up the changes behind it. A notebook lets you type searches and watch a change you make in the table become searchable.

## Use it when

- Your project needs free-text search or filters over items your app keeps in DynamoDB (a catalogue, listings,
  notes), and the index must follow the table. The "Hybrid retrieval with an eval" extension needs such an index:
  this one has the field names of [`hybrid-retrieval-eval`](../hybrid-retrieval-eval/README.md), whose
  `keyword_query()` runs on it unchanged.
- The table stays the source of truth: the app writes only to DynamoDB, never to the index.
- How well the search answers (BM25 against embeddings, recall@5) is the other recipe's question:
  [`hybrid-retrieval-eval`](../hybrid-retrieval-eval/README.md). This one is about keeping a derived index in step.
- You need reports and totals rather than search: [`cdc-to-lake`](../cdc-to-lake/README.md) feeds the lake instead.
- Lectures: L14 (change data capture, derived data, retries, bisecting a batch, failure queues, idempotent
  writes), L17 (BM25, OpenSearch).

## Main scenario

1. The app's items live in the DynamoDB table `$PREFIX-items`, with a stream that carries every change
   (`NEW_AND_OLD_IMAGES`).
2. `main/infra.py` creates a NextGen collection group with the smallest limits the API takes (minimum 0, maximum 2
   OCU), a SEARCH collection in it with its encryption, network and data access policies, and the index `items-v1`
   with our mappings (`main/mappings.py`).
3. It creates the function `$PREFIX-search-indexer` from `main/handler.py` (one file, no packaged dependencies) and
   an event source mapping from the stream to it: batches of up to 10, up to 10 retries, records older than an
   hour given up on, then the failure queue `$PREFIX-search-indexer-failed`. Before the mapping starts it calls
   the function to write and delete a probe document until the function can write to the index.
4. The starting items are written to the table. The function indexes them from the stream.
5. `main/run.py` makes seven changes one at a time (two inserts, three updates, two deletes) and times each from the
   table write to the first search that shows it.
6. It writes an item whose price is `"free"`, which the index's mapping refuses, and an item the index accepts right behind it:
   the bad one ends in the failure queue, the good one is searchable.
7. It gives the function the same batch twice, as Lambda does on a retry: the documents are the same as before.

`demo/search_demo.py` is the same loop to try by hand: a search box over the index, a form that writes to the table,
and a refresh that shows when your change is searchable.

Offline, `main/run.py` and the demo use moto DynamoDB and Streams, a stand-in for Lambda's event source mapping,
the same handler and a stand-in index (`main/standin.py`). They keep the rules this recipe depends on: documents
by id, mapped field types, `dynamic: false`, BM25 scores, bisect and retries, the failure queue.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Source of truth (R10, R11) | DynamoDB; the index is derived from it and can be rebuilt from it | The app writes once; two writes (table and index) can disagree after a failure or arrive in a different order, while a derived index applies the table's changes in the order they happened | L14 (change data capture); DDIA 2e [ch. 12](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch12.html), "Databases and Streams", "Change Data Capture" (the search index as a follower of the database) |
| How the index is fed (R30 to R32) | DynamoDB Streams, a Lambda event source mapping and our function (about 230 lines with its comments). OpenSearch Ingestion (`ext-zero-etl/`) is an alternative | The Lambda function is app code to maintain and test, and it is billed per call (it does not incur charges while idle). OpenSearch Ingestion does not need sync code but bills at least 1 unit, 0.253 USD an hour, while the pipeline exists, and the course sandbox denies it | Course constraint (sandbox budget); AWS docs, [Process DynamoDB records with Lambda](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-eventsourcemapping.html) |
| Document id (R10 to R15) | The item's key (`id`); INSERT and MODIFY index the new image under it, REMOVE deletes it | Lambda's event source mappings deliver each record at least once, so a batch can come twice: under the same ids it leaves one document per item, whatever the number of tries. A delete of a document already gone is not an error | [REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html) make mutating operations idempotent; AWS docs, [Process DynamoDB records with Lambda](https://docs.aws.amazon.com/lambda/latest/dg/with-ddb.html) (at least once; make the function idempotent), [supported operations](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-genref.html#serverless-operations) |
| Order of changes (R13) | Stream order; within one batch only the last change of each item is sent | The stream gives each item's changes in the order they happened, and Lambda works through a shard in order, so the newest change is written last. A replay of an older change by hand would overwrite a newer one (see the open question in Adapt it) | AWS docs, [DynamoDB Streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Streams.html) |
| Temporary errors (R22 to R24) | Retried inside the function with backoff and jitter (waits of half to all of 0.5, 1, 2, 4, then 8 s, at most 8 calls, stopping when the next wait would leave less than 5 s before the 60 s timeout); still failing, the function reports the earliest unwritten record and Lambda sends the batch again from there, up to 10 times, records older than an hour given up on | A collection waking from zero (10 to 30 s), a 403 while a new IAM statement or data access policy spreads, a 429 or a 5xx clear on their own. Retrying them whole keeps good records out of the failure queue: nothing is split. Offline tests show no good record lost after 6 and after 30 failing calls in a row; no temporary error happened in the sandbox runs, so this path has run offline only | AWS docs, [partial batch response](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html), [Scale to zero](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html); IAM docs, [changes not always immediately visible](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot.html#troubleshoot_general_eventual-consistency); [REL05-BP03](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html) control and limit retry calls |
| One bad record (R20, R21, R26) | A document the index refuses (a 4xx other than 403, 408 and 429) is not retried in the function. The function reports the refused document. When it is first in its batch, the function raises an error. Lambda bisects the batch: it splits the batch until the refused document is alone. After the retries, only that document goes to the failure queue in Amazon Simple Queue Service (SQS) | With the defaults (retry until the record is 24 hours old) one bad record holds up its shard for a day. Splitting does not use up retries, so the bad record ends up alone and only it is set aside; the message says where it is on the stream, and `main/envs.py` `record_keys()` reads it back | AWS docs, [Retain discarded records for a DynamoDB event source](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html) ("Splitting batches doesn't consume the retry quota"; with the defaults a bad record can block its shard for up to one day), [partial batch response](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html), [event source mapping parameters](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-params.html); L14 |
| What the function sends (R13, R25) | One `_bulk` request per batch (then only the documents still to write) | The good records are written on the first try; when Lambda sends the batch again they are written again (harmless, same ids) | Follows from the document id row |
| Before the mapping starts (R37) | `main/infra.py` invokes the function's probe (write and delete one probe document) until it works | The first batches do not spend their retries waiting for the new role's grants to spread or the collection to wake | Course choice; same IAM and scale-to-zero pages as above |
| Batch size and window (R32) | 10 records, no batching window | The smallest lag for a demo, and a bad batch is split down to one record in a few calls. A busy table would raise both (default 100, up to 10,000) to make fewer calls | AWS docs, [event source mapping parameters](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-params.html) |
| Starting position (R32) | `TRIM_HORIZON` (the oldest record the stream keeps) | Items written before the mapping was ready are indexed too. A table with items older than 24 hours needs a one-off copy first (a Scan into the same function, or `ext-zero-etl`'s export) | AWS docs, same pages |
| Collection limits (R35) | NextGen collection group, minimum 0 and maximum 2 OCU for indexing and for search | With minimum 0, indexing and search drop to zero once no collection in the group has had a request for 10 minutes. Billing for them stops. The first request after that waits 10 to 30 s. 2 is the smallest maximum the API takes (valid values 0, 2, 4, 8, 16, then multiples of 16), so at most 4 OCU, 1.06 USD an hour, while busy | AWS docs, [Collection group capacity limits](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/collection-groups-capacity-limits.html), [Scale to zero](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html); course constraint (sandbox budget) |
| Standby replicas (R35) | `ENABLED` on the group and the collection | A NextGen group refuses `DISABLED`, so it is not a choice here; the OCU maximum still caps the bill | AWS docs, Scale to zero (its example uses `ENABLED`) |
| Access (R36, R44, R45) | Public endpoint with SigV4; the data access policy lets the function write documents in `items-v1` only, and you create the index, search it and delete an old one after a move; the function's IAM role allows `aoss:APIAccessAll` on this collection only. A rerun merges into the policy: it only adds principals and indexes. `main/migrate.py drop` takes an old index out, `drop-principal <arn>` a principal (never you or the function's role) | Each principal can do its own part and nothing more; no `*` in any resource. A teammate who runs the scripts with a different role is added on their run (the same SSO permission-set role is already in). A team service would use a VPC endpoint | [SEC03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html) grant least privilege access; AWS docs, [data access policies](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-data-access.html), [HTTP 403 errors](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html) |
| Signing (R10) | botocore's `SigV4Auth` signs requests for the service `aoss` with `x-amz-content-sha256`. `urllib` sends the requests | boto3 and botocore are in the Lambda Python runtime, so the zip is one file. Lambda advises packaging the SDK version you test with; a runtime update could change the signer, a risk we take for a one-file demo | AWS docs, [HTTP 403 errors](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html) (sign for `aoss`, with `x-amz-content-sha256` among the signed headers), [Runtime dependencies in Python](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html) (package all dependencies, even those in the runtime) |
| Mappings (R35) | Created by us before the function writes; `dynamic: false` | Field types are chosen before the first document. A new DynamoDB attribute stays in `_source` but does not add a searchable field. A value of the wrong type is refused (step 6) | Course choice; OpenSearch docs, dynamic mapping |
| Is the index complete? (R46) | `main/migrate.py check`: the table's item count, the index's document count, the failure queue's message count; exit 1 when the counts differ | Step 3 of a move says "once its count matches the table", and nothing measured it; a derived index can fall behind or miss a refused item without an error anywhere | Data Analytics Lens, [best practice 7.2](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-7.2---monitor-for-data-quality-anomalies..html) (monitor for data quality anomalies) |
| Index version (R40 to R43) | In the name (`items-v1`), in the mapping's `_meta`, and on every document (`index_version`) | A mapping change cannot change a field's type in place: it builds `items-v2` beside `items-v1`, and the app moves when it is full | Course choice |
| Function logs (R25, R34) | Log group made by us with 1 day retention; one line per batch | Lambda would make it without setting an expiry; clean-up removes it | Course constraint: clean-up |
| Alarms (R70 to R72) | Two CloudWatch alarms with no action: the function's iterator age above 5 minutes for a minute, and any message in the failure queue | A sync that stops, or a record the index does not hold, otherwise shows only to someone who looks at the index | Serverless Applications Lens, [Metrics and alerts](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/opex-metrics-and-alerts.html) (for stream-based invocations, alert on IteratorAge); Data Analytics Lens, [best practice 6.2](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-6.2---monitor-analytics-systems-to-detect-analytics-or-etl-job-failures..html) (detect ETL job failures) |
| Backup of the source of truth (R73, R74) | Point-in-time recovery on the table, 7 days | The index can be rebuilt from the table (backfill, R41), but the table does not rebuild itself after a bad write or a delete | [DynamoDB Well-Architected Lens](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-wal.html), reliability pillar (a backup strategy that fits the RPO and RTO); API, [PointInTimeRecoverySpecification](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PointInTimeRecoverySpecification.html) (1 to 35 days) |
| Role (R36, R56) | Created by `main/infra.py` `ensure_role` with only what the function does (`indexer_statements`); or passed in (`LAMBDA_ROLE_ARN`) | A mistake in the function reaches one stream, one queue, one log group and one collection | [SEC03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html) |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets
after a requirement's number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an extension is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. The requirements of an extension are in its own README, and their numbers go on from here:
[embeddings kept current](ext-embeddings/README.md#requirements) from 80,
[no sync code](ext-zero-etl/README.md#requirements) from 90. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Index follows the table** (the function, `main/handler.py`)

- **R10** (event) When an item is inserted or updated in the table, the recipe shall index its new image under the
  item's key as the document id.
- **R11** (event) When an item is deleted from the table, the recipe shall delete its document.
- **R12** (ubiquitous) The recipe shall store each document with `index_version`, the time of the table change and
  the time the function sent it, and without the key, which is the document id.
- **R13** (event) When one batch holds several changes of one item, the recipe shall send only the last of them.
- **R14** (event) When the function gets a batch it has already written, the recipe shall leave one document per
  item, with the same content.
- **R15** (event) When the function deletes a document that is not in the index, the recipe shall count the delete
  as done.
- **R16** (event) When `main/run.py` runs offline, the recipe shall make seven changes one at a time, write a record
  the index refuses with a good one right behind it, give the function one batch twice, and print one line for
  each, as in `output/main-local.txt`.

**Refused record and temporary errors**

- **R20** (unwanted) If the index refuses a document with a status that is not a temporary one, then the recipe
  shall not send that document again in the same call of the function.
- **R21** (unwanted) If the earliest record the function could not write was refused and is the first of its batch,
  then the recipe shall fail the call, so Lambda splits the batch.
- **R22** (unwanted) If records of a batch are still unwritten and the earliest of them is not a refused first
  record, then the recipe shall return that record's sequence number to Lambda as the batch item failure.
- **R23** (unwanted) If a request or a document gets a temporary error (HTTP 403, 408, 429, 500, 502, 503, 504, a
  time-out or a lost connection), then the recipe shall send it again after a wait of half to all of 0.5, 1, 2, 4,
  then 8 seconds, with at most 8 calls in one call of the function.
- **R24** (unwanted) If the next wait would leave less than 5 seconds before the function's time-out, then the
  recipe shall stop retrying and report the earliest unwritten record.
- **R25** (event) When the function has handled a batch, the recipe shall write one log line with the number of
  records, of documents indexed, deleted and refused, of calls made and the last temporary error.
- **R26** (event) When the index refuses one record of the stream, the recipe shall leave that record alone in the
  failure queue and still index the records behind it.

**Set-up in AWS** (`main/run.py --target aws`, `main/infra.py`)

- **R30** (event) When the set-up runs, the recipe shall create the table `$PREFIX-items`, keyed by `id`, billed per
  request, with a stream of new and old images.
- **R31** (event) When the set-up runs, the recipe shall create the function `$PREFIX-search-indexer` from a zip that
  holds `handler.py` only, on Python 3.13 and arm64, with 128 MB and a 60 second time-out.
- **R32** (event) When the set-up runs, the recipe shall create the event source mapping from the table's stream to
  the function with batch size 10, no batching window, bisect on function error, `ReportBatchItemFailures`, 10
  retries, a maximum record age of 3600 seconds, starting position `TRIM_HORIZON`, and the failure queue as its
  on-failure destination.
- **R33** (event) When the set-up runs, the recipe shall create the failure queue
  `$PREFIX-search-indexer-failed` with messages kept 14 days.
- **R34** (event) When the set-up runs, the recipe shall create the function's log group with logs kept 1 day.
- **R35** (event) When the set-up runs, the recipe shall create a NextGen collection group with standby replicas
  `ENABLED` and 0 to 2 OCU for indexing and for search, a SEARCH collection in it, and the index with
  `dynamic: false` and its version in `_meta`.
- **R36** (ubiquitous) The recipe shall let only the function change documents in the index, and only you and the
  teammates who ran the set-up create, search and delete the index, with no permission on all resources (`*`).
- **R37** (event) When the set-up has created the function, the recipe shall invoke its probe every 10 seconds
  until it can write, before it creates the event source mapping.
- **R38** (unwanted) If the new collection answers 401 or 403 to the index create, then the recipe shall try again
  every 10 seconds for up to 300 seconds.
- **R39** (unwanted) If SQS refuses the queue's name because a queue of that name was deleted in the last 60 seconds,
  then the recipe shall try again every 10 seconds, up to 8 times.

**Moving to a new index version, and access** (`main/run.py --setup`, `main/migrate.py`)

- **R40** (event) When the set-up runs with `INDEX_VERSION=2`, the recipe shall create `items-v2`, add it to the
  data access policy beside `items-v1` and point the function at it, and write nothing to the table.
- **R41** (event) When `main/migrate.py backfill` runs, the recipe shall set `index_backfill` on every item, on the
  condition that the item still exists.
- **R42** (unwanted) If `main/migrate.py drop` names the index the deployed function writes to, then the recipe shall
  refuse and delete nothing.
- **R43** (event) When `main/migrate.py drop` names another index, the recipe shall delete it and take it out of the
  data access policy.
- **R44** (event) When a teammate's role runs the set-up, the recipe shall add that role to the data access policy
  and keep the principals already in it.
- **R45** (unwanted) If `main/migrate.py drop-principal` names you or the function's role, then the recipe shall
  refuse and leave the policy as it is.
- **R46** (event) When `main/migrate.py check` runs, the recipe shall print the table's item count, the index's
  document count and the number of messages in the failure queue, and exit with code 0 only when the counts match.

**Clean-up**

- **R50** (event) When `clean_up.py` runs, the recipe shall delete this `PREFIX`'s event source mappings,
  functions, log groups, queues, collections with their policies, collection groups, table and the roles tagged
  `recipe=zero-etl-search`, and nothing of another `PREFIX`.
- **R51** (event) When `clean_up.py --dry-run` runs, the recipe shall print what it would delete and delete nothing.
- **R52** (event) When `clean_up.py` runs and everything is already gone, the recipe shall report each step absent
  and exit with code 0.
- **R53** (unwanted) If a step of `clean_up.py` is refused with an error that does not mean "already gone", then the
  recipe shall report that step `FAILED` with the error code, try the remaining steps and exit with code 1.
- **R54** (unwanted) If a service control policy denies OpenSearch Ingestion, then the recipe shall report the
  pipeline step `not checked` and leave the exit code as the other steps make it.
- **R55** (event) When `clean_up.py` runs after a function was deleted, the recipe shall still delete that
  function's event source mappings.
- **R56** (unwanted) If a role of the recipe's name does not carry the tag `recipe=zero-etl-search`, then the recipe
  shall leave it.

**Course constraints**

- **R60** (ubiquitous) The recipe shall run its offline run without an AWS account or a network connection.
- **R61** (ubiquitous) The recipe shall start the name of every resource it creates with `$PREFIX`, in the Region
  `AWS_REGION` (default `ca-central-1`).

**Watching the sync, and a backup of the source**

- **R70** (event) When the set-up runs, the recipe shall create an alarm that goes off when the function's iterator
  age, the age of the oldest record it has not finished, stays above 5 minutes for a minute.
- **R71** (event) When the set-up runs, the recipe shall create an alarm that goes off when the failure queue holds
  a message.
- **R72** (event) When `clean_up.py` runs, the recipe shall delete both alarms.
- **R73** (event) When the set-up runs, the recipe shall turn on point-in-time recovery for the table, with a
  recovery period of 7 days.
- **R74** (unwanted) If DynamoDB answers that the new table's backups are not available yet, then the recipe shall
  try again every 10 seconds for up to 60 seconds.

**Requirements of the extensions**, in the README of each folder:

- 80 to 83, embeddings kept current: [`ext-embeddings/README.md`](ext-embeddings/README.md#requirements)
- 90 and 91, no sync code: [`ext-zero-etl/README.md`](ext-zero-etl/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need an AWS account: moto plays DynamoDB, its stream
and SQS inside the Python process.

| Part | Files | Laptop, offline | Laptop, against AWS | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Main scenario | `main/run.py`, `main/envs.py` | yes, the default: moto table and stream, the stand-in event source mapping and stand-in index (`main/standin.py`) | yes, `--target aws`: it creates everything, then writes to the table and times the searches | no |
| Indexing function | `main/handler.py` | called in the Python process by the stand-in mapping | no | **yes: Lambda**, `$PREFIX-search-indexer`, zipped from `main/handler.py` alone |
| Set-up | `main/infra.py` | no | yes: creates the table, queue, role, collection group, collection, index, function and mapping | no |
| Index move | `main/migrate.py`, `main/run.py --setup` | no | yes | no |
| Search demo | `demo/search_demo.py` (marimo) | yes | yes, `-- --target aws` | no |
| Extensions | `ext-embeddings/`, `ext-zero-etl/` | yes, offline runs | `ext-embeddings`: the worker on your laptop, its enqueue function in Lambda; `ext-zero-etl`: denied in the course sandbox | `ext-embeddings/enqueue.py` in Lambda; `ext-zero-etl` is an OpenSearch Ingestion pipeline, a managed service |
| Clean-up | `clean_up.py` | no | yes | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

The stand-in event source mapping (`main/standin.py`) does what this recipe relies on, after the AWS docs: batches
of `BatchSize`, a failed call split in two without using up a retry, `MaximumRetryAttempts`, partial batch
responses (the batch tried again from the reported record), and a message to the failure queue that says where the
records are. It does not model: the age limit (`MaximumRecordAgeInSeconds`; offline there is no clock), the time
between Lambda's retries (2 min 45 s for 11 tries in the run of 2026-11-13), more than one shard at a time, and the
condition in the failure message, which it always writes as `RetryAttemptsExhausted`. Offline, a change is
searchable as soon as it is delivered: the stand-in index does not have a refresh delay.




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole` or `dynamodb:TagResource`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `aoss:APIAccessAll`, `aoss:BatchGetCollection`, `aoss:BatchGetCollectionGroup`, `aoss:CreateAccessPolicy`, `aoss:CreateCollection`, `aoss:CreateCollectionGroup`, `aoss:CreateSecurityPolicy`, `aoss:DeleteAccessPolicy`, `aoss:DeleteCollection`, `aoss:DeleteCollectionGroup`, `aoss:DeleteSecurityPolicy`, `aoss:GetAccessPolicy`, `aoss:GetSecurityPolicy`, `aoss:UpdateAccessPolicy`, `cloudwatch:DeleteAlarms`, `cloudwatch:DescribeAlarms`, `cloudwatch:PutMetricAlarm`, `dynamodb:BatchWriteItem`, `dynamodb:CreateTable`, `dynamodb:DeleteItem`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:PutItem`, `dynamodb:Scan`, `dynamodb:UpdateContinuousBackups`, `dynamodb:UpdateItem`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListRoleTags`, `iam:PassRole`, `iam:PutRolePolicy`, `lambda:CreateEventSourceMapping`, `lambda:CreateFunction`, `lambda:DeleteEventSourceMapping`, `lambda:DeleteFunction`, `lambda:GetEventSourceMapping`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, `lambda:InvokeFunction`, `lambda:ListEventSourceMappings`, `lambda:UpdateEventSourceMapping`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `logs:CreateLogGroup`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `logs:PutRetentionPolicy`, `osis:DeletePipeline`, `osis:GetPipeline`, `s3:DeleteBucket`, `s3:DeleteObject`, `s3:ListBucket`, `sqs:CreateQueue`, `sqs:DeleteQueue`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl`, `sqs:ReceiveMessage` |
| `ext-embeddings/` adds | `sqs:DeleteMessage` |
| `ext-zero-etl/` adds | `osis:CreatePipeline`, `osis:ValidatePipeline`, `s3:CreateBucket`, `s3:PutBucketPublicAccessBlock` |

### 1. Laptop, offline

Run these from the repository root. They do not need an AWS account, so you do not sign in. You need `uv`
(`curl -LsSf https://astral.sh/uv/install.sh | sh`; in CloudShell also `pip install --user uv`).

```bash
cd zero-etl-search
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run
uv run marimo edit demo/search_demo.py          # the demo, offline; opens in your browser
```

What you should see: `pytest` ends with a line like `94 passed`; `behave` ends with `0 failed`; the run prints the
lines of `output/main-local.txt` (below, [What you should see](#what-you-should-see)): every change "searchable on
the next delivery", the refused item alone in the failure queue, the replay leaving 9 documents.

Clean-up and cost: the offline run does not create or spend anything.

### 2. Laptop, against your team sandbox

Run the commands below from `zero-etl-search` on your laptop or CloudShell, in ca-central-1.

1. Sign in with AWS credentials for your team sandbox; the first command must show that account.
2. Set the names, with your own short name for `yourname` (CloudShell sets `AWS_REGION` to the console's Region,
   so set it). The run creates the function's role, `$PREFIX-search-indexer-role`; to use a role you already have,
   export `LAMBDA_ROLE_ARN` (`main/infra.py` lists what it must be allowed to do).

```bash
aws sts get-caller-identity                       # the account you expect, before anything is created
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python -m main.run --target aws            # creates everything, then runs the scenario
uv run marimo edit demo/search_demo.py -- --target aws
uv run python clean_up.py                         # the same day
```

What you should see: about two minutes of set-up (in the run of 2026-11-13 the collection refused your role for
its first 62 s, and the script waited), then the lines of [What you should see](#what-you-should-see), each printed
as it is measured. Waiting for Lambda's retries of the refused item takes a few minutes; the script waits up to 10.
The whole run, with the index move and clean-up, took 13.6 minutes on 2026-11-13.

The code has changed since that run in one place: the function no longer waits after its last retry (fact check,
pull request of 2026-11-13). The sandbox run took no retry, so the captured output is the same. A second run, on
2026-11-14, went through the whole scenario without the index move
([`output/main-aws-2026-11-14.txt`](output/main-aws-2026-11-14.txt)): set-up took 132 s, including a 62 s wait for
the collection. Median lag was 3.3 s over 7 timed changes. On 2026-11-13, median lag was 1.7 s.

Clean-up and cost: `uv run python clean_up.py`; at most about 0.24 USD for a run like the captured one
([Cost and clean-up](#cost-and-clean-up)).

### 3. Laptop, against your team sandbox: a mapping change (index move)

After part 2, before its clean-up, with the same `PREFIX` and sign-in. The three steps are in
[Adapt it](#adapt-it); the commands:

```bash
aws sts get-caller-identity
INDEX_VERSION=2 uv run python -m main.run --target aws --setup
INDEX_VERSION=2 uv run python -m main.migrate backfill
INDEX_VERSION=2 uv run python -m main.migrate check
uv run python -m main.migrate drop items-v1
```

What you should see: `--setup` prints `function ... now writes to items-v2`; the backfill prints the number of
items it touched; `drop` prints `items-v1 deleted and taken out of the data access policy`. In the run of
2026-11-13 the set-up waited 21 s for the changed policy, the backfill touched 10 items and the drop worked.

Clean-up and cost: the same clean-up as part 2; the move adds a minute or two of OCU time.

## What you should see

In a course sandbox, 2026-11-13 ([`output/main-aws.txt`](output/main-aws.txt), which also
holds the function's log, the index move and the clean-up):

```
[aws ca-central-1; function recipe-search-indexer]
1 s after ACTIVE: refused (401; a GET of _cat/indices answers 401 X01:network-policy-deny); trying again in 10 s
11 s after ACTIVE: refused (401; a GET of _cat/indices answers 401 X01:network-policy-deny); trying again in 10 s
21 s after ACTIVE: refused (401; a GET of _cat/indices answers 200 (no hint header)); trying again in 10 s
31 s after ACTIVE: refused (401; a GET of _cat/indices answers 401 X01:network-policy-deny); trying again in 10 s
42 s after ACTIVE: refused (401; a GET of _cat/indices answers 200 (no hint header)); trying again in 10 s
52 s after ACTIVE: refused (401; a GET of _cat/indices answers 200 (no hint header)); trying again in 10 s
collection let you in 62 s after it was ACTIVE
created in 129 s: table, stream, failure queue, role, collection group (min 0, max 2 OCU), collection, index items-v1, function, event source mapping
starting items: 8 documents in items-v1, 94.9 s after the table writes; the first requests to a new or idle collection wait for capacity
index version recorded in the mapping: 1 (source table recipe-items)
search 'waterproof': I02 (0.47)
INSERT I09: searchable 4.5 s; the function sent it 0.1 s after the write
INSERT I10: searchable 8.4 s; the function sent it 0.1 s after the write
MODIFY I04: searchable 1.7 s; the function sent it 0.4 s after the write
MODIFY I02: searchable 3.4 s; the function sent it 0.2 s after the write
MODIFY I09: searchable 1.1 s; the function sent it 0.1 s after the write
REMOVE I08: gone from search 1.7 s
REMOVE I10: gone from search 1.7 s
lag, table write to searchable: median 1.7 s (IQR 1.7 to 3.9 s), max 8.4 s, n = 7
search 'waterproof': I04 (0.58), I02 (0.47), I10 (0.13)
bad record I11 (price_cad 'free'): in the index False; failure queue: 1 message(s), batch size [1], item ['I11']
INSERT I12 written right after it: searchable 5.5 s (behind the retries)
replayed one batch (I04, I09) twice: ['ok', 'ok']; documents 9 before, 9 after (9 items in the table that the index takes); one document each for I04 and I09: True; I04 unchanged: True
```

What the numbers show:

- **Lag.** Seven changes, table write to the first search that shows them: median 1.7 s (IQR 1.7 to 3.9 s), max
  8.4 s, n = 7 (polled every 0.5 s). The function sent each change 0.1 to
  0.4 s after the write; the rest is the index making documents visible to search. Two earlier runs on 2026-11-12
  gave a median of 4.7 s (IQR 2.4 to 5.9 s, max 10.4 s, n = 14)
: the same order, from
  too few changes to say more.
- **New collection.** Every request from your role was refused for the first 62 s after the collection was ACTIVE:
  HTTP 401 with `x-aoss-response-hint: X01:network-policy-deny`, so it was the network policy not yet applied (the
  AWS docs name no time for it; [troubleshooting](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-troubleshooting.html)).
  The answers alternated between 401 and 200 for a while. `create_index` tried every 10 s and got in at 62 s. A run
  the same morning without that retry stopped on the 401 ([`output/main-aws-2026-11-13-refused.txt`](output/main-aws-2026-11-13-refused.txt)).
- **Retries.** Every batch was written on the function's first `_bulk` call: no temporary error happened in this run,
  so the retry inside the function was not needed (it is covered by `tests/test_handler.py`). The function's probe
  wrote on its first try. The refused item I11 went alone to the failure queue after Lambda's 11 tries, and I12,
  written right behind it, was searchable in 5.5 s.
- **Replay.** The same batch given to the function twice left 9 documents, one each for I04 and I09.
- **Search lags a little behind a lookup by id.** Right after I10 was "gone from search" (a search by its id), the
  ranked search for "waterproof" still listed it. Measure lag with the query your app runs.
- **Index move.** `--setup` waited 21 s (403, the changed data access policy) before your role could create
  `items-v2`; the backfill touched 10 items; `drop items-v1` deleted the old index and took it out of the policy. The
  run script's own count check right after the backfill got a 403 and was not repeated, so the document count of
  `items-v2` was not read in this run. An earlier run the same day read 9 documents for 10 items (the refused one
  stays out): [`output/main-aws-2026-11-13.txt`](output/main-aws-2026-11-13.txt).

The demo's offline run, exported with
`uv run marimo export html --no-include-code demo/search_demo.py -o output/demo-local.html`, is in
`output/demo-local.html` (static: the form and buttons do not work in the export). The same page exported against the
sandbox after the index move (`INDEX_VERSION=2 ... -- --target aws`) is `output/demo-aws.html`: index `items-v2`,
2 hits for "waterproof", the failure queue holding I11.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Embeddings kept current | A second function on the stream queues items whose text changed (SQS); a worker re-reads each item from DynamoDB, embeds it through the course LLM gateway and writes a vector index; failures go to a dead-letter queue. Example output from a course sandbox on 2026-11-14: [`output/embeddings-aws.txt`](output/embeddings-aws.txt) | `ext-embeddings/` | After the main scenario: a second function on the same stream, two queues, a vector collection in a collection group of its own, index `item-vectors-v1` |
| No sync code (zero-ETL) | An OpenSearch Ingestion pipeline exports the table and then reads its stream into the same index, with external versions so an older change cannot overwrite a newer one. **Does not run in the course sandbox**: its service control policy denies `osis:*` (an exception is being asked of LT Hub). Cost: 0.253 USD per Ingestion OCU-hour in ca-central-1 (AWS bulk price list), at least 1 OCU, billed every minute the pipeline exists, with or without changes: about 6 USD a day | `ext-zero-etl/` | Replaces steps 3 to 6: no function or mapping; a pipeline, an S3 bucket for the export, point-in-time recovery on the table |

Find search code (keyword, vector, fused) in [`hybrid-retrieval-eval`](../hybrid-retrieval-eval/README.md),
`main/opensearch_store.py`. Its `keyword_query()` runs on `items-v1` unchanged.

## Cost and clean-up

Prices from the AWS bulk price list for ca-central-1:

- OpenSearch Serverless: 0.266 USD per OCU-hour for indexing and the same for search
.
  With minimum 0, nothing is billed after 10 idle minutes; with maximum 2, at most 4 OCU while busy
  (1.06 USD an hour). How many OCU the group uses while busy is not in the docs: the group's `IndexingOCU` and
  `SearchOCU` metrics in CloudWatch show it.
- DynamoDB point-in-time recovery: 0.22 USD per GB-month of table size
;
  the table is a few kilobytes.
- CloudWatch: two alarms, 0.10 USD per alarm-month
,
  billed for the hours they exist: well under 0.01 USD for a run that is cleaned up the same day.
- Lambda: 0.20 USD per million requests and 0.0000133334 USD per GB-second on arm64
;
  a run makes a few dozen calls of 128 MB: under 0.01 USD.
- DynamoDB on demand: 0.6875 USD per million writes
;
  Lambda's reads of the stream are not billed ([Optimizing costs for DynamoDB Streams](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CostOptimization_StreamsUsage.html)).
  SQS: 0.40 USD per million requests
.
  Both well under 0.01 USD for a run.

The run above, with the index move and the demo export, took 13.6 minutes from the first create to the end of
clean-up on 2026-11-13; 5 of those minutes are clean-up, most of it waiting for the collection to be deleted. At most
4 OCU for that time is about 0.24 USD (an upper bound from the price list, not a measured bill)
; everything else is
under 0.01 USD.
Remove everything:

```bash
PREFIX=recipe-yourname uv run python clean_up.py --dry-run
PREFIX=recipe-yourname uv run python clean_up.py     # mapping, function and log group, queue, collection, policies, group, table, role
```

The clean-up script tries every step and prints one line for each: removed, absent, or FAILED with the reason. It exits 1 when a step
failed, so run it again after fixing the reason. In an account that denies OpenSearch Ingestion (the course sandbox)
the pipeline step prints `not checked (denied by a service control policy)` and does not change the exit code. It also removes what the extensions made. A run right after a
clean-up works: it waits for a table, a collection or a queue name that is still being deleted.

## Adapt it

- For your own items, change the key (`id`) in `main/infra.py` `create_table` and in `main/handler.py` `KEY`, and the fields in
  `main/mappings.py` `PROPERTIES`. Only mapped fields are searchable.
- To change a mapping, use `main/migrate.py` and complete the steps through step three:
  1. `INDEX_VERSION=2 uv run python -m main.run --target aws --setup` creates `items-v2`, adds it to the data access
     policy beside `items-v1` (your app keeps reading `items-v1`), and points the function at `items-v2`. It writes
     nothing to the table. From then on `items-v1` no longer follows the table, so do the next steps soon.
  2. `INDEX_VERSION=2 uv run python -m main.migrate backfill` sets one attribute (`index_backfill`) on every item
     that still exists, so each item goes to `items-v2` through the stream, in order with any other change to it: a
     change made during the copy cannot be overwritten by an older copy. One write per item.
  3. `INDEX_VERSION=2 uv run python -m main.migrate check` compares the counts: it exits 0 when `items-v2` holds
     every item of the table, and 1 with the failure queue's count when it does not (a refused item is one such
     difference). Move the app's reads to `items-v2` once it says so, then
     run `uv run python -m main.migrate drop items-v1` to delete the old index and take it out of the policy. It reads the
     index the deployed function writes to and refuses to drop that one.
- When a teammate leaves, run `uv run python -m main.migrate drop-principal <role arn>` (a rerun never removes anyone).
- Many writes: raise `BATCH_SIZE` and `BATCH_WINDOW_S` in `main/infra.py`, and `MAX_OCU` if the group's metrics
  show it at the maximum.
- Bad records: read the failure queue, fix the item in DynamoDB (not in the index), and the fixed change flows through
  the stream like any other.
- An open question for your design: a replay by hand of an old change overwrites a newer one. Lambda's own retries
  never do that (they keep shard order), but a tool that re-sends old records would. External versions (the stream's
  sequence number as the document version, as `ext-zero-etl` does) would refuse the older change. We have not used
  them: the AWS docs do not say that OpenSearch Serverless accepts `version_type=external`, and we have not tried it
  (unchecked).
- A team service: a VPC endpoint for the collection; send the two alarms (R70, R71) to an SNS topic your team
  reads.
