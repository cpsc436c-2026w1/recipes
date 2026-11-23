# The read path: p50, p95 and p99 of each read, warm, cold and after a quiet time

Use this extension of the [lambda-dynamodb recipe](../README.md) if your system answers a user from a Lambda
function that reads DynamoDB (or another store), and you need the p95 and p99 of that answer, also on the first call
after a quiet time or on a fresh copy of the function. A fixed list of reads goes to the function URL one call at a
time, for each variant of the read (a key design, a consistency setting or another store), optionally while a
second process writes; each call's read units, the caller's time and its `REPORT` line go into DuckDB (a SQL
database in one local file, `results.duckdb`), and one SQL query gives the summary. It is the experiment of the final hand-in for a team whose story is a screen that must
answer within a second.

Words used here. Lambda writes a **`REPORT` line** to the function's CloudWatch Logs log group after each call:
its duration, billed duration, memory size and, when the call started a new execution environment, its `Init
Duration`. A **variant** is one way of doing the read. A call is **cold** when it started a new execution
environment of the function (its `REPORT` line has an `Init Duration`), **warm** otherwise. A **fresh copy** is
the first environment after a change to the function's configuration. **p50, p95 and p99** are the times at or under
which 50, 95 and 99 of 100 calls fall. A **service-level objective (SLO)** is the response-time target the run is
checked against.

## What it changes in main/

Nothing in `main/`: it calls the function and URL of `KEEP=1 bash main/run.sh` as they are (the actions `get` and
`rider` already return the `read_units` DynamoDB reports). In AWS it changes only two things, both undone by the
same run, also when a call fails: the 300 trips of rider `r-busy` in `$PREFIX-rider-trips` (`--busy-rider`) and the
function's variable `READ_PATH_ROUND` (`--fresh`). If rider `r-busy` already has items, or the function already has
`READ_PATH_ROUND`, and the ledger does not record them as left by a stopped run of this recipe, the run stops
before its first call and changes nothing (R321): it never overwrites, and then deletes, something it did not make.
What a stopped run left, it takes over and undoes. While its two changes exist they are on the ledger `state/ledger.jsonl`, so the
recipe's clean-up undoes a run that was stopped (Cost and clean-up). On your machine it writes `results.duckdb`.

The function URL of `main/` has auth type `NONE`: anyone who has the address can call it, and every call bills your
lease. Use the simulated data, keep the address to your team, and remove the URL with the clean-up when the
experiment is done.

## Steps

1. `run.sh` plans the calls of `variants.toml` (200 a variant, every key the same number of times, shuffled with
   seed 436) and sends them with `curl` to the URL, each after the answer of the one before. It stores the reply's
   `read_units`, the caller's time (`time_total`: from sending to the last byte), the status and the request id (the
   `x-amzn-RequestId` header).
2. With `--busy-rider` it writes the 300 trips of `r-busy` (`batch-write-item`, 25 a call, unprocessed items sent
   again), sends 200 `rider` calls for them and deletes them. With `--writer 5` the 200 calls run a second time
   while `writer.py`, a second process, updates the same trips at no more than 5 a second (botocore's adaptive
   retries, 10 attempts) and counts the throttled and retried writes.
3. With `--fresh 20` it changes `READ_PATH_ROUND` before each of 20 calls, waits until `LastUpdateStatus` is
   `Successful`, and removes the variable at the end.
4. It reads the `REPORT` lines of the run's time window (`filter-log-events`, asked again until every call's line
   has arrived, at most 90 s), joins them by request id and classes each call `cold` or `warm`. A call whose line
   has not arrived by then is classed `unknown`, shown as `no REPORT line` in the summary: not warm. The run prints
   `N of M calls joined with their REPORT line`; with N under M, treat the classes as incomplete.
5. `summary.sql`, one query over `results.duckdb`, gives the summary; `run.sh` prints it as Markdown.
6. `run.sh --idle` sends one `get` when the last call to the URL is at least the next gap of its plan old (5, 15, 30
   and 60 minutes, five rounds: 20 calls), and otherwise prints when to start it again.

## Data

The reads find the items of the dynamodb recipe, made up in the shape of the course's rides data (DS01, the TLC High
Volume For-Hire Vehicle trip records, Uber, Manhattan, March 2024). A team uses its own table and data. The IDs are
this extension's.

- **SIM01 stored-trips**: 50 trips `t-0000` to `t-0049` in `$PREFIX-trips`, and 50 past trips of riders `r-00` to
  `r-04` in `$PREFIX-rider-trips` (10 each; `ext-riders-and-drivers` adds `t-0100` for `r-03`).
- **SIM02 read-mix**: 600 calls: each trip read 4 times as `get-eventual` and 4 times as `get-strong`, each rider's
  history 40 times as `rider`, shuffled with seed 436.
- **SIM03 busy-rider**: 300 trips of `r-busy`, one every 10 minutes from 2026-10-01T00:00:00Z, about 75 bytes each
  (four short attributes), and 200 `rider` calls for `r-busy`.
- **SIM04 writer-updates**: 300 updates, one per SIM03 trip in an order shuffled with seed 436, each setting `rating`
  (1 to 5) and `rated_time` (not `status`, a DynamoDB reserved word).

**Call format** (`results.duckdb`, table `calls`, one row a call): `run_id`, `seq`, `kind` (`planned`, `after idle`,
`fresh copy`), `variant`, `action`, `key`, `read`, `writer`, `gap_min`, `round`, `sent_at`, `status`, `caller_ms`,
`read_units`, `request_id`, `class`, `duration_ms`, `billed_ms`, `memory_mb`, `init_ms`. Table `writer_runs`:
`updates_done`, `throttled`, `retried`, `failed`, `max_per_second`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| pace (R220, R310) | one call at a time | calls in parallel would measure the account's concurrency limit, not the read: a function URL answers 429 when the limit is reached, and one lease measured a limit of 10 concurrent executions on 2026-10-22. Yours: `aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions` | [function URL throttling](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html) |
| what is compared (R220) | variants named in `variants.toml` | teams compare two keys, two layouts or two stores; the harness and the summary are the same for each. Unchanged, the harness fits routes of the same Lambda function that return `read_units`: for another function or store, adapt the reply's fields, the log group the `REPORT` lines are read from, and the cost | ours |
| keys (R220, R270) | a fixed list, in a seeded order | a re-run sends the same calls; the busy rider and the quiet riders are named, so the two rows can be compared | ours |
| the writer (R280) | a second process with adaptive retries, paced so that no second holds more than the rate | the reads keep their pace while writes go on; adaptive retries slow the writer when DynamoDB throttles it, and the throttled writes are counted | botocore retries, adaptive mode |
| cold or warm (R240, R250, R260) | from the `REPORT` line | Lambda keeps a frozen environment "for a period of time" and ends environments every few hours without saying how long; the line's Init Duration says what happened | [execution environment lifecycle](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html); L2 |
| idle round (R250) | one call per start, from a laptop, five rounds | a CloudShell session ends after 20 to 30 minutes without keyboard input (running processes do not count) and a sign-in may end within the round; the state in `results.duckdb` lets it go on after a break. The 20 calls are listed one by one with their class, not summarised as a percentile | [CloudShell limits](https://docs.aws.amazon.com/cloudshell/latest/userguide/limits.html) |
| fresh copies (R260) | change an environment variable before each call | after an update the next call should meet a new environment with the new configuration (unchecked: each call's class is read from its `REPORT` line, not assumed), and 20 such calls give the spread of first answers | ours |
| percentiles (R290) | p95 only from 20 calls, p99 only from 100, `quantile_cont` (interpolated) | these are reporting cutoffs, not accuracy guarantees: `quantile_cont` interpolates between ordered calls at any count, so a p95 need not be a time any call took, and from very few calls it says little; with 200 calls p99 lies between the 198th and 199th fastest | L13 |
| read units (R220, R290) | from the reply | DynamoDB rounds a `GetItem` up to 4 KB and the total of a `Query`'s items up to the next 4 KB; the handler returns the measured value | [read units](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html) |
| two clocks (R220, R290) | the caller's time beside the Lambda duration | the rider waits for the network and the URL as well; the duration is only the handler | ours |
| a cold call's price (R300) | its billed duration | the billed duration includes the init phase: the captured cold call ran 316.02 ms, had an Init Duration of 493.18 ms and was billed 810 ms (`../output/ext-metrics.txt`) | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| failed calls (R310) | stored with their status, left out of the percentiles, counted per variant | a throttled call that is averaged in hides the throttle; a row with more than 2 failed calls in 200 needs a second look | ours |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/read_path.feature`. A recipe has more lines than a team's story: a team keeps 2 to 5
per story.

- **R220** (event) When `ext-read-path/run.sh` runs, the recipe shall send 200 calls of each variant in
  `variants.toml` to its URL one call at a time, with keys from a fixed list in a seeded order, and store for each
  call the read units in the reply, the caller's time from send to last byte, the HTTP status and the request id.
- **R230** (event) When the calls are done, the recipe shall join each call with its `REPORT` line by request id and
  store its duration, billed duration, memory size and init duration in `results.duckdb`.
- **R240** (ubiquitous) The recipe shall class a call `cold` when its `REPORT` line has an Init Duration and `warm`
  when it does not.
- **R250** (event) When `ext-read-path/run.sh --idle` is started, the recipe shall send one `get` only if the last
  call to the URL is at least the next gap of the plan old (5, 15, 30 and 60 minutes, five rounds), store it as
  `after idle` with its gap and class, and otherwise print when the next call is due.
- **R260** (complex) Where the fresh-copy round is used, when `run.sh --fresh <n>` runs, the recipe shall change the
  function's environment variable `READ_PATH_ROUND` before each of n calls, wait until the update is done, and store
  each call as `fresh copy` with its class.
- **R270** (complex) Where the busy rider is used, when `run.sh --busy-rider` runs, the recipe shall write 300 trips of
  rider `r-busy` before its 200 `rider` calls for `r-busy` and delete them after the calls, also when a call fails.
- **R280** (complex) Where the writer is used, while the busy rider's calls run with `--writer <rate>`, the recipe
  shall update the busy rider's trips at no more than that rate a second from a second process, store each call as
  `writer on`, and count the writer's throttled and retried writes.
- **R290** (event) When the calls are stored, the recipe shall give from one SQL query over `results.duckdb`, for each
  variant, read, class and writer state, the number of calls, the p50, p95 and p99 of the caller's time and of the
  Lambda duration and the median read units, a p95 only from 20 calls and a p99 only from 100, and list each
  `after idle` and `fresh copy` call on its own line.
- **R300** (event) When the summary is written, the recipe shall give each row's cost of 1,000 calls from its medians
  and the course price sheet (Lambda requests, billed GB-seconds and read units), for warm and for cold calls.
- **R310** (unwanted) If a call answers with an HTTP status other than 200, then the recipe shall store it as failed
  with its status, leave it out of the percentiles and count it in its variant's row.
- **R320** (ubiquitous) The recipe shall change in AWS only the busy rider's 300 trips and the variable
  `READ_PATH_ROUND`, and undo both before the run ends, also when a call failed.
- **R321** (unwanted) If rider `r-busy` already has items in `$PREFIX-rider-trips` (with `--busy-rider`) or the
  function already has `READ_PATH_ROUND` (with `--fresh`), and the ledger does not record them as created by a run
  of the recipe, then the recipe shall stop before its first call, change nothing, and say which of the two it
  found.


## Response time and assumptions

**Response time (SLO of R220):** under 1 s at p99 over the warm and cold calls of each variant (the summary's `all`
row), from the call leaving the caller to the last byte of its answer. Load: SIM01's items, with `--busy-rider` the
300 trips of SIM03; one call at a time from one laptop terminal in Canada; no other callers; 200 calls a variant.
The `after idle` and `fresh copy` calls are listed one by one beside it, not inside it. Measure the p99 from your
own laptop before you say whether your variant meets it. One run's result, for example (*measured* on 2026-11-23
from an EC2 instance in ca-central-1, so it does not test the laptop target; `../output/ext-read-path.txt`): the
`all` rows' p99 were 82.7 ms (`get-eventual`), 83.3 ms (`get-strong`), 84.2 ms (`rider`), 332.5 ms (busy rider,
writer off; its one cold call 898 ms) and 222.6 ms (busy rider, writer on). The 20 fresh copies took 624 to 1,042 ms
at the caller, one of them over 1 s.

| Id | What the case does not say | Who would answer | We assume | Affects |
|---|---|---|---|---|
| Q010 | How many reads does the app make in a day? | the app's team | 1,000 trip reads and 200 history reads | Cost |
| Q020 | How long is the app quiet between riders? | the app's team | up to an hour at night; the idle round goes to 60 minutes | R250 |
| Q030 | How large is one trip in a rider's history? | the app's team | about 75 bytes (four short attributes) | R270 |
| Q040 | How fast do updates arrive while riders read? | the app's team | 5 writes a second; set your own replay's rate | R280 |

## Run it

### Laptop, offline

From your laptop, in a terminal in the `lambda-dynamodb` folder; nothing here calls AWS:

```bash
uv sync
uv run pytest -q tests/test_read_path.py
uv run behave features/read_path.feature       # about two minutes: the writer's throttled updates are retried
DRY=1 bash ext-read-path/run.sh --busy-rider     # what it would do; nothing is called
```

`pytest` and `behave` report no failures. The `DRY=1` run prints the AWS calls it would make and calls nothing.

### Run it live

The capture of one live run is `../output/ext-read-path.txt` (the set-up, `--busy-rider --writer 5 --fresh 20`,
two of the idle calls, `--summary`) and its clean-ups with their proof `../output/clean-up-ext-read-path.txt`.

Run it from your laptop, not CloudShell: the idle round takes hours, longer than a CloudShell session. Open a
terminal in the folder that holds both `dynamodb/` and `lambda-dynamodb/` (your copy of the recipes; `ls` shows both)
and sign in with your lease's credentials. Choose one `PREFIX` for this experiment, unique to you, and use it for
the set-up, every run and the clean-up:

```bash
aws sso login                                 # or the sign-in your lease uses
aws sts get-caller-identity                   # "Account": your lease's account
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
cd dynamodb && KEEP=1 bash main/run.sh && KEEP=1 bash ext-riders-and-drivers/run.sh && cd ..
cd lambda-dynamodb && KEEP=1 bash main/run.sh   # the function and its URL; the rest runs in lambda-dynamodb
```

Then, still in `lambda-dynamodb`, the planned calls and the busy rider, the idle round, and only then the fresh
copies. The fresh-copy round ends by removing `READ_PATH_ROUND`, itself a configuration change, so an idle call
right after it may meet a new environment for that reason and not because of the gap:

```bash
bash ext-read-path/run.sh --busy-rider --writer 5   # 600 planned calls, then 400 busy-rider calls
bash ext-read-path/run.sh --idle                    # again when it says; 20 starts in all, over hours
bash ext-read-path/run.sh --no-plan --fresh 20      # 20 fresh copies, after the idle round
bash ext-read-path/run.sh --summary > ext-read-path/summary.md
```

Each run ends with the summary table. `summary.md` holds it: one row per variant, read, writer and class, the
`failed` count of each row, and the after-idle and fresh-copy calls one by one. The fresh-copy round needs
`lambda:UpdateFunctionConfiguration`. If your role refuses it, the round stops at its first change and says so,
nothing else changes, and the idle round's cold calls are your evidence of cold calls instead.

The experiment record is [`experiments/read-path.md`](../experiments/read-path.md): fill in its Result and
Conclusion from the summary. Keep each run's evidence (the command, the commit, `results.duckdb`) in
`evidence/<run id>/`.

## What you should see

Offline, the scenarios. Live, one run's output, for example (*measured*, `../output/ext-read-path.txt`; your counts
of throttled and retried writes, your cold and warm classes and your times differ): `run <id>: 600 planned calls`, the busy
rider's lines `300 trips of r-busy written` and `deleted`, the writer line `300 updates, 0 throttled, 0 retried, at
most 5 in one second`, `1020 of 1020 calls joined with their REPORT line`, then the summary table: one row per
variant, read, writer and class, and an `all` row per variant that the SLO lines below the table read; below them
each fresh-copy and after-idle call. The checks: every call joined with its `REPORT` line, each row's `failed`
count, and a class for every call. In that run each fresh copy was `cold` (init 269 to 497 ms); the first planned
call was warm, because `main/run.sh` had just called the function; one busy-rider call in the middle was cold. Its
first after-idle call (5 minutes) came right after `--fresh` and was cold as well, which cannot be put down to the
gap: that is why the order above runs the idle round first. The 15-minute call was cold too (init 356 ms, 755 ms at
the caller): one call, not yet a rate.

## Cost and clean-up

Prices: ca-central-1, the course price sheet (`436c-student/prices.md` on the course server, rates read
2026-11-18; the rows used are in `../prices.toml`); USD with CAD at 1.45, the course's planning rate. 1 GB = 2^30
bytes. A Lambda GB-second is the memory in MB divided by
1,024, times the seconds. *computed*, not measured. Billed durations: 25 ms a
`get` and 64 ms a `rider` (measured, the `REPORT` lines in `../output/ext-metrics.txt`), 100 ms for the busy rider
(assumed), 810 ms a cold call (measured); half of the after-idle calls cold (assumed), every fresh copy cold. Free
tier left out. One run: 600 calls, 400 busy-rider calls with and without the writer, 20 after idle, 20 fresh copies:
1,040 calls. 30 days: 30,000 trip reads and 6,000 history reads (Q010) and two runs.

| Part | One run | 30 days and two runs | A year and two runs |
|---|---|---|---|
| Lambda requests (0.20 USD per million requests) | 0.00021 | 0.0076 | 0.088 |
| Lambda duration, 128 MB, 10.92 GB-s a run (0.0000133334 USD per GB-second, arm64) | 0.00015 | 0.0022 | 0.023 |
| DynamoDB reads, 1,620 units a run (0.5 a `get`, 1.0 a strong `get`, 0.5 a `rider`, 3.0 a busy-rider Query; 0.1375 USD per million read request units) | 0.00022 | 0.0029 | 0.030 |
| DynamoDB writes, 300 puts, 300 deletes and 300 updates of the busy rider (0.6875 USD per million write request units) | 0.00062 | 0.0012 | 0.0012 |
| CloudWatch Logs ingestion, 800 bytes a call (assumed; 0.55 USD per GB), kept 7 days (`main/` sets the retention) | 0.00043 | 0.016 | 0.18 |
| Data transfer out, 1 KB a reply and 22.5 KB a busy-rider reply (assumed; 0.09 USD per GB) | 0.00083 | 0.0048 | 0.040 |
| **Total** | **0.0025 USD (0.0036 CAD)** | **0.035 USD (0.050 CAD)** | **0.36 USD (0.53 CAD)** |

Against the 30 USD lease, 30 days are about 0.1% and a year about 1.2%. The 30-day figure counts warm calls only: at
810 ms billed a cold `get` costs about five times a warm one (0.0016 against 0.0003 USD per 1,000 calls), so if your
users come in bursts, read your share of cold calls from the idle round and price them apart.

*measured* (2026-11-23, `../output/ext-read-path.txt`): the units of the run, from `results.duckdb` (each call's
`REPORT` line and read units) and CloudWatch, times the same price sheet. The run: 600 planned calls, 400 busy-rider
calls with and without the writer, 20 fresh copies and 2 after-idle calls (not 20): 1,022 calls, 23 of them cold.

| Part | Measured units | USD | CAD |
|---|---|---|---|
| Lambda requests | 1,022 | 0.00020 | 0.00030 |
| Lambda duration, 128 MB arm64 | 82,999 ms billed (10.37 GB-s); median 29.5 ms a warm `get`, 130 ms a warm busy-rider Query, 646 ms a cold call | 0.00014 | 0.00020 |
| DynamoDB reads | 1,672.5 read units (3.0 a busy-rider Query, 3.5 with the writer on) | 0.00023 | 0.00033 |
| DynamoDB writes | 900 write units (300 puts, 300 updates, 300 deletes) | 0.00062 | 0.00090 |
| CloudWatch Logs ingestion | 496,400 bytes (486 a call) | 0.00025 | 0.00037 |
| Data transfer out | 13.6 MB, from reply sizes read after the run (116 bytes a `get`, about 32,500 a busy-rider reply), not metered | 0.00114 | 0.00165 |
| **The run** | | **0.0026** | **0.0037** |

That is 0.009% of the 30 USD lease; the set-up it needs (the dynamodb recipe's main and riders, this recipe's main)
added 0.0001 USD. The computed row above holds: the busy rider's replies are most of the cost (they were 32 KB, not
22.5 KB), and a cold call was billed a median of 646 ms, not 810.

Clean-up. A finished run has undone its own two changes; the ledger shows both `deleted`. What stays after a
finished run is the set-up: the function and URL of `main/` (`KEEP=1`) and the two dynamodb runs' tables. Their
storage and the public URL stay until you remove them, so clean up when the experiment is done, also after a
successful run.

From your laptop, in `lambda-dynamodb`, with the same `PREFIX`. The recipe's `clean_up.py --ext read-path` lists,
bare, what a stopped run left (the busy rider's items and the variable `READ_PATH_ROUND`) and deletes nothing; with
`--yes` it removes them. `main/`'s API stays; without `--ext`, `--yes` removes it too (use that only when the whole
recipe goes).

```bash
uv run python clean_up.py --ext read-path            # lists what it would remove; deletes nothing
uv run python clean_up.py --ext read-path --yes      # removes it
```

Then the tables, in `dynamodb`, the same way (its clean-up covers `main` and `ext-riders-and-drivers`; read the
list before `--yes`, and only delete tables your project no longer needs):

```bash
cd ../dynamodb
bash clean_up.sh                     # lists what it would delete; deletes nothing
bash clean_up.sh --yes               # deletes it
```
