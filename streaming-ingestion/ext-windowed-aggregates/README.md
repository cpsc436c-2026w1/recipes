# Counts per zone in 5-minute windows by event time

A second consumer of the updates stream counts updates per zone in 5-minute **tumbling windows** (fixed length, side
by side, each update in exactly one window) by **event time**, when the phone took the reading by its clock. A **close delay** says how long the consumer waits after a
window's end before the window is final, as measured by progress in event time. A stated rule says what happens to a **late record**, one that arrives
for a window that is already final.

## What changes in main/

| Main scenario | This extension |
|---|---|
| producer stamps each update's event time when it sends | `timeline.py` sets the event times of a simulated half hour (600 updates); 1 in 15 arrives 30 s to 10 min late |
| one consumer, `main/consumer.py` | adds `windows.py` as the function `$PREFIX-windows` on the same stream (a second consumer of the shard) |
| table `$PREFIX-updates`, one item per update | table `$PREFIX-windows`: one item per zone and window, one watermark item per shard recording the newest event time seen minus the close delay, and one marker per update to prevent counting it again |

## Rule for late records

1. The **watermark** is the newest event time seen on the shard, minus the close delay (default 120 s,
   `--close-delay`). It moves after each batch, never back.
2. A window is final once the watermark has passed its end.
3. A record for a final window is late. With `--late-rule drop` (default) the count stays and the window's
   `late_dropped` goes up by 1. With `--late-rule correct` the count goes up and so does `corrections`, so a reader
   that showed the first value knows it changed.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Which clock determines the window (R70) | event time, from the update | an update from a backlog after a pause or from a phone that was offline would otherwise be assigned to the wrong window | event time, windows: L14; [DDIA 2e ch. 12](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch12.html), "Types of windows" |
| Lambda's own tumbling windows (`TumblingWindowInSeconds`) | not used | Lambda cuts them by the time the record was inserted into the stream, not by event time | [Lambda tumbling windows](https://docs.aws.amazon.com/lambda/latest/dg/services-kinesis-windows.html); L14 |
| When a window closes (R71) | watermark = newest event time minus a close delay of 120 s (assumed: long enough for most phones in the simulation) | the wait is measured on event time, so a consumer that pauses and catches up does not drop anything. A longer close delay drops fewer records, and every final value comes later | close delay and watermark: L14; DDIA 2e ch. 12, "Handling straggler events" |
| Late records (R72, R73) | drop and count, or correct and count the corrections; chosen per run | dropped records are visible in `late_dropped`; a rising count says the close delay is too short | DDIA 2e ch. 12, "Handling straggler events" |
| Counting each update once (R74) | the count and a marker `seen#<update_id>` are written in one transaction. The marker must not exist yet | a stream delivers at least once, and `ADD 1` applied twice counts twice | request id with a conditional write: L14; DDIA 2e ch. 12, "Idempotence"; [REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html) |
| Keeping markers (R75) | time to live of 2 days on `expires_at` | markers are needed only while a record can still be redelivered. The stream keeps records 24 h by default | [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html); [Kinesis retention](https://docs.aws.amazon.com/streams/latest/dev/kinesis-extended-retention.html) |
| Several shards | one watermark per shard; a window is final when the smallest of them has passed the window's end | a slow or quiet shard must not let windows close early | follows from the watermark rule above (one clock per input) |

A transaction writes two items, and DynamoDB charges two write units per item in a transaction, so each update takes
4 write units ([DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)).

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R70** (optional) Where `ext-windowed-aggregates` is used, the recipe shall count the updates of each zone in
  5-minute tumbling windows by event time.
- **R71** (optional) Where `ext-windowed-aggregates` is used, the recipe shall report a window as final once the
  newest event time seen on the shard, minus the close delay (120 seconds unless `--close-delay` names another
  number), has passed the window's end.
- **R72** (complex) Where `ext-windowed-aggregates` is used, if a record arrives for a final window and the late
  rule is `drop`, then the recipe shall leave the window's count as it is and add 1 to its `late_dropped`.
- **R73** (complex) Where `ext-windowed-aggregates` is used, if a record arrives for a final window and the late
  rule is `correct`, then the recipe shall add 1 to the window's count and 1 to its `corrections`.
- **R74** (complex) Where `ext-windowed-aggregates` is used, if a batch is delivered a second time, then the recipe
  shall count each of its updates once.
- **R75** (optional) Where `ext-windowed-aggregates` is used, the recipe shall give each marker an `expires_at` 2
  days after it is written and turn on time to live for `expires_at` in the table `$PREFIX-windows`.
- **R76** (complex) Where `ext-windowed-aggregates` is used, when `run.py --target aws` runs, the recipe shall
  create the function `$PREFIX-windows` from `windows.py` alone as a second consumer of the stream
  `$PREFIX-updates`, with batches of at most 25 records and the late rule and close delay of the run.

## Run it

On your laptop, run the extension offline from `streaming-ingestion`:

```bash
uv run python ext-windowed-aggregates/run.py --target local
uv run python ext-windowed-aggregates/run.py --target local --late-rule correct --close-delay 300
```

In a sandbox terminal (the recipe README says what that is). Run by us on 2026-11-12
(`../output/ext-windowed-aggregates.txt`). In a sandbox terminal, sign in and check the account:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-windowed-aggregates/run.py --target aws
```

`windows.py` runs in Lambda, as the function `$PREFIX-windows`. `run.py` and `timeline.py` run in your terminal.
Clean-up: the run removes what it created unless you pass `--keep`; `uv run python clean_up.py` in
`streaming-ingestion` removes what is left. Cost: a shard-hour and about 2,400 write units, under 0.01 USD (from
the price list, not measured).

## What you should see

From `output/ext-windowed-aggregates.txt` (a run on AWS, 2026-11-12, rule `drop`), first and last lines:

```text
zone  window start (UTC)   count late dropped corrections truth  state
 161  2026-11-13T03:55:00Z    27            1           0    28  final
...
total: counted 588, late and dropped 12, late and corrected 0, truth 600 (rule: drop)
every window: count + late dropped = truth: yes
```

- With `drop`, count + late dropped = truth in every window; with `correct`, count = truth and `corrections` > 0.
- The last windows stay `open`: no later update moves the watermark past their end.
- A longer close delay gives fewer late records.
