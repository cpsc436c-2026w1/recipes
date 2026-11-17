# Extension: the same index with no sync code (OpenSearch Ingestion, zero-ETL)

**What it adds.** An Amazon OpenSearch Ingestion pipeline with the DynamoDB source copies the table into the same
keyword index `items-v1`. It first exports the full table from point-in-time recovery through S3. It then copies every change from the
table's stream. You do not write, test or deploy a function: the pipeline's configuration
(`pipeline.py` `render()`, rendered in `pipeline.example.yaml`) is the whole sync.

**It does not run in the course sandbox.** The sandbox organization's service control policy denies every `osis:*`
call (examples: [`../output/main-aws-denied.txt`](../output/main-aws-denied.txt) on 2026-11-12 and, on 2026-11-14,
[`../output/zero-etl-aws-refused.txt`](../output/zero-etl-aws-refused.txt)); an exception has been
asked of LT Hub. Use it in an account that allows OpenSearch Ingestion, or read it as the alternative to the main
scenario's function.

**What changes in the main scenario.** Steps 3 to 6 are replaced. The extension does not use an indexing function, event source mapping or
failure queue. Instead, the extension uses point-in-time recovery on the table, an S3 bucket `$PREFIX-search-<account>` for the export and
for documents the index refuses (`$PREFIX/osis-dlq/`; each teammate's PREFIX has its own folder, so a shared `BUCKET` works), the role `$PREFIX-search-pipeline-role`, and the pipeline
`$PREFIX-items-sync`. The collection, its group and the index are the same. Run one or the other on a table, not both:
two writers to one index would each write every change.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Lambda against a pipeline | A pipeline here; a function in the main scenario | You do not maintain sync code, and the export joins the stream for you. The pipeline bills per hour while it exists, even without changes, and the course sandbox denies it | AWS docs, ["DynamoDB pipeline blueprint"](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/configure-client-ddb.html) |
| Document id and action | `primary_key` and `opensearch_action` from the pipeline's metadata | One document per item, as in the main scenario | AWS docs, same page |
| Ordering | External versions (`document_version`, `document_version_type: external`) | A retried or late change with an older version is refused, so the newest change wins | AWS docs, same page. That Serverless honours `external` versions is not stated in the AWS docs (unchecked until a run: the pipeline's `documentsVersionConflictErrors` metric shows it) |
| Failed documents | The sink's dead-letter files in S3 (`$PREFIX/osis-dlq/`) | A refused document is kept for a look instead of being dropped or blocking the stream | Data Prepper docs, opensearch sink |
| Pipeline units | 1 to 2 | 1 is the smallest minimum. AWS recommends 2 for availability | AWS docs, [scaling pipelines](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/ingestion-scaling.html) |
| Checks before create | Our `validate()` and the service's `ValidatePipeline` | Pipeline validation errors show in a second on the laptop instead of after a 10-minute create | Course choice |

## Requirements

The form table and the rules are in the [recipe README](../README.md#requirements). The scenarios are in
[`../features/extensions.feature`](../features/extensions.feature).

- **R90** (optional) Where the pipeline is used, the recipe shall copy the table into the index first and then
  apply the stream's changes to the same index.
- **R91** (optional) Where the pipeline is used, the recipe shall write each item under its primary key, with the
  stream's action and an external document version.

**Run it.** Refused in the course sandbox, last tried on 2026-11-14: `--target aws` made the bucket and the
pipeline role, then stopped at its first OpenSearch Ingestion call with `AccessDeniedException ... not authorized to
perform: osis:ValidatePipeline ... with an explicit deny in a service control policy`
(`../output/zero-etl-aws-refused.txt`), and `clean_up.py` removed the bucket and the role
(`../output/clean-up-2026-11-14.txt`).
Only the offline run has run; the pipeline itself has not run anywhere.
In an account that allows it, sign in first and check the account with `aws sts get-caller-identity`.

```bash
uv run python ext-zero-etl/zero_etl.py                      # offline: validate, moto table, stand-in pipeline
PREFIX=recipe-yourname uv run python ext-zero-etl/zero_etl.py --target aws   # only where osis:* is allowed
PREFIX=recipe-yourname uv run python clean_up.py            # removes the pipeline, bucket and role as well
```

After a change to `render()`, write the example again: `uv run python ext-zero-etl/pipeline.py --write`
(`tests/test_zero_etl.py` fails until you do).

**What you should see** (offline, `output/zero-etl-local.txt`):

```
mappings and pipeline definition: valid
initial export: 8 items -> 8 documents in items-v1
search 'waterproof headlamp' before the update: ['I02', 'I04']
stream: 2 changes applied; I04 in the index now: 'Headlamp HX-300', in_stock=True, index_version=1
I08 removed from the table, in the index: False
search 'waterproof headlamp' after the update: ['I04', 'I02']
```

**Cost.** OpenSearch Ingestion: 0.253 USD per Ingestion OCU-hour in ca-central-1 (AWS bulk price list), billed while the pipeline runs, at least 1 OCU: about 6 USD a day for a pipeline left
running, with or without changes. The collection costs the same as in the main scenario. The export from
point-in-time recovery is 0.11 USD per GB (see `dynamodb/ext-nightly-report`). This table is a few kilobytes.
Point-in-time recovery itself is billed per GB-month of table size.

Clean up the same day: stop the pipeline
(`aws osis stop-pipeline --pipeline-name <name>`) or run `clean_up.py`.

If CreatePipeline refuses a NextGen collection, you can use a classic collection. A classic collection does not scale
to zero; it uses at least 2 OCUs, about 0.53 USD an hour. Create the collection without `collectionGroupName`. The AWS docs name
Serverless search collections as sinks of the DynamoDB pipeline and do not say whether NextGen ones differ
(unchecked).
