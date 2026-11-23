# Backfill: past trips in the lake beside the live ones, and in DynamoDB by id

Use this if your system needs past data in its lake, or in a DynamoDB table, before or beside its live data: a
history copied once from your own raw files (CSV, JSON Lines or Parquet) or, in the rides example, from the course
catalog; each row marked as backfill and counted against the source per day; the same rows loaded into DynamoDB by
an import from S3 (a new table) or by batch writes (a table that exists). Most systems need it by Checkpoint 2
(Tue Dec 1), and at the final hand-in (Tue Dec 15) as one command with its count check.

It extends [`rep-lakehouse-first`](../rep-lakehouse-first/README.md): the same bucket `<PREFIX>-lake-<account>` and
Glue database `<prefix>_lake`. You do not need to run `rep-lakehouse-first` first: `backfill.py` creates the bucket,
the two workgroups and the database when they are missing, and borrows them when they are there.

| File | What it holds |
|---|---|
| `backfill.py` | the command: copy, count, view, and `--to-dynamodb` |
| `statements.py` | every Athena statement, and its DuckDB twin for the offline run, built by the same functions |
| `engines.py` | where the statements run: Athena (`--target aws`) or DuckDB over local files (offline) |
| `dynamo_load.py` | the import from S3, the batch writes with their retries, and the exact counts |
| `history.toml` | what your files hold: format, columns, the time column, which column fills which |
| `clean_up.py` | deletes what this extension made, and nothing else (`--yes`; a dry run without it) |
| `live_run.sh` | the recipe's own live demonstration, written to `.local/ext-backfill-live.txt`; it ends with `clean_up.py --yes`, so use the step-by-step commands for a project run |
| `sim/` | SIM01 and SIM04 (below), and the DuckDB SQL that drew SIM01 from DS01 |
| `test_ext_backfill.py` | tests of the source guard, the borrowed table and the empty load (`uv run pytest -q ext-backfill/test_ext_backfill.py`) |

**Words used here.** *Backfill*: copying past data into a system that starts with live data only. *External
table*: a Glue table whose rows are files that already exist under an S3 prefix; Athena reads them where they are.
*Cutover time*: the moment from which the view takes the live table's rows instead of the history's.

## Main scenario

1. `backfill.py --source files` creates the external table `<prefix>_lake.history_raw` over your files, as
   `history.toml` describes them (CSV columns all text, cast in the copy). The rides example uses `--source course`,
   the course catalog's `course.course_rides.rides`.
2. It counts the source's rows per day and the days already in `trips_history`, then copies the missing days: one
   CTAS (CREATE TABLE AS SELECT, an Athena query that writes its result as a new table) the first time, in
   `<PREFIX>-ctas-wg`, and one INSERT INTO for each further month. A day whose count differs is deleted (its files
   and its partition) and copied again. It records which source it copied from (`history/trips.source`), and stops
   when a later run names another source, unless `--replace-source` is given. Matching daily counts find missing
   rows, not changed values: if a source day changes but keeps its count, delete the history with `clean_up.py --yes`
   and copy again.
3. Every row gets a `trip_id` (a hash of all its source columns and its number among identical rows),
   `origin = 'backfill'` and the run's `run_id`.
4. It compares each day's count in `trips_history` with the source's (in `<PREFIX>-wg`), prints the days that
   differ and exits 1 if one does.
5. It (re)creates the view `trips_all`: history before the cutover time, the live table `trips` from it. The live
   table `trips` (one row per trip) is the one
   [`cdc-to-lake/ext-scheduled-merge`](../../cdc-to-lake/ext-scheduled-merge/README.md) keeps in `<prefix>_lake`, or
   your own table of that name and shape. Without it, and without `--live-from`, the view is not made and the run
   says so.
6. `backfill.py --to-dynamodb` writes the rows as DynamoDB JSON under `imports/trips-by-id/<load id>/` and creates
   `<PREFIX>-trips-by-id` by an import from S3; into a table that exists it writes with BatchWriteItem. Either way
   it ends with an exact count against the rows read.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Where the history comes from (R90, R100) | an external table over your own files first; the course catalog as the rides example | most histories are a team's own files, not the course catalog; an external table lets Athena read them where they are, and the same CTAS copies them. An unpartitioned external table is read whole by every statement, so k months scan it k times: read `DataScannedInBytes` of the first CTAS | L11 (external tables, CTAS) |
| CSV and JSON Lines columns (R90) | declared `string`, cast in the copy with `try_cast` and `date_parse` | the Open CSV SerDe (the serializer/deserializer Athena uses to read CSV fields) reads a `timestamp` only as UNIX milliseconds and leaves an empty numeric value unread ([Open CSV SerDe](https://docs.aws.amazon.com/athena/latest/ug/csv-serde.html)); a bad value becomes a null and the row is still copied and counted | Athena docs |
| Timestamps in the copy (R100) | `CAST(request_datetime AS timestamp)`, milliseconds | the course table's times are `timestamp(6)`, and a CTAS into a Hive Parquet table refuses them ("Incorrect timestamp precision for timestamp(6); the configured precision is MILLISECONDS"); the source's times are whole seconds, so nothing is lost | Athena |
| A copy of our own (R100) | CTAS into `trips_history`, Parquet with zstd, partitioned by `requested_day` | Athena cannot create a view over a cross-account table, and the live view needs the history partitioned by day; the same CTAS from the course catalog is tutorial B3T1's step 2 and A3's task 3.1 | L11; tutorial B3T1 |
| One statement per month (R100) | one CTAS, then one INSERT INTO for each further month | a CTAS or INSERT INTO writes at most 100 partitions ([CTAS and INSERT INTO for more than 100 partitions](https://docs.aws.amazon.com/athena/latest/ug/ctas-insert-into.html)); a month is at most 31, and each statement reads one month partition of `rides` (and a day of the next, for trips picked up after midnight) | Athena docs |
| Which workgroup (R100, R120) | CTAS and INSERT INTO in `<PREFIX>-ctas-wg`, which does not enforce its query-results location and cancels a query that scans more than 8 GiB; the counts in `<PREFIX>-wg`, which enforces its results location (`athena-results/`, kept 7 days) and cancels a query over 1 GiB. Neither caps total spending | a CTAS with `external_location` fails in a workgroup that enforces its results location ([CTAS table properties](https://docs.aws.amazon.com/athena/latest/ug/create-table-as.html)); both come from `scripts/lake.py`, which never changes a workgroup it did not make | a scan limit caps a wrong query (course workgroups) |
| The id of a past trip (R101) | the first 16 hex digits of SHA-256 of all source columns (as text, a null as `\N`, joined by `\|`), then `-` and its number among identical rows | the source has no trip id; a hash of the values alone gives two identical trips one key, and the import keeps one of them. Identical rows share a day, so a recopied day gets the same ids; a row number over the whole source would change when a day is added. A source with a stable id column uses it (`history.toml`) | idempotent writes |
| One source per history (R104) | `history/trips.source` names the source of the first copy; another source stops the run unless `--replace-source` | a day whose count differs is deleted and copied again, so a second source would silently replace the first one's days | ours |
| A short day (R102, R103) | count each day, delete and copy again a day that differs; leave complete days alone | a failed or cancelled CTAS or INSERT INTO can leave partial files that later queries read ([CTAS considerations](https://docs.aws.amazon.com/athena/latest/ug/ctas-considerations-limitations.html)) | Athena docs |
| The view's live side (R110) | the live table `trips` (one row per trip), from a cutover time | a union with the raw events would count a live trip three times or leave its zone empty (the zone is on `trip_requested` only); a split at a day would drop the history of a partial first day. History ids and live ids are not joinable: a live trip is `t-…`, a past one a hash | one row per trip, as scheduled-merge keeps `trips` |
| Counting against the source (R120, R133) | a count per day against the source; an exact item count against the rows read | a finished statement is not proof that every row arrived | show counts against the source (course practice for a load) |
| DynamoDB: a new table (R130) | an import from S3, DynamoDB JSON by default | the import uses no write units and bills per GB of uncompressed source; it creates a new table only, from CSV, DynamoDB JSON or Ion, not Parquet ([how it works](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/S3DataImport.HowItWorks.html)). CSV imports every non-key column as a string ([formats](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/S3DataImport.Format.html)), so a fare arrives as text and a later `ADD` fails; DynamoDB JSON keeps each type. It is 3.8 times the size of CSV here <!--src: measured: DuckDB over the DS01 file, 2026-11-22, 210,164,405 against 54,874,995 bytes -->, so it costs 0.032 against 0.008 USD | L12; price sheet |
| ImportTable takes no tags (R130) | tag the table with `TagResource` once the import is `COMPLETED` | `TableCreationParameters` has no tags ([ImportTable](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_ImportTable.html)) | course tags on every resource |
| DynamoDB: a table that exists (R131, R132) | BatchWriteItem, 25 items a call, unprocessed items sent again after a random wait under 0.1, 0.2, 0.4, 0.8 s, at most 5 attempts | one call carries up to 25 puts, refuses a repeated key in one call, and returns what DynamoDB did not process ([BatchWriteItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchWriteItem.html)). Use the client's `batch_write_item`, not boto3's `batch_writer`, which hides `UnprocessedItems` | L12; retries with a doubling limit and random waits (BatchWriteItem docs) |
| Counting a load (R133) | `DescribeImport` after an import; a strongly consistent `Scan` with `Select=COUNT` of this `load_id`, every page, after batch writes | `DescribeTable` `ItemCount` is updated about every six hours ([TableDescription](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TableDescription.html)); a table that exists also holds live items, so the scan counts this load's own. The filter on `load_id` changes the count, not the read charge: the scan reads the whole table, live items included | DynamoDB docs |
| Attribute name `origin` (R101) | `origin` | `SOURCE` is a DynamoDB reserved word ([reserved words](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ReservedWords.html)); one name in the lake and in DynamoDB | DynamoDB docs |
| The data source `course` | registered from `COURSE_CATALOG_ID` if missing; recorded `borrowed`, never deleted | the catalog's account id is not written in this repository: tutorial B3T1's `setup_athena.sh` gives it, and an account that ran B3T1 already has `course` | no account numbers in code |
| A table that was there before (R131) | `clean_up.py` keeps a `<PREFIX>-trips-by-id` the ledger records `borrowed` | batch writes added backfill items to a table that also holds live items; deleting it would delete those | delete only what the extension created |
| Offline | DuckDB runs the DuckDB twin of each statement; the Athena text is kept and checked | the two dialects differ only in the hash and in parsing a time from text. For a CSV source the hashed text is the same in both, so the ids are the same; for the course table (typed columns) the text of a timestamp or a double differs between the engines, so offline ids and Athena ids differ, and only one engine's ids are ever in one table | offline first |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/backfill.feature` and `features/trips_by_id.feature`. A team keeps 2 to 5 of these
per story, with its own numbers.

**The history in the lake** (the operations analyst: last month's trips beside the live ones, each marked)

- **R90** (event) When `ext-backfill/backfill.py --source files` runs, the recipe shall create the external table
  `<prefix>_lake.history_raw` over the files under the given prefix, with the format and columns of
  `history.toml`, unless the table exists.
- **R100** (event) When `ext-backfill/backfill.py` copies the days from `--from` to `--to`, the recipe shall write
  the source rows of those days into `trips_history`, Parquet partitioned by `requested_day`, with one CTAS for the
  first month and one INSERT INTO for each further month.
- **R101** (ubiquitous) The recipe shall give every copied row a `trip_id` made from a hash of all its source
  columns and its number among the source rows with the same values, `origin` = `backfill` and the run's `run_id`.
- **R102** (unwanted) If a day asked for is already in `trips_history` with the source's count, then the recipe
  shall leave it as it is and copy only the other days.
- **R103** (unwanted) If a day in `trips_history` has a count other than the source's, then the recipe shall delete
  that day's files and partition and copy the day again.
- **R104** (unwanted) If `ext-backfill/backfill.py` copies from a source other than the one `history/trips.source`
  names, then the recipe shall stop with exit code 1 and leave `trips_history` as it is, unless `--replace-source`
  is given.
- **R110** (ubiquitous) The recipe shall keep the view `trips_all` as one row per trip, the rows of `trips_history`
  before the cutover time and the rows of the live table `trips` from it with `origin` = `live`, and print how many
  history rows at or after the cutover it leaves out.
- **R120** (event) When a copy or `--check-only` ends, the recipe shall compare the count of each day in
  `trips_history` with the source's, print each day that differs and exit with code 1 if one does.

**The history in DynamoDB** (the support agent: a past trip by its id, its fare a number)

- **R130** (complex) While `<PREFIX>-trips-by-id` does not exist, when `ext-backfill/backfill.py --to-dynamodb`
  runs, the recipe shall write the rows as DynamoDB JSON under `imports/trips-by-id/<load id>/` and create the
  table by an import from S3 keyed by `trip_id`, or from CSV with `--import-format csv`.
- **R131** (complex) While `<PREFIX>-trips-by-id` exists, when `ext-backfill/backfill.py --to-dynamodb` runs, the
  recipe shall write the rows with BatchWriteItem in calls of at most 25 items without a repeated key, sending the
  unprocessed items again after a random wait under a limit that doubles, at most 5 attempts.
- **R132** (unwanted) If items are still unprocessed after the 5th attempt, then the recipe shall stop with exit code
  1 and an error that gives their number and the first `trip_id`.
- **R133** (event) When a DynamoDB load ends, the recipe shall compare the items it loaded with the rows it read,
  from `DescribeImport` after an import or from a strongly consistent count of this `load_id` after batch writes,
  and exit with code 1 if they differ or the import had errors.

## Run it

> ⚠️ Commands run in two places, always from the `project-skeleton` folder (the paths below start with
> `ext-backfill/`). Offline commands run in a terminal on your laptop and need no AWS account. Live commands run in a
> laptop terminal with your lease's credentials active, in `ca-central-1`.

From your laptop, in a terminal in `project-skeleton/` (offline):

```bash
uv sync                                                                  # should finish without an error
uv run pytest -q tests/test_backfill.py ext-backfill/test_ext_backfill.py   # every test should pass
uv run behave features/backfill.feature features/trips_by_id.feature     # all pass; the one @live scenario is skipped
uv run python ext-backfill/backfill.py --source course --from 2024-03-01 --to 2024-03-30   # SIM01 into trips_history
uv run python ext-backfill/backfill.py --to-dynamodb      # after the line above: it loads what that copy wrote
uv run python ext-backfill/clean_up.py --local --yes      # deletes .local/ext-backfill/, the offline state
```

The offline `--to-dynamodb` runs in moto, so its bucket name carries moto's stand-in account number, not yours; it
should end `1000 items imported, 1000 rows read, 0 errors`. Your own files offline: give the folder, for example
`--source files --files ext-backfill/sim/SIM04 --from 2024-03-14 --to 2024-03-15`. The offline state in
`.local/ext-backfill/` is one history: a run with another source than the first stops and says so (run
`clean_up.py --local --yes` first, or pass `--replace-source` to replace those days).

### Run it live

From a laptop terminal with your lease's credentials, in `project-skeleton/`. First choose a short lowercase prefix
(letters, digits and hyphens: it goes into the bucket name `$PREFIX-lake-<account>`) that no other resource in your
lease uses, set the region, and check the account:

```bash
export PREFIX=backfill-yourname
export AWS_DEFAULT_REGION=ca-central-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "$ACCOUNT_ID"                                    # must be your lease's account; stop if it is not
aws athena list-data-catalogs --query 'DataCatalogsSummary[].CatalogName'    # is "course" in the list?
```

`--source course` reads the course catalog through the Athena data source `course`. If `course` is in the list
(tutorial B3T1 registers it), go on. If not, set `COURSE_CATALOG_ID` to the catalog account ID that B3T1's
`setup_athena.sh` registers, on its own line (`export COURSE_CATALOG_ID=YOUR_COURSE_CATALOG_ID`), and the first run
registers it; or use `--source files` with your own files instead.

Step by step, from the same terminal:

```bash
uv run python ext-backfill/backfill.py --target aws --source course --from 2024-03-01 --to 2024-03-30
uv run python ext-backfill/backfill.py --target aws --to-dynamodb       # the import waits until it ends: ~10 minutes
uv run python ext-backfill/clean_up.py            # lists what it would delete; deletes nothing
```

The copy should end `counts   30 days, 0 differ` and exit 0, and the import with `0 errors`. The import blocks while
DynamoDB works (9.5 minutes for 574,665 items in one measured run, `output/ext-backfill.txt`). If
`dynamodb:ImportTable` is refused, the command stops with `AccessDenied` after it has written the import files under
`imports/trips-by-id/` (the clean-up deletes them): run it again with `--to-dynamodb --load batch`, which creates the
table and writes it with BatchWriteItem at the price below. Delete with `clean_up.py --yes` only when you no longer
need the history (Cost and clean-up, below).

The view `trips_all` needs the live table `trips` (Main scenario 5). Once it exists, or to set the cutover time
yourself, run the view alone:

```bash
export LIVE_FROM=2026-11-23T14:00:00Z               # the time from which your live trips are complete
uv run python ext-backfill/backfill.py --target aws --view-only --live-from "$LIVE_FROM"
```

It prints `view     <prefix>_lake.trips_all: history before ...` and how many history rows at or after the cutover it
leaves out. Without `--live-from` the cutover is the earliest `requested_time` in `trips`.

Your own files: upload them under `raw/history/` of the lake bucket, describe them in `history.toml` (format,
columns, the time column, `[map]`), and copy them:

```bash
export HISTORY_S3_URI="s3://$PREFIX-lake-$ACCOUNT_ID/raw/history/"
aws s3 cp path/to/history.csv "${HISTORY_S3_URI}history.csv"
aws s3 ls "$HISTORY_S3_URI"                         # your file should be listed
uv run python ext-backfill/backfill.py --target aws --source files --files "$HISTORY_S3_URI" --from 2024-03-01 --to 2024-03-30
```

Replace the dates with your history's. `imports/trips-by-id/` is one load's files; the import needs its prefix to
hold only that load.

`ext-backfill/live_run.sh` is the recipe's own demonstration of every case (a short day made by deleting one of its
files with `aws s3 rm`, the check that finds it, the copy that repairs it, the import, then batch writes into the
table). It ends with `clean_up.py` (the list) and `clean_up.py --yes`, so nothing it made is left; it writes to
`.local/ext-backfill-live.txt`. Use the step-by-step commands for your project.

**With `rep-lakehouse-first` on one `PREFIX`.** Both make `<PREFIX>-wg` with the same settings (a 2^30-byte cutoff,
enforced, engine 3, results under `athena-results/`), so whichever runs second borrows it. `scripts/lake.py` never
changes a workgroup it did not make: if `backfill.py` stops and names a difference in a workgroup's settings, choose a
new `PREFIX`. Do not run the project-wide `clean_up.py` to repair one workgroup: it deletes the bucket and database
too.

### AWS permissions

| Part | Actions |
|---|---|
| copy, count, view | `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution`, `athena:GetWorkGroup`, `athena:ListWorkGroups`, `athena:CreateWorkGroup`, `athena:TagResource`, `athena:GetDataCatalog` (`athena:CreateDataCatalog` once); `glue:GetDatabase`, `glue:CreateDatabase`, `glue:GetTable`, `glue:CreateTable`, `glue:GetPartitions`, `glue:BatchCreatePartition`, `glue:DeletePartition` and read on the course catalog; `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:DeleteObject`, `s3:CreateBucket`, `s3:PutLifecycleConfiguration`, `s3:PutBucketPublicAccessBlock`; `sts:GetCallerIdentity` |
| `--to-dynamodb`, a new table | `dynamodb:ImportTable`, `dynamodb:DescribeImport`, `dynamodb:ListImports`, `dynamodb:DescribeTable`, `dynamodb:TagResource`; `s3:GetObject`, `s3:ListBucket`; `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:DescribeLogGroups`, `logs:DescribeLogStreams`, `logs:PutLogEvents`, `logs:PutRetentionPolicy` on `log-group/aws-dynamodb/*` ([import IAM](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/S3DataImport.Requesting.html)) |
| `--to-dynamodb`, a table that exists, or `--load batch` | `dynamodb:BatchWriteItem`, `dynamodb:Scan`, `dynamodb:DescribeTable` (`dynamodb:CreateTable`, `dynamodb:TagResource` for `--load batch`) |
| `clean_up.py` | `dynamodb:DescribeTable`, `dynamodb:DeleteTable`, `glue:GetTable`, `glue:DeleteTable`, `glue:GetTables`, `glue:DeleteDatabase`, `s3:ListBucket`, `s3:DeleteObject` |

The CTAS from the course catalog is the one you ran in tutorial B3T1 and in A3. If `dynamodb:ImportTable` is
refused, run `--to-dynamodb --load batch`: BatchWriteItem into a table the script makes, at the price below.

## Data

DS01 is the TLC High Volume For-Hire Vehicle trip records of March 2024 (the monthly Parquet file). The rides
example copies the Uber trips (`HV0003`) requested in zones 48, 161 and 230 from 2024-03-01 to 2024-03-30:
524,629 trips, 142,310, 188,010 and 194,309 per zone <!--src: measured: DuckDB over fhvhv_tripdata_2024-03.parquet, 2026-11-22 -->,
read from the course catalog's `course_rides.rides` (Iceberg, partitioned by month of `pickup_datetime`), whose
columns were listed with Athena on 2026-11-22. No two of those trips are identical in every column, so every id
ends in `-1` <!--src: measured: DuckDB over the DS01 file, 2026-11-22 -->. The course table holds the same 524,629
trips for those days <!--src: measured: live run 2026-11-23, output/ext-backfill.txt -->.

- **SIM01 March sample** (R100, R120, R130): 1,000 of DS01's trips of the three zones, 2024-03-01 to 2024-03-30,
  34 on days 2, 5, 8 … 29 and 33 on the others, drawn by `sim/extract_sim01.sql`; offline it stands in for the
  course table.
- **SIM02 Short day** (R103, R120): SIM01 copied, then 2024-03-14 in `trips_history` left with 29 of its 34 rows,
  as an INSERT INTO cut short would leave it.
- **SIM03 Throttled writes** (R131, R132): 60 trips of SIM01; the stand-in table returns the last 5 items of a
  BatchWriteItem call as unprocessed on the first 2 calls, or on every call.
- **SIM04 Team files** (R90, R101): `sim/SIM04/history.csv`, a CSV with a header line and 6 rows of DS01's columns
  (4 requested on 2024-03-14, 2 on 2024-03-15); two of the 2024-03-14 rows are one DS01 trip written twice.

`trips_history` columns: `trip_id, pickup_zone_id, dropoff_zone_id, requested_time, trip_miles, trip_time_s, fare,
driver_pay, origin, run_id, requested_day` (`requested_day` is the UTC day of `requested_time`). The view adds
`trip_state` (`completed` for history rows) and takes from `trips` its `trip_id`, `trip_state`, `pickup_zone_id`,
`requested_time` and `fare`, with nulls for the columns `trips` does not carry. `view()` in `statements.py` is the one
place to change if `trips` has other columns.

## What you should see

Offline, the first run over SIM01 (the run id and time differ in your run):

```text
run      bf-20261123T001323Z-3f2a: course source, 2024-03-01 to 2024-03-30
days     30 asked for: 0 already complete, 30 missing, 0 short
ctas     2024-03-01 to 2024-03-30: 30 days
counts   30 days, 0 differ; 1000 rows in trips_history, 1000 in the source
view     not made: no live table recipe_lake.trips yet and no --live-from
```

A second run says `30 already complete` and copies nothing.

On AWS, check these in your own run: the copy ends with `0 differ` and the history's row count equals the source's;
a second run copies nothing; the import (or the batch load) ends with as many items as rows read and `0 errors`.
Row counts, bytes scanned and the import's duration vary.

One live run's output, for example (`output/ext-backfill.txt`, and the step-by-step run in
`output/ext-backfill-session-2026-11-23.txt`): the CTAS of 2024-03-01 to 2024-03-30 copied 524,629 rows, the
source's count; widening to 2024-04-02 added one INSERT INTO for 2024-03-31 and one for April; one file of
2024-03-14 deleted by hand made `--check-only` exit 1, and the next run deleted that day's files and partition and
copied it again (0 differ). The import created `<PREFIX>-trips-by-id` with 574,665 items, 0 errors, in 9.5 minutes;
BatchWriteItem then wrote 2024-04-02's 21,070 items into it in 843 calls, and the count of that load matched.

## Cost and clean-up

Prices: USD, ca-central-1, free tier left out, from the course price sheet (page `436c-student/prices.md` on the
course server): Athena 5.50 USD per TB scanned with a 10 MB minimum a query; DynamoDB on-demand 0.6875 USD per
million write units and 0.1375 per million read units; S3 Standard 0.025 USD per GB-month; S3 requests per 1,000.
Rates the sheet does not carry are from AWS's public price list for ca-central-1, the source the sheet names:
DynamoDB import 0.165 USD per GB of uncompressed source, DynamoDB storage 0.275 USD per GB-month, the Glue Data
Catalog 1 USD per million requests. 1 GB = 2^30 bytes (the course convention), 1 TB = 2^40 bytes. CAD at 1.45 per
USD, the course's planning rate. Sizes *measured* offline with DuckDB over the DS01 file; Data scanned *assumed* (a
live run measures it); costs *computed*. These are estimates, not a spending cap.

| Part | One run: 30 days of DS01, lake and a new DynamoDB table | 30 days of keeping it | A year of keeping it |
|---|---|---|---|
| Athena CTAS over one month partition of `rides`, about 0.48 GB (*assumed*: the average month of 17.4 GB over 36 months, not a maximum; every column is read for the hash; read `DataScannedInBytes` of your own query) | 0.0026 | — | — |
| Athena count of the source per day, the same partition (at most) | 0.0026 | — | — |
| Athena counts of `trips_history` (two) and the view, at the 10 MB minimum each | 0.0002 | — | — |
| S3 PUT and LIST, about 60 (30 Parquet files, Athena results, the import's files, listings; *assumed*) | 0.0003 | — | — |
| DynamoDB import, 210,164,405 bytes of DynamoDB JSON = 0.196 GB | 0.0323 | — | — |
| Glue Data Catalog, about 100 requests and 35 objects | 0.0001 | 0.0004 | 0.004 |
| S3 storage: Parquet 13,472,122 bytes and the import's gzip files 18,102,047 bytes = 0.029 GB | — | 0.0007 | 0.009 |
| DynamoDB storage: 524,629 items of about 204 bytes plus 100 bytes of per-item overhead (*assumed*) = 0.149 GB | — | 0.0408 | 0.490 |
| **Total** | **0.038 (0.055 CAD)** | **0.042 (0.061 CAD)** | **0.54 with the run (0.78 CAD)** |

Against the 30 USD lease budget: one run 0.13%, 30 days 0.14%, a year 1.8%. The same 524,629 trips written with
BatchWriteItem take 524,629 write units (each item under 1 KB): 0.36 USD, 11 times the import; the count scan after
it reads about 0.1 GB at one read unit per 4 KB, 26,200 units, 0.0036 USD. The Athena lines grow with the months
copied: each month is read once by its statement and once by the source count.

**Measured** (one live run, `output/ext-backfill.txt` and `output/ext-backfill-session-2026-11-23.txt`, 2024-03-01 to 2024-04-02, 574,665 rows; Data scanned from each query's
`GetQueryExecution`, billed per query rounded up to the MB with a 10 MB minimum, failed queries and DDL free; the
import's `ProcessedSizeBytes` from `DescribeImport`):

| Part | Units, *measured* | USD | CAD |
|---|---|---|---|
| Athena, first run (count, CTAS of 30 days, count) | 85,619,001 + 509,127,053 + 0 bytes: 578 MB billed | 0.0030 | 0.0044 |
| Athena, every run of the session (5 runs, 15 queries billed) | 1,897,900,227 bytes: 1,875 MB billed | 0.0098 | 0.0143 |
| DynamoDB import, 574,665 items | 249,742,173 bytes (0.233 GB; 434.6 B a line, the offline estimate was 400.6) | 0.0384 | 0.0557 |
| BatchWriteItem, 2024-04-02 into the existing table | 21,070 items in 843 calls, no item unprocessed: 21,070 write units | 0.0145 | 0.0210 |
| The count scan after it (strongly consistent, every page) | about 28,000 read units (*computed* from the import's size) | 0.0039 | 0.0056 |
| **The session** | | **0.067** | **0.097** |

Against the 30 USD lease: 0.22%. The CTAS of one month read 509 MB, every column of one month partition: the
filter on `pickup_datetime` kept Athena to March (the April days read 207 to 239 MB, of March and April). A line of
the import's DynamoDB JSON is 434.6 bytes live against 400.6 offline, so the import costs 8% more than the table
above says.

Clean up from the terminal with your lease's credentials, in `project-skeleton/`, with the same `PREFIX`. Run this
extension's clean-up before the project-skeleton `clean_up.py`: that one deletes the whole bucket and database, but
it does not know `<PREFIX>-trips-by-id`, which would be left billing for its storage.

```bash
export PREFIX=backfill-yourname
uv run python ext-backfill/clean_up.py            # lists what it would delete; ends "Nothing deleted. Run again with --yes"
uv run python ext-backfill/clean_up.py --yes      # deletes, then asks each service again
```

With `--yes` it deletes `<PREFIX>-trips-by-id` (the whole table) when `backfill.py` created it; a table that was
there before the load (recorded `borrowed`) is kept with its backfill items, each marked with its `load_id`. It
deletes the Glue tables `trips_history`, `history_raw` and the view `trips_all`, the objects under `history/trips/`
and `imports/trips-by-id/` and `history/trips.source`; it then asks each service again and prints
`FAILED: still there` for anything left. It keeps the bucket, its Athena results and your files under
`raw/history/`, the two workgroups, the live table `trips` and the data source `course`: they belong to the project,
and the bucket keeps billing for what it stores until the project-wide clean-up (`scripts/lake.py down` for the
bucket and workgroups, last). The import's error log group `/aws-dynamodb/imports` is written by DynamoDB for every
import of the account and is kept; `backfill.py` sets its retention to 7 days, so do not run the import where that
log group must keep its lines longer. Its stored lines bill until they expire.

## Adapt it

1. **Your files.** Change `history.toml`: the format, every column with its type, the time column and its format,
   your stable id column if you have one, and `[map]`. Then `--source files --files "$HISTORY_S3_URI"`.
2. **Your columns.** `HISTORY_COLUMNS` and `TYPES` in `statements.py` are the columns of `trips_history`; the view
   and `dynamo_load.to_item` follow them.
3. **Your filter.** `where` in `history.toml`; for the course table, `course_source()` in `statements.py`.
4. **Your key.** `KEY` in `dynamo_load.py` and the table's key schema; a second access pattern is a global secondary
   index (GSI, a second key DynamoDB keeps for lookups), given in `TableCreationParameters` of the import.
