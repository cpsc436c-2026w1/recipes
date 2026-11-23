# S3 event ETL: each file checked as it lands, bad rows set aside, good rows loaded once

## What you build

Each JSON Lines file (one JSON object per line) that lands under `raw/trip-events/` makes S3 call a function that
checks every line: the good lines are staged, the bad ones kept with their line number and reason. A load then puts
the staged lines into the Iceberg table `<prefix>_lake.trip_events_clean`, one row per `event_id`: a Glue job at most
every 6 hours by default, or one Athena `MERGE INTO` at most every hour, with limits that keep a broken job from
spending the lease. This is an extract, transform, load (ETL) pipeline: extract the file, check and stage its lines,
load them into the table.

## Use it when

Use this if your system lands files in S3 and must check each file as it arrives, set its bad rows aside with the
reason, and load the good rows into a lake table with a Glue job or an Athena statement. The trigger, the check and
the quarantine are needed at Checkpoint 2 (Tue Dec 1); the retry, the limits and the clean-up, tested, at the final
hand-in (Tue Dec 15). For a table of one row per trip merged from these events, add `scheduled-merge` over
`stage/trip-events/`. A team that needs the table within minutes keeps a stream path for the live view.

## Main scenario

1. A writer (here `sim/replay.py --to s3`, one object a minute) lands `raw/trip-events/dt=<arrival day>/<name>.jsonl`
   in the lake bucket `<PREFIX>-lake-<account>`.
2. The bucket's notification entry `<PREFIX>-s3-event-etl` (prefix `raw/trip-events/`, suffix `.jsonl`) calls
   `<PREFIX>-etl-check`. It records the object in the run log `<PREFIX>-runs` (a repeat event is counted and
   stopped), checks each line against the delivery model (the fields and value rules each line must pass, the
   Pydantic models of `src/rides/events.py`), and writes the good lines to `stage/trip-events/` and the bad ones to
   `quarantine/trip-events-rows/`.
3. After staging, and every hour from `<PREFIX>-etl-sweep`, `maybe_start()` starts the Glue job `<PREFIX>-etl` if no
   run is running, the last started at least `ETL_MIN_GAP_MIN` minutes before (the minimum number of minutes between
   starts: 360 for Glue, 60 for Athena by default) and the day's limit is not reached.
4. The job keeps one line per `event_id` (the earliest `received_time`) across all the staged objects no load
   manifest lists yet, loads them with one insert-only `MERGE INTO` on `event_id`, and only then appends the objects
   to that day's load manifest `lake/etl-state/dt=<day>.txt`.
5. The sweep follows each run: a failed one is recorded with its step and error and emailed through
   `<PREFIX>-etl-alerts`; its objects load in the next run. Three failures in a row hold the job until
   `etl.py resume`.

**Words used here** (S3, Lambda, Glue jobs, Iceberg and `MERGE INTO` were taught in L7 to L11). *Event
notification*: a bucket setting that makes S3 call a function when an object is created under a prefix. *Staged
object*: the lines of a raw object that passed the check, written as an object of their own. *Load manifest*: a file
per day listing the staged objects a finished run has loaded.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| How often the load runs (R50, R70) | a Glue job at most every 6 hours (4 a day), or one Athena `MERGE` an hour | a Glue run of 2 DPU (data processing units, Glue's unit of compute capacity, billed per DPU-hour) bills at least one minute: one run per object at 1,440 objects a day is at least 634 USD for 30 days, one an hour 17.1 USD (57% of the lease), 4 a day 2.84 USD (from the measured 191 DPU-seconds a run, below). Glue earns its minute when the load needs Spark (joins, files of hundreds of MB, a team already in Glue); for this traffic Athena's `MERGE` costs about 0.43 USD for 30 days | price sheet; COST06-BP01, perform cost modeling |
| A check per object, a load per batch (R10, R50) | the function checks every object within seconds; the load runs when the limits allow | the check costs a Lambda call and an S3 PUT per object, under 0.001 USD an hour, so a bad line is visible at once; the load is the expensive part | check at the boundary, before any table reads it |
| Bad rows one by one (R20) | each line checked in code; a bad line set aside with its line number and reason | one broken line costs one row, not the file; the line is kept for someone to fix and land again under `raw/` | data quality at ingestion |
| Repeats (R40) | stopped three times: by the object's key and `sequencer` in the run log, by keeping one line per `event_id` across the waiting staged objects before the `MERGE`, then by `event_id` in the `MERGE` | S3 delivers events at least once, in no set order ([event ordering and duplicate events](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-how-to-event-types-and-destinations.html#event-ordering-and-duplicate-events)); of two events of one key the greater `sequencer` (compared after left-padding with zeros) is the later ([event message structure](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-content-structure.html)), so the padding lets DynamoDB's string comparison decide | idempotent writes |
| Staged and quarantine writes (R10, R40) | `If-None-Match: *` on both | a retry of the same event finds the object there (`412 Precondition Failed`) and goes on ([conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)) | S3 docs |
| The manifest after the commit (R30, R60) | an insert-only `MERGE` on `event_id`, then the manifest | a run that dies between the two is repeated whole, and the second `MERGE` finds every `event_id` there; an append would add the rows again; Glue job bookmarks pick objects by time and do not exist in the Athena path | a failed run is repeated whole, safely |
| The table read on named days (R30) | `ON t.event_id = s.event_id AND t.event_day IN (…)` | the day condition is meant to limit the table read to those days, so a run's read does not grow with the table; whether Athena's `MERGE` prunes this way is *unchecked* (the live table was empty): check `DataScannedInBytes` on a table that holds data before you rely on it. The workgroup `<PREFIX>-wg` cancels any query that scans more than 1 GiB (2^30 bytes), and a cancelled query is still charged for what it scanned ([per-query data usage control](https://docs.aws.amazon.com/athena/latest/ug/workgroups-setting-control-limits-cloudwatch.html)) | L11 (partitions) |
| Limits of the job (R61, R70) | `Timeout` 10, `MaxRetries` 0, `MaxConcurrentRuns` 1, queuing off, at most 4 runs a day, held after 3 failures | Glue's default timeout is 480 minutes for Glue 5.0 and later ([Job](https://docs.aws.amazon.com/glue/latest/webapi/API_Job.html)): a hanging script of 2 DPU would cost 7.04 USD a run; at 10 minutes it is 0.147, a day at most 0.59, three failures 0.44. With queuing off a start while a run is active is refused at once | bound the worst case of a batch job |
| Glue version (R70) | Glue 5.0, the version of A3 and tutorial B3T2 | the A3 job definition is the one students ran; Glue 6.0 (0.308 against 0.44 per DPU-hour) would lower the Glue line by 30% and is *unchecked* with this script | price sheet |
| The trigger on a shared bucket (R90) | the entry added and removed by its `Id`, every other entry kept | `PutBucketNotificationConfiguration` replaces the bucket's whole configuration ([PutBucketNotificationConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutBucketNotificationConfiguration.html)); A3's quality gate does the same | A3 optional task A |
| What the function writes (R10) | only under `stage/`, `quarantine/` and `lake/`, never under `raw/trip-events/` | a function that writes into the prefix that calls it can call itself in a loop ([Lambda destination](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-how-to-event-types-and-destinations.html#lambda-function)) | S3 docs |
| The roles (R70, R80) | one inline policy each, nothing attached; the job's names its own prefixes, the Glue catalog entries of `<prefix>_lake` and the log groups `/aws-glue/*` | an account that lets you create roles may refuse `iam:AttachRolePolicy` (a live run was refused it), and the managed `AWSGlueServiceRole` reaches every Glue resource of the account | least privilege |
| Who starts a load (R50, R80) | the check function with Glue; only the sweep with Athena | the check function has no Athena actions and 60 s; with Athena its start was refused and recorded as a failed run (live run), which counts towards the hold of R61 | ours |
| Lost events (R10, R50) | the hourly sweep checks every raw object of the open days (the run's arrival day and the 2 days before it, `OPEN_DAYS = 3` in `pipeline/control.py`) with no `etl-object` item, or none finished for 15 minutes; a file that lands more than 48 hours late is outside the window and is not loaded automatically | an event lost or failed three times would otherwise leave an object unchecked | reconcile what the events missed |
| Offline | the Glue job's load replaced by `pipeline/local_load.py`: the same steps with DuckDB, no PySpark | the job itself (`pipeline/etl_job.py`) runs only in Glue; its helpers (the manifests, the waiting keys, the `MERGE`) are the ones the stand-in and the Athena path call | offline first |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall", in the EARS forms
(Mavin et al., "Easy Approach to Requirements Syntax", RE 2009):

| Form | How the sentence is built |
|---|---|
| ubiquitous | The recipe shall ... (it always holds) |
| event | When <something happens>, the recipe shall ... |
| state | While <something is the case>, the recipe shall ... |
| unwanted | If <something goes wrong>, then the recipe shall ... |
| optional | Where <a part is used>, the recipe shall ... |
| complex | two or more of these together |

The scenarios are in `features/s3_event_etl.feature`, tagged with the numbers; `uv run behave` runs them offline.
A team keeps 2 to 5 of these per story, with its own numbers.

**Every good row in the lake, every bad row kept** (the analyst)

- **R10** (event) When an object is created under `raw/trip-events/` with a key ending `.jsonl`, the recipe shall
  check each line against the delivery model, write the lines that pass, unchanged, as one object
  `stage/trip-events/dt=<arrival day>/<object stem>-<sequencer>.jsonl`, and record the object with its good and bad
  line counts in the run log.
  <!--src: assumed: validation latency under 2 min at p95 from the raw object to its staged object; the @live scenario measures it -->
- **R20** (unwanted) If a line does not match the delivery model, then the recipe shall write it with its line number
  and the reason to `quarantine/trip-events-rows/dt=<arrival day>/<object stem>-<sequencer>.jsonl` and keep the
  other lines of its object.
- **R30** (event) When the ETL job runs, the recipe shall load every staged object of the open days (the run's
  arrival day and the 2 days before it) that no finished run has loaded into `<prefix>_lake.trip_events_clean`, one row per `event_id`, and only then add the objects to
  the load manifest of their day.

**The same file twice, a failed job, and a bill that cannot run away** (the team member)

- **R40** (unwanted) If the event of an object arrives again, or the same deliveries land again under a new key, then
  the recipe shall leave `trip_events_clean` unchanged and count the repeat in the run log.
- **R50** (event) When an object has been staged or the hourly sweep finds staged objects that no run has loaded, the
  recipe shall start the ETL job only if no run of it is running, the last run started at least `ETL_MIN_GAP_MIN`
  minutes before and fewer than `MAX_RUNS_PER_DAY` runs have started that UTC day.
- **R60** (unwanted) If an ETL run fails or times out, then the recipe shall record the failure with its step and
  error text in the run log, email the team through `<PREFIX>-etl-alerts`, and load the same objects in the next run.
- **R61** (unwanted) If the last 3 runs failed, then the recipe shall start no run until `etl.py resume` and record
  the hold once as a skipped run with the error text "held after 3 failed runs".
- **R70** (ubiquitous) The recipe shall run the Glue job with 2 G.1X workers, a timeout of 10 minutes, no automatic
  retries, at most 1 run at a time and run queuing off.
- **R80** (optional) Where `ENGINE=athena` is set, the recipe shall load with one Athena `MERGE INTO` in
  `<PREFIX>-wg` from the sweep, with the same manifest, run log and limits.
- **R90** (event) When `clean_up.py --yes` runs, the recipe shall remove only its own entry (its `Id`) from the
  bucket's notification configuration, keep every other entry, and delete what it owns.

## Run it

> ⚠️ Commands run in two places. Offline commands run in a terminal on your laptop, in the copied recipe folder, and
> need no AWS account. Live commands run in a terminal with your lease's credentials active, in the same folder, in
> `ca-central-1`.

From your laptop, in a terminal in the recipe folder (offline):

```bash
uv sync                   # installs the dependencies; it should finish without an error
uv run pytest -q          # moto and DuckDB, no AWS account; every test should pass
uv run behave             # the scenarios of the Requirements; none should fail
```

### Run it live

From a terminal with your lease's credentials, in the recipe folder. First choose a short lowercase prefix that no
other resource in your lease uses, set the region, and check the account:

```bash
export PREFIX=etl-yourname
export AWS_DEFAULT_REGION=ca-central-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "$ACCOUNT_ID"                   # must be your lease's account; stop if it is not
export ALERT_EMAIL=you@example.com
```

The whole run, end to end, with the Glue path or the Athena path. Each ends with the clean-up: it lists, then deletes
with `--yes`, and prints what is left; the output goes to `output/main.txt` or `output/main-athena.txt`:

```bash
bash scripts/live_run.sh "$ALERT_EMAIL"           # Glue: SIM01, repeats, 3 failures, resume
ENGINE=athena bash scripts/live_run.sh            # Athena: one MERGE from the sweep
```

Or step by step, from the same terminal. `etl.py up` creates the lake bucket `$PREFIX-lake-$ACCOUNT_ID` and the
workgroup `$PREFIX-wg` first (through `scripts/lake.py`), when they are missing:

```bash
uv run python sim/generate.py SIM01                          # writes sim/data/SIM01/
uv run python scripts/etl.py up --email "$ALERT_EMAIL"       # prints each resource it made
```

SNS sends a confirmation email to that address. Open it and choose **Confirm subscription**; until then no alert is
delivered, and the topic's published-message count alone does not show that an email arrived. Then land SIM01 and
start a load:

```bash
uv run python sim/replay.py SIM01 --run-id run1 --to s3 --target "$PREFIX-lake-$ACCOUNT_ID" --shift-to now
uv run python scripts/etl.py sweep                           # or wait for the hourly sweep
uv run python scripts/etl.py runs                            # each Glue run: state, seconds, DPU-seconds, cost
```

Wait until `etl.py runs` shows the latest run `SUCCEEDED`. A run loads only the objects staged when it started; the
rest wait for the next start after `ETL_MIN_GAP_MIN`. Before you count rows, check in the S3 console that every key
under `stage/trip-events/` appears in a load manifest under `lake/etl-state/`. With `ENGINE=athena` there is no Glue
run: `etl.py sweep` prints `finished run ... Athena MERGE of N objects` when its query has succeeded. This replay of
SIM01 has no broken lines, so `quarantine/` stays empty; `sim/land_cases.py` lands the case with SIM02's three broken
lines. Then clean up (Cost and clean-up, below).

`ENGINE=athena` before `etl.py up` and every later command loads with Athena instead; then `ETL_MIN_GAP_MIN`
defaults to 60 and `MAX_RUNS_PER_DAY` to 24. Both limits, set in your terminal before `etl.py up`, reach the
functions; a second `up` with another `ENGINE` changes the functions' environment too. `etl.py runs` lists the job's
runs with their `ExecutionTime`, DPU-seconds and cost.

The live scenarios of R60 and R61 make runs fail on purpose, from your terminal only: `ETL_FAULT=after-merge`
(the job stops after its `MERGE`, before the manifest) or `ETL_FAULT=hang ETL_FAULT_TIMEOUT_MIN=1` (the job sleeps
until that run's timeout), with `ETL_MIN_GAP_MIN=0` so the next one may start at once. `sim/land_cases.py` lands
SIM01 with SIM02's three broken lines, and with `--again` SIM03's copy.

### AWS permissions

| Part | Actions |
|---|---|
| `etl.py up` | `s3:GetBucketNotification`, `PutBucketNotification`, `PutObject`; `lambda:CreateFunction`, `GetFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `PutFunctionEventInvokeConfig`, `AddPermission`, `TagResource`; `iam:CreateRole`, `PutRolePolicy`, `PassRole`, `TagRole`, `GetRole`; `logs:CreateLogGroup`, `PutRetentionPolicy`; `glue:CreateJob`, `UpdateJob`, `CreateDatabase`, `CreateTable`, `GetTable`, `TagResource`; with Athena, `athena:StartQueryExecution`, `GetQueryExecution` (the table's DDL); `scheduler:CreateScheduleGroup`, `CreateSchedule`, `TagResource`; `sns:CreateTopic`, `Subscribe`, `TagResource`; `dynamodb:CreateTable`, `DescribeTable`, `TagResource`; `sts:GetCallerIdentity` |
| `etl.py sweep`, `resume` (as you) | `glue:GetJobRun`, `StartJobRun`; `dynamodb:Query`, `GetItem`, `PutItem`, `UpdateItem`; `s3:ListBucket`, `GetObject`, `PutObject`; `sns:Publish`; with Athena, `athena:StartQueryExecution`, `GetQueryExecution` |
| `clean_up.py` | the deletes of each of the above, `dynamodb:DeleteItem`, `s3:DeleteObject` |

The functions' and the job's own roles are made by `etl.py up`, each with only its prefixes, the run log and its
one job or topic. You have used the S3 trigger and `lambda:AddPermission` (A3's optional task A), and Glue jobs with
Iceberg (B3T2 and A3). The sweep function's role also needs `glue:GetJobRun`, `sns:Publish` and, with Athena,
`athena:StartQueryExecution`; `etl.py up` needs `scheduler:TagResource` for the tagged schedule group. If the sweep
function is refused an action, its run is recorded failed with the AWS error in the run log: run `uv run python
scripts/etl.py sweep` from your terminal (it uses your sign-in) until the role is fixed. A start refused this way
counts towards the hold after 3 failed runs; run `etl.py resume` once it is fixed.

## What you should see

Offline, the scenario "The rush hour loaded" (the same every run): SIM01 lands as 110 objects (one per minute of `received_time`, 09:00
to 10:49; the completions run past the hour), 110 staged objects, one run, and 2,511 rows in `trip_events_clean`,
one per `event_id`: the 4 requests delivered twice are loaded once.

On AWS, check these in your own run: every raw object gets a staged object; each bad line is in `quarantine/` with
its reason; after the last load `trip_events_clean` has one row per `event_id` (`count(*)` equals `count(DISTINCT
event_id)`); a copy of an object adds 0 rows; a failed run is recorded with its step and is followed by a run that
loads the same objects. Object counts, seconds and DPU-seconds differ from run to run (`--shift-to now` moves the
minute slots, so SIM01 lands as 110 or 111 objects).

One live run's output, for example (`output/main.txt`, `output/main-athena.txt`, and the step-by-step run in
`output/session-2026-11-23.txt`), with SIM01 and SIM02's three broken lines: the three lines quarantined with "extra
field tip", "event_time has no time zone" and "trip_id missing", and 2,508 rows in `trip_events_clean` by either
path. With Athena, the sweep function's one `MERGE` loaded 111 objects (687,641 bytes scanned). With Glue, the first
check started a run that loaded the objects staged by then (80 of 110 in the step-by-step run); the rest waited for
the 6-hour gap. Two runs stopped after their `MERGE` (`failed_step` manifest) and one hung until its 1-minute timeout
(`TIMEOUT`); the next sweep recorded "held after 3 failed runs"; after `etl.py resume` one run loaded every waiting
object. Two things AWS does that the runs showed: the hourly schedule fires within a minute of `up`, so its sweep may
load before yours; and Glue refuses a start for a few seconds after a run ends (`ConcurrentRunsExceededException`),
which the recipe records as `skipped`, not failed.

## Data

DS01 is the TLC High Volume For-Hire Vehicle trip records of March 2024 (Uber trips picked up in Manhattan). The
events are simulated from its rows of zones 48, 161 and 230 by `sim/generate.py` (simulated-runs, unchanged) and
landed by `sim/replay.py --to s3`, one object a minute.

- **SIM01 Rush hour** (R10, R30, R50): 837 trips on 2026-11-23 from 09:00 UTC, 2,511 events sent as 2,515
  deliveries (4 requests twice, 2 late), landed as 110 objects; the table then holds 2,511 rows.
- **SIM02 Bad lines** (R20): SIM01 with 3 lines of the 09:04 object broken as simulated-runs' SIM02 breaks them (an
  extra field `tip`, an `event_time` without a time zone, no `trip_id`), each in place of a delivery sent once:
  2,508 rows load, 3 lines are quarantined.
- **SIM03 Landed twice** (R40): SIM01 with the 09:30 object copied again under `…-again.jsonl`, and its S3 event
  delivered twice: 111 raw objects, 2,511 rows, 1 repeat event counted.
- **SIM04 Failing run** (R60): SIM01, loaded first with the job failing after its commit and before the manifest,
  then again: 2,511 rows after both, 1 run `failed` and 1 email.

These are built by the scenarios' steps from SIM01 (`features/steps/steps.py`), not by `sim/generate.py`.

**Table** `trip_events_clean`: `event_id`, `event_type`, `trip_id`, `pickup_zone_id`, `driver_id`, `dropoff_zone_id`,
`trip_miles`, `trip_time_s`, `fare`, `driver_pay`, `event_time`, `event_day`, `delivery_id`, `received_time`,
`source_key`; partitioned by `event_day`. **Run log items**: `etl-object` (key; `sequencer`, `good_lines`,
`bad_lines`, `repeats`, `checked_at`), `etl` (start time; `run_state`, `failed_step`, `error_text`, `glue_run_id`,
`objects`, `started_at`, `ended_at`), `etl-lock` (`lock_until`, `resumed_at`).

## Cost and clean-up

Prices: USD, ca-central-1, free tier left out, from the course price sheet (page `436c-student/prices.md` on the
course server): Glue 0.44 USD per DPU-hour (Glue 5.0), Athena 5.50 USD per TB scanned with a 10 MB minimum a query,
S3, Lambda and DynamoDB as the sheet lists them. Rates the sheet does not carry are from AWS's public price list files for ca-central-1, the source
the sheet names: CloudWatch Logs ingestion 0.55 USD per GB and the Glue Data Catalog 1 USD per million requests; the
Scheduler's rate (1.10 USD per million invocations) is *unchecked*. CAD at 1.45
per USD, the course's planning rate. Byte counts are converted with 1 GB = 2^30 bytes (the course
convention). The
figures are *computed* from those prices and the counts below, and are not a spending cap. Traffic: one object a
minute, 2,515 deliveries an hour, 230 B a line (*assumed*); a Glue run of 191 DPU-seconds and a check of 3.5 s
(*measured*, below).

| Part | One run: an hour checked, and one load | 30 days | A year |
|---|---|---|---|
| Check function: 60 calls of 3.5 s at 128 MB (*measured* median), and their requests | 0.00036 (0.00052 CAD) | 0.26 (0.38 CAD) | 3.17 (4.59 CAD) |
| S3 PUT: 60 staged objects (quarantine objects are rare) | 0.00033 (0.00048 CAD) | 0.24 (0.34 CAD) | 2.89 (4.19 CAD) |
| S3 GET: the check reads 60 raw objects, the load 60 staged ones | 0.000053 (0.000077 CAD) | 0.038 (0.055 CAD) | 0.46 (0.67 CAD) |
| DynamoDB run log: 60 writes and 120 strongly consistent reads | 0.000058 (0.000084 CAD) | 0.042 (0.060 CAD) | 0.51 (0.73 CAD) |
| CloudWatch Logs, 2 lines of 300 B a call (*assumed*) | 0.000018 (0.000027 CAD) | 0.013 (0.019 CAD) | 0.16 (0.24 CAD) |
| Sweep: 1 call, 1 Scheduler call, 6 LIST and 3 GET an hour | 0.000037 (0.000054 CAD) | 0.027 (0.039 CAD) | 0.33 (0.47 CAD) |
| Glue load, 4 runs a day: 191 DPU-seconds × 0.44 per DPU-hour (*measured*), 0.5 MB of logs, about 30 catalog and 30 S3 requests a run (*assumed*) | 0.0237 (0.0344 CAD) | 120 runs: 2.84 (4.12 CAD) | 1,460 runs: 34.6 (50.2 CAD) |
| S3 storage: staged lines and the table, 0.49 GB at the month's end; 5.9 GB at the year's end | — | 0.006 (0.009 CAD) | 0.89 (1.28 CAD) |
| **Total with Glue** | **0.025 (0.036 CAD)** | **3.47 (5.03 CAD), 11.6% of 30 USD** | **43.0 (62.4 CAD), 143% of 30 USD** |
| Athena load instead, hourly: about 20 MB scanned (*assumed*), 770 S3 GET, 15 S3 PUT, the sweep waiting 30 s at 128 MB, 20 catalog requests | 0.00060 (0.00087 CAD) | 720 runs: 0.43 (0.63 CAD) | 8,760 runs: 5.3 (7.6 CAD) |
| **Total with Athena** | **0.0015 (0.0021 CAD)** | **1.06 (1.53 CAD), 3.5% of 30 USD** | **13.7 (19.8 CAD), 46% of 30 USD** |

Of the 30-day Glue total, the Glue line is 82%: every run pays for its start, however little it loads. A year of
Glue at the default limits is more than the lease: run the Glue path for demonstrations and tests, or use Athena.
The limits bound the worst case: 4 runs of 10 minutes a day is 0.59 USD (0.85 CAD) a day. The Athena estimate
assumes the table read stays at the named days; if it does not, the scan grows with the table, so measure
`DataScannedInBytes` on a table that holds data. Deleting the notification entry and the schedule group stops new
runs; a run already started goes on until it ends.

**Measured** (live run of 2026-11-23; DPU-seconds from `GetJobRun`, which gave `DPUSeconds` on these standard Glue
5.0 runs; Lambda from the REPORT lines; Athena from `GetQueryExecution`, billed at the 10 MB minimum):

| Part | Units, *measured* | USD | CAD |
|---|---|---|---|
| Glue, the run that loaded SIM01 (80 objects) | `ExecutionTime` 95 s, 191 DPU-seconds | 0.0233 | 0.0338 |
| Glue, the run after `resume` (every waiting object) | 111 s, 223 DPU-seconds | 0.0273 | 0.0395 |
| Glue, the three failed runs (after-merge 97 s and 120 s, hang to `TIMEOUT` 70 s) | 194 + 241 + 140 DPU-seconds | 0.0703 | 0.1019 |
| Athena `MERGE` of 111 objects into the empty table | 687,641 bytes scanned, billed 10 MB | 0.00005 | 0.00008 |
| Lambda: 112 checks (median 3.5 s, p90 4.1 s), 8 sweeps (median 11.5 s) at 128 MB | 81.9 GB-seconds, 120 requests | 0.0011 | 0.0016 |
| **The session** | | **0.122** | **0.177** |
| `live_run.sh` end to end, Glue (5 runs: 207 + 183 + 141 + 188 + 137 DPU-seconds) | 856 DPU-seconds | 0.105 | 0.152 |

Against the 30 USD lease: 0.41% for the session; the one loading run is 0.08%. The three failures cost three times a
load, and a real hang at the job's 10-minute timeout would cost 2 DPU × 10 min = 0.147 USD.

Clean up from the terminal with your lease's credentials, in the recipe folder, with the same `PREFIX`. First check
that no load is running: `uv run python scripts/etl.py runs` must show no `RUNNING` run (a run ends within the job's
10-minute timeout; with Athena, the Athena console's query history must show no running query). Then list, read the
list, and delete:

```bash
export PREFIX=etl-yourname
uv run python clean_up.py            # lists what it would remove; ends "Nothing deleted. Run again with --yes"
uv run python clean_up.py --yes      # deletes; exit 1 if a step was refused
```

It removes the notification entry first (the bucket's other entries kept), then the schedule group, both functions
and their log groups, the Glue job, the roles, the topic, the run log's `etl`, `etl-object` and `etl-lock` items, the
tables `trip_events_clean` and `trip_events_stage`, and the prefixes `stage/trip-events/`,
`quarantine/trip-events-rows/`, `lake/etl-state/`, `lake/trip_events_clean/` and `jobs/s3-event-etl/`. It keeps the
bucket, `raw/trip-events/` and the workgroup; they bill for what they store until they are deleted.

The bucket and workgroup are shared with the other recipes of your project. Delete them only when no recipe uses them
any more, and after each has run its own clean-up:

```bash
uv run python scripts/lake.py down          # lists what it would delete; refuses while another recipe's files are left
uv run python scripts/lake.py down --yes    # deletes the bucket and workgroups this ledger records created
```

`lake.py down --yes` deletes everything under `raw/trip-events/` and `athena-results/`, other recipes' files there
included. If another recipe still keeps data in them, do not run it: keep the bucket and list it as kept.

## Adapt it

1. **Your rows.** The delivery model in `src/rides/events.py` is what a line must match; give your own model the
   same `validate_line` and `LineError`, and `reason()` in `pipeline/check.py` its words.
2. **Your table.** `COLUMNS` in `pipeline/etl_job.py` (and the casts in `pipeline/athena_load.py`).
3. **Your limits.** `ETL_MIN_GAP_MIN` and `MAX_RUNS_PER_DAY` in the functions' environment; the open days
   (`OPEN_DAYS` in `pipeline/control.py`) for how late a file may land.
4. **Your prefix.** The filter of the entry in `scripts/etl.py` and `RAW` in `pipeline/check.py`, together.
