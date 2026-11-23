# Live updates into DynamoDB through a stream, with their lag measured

## What you build

Drivers' phones send location updates to a Kinesis data stream, and a Lambda function stores each update in a DynamoDB
table with the time it happened and the time it was stored. At the end of a run you see how many updates arrived and
the median and 95th percentile (p95) of their lag.

**Words used here.**

- **Event time**: when the phone took the reading, by the phone's clock.
- **Arrival time**: when the consumer stored the update, by the consumer's clock.
- **Lag**: arrival time minus event time.
- **Entered time**: when the record entered the stream (Kinesis calls it `approximateArrivalTimestamp`). It splits
  the lag into the part before the stream and the part in the stream and the consumer.
- **p95**: the lag that 95 of every 100 updates stay within. The median says what is typical, the p95 says how bad the
  slow tail is.

## Use it when

- Your project takes the **Streaming ingestion** extension, or any shape where live updates must reach an operational
  store and you need to say how fresh the data is (lakehouse first, operational first).
- Add `ext-windowed-aggregates/` for the **Windowed aggregates over the stream** extension. Use `ext-fan-out/` for
  **Fan-out to several consumers**: it replaces the stream with an Amazon Simple Notification Service (SNS) topic and one Amazon Simple Queue Service (SQS) queue per consumer.
- Use `ext-source-poller/` when your source publishes a file per period on a site outside your account (nothing
  sends you an event): a scheduled function loads each file onto the stream once and the page shows missing ones.
  Use `ext-queue-ingest/` when another service sends you its events through an SQS queue: each completed trip is
  stored once, and a broken message waits in a dead-letter queue until it is replayed.
- Use another design when updates arrive once a day or less (a batch load into the lake fits better), or when you need
  windows that overlap or join two streams (a stream processor such as Spark Structured Streaming or Flink;
  DDIA 2e ch. 12).

## Main scenario

1. The producer (`main/producer.py`) stamps each update with its event time and sends it to the stream
   `$PREFIX-updates` with `PutRecords`, keyed by `driver_id`.
2. Kinesis Data Streams keeps the records in one shard. Each driver's records land in that shard in the order they
   were written. `PutRecords` and its retries can write them out of the order sent, so the event time decides which
   update is newer.
3. Lambda's event source mapping reads the shard and calls the function `$PREFIX-consumer` (`main/consumer.py`) with
   batches of up to 100 records.
4. The function writes each update to the DynamoDB table `$PREFIX-updates`, with its event, entered and arrival times,
   unless an update with the same `update_id` is already there.
5. The run reads the table and prints the median and p95 of the lag and of its two parts (`main/lag.py`).
6. The run removes everything it created (`clean_up.py`), unless you pass `--keep`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Which clock the lag starts from (R20, R31) | the event time the phone writes into the update | a phone that was offline sends readings minutes old. Counted from the time the record entered the stream, that lag would not show | event time against processing time: L14; [DDIA 2e ch. 12](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch12.html), "Event time versus processing time" |
| Which summary of the lag (R30, R32) | median and p95 of all updates in the run, each part of the lag separately | an average hides the slow tail, and the parts show whether the delay comes from the phones or from the pipeline | response time as a distribution, read by percentiles: [DDIA 2e ch. 2](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch02.html), "Average, Median, and Percentiles" |
| Broker (R40) | Kinesis Data Streams, provisioned, 1 shard | a log keeps records after they are read, so a second consumer or a consumer that has been repaired can read them again (replay). Order is kept per partition key | log-based message broker: L14; DDIA 2e ch. 12, "Log-Based Message Brokers" |
| Partition key (R10) | `driver_id` | one driver's updates stay in one shard, in the order written (`PutRecords` does not guarantee the order sent; [PutRecords API](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html)) | ordering per key within a shard: L14; [Kinesis key concepts](https://docs.aws.amazon.com/streams/latest/dev/key-concepts.html) |
| Shard count (R40) | 1 | the run sends 10 updates a second; one shard takes 1,000 records or 1 MB a second of writes | [PutRecords API](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html); select resource type, size and number based on data: [COST06-BP02](https://docs.aws.amazon.com/wellarchitected/latest/cost-optimization-pillar/cost_type_size_number_resources_data.html) |
| Writes from the producer (R11, R12, R13) | `PutRecords` in batches of up to 500, retry only the failed records, at most 5 attempts, each after a random wait under a limit that doubles (0.1 s, 0.2 s ... up to 2 s) | fewer calls than one per record; a busy shard does not get a storm of retries, and producers that were refused together do not come back at the same moment | control and limit retry calls: [REL05-BP03](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html) |
| Repeated records (R21) | the consumer writes with the condition `attribute_not_exists(update_id)` | Lambda reads a stream at least once and the producer may resend, so the same update can arrive twice. The first arrival time stays | at-least-once delivery, idempotence: L14; DDIA 2e ch. 12, "Idempotence"; [REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html); [Lambda Kinesis docs](https://docs.aws.amazon.com/lambda/latest/dg/with-kinesis.html) |
| Record that fails (R22, R41, R45) | report its sequence number (`ReportBatchItemFailures`) and stop the batch there; split a failing batch (`BisectBatchOnFunctionError`); at most 3 retries; then send the batch's details (shard and sequence numbers, not the records) to the queue `$PREFIX-consumer-failures`, which keeps them 14 days | the default retries a bad record until it expires (24 hours in this stream) and holds up every record behind it in the shard (a poison message). 14 days is the longest SQS keeps a message: time to look before the evidence is gone | poison message and dead-letter queue: L14; REL05-BP03; [Kinesis on-failure destination](https://docs.aws.amazon.com/lambda/latest/dg/kinesis-on-failure-destination.html) |
| Batching window (R41) | at most 1 s | a fuller batch means fewer calls; the wait adds up to 1 s to the lag | [Lambda Kinesis docs](https://docs.aws.amazon.com/lambda/latest/dg/with-kinesis.html), "Polling and batching streams" |
| Table (R20) | key `update_id`, on-demand capacity | the run writes each update once by id; on-demand does not need capacity planning for a short run | key from the access pattern: L12; [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/) |
| Function role (R42, R43) | its own role, allowed to read this stream, write this table, send to its failure queue and write its own logs | a mistake in the function cannot touch other tables or streams | [SEC03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html) grant least privilege access |
| Log lines (R46) | each function's log group is made before the function, with a retention of 7 days | Lambda makes the group itself on the first call. That group keeps every line until somebody deletes it. After `--keep`, the lines would be stored and billed for as long as the group exists | Serverless Applications Lens, [logging ingestion and storage](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/logging-ingestion-and-storage.html): set log retention periods |
| Watching a real pipeline | Lambda's `IteratorAge` metric (age of the records the consumer reads) | a rising age says the consumer falls behind, before users notice stale data | consumer too slow: L14; [REL06-BP01](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_monitor_aws_resources_monitor_resources.html) |
| Offline tests (R37) | moto, a library that stands in for AWS inside the process, for Kinesis and DynamoDB; `main/local_poller.py` calls the handler with events structured like those Lambda sends | `uv run pytest -q` and `uv run behave` pass without an AWS account | course constraint: offline tests |

The phone delays in the simulation (50 to 400 ms for most updates, 30 to 120 s for 1 update in 10) are **assumed**,
not measured. Change them in `main/updates.py`.

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets
after a requirement's number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an extension is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. The requirements of an extension are in its own README, and their numbers go on from here:
[windowed aggregates](ext-windowed-aggregates/README.md#requirements) from 70,
[fan-out](ext-fan-out/README.md#requirements) from 80. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Producer** (`main/producer.py`, in your terminal)

- **R10** (event) When an update is sent, the recipe shall put it on the stream `$PREFIX-updates` with its
  `driver_id` as partition key and with its `update_id`, its `event_time` and its `sent_time`.
- **R11** (ubiquitous) The recipe shall send at most 500 records in one `PutRecords` call.
- **R12** (unwanted) If the stream refuses some records of a call, then the recipe shall send only those records
  again, after a random wait under a limit of 0.1 seconds that doubles with every further call.
- **R13** (unwanted) If records are still refused after the 5th call, then the recipe shall stop the run, without a
  further wait, with an error that says how many, and remove what the run created.

**Consumer** (`main/consumer.py`, in Lambda)

- **R20** (event) When the function receives a batch of stream records, the recipe shall store each update in the
  table `$PREFIX-updates` with its event time, its entered time, its arrival time and its lag, in milliseconds.
- **R21** (unwanted) If an update with the same `update_id` is already stored, then the recipe shall leave the
  stored item as it is and count the record as a repeat.
- **R22** (unwanted) If a record cannot be stored, then the recipe shall report its sequence number to Lambda as
  the first failed one and store none of the records after it in that batch.

**Run** (`main/run.py`, in your terminal)

- **R30** (event) When a run ends, the recipe shall print how many updates were sent, how many the producer sent
  again, how many were stored, and the median and the p95 of the lag and of its two parts.
- **R31** (ubiquitous) The recipe shall count the lag of an update from its event time to its arrival time.
- **R32** (ubiquitous) The recipe shall report as p95 the smallest stored lag that at least 95 of every 100 stored
  updates stay within, and as median the same for 50 of every 100.
- **R33** (event) When a run with `--target aws` ends, the recipe shall remove every resource the run created.
- **R34** (optional) Where `--keep` is passed, the recipe shall leave the stream, the table, the function, its
  failures queue and its role in place.
- **R35** (unwanted) If, after `--wait` seconds (180 unless given), fewer updates are stored than distinct updates
  were sent, then the recipe shall print `only <stored> of <expected> stored`, report the lag of the stored ones and exit
  with code 1.
- **R36** (state) While the stream of an earlier run is still being deleted, the recipe shall wait until it is gone
  before it creates the stream again.
- **R37** (ubiquitous) The recipe shall run `main/run.py --target local` without an AWS account, credentials or a
  Region setting.

**Pipeline in the sandbox** (`main/run.py --target aws`, `main/infra.py`)

- **R40** (event) When a run with `--target aws` starts, the recipe shall create the stream `$PREFIX-updates` with
  1 shard in provisioned mode.
- **R41** (ubiquitous) The recipe shall have Lambda read the stream from its oldest record, in batches of at most
  100 records or 1 second, split a failed batch, try it again at most 3 times, give up records older than 3,600
  seconds, and send what it gives up on to the queue `$PREFIX-consumer-failures`.
- **R42** (ubiquitous) The recipe shall let the function read its stream, put items into its table, send to its
  failures queue and write its own log group, and nothing else.
- **R43** (optional) Where `LAMBDA_ROLE_ARN` is set, the recipe shall run the function with that role and create no
  role.
- **R44** (event) When a run with `--target aws` starts, the recipe shall create the function from
  `main/consumer.py` alone, with Python 3.12 on arm64, 128 MB of memory and a timeout of 30 seconds.
- **R45** (ubiquitous) The recipe shall keep what Lambda gave up on in a failures queue for 14 days.
- **R46** (ubiquitous) The recipe shall create each function's log group before the function and keep its lines
  for 7 days.

**Constraints**

- **R50** (ubiquitous) The recipe shall start every resource name with `$PREFIX`, and with `recipe` if `PREFIX` is
  not set.
- **R51** (ubiquitous) The recipe shall send every call to the Region in `AWS_REGION`, and to `ca-central-1` if it
  is not set.

**Clean-up** (`clean_up.py`)

- **R60** (event) When `clean_up.py --yes` runs, the recipe shall remove every mapping, function, log group, role, topic,
  queue, table and stream that the main scenario and both extensions create for `$PREFIX`.
- **R61** (complex) While nothing of the recipe is left, when `clean_up.py --yes` runs, the recipe shall report every
  resource as absent and exit with code 0.
- **R62** (unwanted) If AWS refuses a step of the clean-up, then the recipe shall print that step as `FAILED`,
  attempt every other step, and exit with code 1.
- **R63** (ubiquitous) The recipe shall remove only resources with the exact names it builds from `$PREFIX`.
- **R64** (complex) While the stream, a table, a mapping or the schedule group is still being deleted, when
  `clean_up.py --yes` runs, the recipe shall report it as already being deleted and not ask for its deletion again.
- **R65** (event) When `clean_up.py` runs without `--yes` (or with `--dry-run`), the recipe shall look up each
  resource it would remove, print `would remove` or `absent` for it, and delete nothing.

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal with credentials for your team sandbox: AWS
CloudShell, or your laptop after its sign-in. Three files of this recipe run inside AWS, in Lambda:
`main/consumer.py`, and in the extensions `windows.py` and `fanout_consumer.py`. The other moving parts are
services AWS runs (Kinesis Data Streams, DynamoDB, SNS, SQS); the scripts in your terminal create them, send the
updates and read the tables.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS |
|---|---|---|---|---|
| Producer | `main/producer.py`, `main/updates.py` | `--target local`: sends to moto's stream | `--target aws`: sends to your stream from the terminal | no |
| Consumer | `main/consumer.py` | `main/local_poller.py` reads moto's stream and calls it | `main/infra.py` zips it and creates the function | yes: Lambda runs it on every batch of stream records |
| Creating the pipeline | `main/infra.py` | no | yes: stream, table, failure queue, role, function, event source mapping | no |
| Run and lag report | `main/run.py`, `main/lag.py` | `--target local` | `--target aws`: reads the table from the terminal | no |
| Windowed aggregates | `ext-windowed-aggregates/run.py`, `timeline.py`, `windows.py` | `--target local`: the poller calls `windows.py` | `--target aws` creates the function and its table | `windows.py`: Lambda runs it as a second consumer of the stream |
| Fan-out | `ext-fan-out/run.py`, `fanout_consumer.py` | `--target local`: `run.py` reads moto's queues and calls it | `--target aws` creates the topic, the queues and two functions | `fanout_consumer.py`: Lambda runs it once per queue |
| Clean-up | `clean_up.py` | no: it always calls AWS | yes, with the `PREFIX` of the run | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

This recipe does not have a container or an instance.




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole` or `dynamodb:TagResource`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:Scan`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:PassRole`, `iam:PutRolePolicy`, `kinesis:CreateStream`, `kinesis:DeleteStream`, `kinesis:DescribeStream`, `kinesis:DescribeStreamSummary`, `kinesis:GetRecords`, `kinesis:GetShardIterator`, `kinesis:ListShards`, `kinesis:PutRecords`, `lambda:CreateEventSourceMapping`, `lambda:CreateFunction`, `lambda:DeleteEventSourceMapping`, `lambda:DeleteFunction`, `lambda:GetEventSourceMapping`, `lambda:GetFunction`, `lambda:ListEventSourceMappings`, `lambda:UpdateEventSourceMapping`, `lambda:UpdateFunctionCode`, `logs:CreateLogGroup`, `logs:DeleteLogGroup`, `logs:PutRetentionPolicy`, `sns:DeleteTopic`, `sns:GetTopicAttributes`, `sns:ListSubscriptionsByTopic`, `sns:Unsubscribe`, `sqs:CreateQueue`, `sqs:DeleteQueue`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl` |
| `ext-fan-out/` adds | `sns:CreateTopic`, `sns:Publish`, `sns:Subscribe`, `sqs:SetQueueAttributes` |
| `ext-windowed-aggregates/` adds | `dynamodb:DescribeTimeToLive`, `dynamodb:UpdateTimeToLive` |

### 1. Laptop, offline

Run these from the root of your clone. They do not need an AWS account. They need [uv](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
cd streaming-ingestion
uv sync
uv run pytest -q
uv run behave
uv run python main/run.py --target local
```

What you should see, line by line:

1. `uv sync` installs boto3, pytest, behave and moto (a library that stands in for AWS inside the process).
2. `pytest` ends with a line like `52 passed`.
3. `behave` ends with `0 failed`. It takes about five minutes.
4. The run prints a report of the shape in [What you should see](#what-you-should-see), with `target: local`.

Clean-up and cost: the offline run does not create anything and is not billed.

### 2. Sandbox terminal: the pipeline

Run by us on 2026-11-12 (`output/main.txt`). Sign in to your team sandbox first, and check that the account is the
one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

In `streaming-ingestion`, with a short name of your own in place of `yourname` (in CloudShell, install uv first:
`pip install --user uv`):

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python main/run.py --target aws
```

1. The run creates one IAM role per function (`$PREFIX-consumer-role` and, in the extensions, one for each of
   their functions), which a sandbox user may do. To use a role you already have, also export `LAMBDA_ROLE_ARN`:
   every function then runs with it, no role is created, and the role must allow what each function's statements
   in `main/infra.py` and the extensions' `run.py` allow.
2. The run sends 300 updates at 10 a second. It waits until the consumer has stored them. It prints the report.
   It removes what it created. Sending takes about 30 seconds;
   the deploy before it and the wait and the clean-up after it take a few minutes more, which we did not time.
3. Add options to the run command to change it: `--updates 600 --rate 20` sets the number and rate of updates, `--from-file my-updates.jsonl` reads updates from a file, `--wait 300` sets the wait, and `--keep` leaves the resources in place.

Clean-up and cost: the run removes what it created unless you pass `--keep`. After `--keep`, or after a run that
was interrupted, clean up the same day in the same terminal with the same `PREFIX`:
`uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes`. One run is under 0.01 USD ([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: the extensions

Each extension's README has its own part, with the sign-in, the commands, what you should see and the clean-up:
[windowed aggregates](ext-windowed-aggregates/README.md#run-it), [fan-out](ext-fan-out/README.md#run-it). Both
were run by us on 2026-11-12; [source poller](ext-source-poller/README.md#run-it) and
[queue ingest](ext-queue-ingest/README.md#run-it) on 2026-11-23, in the course's account (not a lease).

## What you should see

From `output/main.txt` (a run on AWS, 2026-11-12):

```text
target: aws, prefix: recipe, sent: 300, retried by the producer: 0
updates stored: 300

part of the lag                             median s     p95 s
lag: event to stored                          63.864   134.632
event to entered (before the broker)           0.248    75.820
entered to stored (broker and consumer)       62.211    72.490
```

Check the output:

- `updates stored` equals `sent`. A smaller number means some records failed or were not stored within `--wait`
  seconds (default 180): look in the function's log group and in the queue `$PREFIX-consumer-failures`.
- In the example above, the median is far below the p95. In the simulation, 1 update in 10 comes from a phone that was offline.
- In the example above, the part before the broker has a p95 of 75.8 s, compared with 134.6 s for the whole lag. Percentiles of the parts do not add up.

What changes between runs: every value in seconds. On AWS, expect "entered to stored" to include the poll interval
and the 1 s batching window. In the run above it was about a minute (median 62 s), far more than both: the producer
starts sending right after the event source mapping is created, so the time the new mapping takes to start polling
is in the number (our reading, not measured apart). Offline (moto) the same part was 0.4 s.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Windowed aggregates over the stream | counts per zone in 5-minute tumbling windows by event time, a close delay, and a stated rule for late records (drop and count, or correct) | [`ext-windowed-aggregates/`](ext-windowed-aggregates/) | a second consumer (`windows.py`) reads the same stream into the table `$PREFIX-windows`; the producer sends a simulated half hour with fixed event times |
| Fan-out to several consumers | each update goes to an SNS topic; the dashboard and history consumers each read their own SQS queue, and a stopped consumer does not hold up the other | [`ext-fan-out/`](ext-fan-out/) | the producer publishes to a topic instead of the stream; each consumer is a Lambda function on its own queue, writing its own table |
| Source poller | a schedule every 15 minutes calls a function that loads each slot file of a partner's feed once (a conditional claim per slot), loads late files, and gives the page every slot's state (`loaded`, `late`, `loading`, `failed`, `missing`, `not due`) | [`ext-source-poller/`](ext-source-poller/) | the poller sends to the main stream in place of the producer; the main consumer and table are borrowed; a slot table `$PREFIX-feed-slots` |
| Queue ingest | trip events through an SQS queue into DynamoDB (or S3) once each, a 30 to 60 s batching window, a dead-letter queue after 5 receives and `replay_dlq.py` to send messages back | [`ext-queue-ingest/`](ext-queue-ingest/) | a queue in place of the stream, a Lambda consumer on it (`queue_consumer.py`) writing `$PREFIX-trips-completed` |

## Cost and clean-up

One `aws` run of the main scenario costs well under 0.01 USD (from the price list, not measured):

- a shard for about 10 minutes at 0.0165 USD a shard-hour (AWS bulk price list, ca-central-1);
- 300 DynamoDB writes at 0.6875 USD a million;
- a few hundred Lambda calls of 128 MB (assumed: inside the free tier on most accounts).

The stream is priced per hour for as long as it exists, whether or not anything is sent, so do not leave it after `--keep`.
Remove everything the recipe and its extensions create:

```bash
PREFIX=recipe-yourname uv run python clean_up.py               # read the list first: would remove, absent; deletes nothing
PREFIX=recipe-yourname uv run python clean_up.py --yes
```

It removes the schedule group, mappings, functions, log groups, roles, topic, queues, tables, stream and the
poller's `feed/` objects with your prefix (the lake bucket and its `raw/trip-events/` stay: the lake owns them). It does not touch
resources with another prefix. It is safe to run twice. Every step is attempted: one that is refused is printed as
`FAILED`, the others still run, and the exit code is then 1 (R60 to R64).

## Adapt it

- **Your own updates**: write them as JSON lines with `update_id`, a partition key and `event_time`, and send them with
  `--from-file`. `main/updates.py` `read_jsonl` already reads stream-sim stream files (`event_id`, `partition_key`).
- **Your own fields**: `store` in `main/consumer.py` builds the item. Add your fields there, and keep `update_id` and
  the three times.
- **Your own key**: if the dashboard reads the latest update per driver, key a second table by `driver_id` and write
  with the condition that the stored `event_ms` is older, so a late update does not replace a newer one.
- **More load**: one shard takes 1,000 records a second. Add shards, or set the stream to on-demand, before you send
  more; the partition key must spread the load over the shards.
- **Names and region**: Set `PREFIX` and `AWS_REGION`. Change how names are built in `main/infra.py` `names`, which builds all names.
