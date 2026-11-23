# Extension: undo a bad load, or change the schema without breaking queries

The two scripts work on the same Iceberg table. Both use PyIceberg, which talks to the local SQLite catalog offline
and to the Glue Data Catalog on AWS, so the code that runs in the test is the code that runs in your sandbox.

## Rollback (`rollback.py`)

**What it adds.** A backfill job writes every trip again with `fare_quote` in cents. A time-travel query shows the
state of the table before the load is still available in a snapshot. The script rolls the table back to that snapshot and appends again the rows
that good commits added after the bad one.

**What changes in the main scenario.** Nothing in `main/`. On AWS the script pauses the event source mapping while it
works and resumes it after: a Firehose commit during the rollback would race it, and the stream keeps the records
(24 hours), so the mapping resumes where it stopped.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| How to find the bad load (R70) | The same check (row count, average fare) at the current and an earlier snapshot | Time travel is a scan at a snapshot id. Athena writes it `FOR VERSION AS OF <id>` | L11 (snapshots); Athena docs, Iceberg time travel |
| How to undo it (R71) | Roll back to the snapshot before the bad one, then append the later good rows again | A plain rollback also drops every change the stream delivered after the bad load | Data Analytics Lens [BP 2.4](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-2.4---build-standard-operating-procedures-for-deployment-test-rollback-and-backfill-tasks..html) (operating procedures for rollback and backfill) |
| Which rows are good | Rows in the current snapshot that do not appear in the bad snapshot, matched on (`trip_id`, `seq`) | Each change row has a unique sequence number; the backfill's rows are all in the bad snapshot | Course choice |
| Tool | PyIceberg `manage_snapshots().rollback_to_snapshot()` | Athena SQL does not have a rollback statement. PyIceberg runs from a laptop without a cluster | PyIceberg docs |
| Credentials for PyIceberg on AWS | The script passes the credentials boto3 resolved to the catalog as `client.*` properties (`catalog_properties` in `rollback.py`) | PyIceberg reads the table's files through pyarrow, which looks for credentials on its own: with a profile that takes them from the EC2 instance it got `ACCESS_DENIED` from S3 where boto3 worked (run of 2026-11-12). The credentials are read once, when the script starts: a run that lasts longer than they do must be started again | Course choice |

## Schema change (`schema_change.py`)

**What it adds.** The extension adds a new optional column, `payment_type`. Old rows read `NULL`, the change does not rewrite any data file, and a
dashboard query that names its columns returns the same answer as before.

**What changes in the main scenario.** On AWS: `ALTER TABLE ... ADD COLUMNS` in Athena, then `payment_type` in
`ITEM_FIELDS` (`main/handler.py`), then `uv run python -m main.infra --update-code`. Firehose skips a field the table
does not have and delivers it once a column with the same name and type exists
([Iceberg considerations](https://docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-considerations.html),
"Columns"). How soon Firehose sees the new column after `ALTER TABLE` the docs do not say;
rows written before that read `NULL`. Firehose's own `SchemaEvolutionConfiguration`, which adds columns by itself, is
in preview and not used here.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Kind of change (R72, R73) | Add an optional column only | Iceberg tracks columns by id, so an added column does not move or rewrite old data. Old readers ignore it | L11; Iceberg spec, schema evolution |
| What we avoid | Changing a column's type or meaning, reusing a dropped name | Old queries would return different numbers without failing | Course choice |
| View | `trips_current` lists its columns | The view keeps its shape when the table grows; recreate it to show the new column | Course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R70** (complex) Where `ext-rollback-or-schema` is used, when `rollback.py` has made the bad load, the recipe
  shall show with a query at the earlier snapshot that the table before the load is still there.
- **R71** (complex) Where `ext-rollback-or-schema` is used, when `rollback.py` restores the table, the recipe shall
  roll back to the snapshot before the bad load and append again the rows that good commits added after it.
- **R72** (complex) Where `ext-rollback-or-schema` is used, when `schema_change.py` adds the column `payment_type`,
  the recipe shall leave the old data files as they are and return the same answer to a query written before the
  change.
- **R73** (complex) Where `ext-rollback-or-schema` is used, when `schema_change.py --target aws` runs, the recipe
  shall add the column with one `ALTER TABLE` in Athena and print the three steps that fill it.

The pause and resume of the stream consumer around a rollback on AWS (above) has no requirement here: that path
needs the Glue Data Catalog and has no scenario.

## Run it

Offline, on your laptop, in `cdc-to-lake`:

```bash
uv run python ext-rollback-or-schema/rollback.py
uv run python ext-rollback-or-schema/schema_change.py
```

In a sandbox terminal (the recipe README says what that is), after `python -m main.run --target aws`. Run by us on
2026-11-13 (UTC; `../output/ext-rollback.txt`, `../output/ext-schema-change.txt`). Sign in and check the account:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account of your pipeline
```

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-rollback-or-schema/rollback.py --target aws
uv run python ext-rollback-or-schema/schema_change.py --target aws
```

Clean-up: nothing new is created; `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` removes the table with the rest. Cost: see below.

## What you should see

`output/rollback-local.txt` reports 5 changes with an average fare of 15.9 before the load. It reports 9 changes and an average of
518.25 after it, about 30 times as high, because the three rows of the load carry fares 100 times too high;
time travel still finds 5. After the restore, the report shows 6 changes (the 5, plus the one good change made after the load) and an
average fare comparable to the average before the load. In `output/schema-local.txt`, the old query's answer is
unchanged. `payment_type` is `None` on old rows and `card` on the new one. Snapshot ids change on every run.

On AWS (`output/ext-rollback.txt` and `output/ext-schema-change.txt`, 2026-11-13 UTC): 7 changes and an average fare of
15.9 before the load, 11 and 581.81 after it, time travel finds 7, and after the restore 7 changes and 15.9 again.
The AWS run pauses the stream consumer during the restore, so no good change arrives after the load and none is
appended again (0). The schema run adds the column and prints the three steps that fill it. It does not run those steps.

**Cost.** Athena queries of a few kilobytes at 5.50 USD per TB of Data scanned in ca-central-1 (AWS bulk price list); PyIceberg calls to Glue and S3 are a handful of requests.
