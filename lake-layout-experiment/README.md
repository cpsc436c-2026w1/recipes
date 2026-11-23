# Lake layout experiment

## What you build

The same trips table in four layouts — Hive folders and Iceberg, each without and with partitions on the column the
queries filter on — at three sizes. The three zone-count queries run 10 times each in Athena on all four layouts,
and in DuckDB in a Lambda function on the `hive-by-day` layout only, in one seeded order; engine comparisons
therefore use that layout. Every run is kept in `results.duckdb`; one SQL file, `summary.sql`, turns them into the
summary: for each cell (layout, size, query, engine) the median, the interquartile range (IQR, the middle half of the
runs) and the 90th percentile (p90, the time at or below which 90% of the runs fall) of the time to the last row, the
median Data scanned beside the billed MB, overlapping ranges marked, and the cost of 1,000 queries.

## Use it when

Your system answers queries from a table in S3 through Athena (or DuckDB in a Lambda function) and your experiment
compares two layouts of that table: you want to pick the layout before the table grows and to know whether the
layout changes the bill. It is worth having the build and one size by Checkpoint 2 (Tue Dec 1) and the full run for
the final hand-in (Tue Dec 15). The experiment write-up in the course template is `experiments/lake-layout.md`.

## Main scenario

1. `experiment.py build` registers the course catalog as the data source `course` if it is missing, and writes
   `<prefix>_layout.ds01` (DS01, the March trips table) with one CTAS (CREATE TABLE AS SELECT, a query that writes
   its result as a new table) from `course.course_rides.rides`: March 2024 (one month partition), base
   `HV0003` (Uber), pickup zones in Manhattan; it checks 6,045,943 rows.
2. It grows the table: at 1, 2, 4 … copies of DS01 it builds the two by-day tables and runs the one-day query once
   in each (the probe). It stops at the first size whose probe passes 10 MB, or earlier when the estimated Athena
   spend of the 360 measured runs of the plan would pass `MAX_USD` (default 2.00 USD), and keeps three sizes: 1, k
   and 4k copies.
3. It builds the four layouts at each size (CTAS in `<PREFIX>-ctas-wg`, Parquet with ZSTD) and checks each table's
   row count, k × DS01; it packages and deploys the function `<PREFIX>-duckdb-query`. Copy n of DS01 holds, on each
   day d of March, the trips of day d + n − 1 (wrapping after the 31st), and a flat table of more than 1 copy is
   written from the Hive by-day table of its size: copies with the same rows next to each other took little more room
   than one, so a table of 64 such copies read no more than one of 8 (measured, `output/live.txt`; why Parquet
   stores them so small is *unchecked*; `layout.rotation_days`, `layout.build_statements`). The build stops when a
   final table's whole-table scan does not grow with its copies.
4. `experiment.py run` sends 360 Athena runs (in `<PREFIX>-wg`, result reuse off) and 90 DuckDB calls, one at a
   time, in one order shuffled with seed 436, and stores each in `results.duckdb`.
5. `experiment.py summary` runs `summary.sql` and writes `summary.md`.
6. `experiment.py late` inserts one late row into the four tables of 1 copy and checks that the day's count is one
   higher in all four. `experiment.py clean` lists what it would drop; `experiment.py clean --yes` drops what the
   build made.

## Decisions

| Choice | Instead of | Why | Constraint or best practice (source) |
|---|---|---|---|
| Four layouts named `hive-flat`, `hive-by-day`, `iceberg-flat`, `iceberg-by-day` | letters A and B | a name says what the layout is; the Iceberg pair is there because most project lakes serve Iceberg tables | L10 (Iceberg), L11 (partitions) |
| Iceberg `partitioning = ARRAY['day(pickup_time)']`, Hive `partitioned_by = ARRAY['dt']` | the same column for both | Iceberg partitions by a transform of a column; Hive needs the partition column last in the `SELECT` | [CTAS properties](https://docs.aws.amazon.com/athena/latest/ug/create-table-as.html); students wrote an Iceberg CTAS from the course catalog in tutorial B3T1 |
| `write_compression = 'ZSTD'` in every layout | each format's default | the Hive CTAS default for Parquet is GZIP and Iceberg's default changes with the engine version; one codec keeps the layout the only difference | same page; [Iceberg table properties](https://docs.aws.amazon.com/athena/latest/ug/querying-iceberg-creating-tables.html) |
| DS01 read from `course_rides.rides` | `course_rides.rides_flat` | `rides` is partitioned by month, so the March CTAS reads one month; `rides_flat` is the original files without partitions | tutorial B3T1 |
| Copies on the same dates, `copy_no` 1 to k | real later months, or copies moved in time | the one-day query reads one day, so more days never grow it; a wider table (more stations, more topics) grows each day. Dated copies (`--copies dated`) stay an option for a system whose smallest query is the whole table | ours: the smallest query must grow with the table |
| Doubling until the probe passes 10 MB, then 4 times that, with `MAX_USD` on the plan's measured runs | fixed sizes of 1, 4 and 10 times | fixed multiples do not reach 10 MB for a small table, and doubling without a limit could spend the lease; `MAX_USD` bounds the estimated Athena spend of the 360 measured runs over the plan's three sizes, computed from measured table bytes before each doubling and again before the final tables. Build queries, probes, Lambda, S3 and storage cost extra, and it is not an AWS spending cap | course budget: 30 USD a group lease |
| CTAS of 100 days, then INSERT INTO of at most 100 days | one CTAS | "When you use CTAS to create a partitioned table, Athena has a write limit of 100 partitions"; DS01 has 31 days, so only dated copies reach it | [CTAS considerations](https://docs.aws.amazon.com/athena/latest/ug/ctas-considerations-limitations.html), [CTAS and INSERT INTO](https://docs.aws.amazon.com/athena/latest/ug/ctas-insert-into.html) |
| CTAS in `<PREFIX>-ctas-wg` (not enforced), measured runs in `<PREFIX>-wg` (enforced), both from `ensure_workgroup` of `scripts/lake.py` | one workgroup of the recipe's own | a CTAS that names its location fails in a workgroup that enforces a results location; the enforced one keeps every measured result under `athena-results/` for 7 days | course convention: two shared workgroups, one helper |
| Query result reuse off on every run | the workgroup default | with reuse on, Athena returns an earlier result of the same query instead of running it, and the run would time the stored result | [ResultReuseConfiguration](https://docs.aws.amazon.com/athena/latest/APIReference/API_ResultReuseConfiguration.html) |
| All cells interleaved in one seeded order | 10 runs of one cell, then the next | a slow minute of the service then touches many cells a little, not one cell entirely; the seed gives the same order on a re-run | ours: a slow minute should not decide a cell |
| Time from the send to the last result row for both engines, service times kept beside it | Athena's `TotalExecutionTimeInMillis` against Lambda's duration | the two service times start and end at different points; the same clock at the caller compares like with like, and engine, queue and planning times still separate the engine's work from queueing | [QueryExecutionStatistics](https://docs.aws.amazon.com/athena/latest/APIReference/API_QueryExecutionStatistics.html) |
| Billed MB = Data scanned rounded up to whole MB, at least 10; a cancelled run billed for what it scanned, a reused result 0 | Data scanned alone | "rounded up to the nearest megabyte, with a 10 MB minimum per query"; "Canceled queries are billed for the total amount of data scanned when the query is canceled" | [Athena pricing](https://aws.amazon.com/athena/pricing/) (read 2026-11-21) |
| Excluded runs counted in the spend | spend from kept runs only | a run cancelled by the cutoff has scanned data by then; the summary's times leave it out, the spend does not | as above |
| Median (IQR) and p90 per cell, `quantile_cont` | a range across cells, a mean | the course reports medians with their spread; with 10 runs, p90 lies between the 9th and 10th run and is shown, not compared | L11 |
| DuckDB in Lambda, 2,048 MB, one call at a time | more memory, or parallel calls | at 1,769 MB a function has the equivalent of one vCPU; the account's concurrent executions are shared with the team's other functions (check the Lambda quota "Concurrent executions" in Service Quotas, ca-central-1, for your lease's value) | [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) |
| DuckDB 2.0.1 with `httpfs` in the package, an S3 secret from the function's environment, no `aws` extension | the `aws` extension's credential chain | without `aws` the package is about 29 MiB zipped and 85 MiB unzipped, under the 50 MB direct upload and 250 MB limits <!--src: measured:measured/package-sizes-2026-11-21.txt --> | Lambda quotas |
| DuckDB reads the `hive-by-day` tables only | all four layouts | reading Iceberg needs DuckDB's `iceberg` extension, whose size in the package is not measured; compare engines on a Hive copy or measure that extension first | — |
| Results compared as rows keyed by zone | the rows in the order returned | `GROUP BY` does not order rows, so two right answers can come in different orders | — |
| Billed duration read from the `REPORT` line, which includes the init phase | duration plus a separate init charge | a captured cold call ran 316.02 ms with an Init Duration of 493.18 ms and was billed 810 ms <!--src: receipt:../lambda-dynamodb/output/ext-metrics.txt --> | L2 (cold starts) |
| Timeout 300 s | the default 3 s | a whole-table query of the largest size reads every file; Lambda's maximum is 1,800 s ([timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)) | Lambda docs |

## Requirements

Each requirement is one sentence about the recipe, in an EARS form (in brackets): ubiquitous, event (When),
state (While), unwanted (If … then), optional (Where), or complex (two of them). The scenarios are in
`features/lake_layout.feature`, tagged with the number; `uv run behave` runs them offline (those tagged `@live` run
in AWS with `LIVE=1`). A team keeps 2 to 5 lines per story.

- **R10** (event) When `experiment.py build` runs, the recipe shall write DS01 into four layouts, `hive-flat`,
  `hive-by-day`, `iceberg-flat` and `iceberg-by-day`, the two `by-day` layouts partitioned by the pickup day (Hive on
  the column `dt`, Iceberg by `day(pickup_time)`; the queries filter Hive by-day on `dt` and the others on the same
  one-day range of `pickup_time`), with the same columns and the same rows in all four layouts of a size.
- **R20** (event) When the build grows the table, the recipe shall double the copies of DS01 until the one-day query
  in the `by-day` layouts scans more than 10 MB, and then build the sizes 1 copy, that number of copies and 4 times
  that number (1, 2 and 4 copies when the first copy passes), and stop without a plan when a doubling grows the probe
  by less than half (from 0.1 MB up: the copies took no room, so they were not added).
- **R30** (unwanted) If the upper bound of the Athena spend of the 360 runs for the plan the next doubling leads to
  (sizes 1, 2k and 8k) would be more than `MAX_USD` (default 2.00 USD), then the recipe shall stop the doubling, keep
  the plan 1, k and 4k (1, 2 and 4 at k = 1), say `10 MB not reached` in the summary, and build that plan only if its
  own bound, computed again before the final tables, is within `MAX_USD`, else build no final tables and say
  `no plan within MAX_USD`.
- **R40** (complex) Where copies are dated, when a `by-day` table has more than 100 days, the recipe shall write it
  with one CTAS of its first 100 days and then INSERT INTO statements of at most 100 days each.
- **R50** (event) When `experiment.py run` runs, the recipe shall run each Athena cell (layout, size, query) 10 times
  in the workgroup `<PREFIX>-wg` with query result reuse turned off, in one order shuffled with a fixed seed over
  all cells.
- **R60** (event) When an Athena run finishes, the recipe shall store in `results.duckdb` its cell, run number, query
  execution id, `DataScannedInBytes`, the billed MB, the engine, queue and planning times, and the time from sending
  the query to receiving the last result row.
- **R70** (unwanted) If a run failed, was cancelled by the workgroup's scan cutoff, or reports a reused result, then
  the recipe shall keep it in `results.duckdb` marked excluded with the reason, leave it out of the times, and still
  count its Data scanned in the run's spend.
- **R80** (event) When all runs are stored, the recipe shall write for each cell, from one SQL query over
  `results.duckdb`, the number of runs kept, the median (IQR) and p90 of the time to the last row and of the engine
  time, the median Data scanned and billed MB side by side, and the run's spend.
- **R90** (event) When the summary is written, the recipe shall mark two cells that differ only in layout, or only
  in engine, as `ranges overlap` when the IQRs of their times to the last row overlap.
- **R100** (complex) Where the DuckDB engine is used, when `experiment.py run` runs, the recipe shall run the same
  queries on the `hive-by-day` tables 10 times each in the Lambda function `<PREFIX>-duckdb-query`, one call at a
  time, and store each call's time from the call to the answer, billed duration, init duration (empty on a warm
  call), S3 GET requests and result rows.
- **R110** (unwanted) If the rows of a DuckDB result, compared by zone, differ from the Athena result of the same
  query on the same table, then the recipe shall mark the DuckDB cell `results differ` and leave it out of the
  comparison.
- **R120** (event) When the summary is written, the recipe shall give each cell's cost of 1,000 queries from its
  medians and the course price sheet: Athena from the billed MB, DuckDB from billed GB-seconds, requests and S3 GET
  requests.
- **R130** (complex) Where the late-row check is used, when `experiment.py late` runs, the recipe shall insert one row
  dated an earlier day into the four tables of the smallest size and show that the one-day count of that day is one
  higher in all four.
- **R140** (event) When `experiment.py clean --yes` runs, the recipe shall drop the tables and the database
  `<prefix>_layout`, delete the prefix `layout/` in the bucket, the function, its log group and its role, and keep
  the bucket, the data source `course`, both shared workgroups and `results.duckdb`.
- **R141** (event) When `experiment.py clean` runs without `--yes`, the recipe shall list each table of
  `<prefix>_layout` and the number of objects under `layout/`, and delete nothing.

## Data

- **DS01**: the High Volume For-Hire Vehicle trip records of the NYC Taxi and Limousine Commission, Uber trips picked
  up in Manhattan in March 2024, 6,045,943 rows, 0.20 GB as Parquet <!--src: measured:cp1-example DuckDB 2026-11-19 -->,
  read from DS03 into `<prefix>_layout.ds01`.
- **DS02**: the course catalog's `course_rides.zones`, the TLC taxi zone lookup (zone id, zone, borough).
- **DS03**: the course catalog's `course_rides.rides`: the same TLC records for every base, January 2023 to
  December 2025, an Iceberg table partitioned by the month of `pickup_datetime` (tutorial B3T1). This recipe reads
  one month of it.
- **SIM01 "two days"** (offline): five rows with DS01's columns, 3 on 2024-03-14 (2 in zone 161, 1 in zone 230) and 2
  on 2024-03-15 (zone 230), one local Parquet file (`sim_data.py`).
- **Copies**: k copies of DS01 have k × 6,045,943 rows; copy n moves each trip by the days `rotation_days` gives, so
  every day of March holds k different days' trips up to 31 copies (copy n + 31 repeats copy n), and `dt` stays the
  date of `pickup_time`. A flat table of more than 1 copy is written from the Hive by-day table of its size, and the
  build stops when a final table's whole-table scan is under 0.75 × k times one copy's. Measured: 506 MB for
  8 copies of a by-day table, 998 MB for 16 <!--src: measured:live build 2026-11-23 -->.
- **SIM02 "late row"**: one row, zone 161, picked up 2024-03-14 at 08:15 New York time, inserted after the build as
  if it arrived late (`experiment.py`, `SIM02`).

Table format, every layout: `pickup_time` (timestamp, from `pickup_datetime`), `pickup_zone_id` (from
`pulocationid`), `dropoff_zone_id` (from `dolocationid`), `trip_miles`, `trip_time_s` (from `trip_time`), `fare` (from
`base_passenger_fare`), `driver_pay`, `copy_no`, and `dt` (the date of `pickup_time`), last.

## Run it

> ⚠️ Commands run in two places. Offline commands run in a terminal on your laptop, in the copied recipe folder, and
> need no AWS account. Live commands run in a laptop terminal in the same folder, with your lease's credentials
> active, in `ca-central-1`.

Offline, no account: DuckDB writes the four layouts as local folders (the Iceberg pair is a labelled stand-in: data
files and a metadata file with the partition spec and one snapshot per write; real Iceberg is checked live) and runs
the queries in place of Athena and of the function. From your laptop, in a terminal in the recipe folder:

```bash
uv sync                                         # installs the dependencies; it should finish without an error
uv run pytest -q && uv run behave               # every test and scenario should pass
uv run python experiment.py build --offline     # SIM01; sizes 1, 2 and 4 (offline the copies stop at 4)
uv run python experiment.py run --offline       # 360 + 90 runs into build/local/results.duckdb
uv run python experiment.py summary --offline   # writes build/local/summary.md
uv run python experiment.py late --offline      # prints "agree": true when the four tables agree
uv run python experiment.py clean --offline --yes
```

## Run it live

From a laptop terminal with your lease's credentials, in the recipe folder. Set the prefix, the region and the
spend limit before the build (the build chooses its plan with `MAX_USD`; a different limit needs a new build), and
check the account:

```bash
export PREFIX=layout-yourname
export AWS_DEFAULT_REGION=ca-central-1
export MAX_USD=2.00
aws sts get-caller-identity          # the Account must be your lease's; stop if it is not
```

The build reads DS01 from the course catalog through the Athena data source `course`. If `course` is already listed
under Data sources in your Athena console (tutorial B3T1 registers it), the build uses it. If not, set
`COURSE_CATALOG_ID` to the course catalog's account ID, the one tutorial B3T1's `setup_athena.sh` registers, on its
own line (`export COURSE_CATALOG_ID=YOUR_COURSE_CATALOG_ID`); without either the build stops with that message.

`bash run.sh` runs the whole sequence below and saves what it prints in `output/live.txt` (account masked);
`STEP=build bash run.sh` stops after the build and `STEP=run bash run.sh` goes on from there, adding to the file;
`DRY=1` prints the commands only. It ends with the clean-up: the listing, then `experiment.py clean --yes`, which
deletes the experiment's tables, `layout/` and the function. A build that stops part-way can be run again: it keeps
the tables it has. Step by step, from the same terminal:

```bash
uv run python experiment.py build                # DS01, the growth, the final tables, the function
uv run python -c 'import duckdb; print(duckdb.connect("results.duckdb", read_only=True).sql("SELECT * FROM growth"))'
uv run python experiment.py run                  # 450 runs into results.duckdb
uv run python experiment.py summary              # writes summary.md
LIVE=1 uv run behave -t @live                    # R10 in AWS (DS01's count and the four tables of 1 copy), before late
uv run python experiment.py late                 # prints "agree": true when the four tables agree
uv run python experiment.py clean                # lists what it would drop; deletes nothing
```

The `growth` table has one row per probe: `copies`, `probe_mb` (the MB the one-day query scanned), the `note`, the
`sizes` the plan keeps and `bound_usd`, the plan's Athena bound against `MAX_USD`. One live run took about 25 minutes to build (17 tables) and 40 minutes for 450 runs
(`output/live.txt`). The run takes about 450 queries one at a time, so expect most of an hour: run it from a laptop
terminal, not
CloudShell (CloudShell ends a session after 20–30 minutes without keyboard input). `run` writes the command and the
commit to `evidence/<run-id>/`. A rerun of `run` gets a new run id, and `summary.sql` reads the latest one only;
a call that fails is stored as an excluded run and the run goes on. The times have the resolution of the poll (0.1 s
for the measured runs); Athena's own engine, queue and planning times are kept beside them. Copy what `summary` prints into `output/summary-live.md` and section 7 of
`experiments/lake-layout.md`.

## What you should see

Offline (`output/offline.txt`, the same every run): the growth note `10 MB not reached: probe … MB at 1 copy (copies
capped at 4)` (SIM01 is five rows), 450 runs stored, 45 cells in `summary.md`, the four tables agreeing after the
late row.

On AWS, check these in your own run: the summary names the sizes your probes chose; it records 450 attempted runs
and marks any excluded run with its reason (look into an exclusion before you compare cells); DuckDB's rows equal
Athena's in every cell, or the cell is marked `results differ`; and the late row raises the day's count by one in
all four tables. One live run's output, for example (`output/live.txt`, `output/summary-live.md`): probes 0.16 to
1.24 MB at 1 to 8 copies, sizes 1, 8 and 32, 450 runs and none excluded, 7,645 then 7,646 trips in zone 161 on
2024-03-14. What the results table shows, and what follows from it, is for your write-up in
`experiments/lake-layout.md`.

## Cost and clean-up

Prices: USD, ca-central-1, free tier left out, from the course price sheet (page `436c-student/prices.md` on the
course server), copied with their date into `prices.toml`: Athena 5.50 USD per TB scanned with a 10 MB minimum a
query, S3, Lambda and Glue as the sheet lists them. Bytes are converted with 1 GB = 2^30 bytes and 1 TB = 2^40 bytes
(the course convention). CAD at 1.45 per USD, the course's planning rate. The table is *computed* before a run, so the sizes are
assumed: the growth rule picks them during the build. It is a conditional estimate at an assumed 0.49 MB a day a
copy (the pickup zone column), where the 10 MB probe is not reached and the 2.00 USD limit stops the doubling at 4
copies (sizes 1, 4 and 16; 0.2, 0.8 and 3.2 GB a layout). "Larger estimate": every query reads every column of
every file it may read. "Estimate": the queries read the zone column and, in the flat layouts, `pickup_time` (24 MB
a copy, assumed). The S3 request counts and the Lambda durations are assumed too, so neither row is a bound, and
neither is a spending cap. S3's default encryption bills no KMS requests.

| Part | One run (build, 360 Athena runs, 90 DuckDB calls, late check; tables kept a day) | 30 days (two runs; tables dropped after each) | A year (the two runs; the chosen layout at 1 copy kept 12 months) |
|---|---|---|---|
| Athena, build: DS01 from DS03 (0.57 GB, upper), 6 probe tables and 8 final tables from DS01 (0.2 GB each), 6 probes and 12 counts at 10 MB: 3.55 GB | 3.55 × 5.50 / 1,024 ≈ 0.019 | 0.038 | 0.038 |
| Athena, the 360 runs: 357.7 GB upper bound (estimate 46.7 GB, 0.25) | ≈ 1.92 | ≈ 3.84 | ≈ 3.84 |
| Athena, late check: 4 INSERT INTO and 4 queries at 10 MB | ≈ 0.0004 | ≈ 0.0008 | ≈ 0.0008 |
| S3 GET by Athena and DuckDB, about 1,000 a query (assumed), 476 queries | 476,000 × 0.00044 / 1,000 ≈ 0.21 | ≈ 0.42 | ≈ 0.42 |
| S3 PUT: about 2,000 table files and 450 result files (assumed) | 2,450 × 0.0055 / 1,000 ≈ 0.013 | ≈ 0.027 | ≈ 0.027 |
| Lambda, 90 calls × 5 s × 2 GB = 900 GB-s (assumed) and 90 requests | ≈ 0.012 | ≈ 0.024 | ≈ 0.024 |
| S3 storage, 17.8 GB (12 tables, 2 probe tables, `ds01`) | one day: 17.8 × 0.025 / 30 ≈ 0.015 | ≈ 0.030 | 0.030, and 0.2 GB × 0.025 × 12 = 0.06 |
| Glue Data Catalog: about 120 objects and 1,500 requests a run | ≈ 0.0015 | ≈ 0.003 | ≈ 0.003 |
| CloudWatch Logs of 90 calls, about 1 KB each | under 0.0001 | under 0.0002 | under 0.0002 |
| **Total, larger estimate** | **≈ 2.19 (3.18 CAD)** | **≈ 4.38 (6.36 CAD)** | **≈ 4.44 (6.44 CAD)** |
| **Total, estimate** | **≈ 0.52 (0.76 CAD)** | **≈ 1.04 (1.51 CAD)** | **≈ 1.10 (1.60 CAD)** |

Measured, one live run (`output/live.txt`; sizes 1, 8 and 32, not the 1, 4 and 16 assumed above): its Athena bill was
63,605 MB, 0.33 USD (0.48 CAD), of which the 450 measured runs, probes and checks 57,253 MB (0.30 USD) and the CTAS
and INSERT statements 6,152 MB (0.032 USD); the function's 90 calls 123.2 GB-s, 90 requests, 3,130 S3 GET and 90
LIST, 0.0035 USD. With an earlier run of the build and the S3 and Glue requests (not metered; *computed* as under
0.11 USD from the assumed counts above), the total was about 0.67 USD (0.97 CAD), 2.2% of the 30 USD lease.

Against the group lease budget of 30 USD, 30 days are about 15% at the larger estimate (3.5% at the estimate), and a
year about 15% (3.7%). Almost all of it is the whole-table and flat-layout queries at the largest size. `MAX_USD`
bounds the estimated Athena spend of the 360 measured runs before the final tables are built; the build queries,
probes, Lambda, S3 and storage (about 0.25 USD a run) come on top. No model calls.

Clean-up, from the same terminal with the same `PREFIX`. Without `--yes` it lists, with read-only calls, each table
of `<prefix>_layout` and the number of objects under `layout/`, and deletes nothing (`--plan` prints the plan by name
with no AWS call):

```bash
uv run python clean_up.py          # lists what it would delete (also: experiment.py clean)
uv run python clean_up.py --yes    # delete (R140)
```

Read the list before `--yes`: every table and the whole `layout/` prefix must be this experiment's. The names come
from your `PREFIX`, so another recipe's data is there only if it used the same database or prefix; if anything on the
list is not the experiment's, stop and delete the experiment's own resources by hand. With `--yes` it drops the
tables and the database `<prefix>_layout` by name (Athena DDL makes them without tags), deletes the prefix `layout/`,
the function, its log group and its role. It keeps the bucket, the data source `course` (recorded borrowed even when
the build registered it), both shared workgroups (`scripts/lake.py down` deletes those the ledger records created,
after every recipe's clean-up), and `results.duckdb` and `summary.md`. It also keeps `athena-results/`: when
`scripts/lake.py up` created the bucket, a lifecycle rule expires those results after 7 days. If the bucket was made
another way, check its Management tab in the S3 console for that rule (and, on a versioned bucket, a rule for
noncurrent versions); without it the results keep billing for storage until you delete them.

## Adapt it

- The filter column: the pickup day here. For stations or regions, name your column in `layout.py` (`query_sql`,
  `with_clause`) and partition the Iceberg table on it with the identity or `bucket` transform.
- Your table instead of DS01: change `extract_ds01` in `backends.py` and the expected row count in `experiment.py`.
- The cutoff of `<PREFIX>-wg` is 1 GiB (2^30 bytes) a query, the setting of the shared workgroups in
  `scripts/lake.py`. The whole-table query of the largest size may read up to 3.2 GB at the larger estimate; a run
  above the cutoff is cancelled, billed for what it scanned, and marked excluded (R70). Before the measured run,
  compare the cutoff with the largest planned query in the `growth` table. Changing the setting in `scripts/lake.py`
  does not change a workgroup that exists (`ensure_workgroup` never changes one, and stops on a mismatch), and the
  same setting is copied in every recipe that shares the workgroup (`tools/shared_helpers.toml`). Raising it is a
  decision for the whole project: agree it with your team, change every copy, and recompute the plan's bound.

## S3 request-cost assumptions

- Assumed: about 1,000 S3 GET requests per Athena query; the run measures them only if the bucket's request metrics
  are on, which bills as CloudWatch metrics (0.30 USD per metric-month, price sheet). Not turned on in the live run.
