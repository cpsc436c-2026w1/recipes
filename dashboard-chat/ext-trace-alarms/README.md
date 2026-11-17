# Trace analysis and alarms

This recipe serves the extension "Trace analysis and alarms". It builds on `main/`. Read the recipe README first.

## What it changes in the main scenario

Step 7 writes spans, records of requests and their calls, and metrics. It creates alarms and analyzes spans:

1. `spans.py` turns one answer into linked spans, records of the request and its calls, in the DynamoDB table `$PREFIX-chat-spans`: the request span
   `0000`, then one span per model call and per tool call (`0001`, `0002`, ...), each with `parent_id = 0000`,
   its start, duration, status, tokens and cost. One Query by `trace_id` returns the whole request in order of
   `span_id`.
2. `alarms.py` sends four metrics per request to CloudWatch (namespace `$PREFIX/dashboard-chat`): Requests,
   Errors, LatencyMs, CostCAD.
3. `alarms.py` creates three alarms that notify the Amazon Simple Notification Service (SNS) topic `$PREFIX-chat-alarms`.
4. `analyze.py` loads the spans into DuckDB and answers the questions you ask when the chat is slow or expensive.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Span table key (R100) | Partition key `trace_id` (the request id), sort key `span_id` | One request is one partition: a single Query reads every call in order | L12–L13 (DynamoDB keys); GENOPS03-BP02 (tracing for agents and RAG) |
| What a span holds (R100) | Kind, name, start and duration in ms, status, tokens, cost; the SQL for a tool call | The time and money recorded per call show which calls contribute to a slow or costly answer | GENOPS02-BP02 (monitor model metrics); AI Engineering ch. 10 (monitoring and observability) |
| Errors alarm (R102) | 3 or more failed questions in 5 minutes (assumed threshold) | One failure is noise; several within five minutes point to an outage of the gateway or the app | GENOPS02-BP01 (monitor all application layers) |
| Slow answers alarm (R102) | 90th percentile of 15 s or more in 3 periods of 5 minutes running (assumed threshold) | A percentile shows the slow answers that an average hides | GENOPS02-BP02; L16 (latency) |
| Daily cost alarm (R102) | 0.25 CAD or more in 24 hours (assumed threshold) | The alarm can notify you about a runaway loop within the same day | Course constraint (per-key budget, sandbox budgets) |
| No traffic (R102) | `TreatMissingData = notBreaching` | A period with no data counts as within the threshold | Course choice |
| Analysis (R104) | Spans into DuckDB, SQL over them | The course's tool for questions over records; a Scan is fine for a demo's few hundred items | L15; course choice |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/trace_alarms.feature`.

- **R100** (optional) Where `ext-trace-alarms` is used, the recipe shall write, for each question, one request span
  `0000` and one span per model call and per tool call with `parent_id` `0000`.
- **R101** (optional) Where `ext-trace-alarms` is used with `--target aws`, the recipe shall send four metrics per
  question (`Requests`, `Errors`, `LatencyMs`, `CostCAD`) to the CloudWatch namespace `$PREFIX/dashboard-chat`.
- **R102** (optional) Where `ext-trace-alarms` is used with `--target aws`, the recipe shall create three alarms
  that notify the SNS topic `$PREFIX-chat-alarms`: 3 or more errors in 5 minutes, a 90th percentile answer time of
  15 s or more in 3 periods of 5 minutes, 0.25 CAD or more in 24 hours.
- **R103** (complex) Where `ext-trace-alarms` is used, if a span write or a metric call fails, then the recipe shall
  print the failure and return the answer as it is.
- **R104** (complex) Where `ext-trace-alarms` is used, when `analyze.py` runs, the recipe shall print four tables
  from the spans: per request, time by kind of call, calls per request, slowest requests.

## Run it

```bash
uv run python ext-trace-alarms/trace_run.py --target local --model stub     # spans in output/spans.jsonl
uv run python ext-trace-alarms/analyze.py
PREFIX=recipe-yourname uv run python ext-trace-alarms/trace_run.py --target aws --email you@example.com
PREFIX=recipe-yourname uv run python ext-trace-alarms/analyze.py --target aws
```

SNS sends a confirmation link to the email address; alarms reach you only after you click it.

## What you should see

Check `output/trace-stub.txt` for one line per request with its number of calls, then four tables. In the stub run,
in the stub run: an answered question has 3 calls (model, tool, model), and a question the model declines has 1. The
`calls per request` table counts both. In the stub run, the tool call (DuckDB) took most of the time. With the
gateway, the model calls do, which is the first thing the table shows you: in a real run with `--target aws`
(2026-11-12, [`output/trace-aws.txt`](../output/trace-aws.txt)), model calls took 99.3% of the time.

```
time by kind of call
│ kind       │ calls │ total_ms │ pct  │
│ tool_call  │     5 │     ...  │ ...  │
│ model_call │    11 │     ...  │ ...  │
```

## Cost

Three alarms at 0.10 USD per alarm-month and four custom metrics at 0.30 USD per metric-month; the same price list
gives an account its first 10 alarms and first 10 custom metrics of a month at no charge (AWS bulk price list,
ca-central-1).
That a short demo pays only a part of a month for each is assumed, not read in the price list.
`clean_up.py` deletes the alarms and the topic. A CloudWatch metric cannot be deleted. It expires 15 months after
its last data point,
so stop sending data when you are done.

## Adapt it

Set the thresholds in `THRESHOLDS` (`alarms.py`) from a week of your own traces: run `analyze.py`, read the p90, and
put the alarm above it. Add a span kind for any other call your app makes (a retrieval, a second model).
