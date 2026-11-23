# Athena as the chat's engine

This extension serves systems where a model writes or fills SQL that runs on Athena: the table lives in the lake,
other jobs write to it, and it is larger than the chat's machine should load. It builds on `main/`. Read the recipe
README first.

The rides case: an operations analyst asks a question about trips in plain words ("How many trips were requested in
zone 161 on 2024-03-14?"), the model writes one SQL query through the `run_sql` tool, and the query runs on Athena
over a `trips` table in the lake instead of the in-memory DuckDB of `main/`. The analyst gets the count with the
query that produced it, so the number can be checked and the query run again, while the chat cannot change the lake
or scan more than its limits allow.

## What it changes in the main scenario

| Step of the main scenario | With this extension |
|---|---|
| 1. The data | Tables `trips` and `zones` in the Glue database `<prefix>_chat`, Parquet under `chat/` of the lake bucket, one folder per request day (`deploy.py`) |
| 2. System prompt | The rides prompt and a `run_sql` tool that says Athena (`chat_athena.py`); the loop is `main/chat.py`'s, at most 4 model calls |
| 3. Model call | Through `DeadlineModel`: no call starts after the question's deadline, and each call's timeout is the smaller of 6 s and the time left |
| 4. SQL check | `main/sqlcheck.py` with the dialect `athena`; the names of common table expressions (CTEs, the named queries of a `WITH`) count only inside their own query |
| 5. The query | `athena_engine.py` counts the question's queries and invokes the function `<PREFIX>-chat-query` (`query_fn.py`), which reserves against a daily cap, runs the query in the workgroup `<PREFIX>-chat-wg`, waits at most 10 s or to the deadline, and returns typed rows |
| 7. Trace | The trace item also holds each query's Data scanned, what Athena bills, the Athena cost in USD and the model calls' cost in CAD |

Three small changes in `main/`, each with a default that keeps the main scenario as it was: `check_sql` takes a
`dialect` (default `duckdb`) and scopes CTE names (a fix for both dialects); `answer()` takes the prompt and the
tool definition (defaults: the shop's); `GatewayModel` has `request_timeout_s` (default: the client's timeout), and
an engine may raise `ToolRefused`, whose text goes back to the model word for word.

## Sketch

Analyst's question → `chat_athena.ask` (deadline 18 s) → `main/chat.py` → course LLM gateway (tool call `run_sql`) →
`main/sqlcheck.py` → query counter → Lambda `<PREFIX>-chat-query`, as its role `<PREFIX>-chat-reader` → daily cap in
DynamoDB `<PREFIX>-chat-usage` → Athena workgroup `<PREFIX>-chat-wg` (cutoff 100 MB a query) → Glue tables
`<prefix>_chat.trips` and `<prefix>_chat.zones` over S3 → typed rows back to the model → reply → trace item in
`<PREFIX>-chat-traces`.

On this page and in the code, 1 MB is 2^20 bytes, 1 GB 2^30 bytes and 1 TB 2^40 bytes; the workgroup's cutoff is set
in bytes (100 MB = 104,857,600).

## Decisions

| Choice | Instead of | Why | Constraint or best practice (source) |
|---|---|---|---|
| Athena over a table in the lake | DuckDB in the chat's process, as in `main/` | Athena when the table lives in the lake, other jobs write to it, and it is larger than the chat's machine should load; DuckDB when the table fits in memory, because it does not bill per query, while every Athena query bills at least 10 MB of Data scanned. At 200 questions a day with 2 queries each, the minimum alone is about 0.63 USD a month. For a table far smaller than 10 MB each query still bills 10 MB, 0.0000524 USD: 1,000 queries cost 0.052 USD against 0 for numbers computed ahead and read from a file | [Athena pricing](https://aws.amazon.com/athena/pricing/) (10 MB minimum, read 2026-11-21); price sheet, ca-central-1, 2026-11-18 (5.50 USD per TB) |
| The extension's own table `<prefix>_chat.trips` | the team's Iceberg table `<prefix>_lake.trips` | the recipe runs before a team has a lake, and DS01 (the March 2024 trips, under Data) is history a live lake does not hold. Its column names are the lake table's, so the prompt and the eval cases carry over. `deploy.py` builds only the recipe's own database and stops when `CHAT_DATABASE` is set; for your lake's table, see Adapt it | course convention: one lake layout |
| Registered partitions (`MSCK REPAIR TABLE` once, checked with `SHOW PARTITIONS`) | partition projection | the 32 request days are loaded once and never change. Without registration the table returns 0 rows and `COUNT(*)` answers 0 without an error, so `deploy.py` checks the count | L11 (partitions) |
| The cutoff in the workgroup, with its settings enforced | a `LIMIT` in the SQL, or a scan estimate in code | a `LIMIT` bounds the rows returned, not the Data scanned, so it cannot enforce a scan budget; the workgroup cancels any query past the cutoff, whatever the SQL says. A cancelled query is still billed for what it scanned; the cutoff can be no lower than 10,000,000 bytes | [per-query data usage control](https://docs.aws.amazon.com/athena/latest/ug/workgroups-setting-control-limits-cloudwatch.html); [BytesScannedCutoffPerQuery](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-athena-workgroup-workgroupconfiguration.html); students set a cutoff on a workgroup in A3 |
| A workgroup of the chat's own, `<PREFIX>-chat-wg`, from `ensure_workgroup` of `scripts/lake.py` | the team's shared query workgroup `<PREFIX>-wg` | the reader role is scoped to one workgroup, and the chat's 100 MB cutoff is lower than other queries need. The helper knows this name's settings; an existing workgroup with other settings stops `deploy.py` and is never changed | course convention: one helper for workgroups |
| A daily cap in code, 5 GB billed a day, reserved before each query | a workgroup-wide data usage alert | the cutoff bounds one query and the counter one question, but nothing bounds the questions of a day; a workgroup-wide limit only sends an alarm, it does not stop queries (same page). 5 GB is above 200 questions × 2 queries × 10 MB ≈ 3.9 GB a day. A reserve of 100 MB per query means concurrent questions cannot pass the cap together; a cancelled query can scan a little past its cutoff, so the cap is a target, not an exact ceiling | Generative AI Lens GENCOST05-BP01 (bound the work per request) |
| Queries in a Lambda function whose execution role is the reader role | assuming the reader role from the person's own sign-in | execution roles and functions are what students made in A1 and A3; assuming a role one made from one's own sign-in has no run on record in the course | students made functions and execution roles in A1 and A3 |
| The reader role may start queries only in `<PREFIX>-chat-wg` | `athena:*` on every workgroup | a role that may use `primary` runs the same query there without a cutoff; Athena takes the workgroup's ARN as the resource of `StartQueryExecution` | [example workgroup policies](https://docs.aws.amazon.com/athena/latest/ug/example-policies-workgroup.html); Generative AI Lens GENSEC01-BP03 (least privilege) |
| The parse check before Athena, and the role | the role alone | the role stops data-definition statements (DDL: `DROP TABLE` needs `glue:DeleteTable`, `CREATE TABLE` needs `glue:CreateTable`), but not a read of `information_schema`, and whether it stops an `UNLOAD` into its result prefix is not tried; every statement that reaches Athena may bill. The check refuses them before they are sent | [Glue access for Athena](https://docs.aws.amazon.com/athena/latest/ug/fine-grained-access-to-glue-resources.html); L15 (guarded text-to-SQL) |
| CTE names count only inside their own query (sqlglot's scopes) | one list of every CTE name in the query | with one list, `SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret)` passes, and the outer `secret` reaches the engine as a table name | ours: a name check must follow SQL's own visibility rules |
| At most 3 queries a question, counted in the engine | only the cap of 4 model calls of `main/` | one reply can hold several tool calls, so the model-call cap does not bound the queries; with both caps a question scans about 300 MB at most | Generative AI Lens GENCOST05-BP01 |
| One absolute deadline of 18 s for the question, passed to every wait: a model call at most 6 s and a query at most 10 s, each cut to the time left | 15 s for each query alone, or a deadline checked only before each call | three queries of 15 s, or four model calls of 250 s, would pass the service-level objective (SLO, the response-time target of R130) of 20 s; a check before a call does not stop a call that starts at 17.9 s, so each wait gets the time left. httpx and botocore timeouts bound each wait, not the whole call (a trickled reply resets httpx's read timeout), so the question also runs in a worker thread joined with the time left | Generative AI Lens GENREL03-BP02 (timeouts on model calls) |
| The Lambda client built per call, timeouts the time left plus 1 s, one attempt | the default client (60 s read timeout, retries) | the function's own wait on the deadline ends the call; a retried invocation would run the query twice | [botocore Config](https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html) |
| Billed MB: Data scanned rounded up to the MB, at least 10, for a query that succeeded or was cancelled; 0 for one that failed | Data scanned alone | "rounded up to the nearest megabyte, with a 10 MB minimum per query"; "no charges for … failed queries. Canceled queries are billed for the total amount of data scanned" | [Athena pricing](https://aws.amazon.com/athena/pricing/) (read 2026-11-21) |
| The version without the model: a fixed query per kind of question, chosen by code | a comparison written here | `prompt-optimization/ext-eval-matrix` scores it as the cell `no-model/templates` on the same cases | — |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/athena_engine.feature`; those tagged `@live` run in AWS (`LIVE=1`, after
`deploy.py`) and are skipped otherwise. A team keeps 2–5 lines per story.

- **R130** (complex) Where `ext-athena-engine` is used, when the analyst asks a question that the model answers with
  a query, the recipe shall run the query on Athena in the workgroup `<PREFIX>-chat-wg` and return status `answered`
  with the reply, the SQL and its rows, with a response time under 20 s at p95 (the 29th of 30 times, measured by
  `run_athena.py --measure`).
- **R140** (complex) Where `ext-athena-engine` is used, if the SQL in a tool call is anything other than one SELECT
  over the tables `trips` and `zones`, then the recipe shall give the model the rejection as the tool result and not
  send the SQL to Athena.
- **R150** (complex) Where `ext-athena-engine` is used, if a query scans more than 100 MB, then the recipe shall let
  the workgroup cancel it and give the model "the query was stopped at the scan limit of 100 MB" as the tool result.
- **R160** (complex) Where `ext-athena-engine` is used, if the model asks for a query after 3 queries of the same
  question have run, then the recipe shall leave it unrun and give the model "query limit reached" as the tool
  result.
- **R170** (optional) Where `ext-athena-engine` is used, the recipe shall run the chat's queries in the function
  `<PREFIX>-chat-query` as its role `<PREFIX>-chat-reader`, which can start queries only in the workgroup
  `<PREFIX>-chat-wg`, read only the tables `trips` and `zones` and their files, and write only under the
  workgroup's result prefix.
- **R180** (complex) Where `ext-athena-engine` is used, when a question ends, the recipe shall write in its trace
  item the Data scanned of each query, the Data scanned Athena bills (rounded up to the MB, at least 10 MB for a
  query that succeeded or was cancelled, 0 for one that failed), the Athena cost in USD and the model calls' cost in
  CAD.
- **R190** (complex) Where `ext-athena-engine` is used, when the analyst asks how many trips match a zone and a
  time, the recipe shall answer with the rows of a query that gives the count the question asks for, passing the
  eval `trip-counts` (10 cases, each run 3 times: at least 24 of the 30 runs and at least 8 of the 10 cases
  correct).
- **R200** (complex) Where `ext-athena-engine` is used, if the Data scanned billed today in the workgroup
  `<PREFIX>-chat-wg` would pass 5 GB with the next query's 100 MB, then the recipe shall leave the query unrun and
  give the model "daily scan limit reached" as the tool result.
- **R210** (complex) Where `ext-athena-engine` is used, if a question has run for 18 s when its next model call or
  query is about to start, or a model call or a query is still running when the question reaches 18 s, then the
  recipe shall end the wait at 18 s, stop a running query with `StopQueryExecution`, and end the question with
  status `stopped` and reason `deadline`.
- **R220** (complex) Where `ext-athena-engine` is used, when `ext-athena-engine/clean_up.py --yes` runs, the recipe
  shall delete the function, its log group, its role, the usage table, the database `<prefix>_chat` with its
  tables, the prefixes `chat/` and `athena-results/chat-wg/` and the workgroup `<PREFIX>-chat-wg` when the ledger
  records it created, and keep the lake bucket and everything else in it.

## The eval `trip-counts`

- Cases: 10 analyst questions about DS01, each with a reference query whose rows are the correct answer: 6 about one
  zone on one day, 2 about one zone over the month, 2 that compare two zones (`evals/trip_counts/cases.jsonl`). The
  reference rows are computed by running each reference query on the same engine and data as the runs, so no number
  is written into the cases.
- Correct when (checked by code, `trip_counts.py`): the status is `answered`, the rows of the last query that
  succeeded equal the reference rows as a multiset (in any order, the header row dropped, each value cast by its
  column type in `ResultSetMetadata`), and a reply about one number states that number.
- Runs: each case 3 times, because the model's queries vary from run to run. A case passes when it is correct in at
  least 2 of its 3 runs. Passes when at least 24 of the 30 runs and at least 8 of the 10 cases are correct: a
  regression check over 10 cases, not an accuracy estimate.
- Model: `qwen3.6-35b-a3b` through the course LLM gateway. Offline, `run_athena.py --target local --eval` runs the
  same code with a scripted model that writes the reference queries: it checks the harness, not a model.

## Data

DS01 is the High Volume For-Hire Vehicle trip records of the NYC Taxi and Limousine Commission: the Uber trips
(base license `HV0003`) picked up in Manhattan in March 2024, 6,045,943 rows, 0.20 GB as Parquet
<!--src: measured:cp1-example DuckDB 2026-11-19 -->. Table `trips` has the column names of the lake table
`<prefix>_lake.trips`: `trip_id` (DS01 has no trip id, so a hash of all the row's columns and its position in the
file), `trip_state` (`completed` for every DS01 row), `pickup_zone_id` (`PULocationID`), `requested_time`
(`request_datetime`), `driver_id` (null: DS01 has no drivers), `fare` (`base_passenger_fare`), `trip_miles` (which
the lake table does not carry), and the partition column `requested_day`, the day of `requested_time`: 32 days, as 814 trips picked up on
March 1 were requested on February 29 <!--src: measured:live run 2026-11-23 -->. DS02 is the
TLC taxi zone lookup, 265 zones, as table `zones` (`zone_id`, `borough`, `zone_name`, `service_zone`).

SIM01 "three-zone day": from DS01, the trips requested in zones 48, 161 and 230 on 2024-03-14, 19,074 trips: 4,345,
7,661 and 7,068 <!--src: measured:DuckDB over DS01 by request day 2026-11-21 -->, one partition. Offline, SIM01 is a
synthetic table with these counts (`standin.py`), not DS01's rows; `deploy.py --data sim01` loads the real ones.

## Run it

Offline, no account and no token. On your laptop, in the recipe folder `dashboard-chat/` (the one that holds
`main/` and `ext-athena-engine/`):

```bash
uv run python ext-athena-engine/run_athena.py --target local            # two questions, stand-ins, SIM01
uv run python ext-athena-engine/run_athena.py --target local --eval     # the eval's code with a scripted model
uv run pytest -q tests/test_athena_engine.py && uv run behave features/athena_engine.feature
```

The first prints two answered questions with their SQL and rows; the second the eval's summary, every case
passed (a check of the harness, not of a model); the third passes, with the `@live` scenarios skipped.

## Run it live

On your laptop, in `dashboard-chat/`, with your lease's AWS credentials in `ca-central-1`. Run
`aws sts get-caller-identity` first and check that it names your lease's account. Set, in your shell:

```bash
export PREFIX=recipe-yourname          # a name of your own; every resource name starts with it
export COURSE_GATEWAY_URL=YOUR_GATEWAY_URL   # the gateway's address, on the course page or a repository variable of your team repo
export KB_MCP_TOKEN=YOUR_COURSE_TOKEN        # your course MCP token (root README, "Course constraints", item 4); never commit it
```

Leave `CHAT_DATABASE` unset: these commands build and use the recipe's own database. Then, in the order the run
takes them:

```bash
DRY=1 bash ext-athena-engine/run.sh          # prints each command; runs nothing
bash ext-athena-engine/run.sh                # deploy SIM01, the live scenarios, then DS01, measure, eval
uv run python ext-athena-engine/clean_up.py  # what clean-up would delete; deletes nothing (Cost and clean-up)
```

`run.sh` ends with the extension's own clean-up (`clean_up.py --yes`, the resources it deployed); the bare run
afterwards lists what is left. The traces table and the lake bucket are not part of it (Cost and clean-up).

`run.sh` saves what it prints under `output/ext-athena-engine-live.txt` (account masked): the live scenarios' results,
the response times, the eval's summary and, for the R150 query, the reason "Bytes scanned limit was exceeded". What
one run measured, for example (measured, `output/ext-athena-engine-live.txt`, 2026-11-23; your times and counts
will differ, the checks are the requirements' thresholds):

| Check | Result |
|---|---|
| R130, SIM01 in AWS | 19,074 trips loaded; zone 161 on 2024-03-14: 7,661, as offline |
| R130, response time, 30 eval questions after one warm-up | p95 (the 29th of 30) 4.78 s, median 2.79 s; one query and 10 MB billed each |
| R190, eval `trip-counts` on `qwen3.6-35b-a3b` | 30 of 30 runs correct, 10 of 10 cases; the 10 reference queries give the same rows on Athena and in DuckDB over the uploaded files |
| R150, `SELECT * FROM trips` on DS01 (257 MB of Parquet) | `CANCELLED`, "Bytes scanned limit was exceeded", Data scanned 100 MB exactly, answered in 6.5 s |
| R140 in AWS, the six refused statements | 6 rejections, 0 queries in the workgroup |
| R170, the function called directly | `DROP TABLE` and CTAS start, then fail on the role's Glue refusal (reported `DENIED`); workgroup `primary` refused at the start; trips still there |
| R200 in AWS, today's billed MB set in the usage table | at 5,000 MB the query runs; at 5,030 MB "daily scan limit reached" and no query starts |

The live scenarios: R130 (the SIM01 question and the response time on DS01), R150 (every column of the month is
cancelled at 100 MB), R170 (the role refuses DDL and the workgroup `primary`), R190 (the eval on the gateway
model). R170 checks the refusals it lists; passing it does not prove that the policy holds no permission it does not
need.

## Cost and clean-up

Prices: the course price sheet, page `436c-student/prices.md` on the course server ("price sheet, ca-central-1,
2026-11-18"): the AWS rates in USD from its sections Storage and queries, Glue jobs and the Data Catalog, Lambda,
DynamoDB, Monitoring and Data transfer; the gateway's rates in CAD and the team pool from "Models: the course LLM
gateway" (read 2026-11-19). 1 GB = 2^30 bytes, 1 TB = 2^40 bytes, as the code counts them, so 10 MB at 5.50 per TB is
0.0000524 USD. AWS amounts in USD with CAD at 1.45, the course's planning
rate; model calls in CAD; the two are not added. The free tier is left out. Each line is computed from the rates and
the counts; the counts are assumed (200 questions a day, 2 queries each) unless marked measured. The live run's measured
bill is below the table.

| Part | One question | 30 days at 200 questions a day (6,000) | A year (73,000) |
|---|---|---|---|
| Athena, 2 queries billed at the 10 MB minimum each (a one-day query of two or three columns scans under 10 MB: the month with all its columns is 0.20 GB, about 6.6 MB a day) | 20 MB × 5.50 per TB ≈ 0.000105 | 0.63 | 7.66 |
| S3 PUT, 2 result objects a query | 4 × 0.0055 / 1,000 = 0.000022 | 0.13 | 1.61 |
| S3 GET, about 6 a query: data files and results | 12 × 0.00044 / 1,000 ≈ 0.0000053 | 0.032 | 0.39 |
| Glue Data Catalog requests, about 4 a query, 1.00 per million | 0.000008 | 0.048 | 0.58 |
| Lambda `<PREFIX>-chat-query`, 256 MB for about 3 s a query, and 2 requests | 1.5 GB-s × 0.0000133334 + 2 × 0.0000002 ≈ 0.00002 | 0.12 | 1.49 |
| DynamoDB: the daily cap's reserve and settle, 2 write units a query, and the trace item, 2 write units | 6 × 0.6875 / 1,000,000 ≈ 0.0000041 | 0.025 | 0.30 |
| CloudWatch Logs ingestion, about 2 KB a question | 0.000001 | 0.0063 | 0.077 |
| S3 storage, DS01 and 7 days of results (`scripts/lake.py up` gives a bucket it creates a rule that deletes `athena-results/` after 7 days; a bucket it finds is not changed, so check its lifecycle rules, or results stay until deleted), under 0.25 GB | — | under 0.0063 | under 0.075 |
| **AWS total, USD** (CAD at 1.45) | **≈ 0.00017** (0.00024 CAD) | **≈ 1.00** (1.45 CAD) | **≈ 12.2** (17.7 CAD) |
| Model calls, course LLM gateway, CAD: one question of 2 model calls on `qwen3.6-35b-a3b` cost 0.000060 CAD on the shop tables <!--src: receipt:../output/main-gateway.txt -->; the rides schema is about as long (assumed) | 0.000060 CAD | 0.36 CAD | 4.38 CAD |

What the limits let the chat bill: one question at most 3 queries cancelled at about 100 MB each, about 300 MB,
0.0016 USD (0.0023 CAD). The 4 model calls are a call limit, not a cost limit: at the measured tokens they cost
about 0.00012 CAD, and the gateway charges the tokens each call really uses (the trace item records them). All
questions together: the daily cap aims to keep Athena to about 5 GB billed a day, 0.027 USD (0.039 CAD), so about
0.81 USD (1.17 CAD) a month and 9.80 USD (14.2 CAD) a year. That is an estimate, not a ceiling: a cancelled query can
scan a little past its cutoff, which the cap does not bound, and the other lines of the table bill as well. One run of
the eval (30 questions) costs about 0.0050 USD (0.0073 CAD) in AWS and 0.0018 CAD in model calls.

Measured, the live run of 2026-11-23 (`output/ext-athena-engine-live.txt`): 164 queries in the DS01 phase, 158 billed,
1,760 MB, 0.0092 USD, and the SIM01 phase under 0.0011 USD; the eval's 30 questions billed 300 MB (one 10 MB query
each), 0.0016 USD (0.0023 CAD) in Athena and 0.0022 CAD in model calls (0.000072 CAD a question, measured from the
traces' token counts). The whole run: under 0.03 USD (0.04 CAD) in AWS, 0.1% of the 30 USD lease, and 0.0088 CAD in
model calls.

Against the group lease budget of 30 USD, a month of normal questions is about 3.3% and a year about 41%; Athena
alone stays near 2.7% a month under the cap. The model calls of a month are about 5% of a team's gateway pool of
7.25 CAD (price sheet, "Models: the course LLM gateway"; a budget separate from the AWS lease), a year about 60%. A team with larger partitions measures `DataScannedInBytes` of its own questions (the
trace item has it) and multiplies by the rate.

Clean-up, on your laptop in `dashboard-chat/`, with the same `PREFIX` and credentials as the run. Without `--yes`
each command prints what it would delete and deletes nothing; read the list, then run it again with `--yes`:

```bash
uv run python ext-athena-engine/clean_up.py          # the plan
uv run python ext-athena-engine/clean_up.py --yes    # delete (R220)
uv run python clean_up.py                            # dashboard-chat's: the traces table (the plan)
uv run python clean_up.py --yes
uv run python scripts/lake.py down                   # the bucket, last (the plan)
```

Run `uv run python scripts/lake.py down --yes` only when the plan lists the bucket and nothing your project still
needs. It deletes the bucket only when its ledger (`state/ledger.jsonl`) records that it created it, and it refuses
while the bucket holds an object outside `raw/trip-events/` and `athena-results/`, listing those keys; a bucket it
found keeps everything else. If you keep the bucket, its objects keep billing S3 storage.

It deletes the function first (what bills per call), then its log group, the role, the usage table, the workgroup
`<PREFIX>-chat-wg` when the ledger `state/ledger.jsonl` records it created, the tables and database by name (Athena
DDL makes them without tags), and the prefixes `chat/` and `athena-results/chat-wg/`. It keeps the bucket
(`scripts/lake.py down` deletes it when it made it), the shared workgroups `<PREFIX>-wg` and `<PREFIX>-ctas-wg`, and
a database set with `CHAT_DATABASE`.

## The reader role

The policy of `<PREFIX>-chat-reader` (`deploy.py`, `reader_policy`): the Athena and Glue rows follow
[Glue access for Athena](https://docs.aws.amazon.com/athena/latest/ug/fine-grained-access-to-glue-resources.html),
the S3 rows [S3 access from Athena](https://docs.aws.amazon.com/athena/latest/ug/s3-permissions.html) and
[GetQueryResults](https://docs.aws.amazon.com/athena/latest/APIReference/API_GetQueryResults.html), which needs
`s3:GetObject` on the results.

| Service | Actions | Resource |
|---|---|---|
| Athena | `StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`, `StopQueryExecution` | the workgroup `<PREFIX>-chat-wg` |
| Glue | `GetDatabase` | the catalog, `database/default`, `database/<prefix>_chat` |
| Glue | `GetTable`, `GetPartitions` | the catalog, the database, `table/<prefix>_chat/trips` and `zones` (partition actions need all three levels) |
| S3 | `ListBucket`, condition `s3:prefix` in `chat/*` and `athena-results/chat-wg/*` | the bucket |
| S3 | `GetBucketLocation`, in a statement without a condition (`s3:prefix` is a condition key of `ListBucket` only: [S3 condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_s3.html)) | the bucket |
| S3 | `GetObject` | `chat/*` |
| S3 | `GetObject`, `PutObject`, `AbortMultipartUpload` | `athena-results/chat-wg/*` (`ListMultipartUploadParts` is unchecked) |
| DynamoDB | `UpdateItem` | the table `<PREFIX>-chat-usage` |
| CloudWatch Logs | `CreateLogStream`, `PutLogEvents` | its log group |

The chat's own identity (your sign-in, or the role of the app that runs `chat_athena.py`) needs
`lambda:InvokeFunction` on the function `<PREFIX>-chat-query` and `dynamodb:PutItem` on the traces table
`<PREFIX>-chat-traces` (`main/traces.py`), nothing else. The function's role above holds the Athena, Glue, S3 and
usage-table permissions.

## Adapt it

- Your lake's table instead of `<prefix>_chat`: these commands do not configure an existing lake table, and
  `deploy.py` stops when `CHAT_DATABASE` names another database. Using one needs changes the recipe has not run:
  `reader_policy` in `deploy.py` (the Glue rows to your database and table, the S3 `ListBucket` condition and
  `GetObject` row to your table's location), the `ddl` and upload steps left out (your table exists and is
  borrowed, so clean-up must not delete it), and the guard in `up()`. Run the R170 scenarios again after such a
  change.

## Known limits

- The SQL check parses with sqlglot 30.31.0's `athena` dialect. A query it cannot parse is refused, not run; one it
  parses differently from Athena is possible. Refused, checked offline: every R140 statement, `EXPLAIN ANALYZE`,
  `SHOW`, `MSCK`, `INSERT` and qualified names (`tests/test_athena_engine.py`).
- `query_fn.py` drops the first row of a SELECT's results only when it equals the column names, as `GetQueryResults`
  returns them.
- A query the workgroup cancels at its cutoff ends `CANCELLED` with the reason "Bytes scanned limit was exceeded"
  (measured, `output/ext-athena-engine-live.txt`); `query_fn.py` reads the reason in any end state and bills the
  query as cancelled.
