# Lakehouse first

Updates go into a Kinesis data stream. Firehose reads the stream, converts each buffer of records to Parquet and
writes it under `s3://<prefix>-lake-<account>/updates/dt=<day>/`. Athena counts updates in the last hour over those files.

| File | What it holds |
|---|---|
| `lakehouse.py` | creates the Glue table, the stream, the Firehose stream and its role, sends updates, waits and runs the Athena query; `run_local` is for the offline path |
| `run.py` | the steps in order: `--target local` (default) or `--target aws` |

```bash
uv run python rep-lakehouse-first/run.py                                     # offline
PREFIX=recipe-yourname uv run python rep-lakehouse-first/run.py --target aws    # your sandbox
```

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/lakehouse.feature`.

- **R40** (event) When `rep-lakehouse-first/run.py --target aws` runs, the recipe shall create a
  Kinesis stream with 1 provisioned shard and a Firehose stream that reads it and writes Parquet with Snappy under
  `updates/dt=<arrival day>/` after 60 seconds or 64 MB.
- **R41** (optional) Where lakehouse first is used, the recipe shall describe the files as the Glue table `updates`,
  whose partition `dt` Athena works out by partition projection.
- **R42** (optional) Where lakehouse first is used, the recipe shall run every query in the workgroup `$PREFIX-wg`,
  which cancels a query that would scan more than 1 GB.
- **R43** (optional) Where lakehouse first is used, the recipe shall let Firehose read its stream, write its bucket
  and read its Glue table, and nothing else, unless `FIREHOSE_ROLE_ARN` names a role to use.
- **R44** (optional) Where lakehouse first is used, the recipe shall put each update on the stream with its
  `entity_id` as partition key, at most 500 records in one call.
- **R45** (unwanted) If the stream refuses some records of a call from `rep-lakehouse-first/run.py`, then the
  recipe shall send only those records again.
- **R46** (unwanted) If records of `rep-lakehouse-first/run.py` are still refused after the 5th call, then the
  recipe shall stop with an error that says how many.
- **R47** (optional) Where lakehouse first is used, the recipe shall count distinct `event_id`s and read only the
  day folders from the first day of the window on.
- **R48** (complex) While the stream or the Firehose stream of an earlier run is still being deleted, when
  `rep-lakehouse-first/run.py --target aws` runs, the recipe shall wait until it is gone before it creates it again.
- **R49** (unwanted) If the Firehose stream of `rep-lakehouse-first/run.py` ends in a failed state, then the
  recipe shall stop with an error before it sends an update.

Read the Decisions section of the recipe's [README](../README.md) to see why each step is built this way.
