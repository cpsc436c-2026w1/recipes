# Each update to two consumers through a topic

Each update goes to an Amazon Simple Notification Service (SNS) topic. Two Amazon Simple Queue Service (SQS) queues subscribe to it, and each queue has its own consumer: the dashboard
consumer, which feeds the dashboard, and the history consumer. In **fan-out**, every subscriber gets a copy of every
message. The run stops the history consumer while the updates are sent and shows that the dashboard still received
every update.

## What changes in main/

| Main scenario | This extension |
|---|---|
| producer puts records on the stream `$PREFIX-updates` | producer publishes to the topic `$PREFIX-updates-topic` with `PublishBatch` (`run.py` `publish`) |
| one Lambda consumer on the stream | two Lambda functions, `$PREFIX-dashboard` and `$PREFIX-history`, each on its own queue, running `fanout_consumer.py` |
| failed batches go to `$PREFIX-consumer-failures` | each queue has a dead-letter queue for messages that could not be processed: `$PREFIX-dashboard-dlq` for the dashboard queue and `$PREFIX-history-dlq` for the history queue |
| table `$PREFIX-updates` | tables `$PREFIX-dashboard` and `$PREFIX-history`, same item shape |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Topic and queues (R80, R81, R82) | SNS topic, one SQS queue per consumer | the topic does not keep messages after delivery. Each queue keeps its consumer's copy until that consumer deletes it, so a stopped consumer finds its messages waiting | fan-out, consumer groups: L14; [DDIA 2e ch. 12](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch12.html), "Multiple consumers"; [SNS to SQS](https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html) |
| Coupling | the producer knows only the topic, not the consumers | adding a consumer is a new subscription. A slow or stopped consumer does not slow the producer or the other consumer | [REL04-BP02](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_loosely_coupled_system.html) implement loosely coupled dependencies |
| Raw message delivery (R83) | on | the body the consumer receives is the update itself, not the SNS envelope around it | [SNS raw message delivery](https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html) |
| Queue access (R83) | a queue policy that lets only this topic send | another topic or account cannot write to the queue | [SEC03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html) |
| Failed messages (R84, R86, R88) | `ReportBatchItemFailures`; after 5 receives (the least AWS recommends with Lambda) the message moves to the dead-letter queue, which keeps it 14 days | one bad message does not come back forever, and the rest of the batch is not received again. A message's expiry in a dead-letter queue is calculated from when it was first sent, so the dead-letter queue keeps messages longer than the 4 days of the consumer's queue ([dead-letter queue retention](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/setting-up-dead-letter-queue-retention.html)) | poison message, dead-letter queue: L14; [SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html) |
| Visibility timeout (R84) | 180 s during which a received message is invisible to other consumers, six times the function's 30 s timeout | a message still being processed must not become visible to another invocation of the function. The timeout also gives Lambda time to retry when a function invocation is throttled because it cannot run yet | at least six times the function's timeout: [Lambda, configuring a queue](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html) |
| Repeats (R85) | the write is conditional on `update_id` | SNS and SQS standard deliver at least once | REL04-BP04; L14 |
| Order | not kept | standard topics and queues do not keep order. When order per key matters, keep the stream of main/ and add a second consumer to it | L14 (queue, topic and log compared) |
| How many copies of a consumer run (R87) | at most 2 concurrent Lambda invocations per queue (`MaximumConcurrency`) | a burst cannot use up the account's Lambda concurrency | [Lambda SQS scaling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html) |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/extensions.feature`.

- **R80** (optional) Where `ext-fan-out` is used, the recipe shall publish each update to the topic
  `$PREFIX-updates-topic` and have the topic deliver a copy to the queue of each consumer.
- **R81** (complex) Where `ext-fan-out` is used, while the history consumer is stopped, the recipe shall store
  every update in the dashboard table and keep every update in the history queue.
- **R82** (complex) Where `ext-fan-out` is used, when the history consumer starts, the recipe shall store every
  update that waited in its queue in the history table.
- **R83** (optional) Where `ext-fan-out` is used, the recipe shall let only the topic send to a consumer's queue
  and subscribe each queue with raw message delivery.
- **R84** (optional) Where `ext-fan-out` is used, the recipe shall move a message to its queue's dead-letter queue
  after 5 receives and set each queue's visibility timeout to 180 seconds, six times the function's timeout.
- **R85** (optional) Where `ext-fan-out` is used, the recipe shall store each update once, with its first arrival
  time, however often its message is delivered.
- **R86** (complex) Where `ext-fan-out` is used, if a message of a batch cannot be stored, then the recipe shall
  report that message to Lambda as failed and store the others.
- **R87** (complex) Where `ext-fan-out` is used, when `run.py --target aws` runs, the recipe shall create one
  function per queue, with batches of at most 10 messages and at most 2 copies running, and start the history
  function's mapping only after the updates are published.
- **R88** (optional) Where `ext-fan-out` is used, the recipe shall keep a message in a dead-letter queue for 14
  days and in a consumer's queue for 4 days.

## Run it

On your laptop, run the extension offline from `streaming-ingestion`:

```bash
uv run python ext-fan-out/run.py --target local
```

In a sandbox terminal (the recipe README says what that is). Run by us on 2026-11-12 (`../output/ext-fan-out.txt`).
In a sandbox terminal, sign in and check the account:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python ext-fan-out/run.py --target aws --stopped-for 60
```

`fanout_consumer.py` runs in Lambda, once as `$PREFIX-dashboard` and once as `$PREFIX-history`. `run.py` runs in
your terminal. The run removes what it created unless you pass `--keep`. In `streaming-ingestion`, run
`uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` to remove what is left. Cost: under 0.01 USD (from the price list, not measured): the first
million SNS requests a month are free, SQS requests are 0.40 USD a million.

## What you should see

From `output/ext-fan-out.txt` (a run on AWS with `--stopped-for 60`, 2026-11-12):

```text
while history was stopped: dashboard stored 200, history stored 0, history queue holds 200
```

- While history is stopped, the dashboard has every update and history's queue holds every update.
- After history starts, it stores every update too. Its "entered to stored" lag is at least the time it was stopped;
  the dashboard's "entered to stored" lag is not.
