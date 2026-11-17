# Operational first

Updates arrive at an API: a Lambda function behind a function URL. The function stores each update once in DynamoDB,
adds it to a counter for its minute and updates the key's current state only if the update is newer. `GET /number` adds up the last 60
counters. A full export copies the table to S3, and Athena counts the same hour over the export.

| File | What it holds |
|---|---|
| `handler.py` | the function: `POST /updates`, `GET /number`, `GET /current`; deployed on its own |
| `operational.py` | the table, the role, the function and its URL, signed calls, the export, the Athena table; `run_local` for the offline path |
| `run.py` | the steps in order: `--target local` (default) or `--target aws`; `--skip-export` stops after the API answers |

```bash
uv run python rep-operational-first/run.py                                     # offline
PREFIX=recipe-yourname uv run python rep-operational-first/run.py --target aws    # your sandbox
```

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/operational.feature`.

**API** (`handler.py`, in Lambda)

- **R50** (event) When `POST /updates` arrives at `handler.py`, the recipe shall store each
  update as one item, add 1 to the counter of its minute in the same transaction, and move the state of its key to
  it.
- **R51** (unwanted) If an update that `handler.py` receives has an `event_id` that is already stored, then the
  recipe shall count the update as a duplicate and leave the counter as it is.
- **R52** (unwanted) If an update that `handler.py` receives is older than the state of its key, then the recipe
  shall count the update and keep the newer state.
- **R53** (unwanted) If a request to `handler.py` is not JSON, holds no update or more than 100, or
  holds an update without the four fields in their form (the two ids as strings of 1 to 64 characters), then the
  recipe shall answer 400 and store nothing.
- **R54** (event) When `GET /number` arrives at `handler.py`, the recipe shall add up the 60
  minute counters before the minute of `as_of` with one eventually consistent `BatchGetItem`.
- **R55** (unwanted) If the `as_of` of a request to `handler.py` is not a time in the form
  `2026-11-12T16:00:00.000Z`, or has no hour before it, then the recipe shall answer 400.
- **R56** (unwanted) If a `GET /current` to `handler.py` names a key that has no update, then the recipe shall
  answer 404.
- **R57** (event) When a request to `handler.py` ends, the recipe shall write one JSON log line with Lambda's
  request id, the method, the route, the status and the counts of the reply, and with no id of an update or a key
  in it.
- **R58** (unwanted) If DynamoDB leaves counters unread in 5 reads in a row of a `GET /number` to `handler.py`, then
  the recipe shall answer 503 and no number.

**In the sandbox** (`run.py --target aws`)

- **R60** (event) When `rep-operational-first/run.py --target aws` runs, the recipe shall create the
  table on demand with point-in-time recovery and time to live, the function from `handler.py` alone, its log
  group with 7 days of retention, and a function URL that takes signed requests only.
- **R61** (optional) Where operational first is used, the recipe shall let the function get, put, update,
  batch-get and condition-check items of its table and write its own log group, and nothing else, unless
  `API_ROLE_ARN` names a role to use.
- **R62** (event) When `rep-operational-first/run.py --target aws` goes on to the lake, the recipe shall export the
  whole table to `exports/` in the bucket, point the Athena table `updates_export` at that export, and count its
  items of kind `event` in the window.

Read the Decisions section of the recipe's [README](../README.md) to see why each step is built this way.
