# Riders and drivers: a second key and writes that change two tables together

> ⚠️ Run dry runs on your laptop and AWS commands in a sandbox terminal. The scripts are in your clone; DynamoDB is
> a service AWS runs.

Extension of the [dynamodb recipe](../README.md). Riders open their trip history, newest first, and drivers have a
status (available, busy) that must agree with the trips they hold. The trip history and driver status requirements need a second way to find trips and writes
that change two tables together.

## What it changes in main/

- Two more tables: `$PREFIX-rider-trips` (partition key `rider_id`, sort key `requested_at`) and `$PREFIX-drivers`.
- A new trip goes into `$PREFIX-trips` and `$PREFIX-rider-trips` in one transaction; an accept changes the trip and the
  driver in one transaction.
- The fare carries a version number, and each write sends the version it read.
- Run `main/run.sh` first with `KEEP=1`. The extension does not change it.

Words used here. A **sort key** orders the items under one partition key, so `Query` reads one rider's trips in time
order, or a time range of them. A **transaction** (`TransactWriteItems`) applies up to 100 writes on different items
in one or more tables. It either applies all the writes or does not apply any of them. Each write can carry its own condition.

## Steps

1. Create the two tables.
2. Load 50 past trips of 5 riders over 10 days, and 5 available drivers.
3. Step 3b: remove trips t-0100 and t-0101 left by an earlier run, and make d-3 available.
4. Rider r-03 opens their history for Oct 3 to Oct 6: `Query` on one partition key and a sort-key range.
5. ...and their latest trip: newest first, one item.
6. r-03 requests trip t-0100: one transaction puts it in both tables.
7. Driver d-3 accepts t-0100: the trip gets the driver and the driver becomes busy, together.
8. Rider r-01 requests trip t-0101 the same way.
9. d-3, still busy, accepts t-0101: the driver's condition fails, so neither item changes.
10. Two screens change t-0100's fare, each sending the version it read. The first write succeeds; the second is refused.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| second access pattern (R50, R51, R52) | its own table and key, written together with the first | shows the transaction that keeps two copies in agreement. A global secondary index is the usual alternative and does not need a transaction. It is updated after the write, not with it. A read from the index is eventually consistent ([read consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadConsistency.html)) | key design from access patterns (L12–13; [DynamoDB access patterns](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-general-nosql-design.html)) |
| transaction only where items must agree (R52, R53) | trip + rider history, trip + driver | a transactional write uses 2 write units per item up to 1 KB, twice a plain one, also when the transaction is cancelled; one item needs only a conditional write | L12–13; [transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html) |
| optimistic locking (R54) | `version = :seen` on every fare write | the system does not hold a lock while a person edits. A writer with an old version is refused. The writer reads again and decides | L12–13 (compare-and-set); [optimistic locking with version number](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/BestPractices_OptimisticLocking.html) |
| second run (R55) | step 3b deletes the two trips of an earlier run and makes d-3 available before the transactions | the transactions carry conditions (`attribute_not_exists(trip_id)`, driver `available`) that an earlier run's items would fail | Course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/riders_and_drivers.feature`.

- **R50** (complex) Where `ext-riders-and-drivers` is used, when a rider opens their history for a range of days,
  the recipe shall return that rider's trips of those days in time order from one Query that reads only those
  trips.
- **R51** (complex) Where `ext-riders-and-drivers` is used, when a rider opens their latest trip, the recipe shall
  return the newest trip of that rider from a Query limited to one item.
- **R52** (complex) Where `ext-riders-and-drivers` is used, when a rider requests a trip, the recipe shall write the
  trip to `$PREFIX-trips` and to `$PREFIX-rider-trips` in one transaction.
- **R53** (complex) Where `ext-riders-and-drivers` is used, if a driver who is busy accepts a trip, then the recipe
  shall leave the trip and the driver as they were.
- **R54** (complex) Where `ext-riders-and-drivers` is used, if a fare write carries a version other than the trip's
  current one, then the recipe shall refuse the write and leave the fare and the version as they were.
- **R55** (complex) Where `ext-riders-and-drivers` is used, when the extension runs a second time, the recipe shall
  end with the same items in its three tables as after the first run.

## Run it

Run by us on 2026-10-21 (`../output/ext-riders-and-drivers.txt`). From your sandbox terminal, sign in and check that the account is the one you expect. The recipe README explains
what a sandbox terminal is:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

From your sandbox terminal in `dynamodb`, use your own name in place of `ana`:

```bash
export PREFIX=recipe-ana AWS_REGION=ca-central-1
KEEP=1 bash main/run.sh                       # first, once: the trips table stays
DRY=1 bash ext-riders-and-drivers/run.sh      # prints every command, runs nothing
bash ext-riders-and-drivers/run.sh            # runs the ten steps, then cleans up the whole recipe
```

To go on to the nightly report, run the last line with `KEEP=1` in front. A run without `KEEP=1` ends with clean-up.
Otherwise, run `bash clean_up.sh` in the same terminal with the same `PREFIX`. Cost: see below.

## What you should see

Captured: [../output/ext-riders-and-drivers.txt](../output/ext-riders-and-drivers.txt). Step 4 returns 4 trips in time
order. The query reads 4 items and uses 0.5 read units. Steps 6 to 8 use 2.0 write units on each table. Step 9 prints
`TransactionCanceledException` with one reason per item, in order: `None` for the trip, `ConditionalCheckFailed` for
the driver; t-0101 is still `requested` and does not have a driver. In step 10, the first fare write succeeds (fare 24, version 2).
The second is refused.

## Cost

The run uses about 70 write units, mostly for the history load. The charge is well under one cent (0.6875 USD per million write request units in
ca-central-1, AWS bulk price list).
