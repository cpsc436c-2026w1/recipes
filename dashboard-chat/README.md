# Chat on the dashboard: questions in plain words, answers from your table

## What you build

A user types a question about your team's table ("What were total sales in Vancouver?") and gets a one-sentence
reply with the query's result rows. The model writes one read-only Structured Query Language (SQL) query through one tool, your app code checks the
query and runs it in DuckDB, and every question leaves one trace item in DynamoDB.

## Use it when

- Your project has any of the three shapes (lakehouse first, operational first, generative AI first) and its users
  ask questions about a table: the recipe needs only a table it can load into DuckDB.
- You chose the extension "Chat on the dashboard" (2 points): `main/` is the chat, and
  [`ext-shiny-app/`](ext-shiny-app/README.md) is the same chat as a web page (Shiny Express with querychat). The
  other extensions here serve "Answer or retrieval cache", "Guardrails with a red-team log" and "Trace analysis and
  alarms" (1 point each).
- Your users ask about numbers in a table you already have (Parquet in S3 or on disk, a DuckDB file).
- Questions over documents, product descriptions or other free text need retrieval instead: see
  [`hybrid-retrieval-eval`](../hybrid-retrieval-eval/README.md).
- Lectures: L15 (an LLM call, tool calling, the guarded text-to-SQL runner, traces) and L16 (the course LLM gateway,
  per-key budgets, rate limits).

## Main scenario

1. `main/data.py` writes the shop's two tables as Parquet (`data/orders.parquet`, `data/stores.parquet`).
2. `main/engine.py` copies them into an in-memory DuckDB, then switches off file and network access and locks the
   settings. Every later query runs as one statement in a read-only transaction.
3. `main/chat.py` sends the system prompt (schema, rules, the asking customer's id) and the user message (the
   question) through the course LLM gateway (`main/model.py`).
4. The LLM returns a tool call `run_sql` with one query. `main/sqlcheck.py` parses it and lets it through only if
   it is one SELECT over `orders` and `stores`, with no table functions; otherwise the rejection goes back to the
   LLM as the tool result.
5. DuckDB runs the query for at most 5 seconds. At most 50 rows, with text values cut at 200 characters, reach the LLM
   as the tool result and the dashboard as the table.
6. The LLM returns a reply. The system prompt asks it to answer from the rows. App code stops the loop after at most
   4 model calls; a query the LLM asks for in the 4th is not run, and the result then does not show a table. The app code does not check that the reply quotes the rows correctly. Compare the reply with `rows`.
7. `main/traces.py` writes one item per question: question, SQL, status, every step with its time, tokens and cost,
   a hash of the user id, an expiry date. A run with `--target aws` writes it to the DynamoDB table
   `$PREFIX-chat-traces`; a local run writes it to `output/traces.jsonl`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Where the number comes from (R10, R11) | The LLM writes SQL; DuckDB computes the value; the reply quotes the rows | An LLM that types a number can make it up. A query can be read, checked and rerun | L15, guarded text-to-SQL; Generative AI Lens GENSEC02-BP01 (validate responses) |
| What the LLM may do (R20, R23) | One tool, `run_sql`, read-only; no tool that writes | The smallest set of actions that answers the question | Generative AI Lens GENSEC05-BP01 (least privilege for agentic workflows); Agentic AI Lens AGENTREL02-BP02 |
| Checking the SQL (R20, R22) | Parse with sqlglot (a SQL parser for Python) in app code: one statement, a query, known tables only, no file or environment functions | Rules over the parsed query do not miss a write hidden in odd spelling and do not refuse a column called `updated` | L15; Agentic AI Lens AGENTSEC02-BP02 (validate tool inputs: the SQL is the input of the tool) |
| Second layer (R21) | DuckDB holds copies of the tables in memory with external access off and settings locked, and runs each query as one statement in a read-only transaction | A check that has a gap still cannot read a file, change a setting or change the tables: DuckDB refuses the write itself | L15 (read-only engine); defence in depth, GENSEC01-BP03 (least privilege on data stores); GENSEC05-BP01 (a boundary the agent cannot widen) |
| Who may see whose rows (R91) | Main puts the asking customer's id in the system prompt and does not enforce row access. `ext-guardrails-redteam` enforces it: its queries run in an engine that holds only the asking customer's rows | A system prompt guides the LLM; it does not stop a user who asks it to ignore the rule, and a rule over the SQL text misses the query nobody thought of. Do not give main to users who may see only their own rows | L15 (guardrails); GENSEC05-BP01; Agentic AI Lens AGENTREL02-BP02 |
| Bounded work per question (R13, R15, R16, R30, R31, R33, R34) | At most 4 model calls, 400 output tokens (600 for a model that reasons first, next row) and 250 seconds per call (the gateway itself waits up to 240 s for a thinking model), 50 rows, 200 characters per text value and 5 seconds per query (course default); a result that was cut says what was cut, for the LLM and in the trace | A loop that never ends spends the key's budget. Long tool results add input tokens on every later call. A table with a long text column makes tool results long. A result cut without a mark is read as the whole result. A query that joins every row with every row holds the dashboard until someone stops the app. With these limits a question ends within about 4 × 255 seconds | GENCOST05-BP01 (stopping conditions); GENREL03-BP02 (timeouts on agentic workflows); GENCOST03-BP01 and BP02 (prompt and response length); Agentic AI Lens AGENTSEC02-BP02 (size limits on tool outputs, each truncation logged) |
| Reasoning tokens and replies stopped at the token limit (R35, R36) | 600 output tokens per call for `qwen3.8-27b`; a reply that stops at `finish_reason` `length` with no text and no tool call, or with a tool call whose arguments are cut, ends the question with status `error` and "the model used its token cap before answering" | `qwen3.8-27b` reasons before it answers, and the reasoning counts against `max_tokens`. With a cap of 150 its replies came back empty; with 600, one answer of two sentences took 330 completion tokens, 277 of them reasoning. 600 is a margin over the most we measured, and it was enough for this recipe's six questions: on the gateway (2026-11-14, `output/main-gateway-qwen3.8-27b.txt`) the model answered the four questions about the tables and declined the other two, in 10 model calls of at most 162 completion tokens each, 847 in all, 0.000729 CAD by the gateway's count (the recipe's estimate printed 0.000719). These calls would also have fit in 400. An empty reply is not the model's answer and not a refusal, so it is neither shown as one nor counted as `no_answer` | [GENCOST03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp02.html) (control response length); GENREL03-BP01 (retry or fail gracefully) |
| Model calls (R40, R43, R44) | Through the course LLM gateway with `KB_MCP_TOKEN` from the environment; never a provider key | One key per student with its own budget and rate limit; nothing secret in the repo | Course constraint; L16 (model gateway); GENSEC01-BP01; [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), Step 3. Add Model Router and Gateway (the book's term is model gateway) |
| Default model (R32) | `qwen3.6-35b-a3b`, the cheaper of the gateway's two chat models (`CHAT_MODEL` changes it) | It answered the recipe's six questions (`output/main-gateway.txt`), and every call is charged to your key's budget. Check it on your own questions before you rely on it, and move to the stronger model when it fails them | GENCOST01-BP01 (right-size the model); [AI Engineering ch. 4](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch04.html), Model Selection: judge models on your own criteria, then weigh cost against performance. The book starts prompt work from the strongest model and works back to smaller ones; starting from the cheaper one is a course choice |
| Temperature (R31) | 0 | The same question gives the same SQL more often, which makes traces and evals comparable | Runs you can compare: an eval reruns the same questions (course default) |
| Traces (R50, R51, R52, R53, R54, R55) | One DynamoDB item per question, on-demand, user id hashed, question, SQL and the first 5 rows kept, an expiry time 7 days ahead (course default) for DynamoDB time to live (TTL) | You debug a wrong answer from its question and SQL. TTL deletes an expired item within a few days of its expiry time, without a write charge; until then it is stored and a read still returns it ([DynamoDB TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html)). On demand, a table with no requests is not charged for throughput; its stored items still are ([on-demand mode](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/on-demand-capacity-mode.html)) | L15–L16 (traces in DynamoDB); GENOPS03-BP02 (tracing); course constraint (sandbox budget) |
| Failed call (R41, R42, R95) | The question ends with status `error` and a trace; the app goes on; no retry in the chat (the gateway's 429 says why: `rate_limit_exceeded` or `budget_exhausted`). `main/run.py` prints the reason in the line's `error` field and exits with code 1 when a question ended in error. The batch run of `ext-guardrails-redteam` waits once for `Retry-After` on a rate-limit refusal | A retry on a spent budget only fails again, and a person at the dashboard should see a refusal at once. A batch run has nobody waiting, and a case that ends in a rate-limit refusal was not tested | L16 (budget and rate errors); [`llm-gateway`](../llm-gateway/README.md); GENREL03-BP01 (retry or fail gracefully) |
| Tests (R62) | A stub model with fixed replies (`main/stub_replies.json`) and moto (a local stand-in for AWS) for DynamoDB | Tests pass offline, with no key and no account | Offline tests after a clone (course constraint) |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets
after a requirement's number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an extension is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. The requirements of an extension are in its own README, and their numbers go on from here:
[answer cache](ext-answer-cache/README.md#requirements) from 80,
[guardrails](ext-guardrails-redteam/README.md#requirements) from 90,
[trace analysis and alarms](ext-trace-alarms/README.md#requirements) from 100,
[a user's verdict](ext-feedback/README.md#requirements) from 110,
[the web page](ext-shiny-app/README.md#requirements) from 120; the list at the end of this section has each range.
When you change what the recipe does, change the requirement and its scenario with it:
`python3 tools/check_requirements.py` (repo root) fails when they do not match.

**Main scenario**

- **R10** (ubiquitous) The recipe shall take every value in a result's `rows` from a DuckDB query over the team's
  tables.
- **R11** (event) When a user asks a question that the LLM answers with a query, the recipe shall return status
  `answered` with the reply, the SQL that ran and its rows.
- **R12** (event) When the LLM replies without a query, the recipe shall return status `no_answer` with that reply
  and empty `rows`.
- **R13** (ubiquitous) The recipe shall pass at most 50 rows of a query's result to the LLM and to the dashboard.
- **R14** (event) When a run of `main/run.py` ends, the recipe shall print the total cost of the run's model calls
  in CAD and where its traces are.
- **R15** (ubiquitous) The recipe shall pass at most 200 characters of a text value in a query's result to the LLM
  and to the dashboard.
- **R16** (event) When a query's result is cut to the row limit or the character limit, the recipe shall say which
  (`rows`, `text`) in `truncated` of the tool result and of the step in the trace item.

**SQL from the LLM**

- **R20** (unwanted) If the SQL in a tool call is anything other than one SELECT over the team's tables (a write, a
  second statement, a file or environment function, another table), then the recipe shall give the LLM the
  rejection as the tool result and leave the SQL unrun.
- **R21** (ubiquitous) The recipe shall raise an error for a statement that reads a file, changes a setting or
  writes to a table, also for one the SQL check did not stop.
- **R22** (unwanted) If DuckDB raises an error on a query that passed the check, then the recipe shall give the LLM
  the first line of the error as the tool result.
- **R23** (unwanted) If the LLM calls a tool other than `run_sql`, then the recipe shall give the LLM `unknown tool`
  as the tool result.

**Bounded work per question**

- **R30** (unwanted) If the LLM asks for the tool in the 4th model call of a question, then the recipe shall end
  the question with status `no_answer`, the reply "I could not answer within the step limit." and empty `sql` and
  `rows`, and leave the SQL of that call unrun.
- **R31** (ubiquitous) The recipe shall send `temperature` 0 with every request to the gateway, and `max_tokens` 400
  for every other model (R35 names `qwen3.8-27b`).
- **R32** (ubiquitous) The recipe shall ask the gateway for the model `qwen3.6-35b-a3b` unless `CHAT_MODEL` names
  another.
- **R33** (unwanted) If the gateway does not answer a model call within 250 seconds, then the recipe shall end the
  question with status `error`.
- **R34** (unwanted) If a query runs longer than 5 seconds, then the recipe shall stop it and give the LLM the time
  limit as the tool result.
- **R35** (optional) Where `CHAT_MODEL` is `qwen3.8-27b`, the recipe shall send `max_tokens` 600 with every
  request to the gateway.
- **R36** (unwanted) If a reply of the gateway stops at `finish_reason` `length` with no text and no tool call, or
  with a tool call whose arguments do not parse, then the recipe shall run no query and end the question with status `error`, "the model used its token cap before answering" in `error`,
  an empty reply and empty `rows`.

**Model calls**

- **R40** (ubiquitous) The recipe shall send every model call of a gateway run to `$COURSE_GATEWAY_URL/chat/completions`
  with `KB_MCP_TOKEN` as the bearer key.
- **R41** (unwanted) If a model call fails (the gateway answers 401, 403, 429 or a 5xx status), then the recipe
  shall end the question with status `error` and the reason in `error`, without a second attempt.
- **R42** (unwanted) If one or more questions of a run of `main/run.py` end with status `error`, then the recipe
  shall exit with code 1 after it has asked the remaining questions.
- **R43** (unwanted) If `COURSE_GATEWAY_URL` or `KB_MCP_TOKEN` is not set for a gateway run, then the recipe shall exit
  with a message that names both, before any request is sent.
- **R44** (ubiquitous) The recipe shall keep the course token out of every result, trace item and printed line.

**Traces**

- **R50** (event) When a question ends, with any status, the recipe shall write one trace item with the question,
  the SQL, the status, every step and a hash in place of the user id.
- **R51** (ubiquitous) The recipe shall set `expires_at` of every trace item to 7 days after it is written.
- **R52** (event) When a run with `--target aws` starts and the table `$PREFIX-chat-traces` does not exist, the
  recipe shall create it with on-demand capacity and time to live on `expires_at`.
- **R53** (complex) While the table `$PREFIX-chat-traces` is in the `DELETING` state, when a run with `--target aws`
  starts, the recipe shall wait until the table is gone and create it again.
- **R54** (unwanted) If a trace write to DynamoDB fails, then the recipe shall print `trace write failed` and return
  the answer as it is.
- **R55** (ubiquitous) The recipe shall keep at most 5 rows of a result in its trace item.

**Course constraints**

- **R60** (ubiquitous) The recipe shall start the name of every AWS resource it creates with `$PREFIX` (default
  `recipe`).
- **R61** (ubiquitous) The recipe shall create its AWS resources in `ca-central-1` unless `AWS_REGION` names another
  region.
- **R62** (ubiquitous) The recipe shall run its stub run without an AWS account, a course token or a network
  connection.

**Clean-up**

- **R70** (event) When `clean_up.py` runs, the recipe shall delete the three tables, the three alarms and the SNS
  topic with its subscriptions that it created for this `PREFIX`.
- **R71** (event) When `clean_up.py --dry-run` runs, the recipe shall print the names it would delete without a call
  to AWS.
- **R72** (state) While a table is in the `DELETING` state, the recipe shall report it as `absent` in a clean-up and
  delete the other resources.
- **R73** (unwanted) If AWS refuses a step of `clean_up.py`, then the recipe shall exit with code 1 after it has
  tried every other step and printed `FAILED` for the refused one.
- **R74** (event) When `clean_up.py` runs and a resource is already gone, the recipe shall report it as `absent`,
  not as a failure.
- **R75** (ubiquitous) The recipe shall leave in place every alarm other than its own three, also one whose name
  starts with `$PREFIX-chat-`.

**Requirements of the extensions**, each in the README of its folder:

- 80 to 86, answer cache: [`ext-answer-cache/README.md`](ext-answer-cache/README.md#requirements)
- 90 to 95, guardrails with a red-team log: [`ext-guardrails-redteam/README.md`](ext-guardrails-redteam/README.md#requirements)
- 100 to 104, trace analysis and alarms: [`ext-trace-alarms/README.md`](ext-trace-alarms/README.md#requirements)
- 110 to 114, a user's verdict on an answer: [`ext-feedback/README.md`](ext-feedback/README.md#requirements)
- 120 to 129, the chat as a web page: [`ext-shiny-app/README.md`](ext-shiny-app/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need a key or an AWS account. The course LLM gateway
runs in the course's account, never in yours: you only send it requests.

| Part | Files | Laptop, offline | Laptop, with the gateway and your team sandbox | EC2 instance or Fargate task in your sandbox |
|---|---|---|---|---|
| Chat on the command line | `main/run.py`, `main/chat.py`, `main/prompts.py` | yes, `--model stub` | yes, `--model gateway` | no: nothing there uses it |
| SQL check and read-only engine | `main/sqlcheck.py`, `main/engine.py` | inside the chat's or the page's own Python process, wherever that runs: they are not a service | the same | the same, inside the page's process |
| Tables | `main/data.py` writes `data/*.parquet` | on your disk | on your disk | on the instance's disk, or inside the container image |
| Model | `main/model.py`, `ext-shiny-app/client.py` | a stub: fixed replies, no model | the course LLM gateway | the course LLM gateway |
| Traces | `main/traces.py` | `output/traces.jsonl` | the DynamoDB table `$PREFIX-chat-traces`, with `--target aws` | the page does not write traces |
| Cache, guardrails, spans and alarms, verdicts | `ext-answer-cache/`, `ext-guardrails-redteam/`, `ext-trace-alarms/`, `ext-feedback/` | yes, their stub runs | yes; cache, spans, alarms and verdicts in your sandbox with `--target aws` | no |
| Web page | `ext-shiny-app/app.py`, `client.py`, `source.py` | yes, `APP_MODEL=stub`, on `127.0.0.1:8000` | yes, on `127.0.0.1:8000` with the gateway | yes: `shiny run` on the instance, or the container on Fargate |
| Stub gateway | `ext-shiny-app/stub_gateway.py` | yes, started by the page | no | no: the file is in the container image, and with `APP_MODEL` not set to `stub` nothing starts it |
| Fargate scripts | `ext-shiny-app/fargate/*.sh` | `DRY=1` prints their commands | run from the laptop, which needs Docker for the image build; they create things in your sandbox | they are not copied there |
| Clean-up | `clean_up.py`; `ext-shiny-app/fargate/teardown.sh` | `--dry-run`, `DRY=1` | run from the laptop with your sandbox credentials | never run there |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS or the gateway | no |

This recipe does not have a Lambda function. For a handler behind a function URL, see
[`lambda-dynamodb`](../lambda-dynamodb/README.md).




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole` or `dynamodb:TagResource`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `cloudwatch:DeleteAlarms`, `cloudwatch:DescribeAlarms`, `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateTimeToLive`, `sns:DeleteTopic`, `sns:ListTopics` |
| `ext-feedback/` adds | `dynamodb:Scan`, `dynamodb:UpdateItem` |
| `ext-shiny-app/` adds | `ec2:AuthorizeSecurityGroupIngress`, `ec2:CreateSecurityGroup`, `ec2:CreateTags`, `ec2:DeleteSecurityGroup`, `ec2:DescribeInstances`, `ec2:DescribeNetworkInterfaces`, `ec2:DescribeSecurityGroups`, `ec2:DescribeSubnets`, `ec2:DescribeVpcs`, `ec2:RunInstances`, `ec2:TerminateInstances`, `ecr:BatchCheckLayerAvailability`, `ecr:CompleteLayerUpload`, `ecr:CreateRepository`, `ecr:DeleteRepository`, `ecr:DescribeRepositories`, `ecr:GetAuthorizationToken`, `ecr:InitiateLayerUpload`, `ecr:PutImage`, `ecr:UploadLayerPart`, `ecs:CreateCluster`, `ecs:DeleteCluster`, `ecs:DeleteTaskDefinitions`, `ecs:DeregisterTaskDefinition`, `ecs:DescribeClusters`, `ecs:DescribeTasks`, `ecs:ListTaskDefinitions`, `ecs:ListTasks`, `ecs:RegisterTaskDefinition`, `ecs:RunTask`, `ecs:StopTask`, `iam:AttachRolePolicy`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:DetachRolePolicy`, `iam:GetRole`, `iam:PassRole`, `iam:PutRolePolicy`, `logs:CreateLogGroup`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `logs:DescribeLogStreams`, `logs:PutRetentionPolicy`, `secretsmanager:CreateSecret`, `secretsmanager:DeleteSecret`, `secretsmanager:DescribeSecret`, `secretsmanager:PutSecretValue`, `ssm:GetParameter` |
| `ext-trace-alarms/` adds | `cloudwatch:PutMetricAlarm`, `cloudwatch:PutMetricData`, `dynamodb:BatchWriteItem`, `dynamodb:Query`, `dynamodb:Scan`, `sns:CreateTopic`, `sns:Subscribe` |

### 1. Laptop, offline: the dry run

On your laptop, from the repository root, run the commands below. You do not need a key or an AWS account.

```bash
cd dashboard-chat
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run --target local --model stub
APP_MODEL=stub uv run shiny run ext-shiny-app/app.py --port 8000
```

What you should see, line by line:

1. `uv sync` installs the packages, the Shiny app's among them.
2. `pytest` ends with a line like `89 passed`.
3. `behave` ends with `0 failed`. One scenario opens the page in a headless browser; without Chromium for
   Playwright it is skipped (`uv run playwright install chromium` installs it, about 150 MB).
4. The stub run prints one JSON line per question and a last line with the total, and writes `output/traces.jsonl`.
5. `shiny run` prints `Uvicorn running on http://127.0.0.1:8000`. Open that address, type "Show the cake orders":
   the box "Orders shown" goes from 1,200 to 285. Stop the page with Ctrl-C.

### 2. Laptop, with the course LLM gateway

Run the commands in `dashboard-chat`. Traces go to `output/traces.jsonl`. The run does not create anything in AWS.

1. Get the gateway's base URL from the course page or from your team repo on github.students. Replace
   `YOUR-GATEWAY-BASE-URL` with it and keep the quotes.
2. `read` does not show a prompt: paste your course MCP token, then press Enter.

```bash
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN    # the token does not show and is not saved in the shell history
uv run python -m main.run --target local --model gateway
uv run shiny run ext-shiny-app/app.py --port 8000
```

When the new names are not set, `GATEWAY_URL` and `COURSE_MCP_TOKEN` work instead.

What you should see: the same six lines as offline, with replies in the LLM's own words and a total of a fraction
of a cent; then the page on `http://127.0.0.1:8000`, where every question now spends a little of your key's budget.

### 3. Laptop, against your team sandbox

In the same terminal; traces go to DynamoDB in ca-central-1. Sign in to your team sandbox first, and check that the account is the one you expect:

```bash
aws sso login                                          # or the sign-in your team uses for its sandbox
aws sts get-caller-identity                            # the account you expect, before anything is created
```

1. Keep `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` set: `--target aws` also calls the gateway.
2. Replace `yourname` with a name of your own before you run the `export` line.

```bash
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python -m main.run --target aws
uv run python -m main.run --target aws --question "Which store opened most recently?"
```

What you should see: the six lines, then `traces in recipe-yourname-chat-traces`; in the DynamoDB console, one item
per question in that table. When you are done: [Cost and clean-up](#cost-and-clean-up).

### 4. Web page on Fargate

**Not run by us yet (2026-11-13).** The scripts were written from the AWS documentation and the course's L15 demo,
and checked only with `DRY=1`, which prints every command and runs none. The container image was built on an ARM
machine and the page answered from it with the stub; nothing was pushed or started in AWS. Read the commands that
`DRY=1` prints before you run them.

Where the task runs. The course sandbox we ran part 5 in on 2026-11-14 has no default VPC: its only VPC is
`cpsc436c-vpc`, with two public subnets.
So `deploy.sh` and `start.sh` look for the VPC tagged `Name=cpsc436c-vpc` first and for the account's default VPC
second, and take a subnet of it that gives a public address (`vpc_id` and `public_subnet` in `common.sh`). With
neither VPC, or without such a subnet, `deploy.sh` stops with a `FAILED` line before it makes anything. These
look-ups have run only against a stand-in for the `aws` command (`tests/test_app.py`), not in AWS; `DRY=1` does not
make them and prints example ids.

Sign in to your team sandbox first, and check that the account is the one you expect:

```bash
aws sso login                                          # or the sign-in your team uses for its sandbox
aws sts get-caller-identity                            # the account you expect, before anything is created
```

`deploy.sh` makes an IAM role for the task. If your sandbox refuses `iam:CreateRole`, set `EXEC_ROLE_ARN` to the
execution role your sandbox provides before you run it; `deploy.sh` and `teardown.sh` then leave roles alone, and
that role must be allowed to read the secret `$PREFIX-chat-app/token`.

On your laptop, in `dashboard-chat`, with Docker running and the two gateway variables and `PREFIX` set:

```bash
DRY=1 bash ext-shiny-app/fargate/deploy.sh      # prints what the next line would do
bash ext-shiny-app/fargate/deploy.sh            # secret, role, image, cluster, security group, task definition
bash ext-shiny-app/fargate/start.sh             # starts one task and prints its address; billing by the hour starts
bash ext-shiny-app/fargate/url.sh               # prints the address again
bash ext-shiny-app/fargate/stop.sh              # stops the task; billing by the hour ends
bash ext-shiny-app/fargate/teardown.sh          # removes everything deploy.sh made
```

What each step does, and what you should see:

1. `deploy.sh` stores your course MCP token in a Secrets Manager secret (`$PREFIX-chat-app/token`), makes an
   execution role that may pull the image, write logs and read that one secret, builds the image from
   `ext-shiny-app/Dockerfile` and pushes it to an ECR repository, makes a log group, an ECS cluster and a security
   group that lets only your current address reach port 8000, and registers a task definition (0.5 vCPU, 1 GB) with
   `GATEWAY_URL` and `CHAT_MODEL` as environment variables and the token from the secret. It ends with
   `prepared; nothing is running`. It does not start a task.
2. `start.sh` runs one task with a public address, waits one to two minutes, and prints `http://<address>:8000/`.
   Open it: the page of step 1, now answering through the gateway.
3. `stop.sh` prints `tasks still running: 0`. Run it when you stop working, not at the end of the term.
4. `teardown.sh` prints one command per thing it removes and ends with
   `done: everything deploy.sh made for ... is removed`. If a step fails it prints a line that starts with
   `FAILED`, goes on with the other steps, and exits with code 1: run it again after you fix the cause.
   `clean_up.py` does not know these resources.

Two things to know while it runs:

- **New token or new gateway address.** Run `deploy.sh` again, then `stop.sh` and `start.sh`: a running task
  keeps the value it read when it started, and gets the new one only as a new task
  ([secrets in ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)).
- **New network.** The security group lets in the address you had at `deploy.sh`. From another network the page
  does not answer: delete the group's rule and add your new address, or run `teardown.sh` and `deploy.sh` again.

### 5. Web page on an EC2 instance

Run by us on 2026-11-14 in a course sandbox ([`output/shiny-ec2.txt`](output/shiny-ec2.txt)). The same page
without a container: an instance you sign in to, and the same `shiny run` as on your laptop. Replace
`YOUR-KEY-PAIR` with a key pair of your sandbox (`aws ec2 create-key-pair` makes one). The run changed three things
in the commands below: the course sandbox does not have a default VPC, so the security group and the instance name the
sandbox's VPC and one of its public subnets. Check your VPC and subnet before creating resources; `libsass`, which Shiny needs, has no ready-made package for Linux on
arm64, so the instance needs a C compiler; and `uv` is told to use Python 3.12, because left alone it chose 3.15.
The course repository is private, so the folder is copied to the instance, as in the run. A clone is the
alternative for a repository the instance can read.

Sign in to your team sandbox first, and check that the account is the one you expect:

```bash
aws sso login                                          # or the sign-in your team uses for its sandbox
aws sts get-caller-identity                            # the account you expect, before anything is created
```

On your laptop, with `PREFIX` set:

```bash
MY_IP=$(curl -s https://checkip.amazonaws.com)
VPC=$(aws ec2 describe-vpcs --filters Name=tag:Name,Values=cpsc436c-vpc --query 'Vpcs[0].VpcId' --output text)   # the course sandbox's VPC; it has no default VPC
[ "$VPC" != None ] || VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)   # another account: its default VPC
SUBNET=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC Name=map-public-ip-on-launch,Values=true --query 'Subnets[0].SubnetId' --output text)
echo $VPC $SUBNET                      # a vpc- id and a subnet- id; neither may be None: stop here if one is
SG=$(aws ec2 create-security-group --group-name $PREFIX-chat-app-ec2 --vpc-id $VPC --description "dashboard-chat page on EC2" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr $MY_IP/32
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 8000 --cidr $MY_IP/32
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --query Parameter.Value --output text)
INSTANCE=$(aws ec2 run-instances --image-id $AMI --instance-type t4g.small --key-name YOUR-KEY-PAIR --subnet-id $SUBNET --security-group-ids $SG --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PREFIX-chat-app}]" --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids $INSTANCE
aws ec2 describe-instances --instance-ids $INSTANCE --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
```

In our run the first `describe-vpcs` had no filter and took the account's only VPC, which is `cpsc436c-vpc`; the
filter on the name and the fall-back to the default VPC have not run in AWS.

The last line prints the instance's address. Copy the recipe folder to the instance: on your laptop, from the root
of your clone, with the address in place of `<the address>`:

```bash
COPYFILE_DISABLE=1 tar --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude output -czf - dashboard-chat | ssh ec2-user@<the address> 'tar -xzf -'
```

On a Mac, `COPYFILE_DISABLE=1` stops `tar` from adding a `._*` file beside each file it copies (macOS's extended
attributes); elsewhere it does not change anything.

That is the copy our run made, except that our copy unpacked under a folder `recipes/`.
The course repository was private on 2026-11-14 (`gh repo view` said `PRIVATE`), so a `git clone` on the instance
needs a sign-in; the alternative is under the block. Sign in (`ssh ec2-user@<the address>`), and on the instance:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
sudo dnf install -y gcc gcc-c++ && cd dashboard-chat
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN
uv run --no-dev --python 3.12 shiny run ext-shiny-app/app.py --host 0.0.0.0 --port 8000
```

What you should see: `uv run --no-dev` installs the packages without the test tools, building `libsass` on the
way, then prints `Uvicorn running on http://0.0.0.0:8000`; the page is at `http://<the address>:8000/`, from your
own network only. In the captured run the page answered 4.5 minutes after the instance did (the compiler, the
packages and the build are in that time), and "Show the cake orders" took the table from 1,200 orders to 285
through the gateway.

To clone in place of the copy, for a public repository or your team's own: on the instance, `sudo dnf install -y
git`, then give the instance a way to read the repository. Either make a key there (`ssh-keygen -t ed25519`), add
its public half to the repository as a read-only deploy key (repository Settings, Deploy keys) and clone the SSH
address; or install the GitHub CLI and run `gh auth login`, then `gh repo clone <owner>/<repository>`. Then `cd`
into its `dashboard-chat` folder and go on from the `export` line. Delete the deploy key when the instance is gone.
The token is in that shell's memory and nowhere on disk; when you sign out, the page stops. When you are done, on
your laptop, in the terminal that still has `INSTANCE` and `SG`:

```bash
aws ec2 terminate-instances --instance-ids $INSTANCE
aws ec2 wait instance-terminated --instance-ids $INSTANCE    # the security group is in use until then
aws ec2 delete-security-group --group-id $SG
```

## What you should see

Offline, with the stub model (`output/main-stub.txt`), one JSON line per question, then the total:

```
{"question": "What were total sales in Vancouver?", "status": "answered", "reply": "The answer is 7044.58.", "sql": "SELECT ROUND(SUM(o.amount_cad), 2) AS sales_cad FROM orders o JOIN stores s USING (store_id) WHERE s.city = 'Vancouver'", "rows": [[7044.58]], "model_calls": 2, ...}
{"question": "Delete all the orders", "status": "no_answer", "reply": "I could not run a query for that question.", "sql": "", "rows": [], "model_calls": 2, ...}
model: stub; total 0.000148 CAD (stub: tokens estimated from characters); traces in output/traces.jsonl
```

What holds on every run: the DELETE never runs (its step has status `rejected`); the weather question ends without a
query. With the stub, the number in the reply equals the value in `rows`; with the gateway the LLM writes the reply,
so compare its number with `rows`. The times, the request ids and, with the gateway, the replies' wording and the
token counts change from run to run.

A real run with `--target aws` (2026-11-12, both commands) is in [`output/main-gateway.txt`](output/main-gateway.txt):
every reply's number equals its `rows`, 0.000287 CAD for the six questions. In that run the model itself declined the
DELETE without writing a query (status `no_answer`, one model call), so the SQL check was not reached; the check is
shown by the stub run and the tests.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Answer or retrieval cache | Exact answer cache keyed on the normalized question, the data version, the model and prompt version and who asks; looked up before the model | `ext-answer-cache/` | Step 3 is skipped on a hit; a new table `$PREFIX-chat-cache` |
| Guardrails with a red-team log | Input check, an engine that holds only the asking customer's rows, output check; a log that counts attacks that got through and normal questions refused | `ext-guardrails-redteam/` | Before step 3, inside step 4 and after step 6 |
| User's verdict on an answer | A thumbs up or down stored on the trace item of the question; a report of the answers marked down | `ext-feedback/` | After step 7; nothing new in AWS |
| Chat as a web page | A Shiny Express app with querychat: the question is typed in a page, the result is a table on it; scripts to run it on Fargate | `ext-shiny-app/` | The page takes the place of `main/run.py`; steps 2, 4 and 5 are reused, step 7 (traces) is not |
| Trace analysis and alarms | One span (a record of a call) per model call and tool call, linked to its request; metrics; CloudWatch alarms on errors, slow answers and daily cost; DuckDB queries over the spans | `ext-trace-alarms/` | Step 7 also writes spans and metrics; three alarms and an SNS topic |

## Cost and clean-up

One run of the six questions in `main/questions.txt` on the gateway made 10 model calls and cost 0.000287 CAD of
the key's budget at the gateway's prices for `qwen3.6-35b-a3b`
(0.05 CAD per million input tokens, 0.10 per million output
). Six DynamoDB writes on demand
are far below one cent (0.6875 USD per million write request units in ca-central-1, AWS bulk price list).
Stored items are billed until TTL or clean-up deletes them (0.275 USD per GB-month beyond the first 25 GB-months,
same price list); six traces are a few kilobytes.

The web page incurs charges while it runs, and only then. The prices below are per hour (AWS bulk price lists for ca-central-1; in USD):

| Where | What is billed | Per hour |
|---|---|---|
| Fargate, ARM, 0.5 vCPU and 1 GB | 0.5 × 0.03565 for the vCPU, 0.00389 for the memory, 0.005 for the public IPv4 address | 0.0267 |
| Fargate, x86, 0.5 vCPU and 1 GB | 0.5 × 0.04456, 0.004865, 0.005 | 0.0321 |
| EC2 `t4g.small` (2 vCPU, 2 GB) | 0.0184 for the instance, 0.005 for the public IPv4 address | 0.0234 |
| Fargate: the page's log lines | 0.55 per GB of log data sent to CloudWatch Logs; a day of the page's log lines is a few hundred kilobytes | cents per month |
| Either: the pages your browser loads | 0.09 per GB of data sent out to the internet, beyond a free allowance; the page and its tables are a few megabytes per visit | cents per month |


A working day of eight hours on Fargate is about 0.21 USD. What stays after
`stop.sh` and bills by the month until `teardown.sh`: the secret (0.40 USD per month) and the image in ECR
(0.10 USD per GB-month; the image is about 0.2 GB). An EC2 instance that is stopped, not terminated, still
bills its disk (0.088 USD per GB-month for gp3).
Every question in the page also spends your course key's budget at the gateway, as on the command line.

On your laptop, in `dashboard-chat`, with the same `PREFIX` and sandbox credentials as the run. The dry run
prints the names it would delete and does not contact AWS; check them, then run the second line:

```bash
uv run python clean_up.py --dry-run    # names of the tables, alarms and topic of this PREFIX
uv run python clean_up.py              # deletes those that exist
bash ext-shiny-app/fargate/teardown.sh # only if you ran deploy.sh: the Fargate pieces of the web page
```

## Adapt it

1. Put your Parquet files in `data/` and list them in `TABLES` in `main/data.py` (or point `ensure_data` at your
   files and delete `make_data`). The schema in the system prompt follows from the tables.
2. Rewrite the rules in `main/prompts.py` for your users: what "my" means, which questions are out of scope.
3. Keep `main/sqlcheck.py` and `main/engine.py` as they are; `allowed_tables` comes from your table names.
4. Write ten of your users' real questions into `main/questions.txt`, and write a tool call and a reply for each one in
   `main/stub_replies.json`, in the format already there. The stub run then answers them offline. The tests in
   `tests/` ask their own fixed questions: add a test for each question whose answer you want checked.
5. For a dashboard page, start from [`ext-shiny-app/`](ext-shiny-app/README.md): its `source.py` gives querychat
   the recipe's SQL check and engine. In a page of your own (Streamlit, FastAPI), call `main.chat.answer(...)` and
   show `reply` above `rows`.
