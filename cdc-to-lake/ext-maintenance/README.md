# Extension: compaction and snapshot expiry on a schedule

**What it adds.** Athena's `OPTIMIZE ... REWRITE DATA USING BIN_PACK` rewrites the many small files Firehose commits
into fewer, larger ones. `VACUUM` expires snapshots older than the table's 7-day limit in
`main/sql/create_table.sql` and deletes the files no snapshot uses. The script measures Data scanned and run time of
one query before and after, and with `--schedule` creates two EventBridge Scheduler schedules that run both every
night.

**What changes in the main scenario.** Step 5's table has fewer files and fewer snapshots. With `--schedule`, two schedules
run every night at 03:00 and 04:00 Vancouver time until `clean_up.py` removes them.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Why maintain at all (R80, R81) | Compaction and expiry | One commit per buffer interval leaves many small files and snapshots. A query opens the files it reads and reads more metadata | L11 (small files); Firehose docs, [Iceberg tables: compaction and storage optimization](https://docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-considerations.html) |
| How to compact (R82) | Athena `OPTIMIZE ... BIN_PACK` | Runs where the table already lives and is billed as Data scanned; does not need a cluster | Athena docs, [OPTIMIZE](https://docs.aws.amazon.com/athena/latest/ug/optimize-statement.html) |
| How long to keep snapshots (R84) | 7 days (`vacuum_max_snapshot_age_seconds`), at least 5 snapshots (course default; Athena's defaults are 5 days and 1) | Time travel and rollback (ext-rollback-or-schema) are available only for snapshots that are kept | Athena docs, [VACUUM](https://docs.aws.amazon.com/athena/latest/ug/vacuum-statement.html); Data Analytics Lens [BP 13.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-13.html) (remove unused data) |
| How to schedule (R83) | EventBridge Scheduler calling Athena `StartQueryExecution` directly | You do not need to write a function. The schedule is one API resource that clean-up removes | EventBridge Scheduler docs, universal targets |
| Alternative | Glue Data Catalog table optimizers (managed compaction and snapshot retention) | They require less to run, but need a role and run on AWS's schedule. With Athena, we run the measurement ourselves | AWS docs, Glue table optimizers |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R80** (complex) Where `ext-maintenance` is used, when `maintenance.py` has compacted the table, the recipe shall
  return the same answer to the measured query from fewer data files.
- **R81** (complex) Where `ext-maintenance` is used, when `maintenance.py` has expired the old snapshots, the recipe
  shall show that a query at the first snapshot is no longer possible.
- **R82** (complex) Where `ext-maintenance` is used, when `maintenance.py --target aws` runs, the recipe shall run
  the measured query, `OPTIMIZE`, `VACUUM` and the measured query again in Athena, and print Data scanned and run
  time before and after.
- **R83** (complex) Where `ext-maintenance` is used, when `maintenance.py --target aws --schedule` runs, the recipe
  shall create two schedules that start `OPTIMIZE` at 03:00 and `VACUUM` at 04:00 Vancouver time every night.
- **R84** (optional) Where `ext-maintenance` is used, the recipe shall keep the table's snapshots for 7 days, and
  at least 5 of them.

## Run it

Offline, on your laptop, in `cdc-to-lake`:

```bash
uv run python ext-maintenance/maintenance.py
```

In a sandbox terminal (the recipe README says what that is), after `python -m main.run --target aws`. Run by us on
2026-11-13 (UTC; `../output/ext-maintenance.txt`). Sign in and check the account:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account of your pipeline
```

To use a role you already have, set `SCHEDULER_ROLE_ARN` to its ARN before running the schedule command.
Otherwise, `--schedule` creates the role EventBridge Scheduler assumes, `$PREFIX-cdc-scheduler-role`.

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-maintenance/maintenance.py --target aws              # once, measured
uv run python ext-maintenance/maintenance.py --target aws --schedule   # and the two schedules
```

Clean-up: the schedules start two Athena queries every night until
`uv run python clean_up.py` removes them, so clean up when you are done. Cost: see below.

**What you should see** (`output/maintenance-local.txt`, 120 commits of 5 rows):

```
            data files  snapshots   data scanned   run time
before             120        120       452745 B  1012.6 ms
after                1          1         9937 B    53.9 ms
same query answer before and after: True
time travel to the first snapshot: no longer possible (expired)
```

On AWS (`output/ext-maintenance.txt`, 2026-11-13 UTC, after the main scenario and the other extensions: a few commits,
not 120), Data scanned fell from 301 B to 197 B. The query took 625 ms before compaction and 1233 ms after it:
with a handful of tiny files the run time is Athena's overhead and varies from query to query, so a shorter query run time after compaction
can show on a table with many small files. The local numbers are
DuckDB on a laptop, not Athena. The offline demo expires every snapshot but the current one. The AWS schedule keeps
7 days.

**Cost.** `OPTIMIZE` bills the Data scanned to rewrite the table (here a few kilobytes), at 5.50 USD per TB.
`VACUUM` makes requests to S3, which are billed as S3 requests. EventBridge Scheduler: the first 14 million
invocations a month are free in ca-central-1, and two schedules make about 60 (AWS bulk price list).
