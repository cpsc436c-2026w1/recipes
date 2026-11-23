# Test runs whose right answer is known before they run

## What you build

A seeded generator writes a simulated hour of rides events from real trip records, adds faults on purpose (an event
delivered twice, delivered late, never delivered, or corrected later), and writes down the events as they happened
and the faults it added (`_truth/`), so the answer is known before anything runs. One replayer sends that hour to a Kinesis stream, an SQS queue, a function URL, the lake
bucket or a function in the same process, at a speed you choose, and a scenario then checks your tables against the
answer: offline in moto (a Python library that imitates the AWS APIs inside the test process, so no AWS resource is
made), and on AWS. A daily check sends a small probe to the deployed system, counts it later, and
emails the team when the count is wrong or the check did not run.

## Use it when

Your system takes events from a stream, a queue, an API or files, and you need a test run whose right answer you
know before it runs: a *Then* (the expected-result step of a Behave scenario, which is written as Given, When and
Then steps) such as "zone 161 has 188 trips" that fails when a repeat is counted twice or a late
event is dropped. This recipe also holds the pieces the other recipes share: the event models (`src/rides/events.py`),
the replayer (`sim/replay.py`), the shared lake bucket and Athena workgroups (`scripts/lake.py`), the course tags and
the creation ledger (`scripts/tags.py`) and the run log (`scripts/run_log.py`); other recipes copy them unchanged.

For a pipeline that is one stream into one table, [streaming-ingestion](../streaming-ingestion/) builds the pipeline
itself; use this recipe to feed it and check it.

## Main scenario

1. `sim/generate.py SIM01 --seed 436` reads 837 trips of the DS01 sample (`sim/source/ds01_sample.csv`) and makes
   three events per trip (request; accept 3 to 15 s later; completion 5 minutes plus the trip time after the accept),
   one delivery per event received 1 s after it happened, and the injected faults the data set's line in
   `sim/datasets.toml` gives. It writes `sim/data/SIM01/`: the deliveries in
   `trip-events/dt=<day>/hour=<hour>/deliveries.jsonl` by day and hour of receipt (the folders of `raw/trip-events/`
   in the lake bucket), and under `_truth/` the events as they happened (`events.jsonl`) and one line per injected
   fault (`faults.jsonl`). Every line is built as a Pydantic model and written with sorted keys. Nothing under
   `_truth/` is sent to the system.
2. `sim/truth.py` computes the answer from `_truth/events.jsonl` and `_truth/faults.jsonl` when a scenario needs it:
   each event once, late ones included, the `never` events left out, a corrected trip with its latest values.
3. `sim/replay.py SIM01 --speed 60 --run-id <id> --to kinesis` sends each delivery (its received_time minus the first
   received_time) / 60 after the start, with the run id in front of every id, and writes each send to
   `evidence/<run-id>/sent.jsonl`.
4. Offline, `uv run behave` replays into stand-ins (moto for AWS, `rides.pipeline` for your pipeline) and compares
   each table a scenario names with that answer. A scenario tagged `@live` uses AWS instead: in this recipe its Then
   reads back the stream it replayed into (the recipe has no pipeline of its own); in your project, replace that Then
   step with a read of your own deployed tables before you use it as a project test.
5. `scripts/check.py up` deploys the daily check: three EventBridge Scheduler schedules call Lambda function
   `<PREFIX>-live-check` to send the probe SIM03, to count its 20 trips `CHECK_DELAY_MIN` later into the run log and
   metric `LiveCheck/CheckPassed`, and at 00:15 to email the team through SNS when the day just ended had no passing
   check. `scripts/check.py alarm` adds a backstop alarm.
6. `clean_up.py` lists what the recipe made and deletes nothing; `clean_up.py --yes` deletes it. After every recipe
   that uses the shared bucket and workgroups has run its own clean-up, `scripts/lake.py down --yes` deletes them;
   `clean_up.py --check` lists what is left by schedule group and by name prefix.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| One definition of the events (R50) | Pydantic models in `src/rides/events.py`, `extra="forbid"`, an aware time for every time; the generator, the stand-in pipeline and the steps import them; the replayer does not (it needs only `event_id` and `received_time`, so it also sends a team's own lines) | a field renamed in one place fails everywhere at once; a line of `_truth/` with an unknown field or a time without a zone is refused with its file, line and field, not passed over; the stand-in pipeline refuses a delivery that does not match, and the replay writes its batch as failed in `sent.jsonl` | course constraint: offline first; DDIA 2e ch. 5 (schemas and their evolution) |
| `event_id` on every event, the completion too (R30, R40) | an id per event, kept by every copy of a delivery | `twice` and `changed` are counted by event id; a completion without an id cannot be told from its repeat | L14 (at-least-once delivery, idempotent writes) |
| What the answer counts (R40) | each event once, late ones included, `never` left out, a corrected trip's latest values | a late or repeated delivery changes what the system receives, not what happened; only an event that never arrives cannot be in the system's answer | DDIA 2e ch. 12 (event time against processing time) |
| Four kinds of fault (R30) | `twice`, `late`, `never`, `changed` (a later event of the same kind with a new id) | twice, late and never are what a stream does; a correction with a later `event_time` lets a merge order it | L14 |
| The same seed gives the same bytes (R20) | one `random.Random(seed)`, SIM04 draws SIM01's picks first, sorted keys | a scenario can name exact numbers, and SIM04's faults leave SIM01's six touched trips alone | ours: a Then needs a value known before the run |
| The run id in front of every id of a replay (R60) | `r1-t-0001`, `r1-ev-000001`, `r1-dl-000001` | a second replay into a system that keeps event ids would be dropped as repeats, so its data latency would measure the deduplication | L13 (data latency) |
| Send times written down (R60) | `evidence/<run-id>/sent.jsonl`: `delivery_id`, `event_id`, `sent_time`, `target`, `result` | data latency is measured from when a delivery was sent, and a stored item is joined to its send by `delivery_id` | L13 (data latency, p95) |
| A slow call does not push later sends later (R60) | the replayer waits for the next delivery's time, then sends it with every later delivery already due, up to the target's limit per call: 500 records for Kinesis ([PutRecords](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html)), 10 messages for SQS ([SendMessageBatch](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatch.html)), and 100 deliveries a POST for a function URL (the recipe's own batch size, not an AWS limit) | at speed 60 a call per delivery would fall behind; batching what is due keeps each send within 1 s (the latest send was 0.042 s after its planned time, measured, `output/main.txt`) | REL05-BP03 for the retries (backoff with jitter) |
| One file per slot for the bucket (R60) | `--to s3` writes one JSON Lines object per `--slot-min` minutes at `raw/trip-events/dt=<UTC day>/<slot start>-<run id>.jsonl`, sent when the slot's last delivery is due | a lake ingests files, and one PUT per delivery is 2,515 PUTs a run against 110; the 1 s rule applies to the per-delivery targets | course lake layout (raw files by arrival day) |
| Offline stand-ins and what they do not show (R70) | moto for Kinesis, SQS, S3, DynamoDB, SNS, Lambda and the Scheduler; DuckDB for the `SELECT` and aggregates; Athena-only statements (`MERGE`, `OPTIMIZE`, CTAS `WITH`) only under `@live` | the offline run is free and repeatable. It does not show a stream's or a batching window's delay, IAM refusals, throttling, Lambda's concurrency limit, cold starts, Athena's 10 MB minimum, or what `MERGE`, `OPTIMIZE` and CTAS `WITH` produce | course constraint 5 |
| What "the deployed system" is in this recipe's own live run (R80) | the records on a 1-shard stream and the slot files in the bucket, read back and counted with the same SQL as offline | the recipe has no pipeline of its own to deploy; your `@live` step reads your deployed tables instead | ours |
| The live stream is deleted at once (R80) | `scripts/live_run.py stream-up`, the replay, then `stream-down`; the ledger records it so `clean_up.py` deletes it if a run stops halfway | a provisioned shard bills by the hour whether or not anything is sent | course constraint 2 |
| Two schedules, the read `CHECK_DELAY_MIN` after the send (R90) | send at `CHECK_CRON`, read at the send time plus `CHECK_DELAY_MIN` (default 90: a data latency of 75 min at p95 plus 15); a read past midnight is refused | a waiting function bills its wait and Lambda stops it at 30 minutes ([timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)); a fixed 15 minutes fails every day for a pipeline that merges hourly | Lambda timeout (AWS docs) |
| A second send of one day changes nothing (R90) | no check that a call is still running; the probe's ids are the same in both sends | the Scheduler delivers at least once; the deployed system keeps one row per event id, so the count stays 20 | L14 (idempotent writes) |
| The function carries only boto3 code (R90) | the zip holds `live_check.py`, `replay.py` and the probe's 60 deliveries; no Pydantic | the Lambda Python runtime has boto3 and not Pydantic, so the function needs no layer and no build step | ours: smallest deploy |
| How the probe is counted (R90) | `CHECK_QUERY`: `dynamodb:<table>` (a scan filtered to the day's `probe-<date>-` ids), `s3:<bucket>` (the day's probe files) or `athena:<SQL>` (in `<PREFIX>-wg`) | each team stores its events elsewhere; a filtered scan is fine for a demonstration table, and a table of millions should be counted by a query on a key instead | ours: count by a key on a large table |
| A verdict at 00:15 that emails, an alarm as its backstop (R100, R110) | the verdict reads the day's item, strongly consistent, and publishes one message when it is missing or not finished; a 26-hour alarm on `LiveCheck/CheckPassed` with missing data breaching | the email reaches the team within an hour after the day ends; the alarm catches a verdict that does not run, and warns 26 hours or more after the last passing check ([missing data](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html)) | OPS08-BP04 (actionable alerts) |
| The verdict judges only the days a send was due (R100) | it works out from `CHECK_CRON` in `CHECK_TZ` whether the date just ended had a send; on a date with none it records and sends nothing | a weekly `CHECK_CRON` would otherwise report "no check" on the other six days | ours |
| Local dates from the time zone database (R100) | `CHECK_TZ` (default America/Vancouver) for the schedules and for the run log's date | the Scheduler's cron follows a zone's clock changes ([schedule types](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html)); a zone that changes its clocks has days of 23 and 25 hours, which the same code handles | ours |
| Asynchronous retries 0, schedule retries 0 (R90, R100) | `put_function_event_invoke_config(MaximumRetryAttempts=0)` and `RetryPolicy.MaximumRetryAttempts=0` | a failed call is that day's result; a retry would send the probe again | ours |
| One least-privilege role per caller (R90, R100) | the function may write its log group, put the `LiveCheck` metric, `PutItem` and `GetItem` on `<PREFIX>-runs`, publish to `<PREFIX>-check-alerts`, send to the one target and read the one query's table; the Scheduler's role may invoke the one function | a policy names the resources it needs and no others | SEC03-BP02 |
| CI offline only (R120) | `.github/workflows/offline.yml` runs `uv run pytest` and `uv run behave` without `@live` and `@eval`, and holds no AWS secret or role | a lease sign-in ends after a few hours; a role for an outside service is not on record as allowed; an `@eval` scenario spends the team's gateway pool | course constraint 4 |
| Every create recorded (R130) | `scripts/tags.py`: the course tags in each create call, and one line per create, borrow or delete in `state/ledger.jsonl` | clean-up deletes what this recipe created and keeps what was there before; a resource that takes no tags (a schedule) is still on the list | AWS tagging best practices: tag in the create call |
| The shared bucket and workgroups are deleted last, by one script (R130) | a bare `scripts/lake.py down` lists what it would delete; `scripts/lake.py down --yes` deletes `<PREFIX>-wg`, `<PREFIX>-ctas-wg` and the bucket only when the ledger records them `created`, and refuses while another recipe's objects are left outside `raw/trip-events/` and `athena-results/` | other recipes borrow them; run it only after every recipe that uses them has run its own clean-up | ours |
| Clean-up deletes only this recipe's files in the shared prefix (R130) | `clean_up.py` deletes the slot files under `raw/trip-events/` whose run id is one this recipe used (a ledger `s3-replay` row, written by `live_run.py run --to s3`, or a folder of `evidence/`) and the probe's files when this ledger deployed the check | other recipes (s3-event-etl, cdc-to-lake, streaming-ingestion) replay into the same prefix; deleting all of it would delete their data | ours |
| The DS01 sample as CSV | `sim/source/ds01_sample.csv`, 837 rows, 70 KB | readable in a diff and with no Parquet package; `sim/source/extract_seed.sql` wrote it from the published TLC Parquet file (DuckDB) | ours |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets after a requirement's
number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When the generator runs, the recipe shall write four files. |
| state | While <something is the case>, the recipe shall ... | While a stream is being deleted, the recipe shall report it as going. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a line has an unknown field, then the recipe shall stop. |
| optional | Where <an extension or tag is used>, the recipe shall ... | Where a scenario is tagged `@live`, the recipe shall use AWS. |
| complex | two or more of these together | When the verdict runs, if the day has no check, then the recipe shall email. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number; `uv run behave` runs
them offline. The recipe has more requirements than a team keeps for one story (2 to 5), because it serves every
pipeline shape. The numbers are this recipe's own.

- **R10** (event) When the generator runs with a data set id and a seed, the recipe shall write the deliveries to
  `sim/data/<id>/trip-events/dt=<day>/hour=<hour>/deliveries.jsonl` by day and hour of receipt, and the events and the
  injected faults to `sim/data/<id>/_truth/events.jsonl` and `_truth/faults.jsonl`.
- **R20** (event) When the generator runs again with the same data set id and seed, the recipe shall write
  byte-identical files.
- **R30** (ubiquitous) The recipe shall record in `_truth/faults.jsonl` each injected fault with its kind (`twice`, `late`,
  `never`, `changed`) and the ids it added, delayed or held back, as many of each kind as the data set's line under
  Data gives.
- **R40** (ubiquitous) The recipe shall compute the answers the scenarios state from `_truth/events.jsonl` and
  `_truth/faults.jsonl`, with each event counted once, late events included and `never` events left out.
- **R50** (unwanted) If a line of `_truth/events.jsonl` does not match its model (an unknown field, a time without a
  time zone, a missing field), or a line of a `deliveries.jsonl` has no `event_id` or no `received_time` with a time
  zone, then the recipe shall stop with an error that names the file and the line (and, for `_truth/`, the field).
- **R60** (event) When the replayer sends a data set at speed k with a run id, the recipe shall send each delivery
  (its received_time minus the first received_time) / k after the start, within 1 s (for `s3`: each slot file when
  its last delivery is due), with the run id in front of every `event_id`, `trip_id` and `delivery_id`, to the
  target named, and write each send to `sent.jsonl`.
  <!--src: ours: a send later than 1 s would add to the data latency it measures -->
- **R70** (event) When the whole-run scenario runs offline, the recipe shall replay the data set into stand-ins for
  the cloud services and the pipeline, and compare each table the scenario names with the answer from `_truth/`.
- **R80** (optional) Where a scenario is tagged `@live`, the recipe shall replay the data set into the deployed
  system, check the same Then against what the deployed system holds, and keep the run's output in
  `evidence/<run-id>/`.
- **R90** (event) When the send schedule fires, the recipe shall send the probe SIM03 to the deployed system and,
  `CHECK_DELAY_MIN` minutes later, write the count of that day's probe trips to the day's item in the run log.
- **R100** (complex) When the verdict schedule fires at 00:15, if the local date that has just ended was due a send
  and has no finished check (a wrong count, a failed read or no item), then the recipe shall publish one message
  naming the date and what was wrong to topic `<PREFIX>-check-alerts`.
- **R110** (unwanted) If no check has passed for 26 hours, then the recipe shall raise alarm `<PREFIX>-check-failed`
  to the same topic.
- **R120** (event) When a commit is pushed, the recipe shall run `uv run pytest` and `uv run behave` without the
  `@live` and `@eval` scenarios in GitHub Actions, with no AWS credentials.
- **R130** (event) When `clean_up.py --yes` and then `scripts/lake.py down --yes` run, the recipe shall delete every
  resource its ledger records `created` and keep every one it records `borrowed`.
- **R131** (event) When `clean_up.py --yes` runs, the recipe shall delete under `raw/trip-events/` only the files
  whose run id one of its own replays used (or the probe's, when its ledger deployed the check), and keep every other
  file.

## Data

DS01 is the Checkpoint 1 example's DS01 ([cpsc436c-2026w1/cp1-example](https://github.com/cpsc436c-2026w1/cp1-example)):
the NYC TLC High Volume For-Hire Vehicle trip records, Uber trips picked up in Manhattan in March 2024, 6,045,943 rows
<!--src: measured: cp1-example, the count of the DS01 subset -->. The sample in `sim/source/ds01_sample.csv` is its
837 trips of zones 48, 161 and 230 requested on 2024-03-14 from 08:00 to 09:00 New York time, moved to 09:00 to 10:00
UTC on 2026-11-23. Every data set is simulated from it; the ids are this recipe's.

| Data set | What it is | Events | Deliveries | Injected faults | Answer from `_truth/` |
|---|---|---|---|---|---|
| SIM01 rush-hour | the 837 trips: 304, 191 and 342 in zones 48, 161 and 230 | 2,511 | 2,515 | 4 requests `twice`, 2 requests `late` by 6 to 10 minutes | 2,511 events, 837 trips |
| SIM02 bad-lines | SIM01's first 10 events, three lines broken on purpose: line 4 has a field `tip`, line 7 an `event_time` without a time zone, line 9 no `trip_id` | 10 | 10 | none (three `broken` lines in `_truth/faults.jsonl`) | the 7 good lines |
| SIM03 probe | the first 20 trips of zone 161, each event delivered once; the check sends it under the run id `probe-<local date>` | 60 | 60 | none | 60 events, 20 trips |
| SIM04 all-faults | SIM01's 4 `twice` and 2 `late` requests; `never`: the 3 events of each of 3 trips of zone 161; `changed`: 2 trips of zone 230 get a second `trip_completed`, 5 minutes later, fare 5.00 higher. None of these 5 trips is one SIM01's faults touch | 2,513 | 2,508 | 4, 2, 9, 2 | 2,504 events, 834 trips (304, 188, 342), 834 completed |

<!--src: ours: the counts the generator writes with seed 436, checked by the tests -->

Event format (each delivery also has `delivery_id` and `received_time`):

- `trip_requested`: `event_id`, `trip_id`, `pickup_zone_id`, `event_time`
- `trip_accepted`: `event_id`, `trip_id`, `driver_id`, `event_time`
- `trip_completed`: `event_id`, `trip_id`, `driver_id`, `dropoff_zone_id`, `trip_miles`, `trip_time_s`, `fare`,
  `driver_pay`, `event_time`

Layout of a data set (the layout of the course's checkpoint example):

```text
sim/data/SIM01/
  trip-events/dt=2026-11-23/hour=09/deliveries.jsonl   what the system receives, by day and hour of receipt
  trip-events/dt=2026-11-23/hour=10/deliveries.jsonl
  trip-events/dt=2026-11-23/hour=11/deliveries.jsonl
  _truth/events.jsonl                                  each event once, as it happened
  _truth/faults.jsonl                                  one line per injected fault; never sent to the system
```

`_truth/faults.jsonl`, one object per line: `{"fault": "twice", "delivery_id", "event_id"}` (the second copy's
delivery id), `{"fault": "late", "delivery_id", "event_id"}`, `{"fault": "never", "event_id", "trip_id"}`,
`{"fault": "changed", "event_id", "trip_id"}` (the correction's event id), and in SIM02
`{"fault": "broken", "line", "field"}`. The answer `sim/truth.py` computes (`truth.answer(folder)`): `events`,
`trips`, `requested_per_zone`, `requested_per_zone_day`, `accepted`, `completed`, `fare_by_trip`; add a key in
`sim/truth.py` for each table your own scenarios name. The seed is the one in `sim/datasets.toml` unless
`--seed` gives another.

## Run it

> ⚠️ Commands run in two places. Offline commands run in a terminal on your laptop, in the copied recipe folder, and
> need no AWS account. AWS commands run in a laptop terminal (or CloudShell) in the same folder, with your lease's
> credentials active and the region set to `ca-central-1`. Running an AWS command with another account's credentials
> creates resources there.

From your laptop, in a terminal in the recipe folder (offline):

```bash
uv sync                                  # installs the dependencies; it should finish without an error
uv run pytest -q                         # every test should pass
uv run behave                            # the scenarios of the Requirements section; none should fail
uv run python sim/generate.py --all      # writes SIM01 to SIM04 into sim/data/
uv run python sim/replay.py SIM01 --speed 0 --run-id try1 --to local --target replay:discard    # nothing is stored
```

The plain `--to local` target sends into this recipe's stand-in pipeline, which stores events in a DynamoDB table
(`<PREFIX>-sim-events`): with AWS credentials active it creates that table in your account. Keep
`--target replay:discard` for a run that calls no AWS service.

The local replay writes `evidence/try1/sent.jsonl`, one line per delivery; speed 0 sends without waiting.

From a terminal with your lease's credentials, in the recipe folder (a stream bills by the hour while it exists, so
delete it right after the replay). First choose a short lowercase prefix that no other resource in your lease uses,
and check that the account is your lease's:

```bash
export PREFIX=recipe-yourname
export AWS_DEFAULT_REGION=ca-central-1
aws sts get-caller-identity                                       # the Account must be your lease's; stop if not
uv run python scripts/lake.py up                                  # the shared bucket and workgroups, if missing
uv run python scripts/live_run.py stream-up                       # 1 provisioned shard; prints "created  Kinesis stream ..."
LIVE_TARGET=kinesis LIVE_SPEED=60 uv run behave --tags=@live features/whole_run.feature
uv run python scripts/live_run.py stream-down                     # now; prints "removed  Kinesis stream ..."
RUN_ID="$(date +%F)-SIM01-s3"
uv run python scripts/live_run.py run SIM01 --to s3 --speed 0 --run-id "$RUN_ID"
```

The `@live` scenario checks delivery to and read-back from this recipe's own stream; it does not read your project's
tables until you replace its Then step. `live_run.py run` ends with `same` when the counts it read back equal the
answer from `_truth/`.

The daily check of your deployed system needs to know where the probe goes and how its trips are counted. Set both,
and the email address that gets the alerts, on their own lines:

```bash
export CHECK_TARGET=kinesis:YOUR_STREAM CHECK_QUERY=dynamodb:YOUR_TABLE
export ALERT_EMAIL=you@example.com
uv run python scripts/check.py up --email "$ALERT_EMAIL"
```

SNS sends a confirmation email to that address. Open it and choose **Confirm subscription**; until then no alert is
delivered. In the SNS console, the subscription's status should then read `Confirmed`. To try the check by hand:

```bash
uv run python scripts/check.py invoke send
```

Wait `CHECK_DELAY_MIN` minutes (90 by default) so your pipeline has time to store the probe, then read it:

```bash
uv run python scripts/check.py invoke read --date "$(TZ=America/Vancouver date +%F)"
uv run python scripts/check.py alarm                # after the first read has put its point
```

The read prints the day's item: `run_state` should be `finished` and `checked_count` 20. A read made before your
pipeline has stored the probe counts fewer trips and reports the day as failed.

A replay of SIM01 at speed 1 takes about 2 hours 17 minutes (its completions run past the hour: 137 s at speed 60, measured); run it from a laptop terminal, not CloudShell (a
CloudShell session ends after 20 to 30 minutes without keyboard input).

### Replay your own SIM01

The replayer sends any data set laid out as `sim/data/<SIM>/<source>/dt=<day>/hour=<hour>/deliveries.jsonl`, with
your own event types and your own name for `<source>`. Each line needs `event_id` and `received_time` (ISO 8601 with
a time zone); the other fields are sent as they are. A line without `delivery_id` is given one from its file and
line (`line-2026-11-23-09-000001`) in `sent.jsonl`, and is sent without it. The replayer does not check your fields
against your models, so run your own check of the data set first. The default `--to local` target, this recipe's
stand-in pipeline, takes only this recipe's rides events: replay your own lines to `--target replay:discard`, which
keeps nothing, so `sent.jsonl` is the record of the order and the send times.

For example, the SIM01 of tutorial B5T2, cloned into your home folder:

```bash
git clone https://github.com/cpsc436c-2026w1/b5-t2-behave-sim ~/b5-t2-behave-sim
(cd ~/b5-t2-behave-sim && uv sync && uv run python -m sim.generate)    # writes SIM01 to SIM03 into its sim/data/
uv run python sim/replay.py SIM01 --run-id b5t2-try1 --to local --target replay:discard --data-dir ~/b5-t2-behave-sim/sim/data
```

The replay should end with `2515 sent, 0 failed` and write `evidence/b5t2-try1/sent.jsonl`. The tutorial's delivery
files are under `trip-events/`, its only folder besides `_truth/`, so the replayer finds them without `--source`.
Where your SIM01 has more than one such folder, the replayer stops and names them; name the one to send:

```bash
uv run python sim/replay.py SIM01 --run-id team-try1 --to local --target replay:discard --data-dir ~/your-team-repo/sim/data --source orders
```

To send your lines into your own system, run the same command with your lease's credentials active (as for the AWS
commands above), with `--to kinesis --target <your stream>` or `--to s3 --target <your bucket>` in place of
`--to local --target replay:discard`. The `s3` target writes under `raw/trip-events/`, whatever your source folder
is called.

### AWS permissions

| Part | Actions your sign-in needs |
|---|---|
| Lake bucket and workgroups (`scripts/lake.py`) | `s3:CreateBucket`, `s3:TagResource` (tags in the create call), `s3:PutBucketPublicAccessBlock`, `s3:PutLifecycleConfiguration`, `s3:ListBucket`, `s3:ListBucketVersions`, `s3:DeleteObject`, `s3:DeleteObjectVersion`, `s3:DeleteBucket`; `athena:CreateWorkGroup`, `athena:GetWorkGroup`, `athena:ListWorkGroups`, `athena:DeleteWorkGroup`, `athena:TagResource`; `sts:GetCallerIdentity` |
| Replay (`sim/replay.py`, `scripts/live_run.py`) | `kinesis:CreateStream`, `kinesis:AddTagsToStream`, `kinesis:DescribeStreamSummary`, `kinesis:PutRecords`, `kinesis:ListShards`, `kinesis:GetShardIterator`, `kinesis:GetRecords`, `kinesis:DeleteStream`; `sqs:GetQueueUrl`, `sqs:SendMessage` (for `--to sqs`); `s3:PutObject`, `s3:GetObject`; `lambda:InvokeFunctionUrl` (for `--to url`) |
| Daily check (`scripts/check.py`) | `dynamodb:CreateTable`, `dynamodb:DescribeTable`, `dynamodb:TagResource`; `sns:CreateTopic`, `sns:TagResource`, `sns:Subscribe`; `iam:CreateRole`, `iam:TagRole`, `iam:GetRole`, `iam:PutRolePolicy`, `iam:PassRole`; `logs:CreateLogGroup`, `logs:TagResource`, `logs:PutRetentionPolicy`; `lambda:CreateFunction`, `lambda:TagResource`, `lambda:GetFunction`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `lambda:PutFunctionEventInvokeConfig`, `lambda:InvokeFunction`; `scheduler:CreateScheduleGroup`, `scheduler:TagResource`, `scheduler:CreateSchedule`, `scheduler:UpdateSchedule`; `cloudwatch:PutMetricAlarm`, `cloudwatch:TagResource`, `cloudwatch:DescribeAlarms` |
| Clean-up and its proof (`clean_up.py`) | `sts:GetCallerIdentity`; the look-ups `kinesis:DescribeStreamSummary`, `scheduler:GetScheduleGroup`, `scheduler:GetSchedule`, `cloudwatch:DescribeAlarms`, `lambda:GetFunction`, `logs:DescribeLogGroups`, `sns:GetTopicAttributes`, `iam:GetRole`, `dynamodb:DescribeTable`, `s3:ListBucket`; the deletes (`kinesis:DeleteStream`, `scheduler:DeleteScheduleGroup`, `scheduler:DeleteSchedule` for schedules in group `default`, `cloudwatch:DeleteAlarms`, `lambda:DeleteFunction`, `logs:DeleteLogGroup`, `sns:DeleteTopic`, `iam:ListRolePolicies`, `iam:DeleteRolePolicy`, `iam:DeleteRole`, `dynamodb:DeleteTable`, `s3:DeleteObject`), and for `--check`: `scheduler:ListSchedules`, `tag:GetResources`, `scheduler:ListScheduleGroups`, `lambda:ListFunctions`, `logs:DescribeLogGroups`, `kinesis:ListStreams`, `dynamodb:ListTables`, `sns:ListTopics`, `iam:ListRoles`, `athena:ListWorkGroups`, `s3:ListAllMyBuckets` |

You have used EventBridge Scheduler with its role and SNS email subscriptions (Assignment 3), and Lambda with its
execution role (Assignment 1). Three calls may be refused in your lease; each has a fallback, and the script prints
`REFUSED` with the call's name when it takes one:

- `lambda:PutFunctionEventInvokeConfig` refused: `check.py up` goes on, and Lambda's asynchronous retries stay at 2.
  A retried send repeats the probe's ids, so the count stays 20.
- `scheduler:CreateScheduleGroup` with tags (`scheduler:TagResource`) refused: the three schedules go in group
  `default`. `clean_up.py` then needs `scheduler:GetSchedule` and `scheduler:DeleteSchedule` to delete them by the
  name prefix `$PREFIX-check-`; if those are refused too, delete the three schedules in the EventBridge Scheduler
  console and run `clean_up.py --check` again.
- `tag:GetResources` refused: `clean_up.py --check` lists by name prefix only.

If any other call is refused, the script stops with the AWS error. Run `uv run python clean_up.py` to list what this
attempt created, then `uv run python clean_up.py --yes` to delete it, before you try again.

## What you should see

One run's output, for example, from [`output/main.txt`](output/main.txt) (account masked; your resource names begin
with your own `PREFIX`):

```text
sent     2515 deliveries to kinesis:recipe-simruns-trip-events in 1916 calls, 137.242 s; latest send 0.042 s after its planned time; 0 failed
read     2515 deliveries back, 2511 events once each, 837 trips; requested per zone {'48': 304, '161': 191, '230': 342}; truth {'48': 304, '161': 191, '230': 342}: same
sent     2515 deliveries to s3:recipe-simruns-lake-111122223333 in 109 calls, 3.182 s; latest send 3.154 s after its planned time; 0 failed
read     2515 deliveries back, 2511 events once each, 837 trips; requested per zone {'48': 304, '161': 191, '230': 342}; truth {'48': 304, '161': 191, '230': 342}: same
```

What changes between runs: the number of calls, the seconds and the send delays (they depend on how fast each call
returns). What holds in every correct run of SIM01: the deliveries read back equal the deliveries sent, `0 failed`,
the events counted once each and the trips equal the answer from `_truth/` (2,511 and 837 for SIM01 with seed 436),
and the line ends `same`. At a speed above 0, compare the latest send delay with R60's 1 s target and record it: a
slow network can exceed it, and that is a finding about the replay, not a failed pipeline. At speed 0 every send is
planned at 0, so "after its planned time" is how long the whole replay took. In the same run the check's day item read
`run_state` `finished` with `checked_count` 20, the metric took the point 1.0, and the verdict of a date without an
item published `2026-11-20: no check`. [`output/clean-up.txt`](output/clean-up.txt) shows the bare clean-up listing, the
clean-up with `--yes`, `lake.py down` and the proof: by schedule group, by tag and by name prefix, nothing left, and
each service asked by name answers "not found".

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| replay speed against data latency | a question with a method: SIM01 at speeds 1, 10 and 50, five replays each, data latency at p95 measured from `sent.jsonl` | `experiments/replay-speed.md`, `experiments/latency.py` | your read step writes `evidence/<run-id>/seen.jsonl` (the first time the table shows each event) |

## Cost and clean-up

Prices: USD, ca-central-1, free tier left out. S3, Lambda and DynamoDB rates are from the course price sheet (page
`436c-student/prices.md` on the course server); Kinesis and CloudWatch rates, which the sheet does not carry, are from
AWS's public price list files for ca-central-1 (`AmazonKinesis`, `AmazonCloudWatch`), the source the sheet names for
such rates. CAD at 1.45 per USD, the course's planning rate. 1 GB = 2^30 bytes (the course convention); no rate below is per GB. The units are from the run in
`output/main.txt` and `output/clean-up.txt` (measured):

| Part | Units in the captured run | USD |
|---|---|---|
| Kinesis, 1 provisioned shard | about 3 minutes; counted as 1 shard-hour × 0.0165 (*assumed*: billed by the started hour) | 0.0165 |
| Kinesis PUT payload units | 2,515 × 0.0154 per million | 0.00004 |
| S3 PUT and LIST | 136 objects (110 slot files, 26 probe files) and about 10 lists × 0.0055 per 1,000 | 0.0008 |
| S3 GET | 136 × 0.00044 per 1,000 | 0.00006 |
| Lambda, arm64, 256 MB | 3 calls of under 3 s (*assumed*) | 0.00003 |
| DynamoDB, SNS | 1 write, 1 read, 1 publish | under 0.00001 |
| CloudWatch custom metric | 1 metric, points in one hour: 0.30 a metric-month / 730 hours | 0.0004 |
| CloudWatch alarm | 1 alarm for about a minute, counted as an hour: 0.10 / 730 | 0.0001 |
| **Total** (*computed* from the measured units) | | **0.018 (0.026 CAD), 0.06% of the 30 USD lease** |

<!--src: measured: output/main.txt and output/clean-up.txt for the units; the prices are the course price sheet -->

30 days of daily checks (*computed*, from the same rates): 90 Lambda calls, 1,800 probe records, 30 run-log items
and 30 verdict reads, the custom metric (0.30 USD a metric-month) and the alarm (0.10 USD an alarm-month): about
0.41 USD (0.59 CAD), 1.4% of the 30 USD lease. A year: 4.87 USD (7.06 CAD), 16%; keep the check only while the
system is deployed. These figures leave out what `CHECK_QUERY` costs on your own system: an `athena:` count bills the
data it scans (at least 10 MB a query), and a `dynamodb:` scan bills a read unit per 4 KB it reads. Nor do they count
what the checked system bills on its own: a provisioned shard left up for a month is 720 × 0.0165 = 11.88 USD, about
40% of the lease. None of these estimates is a spending cap.

Remove everything:

From the terminal with your lease's credentials, in the recipe folder, with the same `PREFIX` the run used:

```bash
export PREFIX=recipe-yourname
uv run python clean_up.py                # lists what it would remove; ends "Nothing deleted. Run again with --yes"
uv run python clean_up.py --yes          # deletes it; every line reads "removed" or "absent"
uv run python clean_up.py --check        # by schedule group and by name prefix: "0 left" once lake.py down has run
```

Clean-up deletes, hourly parts first: a stream the ledger records `created`, the schedule group (its schedules go
with it), the alarm, the function, its log group, the topic and its subscriptions, the roles and the run log the
ledger records `created`, and this recipe's own files under `raw/trip-events/` (the slot files of its replays' run
ids and, when this ledger deployed the check, the probe's). Other recipes' files under that prefix, and a role,
table or bucket that was there before, are kept. Every step is attempted; one that is refused prints `FAILED` and
the exit code is 1. It is safe to run twice. The custom metric `LiveCheck/CheckPassed` cannot be deleted; clean-up
deletes the function that puts its points, and a metric that gets no new points is not billed (*assumed*).

The shared bucket and workgroups go last, and only once no recipe uses them: other recipes borrow them. When every
recipe in your project that uses them has run its own clean-up, run these from the same terminal:

```bash
uv run python scripts/lake.py down       # lists what it would delete; refuses while another recipe's files are left
uv run python scripts/lake.py down --yes # deletes the bucket and workgroups this ledger records created
```

If `lake.py down` refuses, it names the files that are left: run that recipe's clean-up, or keep the bucket and
record it in your resource inventory.

## Adapt it

- **Your events:** change the models in `src/rides/events.py` and the generator's `trip_events` together; the
  scenarios fail until both agree.
- **Your faults and data sets:** add a section to `sim/datasets.toml` (counts and delays), and a key in
  `sim/truth.py` for each table your scenarios name. A Then states the number before the run.
- **Your pipeline:** point `--to` and `--target` at your stream, queue, function URL or bucket; offline, give
  `--target your.module:handler`. Replace `rides/pipeline.py` and the whole-run steps with reads of your own tables.
- **Your check:** `CHECK_TARGET` and `CHECK_QUERY`; `CHECK_DELAY_MIN` is your data latency at p95 plus 15 minutes.
  For a weekly job, set `CHECK_CRON` weekly (`cron(0 6 ? * MON *)`) and keep a `CHECK_QUERY` that counts the probe's
  20 trips on the send date; a query that returns something else needs `CHECK_EXPECTED` set to the value it returns.
  The verdict skips the days with no send. Do not add `check.py alarm` with a weekly schedule: it fires when no check
  has passed for 26 hours, so it would fire every week.
- **CI:** copy `.github/workflows/offline.yml` to the root of your repository and set `working-directory`.
- **Shared helpers:** `events.py`, `replay.py`, `lake.py`, `tags.py` and `run_log.py` are copied unchanged into the
  recipes that use them (`tools/shared_helpers.toml`); change every copy or none.
