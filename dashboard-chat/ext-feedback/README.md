# User's verdict on an answer

This extension is not on the proposal page's list of extensions. It records which answers users mark down. It builds on
`main/`; read the recipe README first.

## What it changes in the main scenario

After step 7, the person who asked can mark the answer `up` or `down`. `feedback.py` stores the verdict and its time
on the trace item of that question (`feedback`, `feedback_at`):

1. In a real run it is one `UpdateItem` on the table `$PREFIX-chat-traces`, with three conditions: the item exists,
   its `user_hash` is that of the user who gives the verdict, and its `expires_at` has not passed.
2. In a local run the line of that request in `output/traces.jsonl` is rewritten, under the same conditions.
3. `feedback_run.py report` counts the verdicts and lists the answers marked `down`, each with its question, its SQL
   and its reply.

It does not create anything new in AWS, so `clean_up.py` already removes everything it writes: the verdict goes with the
trace table, or with the item when its time to live passes. DynamoDB deletes an expired item within a few days of
its expiry time, not at that second;
in between, the item is still in the table, and a verdict for it is refused as if it were gone.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Who says an answer is wrong (R110) | The person who asked, with one click: `up` or `down` | A trace shows what the LLM and DuckDB did, not whether the answer was right. An eval set is a set of questions with known answers for testing. A verdict does not need a known answer | Generative AI Lens GENOPS01-BP02 (collect and monitor user feedback; a thumbs up or down stored in a database is enough) |
| Where the verdict is kept (R110, R112) | The verdict is written on the trace item of the question only if that item exists | The question, the SQL and the verdict are read together; a second table would have to be joined and cleaned up. Without the condition, `UpdateItem` would create an item that holds a verdict and nothing else | L12–L13 (DynamoDB conditional writes); [UpdateItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_UpdateItem.html) |
| Whose verdict (R114) | Only the user who asked: the item's `user_hash` must be the hash of the user who gives the verdict. Every refusal uses the same wording for "not yours" and "not there" | A request id is not a secret; without the condition, anyone who has one could mark another user's answer. One wording for "not yours" and "not there" does not reveal that another user's request id exists | Generative AI Lens GENSEC01-BP03 (least privilege on data); L12–L13 (conditional writes) |
| Which verdicts (R111) | `up` and `down`, nothing else; a later verdict takes the place of an earlier one | Two values can be counted and compared across weeks. A free-text comment would be a place where personal data collects | Course choice |
| What to do with them (R113) | A report of the answers marked `down`, with question and SQL | Those questions are the first rows of your eval set, and the SQL usually shows what went wrong | Generative AI Lens GENOPS01-BP02; course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/feedback.feature`.

- **R110** (complex) Where `ext-feedback` is used, when a user marks an answer `up` or `down`, the recipe shall
  store the verdict and its time on the trace item of that question, in place of an earlier verdict.
- **R111** (complex) Where `ext-feedback` is used, if a verdict is neither `up` nor `down`, then the recipe shall
  refuse it and leave the trace item as it was.
- **R112** (complex) Where `ext-feedback` is used, if no trace item has the request id of a verdict, or the item has
  passed its `expires_at`, then the recipe shall refuse the verdict and create no item.
- **R113** (complex) Where `ext-feedback` is used, when `feedback_run.py report` runs, the recipe shall print the
  count of each verdict and, for each answer marked `down`, its question, its SQL and its reply.
- **R114** (complex) Where `ext-feedback` is used, if the user who gives a verdict is not the user who asked the
  question, then the recipe shall refuse the verdict and leave the trace item as it was.

## Run it

On your laptop, in `dashboard-chat`, offline. The first command prints one line per question with its `request_id`;
replace `REQUEST-ID` with one of them:

```bash
uv run python -m main.run --target local --model stub
uv run python ext-feedback/feedback_run.py mark REQUEST-ID down
uv run python ext-feedback/feedback_run.py report
```

`mark` gives the verdict as customer `c-0007`, the default asker of `main/run.py`; `--user` names another. Against your team sandbox, after a run with `--target aws` (same `PREFIX` and credentials):

```bash
uv run python ext-feedback/feedback_run.py --target aws mark REQUEST-ID down
uv run python ext-feedback/feedback_run.py --target aws report
```

## What you should see

`mark` prints the item's question with the verdict. `report` prints the counts, then one line per answer marked
`down`:

```
{"request_id": "5e1b3bf8-...", "question": "Delete all the orders", "feedback": "down", "feedback_at": "2026-11-13T08:41:36Z"}
6 answers: 0 marked up, 1 marked down, 5 not marked
{"request_id": "5e1b3bf8-...", "question": "Delete all the orders", "sql": "", "reply": "I could not run a query for that question.", "status": "no_answer"}
```

A verdict for a request id that has no trace item, for another user's answer, for an item past its expiry time, or
a verdict other than `up` and `down`, prints `not stored: ...` and exits with code 1. A run against a course sandbox on 2026-11-14, after `main/run.py --target aws`, is in
`../output/feedback-aws.txt`: `mark` stored the verdict on the trace item and `report` counted 6 answers, 1 marked
down.

## Cost

One verdict is one DynamoDB write (0.6875 USD per million write request units in ca-central-1, AWS bulk price list).
`report --target aws` reads the whole table with a Scan, which is fine for a demo's few hundred items.

## Adapt it

In a dashboard, put two buttons under the reply and call `DynamoFeedback(...).mark(request_id, "up", user_id)` or
`"down"` from their handlers, with the id of the signed-in user; `main.chat.answer` returns the `request_id`. Tell your users that their verdicts are stored
with their questions. Copy the questions marked `down` into your eval set with the answer you expected.
