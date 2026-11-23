# Extension: compaction by a rule, snapshot expiry, on a schedule

**Use this if your system** writes small files into a lake table (a stream consumer, a Firehose delivery or an hourly
`MERGE`) and a query reads many days of them. Needed by Checkpoint 2 (Tue Dec 1) for the rule and the nightly run,
and at the final hand-in (Tue Dec 15) with the before-and-after measurement.

**What it adds.** Athena's `OPTIMIZE ... REWRITE DATA USING BIN_PACK` rewrites the many small files a streaming writer
commits into fewer, larger ones. `VACUUM` expires snapshots older than the table's 7-day limit in
`main/sql/create_table.sql` and deletes the files no snapshot uses. Each night a Lambda function counts the data files
of each day and compacts a day that has closed with more files than a query over your window may read per day: by
`OPTIMIZE ... WHERE <that day>` for an Iceberg table (`trip_changes`, and `trips` of `ext-scheduled-merge`), and for
the plain Parquet table `trip_events_parquet` (raw deliveries landed as Parquet, as Firehose's record format conversion
writes them) by one CTAS (`CREATE TABLE AS SELECT`: an Athena query that writes its result as new Parquet files) of
the day under a new prefix, and a switch of the day's partition to that prefix in the Glue Data Catalog. Two alarms e-mail
you when a night fails or does not run. `maintenance.py --target aws` still measures Data scanned and run time of one
query before and after one `OPTIMIZE` of the whole change log.

**What changes in the main scenario.** Step 5's table has fewer files and fewer snapshots. With `--schedule`, the
function `$PREFIX-compact-days` runs at 03:00 (the rule) and 04:00 (`VACUUM`) Vancouver time every night until
`ext-maintenance/clean_up.py --yes` removes it. It replaces the earlier `$PREFIX-lake-optimize` schedule, which
started an `OPTIMIZE` of the whole table every night.

```
(03:00 Vancouver) Scheduler group <PREFIX>-maintenance-compaction -> Lambda <PREFIX>-compact-days
  -> Athena "<table>$partitions" and S3 ListObjectsV2 -> for each closed day over the rule:
     Iceberg: OPTIMIZE ... WHERE <day>; Parquet: file list -> CTAS of the day -> count -> Glue UpdatePartition -> count
  -> run log <PREFIX>-runs
(04:00) the same function: VACUUM of each Iceberg table.  Alarms on the function -> SNS <PREFIX>-maintenance-alerts
```

Words used here. *Compaction*: rewriting many small files of a table into a few large ones, with the same rows.
*Snapshot*: a past version of an Iceberg table. *Partition projection*: an Athena table setting that computes the
partitions from a pattern instead of reading them from the Glue Data Catalog. *Closed day*: a day that ended more
than `CLOSE_AFTER_HOURS` before the run. *Moved day*: a day of `trip_events_parquet` whose partition now points under
`compacted/trip-events/`.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Why maintain at all (R110, R111) | Compaction and expiry | One commit per buffer interval leaves many small files and snapshots. A query opens the files it reads and reads more metadata | L11 (small files); Firehose docs, [Iceberg tables: compaction and storage optimization](https://docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-considerations.html) |
| How to compact (R112, R122) | Athena `OPTIMIZE ... BIN_PACK` | Runs where the table already lives and is billed as Data scanned; does not need a cluster | Athena docs, [OPTIMIZE](https://docs.aws.amazon.com/athena/latest/ug/optimize-statement.html) |
| How long to keep snapshots (R114) | 7 days (`vacuum_max_snapshot_age_seconds`), at least 5 snapshots (course default; Athena's defaults are 5 days and 1) | Time travel and rollback (ext-rollback-or-schema) are available only for snapshots that are kept | Athena docs, [VACUUM](https://docs.aws.amazon.com/athena/latest/ug/vacuum-statement.html); Data Analytics Lens [BP 13.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-13.html) (remove unused data) |
| When to compact (R120, R121) | A rule in files a query reads: a closed day over `MAX_FILES_PER_QUERY` / `QUERY_DAYS` (3,000 / 30 = 100) files. 3,000 is this recipe's default budget of files a query may open, not an AWS limit: set `QUERY_DAYS` to your query's window and choose the budget from the files and run time you accept | The cost and the time of a query grow with the files it opens (at least one GET each, 0.00044 USD per 1,000). A writer every 15 minutes makes 96 files a day; with a year of trend queries (`QUERY_DAYS` 365) the same 3,000 gives 8 a day, where a fixed 100 would leave 35,040 files a query. 3,000 files cost 0.00132 USD a query in GETs: 0.04 USD a month at one query a day, 11.40 USD at one every 5 minutes, so a dashboard query wants a lower number <!--src: assumed --> | Course price sheet `436c-student/prices.md` on the course server (S3 GET); ours: the limit starts from the query |
| Which days (R121) | Closed days only, at most `COMPACT_MAX_DAYS` (10) a run, oldest first; none started with under 120 s of the 900 s left | A day still being written gets new files after the rewrite; the cap keeps a run under the function's timeout, so a first night with 30 days of backlog takes 3 nights (`--measure --all-days` does it at once from a terminal) | ours |
| Who decides (R113, R120) | A function between the schedule and Athena | A rule per day needs a count first and a statement per marked day; a schedule's universal target sends one fixed statement | EventBridge Scheduler docs, targets |
| The bounds of an Iceberg day (R122) | `OPTIMIZE ... WHERE <column> >= TIMESTAMP '<day> 00:00:00' AND <column> < TIMESTAMP '<next day> 00:00:00'`; if refused, no `OPTIMIZE` of that table and an e-mail | The docs allow "only partition columns" in the `WHERE`; Athena accepts whole-day bounds on the source column of the hidden partition `day(change_time)`: the `@live` example compacted 1,440 files of a day into 1 and left the other day's 40 (`output/behave-live.txt`); a `WHERE` on another column fails with "Unexpected FilterNode found in plan" (`output/athena-probes.txt`). A whole-table `OPTIMIZE` as the fallback would rewrite days the rule did not mark and bill the whole table's Data scanned | Athena docs, [OPTIMIZE](https://docs.aws.amazon.com/athena/latest/ug/optimize-statement.html) |
| Counting an Iceberg table's files per day (R120) | `"<table>$partitions"`: `partition.<column>_day` (a date) and `file_count` | In Athena `"<table>$files"` has no partition column, and lists data files only; `$partitions` counts the data files of each day (`output/athena-probes.txt`) | Athena docs, [Iceberg metadata tables](https://docs.aws.amazon.com/athena/latest/ug/querying-iceberg-table-data.html) |
| A refused OPTIMIZE or a table that cannot be counted (R120, R122, R141) | Recorded, the other tables done, then one error raised naming every refused or uncounted table | A caught error does not count in Lambda's `Errors` metric, so the alarm would not send | Lambda docs, function metrics |
| Projection (R130, R131) | Refuse a raw table with projection on; `--register-partitions` once | With projection on, Athena ignores the partition locations in the catalog, so a switch would not change what Athena reads, and the late-file step would delete files queries still read. The switch registers the existing days and the next 7 | Athena docs, [partition projection](https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html) |
| The catalog change (R132, R140) | Glue `UpdatePartition` and `BatchCreatePartition` | The same catalog change as Athena's `ALTER TABLE ... SET LOCATION` (that the DDL calls the same Glue actions is *assumed*); one catalog change, so a query reads the old files or the new ones, never both; the call runs offline under moto | Athena docs, [ALTER TABLE SET LOCATION](https://docs.aws.amazon.com/athena/latest/ug/alter-table-set-location.html) |
| Trusting a copy (R132, R133) | Count the rows of the new files and of the day before the switch, and through the table after it | A failed CTAS can leave partial files; the second count shows the table reads the new prefix only | Athena docs, [CTAS considerations](https://docs.aws.amazon.com/athena/latest/ug/ctas-considerations-limitations.html) |
| Late files (R134) | Record the day's file list; copy a later file into the day's location (`late-<name>`) before deleting raw files, and delete a moved day's raw files 24 hours after the move | Firehose names an object by the arrival time of the oldest record in it and retries a failed delivery to S3 for up to 24 hours, so a file can land in a day's folder after the day moved; a query that started on the old location can finish | Firehose docs, [S3 prefixes](https://docs.aws.amazon.com/firehose/latest/dev/s3-prefixes.html), [delivery retries](https://docs.aws.amazon.com/firehose/latest/dev/retry.html) |
| With `ext-scheduled-merge` (R134) | Move or delete a raw file only when the merge's manifest of its day lists it as `merged`; a day's raw files only when it lists all of them | The merge chooses its objects by key against these manifests, so they say exactly which files are in `trips`; a file moved before it is merged would never reach `trips`. 30 manifests a night add about 30 GETs | ours: the merge's manifest is the record of what it took |
| Deleting raw files (R134) | `raw_delete` = `started` before the first `DeleteObjects`, `done` after the last; a day left `started` is finished first by the next run | S3 deletes are not atomic; deleting a key that is already gone succeeds | S3 API reference, DeleteObjects |
| Where the CTAS runs (R132) | `<PREFIX>-ctas-wg`, not enforced | In a workgroup that enforces its settings a CTAS is written under the results location, where the 7-day rule deletes it | Athena docs, [CTAS considerations](https://docs.aws.amazon.com/athena/latest/ug/ctas-considerations-limitations.html); L11 (CTAS) |
| Files a day after the CTAS (R132) | 1 bucket by `trip_id` | Athena writes "one or more files per bucket per partition": few files, not exactly one; the measured runs wrote one file for each day (2,515 to 17,292 rows; `../output/behave-live.txt`) | Athena docs, [bucketing](https://docs.aws.amazon.com/athena/latest/ug/ctas-partitioning-and-bucketing-what-is-bucketing.html) |
| Measuring (R135) | A spend estimate and a stop over `MAX_MEASURE_USD` (0.10) | The before-query over 43,200 files is 0.019 USD in GETs; run every 5 minutes for 30 days it would be 164.68 USD, five times the lease <!--src: assumed --> | ours |
| Missed nights (R140, R141) | 7 days registered ahead; alarms on `Errors` and on `Invocations` (missing data breaching) | 3 failed nights would make new days invisible and counts drop quietly; an alarm on `Errors` misses a night that did not run | CloudWatch docs, missing data |
| Alternative (RQ) | Our own nightly run, not Glue table optimizers or S3 Tables | Both compact without our code. Glue optimizers apply to Iceberg tables only and need a role; S3 Tables bill compaction at 0.0022 USD per 1,000 objects and 0.005 USD per GB processed (about 0.096 USD for 30 days of 1,440 objects a day) and need the table moved into a table bucket. Neither handles the plain Parquet table <!--src: assumed --> | Glue docs, [table optimizers](https://docs.aws.amazon.com/glue/latest/dg/optimizer-notes.html); course price sheet `436c-student/prices.md` |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature` (the first five), `features/compaction.feature` and
`features/on_call.feature` (the last two). A team keeps 2 to 5 per story.

- **R110** (complex) Where `ext-maintenance` is used, when `maintenance.py` has compacted the table, the recipe shall
  return the same answer to the measured query from fewer data files.
- **R111** (complex) Where `ext-maintenance` is used, when `maintenance.py` has expired the old snapshots, the recipe
  shall show that a query at the first snapshot is no longer possible.
- **R112** (complex) Where `ext-maintenance` is used, when `maintenance.py --target aws` runs, the recipe shall run
  the measured query, `OPTIMIZE`, `VACUUM` and the measured query again in Athena, and print Data scanned and run
  time before and after.
- **R113** (complex) Where `ext-maintenance` is used, when `maintenance.py --target aws --schedule` runs, the recipe
  shall create, in a tagged schedule group, two schedules that call the function `$PREFIX-compact-days` at 03:00 and
  04:00 Vancouver time every night, without retries.
- **R114** (optional) Where `ext-maintenance` is used, the recipe shall keep the table's snapshots for 7 days, and
  at least 5 of them.
- **R120** (event) When the nightly schedule fires, the recipe shall count the data files of each day of the last 30
  days of `trip_events_parquet` and of each table in `ICEBERG_TABLES` (the whole table for one without a day
  partition) and write the counts to the run log.
- **R121** (unwanted) If a day has closed and holds more data files than `MAX_FILES_PER_QUERY` / `QUERY_DAYS`, then
  the recipe shall compact that day, at most `COMPACT_MAX_DAYS` days a run, oldest first, and no other day.
- **R122** (event) When a day of an Iceberg table is compacted, the recipe shall run `OPTIMIZE ... REWRITE DATA USING
  BIN_PACK` with a `WHERE` that selects that day only (without a `WHERE` only for a table without a day partition),
  and on a refused `WHERE` run no `OPTIMIZE` of that table and end the run with an error that names it.
- **R130** (unwanted) If `trip_events_parquet` has partition projection on, or its files are not Parquet, then the
  recipe shall stop before it changes anything, with an error that names the cause.
- **R131** (event) When `maintenance.py --register-partitions` runs, the recipe shall turn partition projection off
  on `trip_events_parquet`, register the partition of every day folder under `raw/trip-events/` and of the next 7
  days, and print how many it registered.
- **R132** (event) When a day of `trip_events_parquet` is compacted, the recipe shall record the day's file list,
  write the day with one CTAS under `compacted/trip-events/dt=<day>/<run>/`, and point the day's partition at that
  prefix only if the new files hold the day's row count, counting the day again through the table after the switch.
- **R133** (unwanted) If the CTAS of a day fails, the day's files change during the copy, or a count before or after
  the switch differs from the day's count, then the recipe shall point the partition at its old location, delete
  the new prefix, and record the failure with its error in the run log.
- **R134** (event) When a nightly run starts, the recipe shall copy into a moved day's location every file of its raw
  folder that is not in the day's recorded file list and delete the listed raw files of each day moved more than 24
  hours before, and with `MERGE_READS_RAW` move or delete a raw file only when the merge's manifest lists it as
  merged.
- **R135** (optional) Where `--measure` is given, the recipe shall print the estimated cost of the measurement, stop
  if it is over `MAX_MEASURE_USD`, and otherwise run the count by zone over 30 days before and after the compaction
  and print its Data scanned, run time and file count.
- **R140** (event) When a nightly run ends, the recipe shall register the partitions of `trip_events_parquet` for the
  next 7 days, each at its `raw/trip-events/dt=<day>/` folder, unless they are registered.
- **R141** (unwanted) If the function reports an error, or is not called, in a day, then the recipe shall send an
  e-mail to the address subscribed to `$PREFIX-maintenance-alerts`.

Assumptions (a team replaces them with its own answers): a file can land in a day's folder after the day ended
(Firehose retries for up to 24 hours; an event-day partition gets late events), and the run copies it; a count up to
a day old is acceptable while a compaction runs (a query during the switch reads the old files or the new ones); a
closed day holds up to 1,440 files with a 60 s buffer; `trip_events_parquet` is filed by arrival day (a team that
files by event day sets `CLOSE_AFTER_HOURS` to its late window).

## Data

DS01, the TLC High Volume For-Hire Vehicle trip records (Uber, Manhattan, March 2024): the one hour of its trips in
zones 48, 161 and 230 that is `ext-scheduled-merge`'s SIM01 (2,515 deliveries). `land_parquet.py` writes deliveries
as Parquet, one file a slot, as Firehose's record format conversion would. The scenarios run with their clock fixed
at 2026-11-23, so the dates below are those of the scenarios, not of your run; `land_parquet.py --days N` lands the N
days before the day you run it.

- **SIM01 thirty-days** (R131, R135): SIM01's hour landed on each day from 2026-10-24 to 2026-11-22 as 1,440 small
  files a day: 43,200 files under `raw/trip-events/dt=<day>/`, and 25,230 requests in all (9,150, 5,730 and 10,350
  in zones 48, 161 and 230: 305, 191 and 345 a day, repeated requests included). Offline the files are placeholders
  and the stand-in for Athena returns the counts.
- **SIM02 three-days** (R120, R121, R132): day 2026-11-21 as 1,440 files of 12 events (17,280 events), day
  2026-11-22 as 80 files, day 2026-11-23 (the scenario's today, not closed) as 600 files.
- **SIM03 change-log-day** (R122): `trip_changes` with 1,440 data files on 2026-11-22 and 40 on 2026-11-21.
- **SIM04 failed-copy** (R133): SIM02, and the CTAS of 2026-11-21 writes 12 rows fewer than the day holds (17,268).
- **SIM05 late-file** (R134): SIM02's 2026-11-21 compacted at 2026-11-23T11:05:00Z, then one more file of 12 events
  under `raw/trip-events/dt=2026-11-21/` (a Firehose retry).

Run log items of job `compact-days`: per run (`run_id` its start time) `run_state` (`running`, `finished`, `failed`,
`skipped`), `failed_step` (`optimize` or `copy`), `error_text`, `files_by_day`, `limit_per_day`, `marked`, `waiting`,
`moved_days`, `failed_days`, `registered`, `started_at`, `ended_at`; per moved day (`run_id` `moved-<day>`)
`moved_day`, `moved_at`, `location`, `old_location`, `inputs_key`, `rows_before`, `rows_after`, `late_files_copied`,
`raw_delete` (`waiting`, `started`, `done`), `raw_kept`; item `lock`.

## Run it

From your laptop, in a terminal in the `cdc-to-lake` folder: these two commands are offline (moto stands in for
AWS, a recording stand-in for Athena) and use no AWS account.

```bash
uv run python ext-maintenance/maintenance.py
uv run behave features/compaction.feature features/on_call.feature
```

`maintenance.py` prints the before-and-after table under What you should see, and `behave` ends with no failed
scenarios.

On AWS, from your laptop in `cdc-to-lake`, after the main scenario (`python -m main.run --target aws`) has run in
your lease. Use a laptop terminal, not CloudShell: a run can pass 20 minutes, and CloudShell ends a session after 20
to 30 minutes without keyboard or pointer input; a running process does not count
([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)). Sign in with your
lease's credentials and check the account:

```bash
aws sso login                                 # or the sign-in your lease uses
aws sts get-caller-identity                   # "Account": your lease's account
```

Use the exact `PREFIX` your pipeline was deployed with: the function finds the lake bucket, the tables and the run
log by it, and a new prefix finds none of them. Set `ICEBERG_TABLES` to the Iceberg tables your pipeline has, each
`database.table:day column`, comma-separated. Without it the function compacts the main scenario's
`<prefix>_cdc.trip_changes` and `ext-scheduled-merge`'s `<prefix>_lake.trips`, and a table that does not exist fails
every night with an error. Replace `you@example.com` with your address.

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1               # the PREFIX of your pipeline
export ICEBERG_TABLES=recipe_yourname_cdc.trip_changes:change_time  # PREFIX with - as _, then _cdc; add your tables
uv run python ext-maintenance/maintenance.py --target aws                                    # once, measured
uv run python ext-maintenance/maintenance.py --target aws --register-partitions              # once, before the first night
uv run python ext-maintenance/maintenance.py --target aws --schedule --email you@example.com # the nightly function
```

`--schedule` ends with `subscribed you@example.com to ...: confirm the e-mail AWS sends`. Open that e-mail from AWS
Notifications and choose **Confirm subscription**: until you do, neither alarm reaches you. In the SNS console the
subscription of the topic `$PREFIX-maintenance-alerts` then shows the status Confirmed.

### Run it live (the rule and the measurement)

These steps land thirty days of small Parquet files in your lake bucket, measure the 30-day count before and after
compacting them, deploy the nightly function and run one night by hand, from your laptop in `cdc-to-lake` as above.
Landing the files is 43,200 PUT requests, about 0.24 USD (Cost and clean-up). If the bucket also takes
`ext-scheduled-merge`'s JSON Lines, read the format warning in its Run it live first: land one format under
`raw/trip-events/`.

```bash
export PREFIX=recipe-yourname                                     # the PREFIX of your pipeline
export ICEBERG_TABLES=recipe_yourname_cdc.trip_changes:change_time  # as above: only Iceberg tables that exist
uv run python scripts/lake.py up
uv run python ext-maintenance/land_parquet.py --days 30           # 43,200 small Parquet files (SIM01's hour a day)
uv run python ext-maintenance/maintenance.py --target aws --register-partitions
uv run python ext-maintenance/maintenance.py --target aws --measure --all-days   # stops over MAX_MEASURE_USD
uv run python ext-maintenance/maintenance.py --target aws --schedule --email you@example.com
aws lambda invoke --function-name "$PREFIX-compact-days" --cli-binary-format raw-in-base64-out \
  --payload '{"mode": "compact"}' night.json && cat night.json       # one night now: "$partitions", OPTIMIZE ... WHERE
```

These steps need the main scenario's `trip_changes` (or whichever tables `ICEBERG_TABLES` names) to exist: a
table that is not there fails the night. `aws lambda invoke` prints `"StatusCode": 200`, and a `"FunctionError"`
line when the function raised: a refused `OPTIMIZE` or a table it could not count, named in the `errorMessage` in
`night.json`. Otherwise `night.json` holds the function's reply: `"run_state": "finished"`, with the days it
`marked` and its `moved_days`, or `"run_state": "failed"` with the days whose copy failed in `failed_days`. In
either failure, read the run's `error_text` in the run log `$PREFIX-runs` (job `compact-days`) before you leave the
schedule on. Leave the schedule installed until you have recorded a nightly
run for Checkpoint 2, and clean up afterwards (Cost and clean-up).

The `@live` examples (`uv run behave --tags=@live`, about 25 minutes the first time) use `$PREFIX-live` and their
own bucket; `uv run python features/live.py down` lists what they left and `down --yes` deletes it.

Permissions. Scheduler `CreateSchedule` (Assignment 3, optional task B) and SNS e-mail subscriptions (Assignment 3
set-up) are steps you have done before. `--schedule` also needs `scheduler:CreateScheduleGroup` and
`scheduler:TagResource` (a tagged schedule group of its own) and `cloudwatch:PutMetricAlarm` (the two alarms);
`--register-partitions` calls Glue `UpdatePartition` and `BatchCreatePartition` with your credentials, and the
nightly function's role calls them too. If your role refuses one, the command stops with an `AccessDeniedException`
that names the action, and what it made before that step stays. Run the bare `clean_up.py` to list it, delete it
with `--yes`, and ask the course staff for the action: the recipe has no other path for these steps.

## What you should see

**Offline** (`../output/maintenance-local.txt`, 120 commits of 5 rows). The file and snapshot counts and the answer
are the same in every correct run; the bytes and times are one laptop's and vary:

```
            data files  snapshots   data scanned   run time
before             120        120       452745 B  1012.6 ms
after                1          1         9937 B    53.9 ms
same query answer before and after: True
time travel to the first snapshot: no longer possible (expired)
```

On AWS (`output/ext-maintenance.txt`, 2026-11-13 UTC, after the main scenario and the other extensions: a few commits,
not 120), Data scanned fell from 301 B to 197 B. The query took 625 ms before compaction and 1233 ms after it:
with a handful of tiny files the run time is Athena's overhead and varies from query to query. The local numbers are
DuckDB on a laptop, not Athena. The offline demo expires every snapshot but the current one. The AWS schedule keeps
7 days. The nightly rule's offline runs are the scenarios of `features/compaction.feature`.

**The nightly function on AWS**, one run's output, for example (*measured*, `../output/ext-maintenance-nightly.txt`,
2026-11-23 UTC, after `land_parquet.py --days 3`): three closed days of 1,440 small Parquet files each were each
written by one CTAS into one file under `compacted/trip-events/`, the partitions switched, 2,515 rows counted before
and after each switch; `trips` had 1 file a day and was left alone; the run took 35.8 s of Lambda. Your days and
times differ; the checks are `"run_state": "finished"` and the same count before and after each switch.

```
"marked": ["<prefix>_lake.trip_events_parquet 2026-11-20", "... 2026-11-21", "... 2026-11-22"],
"moved_days": ["2026-11-20", "2026-11-21", "2026-11-22"], "run_state": "finished"
```

**The thirty-day measurement** (`@live`, R135), one run's output, for example (*measured*,
`../output/behave-live.txt`). Your Data scanned and times differ; the checks are the same answer before and after
and far fewer files after:

```
            files    data scanned   run time
before     43200       3949860 B    4817 ms
after         30         41015 B     550 ms
same answer before and after: True
```

**The other `@live` examples** (`../output/behave-live.txt`): `OPTIMIZE ... WHERE` one day of a 1,480-file copy of the
change log left that day 1 file and the other day its 40; the 1,440-file day of SIM02 counted 17,280 rows, all from
`compacted/`, in 1 file; the late file of SIM05 was copied into the moved day, which then counted 17,292.

## Cost and clean-up

Prices: the course price sheet (`436c-student/prices.md` on the course server), ca-central-1, read 2026-11-18. USD,
with CAD at 1.45, the course's planning rate. *computed* from those prices and the counts below; free allowances
left out. 1 GB = 2^30 bytes, 1 TB = 2^40 bytes. A Lambda GB-second is the memory in MB divided by 1,024, times the seconds. The
Athena and Lambda units are *measured* in live runs (`../output/ext-maintenance-nightly.txt`,
`../output/behave-live.txt`): a CTAS of a 1,440-file day scanned 1.6 MB, an `OPTIMIZE` of one 1,440-file day 1.5 MB,
so each statement is billed at the 10 MB minimum; the function took 35.8 s for three days and 2.2 s for a `VACUUM`.
The S3 requests Athena makes are not reported by AWS: those rows stay *computed* from file counts.

| Part | One nightly run: one day of each Iceberg table and one of `trip_events_parquet` compacted | 30 nights | A year |
|---|---|---|---|
| Athena: 2 `"$partitions"` queries, 2 `OPTIMIZE` of one day, CTAS of one day, 3 row counts, 2 `VACUUM`, each at the 10 MB minimum (*measured*: a day scans under 2 MB) | 10 × 10 MB × 5.50 per TB ≈ 0.00052 | 0.016 | 0.191 |
| S3 GET of data files: 1,440 small files read by the CTAS, the count and the `OPTIMIZE` of `trip_changes`, and a day of `trips`, 720 (one GET a file, *assumed*) | 5,040 × 0.00044 / 1,000 ≈ 0.0022 | 0.067 | 0.809 |
| S3 GET of Iceberg manifests, up to one per commit, read by the `"$partitions"` query and the `OPTIMIZE` (*assumed*) | 4,320 × 0.00044 / 1,000 ≈ 0.0019 | 0.057 | 0.694 |
| S3 GET by `VACUUM`, about 100 (*assumed*) | ≈ 0.00004 | 0.0013 | 0.016 |
| S3 LIST and PUT: about 61 listings, 6 new files, 2 `DeleteObjects` calls (priced at the PUT rate as an upper bound, *assumed*) | 69 × 0.0055 / 1,000 ≈ 0.00038 | 0.011 | 0.139 |
| Lambda: about 12 s a compacted day and 2 s for `VACUUM` at 256 MB (*measured*), 2 requests | ≈ 0.00005 | 0.0014 | 0.017 |
| DynamoDB run log, Glue Data Catalog, CloudWatch Logs, Scheduler | ≈ 0.00002 | 0.0011 | 0.014 |
| S3 storage of the old files a snapshot keeps for 7 days, under 0.1 GB (*assumed*) | — | 0.0025 | 0.030 |
| CloudWatch alarms, 2 at 0.10 a month | — | 0.20 | 2.40 |
| **Total** | **≈ 0.0051 (0.007 CAD)** | **≈ 0.36 (0.52 CAD), 1.2% of 30 USD; 0.16 without the alarms** | **≈ 4.31 (6.25 CAD), 14.4% of 30 USD** |

<!--src: assumed -->What the 30-day count costs in GETs alone: over SIM01's 43,200 small files, 0.019 USD a query; over 30 compacted days of F
files each, 30 × F × 0.00044 / 1,000. At one query every 5 minutes for 30 days (8,640 queries) that is 164.68 USD before
compaction and 0.114 × F after: measure F at your size. The live measurement (`--measure --all-days` over SIM01) is
estimated at 0.08 USD (0.12 CAD) before it runs; landing its 43,200 files first is 43,200 PUT requests, 0.24 USD
(*computed*), which the estimate leaves out. Measured in one `@live` run (`../output/behave-live.txt`): the count
read 43,200 files, 3,949,860 B Data scanned in 4,817 ms, before, and 30 files, 41,015 B in 550 ms, after, with the
same answer (9,150, 5,730 and 10,350 requests in zones 48, 161 and 230); 122 statements in all. *Measured*, the live runs of 2026-11-23 (`../output/cost-live.txt`): the nightly run of three days and one
`VACUUM` were 38 s of Lambda (0.0001 USD) and 13 Athena statements billed at the minimum (about 0.0007 USD), with
about 4,400 PUTs to land the three days (0.024 USD, *computed*); the four `@live` examples about 0.35 USD, 0.30 of
it the thirty-day example. Every live example of the recipe that day came to about 0.39 USD (0.56 CAD), 1.3% of
the 30 USD lease. Deleting the schedule group stops the runs; the alarms bill until the clean-up deletes them.

From your laptop, in `cdc-to-lake`, with your pipeline's `PREFIX`. The bare run only lists: it copies, deletes and
changes nothing, and for moved days it prints `would restore`. Read the list, then run it with `--yes`; `--check`
then lists what is left of this extension:

```bash
export PREFIX=recipe-yourname
uv run python ext-maintenance/clean_up.py          # what it would delete; changes nothing
uv run python ext-maintenance/clean_up.py --yes    # delete it
uv run python ext-maintenance/clean_up.py --check  # what is left: all empty?
```

With `--yes` it deletes the alarms, the schedule group (and its two schedules), the function, its log group, its two roles and
the topic. `trip_events_parquet` is kept (the landing path may still feed it), so first, for each moved day, it counts
the day through the table, finishes a raw delete left `started`, and makes the raw folder whole: if none of the
listed files is left there it copies every file of the day's location back (`compacted-<name>`), if all are there it
copies back only the `late-*` files. It then points the partition at the raw folder and counts the day again; if the
counts differ it points the partition back, keeps that day's files and stops with an error naming the day. Only when
every day matched does it delete `compacted/trip-events/` and `maintenance/inputs/`.

It does not delete the bucket, `raw/trip-events/`, the table `trip_events_parquet`, the database `<prefix>_lake`,
the run log `<PREFIX>-runs` or the Iceberg tables: the main pipeline still uses them, and their storage still bills.
When the whole project is finished, run the recipe's clean-up from `cdc-to-lake` (`uv run python clean_up.py`, read
the list, then `--yes`; it runs every extension's clean-up) and then `uv run python scripts/lake.py down` and
`--yes` for the bucket. Those keep `trip_events_parquet`, `<prefix>_lake` and `<PREFIX>-runs` too, because a landing
path may still feed them: list them in the Glue and DynamoDB consoles and delete what your project no longer uses.

A run of an earlier revision of this recipe made the schedules `$PREFIX-lake-optimize` and `$PREFIX-lake-vacuum` in
the `default` group, which `OPTIMIZE` the whole table every night. The recipe's `clean_up.py --yes` removes them with
everything else. To remove only those two while you keep the pipeline, list them, then delete each:

```bash
aws scheduler list-schedules --group-name default --name-prefix "$PREFIX-lake-"
aws scheduler delete-schedule --group-name default --name "$PREFIX-lake-optimize"
aws scheduler delete-schedule --group-name default --name "$PREFIX-lake-vacuum"
```
