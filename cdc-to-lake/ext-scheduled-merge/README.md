# Extension: an hourly merge into one trips table, with counts that late events correct

**Use this if your system** lands events or updates in S3 and keeps a table of each item's latest state, with
summaries by day, week or month that late, repeated or corrected events must correct. Needed at Checkpoint 2
(Tue Dec 1).

**What it adds.** Every hour an EventBridge Scheduler schedule calls a Lambda function. The function takes the raw JSON
Lines objects under `raw/trip-events/dt=<arrival day>/` that no earlier run merged, sets aside an object with a broken
line, and runs two Athena statements: `MERGE INTO trips` (one row per trip, its latest state) and `MERGE INTO
zone_counts` (trips per zone and period, rebuilt for every period that holds a day from K + 1 days before the
run's day to the run's day: five days with the default K = 3, the lateness the team accepts). A run log in DynamoDB records each run, a
lease stops two runs from overlapping, and a manifest per arrival day records which objects are merged.

**What changes in the main scenario.** Nothing in steps 1 to 6: this extension reads the lake bucket
`<PREFIX>-lake-<account>`, made by `scripts/lake.py up`, and the replayer's `s3` target (`sim/replay.py`), not the
change stream. Both are copies of the `simulated-runs` recipe's files in this folder: you need nothing from the
`simulated-runs` folder itself. Its tables are in the Glue database `<prefix>_lake`, beside the main scenario's `<prefix>_cdc`.
`ext-maintenance` compacts `trips` and reads this extension's manifests before it moves a raw file.

```
sim/replay.py --to s3 (or your consumer) -> S3 raw/trip-events/dt=<arrival day>/ -> Glue table trip_events
  -> (hourly) Scheduler group <PREFIX>-scheduled-merge -> Lambda <PREFIX>-merge-run
  -> Athena MERGE INTO trips -> Athena MERGE INTO zone_counts -> run log <PREFIX>-runs, manifests lake/merge-state/
```

Words used here. *Delivery*: one event as it reaches the pipeline, with its `delivery_id` and `received_time`.
*Slot*: the minutes of deliveries the replayer writes as one object (`--slot-min`, 1 by default), when the slot ends.
*Settle*: how long an object must have been in S3 before a run takes it (`SETTLE_S`, 300 s): a run takes only
objects whose `LastModified` is at least 5 minutes old. *Truth*: the answer `sim/truth.py` computes from the
simulated data alone, kept under the data set's `_truth/` folder, which the checks compare with.
*Arrival day*: the UTC day a delivery reached S3, which names its folder. *Manifest*: the file
`lake/merge-state/dt=<day>.txt`, one line per object a finished run took, with `merged` or `quarantined` after it.
*Lease*: an item in the run log that only one run at a time can hold (a conditional write in DynamoDB).
*Partition projection*: an Athena table setting that works out the day folders from a pattern instead of reading
registered partitions from the Glue Data Catalog.

## Decisions

Q010 and the other Q ids are rows of the Assumptions table under Targets and assumptions.

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Which event sets a trip's state (R81, R82) | The furthest kind (requested, accepted, completed), then the later `event_time`, then the larger `event_id`; the zone and request time from the newest request | A corrected request carries a later `event_time` than the trip's completion can, and ordering by time first would take the trip back to `requested`. Only the request carries the zone. The order is fixed, so two runs give the same row | L13 (the latest state derived from a change log); the course event model of `simulated-runs` |
| One source row per trip (R81) | The kinds folded into one row per `trip_id` (`sql/trips_source.sql`) | Athena raises an error "when a single target table row matches more than one source row", and the newest event alone would drop the zone | Athena docs, [MERGE INTO](https://docs.aws.amazon.com/athena/latest/ug/merge-into-statement.html) |
| How a late event updates a row (R82) | `WHEN MATCHED AND` newer `THEN UPDATE`, then a second clause that only fills missing fields and takes a newer request | Only the first matching clause runs: an older late event must not overwrite a newer state, but a late request is the only event with the zone | Athena docs, [MERGE INTO](https://docs.aws.amazon.com/athena/latest/ug/merge-into-statement.html) |
| Which objects a run takes (R81, R100) | Keys that no manifest of their arrival day holds, settled 5 minutes, ordered by `LastModified` then key, at most `MAX_OBJECTS` (500) | A time window misses an object stamped before its end but listed after it, however long its overlap; 600 objects with one timestamp are taken 500 and 100. A list after a write is strongly consistent in S3, so a key no manifest holds is not merged yet. The manifests also tell `ext-maintenance` which raw files it may move | S3 docs, [consistency model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel) |
| How the raw table finds its days (R80, R81) | Partition projection on `trip_events` | No day can be missed by a failed registration step. This table is read one or two days a run, so it is not compacted, and `ext-maintenance` refuses a table with projection on | Athena docs, [partition projection](https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html) |
| Which periods a run rebuilds (R90) | Every period that holds a day from K + 1 days before the run's day to the run's day (K + 2 days: five with K = 3) | A completion names no zone or request day, a corrected request moves a trip out of a period the run's events do not name, and the run after a failed rebuild does not know what that run changed. Five days of `trips` stay at Athena's 10 MB minimum | ours: the range is the lateness the team accepts (Q010) |
| A broken object (R103) | Every line checked against the delivery model (`src/rides/events.py`) before Athena reads the object; an object with a bad line set aside whole, under `quarantine/`, and listed `quarantined` in its manifest | A line with a missing or wrong field never reaches the `MERGE`, so it cannot merge as a wrong row; failing the run instead would hold every later object back | ours: one broken line must not hold back every later object |
| The rebuild in one statement (R90) | One `MERGE` with `DELETE` for groups now at 0 | Two statements are two commits, and a reader between them sees the period missing; Athena's transactions are per table and per statement | Athena docs, [Iceberg tables](https://docs.aws.amazon.com/athena/latest/ug/querying-iceberg.html) |
| K (R90, R91) | K days from the team's lateness (`K_DAYS`, 3), with `old_events` counted | A monthly summary needs one late observation of an earlier month to be rebuilt; a team may show the count of late events instead of a rebuild | ours: Q010 |
| When a run counts as done (R101) | The manifests and then `last-finished`, written only after both statements succeed | A failed rebuild would otherwise leave its periods stale for good | ours: a run's effects count only when all of them are in |
| Overlapping runs (R102) | A conditional lease in the run log, plus the recorded query ids of the last run still `running` | The lease works for any job in the run log; listing the workgroup's queries returns other recipes' queries too | DynamoDB docs, [condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html) |
| Retries (R101, R102) | Function timeout 600 s, asynchronous retries 0, schedule retries 0 | The Scheduler invokes asynchronously and Lambda retries a failed call twice by default; each timed-out try would leave its `MERGE` running on the same table. The next hour's run is the retry | Lambda docs, [asynchronous error handling](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html) |
| Engine | Athena `MERGE INTO` from a scheduled function | A Glue 6.0 Spark job of 2 data processing units (DPU) bills at least 1 minute: 2 DPU × 1/60 hour × 0.308 USD per DPU-hour = 0.0103 USD a run, 7.39 USD for 720 runs, against about 0.30 USD here. A team that merges in Glue ports the source query, the clauses and the rebuild rule to Spark SQL, which has the same `WHEN MATCHED AND`, and keeps the lease, the manifests and the quarantine <!--src: assumed --> | Course price sheet `436c-student/prices.md` on the course server, ca-central-1, read 2026-11-18 |
| Trigger | A schedule | An S3 event trigger per object: 1,440 objects a day start 43,200 runs a month, at least 43,200 × 2 × 10 MB × 5.50 USD per TB scanned = 4.53 USD, with their commits on one table at once, and a trigger that writes under the prefix it watches can loop <!--src: assumed --> | Lambda docs, S3 event notifications (recursive invocation) |
| Table deletes | Glue `DeleteTable` and `DeleteDatabase` by name, only for the tables and database the ledger records `created` by `merge.py up`, then the S3 prefixes of those tables | Tables made by Athena DDL carry no tags, so the ledger (`state/ledger.jsonl`) is what says a table is ours: a table that was there before `up` is recorded `borrowed` and kept, with its data; the same calls run offline under moto | Course convention: untaggable resources found by name prefix and the creation ledger |
| Engine offline | DuckDB runs the same `sql/trips_source.sql`, `sql/merge_trips.sql` and `sql/rebuild_counts.sql`; only the raw read differs | Offline green then says the clauses and the rebuild rule are right; whether Athena accepts each text is checked by the first live run | ours: one SQL text for both engines, as `main/sql/` |

Which research question (RQ) of the case this answers. *When is Glue or an S3 event trigger the better choice?* The Engine and Trigger rows: Glue
when the team already runs Spark jobs or the merge outgrows Athena's per-query limits; an event trigger when each
object must be merged within seconds and objects are few.

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/scheduled_merge.feature`. A team keeps 2 to 5 per story; the targets and the
assumptions behind them are in the next section.

- **R80** (event) When the replayer's s3 target or a team's consumer lands a slot of deliveries, the recipe shall
  write it as one JSON Lines object under `raw/trip-events/dt=<arrival day>/` of the lake bucket.
- **R81** (event) When the hourly schedule fires, the recipe shall merge every raw object that no finished run has
  merged into `trips`, one row per `trip_id` with the state of its furthest event (requested, then accepted, then
  completed; within one kind the newest by `event_time`, then `event_id`), the zone and time of its newest request,
  and the fields each kind carries.
- **R82** (unwanted) If an event arrives that comes before the event of its trip's row in that order, then the recipe
  shall keep the row's state, fill in only the fields the row does not have, and take the zone and request time of a
  request that is newer than the row's.
- **R90** (event) When a merge finishes, the recipe shall rebuild in one statement the rows of `zone_counts` of every
  period (`PERIOD`: day, week or month) that holds a day from K + 1 days before the run's day to the run's day, and
  delete a row whose count is now 0.
- **R91** (unwanted) If the merged objects hold events from more than K + 1 days before the run's day, then the
  recipe shall merge them into `trips`, rebuild no older period, and record their number as `old_events` in the run
  log.
- **R100** (unwanted) If the same objects are merged a second time, then the recipe shall leave `trips` and
  `zone_counts` unchanged.
- **R101** (unwanted) If the merge or the rebuild fails, then the recipe shall record the failed step and its error
  text in the run log and merge that run's objects again in the next run.
- **R102** (unwanted) If a query of the previous run is still running at the schedule's next firing, then the
  recipe shall record the run as `skipped` and start no query.
- **R103** (unwanted) If a line of a raw object does not match the delivery model, then the recipe shall leave the
  whole object out of the merge, copy it under `quarantine/trip-events/`, and record the object and its bad lines in
  the run log.
- **R104** (event) When `merge.py --target aws pause` or `resume` runs, the recipe shall set the hourly schedule's
  state to disabled or enabled and leave its rate, its target and every other resource unchanged.

## Targets and assumptions

A service-level objective (SLO) is a target the team measures its system against; p95 is the 95th percentile, the
value that 95 of 100 measured objects are at or under. SIM01 is the rush-hour data set described under Data.

| Target (SLO) | Value | Measured over |
|---|---|---|
| Slot end to its object in S3, p95 (R80) | under 5 min <!--src: ours: a slot a minute, sent when it ends --> | every slot of one SIM01 run |
| Object in S3 to its trips' rows in `trips`, p95 (R81) | under 75 min <!--src: ours: an hourly run plus the 5-minute settle and the run itself --> | the 24 runs of one day at SIM01's rate: per object, the `ended_at` of the run whose manifest took it minus its `LastModified`; an object no run took by the day's end counts as over; nearest rank over 1,440 objects |

Assumptions the recipe makes (a team replaces them with its own answers):

| Id | What the case does not say | We assume | Affects |
|---|---|---|---|
| Q010 | How late can an event arrive, and its correction? | up to 48 hours (arrival day minus event day), a correction within 48 hours of the event it corrects; so K = 3 days | R90, R91 |
| Q020 | Which event sets a trip's state, which request its zone? | the furthest kind, then `event_time`, then `event_id`; the zone and `requested_time` from the newest request | R81, R82 |
| Q030 | Is hourly freshness enough? | yes; a team that needs minutes keeps a stream path for the live view | R81 |
| Q040 | Which day does a trip belong to? | the UTC day of its newest request's `event_time`; a trip ends at most a day after its request | R90, R91 |
| Q050 | Which period does the summary use? | a day; `PERIOD` week (weeks start on Monday) or month | R90 |

## Data

The events are simulated by `simulated-runs`' generator (`sim/generate.py`, a copy) from DS01, the TLC High Volume
For-Hire Vehicle trip records (Uber, Manhattan, March 2024): its trips in zones 48, 161 and 230 in one hour, moved to
2026-11-23 09:00 to 10:00 UTC. `sim_data.py` builds the three data sets.

- **SIM01 rush-hour** (R81, R100): 837 trips (304, 191 and 342 in zones 48, 161 and 230), 2,511 events sent as 2,515
  deliveries: 4 requests arrive twice and 2 arrive 6 to 10 minutes late.
- **SIM02 late-day** (R90): SIM01's events moved to 2026-11-22, each delivered once, except 12 requests of zone 161,
  delivered 30 hours later (2026-11-23 15:00 to 16:00 UTC). The counts of 2026-11-22 are 304, 179 and 342 before
  the late requests and 304, 191 and 342 after.
- **SIM03 all-faults** (R90): the generator's SIM04: SIM01 with 9 events of 3 trips of zone 161 never delivered and
  2 completions of zone 230 corrected (fare 5.00 higher). Its answer from `_truth/` (`sim/truth.py`): 834 trips, 304, 188 and 342 requested,
  834 completed.

Tables. `trips`: `trip_id`, `trip_state` (`requested`, `accepted`, `completed`), `pickup_zone_id`, `requested_time`,
`request_event_id`, `driver_id`, `fare`, `last_event_time`, `last_event_rank`, `last_event_id`; partitioned by
`day(requested_time)`. `zone_counts`: `period_start`, `pickup_zone_id`, `trips_requested`, `trips_completed`,
`fare_total`. Run log items of job `merge`: `run_state` (`running`, `finished`, `failed`, `skipped`), `failed_step`,
`error_text`, `merge_query_id`, `rebuild_query_id`, `objects`, `quarantined`, `bad_lines`, `old_events`,
`window_start`, `window_end`, `started_at`, `ended_at`; items `lock` (`lock_until`) and `last-finished`.

## Run it

From your laptop, in a terminal in the `cdc-to-lake` folder: these two commands are offline (moto stands in for S3
and DynamoDB, DuckDB for Athena) and use no AWS account.

```bash
uv run python ext-scheduled-merge/merge.py
uv run behave features/scheduled_merge.feature
```

`merge.py` prints the three runs shown under What you should see, and `behave` ends with no failed scenarios. Go on
to the live steps only when both do.

### Run it live

From your laptop, in a terminal in the `cdc-to-lake` folder, with the credentials of your AWS lease. Every script
uses ca-central-1. `aws sts get-caller-identity` prints the account your credentials reach: go on only when its
`Account` is your lease's account. Use the same `PREFIX` for every step and for clean-up.

```bash
aws sso login                                              # or the sign-in your lease uses
aws sts get-caller-identity                                # "Account": your lease's account
export PREFIX=recipe-yourname
uv run python scripts/lake.py up                           # the lake bucket (scripts/ in this folder)
uv run python ext-scheduled-merge/merge.py --target aws up # tables, workgroup, run log, function, hourly schedule
uv run python ext-scheduled-merge/live_run.py --out live-run.txt   # SIM01 into S3, one run now; about 8 minutes
uv run python ext-scheduled-merge/merge.py --target aws status
```

`status` prints the run log's last runs, newest first, one line each: the run's start time, its state, and
`objects=`, `quarantined=` and `old_events=`, and a failed run's step. One run's output, for example:

```
2026-11-23T08:16:49.000Z  finished  objects=0 quarantined=0 old_events=0
2026-11-23T08:16:34.000Z  finished  objects=109 quarantined=0 old_events=0
```

Until you clean up, the schedule runs every hour. To keep the tables for your project but stop the runs (and their
cost), pause the schedule; resume it when the pipeline should merge again. Neither deletes anything:

```bash
uv run python ext-scheduled-merge/merge.py --target aws pause    # "DISABLED schedule ..."
uv run python ext-scheduled-merge/merge.py --target aws resume   # "ENABLED schedule ..."
```

When you are done, clean up as Cost and clean-up says: list first, then delete with `--yes`.

Your own events, in place of the replayer. Your consumer writes each batch as one new object under
`raw/trip-events/dt=YYYY-MM-DD/` of the lake bucket, where the date is the UTC day the batch reaches S3, with a key
no earlier object had (for example `<slot start>-<your run id>.jsonl`). An object is JSON Lines: one JSON object a
line, a delivery as `src/rides/events.py` defines it (`event_type` one of `trip_requested`, `trip_accepted`,
`trip_completed`; `event_id`, `trip_id`, `event_time` in UTC ISO 8601; the fields of its kind, such as
`pickup_zone_id` for a request; and `delivery_id`, `received_time`). A line with a missing or extra field sets the
whole object aside (R103). To write lines that pass, build them with the models of `src/rides/events.py` and
`to_line()`, as the replayer does.

`live_run.py` writes what it prints to the file `--out` names (`live-run.txt` here; without `--out`,
`.local/ext-scheduled-merge.txt`, so the recorded capture in `output/` stays). It replays SIM01 through the replayer's `s3` target, waits the 5-minute settle, invokes the function once,
reads `trips` and `zone_counts` through Athena and compares both with the answer from `_truth/`, and prints each statement's Data
scanned, the bytes of the objects the run took and how long the run took. The run worked when the function run
says `finished` and the last line says `result: trips and zone_counts match truth`. The schedule fires once when
`up` creates it (a run with no objects, done long before the 5-minute settle ends), then every hour. If the function
run says `skipped`, another run held the lease: run `merge.py --target aws status`, wait until no run is `running`,
and run `live_run.py` again.

With `ext-maintenance` in the same bucket, choose one format for `raw/trip-events/` before you land any data:
`trip_events` reads JSON Lines and `ext-maintenance`'s `trip_events_parquet` reads Parquet, both under that prefix.
The function checks every line as JSON, so a Parquet file in a folder the merge lists is quarantined; a JSON file in
a day of `trip_events_parquet` fails the queries of that day. Changing format later means deleting raw objects your
pipeline may still need as input, so decide first.

Permissions the run needs, by step (the clean-up needs the matching `Delete*` actions):

| Step | Actions |
|---|---|
| `scripts/lake.py up` | `s3:CreateBucket`, `PutBucketTagging`, `PutPublicAccessBlock`, `PutLifecycleConfiguration`; `athena:CreateWorkGroup`, `GetWorkGroup`, `TagResource` |
| `merge.py --target aws up` | the Athena DDL (`athena:StartQueryExecution`, `GetQueryExecution`, and the Glue `CreateDatabase`, `CreateTable`, `GetTables` it calls); `dynamodb:CreateTable`, `DescribeTable`, `TagResource`; `iam:CreateRole`, `PutRolePolicy`, `TagRole`, `PassRole`; `logs:CreateLogGroup`, `PutRetentionPolicy`; `lambda:CreateFunction`, `GetFunction`, `TagResource`, `PutFunctionEventInvokeConfig`; `scheduler:CreateScheduleGroup`, `TagResource`, `CreateSchedule` |
| `live_run.py`, `status`, `pause`, `resume` | `lambda:InvokeFunction`; Athena queries as above; `dynamodb:Query`; `scheduler:GetSchedule`, `UpdateSchedule`; `s3:ListBucket`, `PutObject` |

Athena `MERGE INTO` on an Iceberg table (Assignment 3, task 3.2, and its job aid),
creating the function's role (Assignment 1) and EventBridge Scheduler schedules (Assignment 3, optional task B) are
steps you have done before. `up` also needs `scheduler:CreateScheduleGroup` and `scheduler:TagResource` (a tagged
schedule group of its own) and `lambda:PutFunctionEventInvokeConfig` (asynchronous retries set to 0). If your role
refuses one, `up` stops with an `AccessDeniedException` that names the action, and what it made before that step
stays. Run the bare `clean_up.py` to list it, delete it with `--yes`, and ask the course staff for the action. The
recipe has no fallback: with Lambda's default of two asynchronous retries, a timed-out try could leave a second
`MERGE` running on the same table (Decisions, Retries).

## What you should see

Offline (`../output/scheduled-merge-local.txt`; the data and the run times are fixed, so a correct run prints
these lines):

```
run 10:00  finished  objects  55  quarantined 0  old_events 0
run 11:00  finished  objects  53  quarantined 0  old_events 0
run 12:00  finished  objects   2  quarantined 0  old_events 0
trips: 837 rows; zones 48, 161, 230: 304, 191, 342 (truth: 304, 191, 342)
zone_counts 2026-11-23 (zone, requested, completed): 48 304 304, 161 191 191, 230 342 342
the same objects merged again leave trips unchanged: True
```

The 10:00 run takes the objects that had settled 5 minutes before it; the last two completions land after 11:00. The
counts equal the answer from `_truth/` although 4 requests came twice and 2 came late.

On AWS, one run's output, for example (*measured*, `../output/ext-scheduled-merge.txt`; the replay's times moved
to the run's day). Your object count, bytes and times differ; the check is that `trips` and `zone_counts` match the
truth, the last line:

```
replayed SIM01 as live0810: 2515 deliveries in 109 objects, 0 failed, 3.2 s
function run: finished in 11.0 s, objects 109, quarantined 0, old_events 0
merge    8dd8bc4e-...: 695906 B Data scanned, 2092 ms
rebuild  e7fe4198-...: 6206 B Data scanned, 1301 ms
trips of live0810 per zone: {"161": 191, "230": 342, "48": 304}; truth: {"161": 191, "230": 342, "48": 304}
zone_counts 2026-11-23 requested per zone {"161": 191, "230": 342, "48": 304}, completed 837; truth: ... completed 837
objects in the day folder: 109, 695906 B; taken by the run: 109, 695906 B
result: trips and zone_counts match truth
```

The `MERGE` scanned the objects' bytes once (695,906 B): `sql/trips_source.sql` names the run's events once (one
pass of window functions and one `GROUP BY`). Athena does not keep the result of a `WITH` query, so a source that
referred to the events four times would read the objects four times (*measured* in an earlier run: 2,783,624 B,
4 × 695,906 B).
`"$path" IN (...)` does limit the read to the objects named (`../output/athena-probes.txt`: 263 B for one small
object of a folder of 7.1 MB). Merging the same 109 objects again, after deleting the day's manifest, left every row
of `trips` and the sums of `zone_counts` unchanged (R100, `ext-scheduled-merge/r100_live.py`,
`../output/ext-scheduled-merge-one-read.txt`); that `MERGE` scanned 713,308 B: the objects once and the one day of
`trips`.

## Cost and clean-up

Prices: the course price sheet (`436c-student/prices.md` on the course server), ca-central-1, read 2026-11-18. USD,
with CAD at 1.45, the course's planning rate; the free tier is left out. 1 GB = 2^30 bytes, 1 MB = 2^20 bytes. A Lambda GB-second is the
memory in MB divided by 1,024, times the seconds.
A cost marked *measured* is read from a run's capture in `output/`, *computed* from measured units and the
prices, and *assumed* is an estimate no run measured. This estimate leaves out the ingestion that lands the objects and `ext-maintenance`'s nightly run: budget those from
their own READMEs. Traffic: SIM01's rate every hour (2,515 deliveries,
one object a minute), `trips` compacted nightly by `ext-maintenance`. The units are *measured* in one live run
(`../output/ext-scheduled-merge.txt`): 695,906 B of objects an hour (277 B a JSON line), 21 B a `trips` row
(17,918 B for 837 trips), the `MERGE` reads the run's objects once and `trips` (one day of it in that run; all of it
assumed as it grows), the rebuild 6.4 KB, a run of 110 objects 12.8 to 16.1 s of Lambda at 256 MB (priced at 16.1). The rows below are *computed* from those units and the prices; the
S3 requests Athena makes are not reported by AWS, so their row is *computed* from file counts.

| Part | One hourly run (the first month's average) | 30 days (720 runs) | A year (8,760 runs) |
|---|---|---|---|
| Athena, 2 statements, each billed at least 10 MB: the `MERGE` reads 0.66 MB of objects and, *unchecked* beyond one day, all of `trips` (6.8 MB on average in the first month, 76 MB over a year as `trips` grows to 150 MB); the rebuild stays at the minimum | 20.4 MB: 0.00011 | 0.077 | 3.94 |
| S3 GET (*computed*): Athena reads the run's 60 objects and about 30 files of `trips`; the function reads its 60 objects and the open days' manifests | 170 × 0.00044 / 1,000 = 0.00008 | 0.054 | 0.66 |
| Lambda, 1 request, 16.1 s at 256 MB (*measured*, 110 objects) | 4.0 GB-s × 0.0000133334 = 0.00005 | 0.039 | 0.47 |
| S3 PUT and LIST, about 27 a run (*assumed*) | 0.00015 | 0.107 | 1.30 |
| Glue Data Catalog requests, about 20 a run (*assumed*) | 0.00002 | 0.014 | 0.18 |
| DynamoDB run log, 4 writes and 2 reads a run | 0.000003 | 0.0022 | 0.027 |
| CloudWatch Logs, 1 KB a run (*assumed*) | under 0.000001 | 0.0004 | 0.005 |
| Scheduler, 1.10 per million invocations | 0.000001 | 0.0008 | 0.010 |
| S3 storage: 0.48 GB at the month's end; 5.8 GB at the year's end | — | 0.006 | 0.87 |
| **Total** | **0.00042 (0.0006 CAD)** | **0.30 (0.44 CAD), 1.0% of 30 USD** | **about 7.5 (10.8 CAD), 25% of 30 USD** |

<!--src: assumed -->The year is 25 times the first month, not 12, if the `MERGE` reads all of `trips` as it grows. The measured run does not
show that: its `trips` held one day, 17,918 B, which the second `MERGE` read whole, so a read of all of `trips` and
of one day look the same (*unchecked* beyond one day; the `ON` has no day bound, so this estimate prices the whole table): the
Athena line goes from 0.077 to 3.94. With the first source's four reads of the objects (*measured*, 2.7 MB a run)
the Athena line was 0.079 for 30 days and 4.03 for a year, and Athena's GETs of the objects four times as many. Measure
`DataScannedInBytes` of one run at your size and price your horizon from that. Objects of 5 minutes (`--slot-min 5`)
bring the S3 GET line down. Pausing the schedule (`merge.py --target aws pause`) or deleting the schedule group
stops new runs; the objects and tables stay, and their S3
storage bills until clean-up deletes them.

*Measured*, one live run of 2026-11-23 (`../output/cost-live.txt`): the extension's Athena statements in the
workgroup `<PREFIX>-wg` (DDL, the four runs' `MERGE` and rebuild statements, the checks of `live_run.py` and of R100)
billed about 0.002 USD, its five function runs 30.3 s of Lambda (0.0001 USD), its 110 objects and manifests about
0.001 USD of S3 requests (*computed*): under 0.01 USD (0.015 CAD), 0.03% of the 30 USD lease. Every live example
of the recipe run that day, the thirty-day `@live` example of `ext-maintenance` included, came to about 0.39 USD
(0.56 CAD).

From your laptop, in `cdc-to-lake`, with the same `PREFIX` as the run. The bare run lists what it would delete and
deletes nothing; read the list, then run it with `--yes`; `--check` then lists what is left of this extension:

```bash
export PREFIX=recipe-yourname
uv run python ext-scheduled-merge/clean_up.py          # what it would delete; deletes nothing
uv run python ext-scheduled-merge/clean_up.py --yes    # delete it
uv run python ext-scheduled-merge/clean_up.py --check  # "nothing of ext-scheduled-merge is left"
```

It deletes the schedule group (and with it the schedule), the function, its log group and its two roles, the run
log's items of job `merge` (the table only when the ledger records it `created` and no other job has items), those
of the tables `trips`, `zone_counts` and `trip_events` that the ledger records `created` by `merge.py up`, with
their prefixes `lake/trips/` and `lake/zone_counts/`, the prefixes `lake/merge-state/` and `quarantine/trip-events/`,
and the database `<prefix>_lake` when the ledger records it `created` and no table is left in it. Tables made by
Athena DDL carry no tags, so the ledger is what says a table is ours: a table that was there before `up` is listed
`kept (borrowed)`, and so is one the ledger does not know (on another machine, copy `state/ledger.jsonl` along).

It never deletes the bucket, `raw/trip-events/` or the shared workgroups `<PREFIX>-wg` and `<PREFIX>-ctas-wg`; they
still bill for storage. When no other recipe or extension of yours uses them any more, remove them the same way:

```bash
uv run python scripts/lake.py down          # what it would delete; deletes nothing
uv run python scripts/lake.py down --yes    # delete the bucket and the workgroups
```

`down` refuses while objects other than `raw/trip-events/` and `athena-results/` are left in the bucket, and lists
their keys: those belong to something not yet cleaned up.

## Adapt it

- Your own events: the columns of `sql/athena_tables.sql` `trip_events`, the kinds and their ranks in
  `sql/trips_source.sql`, and the delivery model in `src/rides/events.py` (a copy; change every copy or none).
- Your lateness: `K_DAYS`; your summary: `PERIOD` (day, week or month); a backlog: `MAX_OBJECTS`.
- Your own consumer: write every batch to a new S3 key and never overwrite a key a run has taken. The manifests
  record keys, not object versions, so an overwritten object is not merged again.
- A Parquet landing path (Firehose with record format conversion): make `trip_events` a Parquet table, still with
  projection over `raw/trip-events/`, and change the object check in `merge_run.py`, which reads every line as JSON
  and would quarantine each Parquet file. Do not land both formats under one prefix (Run it live).
- A period older than K + 1 days, by hand. `FIRST_DAY` is a UTC day, `YYYY-MM-DD`; the command rebuilds the
  period that holds it (the day, its week from Monday, or its month, as `PERIOD` says). Then compare that period's
  `zone_counts` with counts you derive from `trips`.

  ```bash
  FIRST_DAY=2026-11-16
  uv run python ext-scheduled-merge/merge.py --target aws rebuild --period "$FIRST_DAY"
  ```
