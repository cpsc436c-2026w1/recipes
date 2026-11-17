# Retried write that applies once

A rider taps "add 5 tip". The write reaches DynamoDB and is applied, but the reply is lost and the app sees a timeout.
The app cannot tell a lost request from a lost reply, so it sends the write again (boto3 does the same on its own,
before the app sees the timeout: see Decisions, "Who retries"). Without a request id the tip is
added twice. With one, the retry is recognised and the tip is added once.

**Words used here.**

- **Request id**: an id the app makes once, when the rider taps, and sends with every attempt of that tap (L14
).
- **Idempotent write**: a write that leaves the same result whether it is applied once or several times.

## What the idempotent retry extension changes in main/

| Main scenario | This extension |
|---|---|
| two clients race to accept a trip | one client adds a tip, and its first reply is lost (`tips.py` `ReplyLostOnce`) |
| `UpdateItem` with a condition on the version | `TransactWriteItems`: a marker item for the request id, on the condition that it does not exist, and the `ADD` of the tip |
| table `$PREFIX-race-trips` | also `$PREFIX-race-requests` (key `request_id`). The table's time to live uses `expires_at` to remove markers within a few days of their expiry |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Where the id is made (R50) | the phone makes the id with `uuid4` when the rider taps | only the phone that starts the request can tell a retry from a new tap. An id made by the server or the function would be new on every attempt | request id, end-to-end argument: L14; [DDIA 2e ch. 13](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch13.html), "Uniquely identifying requests", "The end-to-end argument" |
| How a repeat is detected (R51, R53) | the marker and the tip are written in one transaction. The marker's condition `attribute_not_exists(request_id)` refuses a repeat, so the tip is not added either | marker and tip are written together or not at all, so there is no moment where one exists without the other | [DDIA 2e ch. 13](https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/ch13.html), "Duplicate suppression"; [REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html) make mutating operations idempotent |
| DynamoDB's own token (R56) | also send `ClientRequestToken = request_id` | DynamoDB then treats a repeat within 10 minutes as done and returns success without a change. The marker covers a retry after 10 minutes | [DynamoDB transaction idempotency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html) |
| How long markers stay (R55) | 7 days (course default: longer than any retry the app makes), then removed by the table's time to live, within a few days of that time | the requests table does not grow forever | [DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html) |
| Retries (R54, R58) | at most 3 attempts (course default); before the second a random wait of at most 0.2 s, before the third of at most 0.4 s (course default); no wait after the last | a lost reply is retried, and retries to a service that is down stop at the attempt limit. The wait is random, so that many apps that timed out at the same moment do not all come back at the same moment | [REL05-BP03](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html) control and limit retry calls: exponential backoff, jitter, a maximum number of retries |
| Who retries | the app in this extension; in a real app, boto3 does it before the app sees anything | boto3 sends a call again on a connection error or a timeout: up to 4 attempts for DynamoDB in its default (standard) mode. So a naive `ADD` can be applied twice with no retry code in the app at all. The request id is needed as soon as a write is not idempotent, whether or not the app retries | [boto3 retries](https://docs.aws.amazon.com/boto3/latest/guide/retries.html) |
| Tip on a trip that does not exist (R53) | refused (`attribute_exists(trip_id)` on the update) | a typo in the id must not create a trip. The whole transaction is refused, marker included | course choice |
| How the timeout is made (R50, R52) | `ReplyLostOnce` applies the first write, then raises `ReadTimeoutError` | a real timeout happens by chance. The test needs the case where the write was applied and the reply was lost | course choice |

A transactional write costs two write units per item up to 1 KB, so each tip takes 4 write units against 1 for the
naive write ([read and write operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html)).

## Requirements

The form in parentheses and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/idempotent_retry.feature`.

- **R50** (complex) Where `ext-idempotent-retry` is used, when a write was applied and its reply is lost, the recipe
  shall send the write again with the same request id.
- **R51** (complex) Where `ext-idempotent-retry` is used, if a tip arrives with a request id that was applied
  before, then the recipe shall leave the tip total as it is.
- **R52** (complex) Where `ext-idempotent-retry` is used, when the naive write is sent again after a lost reply, the
  recipe shall show the tip added twice.
- **R53** (complex) Where `ext-idempotent-retry` is used, if a tip names a trip that does not exist, then the recipe
  shall refuse the tip and write neither a trip nor a marker.
- **R54** (complex) Where `ext-idempotent-retry` is used, if a write gets no reply in 3 attempts, then the recipe
  shall stop with an error that names the 3 attempts.
- **R55** (optional) Where `ext-idempotent-retry` is used, the recipe shall give every marker an expiry time 7 days
  ahead in `expires_at`, the attribute of the table's time to live.
- **R56** (optional) Where `ext-idempotent-retry` is used, the recipe shall send the request id as the
  `ClientRequestToken` of the transaction.
- **R57** (complex) Where `ext-idempotent-retry` is used, when a run with `--target aws` ends without `--keep`, the
  recipe shall remove both tables.
- **R58** (complex) Where `ext-idempotent-retry` is used, when an attempt other than the last times out, the recipe
  shall wait a random time before the next attempt: at most 0.2 seconds after the first, at most 0.4 seconds after
  the second.

## Run it

From your local device (laptop), in a terminal in `concurrency-correctness`, run the extension offline:

```bash
uv run python ext-idempotent-retry/run.py --target local
```

In a sandbox terminal (the recipe README says what that is), sign in and check that the account is the one you expect:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

In the sandbox terminal, run the extension:

```bash
export AWS_REGION=ca-central-1 PREFIX=recipe-yourname
uv run python ext-idempotent-retry/run.py --target aws
```

Run by us on 2026-11-12 (`../output/ext-idempotent-retry.txt`).

Clean-up: after `--keep` or a run that stopped early, run
`uv run python clean_up.py` with the same `PREFIX`. The run removes both tables itself. Cost: two tables, 2 plain writes and 2 transactions of 2 items:
far below 0.001 USD (0.6875 USD per million write request units in ca-central-1, AWS bulk price list).

## What you should see

From `output/ext-idempotent-retry.txt` (a run on AWS, 2026-11-12):

```text
naive: rider taps 'add 5 tip' on t-tip-naive, the app sends it with no request id
  attempt 1: timed out (ReadTimeoutError), retrying
  attempt 2: applied
  tip on the trip now: 10 (the rider asked for 5)

request id: rider taps 'add 5 tip' on t-tip-once, the app sends it with request id <uuid4>
  attempt 1: timed out (ReadTimeoutError), retrying
  attempt 2: applied
  tip on the trip now: 5 (the rider asked for 5)
```

On AWS the second attempt with the request id prints `applied` (moto prints `already applied`): DynamoDB recognises the
`ClientRequestToken` and returns success without a change. moto does not implement the token, so offline the marker
refuses the repeat. In both cases the tip is 5.
