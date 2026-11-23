# Every completed trip from a queue, stored once, and a broken message kept

Use this if your system takes events from another service through an Amazon Simple Queue Service (SQS) queue and must
store each completed trip once, without a broken message holding up the rest. The DynamoDB sink stores one item per
`trip_id`; the S3 sink can store a repeated message twice, so whatever reads those files keeps one row per
`event_id` (the lake's merge does). The trips service sends each trip event as a
message to the queue `$PREFIX-trip-completed`; a Lambda function stores each completed trip in DynamoDB once (or, for
a team whose queue feeds its lake, writes every message as files in S3). A message that keeps failing moves to a
dead-letter queue, and `replay_dlq.py` sends it back after the fault is fixed.

Words used here (SQS, dead-letter queues, the visibility timeout and event source mappings were taught in L14):

- *Dead-letter queue*: a second queue that holds the messages that kept failing, so they stop being retried.
- *Visibility timeout*: how long a received message stays hidden from other receivers before it can be received
  again.
- *Event source mapping*: Lambda's poller that reads the queue and calls the function with batches.
- *p95*: the latency that 95% of the stored completions are at or under.
- *SIM01*: the synthetic rush-hour data set described under Data.

- *Batching window*: how long Lambda gathers messages before it calls the function with them
  (`MaximumBatchingWindowInSeconds`).
- *Partial batch response*: the function's answer that names only the failed messages of a batch
  (`batchItemFailures`).
- *Message move task*: an SQS task, started with `StartMessageMoveTask`, that moves the messages of a dead-letter
  queue back to their source queue at a set rate.

## What changes in main/

| Main scenario | This extension |
|---|---|
| the producer puts records on the stream `$PREFIX-updates` | the trips service (here `sim/replay.py --to sqs`) sends each delivery line to the queue `$PREFIX-trip-completed` with `SendMessageBatch`, up to 10 a call |
| one Lambda consumer on the stream, `main/consumer.py` | the function `$PREFIX-trip-completed-consumer` runs `queue_consumer.py`, on an SQS event source mapping |
| table `$PREFIX-updates`, keyed by `update_id` | table `$PREFIX-trips-completed`, keyed by `trip_id`; or, with `SINK=s3`, JSON Lines objects under `raw/trip-events/` of the lake bucket |
| failed batches go to `$PREFIX-consumer-failures` | a message received 5 times moves to `$PREFIX-trip-completed-dlq`; `replay_dlq.py` sends it back |

`main/infra.py` gives the queues, the role, the log group, the function and the mapping (`create_queue` now takes a
visibility timeout and tags); `clean_up.py` removes the new names. The course tags come from `scripts/tags.py`, and
every create is written to `state/ledger.jsonl`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Queue or stream (R90) | an SQS standard queue | a queue retries and parks each message on its own and bills per request only; a provisioned shard bills 0.0165 USD (0.024 CAD) an hour while it exists, 11.88 USD (17.23 CAD) for 30 days, and keeps order per key, which this job does not need. Keep the stream of `main/` when order per key or a second reader of the same records matters <!--src: ours: the course price sheet, ca-central-1, 2026-11-18; 720 hours --> | queue, topic and log compared: L14; [Kinesis pricing](https://aws.amazon.com/kinesis/data-streams/pricing/) |
| Failed messages (R92) | partial batch response (`ReportBatchItemFailures`) | without it every message of a failed batch becomes visible again, the ones already stored too | [Lambda, handling errors for an SQS event source](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html) |
| Repeats (R91) | the write is conditional on the `trip_id` not being stored yet | Lambda processes each message at least once; the item itself is the marker, so a repeat costs one write unit and does not change the item. An increment needs a marker and the `ADD` in one transaction: `concurrency-correctness/ext-idempotent-retry` | conditional writes: L12; REL04-BP04 |
| Dead letters (R93) | redrive after 5 receives; the dead-letter queue keeps a message 14 days | 5 is the least AWS recommends with Lambda, so a throttled call does not send a good message to the dead-letter queue. A message's expiry there counts from its first send, so the dead-letter queue keeps it longer than the source queue's 4 days | [Lambda, configuring a queue](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html); [dead-letter queue retention](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/setting-up-dead-letter-queue-retention.html) |
| Batching (R90, R95) | a batching window of 60 s (`BATCH_WINDOW_S`, 30 to 60 allowed), batches of up to 100, visibility timeout six times the function's 30 s timeout plus the window: 240 s (210 s at 30 s) | the window cuts the function's calls and its log lines, not the SQS requests. Measured with SIM01 at its own pace, the 30 s window and no window side by side (`../output/batching-window.txt`): with the window the function was called 331 times for 2,515 messages (7.6 a call, median 6) instead of 2,514 times, the messages were deleted in 428 calls instead of 2,515, and the log took in 140 KB instead of 1,058 KB. The SQS requests were 12,135 against 11,543, because Lambda's pollers received about one message per receive either way (2,520 receives returned messages in each run) and made more empty receives with the window; 384 an hour of them are the floor a switched-on mapping bills with no message at all (0.11 USD, 0.16 CAD, in 30 days). 30 days of such traffic cost 2.10 USD (3.05 CAD), 7.0% of the lease, instead of 2.37 USD (3.43 CAD), 7.9% (Cost). The window costs data latency: p95 34.1 s at 30 s instead of 0.07 s; R90 allows 90 s at 60 s. The lever for the SQS requests is the sender's batching: 10 messages a `SendMessageBatch` call would make 252 sends instead of 2,514, 1.76 USD instead of 2.10 in 30 days (Cost). The 60 s window is not measured: its calls, log and requests are not known yet. A window outside 30 to 60 stops `run.py` before anything is created; `run.py --no-window` exists for the comparison only <!--src: measured: ../output/batching-window.txt, one run per setting, 2026-11-23 (0 s and 30 s); unchecked: 60 s not run --> | with a window above 0 s, AWS recommends a visibility timeout of six times the function's timeout plus `MaximumBatchingWindowInSeconds`, so a batch can be processed and retried after a throttle ([Lambda, configuring a queue: Batch window](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html#events-sqs-eventsource)); Lambda long-polls a standard queue and, when traffic is low, can drop to 2 concurrent invokes to cut SQS calls, but not with `MaximumConcurrency` set ([SQS scaling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)); a batch over 10 needs a window of at least 1 s ([SQS mapping parameters](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-parameters.html)); Lambda calls the function when the window ends, the batch is full or it reaches 6 MB: [batching behavior](https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventsourcemapping.html) |
| How many copies run (R95) | at most 2 (`MaximumConcurrency` on the mapping) | an account's concurrent executions are limited, and a new account's or a lease's limit can be much lower than the default 1,000 (check yours in Service Quotas, `Concurrent executions`); a burst must not take them all ([Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)). Lambda does not scale its polling down to 2 when the queue is idle with this setting, so idle polling bills more (Cost) | [Lambda SQS scaling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html) |
| Replay (R94) | a message move task; receive, send, then delete where it is refused | one call moves every message back at a set rate (up to 500 a second, one task per queue at a time). `replay_dlq.py --probe` tells you whether your sign-in may start a move task; where it is refused, the copy path does the replay; it deletes from the dead-letter queue only after the send, so a failed send leaves the message where it was | [StartMessageMoveTask](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_StartMessageMoveTask.html) |
| What the queue carries (R97) | every kind; the DynamoDB sink stores completions and counts the rest | an event filter on `event_type` would save the calls of the other messages, but Lambda removes from the queue every message that does not match a filter, a broken body included, which must reach the dead-letter queue | [event filtering](https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventfiltering.html) |
| The S3 sink (R96) | one new object per batch gathered for up to 60 s, written with `If-None-Match: *`, never overwritten | without a window the lake would get about one object per message, 60,360 a day at this rate; with 60 s, one object per batch, about one a minute if one poller gathers the minute's messages (not measured: concurrent pollers and full batches write more objects, so count the objects of a run before projecting a month). An object written over after the lake's merge took it would make the merge's list of merged keys wrong; a repeat is removed by the merge, which keeps one row per `event_id` <!--src: assumed: one batch a minute, more pollers split it --> | [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) |
| Checking a message (R92) | the consumer checks each body with the standard library against the fields of `src/rides/events.py`; a test checks that the check and the Pydantic models accept and refuse the same lines | the function is zipped as it is and runs with what the Lambda Python runtime has (boto3 and the standard library); Pydantic would need a package built for arm64 | one definition of the events (simulated-runs); [Lambda Python runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html) |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/queue_ingest.feature`; examples tagged `@live` run only in AWS (`LIVE=1`).

- **R90** (complex) Where `ext-queue-ingest` is used, when a message with a completed trip arrives in the queue
  `$PREFIX-trip-completed`, the recipe shall store the trip in the table `$PREFIX-trips-completed` with its fare, its
  `delivery_id` and the time it was stored, within 90 s of its send at p95 at the default 60 s window (60 s at a
  30 s window). <!--src: ours: the window plus 30 s for the poll and the call -->
- **R91** (complex) Where `ext-queue-ingest` is used, if a message carries a `trip_id` whose completion is already
  stored, then the recipe shall leave the stored item unchanged and count the message as a repeat.
- **R92** (complex) Where `ext-queue-ingest` is used, if a message of a batch cannot be stored, then the recipe shall
  report only that message to Lambda as failed and store the other messages of the batch.
- **R93** (complex) Where `ext-queue-ingest` is used, if a message has been received 5 times without being stored,
  then the recipe shall have SQS move it to the dead-letter queue `$PREFIX-trip-completed-dlq` (redrive policy
  `maxReceiveCount` 5) and keep it there 14 days from its first send.
  <!--src: assumed: the SQS docs do not say when the move happens; moto moves it at the next receive -->
- **R94** (complex) Where `ext-queue-ingest` is used, when `replay_dlq.py` runs, the recipe shall move every message
  of `$PREFIX-trip-completed-dlq` back to `$PREFIX-trip-completed` at most 10 a second, by a message move task or by
  receiving, sending and then deleting each message if the task is refused, and print how many it moved.
- **R95** (optional) Where `ext-queue-ingest` is used, the recipe shall run at most 2 copies of the consumer and
  gather batches of up to 100 messages for `BATCH_WINDOW_S` seconds (60 unless set; `run.py` stops on a value
  outside 30 to 60), with a visibility timeout of six times the function's 30 s timeout plus the window.
- **R96** (optional) Where `ext-queue-ingest` is used with `SINK=s3`, the recipe shall gather batches of up to 100
  messages for 60 s, write every message of a batch unchanged as one new JSON Lines object under
  `raw/trip-events/dt=<arrival day>/` with `If-None-Match: *`, and report the whole batch as failed if the write fails.
- **R97** (complex) Where `ext-queue-ingest` is used with the DynamoDB sink, if a message is a request or an accept,
  then the recipe shall store nothing for it and count it as other.

A team keeps 2 to 5 of these per job story; the numbers are this recipe's own.

## Data

The consumer's message body is one delivery line: the event's fields plus `delivery_id` and `received_time`
(`src/rides/events.py`). The kind stored here, `trip_completed`: `event_type`, `event_id`, `trip_id`, `driver_id`,
`dropoff_zone_id`, `trip_miles`, `trip_time_s`, `fare`, `driver_pay`, `event_time`. The item:
`trip_id`, `fare`, `event_id`, `delivery_id`, `stored_time`.

Simulated data sets, written by `sim/stand_in.py` in the simulator's layout
(`sim/data/<id>/trip-events/dt=<day>/hour=<hour>/deliveries.jsonl`, `_truth/events.jsonl`, `_truth/faults.jsonl`;
`sim/truth.py` computes the answer from `_truth/`). **Their trips are synthetic**: the course simulator makes the same data
sets from DS01 (the TLC High Volume For-Hire Vehicle trips, Manhattan, March 2024); `sim/stand_in.py` makes ones with
the same counts and faults from a seeded random source, and a data set from the simulator replaces one without a
change to the code.

| Id | Name | What it is |
|---|---|---|
| SIM01 | `rush-hour` | 837 trips requested 09:00 to 10:00 UTC, 304, 191 and 342 in zones 48, 161 and 230; 2,511 events sent as 2,515 deliveries (4 requests twice, 2 requests late by 6 to 10 minutes): 837 completions stored, 0 repeats, 1,678 other |
| SIM02 | `refused-write` | SIM01, with the fault the scenario puts in: the table refuses the write of trip `t-0412` until the scenario clears it |
| SIM03 | `completions-twice` | SIM01 with 4 more completions twice: 2,519 deliveries; 837 stored, 4 repeats, 1,678 other |

## Run it

From your laptop, in `streaming-ingestion`, offline (moto stands in for AWS; `drain()` in `run.py` stands in for
Lambda's poller):

```bash
uv run python ext-queue-ingest/run.py --target local
uv run behave features/queue_ingest.feature      # no failed scenarios; the @live ones are skipped without LIVE=1
```

From your laptop, in `streaming-ingestion`, with your lease's credentials: run `aws sts get-caller-identity` first
and check that the account is your team's. The run sends at the hour's own pace; the last completions come about 55
minutes after the hour's last request, so it takes about 2 hours. Run it from a laptop terminal, not CloudShell,
which ends a session after 20 to 30 minutes without keyboard input
([CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html)). Not measured: the 60 s
window and `SINK=s3`.

Counting the SQS requests is optional. With it: run `request_count.py up`, wait ten minutes (CloudTrail starts
logging a few minutes late), start the run, and run `count` fifteen minutes after the run ends. If `up` is refused,
or you skip it, leave out the two `request_count.py` lines and `--requests requests.json`: `latency.py` then gives
the SQS requests as a lower bound.

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-queue-ingest/request_count.py up                       # optional; then wait ten minutes
uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep      # prints the run id
read -r -p 'Run ID: ' RUN_ID                                             # paste the run id it printed
uv run python ext-queue-ingest/request_count.py count --json requests.json   # optional; fifteen minutes after the run
uv run python ext-queue-ingest/latency.py --run-id "$RUN_ID" --requests requests.json   # p95, batches, cost
uv run python ext-queue-ingest/replay_dlq.py --probe                     # is StartMessageMoveTask allowed?
uv run python ext-queue-ingest/run.py stop                               # the mapping off: no idle polls
BATCH_WINDOW_S=30 uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep   # the 30 s window
LIVE=1 LIVE_RUN_ID="$RUN_ID" uv run behave --tags=live features/queue_ingest.feature
```

Then clean up (Cost and clean-up).

`bash ext-queue-ingest/live_run.sh` runs the measured comparison: the trail, the rush hour at the 30 s window
(`$PREFIX`, `BATCH_WINDOW_S=30`) and, at the same time, without a window (`$PREFIX-w0`, `run.py --no-window`: window 0 and batches of up
to 10, because Lambda refuses a batch size over 10 without a window of at least 1 s), `latency.py` for each, the
table `../output/batching-window.txt`, the probe and the clean-up (the bare run's list, then `--yes`). It writes
to `.local/` unless `OUT` names another file. `--no-window` is for that
comparison only; `BATCH_WINDOW_S` still refuses anything outside 30 to 60.

The trail (`request_count.py`) is there because SQS bills every request while its CloudWatch metrics count messages
and empty receives only, so the receives that return messages are not in CloudWatch. It logs only the queues named
`$PREFIX…`; CloudTrail begins logging a few minutes after `up`, so start it ten minutes before the run, and
`latency.py` refuses counts whose `SendMessageBatch` calls differ from the sender's own count.

With `SINK=s3`, run `uv run python scripts/lake.py up` first: the objects go to the shared lake bucket
`$PREFIX-lake-<account>` under `raw/trip-events/`. `run.py` records in the ledger each object that holds this run's
messages; `clean_up.py` leaves them, and `scripts/lake.py down --yes` deletes those objects and no others.

What a run in AWS should show: every completion stored once, 0 messages in the dead-letter queue, and a p95
under 90 s at the default 60 s window (under 60 s at `BATCH_WINDOW_S=30`). Not measured: the 60 s window, with its
visibility timeout of 240 s.

### AWS permissions

| Part | Actions your sign-in needs |
|---|---|
| `run.py --target aws` | `sqs:CreateQueue`, `GetQueueUrl`, `GetQueueAttributes`, `TagQueue`, `SendMessageBatch`; `dynamodb:CreateTable`, `DescribeTable`, `Scan`; `iam:CreateRole`, `PutRolePolicy`, `GetRole`, `PassRole`; `logs:CreateLogGroup`, `PutRetentionPolicy`; `lambda:CreateFunction`, `GetFunction`, `CreateEventSourceMapping`, `ListEventSourceMappings`, `UpdateEventSourceMapping`; `sts:GetCallerIdentity`; with `SINK=s3`, `s3:CreateBucket` and the bucket calls of `scripts/lake.py up` |
| `latency.py` | `dynamodb:Scan`; `cloudwatch:GetMetricStatistics`; `logs:FilterLogEvents` on the function's log group; `sqs:GetQueueUrl`, `GetQueueAttributes` on the dead-letter queue; `lambda:ListEventSourceMappings` |
| `request_count.py` (optional) | `s3:CreateBucket`, `PutBucketTagging`, `PutBucketPolicy`, `ListBucket`, `GetObject`, `DeleteObject`, `DeleteBucket` on `$PREFIX-sqs-requests-<account>`; `cloudtrail:CreateTrail`, `AddTags`, `PutEventSelectors`, `StartLogging`, `GetTrail`, `DeleteTrail`. A lease may refuse CloudTrail: then `latency.py` prints the requests as a lower bound |
| `replay_dlq.py` | on the dead-letter queue `sqs:StartMessageMoveTask`, `ListMessageMoveTasks`, `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`; on the source queue `sqs:SendMessage` ([redrive permissions](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)); the copy path needs all but the two move-task actions |

The function's role `$PREFIX-queue-consumer-role` may receive, delete and read the attributes of its queue, put
items into its table (or put objects under `raw/trip-events/` of the lake bucket) and write its own log group.

If a create is refused (`AccessDeniedException` names the action), `run.py` stops: run the bare `clean_up.py` to
list what it made, delete it with `--yes`, and ask for the action. `replay_dlq.py --probe` says whether a message
move task is allowed; where it is refused, `replay_dlq.py` uses its copy path (receive, send, then delete), which
needs only the queue actions above.

## What you should see

Offline, `run.py --target local` (from `../output/ext-queue-ingest-local.txt`; the batch count changes with the
stand-in's poller, the other numbers do not):

```text
sent 2515 deliveries to recipe-trip-completed in 252 calls
consumer: 836 stored, 0 repeats, 1678 other, 5 failed receives, 26 batches; table 836 of 837 completions; dead-letter queue 1 message
fault cleared; replaying the dead-letter queue
moved 1 messages by copy from recipe-trip-completed-dlq to recipe-trip-completed
table 837 of 837 completions; dead-letter queue 0 messages
```

- One refused trip does not hold up the others: 836 of 837 are stored, and the refused one waits in the dead-letter
  queue after its 5 receives.
- After the replay every completion is stored once; the 1,678 requests and accepts store nothing.

In AWS, one run's output, for example (`../output/ext-queue-ingest.txt`, 30 s window; timings, batch sizes and
request counts vary from run to run):

```text
table recipe-queue-trips-completed: 837 of 837 completions stored
837 latencies; p95 34.1 s (nearest rank 796 of 837); target under 60 s at a 30 s window: met
batches: 2515 messages received in 331 calls, mean batch 7.60, median 6; calls 94 ms mean, 31.7 s billed; 6542 empty receives; 2514 send calls; 12135 SQS requests (counted (CloudTrail data events))
dead-letter queue: 0 messages
30 days at this rate: 2.10 USD (3.05 CAD), 7.0% of the 30 USD lease; a year 25.56 USD (37.06 CAD), 85% (the run's measured units x 720 / 1.93 h)
```

The same hour without a window stored the same 837 completions in 2,514 calls (`../output/batching-window.txt`).

## Cost and clean-up

Prices: USD, ca-central-1, from the course price sheet (`436c-student/prices.md` on the course server,
2026-11-18); 1 GB = 2^30 bytes. CAD beside at 1.45, the course's planning rate. Free allowances are left out: the
free tier is shared by the course organisation. The totals leave out the optional request counting: its CloudTrail
data events and their S3 objects bill while the trail exists ([CloudTrail pricing](https://aws.amazon.com/cloudtrail/pricing/));
`clean_up.py --yes` removes the trail and its bucket.

**Measured, DynamoDB sink** (`../output/batching-window.txt`, `latency.py`): every unit is from the run of SIM01 at
its own pace (2,515 messages in 1.93 h): the SQS requests on the queue counted by CloudTrail (sends, receives with
and without messages, deletes, the poller's attribute reads), the calls and billed milliseconds from the function's
log, the write attempts, the bytes the log group took in. 30 days and a year are that run repeated back to back
(720 / 1.93 = 374 runs, about 940,000 messages in 30 days). <!--src: measured: ../output/batching-window.txt -->

| Part, 30 s window | One run: SIM01, 2,515 messages, 1.93 h | 30 days of such traffic | A year |
|---|---|---|---|
| SQS: 12,135 requests (2,514 sends, 9,062 receives of which 6,542 empty, 428 deletes, 131 others) × 0.40 / 10^6 | ≈ 0.0049 (0.0070 CAD) | ≈ 1.81 (2.63 CAD) | ≈ 22.1 (32.0 CAD) |
| Lambda: 331 calls, 31.7 s billed at 128 MB, arm64 | ≈ 0.00012 (0.00017 CAD) | ≈ 0.044 (0.064 CAD) | ≈ 0.54 (0.78 CAD) |
| DynamoDB: 837 conditional writes × 0.6875 / 10^6 | ≈ 0.00058 (0.00083 CAD) | ≈ 0.22 (0.31 CAD) | ≈ 2.62 (3.79 CAD) |
| CloudWatch Logs: 140 KB ingested × 0.55 per GB | ≈ 0.00007 (0.00011 CAD) | ≈ 0.028 (0.040 CAD) | ≈ 0.33 (0.49 CAD) |
| **Total, 30 s window** | **≈ 0.0056 (0.0082 CAD)** | **≈ 2.10 (3.05 CAD)** | **≈ 25.6 (37.1 CAD)** |
| The same without a window (`--no-window`, measured): 11,543 SQS requests, 2,514 calls, 1,058 KB of log | ≈ 0.0063 (0.0091 CAD) | ≈ 2.37 (3.43 CAD) | ≈ 28.8 (41.7 CAD) |
| Computed from the 30 s run: a sender that sends 10 messages a call (252 sends instead of 2,514) | ≈ 0.0047 (0.0068 CAD) | ≈ 1.76 (2.56 CAD) | ≈ 21.4 (31.1 CAD) |
| Computed: DynamoDB storage, 0.073 GB a month of items (250 B each with overhead) at 0.275 per GB-month, growing | ≈ 0 | ≈ 0.010 (0.015 CAD) | ≈ 1.45 (2.10 CAD) |

Against the 30 USD lease, 30 days of such traffic are 7.0% with the window and 7.9% without; a year is 85% and 96%.
Most of the bill is SQS requests, and most of those are the pollers' receives: about 4.8 requests per message sent.
The window cuts the function's calls and log lines, not the receives. The sender's batching (10 a call) saves more
than the window does at this traffic. The rate is one rush hour every 1.93 hours, all day: an upper bound for a
service that is busy only at its peaks.

Idle, measured: the same two mappings switched on for 30 minutes with no message sent (`run.py deploy`, then
`latency.py --idle`) made 384 SQS requests an hour each (360 empty receives, 24 attribute reads), with either
window: 0.11 USD (0.16 CAD) for 30 days, 0.4% of the lease, and 1.35 USD (1.95 CAD) a year. That floor is 6% of the
requests under load; the traffic adds about 5,600 (no window) to 5,900 (30 s) requests an hour, 1.62 and 1.70 USD
in 30 days. Switch the mapping off between runs with `run.py stop`. <!--src: measured: ../output/batching-window.txt -->

**Computed, not measured: `SINK=s3`** (60 s window), at the rush hour's rate every hour (2,515 messages an hour,
1,810,800 in 30 days), with the SQS requests at the measured 4.8 a message. An estimate, not a cap: concurrent
pollers write more objects than one a minute, so count a run's objects and requests before you project a month: <!--src: unchecked: SINK=s3 not run; one object a minute assumed -->

| Part | One hour | 30 days | A year |
|---|---|---|---|
| SQS: about 12,100 requests × 0.40 / 10^6 | ≈ 0.0048 (0.0070 CAD) | ≈ 3.48 (5.05 CAD) | ≈ 42.4 (61.5 CAD) |
| S3 PUT, about one object a minute: 60 × 0.0055 / 1,000 | ≈ 0.00033 (0.00048 CAD) | ≈ 0.24 (0.35 CAD) | ≈ 2.89 (4.19 CAD) |
| Lambda, about 60 calls of 0.5 s, and logs | ≈ 0.00008 (0.00012 CAD) | ≈ 0.058 (0.084 CAD) | ≈ 0.70 (1.02 CAD) |
| S3 storage, 0.39 GB a month of delivery lines (230 B each), growing | — | ≈ 0.005 (0.007 CAD) | ≈ 0.71 (1.03 CAD) |
| **Total, S3 sink** | **≈ 0.0052 (0.0076 CAD)** | **≈ 3.79 (5.49 CAD)** | **≈ 46.7 (67.7 CAD)** |

Remove everything: the mapping first, then the queues, the table, the function, its role and log group, and the
request-counting trail and its bucket `$PREFIX-sqs-requests-<account>` if `request_count.py up` made them. A bare run
lists what it would delete and deletes nothing; read the list (account, region, names), then run it with `--yes`:

```bash
PREFIX=recipe-yourname uv run python clean_up.py
PREFIX=recipe-yourname uv run python clean_up.py --yes
```

The lake bucket is shared. `scripts/lake.py down` lists what it would delete: under `raw/trip-events/` only the
objects the ledger records as this recipe's (the `SINK=s3` run's), and the bucket itself only when the ledger
records it created and no other recipe's files are left. Run it with `--yes` only after every recipe that uses the
bucket has cleaned up; objects you keep bill for their storage:

```bash
PREFIX=recipe-yourname uv run python scripts/lake.py down
```

A second run with other settings (`BATCH_WINDOW_S=30`, `SINK=s3`) gives the existing queue, function and mapping
the new settings, and switches the mapping on again after `run.py stop`.

## Adapt it

- Your own messages: change `KINDS` in `queue_consumer.py` with `src/rides/events.py`, and keep the test that
  checks they agree (`tests/test_queue_ingest.py`).
- Order per key or a second reader: keep the stream of `main/` instead (Decisions, queue or stream).
- An increment instead of an item per trip: the marker and the `ADD` in one transaction,
  `concurrency-correctness/ext-idempotent-retry`.
- More traffic: measure the batch size first (`latency.py`), then the window; raise `maxReceiveCount` if the
  consumer's `Throttles` metric is above 0.
