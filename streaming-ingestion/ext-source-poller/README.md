# Every slot of a partner's feed stored once, and the missing ones shown

Use this if your system reads a source that publishes a file per period (a 15-minute feed, a monthly release) on a
site outside your account, so nothing sends you an event. In the rides case a fleet partner publishes the completed
trips of every 15-minute slot as one file and lists it in a manifest. A scheduled function loads each slot onto
the main scenario's stream, loads a late file when it appears, and the page shows the state of every slot of a day,
so a gap in the counts reads as a missing file and not as a quiet hour. A slot can be sent to the stream more than
once (a run that stopped or failed is taken over); the consumer's conditional write on `update_id` keeps one stored
update per trip.

Words used here:

- *Slot*: a 15-minute period, named by its start in UTC (`2026-11-22T08:00Z`); it ends 15 minutes later.
- *Manifest*: the partner's list of published files (`manifest.json`).
- *Claim*: a conditional write that marks a slot as being loaded by one run (conditional writes: L12).

## What changes in main/

| Main scenario | This extension |
|---|---|
| the producer sends location updates from a laptop | the function `$PREFIX-feed-poller` sends the trips of each slot file, called every 15 minutes by EventBridge Scheduler (schedule `$PREFIX-feed-schedule` in the group `$PREFIX-source-poller`) |
| `PutRecords` keyed by `driver_id` | `PutRecords` of at most 500 keyed by `trip_id`; the refused records of all calls are gathered and sent again, again in calls of at most 500 |
| the stream `$PREFIX-updates`, the consumer and its table | the same, borrowed: run `main/run.py --target aws --keep` first. Each `trip_completed` becomes an update the consumer stores unchanged: `update_id` = `event_id`, `trip_id`, `driver_id`, `zone_id` = `dropoff_zone_id`, `event_time`, `sent_time` |
| — | the table `$PREFIX-feed-slots` (key `slot_day`, sort key `slot_start`) holds each slot's state; `slots.py` gives the page its slot list |

The feed's zone is the dropoff zone; a page that counts pickups joins on `trip_id` with `trip_requested`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| How the poller is started (R100) | a schedule every 15 minutes calling a function | the partner's site is outside our account, so it sends no event (an S3 event needs our bucket); a process that runs all the time on EC2 or Fargate bills every hour it exists, a function only the milliseconds it runs | EventBridge Scheduler: used by students (A3, optional task B); [Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html) |
| One load per slot (R101) | claim each slot with a conditional put before sending | Scheduler delivers at least once, and a slow run can overlap the next; the condition lets one writer win. The run log's `lock` item (`$PREFIX-runs`, job `source-poller`, as the lake's scheduled merge takes it) stops a second run before it reads anything; the claim holds if two runs pass it | [Scheduler, at-least-once delivery](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html); [condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html); L12 |
| A claim that is left (R105, R106) | a `loading` claim older than 15 min, or a `failed` slot, is claimed again | a run that stops after its claim, or fails, would otherwise leave the slot unloaded for good; a record sent twice is a repeat for the consumer's conditional write on `update_id` | REL04-BP04; L14 |
| Sending (R102) | `PutRecords` of at most 500, keyed by `trip_id`; only the refused records sent again, gathered across the slot's calls, after a random wait under a doubling limit, 5 attempts | one call carries up to 500 records and reports which it refused; a slot is one unit, so its refused records are gathered across its calls and sent again in calls of at most 500. A slot of about 200 trips of a few hundred bytes each is far under the call's 5 MB limit; a feed with larger records must check that limit too | [PutRecords](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html); REL05-BP03 |
| The slot table's key (R104) | key `slot_day`, sort key `slot_start`; attributes `slot_state`, `record_total`, `claimed_at`, `first_seen_time`, `published_time`, `error_text` | the page reads a day with one Query, without a Scan; `DAY`, `STATUS` and `COUNT` are DynamoDB reserved words | [reserved words](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ReservedWords.html) |
| How far back a run looks (R100) | slots that ended in the last 25 hours; items read with one Query per UTC date from 25 h 15 min before the run to its own date | a file published just before the 24-hour limit must still be in the window at the next poll and at a takeover 15 min later; reading the dates of the whole window (three dates in the first 1 h 15 min after midnight) means a slot keyed two dates back is not taken for one without an item <!--src: ours: the partner publishes within 24 hours (assumed), plus an hour --> | the partner's lateness: assumed (Data) |
| When a slot is missing (R104) | `missing` only more than 30 min after a slot ends; `not due` before | the partner publishes about 2 min after a slot ends, so a slot inside its 30 min is not yet a gap; calling it missing would show a false gap <!--src: assumed: the simulated partner's 2 minutes --> | — |
| Lateness (R103) | from `published_time`, or from `first_seen_time` (the first run that listed the file) when the feed gives none | not every feed gives a publication time; the first listing is at most one poll (15 min) after the publication | — |
| The function's time (R100) | timeout 300 s, no new slot when less than 45 s are left, asynchronous retries 0, the run exits if the previous run holds the lock | a first run catching up 96 slots, about 19,000 records, on one shard can pass 60 s when throttled; a run cut off by its timeout leaves claims that wait 15 min; the next run is 15 min away, so Lambda does not retry | scheduled Lambda jobs (course convention); [Lambda async retries](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html) |
| Where the records go (R102) | the main scenario's stream | the stream feeds the consumer and the page within seconds; a lakehouse team without a stream writes each slot's file to S3 instead and avoids the shard's hourly charge (Cost) | — |
| A feed without a manifest | `FILE_PATTERN` (a `strftime` pattern of the slot start): the poller builds each due slot's file name and asks for it with an HTTP `HEAD` | some public feeds name their files by period and list none (GDELT's 15-minute files are named this way). Use `FILE_PATTERN` only after checking that your source answers `HEAD` for a published file and tells an absent file apart (404); otherwise use a manifest | — |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/source_poller.feature`; examples tagged `@live` run only in AWS (`LIVE=1`).

- **R100** (complex) Where `ext-source-poller` is used, when the 15-minute schedule fires, the recipe shall read the
  feed's manifest and load every listed slot that ended in the last 25 hours, is not loaded yet and was published
  within 24 hours of its end, with a data latency under 20 min at p95 (at least 95% of the published files have
  their last record on the stream within 20 min of publication). <!--src: ours: one poll period plus the time to load a slot -->
- **R101** (complex) Where `ext-source-poller` is used, when a run is to load a slot, the recipe shall claim it with a
  conditional put on its item in `$PREFIX-feed-slots` and send the slot's records only if that put succeeds.
- **R102** (complex) Where `ext-source-poller` is used, when a slot is loaded, the recipe shall send its records to
  the stream `$PREFIX-updates` in `PutRecords` calls of at most 500 keyed by `trip_id`, send again only the records a
  call refused, and then mark the slot `loaded` with its record count.
- **R103** (complex) Where `ext-source-poller` is used, if a slot's file is published or first listed more than 30 min
  after the slot ended, then the recipe shall load it at the next run and mark the slot `late`.
- **R104** (complex) Where `ext-source-poller` is used, when the page lists a day, the recipe shall show each of its
  96 slots as `loaded`, `late`, `loading`, `failed`, `missing` (no file more than 30 min after the slot ended) or
  `not due`.
- **R105** (complex) Where `ext-source-poller` is used, if a slot has been `loading` for more than 15 min, then the
  recipe shall claim it again and send its records again.
- **R106** (complex) Where `ext-source-poller` is used, if loading a claimed slot fails, then the recipe shall mark
  the slot `failed` with its error and claim it again at the next run.

A team keeps 2 to 5 of these per job story; the numbers are this recipe's own.

## Data

**Manifest**: `{"slots": [{"slot": "2026-11-22T08:00Z", "file": "trips-2026-11-22T0800Z.jsonl",
"published_time": "2026-11-22T08:17:00Z"}]}` (`published_time` optional). **A slot file**: one `trip_completed` a
line (`event_type`, `event_id`, `trip_id`, `driver_id`, `dropoff_zone_id`, `trip_miles`, `trip_time_s`, `fare`,
`driver_pay`, `event_time`). **The slot list** (`GET /api/slots?day=2026-11-22`): `{"day": "2026-11-22",
"read_time": "2026-11-23T00:46:00Z", "slots": [{"slot_start": "2026-11-22T00:00Z", "slot_state": "loaded",
"record_total": 201}, …]}`, 96 entries in slot order, `record_total` only for `loaded` and `late`; a day that is not
a date gets 400. dashboard-page serves it (either variant, with `SLOTS_TABLE` set) from `slot_states` and
`get_slots` in `slots.py`: in that recipe, set `SLOTS_TABLE` to `$PREFIX-feed-slots` and give its function's role
`dynamodb:Query` on that table; `GET /api/slots?day=` and the feed's date then answers 200 with 96 entries.

What the partner does is assumed: a file comes within 24 hours of its slot's end or not at all; a published file
does not change; `published_time` is not always given (then lateness is judged by the first listing).

Simulated data sets. **Their trips are synthetic**: the course simulator makes this day from DS01 (the TLC High
Volume For-Hire Vehicle trips, Manhattan, March 2024); `sim/stand_in.py` makes one with the same counts and faults
from a seeded random source, and `feed.py` writes it as a feed.

| Id | Name | What it is |
|---|---|---|
| SIM01 | `feed-day` | 19,074 completed trips in zones 48, 161 and 230 on 2026-11-22 as 96 slot files, each published 2 min after its slot ends; slot 09:15 is never published (345 trips), slot 10:30 is published 2 hours after it ends (at 12:45): 95 files and 18,729 trips reach the stream |
| SIM02 | `overlapping-runs` | slots 08:00 and 08:15 of SIM01; two poller runs start at the same moment |
| SIM03 | `stopped-run` | slot 08:00 of SIM01; the first run stops after its claim, before it sends |
| SIM04 | `unreadable-file` | slot 08:00 of SIM01; its file is cut off in the middle of a line on the first read, and whole on the next |

## Run it

On your laptop, offline (moto stands in for AWS; a whole simulated day of runs, every 15 minutes):

```bash
uv run python ext-source-poller/run.py --target local
uv run behave features/source_poller.feature
```

In AWS: on your laptop, in `streaming-ingestion/`, with your lease's AWS credentials in `ca-central-1`. Run
`aws sts get-caller-identity` first and check that it names your lease's account; stop if a permission below is
refused (AWS permissions). `feed.py --live` publishes in real time: a whole day is 26 hours, `--slots 8` about
2 hours 15 minutes. Keep it on the laptop, not in CloudShell, which ends a session after 20 to 30 minutes without
keyboard or pointer input, running processes or not
([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)).

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1             # a PREFIX of your own: every name starts with it
uv run python main/run.py --target aws --keep                   # the stream, the consumer and its table
uv run python ext-source-poller/run.py --target aws            # the slot table, the poller, the schedule
uv run python ext-source-poller/feed.py --to aws --live --slots 8   # prints the evidence/feed-…/published.jsonl it writes
```

Wait 20 minutes after `feed.py` ends: the next two polls load the last files. Then check that every published slot is
`loaded` or `late` (`GET /api/slots` of dashboard-page, or the items of `$PREFIX-feed-slots`); a `loading` slot
needs another poll, and a `failed` one has its error in `error_text`. Then:

```bash
PUBLISHED=$(ls -t evidence/feed-*/published.jsonl | head -1)   # this run's file
uv run python ext-source-poller/latency.py --published "$PUBLISHED"   # one latency a slot, the p95, met or not
uv run python ext-source-poller/run.py stop                    # delete the schedule group: no more runs
LIVE=1 LIVE_PUBLISHED="$PUBLISHED" uv run behave --tags=live features/source_poller.feature   # the live scenarios pass
```

Clean-up follows in Cost and clean-up. `bash ext-source-poller/live_run.sh` runs this sequence with `--slots 8`,
waits the 20 minutes, cleans up, and writes the masked output to `output/ext-source-poller.txt`.

### AWS permissions

| Part | Actions your sign-in needs |
|---|---|
| `run.py --target aws` | `kinesis:DescribeStreamSummary`; `dynamodb:CreateTable`, `DescribeTable`, `TagResource`; `iam:CreateRole`, `TagRole`, `PutRolePolicy`, `GetRole`, `PassRole`; `logs:CreateLogGroup`, `TagResource`, `PutRetentionPolicy`; `lambda:CreateFunction`, `TagResource`, `GetFunction`, `PutFunctionEventInvokeConfig`; `scheduler:CreateScheduleGroup`, `TagResource`, `CreateSchedule`, `UpdateSchedule`; `sts:GetCallerIdentity`; the bucket calls of `scripts/lake.py up` |
| `feed.py --to aws` | `s3:PutObject` on `feed/` of the lake bucket |
| `latency.py` | `logs:FilterLogEvents` on the poller's log group |
| `run.py stop` | `scheduler:GetScheduleGroup`, `DeleteScheduleGroup` |
| `clean_up.py` (this folder's, for the main scenario and all four extensions) | `scheduler:GetScheduleGroup`, `DeleteScheduleGroup`; `lambda:GetFunction`, `DeleteFunction`, `ListEventSourceMappings`, `GetEventSourceMapping`, `DeleteEventSourceMapping`; `logs:DescribeLogGroups`, `DeleteLogGroup`; `iam:GetRole`, `GetRolePolicy`, `DeleteRolePolicy`, `DeleteRole`; `dynamodb:DescribeTable`, `DeleteTable`; `kinesis:DescribeStreamSummary`, `DeleteStream`; `sns:GetTopicAttributes`, `ListSubscriptionsByTopic`, `Unsubscribe`, `DeleteTopic`; `sqs:GetQueueUrl`, `DeleteQueue`; `s3:ListBucket`, `DeleteObject` on `feed/` |

The function's role `$PREFIX-feed-poller-role` may read `feed/` of the lake bucket, query, put and update items of
the slot table, put and delete items of the run log, put records on the stream and write its own log group. The
schedule's role `$PREFIX-feed-scheduler-role` is trusted by EventBridge Scheduler from your account only and may call
the one function.

EventBridge Scheduler schedules and a schedule group were made by students in A3 (optional task B,
`create-schedule-group`). If `CreateScheduleGroup` is refused in your lease, stop and run the clean-up (bare, then
`--yes`) for what `run.py` made so far. Do not make the schedule in the `default` group instead: `run.py stop` and
`clean_up.py` delete the group `$PREFIX-source-poller`, so a schedule elsewhere would keep calling the function.

## What you should see

Offline, `run.py --target local` (from `output/ext-source-poller-local.txt`; the simulated day is fixed, so these
counts repeat):

```text
104 runs from 2026-11-22 00:15 to 02:00 the next day: 95 slots loaded, 1 of them late; 18729 updates stored of the 18729 trips in the 95 published files
GET /api/slots?day=2026-11-22 at 00:46 the next day: 200, 96 slots: 94 loaded, 1 late, 1 missing
missing: 2026-11-22T09:15Z
```

- Every published file is stored once: the table holds each of its trips once, however many runs listed it.
- The 10:30 slot, published 2 hours late, is loaded by the next run and shown `late`; the 09:15 slot, never
  published, is shown `missing`, not as a quiet quarter hour.

In AWS, one run's output, for example (measured, `output/ext-source-poller.txt`: 3 slots published live at 06:02,
06:17 and 06:32 UTC; your times, counts and table name differ, and with `--slots 8` you get 8 latencies). The checks:
`latency.py` reports a p95 under 20 min, the slot list answers 200 with 96 slots, and the live scenarios pass:

```text
2026-11-23T05:45Z 10.6 min
2026-11-23T06:00Z 10.6 min
2026-11-23T06:15Z 0.3 min
3 latencies; p95 10.6 min (nearest rank 3 of 3); target under 20 min: met
GET /api/slots?day=2026-11-23 at 06:46: 200, 96 slots: 3 loaded, 23 missing, 70 not due
main/run.py's updates (before 06:05): 300; the poller's (after): 366 = 123 + 122 + 121 trips
table recipe-poller-updates: 666 items = 300 + 366
```

- Each scheduled run listed every slot of the manifest that ended in the last 25 hours; the runs at 06:27 and
  06:42 listed the slots loaded before them and sent nothing for them: the stream took in 366 records for 366 trips.
- Two runs started at the same moment with slot 06:15 due: one took the run log's lock and loaded the slot, the
  other printed `"run_state": "skipped", "why": "the previous run holds the lock"`.
- A latency is the wait for the next run (up to 15 min) plus about 3 s; 06:15 was loaded by the run started by hand.
- The 23 slots of the day before the feed started are `missing`, as a partner's gap would be.

Two runs can start in the same second, so a run's `run_id` in the run log is its start time and its Lambda request
id (`2026-11-23T06:32:12Z-4cce…`). After `run.py stop` the schedule group stays `DELETING` for a while; `clean_up.py`
reports it as already being deleted.

## Cost and clean-up

Prices: the course price sheet, page `436c-student/prices.md` on the course server ("price sheet, ca-central-1,
2026-11-18"), its sections Lambda, Storage and queries, DynamoDB, Streams, Monitoring and APIs and messaging. 1 GB = 2^30
bytes. These lines are
the poller's and the two main-scenario lines below; storage in S3 and DynamoDB and the main scenario's other parts
are left out (the recipe README has them). USD with CAD beside at 1.45, the
course's planning rate. Free allowances are left out: the free tier is shared by the course organisation. The
Lambda line is from the live run: 8 runs billed 21.4 s in all, 2.7 s a run (3.4 s for a run that loads a slot, 0.5
to 0.6 s for one that finds nothing new). The other lines are computed from the prices and the counts below.
<!--src: measured: ../output/ext-source-poller.txt, REPORT lines; 2 log lines of 300 B a run assumed -->

| Part | One run: one new slot of about 199 trips | 30 days of runs every 15 minutes (2,880 runs, 572,220 trips) | A year (35,040 runs) |
|---|---|---|---|
| Lambda, 2.7 s at 128 MB a run (measured), and 1 request | ≈ 0.0000047 (0.0000068 CAD) | ≈ 0.0134 (0.019 CAD) | ≈ 0.163 (0.24 CAD) |
| S3 GET of the simulated feed, 2 a run (a real partner's site is not billed by AWS) | ≈ 0.0000009 (0.0000013 CAD) | ≈ 0.0025 (0.0036 CAD) | ≈ 0.031 (0.045 CAD) |
| DynamoDB writes: 2 per slot (claim, `loaded`) and 2 run-log items a run: 4 × 0.6875 / 10^6 | ≈ 0.0000028 (0.0000040 CAD) | ≈ 0.0079 (0.011 CAD) | ≈ 0.096 (0.14 CAD) |
| DynamoDB reads: two Queries of a day's slot items (three after midnight), 8 units strongly consistent, and 1 for the run log: 9 × 0.1375 / 10^6 | ≈ 0.0000012 (0.0000018 CAD) | ≈ 0.0036 (0.0052 CAD) | ≈ 0.043 (0.062 CAD) |
| Kinesis PUT payload units, one per trip: 199 × 0.0154 / 10^6 | ≈ 0.0000031 (0.0000045 CAD) | ≈ 0.0088 (0.013 CAD) | ≈ 0.107 (0.16 CAD) |
| CloudWatch Logs, 2 lines of 300 B a run, 0.55 per GB | ≈ 0.0000003 (0.0000004 CAD) | ≈ 0.0009 (0.0013 CAD) | ≈ 0.011 (0.016 CAD) |
| Scheduler, 1.10 per million invocations | ≈ 0.0000011 (0.0000016 CAD) | ≈ 0.0032 (0.0046 CAD) | ≈ 0.039 (0.057 CAD) |
| **Total of the poller** | **≈ 0.000014 (0.000020 CAD)** | **≈ 0.040 (0.058 CAD)** | **≈ 0.49 (0.71 CAD)** |
| The main scenario's Kinesis shard, if it is kept | — | 720 × 0.0165 = 11.88 (17.23 CAD) | 144.54 (209.58 CAD) |
| The main consumer's DynamoDB writes, one unit per trip, and its Lambda calls | — | about 0.39 (0.57 CAD), 572,220 × 0.6875 / 10^6, plus its calls | about 4.8 (7.0 CAD) plus its calls |

The poller itself is about 0.1% of the 30 USD lease for 30 days and 1.6% for a year. The stream it feeds is not: a
shard kept up all month is 40% of the lease, and a year is almost five times it. A team that polls a 15-minute feed
and does not need seconds-fresh data writes each slot to S3 and drops the stream. The page's reads are counted in
dashboard-page. Deleting the schedule group (`run.py stop`) stops new poller runs, not every charge: the stream
kept with `--keep` bills its shard every hour it exists, and the tables and the bucket bill what they store.

`clean_up.py` of this folder removes the main scenario and all four extensions of `streaming-ingestion` together,
whatever of them you ran, by names built from `PREFIX`; for this extension and the main scenario that is the schedule
group first, then the functions (the poller and the consumer) with their mappings, roles and log
groups, the slot table, the consumer's table, the stream `$PREFIX-updates`, `feed/` of the lake bucket, and the run
log if this folder's ledger created it. It keeps the lake bucket and its other objects. On your laptop in
`streaming-ingestion/`, with the same `PREFIX`:

```bash
uv run python clean_up.py          # lists what it would delete (or `absent`); deletes nothing
uv run python clean_up.py --yes    # only after reading the list: every name starts with your PREFIX
uv run python scripts/lake.py down     # the lake bucket: lists what it would delete; deletes nothing
```

Run `uv run python scripts/lake.py down --yes` only when that list shows nothing another recipe or your project still
needs: it deletes the bucket only when its ledger records that it created it, and refuses while the bucket holds
objects outside `raw/trip-events/` and `athena-results/`.

## Adapt it

- A monthly release instead of 15-minute slots: the same claim and state rules hold with a monthly schedule; the
  25-hour window becomes the release's own lateness plus a day (`SLOT`, `LOOK_BACK`, `LATEST` in `poller.py`, and
  `slots.py` for the page).
- Your partner's site: set `FEED_URL` to its `https://` address; for a feed without a manifest set `FILE_PATTERN`.
- No stream: write each slot's file under `raw/` of the lake instead of `send()` (Cost, the shard's hourly charge).
