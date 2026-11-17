# Traces: one item per model call or tool call

> ⚠️ Run commands on your local device or in AWS CloudShell. Offline runs use moto; sandbox runs use a DynamoDB table.

This is the trace half of the extension "Trace analysis and alarms". The alarms are in `limits-and-alarms`.
Read the recipe README first. This extension builds on `main/`.

## What it changes in the main scenario

A trace records the model calls and tool calls for a request. Each incoming request gets a `Tracer` (`trace_store.py`). Every model call and tool call in the request handler runs
inside `with tracer.call(kind, name) as c:` and leaves one item in `$PREFIX-llm-traces`. `calls_of(request_id)`
returns all the calls of one request, in the order they started, with one Query.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Unit of a trace (R60) | One item per call, all under the `request_id` of the request that made them | For a slow or failed answer, you can find every call the request handler made and each call's time, tokens and status | L16 (traces); Generative AI Lens [GENOPS02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops02-bp01.html) (monitor every layer of the application) |
| Keys (R60, R63) | Partition key `request_id`, sort key `seq` (1, 2, 3 in the order the calls started) | The access pattern is "this request's calls, in order": one Query (and its next pages), no sort in app code | L12–L13 (keys from access patterns) |
| What a trace keeps (R64) | The item stores kind, name, start, duration, status, error text (at most 200 characters), tokens, cost, a hash of the user id and the conversation id. It does not store the prompt text | These fields are enough to find the slow or failing call. Prompt text can hold personal data and grows the item. The hash is SHA-256 without a salt, an extra value added before hashing. It hides an id that cannot be guessed, such as a Cognito `sub`. It does not hide an email address or a student number, which anyone can hash and compare | AGENTSEC01-BP01; course choice |
| When the trace is written (R61, R62) | The trace is written in a `finally` block, including when the call raises. The error goes on to the caller. A failed trace write is printed and the request goes on | A trace exists for the calls that failed, which are the ones you need; tracing never breaks the answer | Course choice |
| Expiry (R65) | 7 days, TTL on `expires_at` | The table does not grow without limit | [DynamoDB Developer Guide, Using time to live (TTL)](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html) |
| Metrics and alarms | Not here: emit them from the same handler with embedded metric format | Traces record calls in a request. Metrics aggregate measurements to show whether something is wrong now | [`limits-and-alarms`](../../limits-and-alarms/README.md) |

## Requirements

The extension's requirements go on from the recipe's and the answer cache's (in the
[recipe README](../README.md#requirements), with the table of EARS forms). Their scenarios are in
[`features/traces.feature`](../features/traces.feature) and [`features/local_runs.feature`](../features/local_runs.feature).

- **R60** (optional) Where traces are used, the recipe shall write one item per model call or tool call, under the
  request's id, numbered in the order the calls started.
- **R61** (complex) Where traces are used, if a call raises an error, then the recipe shall write its item with
  status `error` and at most 200 characters of the error, and pass the error on to the caller.
- **R62** (complex) Where traces are used, if a trace write fails, then the recipe shall print `trace write failed`
  and let the call's result through.
- **R63** (optional) Where traces are used, the recipe shall return every call of a request, in order, from its
  request id, also when the calls span more than one Query page.
- **R64** (optional) Where traces are used, the recipe shall store no user id and no prompt text in any trace
  item.
- **R65** (optional) Where traces are used, the recipe shall set each item's expiry to 7 days after its call ended.
- **R66** (complex) Where traces are used, when `trace_run.py --target local` runs, the recipe shall print the four
  calls of one request in order, the failed tool call with its error.

## Run it

Read the [recipe README](../README.md#run-it) for where each part runs and how to sign in for the AWS run. From your local device or AWS CloudShell, run the offline command or the sandbox command in
`llm-state-dynamodb`:

```bash
uv run python ext-traces/trace_run.py --target local    # offline, moto
uv run python ext-traces/trace_run.py --target aws      # your sandbox: the table $PREFIX-llm-traces
```

Clean-up and cost: `uv run python clean_up.py` removes the table with the recipe's other tables; one run makes five
DynamoDB requests.

## What you should see

`output/ext-traces.txt` (a run on AWS, 2026-11-14; the request id changes every run):

```
request e5994556c36d464998a366d02d155f79
  1  model  plan           ok     in 420  out 30
  2  tool   search_orders  ok     in 0  out 0
  3  tool   lookup_store   error  in 0  out 0  TimeoutError: store service did not answer in 2 s
  4  model  reply          ok     in 610  out 55
```

The failed tool call has its own item with its error, between the calls before and after it.

## Adapt it

1. Create one `Tracer` per request in your handler, with the signed-in user id and the conversation id.
2. Wrap each model call and tool call in `tracer.call(...)`; set `c.tokens_in`, `c.tokens_out` and `c.cost_cad`
   from the gateway's usage numbers.
3. To find a user's slow requests, add a global secondary index on `user_hash` with `started_at` as its sort key.
