# An agent on Lambda that answers with a number it can show

Use this when your system answers a person's question with an agent built on the Strands Agents SDK or the Pydantic
AI SDK, and one question is one short job: the function gets the question, runs the agent and returns one JSON
answer. The example is an operations analyst who asks about the trips of one day in three pickup zones; the agent
has one tool, `run_sql`, a read-only DuckDB SELECT over that day's trips, which ship inside the function's package.
The answer carries the value only when the code has checked it against what the queries returned, and the SQL and
rows that produced it. Otherwise the status says plainly that the system did not answer, or that it stopped.

`AGENT_SDK` chooses the SDK: `strands` (Strands Agents 1.64.0, with the hooks of the main scenario) or `pydantic-ai`
(Pydantic AI 2.71.0). Both run the same system prompt, the same tool and the same limits behind one function URL.

A **function URL** is an HTTPS address that Lambda gives a function. With auth type `AWS_IAM`, each request must be
signed with **SigV4** (AWS Signature Version 4: the request carries a signature made with the caller's AWS
credentials), and only identities allowed to invoke the function get through.

```
question -> invoke_url.py (SigV4) -> function URL (AWS_IAM) -> Lambda <PREFIX>-agent-sdk
         -> Strands agent with hooks, or the Pydantic AI agent (AGENT_SDK) -> course LLM gateway
         -> run_sql: sqlcheck.py, then DuckDB over trips.parquet (read-only) -> the tool log
         -> the model's TripAnswer -> the value checked against the tool log -> JSON answer with a status
```

## What changes in main/

Nothing: `main/` keeps the library agent on Amazon Bedrock AgentCore Runtime, a service that hosts an agent in a
**session** of its own for each request, which can stay alive while the agent waits for a person. This extension is its own folder; it copies the patterns of
`main/hooks.py` (a cap hook, an argument check), `main/contracts.py` (one pydantic model per tool's arguments),
`main/config.py` (one frozen settings object) and `main/model.py` (the gateway model with the OpenAI client's retries
off), and changes them where the table says.

| Main scenario (AgentCore) | This extension (Lambda) |
|---|---|
| a Runtime session per member request; the agent waits in memory for an approval | one invocation per question; no write tool and no approval |
| `CallCap` checks the deadline before a model call | `caps.CallCap` checks it before a model call and before a tool call, and `asyncio.timeout_at` cancels what is running at the deadline |
| `CallCap` counts every tool call | `caps.CallCap` leaves out the structured-output tool `TripAnswer` |
| at the tool cap the LLM gets the refusal and may reply | at the tool cap the question ends `stopped` |
| a refused model call ends the request with status `error` | it ends with `did_not_answer`, the gateway's status, code and `Retry-After` |
| the reply is text | the reply is a `TripAnswer`; the value is checked in code (`verdict.py`) |
| Strands only | Strands or Pydantic AI (`AGENT_SDK`) |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Hosting (R110) | Lambda behind a function URL | one question is one invocation, billed per millisecond at the arm64 rate, and no session needs to outlive it. Lambda does not promise to keep an execution environment, so an agent waiting for a person's approval cannot wait in memory as it does in a Runtime session: the write tool and the approval stay in the main scenario | [Lambda execution environment lifecycle](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html); L18 |
| Who may call (R170) | function URL with auth type `AWS_IAM`, requests signed with SigV4 by `invoke_url.py` | only identities with `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` reach the function; with auth type `NONE` anyone with the URL spends the team's gateway pool. Signing proves which AWS identity sent the request; it is not a sign-in of the analyst. One URL and one role per calling system keeps the callers named | [Function URL access control](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html); SEC03-BP02 |
| Where the answer's SQL and rows come from (R110, R111) | the tool log: every `run_sql` call with its SQL, status and rows; the value only when a query returned it | the model writes its output, so it can name a query it never ran or a number no query returned (a reply of 19,000 after a query that returned 19,074). The tool log is what ran, and the check needs no second model call | Agentic AI Lens AGENTSEC02-BP02 (validate tool inputs and outputs); L17 |
| A value derived from the results (R111) | the value may also be the row count or a column total of one complete result (not cut at 50 rows), or the sum or difference of two numeric cells of the question's results; `derived` in the answer names which, and `sql` and `rows` are those of the queries it uses | the live run withheld a correct 593 that the model computed from the 7661 and 7068 it had queried, in all 6 runs of the case. Every operand still comes from the tool log, so an invented number is withheld; a wider rule (any arithmetic) would let more coincidences through. After the change the case was answered in 6 of 6 runs, 3 with each SDK <!--src: measured: output/ext-sdk-lambda.txt --> | the rule of the guardrails recipe (row count, column total, sum or difference of two values from a result) |
| The reply beside the value (R113) | the reply must state the value and hold no number of 10 or more that no query result, the question or the SQL holds | the value is checked, but the reply is text the model wrote, so "about 19000 trips" beside a value of 19,074 would contradict it | L17 (check model output in code) |
| The tool (R120) | one SELECT over `trips` in a read-only DuckDB engine, checked first by `sqlcheck.py` (a copy of dashboard-chat's), then by `toollog.reads_table`: the SELECT must read `trips` itself (a subquery counts) | the day's trips fit in the package and DuckDB does not bill per query; Athena with its limits is `dashboard-chat/ext-athena-engine`. A read-only tool does not need an approval. In the live run Pydantic AI sent `SELECT 7661 - 7068 AS difference`: a SELECT of constants put the model's own arithmetic into the tool log, and the value check passed it. A SELECT that reads no table is rejected, and so is an output column made only of numeric constants in any SELECT of the query (`SELECT 593 AS d FROM trips`, `SELECT 161 AS zone, COUNT(*) ...`; the SELECT inside an `EXISTS` aside), and a `VALUES` list. A constant inside an expression that also reads the data (`COUNT(*) + 5`) still passes: the check is that each output column is computed from the data, not that it equals the right answer | dashboard-chat Decisions (read-only SQL, checked in app code) |
| Strands (R110, R140) | 1.64.0, the version the main scenario pins, `retry_strategy=None`, structured output with `structured_output_model=TripAnswer` | structured output in 1.64.0 is a tool named after the class, so `CallCap` leaves it out of the tool count; otherwise a question of 3 queries would be stopped on its output under Strands and answered under Pydantic AI. Its default retry makes 6 attempts with waits of 4 s, 8 s and longer, past the function's timeout | [Strands structured output](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/structured-output/); main scenario Decisions |
| Pydantic AI (R130, R140) | `pydantic-ai-slim[openai]` 2.71.0 with `OpenAIChatModel`, `output_type=TripAnswer`, `UsageLimits(request_limit=4, tool_calls_limit=3)`, `ModelSettings(max_retries=0)`, output retries 1 (`retries={"output": 1}`) | `UsageLimits` checks the request count before each request and the tool count before function tools, not the output tool. `OpenAIChatModel`, not the model string `openai:...`: in 2.71.0 that string builds the Responses model, which calls `/v1/responses`, and the gateway serves `/v1/chat/completions` | [Pydantic AI agents, usage limits](https://ai.pydantic.dev/agents/#usage-limits); [OpenAI-compatible models](https://ai.pydantic.dev/models/openai/) |
| The prompt (R180) | one system prompt for both SDKs (`trip_contracts.py`) that states the rules the live run showed the model does not infer: a time window includes its start and not its end, and one that ends at midnight runs to the end of the day; parentheses around an OR; a number computed from several counts is computed in one SELECT over `trips`; `fare` is the base fare only; another day or a missing column is answered with no value and no query | the first live eval failed with 24 of 36 runs correct and 8 of 12 cases for each SDK: "from 22:00 to midnight" was queried as one hour, `... = 161 AND hour = 12 OR hour = 13` counted another zone, the sum of fares was given as the riders' tips, and a count of 0 over 2024-03-15 as an answer. After two revisions of the prompt: 33 of 36 and 11 of 12 for each SDK; with derived values (R111) as well, 36 of 36 and 12 of 12 <!--src: measured: output/ext-sdk-lambda.txt --> | DS01's own columns (`base_passenger_fare`, `tips`, `tolls` and `sales_tax` apart); L17 |
| Limits (R140) | at most 4 model calls and 3 `run_sql` calls a question; one absolute deadline of 25 s around the whole agent, also checked before each call; 15 s for one model call; function timeout 60 s | a check before a call does not stop a call that starts at 24.9 s; `asyncio.timeout_at` cancels whatever is waiting at the deadline (Python 3.11 and later), so the question ends at about 25 s, and the function's timeout leaves room to return `stopped` instead of being cut off by Lambda | Agentic AI Lens AGENTCOST07-BP01; [asyncio.timeout_at](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout_at) |
| Refusals of the gateway (R150) | no retry, and no wait on `Retry-After`: the answer carries it | the course gateway sends `Retry-After: 60` with `rate_limit_exceeded`, longer than the question's 25 s and equal to the function's 60 s; the caller can ask again after it. A retry on a spent pool (`team_budget_exhausted`) only fails again | llm-gateway recipe, "What you should see" |
| Output that does not validate (R160) | one retry, then `did_not_answer` with `output_format` | a reply in prose or a `TripAnswer` with a wrong field gets one more model call with the reason; a second failure is not shown as an answer | Pydantic AI output retries; Strands forced structured output |
| The token (R110) | a SecureString in Parameter Store, read once per execution environment; the function's settings hold only its name | anyone who can read the function's configuration can read its environment variables | main scenario Decisions, "Secrets"; L18 |
| Package (R110) | built for arm64 with `trips.parquet` inside, without boto3 and botocore (the runtime has them), uploaded through S3 | a zip over 50 MB cannot be uploaded through the Lambda API, and the unzipped package must stay under 250 MB. The built zip, with SIM01 as `trips.parquet` (855,846 bytes), is 44,738,718 bytes (42.7 MiB) zipped and 141,486,071 bytes (134.9 MiB) unzipped <!--src: measured: output/ext-sdk-lambda.txt -->: under both limits, but close to the 50 MB of a direct upload | [Lambda quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html) |
| Parts of the SDKs left out | Strands' agents as tools and session managers; Pydantic AI's graphs and deferred tools | one question, one tool and no conversation do not need them. A team that keeps a conversation reads each SDK's session or message-history pages first | |
| The version without the model | a fixed query per kind of question, chosen by code | `prompt-optimization/ext-eval-matrix` scores it as the cell `no-model/templates` on the same cases and compares it with the agent | |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/ext_sdk_lambda.feature`; `@live` ones run only with `LIVE=1` against a deployed
function.

- **R110** (complex) Where `ext-sdk-lambda` is used, when the analyst sends a question to the function URL, the
  recipe shall return status `answered` with the reply, the value, and the SQL and rows of the `run_sql` call whose
  result holds that value, taken from the tool log, within 30 s at p95 (the time that 95% of the questions finish within) over the eval's
  36 questions. <!--src: assumed -->
- **R111** (complex) Where `ext-sdk-lambda` is used, if the model's value is not a cell of a successful `run_sql`
  result of the question, nor the row count or a column total of one complete result, nor the sum or difference of
  two numeric cells of the question's results (a whole number exactly, any other value within 0.01), then the recipe
  shall return status `did_not_answer` with reason `value_not_in_result` and no value.
- **R112** (complex) Where `ext-sdk-lambda` is used, if the model's output has no value, then the recipe shall return
  status `did_not_answer` with reason `no_value` and the model's reply.
- **R113** (complex) Where `ext-sdk-lambda` is used, if the reply does not state the value, or holds a number of 10
  or more that no query result of the question, the question or the SQL of its `run_sql` calls holds, then the recipe
  shall return status `did_not_answer` with reason `reply_disagrees` and no value.
- **R120** (optional) Where `ext-sdk-lambda` is used, the recipe shall give the agent one tool, `run_sql`, that runs
  one SELECT over table `trips` in a read-only DuckDB engine and returns the rejection as the tool result for any
  other statement, for a SELECT that does not read `trips`, and for a SELECT with an output column made only of
  numeric constants, or with a `VALUES` list.
- **R130** (optional) Where `AGENT_SDK` is `pydantic-ai`, the recipe shall run the same system prompt and tool in a
  Pydantic AI agent whose output is the pydantic model `TripAnswer`.
- **R140** (complex) Where `ext-sdk-lambda` is used, if the next model call would be the 5th of the question, or the
  next `run_sql` call the 4th, or 25 s have passed since the question started, then the recipe shall end the question
  with status `stopped` and the counts of the calls that ran, without starting that call or after cancelling the one
  in flight, with either SDK. <!--src: assumed -->
- **R150** (complex) Where `ext-sdk-lambda` is used, if the course LLM gateway refuses a model call (HTTP 401, 403,
  429 or a 5xx status), then the recipe shall return status `did_not_answer` with the gateway's status, code and
  `Retry-After`, and no value, after one request.
- **R160** (complex) Where `ext-sdk-lambda` is used, if the model's final output does not validate as `TripAnswer`
  after one retry, then the recipe shall return status `did_not_answer` with reason `output_format` and no value,
  with either SDK.
- **R170** (complex) Where `ext-sdk-lambda` is used, if a request to the function URL is not signed with SigV4 by an
  identity allowed to invoke the function, then the recipe shall have Lambda refuse it with HTTP 403 before the
  function runs.
- **R171** (complex) Where `ext-sdk-lambda` is used, when `deploy.py up` runs, the recipe shall create each resource
  with the tags `Project`, `Team` and `Owner` and write one line for it to `state/ledger.jsonl`.
- **R180** (complex) Where `ext-sdk-lambda` is used, when the analyst asks how many trips match a zone and a time of
  the day, the recipe shall answer with the value the question asks for, or say it did not answer when the day's
  trips cannot answer it, with at least 29 of the eval's 36 runs correct and at least 10 of its 12 cases passed for
  each SDK. <!--src: assumed -->
- **R190** (complex) Where `ext-sdk-lambda` is used, when `clean_up.py --yes` runs, the recipe shall delete the
  function, its URL, its log group, its role, its parameter and the objects under `code/agent-sdk/`, and keep the
  lake bucket.

## Evals

**trip-questions** (R180): 12 questions in `evals/trip_questions/cases.jsonl`. 10 have a reference query whose
value is the correct answer (6 about one zone in a time window, 2 that compare zones, one of them needing 3 queries,
2 about the whole day); 2 cannot be answered from the day's trips (a tip, which the table does not have; another
day). Correct when, checked in code: with a reference, the status is `answered`, the value equals the reference (a
whole number exactly, any other within 0.01) and the logged query's rows hold it; without one, the status is
`did_not_answer` with reason `no_value`. Each case runs 3 times with each SDK, because the model's replies vary from
run to run, and passes when it is correct in at least 2 of its 3 runs. The eval passes with at least 29 of the 36
runs correct and at least 10 of the 12 cases passed, for each SDK; the report gives correct answers and correct
refusals apart. It is a regression check over 12 cases, not an accuracy estimate. Model in use: `qwen3.6-35b-a3b`
through the course LLM gateway. The reference values are computed from the reference queries over the same
`trips.parquet` the function ships. Live run, 2026-11-22: 33 of 36 runs and 11 of 12 cases for each SDK (PASS); the
case that failed is the difference of two zones, which the model computed itself from the two counts and the value
check then withheld. With derived values allowed (R111) the eval was run again: 36 of 36 runs and 12 of 12 cases
for each SDK (PASS) <!--src: measured: output/ext-sdk-lambda.txt, FINAL -->.

## Data

`trips.parquet`, one day of trips with the columns of the lake table `trips`: `trip_id`, `trip_state`,
`pickup_zone_id`, `requested_time`, `driver_id`, `fare`, and `trip_miles`, which the lake table does not carry.

- **SIM01, the three-zone day** (`make_trips.py --from-tlc`): from DS01, the TLC High Volume For-Hire Vehicle trip
  records (the Uber trips of March 2024), the trips requested in zones 48, 161 and 230 on 2024-03-14: 19,074 trips,
  4,345, 7,661 and 7,068 by zone, 191 of them in zone 161 between 08:00 and 09:00. <!--src: measured: output/ext-sdk-lambda.txt (make_trips.py --from-tlc) -->
  DS01 has no trip id, so `trip_id` is the MD5 of the row's columns and its position in the file. `driver_id` is
  null (DS01 has no drivers) and `trip_state` is `completed`.
- **The offline stand-in** (`make_trips.py --synthetic`, `build/trips-synthetic.parquet`): the same columns and
  the same counts by zone and for zone 161 at 08:00, from a fixed seed; its fares, miles and the other hours are made
  up. The tests and scenarios run on it, each in a folder of its own; the package and the live eval read only
  `build/trips.parquet`, which `run.sh` builds from DS01.

The model's replies in the offline scenarios are scripted (`tests/sdk_lambda_fakes.py`): a Strands model that plays
the script as a stream of events, and Pydantic AI's `FunctionModel` with the same replies. The refusal scenarios run
the real model classes of both SDKs against a stand-in HTTP server that answers like the gateway.

## Run it

From the recipe folder (`agent-agentcore/`), offline:

```bash
uv sync
uv run pytest -q tests/test_ext_sdk_lambda.py tests/test_ext_sdk_lambda_parts.py
uv run behave features/ext_sdk_lambda.feature
uv run python ext-sdk-lambda/make_trips.py --synthetic --out build/trips-synthetic.parquet
uv run --env-file ext-sdk-lambda/.env.example python ext-sdk-lambda/trip_eval.py --backend scripted --trips build/trips-synthetic.parquet
```

`pytest` and `behave` pass, with the `@live` scenarios skipped; `make_trips.py` writes
`build/trips-synthetic.parquet`. The last line runs the eval with a stand-in model that sends each reference query
and states its value: it checks the scoring and the plumbing, never the model. Fix an offline failure before you
deploy.

## Run it live

On your laptop, in the recipe folder (`agent-agentcore/`), with your lease's AWS credentials in `ca-central-1`.
Run `aws sts get-caller-identity` first and check that it names your lease's account. Copy
`ext-sdk-lambda/.env.example` to `ext-sdk-lambda/.env` and fill in:

- `COURSE_GATEWAY_URL`: the gateway's address, on the course page or as a repository variable of your team repo;
- `KB_MCP_TOKEN`: your course MCP token, the key for the gateway (root README, "Course constraints", item 4). It is
  not an AWS key. `.env` stays out of git;
- `PREFIX`: a name of your own, such as `recipe-yourname`, that no teammate uses in the same account: every resource
  name starts with it, and clean-up finds what to delete by it.

Then:

```bash
bash ext-sdk-lambda/run.sh               # SIM01, the package, the deploy; prints the URL, a signed answer and a 403 for the unsigned request
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/invoke_url.py "How many trips were requested in zone 161 between 08:00 and 09:00?"   # HTTP 200 and the JSON answer
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/invoke_url.py --measure    # 36 questions; prints the p95
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/trip_eval.py --backend gateway   # one line per SDK, PASS or FAIL
LIVE=1 uv run --env-file ext-sdk-lambda/.env behave features/ext_sdk_lambda.feature --tags=@live   # the live scenarios pass
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py          # lists what it would delete; deletes nothing
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py --yes    # only after reading the list
```

`run.sh` downloads the month's DS01 file (about 0.5 GB) into `build/`, builds SIM01 and checks its counts, builds the
package (`deploy.py package`: the pinned libraries for Python 3.12 on arm64, the function's files and
`trips.parquet`), and deploys it (`deploy.py up`). `DRY=1 bash ext-sdk-lambda/run.sh` prints the commands without
running them. `trip_eval.py --backend gateway` runs the agent on your laptop against the gateway, both SDKs, 72
questions; `invoke_url.py --measure` sends the 12 questions 3 times to the deployed function and prints the p95.

One live run, for example (measured, `output/ext-sdk-lambda.txt`, 2026-11-22, both SDKs, `REPLY_MAX_TOKENS`
16,384; your times will differ): the first question of a new execution environment took 9.5 to 10.5 s from the
request to the reply (7.2 to 8.2 s of it inside the function: importing the SDK, reading the parameter and the first
connection to the gateway; Init Duration 0.9 to 1.4 s), a warm question 0.9 to 6.4 s; p95 of the 36 warm questions
3.5 s with Strands and 6.4 s with Pydantic AI, under the 30 s target; an unsigned request got 403 in 0.01 s
<!--src: measured: output/ext-sdk-lambda.txt -->. The first question's setup counts toward its 25 s deadline. The
scenarios that need the deployed function are tagged `@live`; with `LIVE=1` they run on your shell's settings.

### AWS permissions

| Part | Actions of your sign-in |
|---|---|
| deploy (`deploy.py up`) | `sts:GetCallerIdentity`; `s3:CreateBucket`, `PutBucketPublicAccessBlock`, `PutBucketLifecycleConfiguration`, `PutObject` (lake bucket, `scripts/lake.py up`); `s3:TagResource` or `PutBucketTagging` (the bucket's tags); `iam:CreateRole`, `TagRole`, `GetRole`, `PutRolePolicy`, `PassRole`; `logs:CreateLogGroup`, `TagResource` (the log group's tags), `PutRetentionPolicy`; `ssm:PutParameter`, `GetParameter`, `AddTagsToResource`; `lambda:CreateFunction`, `GetFunction`, `GetFunctionConfiguration`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `TagResource`, `CreateFunctionUrlConfig`, `GetFunctionUrlConfig` |
| invoke (`invoke_url.py`) | `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (the function URL checks both), `lambda:GetFunctionUrlConfig` |
| clean-up (`clean_up.py --yes`) | `lambda:DeleteFunctionUrlConfig`, `DeleteFunction`; `logs:DeleteLogGroup`; `ssm:DescribeParameters`, `DeleteParameter`; `iam:ListRolePolicies`, `DeleteRolePolicy`, `DeleteRole`; `s3:ListBucket`, `DeleteObject` |

The function's own role, `$PREFIX-agent-sdk-role`, may write to its log group and read its one parameter: in the
live run it read its SecureString under the AWS managed key `aws/ssm` with `ssm:GetParameter` alone, no
`kms:Decrypt` grant, and the deploying identity (same account) reached the `AWS_IAM` URL with no resource-based
policy on the function.

If your lease refuses one of these actions, `deploy.py up` stops at that step with `AccessDenied` and the action's
name. Run the clean-up (bare, then `--yes`) to remove what it made so far, and do not switch to another account.

- Refused `lambda:CreateFunctionUrlConfig` or `lambda:InvokeFunctionUrl`: the function URL is not available to you;
  run the agent on your laptop instead with `trip_eval.py --backend gateway`, which calls the gateway directly and
  needs no AWS resources.
- Refused `ssm:PutParameter` or `ssm:GetParameter` on `$PREFIX-agent-sdk-token`: the deploy has no other place for
  the token. Do not put it in a function environment variable: anyone who can read the function's configuration can
  read those, and `deploy.py` is not built for it. Ask the course staff for Parameter Store access on that name, or
  run the agent on your laptop as above. `aws ssm describe-parameters --max-results 1` is a read-only probe of
  whether your sign-in reaches Parameter Store at all; it does not show whether writes are allowed.

## What you should see

```
$ uv run --env-file ext-sdk-lambda/.env.example python ext-sdk-lambda/trip_eval.py --backend scripted --trips build/trips-synthetic.parquet
strands      runs correct 36/36 (answers 30, refusals 6); cases passed 12/12; PASS (needs 29 runs and 10 cases)
pydantic-ai  runs correct 36/36 (answers 30, refusals 6); cases passed 12/12; PASS (needs 29 runs and 10 cases)
```

With the stand-in model every run is correct; with the model on the gateway, runs vary, and the eval passes or fails
on the thresholds above (at least 29 of 36 runs and 10 of 12 cases for each SDK). One live run's eval, for example
(`--backend gateway`, abridged; measured, `output/ext-sdk-lambda.txt`; your counts and tokens will differ):

```
strands      runs correct 36/36 (answers 30, refusals 6); cases passed 12/12; PASS (needs 29 runs and 10 cases)
  model calls 81, tokens in 89,154, out 5,904: 0.005048 CAD at the rates, at most 0.005210 CAD with the gateway's rounding
pydantic-ai  runs correct 36/36 (answers 30, refusals 6); cases passed 12/12; PASS (needs 29 runs and 10 cases)
  model calls 72, tokens in 72,471, out 4,881: 0.004112 CAD at the rates, at most 0.004256 CAD with the gateway's rounding
```

An answer from the deployed function (`invoke_url.py`, one live run, for example):

```json
HTTP 200 in 1.52 s
{"status": "answered", "reason": null,
 "reply": "There were 191 trips requested in zone 161 between 08:00 and 09:00.", "value": 191, "derived": null,
 "sql": "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 AND hour(requested_time) = 8",
 "rows": [{"count_star()": 191}], "model_calls": 2, "tool_calls": 1, "http_status": null, "code": null,
 "retry_after": null, "sdk": "strands", "seconds": 1.477}
```

Strands prints `overriding stop reason due to toolUse blocks in response` to stderr on some questions: its own
warning when the model ends a turn with a tool call, not an error.

`seconds`, the reply's wording and the SQL change between runs. When `derived` is null, `value` equals a cell of
`rows`; when it is set, `value` is the row count, the column total, or the sum or difference of two cells that it
names (R111). `model_calls` is at most 4 and `tool_calls` at most 3. A refusal of the gateway looks like
`{"status": "did_not_answer", "reason": "gateway_refused", "http_status": 429, "code": "rate_limit_exceeded",
"retry_after": 60, "value": null, ...}`.

## Cost and clean-up

Prices: the course price sheet, page `436c-student/prices.md` on the course server ("price sheet, ca-central-1,
2026-11-18"): the AWS rates in USD from its Lambda, Monitoring and Data transfer sections, the gateway's rates in
CAD and the team pool from its section "Models: the course LLM gateway". Lambda's GB-second counts memory in GB of
1,024 MB, so 1,024 MB is 1 GB-second per second. 1 GB = 2^30 bytes. Each line is computed from the rates and the
counts measured in the live run (`output/ext-sdk-lambda.txt`, 200 invocations) unless marked assumed. CAD at 1.45,
the course's planning rate. The free tier is left out. Model calls are billed by the gateway in CAD; the AWS lines
are in USD.

| Part | One question | 30 days at 200 questions a day (6,000) | A year (73,000) |
|---|---|---|---|
| Lambda duration, 1,024 MB, billed 2.3 s for a warm question (median; the middle half of the questions, the interquartile range or IQR, 1.5 to 3.0 s; 8.1 to 10.1 s for the first one of an execution environment, its init included), mostly waiting for the gateway, at 0.0000133334 USD per GB-s (arm64) <!--src: measured: output/ext-sdk-lambda.txt, REPORT lines --> | 0.000031 USD | 0.18 USD | 2.24 USD |
| Lambda requests, 0.20 USD per million | 0.0000002 USD | 0.0012 USD | 0.015 USD |
| CloudWatch Logs ingestion, about 1 KB a question (207,614 bytes for 200), 0.55 USD per GB (storage at 7 days' retention under 0.0001 USD a month) <!--src: measured: output/ext-sdk-lambda.txt --> | 0.0000005 USD | 0.0032 USD | 0.039 USD |
| KMS: one decrypt of the SecureString per execution environment, 0.03 USD per 10,000; at most one a question | at most 0.000003 USD | at most 0.018 USD | at most 0.22 USD |
| Data out, about 1 KB a reply, 0.09 USD per GB <!--src: assumed --> | 0.0000001 USD | 0.0005 USD | 0.0063 USD |
| S3 storage of the package, 42.7 MiB, 0.025 USD per GB-month | | 0.0010 USD | 0.012 USD |
| **AWS total** | **0.000035 USD (0.00005 CAD)** | **0.21 USD (0.30 CAD)** | **2.53 USD (3.7 CAD)** |
| Model calls on `qwen3.6-35b-a3b`: with Strands 2.3 calls, 2,568 tokens in and 167 out a question; with Pydantic AI 2.0 calls, 2,012 in and 135 out; at 0.05 and 0.10 CAD per million, each call's two parts rounded up to 0.000001 CAD <!--src: measured: output/ext-sdk-lambda.txt, 78 questions on the function with each SDK --> | about 0.00015 CAD (0.00010 USD) with Strands, 0.00012 CAD with Pydantic AI (averages, not a cap) | 0.90 CAD (0.62 USD) with Strands, 0.71 CAD with Pydantic AI | 10.95 CAD (7.55 USD) with Strands, 8.6 CAD with Pydantic AI |

The 200 questions a day are assumed. Against the 30 USD lease: a month of AWS is about 0.7%, a year about 8.4%.
Against a team's gateway pool of 7.25 CAD (price sheet, "Models: the course LLM gateway"; a separate budget from
the AWS lease): a month of model calls with Strands is about 12%, a year about 151%, so a year of questions needs the
production budget the price sheet says to ask the course staff for. One run of the eval
(72 questions) cost at most 0.0095 CAD in model calls on the laptop, and would add 0.0025 USD (0.0036 CAD) in AWS on
the function; the caps bound the calls, not the tokens: a question stopped by the caps makes at most 4 model calls, about
0.0003 CAD at the measured average tokens a call, and the most 4 calls can cost is about 0.0066 CAD of output at
16,384 tokens each, plus their input. A call cancelled at the deadline may be charged its whole reserve, up to
about 0.0016 CAD at 16,384 tokens (llm-gateway recipe, Cost; not measured here). The whole live run (200
invocations, 448 questions with the evals and the diagnosis runs) cost about 0.0075 USD in AWS and about 0.063 CAD in
model calls. The cost grows with the time the function waits for the model: read the billed duration of your own
questions in the function's log (its REPORT lines; the JSON line of each question has `tokens_in` and `tokens_out`)
and multiply by the rates.

Remove what the deploy made (the function, its URL, its log group, its role, its parameter and the package under
`code/agent-sdk/` of your `$PREFIX` lake bucket, which only this deploy writes). On your laptop, in
`agent-agentcore/`:

```bash
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py          # what it would delete
uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/clean_up.py --yes    # delete it
```

Read the list first; every name in it starts with your `PREFIX`. Deleting the function stops its Lambda, log and
KMS charges, and the package's storage goes with its objects. The lake bucket stays, with whatever else is in it,
and keeps billing S3 storage for its objects: it may hold your project's data or another recipe's. Remove it only
when nothing in it is needed, with the lake's own clean-up, `uv run python scripts/lake.py down` (it lists; `--yes`
deletes, and only a bucket that its ledger, `state/ledger.jsonl`, records it created).

## Adapt it

- Your data: replace `trips.parquet` (`make_trips.py`), the table name in `engine.py` and the schema lines of
  `SYSTEM_PROMPT` in `trip_contracts.py`. A day of data that does not fit in the package goes to Athena instead:
  take the tool from `dashboard-chat/ext-athena-engine`.
- Your output: change `TripAnswer` and `verdict.py` together; keep the rule that a value is shown only when the tool
  log holds it.
- Your limits: `MAX_MODEL_CALLS`, `MAX_TOOL_CALLS`, `DEADLINE_S`, `CALL_TIMEOUT_S` in `.env.example`; keep the
  function's timeout above the deadline.
- One SDK only: drop the other loop file and its pin; the handler, the tool and the checks stay.
- The unzipped package, libraries and data together, must stay under 250 MB (Lambda quotas). If the two SDKs'
  libraries push it over, keep one SDK per function. If the data does, splitting the SDKs does not help: read the
  data from S3 or query it with Athena (the tool of `dashboard-chat/ext-athena-engine`).
