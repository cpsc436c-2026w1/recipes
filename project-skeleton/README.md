# Project skeleton: live updates to one number on a page

## What you build

Build the first slice end to end, due at Checkpoint 2: live updates arrive, are stored, reach the lake and show as one number to users
(updates in the last hour). Use the shape your team chose: lakehouse first, operational first or generative AI first.
Each has its own folder, and you build over the same data and the same number.

## Use it when

- Your team needs the first slice (due at Checkpoint 2) and wants a working start in its project shape.
- You want to compare the shapes on one job before your proposal's design is fixed.

For an extension such as windowed aggregates, change data capture, concurrency races, chat on the
dashboard or a bounded agent, use the recipe named in Extensions. Each one starts from the folder of your shape.

## Main scenario

Shared part, in every folder:

1. A producer makes keyed updates: `event_id`, `entity_id` (the key), `event_time` (UTC), `value`. The synthetic
   data is bike-share stations reporting free bikes, about 300 updates over two hours, five of them sent twice
   (`skeleton/events.py`).
2. The updates are stored and reach the lake (S3), each shape by its own path, below.
3. A query counts the distinct `event_id`s whose event time falls in the 60 whole minutes before the current minute
   (at 16:00:30, from 15:00:00 up to 16:00:00; `skeleton/number.py`).
4. The number is written to a page for users (`.local/<shape>.html`; replace it with your dashboard).

Each representative's path for step 2 and step 3:

| Step | `rep-lakehouse-first/` | `rep-operational-first/` | `rep-genai-first/` |
|---|---|---|---|
| Arrive | `PutRecords` into a Kinesis data stream, keyed by `entity_id` | signed `POST /updates` to a Lambda function URL | as in operational first |
| Stored | the stream keeps each record for 24 hours | DynamoDB: one item per update, a counter per minute, the current state per key | as in operational first |
| Reach the lake | Firehose reads the stream and writes Parquet to `s3://…/updates/dt=<day>/` every 60 s | a full DynamoDB export to `s3://…/exports/` | as in operational first |
| One number | Athena over the Parquet files | `GET /number` adds up 60 minute counters; Athena over the export gives the same number | a user message goes through the course LLM gateway; the model calls one read-only tool, `count_updates`, which reads the table's minute counters; the reply states the number. `lake_example.py` asks the same question of the lake (Athena over the export), without a model |

## Decisions

The three representatives, side by side:

| Representative | What it puts first | What it trades off | Draws on |
|---|---|---|---|
| Lakehouse first | how data lands in the lake and how a query over it runs: columnar files, folder partitions with a key and value for each day, a scan limit | the number is about a minute old (Firehose's buffer); there is no current state per key to read or change | L8, L10, L11 (lakes, Athena, partitions); L14 (stream storage, event time); FDE ch. 6 and 7 (storage, ingestion); DDIA 2e ch. 12 (stream processing) |
| Operational first | an API over current state whose writes stay correct when copies and late updates arrive | the lake copy is as fresh as the last export, and a counter per minute takes every write | L3 (functions behind a URL); L12 (DynamoDB keys, read units); L14 (retries, idempotency with a request id); DDIA 2e ch. 8 (transactions); REL04-BP04 |
| Generative AI first | the assistant: what context the model gets, which single tool it may call, and a check of the reply | a model call adds extra delay and a per-token bill on top of operational first; app code checks the reply before using it; the assistant reads current state, so questions about history go to the lake | L15 to L17 (the querychat demo, tools, bounded agents); AI Engineering ch. 5 (prompt engineering) and ch. 6 (RAG and agents) |

Each choice inside them:

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| names (R20, R21) | every name starts with `$PREFIX` (default `recipe`), bucket `<prefix>-lake-<account>` | teammates in one account do not collide; `clean_up.py` finds everything | course constraint; `skeleton/config.py` |
| window (R10, R11) | the 60 whole minutes before the current minute, by event time | the API's minute counters and the lake queries then agree exactly. Event time, not arrival time, is what the update is about | L14 (event time against processing time) |
| duplicates (R10, R51) | every update carries an `event_id`; the lake counts `DISTINCT event_id`; the API refuses a second item with the same id | a producer that times out sends again, and Kinesis and Firehose deliver at least once | L14 (idempotency with a request id); REL04-BP04 Make mutating operations idempotent |
| stream capacity (R40) | Kinesis provisioned mode, one shard | one shard takes up to 1,000 records or 1 MB a second of writes, far above a checkpoint's load; 0.0165 USD per shard-hour against 0.048 USD per stream-hour on demand | AWS bulk price list, ca-central-1; FinOps ch. 15 (using less) |
| partition key (R44) | `entity_id` | updates of one key land in one shard, in the order written (`PutRecords` does not guarantee the order sent, so event time decides which update is newer) | L14; DDIA 2e ch. 12; [PutRecords API](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html) |
| file format (R40) | Firehose converts JSON to Parquet with Snappy, schema from the Glue table | Athena reads only the columns a query names, so Data scanned stays small | L10, L11; Firehose docs (record format conversion) |
| buffer (R40) | 60 s or 64 MB, whichever comes first | 64 MB is the smallest size Firehose allows with conversion ([record format conversion](https://docs.aws.amazon.com/firehose/latest/dev/enable-record-format-conversion.html)); 60 s bounds how old the number is | Firehose API reference, [BufferingHints](https://docs.aws.amazon.com/firehose/latest/APIReference/API_BufferingHints.html) |
| partitions (R41, R47) | one folder per arrival day `dt=YYYY-MM-DD`, with Athena working out the folders to read using partition projection | a query over the last hour reads only the folders from the window's first day on (today's, or yesterday's too just after midnight); Athena works out the folders, so you do not run a crawler | L11; FDE ch. 6 |
| Athena's result files (R24) | a lifecycle rule on the bucket: objects under `athena-results/` expire after 7 days | every query leaves a result file and a metadata file there, and the scripts never read them again. The lake and the exports do not have a rule | Data Analytics Lens [BP 13.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/design-principle-13.html) (remove unused data) |
| scan limit (R42) | Athena workgroup `<prefix>-wg` cancels any query that would scan more than 1 GB | a wrong query, or a model's, cannot run up the bill | COST02-BP05 Implement cost controls |
| one table (R50, R60) | DynamoDB on demand, key `pk`, three kinds of item: `event#<id>`, `minute#<time>`, `key#<entity>` | each read the API needs is a lookup by key, so the table does not need a Scan or an index | L12 (design keys for the reads you have) |
| out of order (R52) | the state item changes only if the update is newer (`last_event_time < :t`) | a late update is counted but does not replace newer state | L14 (late records); DDIA 2e ch. 8 |
| writes together (R50, R51) | the event item and its minute counter in one `TransactWriteItems` | a copy cannot raise the counter without its item, and its item cannot be stored without raising the counter. Each item in a transaction bills two write units | DDIA 2e ch. 8; DynamoDB docs (transaction capacity) |
| reads (R54, R58) | `GET /number` uses eventually consistent `BatchGetItem` of 60 counters; counters that DynamoDB leaves unread are asked for again, at most 5 times in all, then the answer is 503 | half the read units of a strongly consistent read; a dashboard can miss the last second's writes. A number added up from only some of the counters would look right and be too small, and a loop without a limit ends when Lambda's timeout ends it | L12 |
| hot counter | one counter item per minute takes every write of that minute | fine up to about 500 updates a second (worked out from the docs, not measured): one partition takes 1,000 write units a second, and each update writes the counter in a transaction (two units); above that, split each minute into several counters | DynamoDB docs (partition key design) |
| lake copy (R60, R62) | a full export after the run; point-in-time recovery on; time to live (TTL) deletes event items after 7 days | an export does not use the table's read units; the lake retains exported event items after the table deletes them | L11, L12; DynamoDB docs (export to S3) |
| what a request may hold (R53) | 1 to 100 updates, each with its four fields: the two ids as strings of 1 to 64 characters, the time in the fixed form, the value a number | the URL receives whatever a caller sends. Without validation, an id of another type would fail inside the write and reach the caller as a server error, and one bad update must not leave half a request stored | Serverless Applications Lens, [data protection](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/data-protection.html): validate inbound events |
| log line (R57) | the function writes one JSON line per request: Lambda's request id, method, route, status, the counts of the reply; no id of an update or a key, and a path that is not a route is written as `other` | Lambda's own `REPORT` lines say what a call was billed, not what it did; with this line "how many copies were refused today" is a query over the log group. Ids and text a caller typed stay out of a log that more people can read than the table | Serverless Applications Lens, [centralized and structured logging](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/opex-logging.html) |
| who may call (R60) | function URL with `AWS_IAM` auth; requests are signed | only signed requests from identities in your account that may invoke the URL reach the API, without writing an auth layer. The recipe does not grant another account access to the API. A caller needs both `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` | SEC03-BP02 Grant least privilege access; Lambda docs, [Using the `AWS_IAM` auth type](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html) |
| permissions (R43, R61) | each role names its actions and its resources: Firehose reads one stream and one Glue table and writes one bucket; the function reads and writes one table and its own log group | a mistake or a leaked role reaches one table, not the account | SEC03-BP02 Grant least privilege access |
| model calls (R70, R75) | through the course LLM gateway, with your course token from `KB_MCP_TOKEN` (`COURSE_MCP_TOKEN` still works) | the gateway holds the provider key and your budget and rate limit; provider keys never reach your code | course constraint: model calls go through the course LLM gateway |
| where the assistant reads (R76, R77) | the DynamoDB table (current state), not the lake. `lake_example.py` asks the same question of the lake | the table is current to the last write; the lake is as fresh as its last export, so it suits questions about history | L12 (operational store); L11 (lake) |
| context (R70) | the system prompt says what the tool counts and gives the exact window as two times | the model does not guess the time or the table | AI Engineering ch. 5; L16 |
| read-only tool (R71, R72) | one tool, `count_updates(start, end)`, whose only DynamoDB action is `BatchGetItem` on minute counters; it refuses windows that are not whole minutes or are longer than a day | the model can only read: the tool's code calls only `BatchGetItem`. Here the tool runs with your own credentials; a deployed assistant gets a role with only `tool_policy()`, which still holds if the code changes | AI Engineering ch. 6 (tools, read-only actions); L17; Agentic AI Lens [AGENTREL02-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentrel02-bp02.html) limit agent permissions to minimum required access, [AGENTSEC02-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec02-bp02.html) validate tool inputs and outputs |
| bounded loop (R73, R79, R80) | at most 3 model calls and 400 output tokens each; 600 for `qwen3.8-27b`; a reply that stops at `finish_reason` `length` with no text, or with a cut tool call, is an error, not a reply | the loop stops after 3 calls, so a confused model does not keep spending your budget. `qwen3.8-27b` reasons before it answers, and the reasoning counts against `max_tokens`: with 150 its replies came back empty, with 600 one answer of two sentences took 330 completion tokens, 277 of them reasoning; 600 is a margin over the most we measured. An empty reply that stopped at the cap is not an answer and not a refusal, so it is not checked as a reply | L17 (bounded agents); [GENCOST03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp02.html) (control response length) |
| check the reply (R74) | `grounded()` accepts the reply only if its number is a count the tool returned | a reply that states a number it did not read is caught before a user sees it | AI Engineering ch. 4 (evaluate AI systems); L16 |

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
| optional | Where <a part is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. Read each representative's requirements in its own README. Their numbers go on from here:
[lakehouse first](rep-lakehouse-first/README.md#requirements) from 40,
[operational first](rep-operational-first/README.md#requirements) from 50,
[generative AI first](rep-genai-first/README.md#requirements) from 70. When you change what the recipe does,
change the requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when
they do not match.

**Number, in every representative**

- **R10** (ubiquitous) The recipe shall count as updates in the last hour the distinct `event_id`s whose event time
  is in the 60 whole minutes before the minute of `--as-of`, or of the current time without it.
- **R11** (ubiquitous) The recipe shall give the same number in every representative for the same updates and the
  same `--as-of`.
- **R12** (event) When a run has its number, the recipe shall write it to the page `.local/<shape>.html`.
- **R13** (optional) Where `--events` or `--streamsim` names a file, the recipe shall send the updates of that file
  in place of the synthetic ones.
- **R14** (unwanted) If an update in an `--events` file lacks one of the four fields, then the recipe shall stop
  with an error that names the field, before it stores anything.
- **R15** (ubiquitous) The recipe shall run every representative offline without an AWS account, credentials or a
  gateway token.

**Constraints**

- **R20** (ubiquitous) The recipe shall start every resource name with `$PREFIX`, and with `recipe` if `PREFIX` is
  not set.
- **R21** (unwanted) If `PREFIX` holds anything but letters, digits and hyphens, or more than 30 characters, then
  the recipe shall stop before it creates anything.
- **R22** (ubiquitous) The recipe shall send every call to ca-central-1, whatever `AWS_REGION` says.
- **R23** (unwanted) If the bucket's name belongs to another account, then the recipe shall stop before it creates
  a stream or writes an object.
- **R24** (ubiquitous) The recipe shall have S3 delete Athena's result files 7 days after they were written.

**Clean-up** (`clean_up.py`)

- **R30** (event) When `clean_up.py` runs, the recipe shall remove every resource the three representatives created
  for `$PREFIX`, and the folder `.local`.
- **R31** (complex) While nothing of the recipe is left, when `clean_up.py` runs, the recipe shall report every
  resource as absent and exit with code 0.
- **R32** (unwanted) If AWS refuses a step of the clean-up, then the recipe shall print that step as `FAILED`,
  attempt every other step, and exit with code 1.
- **R33** (ubiquitous) The recipe shall remove only resources with the exact names it builds from `$PREFIX`.
- **R34** (ubiquitous) The recipe shall delete an IAM role only if the role carries the tag `recipe` =
  `project-skeleton`.
- **R35** (complex) While the stream, the Firehose stream or the table is still being deleted, when `clean_up.py`
  runs, the recipe shall report it as already being deleted and not ask for its deletion again.
- **R36** (optional) Where `--local` is passed, the recipe shall remove the folder `.local` and make no call to
  AWS.
- **R37** (event) When `clean_up.py` deletes the table, the recipe shall print that DynamoDB keeps a system backup
  of it for 35 days, at no cost.

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal with credentials for your team sandbox: AWS
CloudShell, or your laptop after its sign-in. One file of this recipe runs inside AWS: `handler.py` of operational
first, in Lambda. The model of generative AI first runs behind the course LLM gateway; the loop around it and its
tool run in your terminal. Everything else in AWS is a service (Kinesis Data Streams, Firehose, DynamoDB, Athena)
that the scripts in your terminal create and ask.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS |
|---|---|---|---|---|
| Updates and the number | `skeleton/events.py`, `skeleton/number.py`, `skeleton/cli.py` | yes | yes | no; the SQL of `number.py` runs in Athena |
| Lakehouse first | `rep-lakehouse-first/run.py`, `lakehouse.py` | a list simulates the stream, Parquet files are on disk, and DuckDB simulates Athena | creates the stream, the Firehose stream, the Glue table and the workgroup, sends the updates, asks Athena | no code of yours |
| API of operational first | `rep-operational-first/handler.py` | called as a plain function, against DynamoDB simulated by moto, a library that stands in for AWS inside the process | `operational.py` zips it and creates the function and its URL | yes: Lambda runs it for every request to the function URL |
| Operational first, the run | `rep-operational-first/run.py`, `operational.py` | moto, an export written to disk, DuckDB | creates the table, sends signed requests to the URL, starts the export, asks Athena | no; the export and the query run in DynamoDB and Athena |
| Assistant | `rep-genai-first/run.py`, `genai.py`, `stub.py` | the stub model and moto's table | the loop and the tool, reading your table with your credentials | no; the model runs behind the course LLM gateway |
| Same question of the lake | `rep-genai-first/lake_example.py` | DuckDB over the export on disk | asks Athena, then the table | no |
| Page | `skeleton/page.py` | writes `.local/<shape>.html` | the same file, on the machine of the terminal | no |
| Clean-up | `clean_up.py` | `--local` removes `.local/` | yes, with the `PREFIX` of the runs | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

This recipe does not have a container or an instance.




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole` or `dynamodb:TagResource`. Each Athena query also needs access to the Glue Data Catalog tables and the S3 data it uses, and write access to the query result location ([Identity and access management in Athena](https://docs.aws.amazon.com/athena/latest/ug/security-iam-athena.html)).

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `athena:CreateWorkGroup`, `athena:DeleteWorkGroup`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:ListWorkGroups`, `athena:StartQueryExecution`, `athena:StopQueryExecution`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `firehose:DeleteDeliveryStream`, `firehose:DescribeDeliveryStream`, `glue:DeleteDatabase`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListRoleTags`, `iam:PutRolePolicy`, `kinesis:DeleteStream`, `kinesis:DescribeStreamSummary`, `lambda:DeleteFunction`, `lambda:DeleteFunctionUrlConfig`, `logs:DeleteLogGroup`, `s3:CreateBucket`, `s3:DeleteBucket`, `s3:DeleteObject`, `s3:ListBucketVersions`, `s3:PutLifecycleConfiguration` |
| `rep-genai-first/` adds | `dynamodb:BatchGetItem` |
| `rep-lakehouse-first/` adds | `firehose:CreateDeliveryStream`, `glue:CreateDatabase`, `glue:CreateTable`, `glue:UpdateTable`, `iam:PassRole`, `kinesis:AddTagsToStream`, `kinesis:CreateStream`, `kinesis:DescribeStream`, `kinesis:PutRecords`, `s3:ListBucket` |
| `rep-operational-first/` adds | `dynamodb:CreateTable`, `dynamodb:DescribeExport`, `dynamodb:ExportTableToPointInTime`, `dynamodb:UpdateContinuousBackups`, `dynamodb:UpdateTimeToLive`, `glue:CreateDatabase`, `glue:CreateTable`, `glue:DeleteTable`, `iam:PassRole`, `lambda:CreateFunction`, `lambda:CreateFunctionUrlConfig`, `lambda:GetFunction`, `lambda:GetFunctionUrlConfig`, `lambda:InvokeFunction`, `lambda:InvokeFunctionUrl`, `lambda:UpdateFunctionCode`, `logs:CreateLogGroup`, `logs:PutRetentionPolicy`, `s3:AbortMultipartUpload`, `s3:PutObject`, `s3:PutObjectAcl` |

### 1. Laptop, offline

You do not need an AWS account or a token. You need [uv](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`), which fetches Python 3.11 or newer if the machine does not
have it.

From your local device (laptop) in a terminal:

```bash
cd project-skeleton
uv sync
uv run pytest -q
uv run behave
uv run python rep-lakehouse-first/run.py
uv run python rep-operational-first/run.py
uv run python rep-genai-first/run.py            # the stub model gives a fixed reply
uv run python rep-genai-first/lake_example.py   # the same question of the lake, without a model
```

The following list describes what the commands do and what you should see:

1. `uv sync` installs boto3, DuckDB, PyArrow, the OpenAI client, pytest, behave and moto (a library that stands in
   for AWS inside the process).
2. `pytest` ends with a line like `76 passed`.
3. `behave` ends with `0 failed`; it takes several minutes.
4. Each run ends with a line that starts `updates in the last hour:` or, for the assistant, a reply with the
   number ([What you should see](#what-you-should-see)).

Clean-up and cost: `uv run python clean_up.py --local` removes `.local/`. Nothing is billed.

### 2. Sandbox terminal: lakehouse first

Run by us on 2026-11-12, Vancouver time (`output/rep-lakehouse-first.txt`). Sign in to your team sandbox first,
and check that the account is the one you expect.

In your sandbox terminal:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

The stream bills every hour it exists, so clean up the same day (part 5).

In this folder, with a short name of your own in place of `yourname` (in CloudShell, install uv first:
`pip install --user uv`):

```bash
export PREFIX=recipe-yourname
uv run python rep-lakehouse-first/run.py --target aws
```

It creates the stream, the Firehose stream with its role (`$PREFIX-firehose-role`), the bucket, the Glue table and
the workgroup, sends the updates, waits for Firehose's first file (a few minutes, assumed) and asks Athena.
 To use a role you already have, export `FIREHOSE_ROLE_ARN`. The script then does not create a role.

One run is under 0.01 USD ([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: operational first

Run by us on 2026-11-12 (`output/rep-operational-first.txt`). Your own identity needs
`lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on the function to call the API
([Lambda docs](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html)); sandbox users have both
.
`--skip-export` stops after the API has answered.

In the same terminal, with the same `PREFIX`, check the account first:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
uv run python rep-operational-first/run.py --target aws
```

It creates the table, the function with its role (`$PREFIX-api-role`) and its URL, sends the updates in signed
requests, asks `GET /number`, then exports the table and asks Athena. The export takes several minutes (assumed).
 To use a role you already have, export `API_ROLE_ARN`.

Clean-up and cost: part 5; under 0.01 USD.

### 4. Sandbox terminal: generative AI first

Run by us on 2026-11-12 (`output/rep-genai-first.txt`). After operational first, in the same terminal. This part
calls a model through the course LLM gateway, which counts against your gateway budget.

The recipe reads the token and the address from the environment only, never from a `.env` file, and your coding
assistant's MCP config reads the environment variable too: set the variable in the environment of your terminal and your coding assistant. If your
team keeps a `.env`, it stays out of git (`.env` is in this folder's `.gitignore`), and the tests do not read it.

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account of operational first's table
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'   # from the course page, or your team repo on github.students
export KB_MCP_TOKEN='YOUR-COURSE-MCP-TOKEN'
uv run python rep-genai-first/run.py --target aws --llm gateway   # reads the table of operational first
uv run python rep-genai-first/lake_example.py --target aws        # after operational first's export; no model
```

Clean-up and cost: it does not create anything in AWS. One question is under 0.001 CAD of your gateway budget.

### 5. Clean up

In the same terminal, with the same `PREFIX`, the same day:

```bash
uv run python clean_up.py
```

It prints one line per resource: `removed`, `absent` or `FAILED` ([Cost and clean-up](#cost-and-clean-up)).

## What you should see

An offline run on 2026-11-12 with `--as-of 2026-11-12T16:00:30Z`:

```
$ uv run python rep-operational-first/run.py --as-of 2026-11-12T16:00:30Z
1. 305 updates to send (keys: 20)
2. POST /updates: stored 300, older 0, duplicates 5
3. GET /number, window 2026-11-12T15:00:00.000Z to 2026-11-12T16:00:00.000Z: 143
4. export of 432 items to .local/operational/exports/AWSDynamoDB/local-…/data; DuckDB stands in for Athena
updates in the last hour: API 143, lake 143   (page for users: .local/operational-first.html)

$ uv run python rep-genai-first/run.py --as-of 2026-11-12T16:00:30Z
3. tool call count_updates(2026-11-12T15:00:00.000Z, 2026-11-12T16:00:00.000Z): tool result 143
4. reply after 2 model calls (0 tokens in, 0 out): There were 143 updates in the last hour.
5. reply states a number the tool returned: yes; the API's number is 143

$ uv run python rep-genai-first/lake_example.py --as-of 2026-11-12T16:00:30Z
updates in the last hour: lake 143, table 143
```

Without `--as-of` the window follows the clock, so the number changes from run to run. These relations hold in every
offline run, and in AWS once Firehose has written its file and the export has finished:

- The three representatives give the same number for the same updates and the same `--as-of`.
- `duplicates` equals the copies sent (5 with the synthetic data), and they are not counted.
- The API's number equals the lake's, and the assistant's equals the table's.
- Offline, `uv run pytest -q` ends with `76 passed`.

In AWS, Athena's Data scanned is a few kilobytes, billed as the 10 MB minimum: 6967 bytes over the Parquet files and
9366 bytes over the export in a sandbox run on 2026-11-12 (Vancouver time; 2026-11-13 03:01 UTC), all four commands in order ([`output/`](output/): one file
per representative and `clean-up.txt`). In that run the lake and the API of operational first agreed (143), lakehouse first counted 141 over its own,
later window, and the assistant's number equalled the table's (118, a later window still). Firehose wrote its first file and the export finished within the 16
minutes the resources existed.

## Extensions

Each extension is its own recipe and starts from one of these folders.

| Extension | What it adds | Recipe | What changes here |
|---|---|---|---|
| Streaming ingestion | a consumer that writes each update as it arrives, with its lag measured | `streaming-ingestion` | a Lambda consumer on the Kinesis stream of `rep-lakehouse-first/` |
| Windowed aggregates | totals per window that still place late updates in the right window | `streaming-ingestion` | a rollup next to the Parquet files |
| Change data capture | every DynamoDB change reaches the lake by itself, not only at export time | `cdc-to-lake` | the export step of `rep-operational-first/` |
| Concurrency correctness | two users change the same key at once; one change is accepted, one refused, and a race test shows it | `concurrency-correctness` | `handler.py`: a conditional write on `key#` items |
| Chat on the dashboard | plain-language questions over your data, with the model writing SQL over the lake | `dashboard-chat` | `rep-genai-first/` grows from one question to a chat; `lake_example.py` is its starting point |
| Agent with tools under controlled autonomy | a write tool behind an approval, a step and budget cap, a trace per step | `limits-and-alarms` (`ext-runaway-loop/`) | `genai.py`: a second tool, and the API of operational first as its target |

## Cost and clean-up

One run of all three in a sandbox costs well under 0.05 USD. The figure adds up the AWS price-list rates for
ca-central-1 (AWS bulk price lists for Kinesis, Firehose,
DynamoDB and Athena) for one run's volumes (assumed, not measured).

| Part | Rate | One run |
|---|---|---|
| Kinesis stream, one shard | 0.0165 USD per shard-hour, while it exists | under 0.01 USD for half an hour |
| Firehose from Kinesis, with conversion to Parquet | 0.032 USD per GB read, plus 0.019 USD per GB converted | under 0.001 USD |
| DynamoDB on demand | 0.6875 USD per million write units; about 5 write units per update (assumed: items under 1 KB) | about 0.001 USD for 300 updates |
| DynamoDB export, point-in-time recovery | 0.11 USD per GB exported, 0.22 USD per GB-month backed up | under 0.001 USD |
| Athena | 5.50 USD per TB of Data scanned, 10 MB minimum per query | under 0.001 USD for a few queries |
| Lambda, S3, CloudWatch Logs | a few requests and kilobytes | under 0.001 USD (assumed: not priced) |
| Model calls through the course LLM gateway | qwen3.6-35b-a3b: 0.05 CAD per million input tokens, 0.10 CAD per million output tokens | under 0.001 CAD for one question. It counts against your course LLM gateway budget |

The Kinesis stream bills every hour it exists, about 0.40 USD a day, even with no updates. Remove everything a run
created when you are done.

In your sandbox terminal:

```bash
PREFIX=recipe-yourname uv run python clean_up.py      # AWS resources for this prefix, then .local/
```

From your local device (laptop) in a terminal:

```bash
uv run python clean_up.py --local                  # only the offline files
```

The table has point-in-time recovery on, so when `clean_up.py` deletes it DynamoDB keeps a system backup of it,
`<table>$DeletedTableBackup`, for 35 days at no cost; you can restore the table from it in that time
([DynamoDB docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html)).
`output/clean-up.txt` was captured before the clean-up's line for the table was corrected; it still says the
backups go with the table.

`clean_up.py` deletes IAM roles only when they carry the tag `recipe=project-skeleton`, so a role you passed in by
ARN stays. It attempts every step: one that is refused is printed as `FAILED`, the others still run, and the exit
code is then 1.

## Adapt it

1. **Write your data.** Write your updates as JSONL with `event_id`, `entity_id`, `event_time` (UTC, ISO 8601) and `value`,
   and pass `--events your.jsonl`. With 436c-stream-sim, pass one stream file and the payload field to count:
   `--streamsim out/<run>/streams/<stream>.jsonl --value-field <field>`.
2. **Add more fields.** Add them to `COLUMNS` in `skeleton/events.py`, to `write_parquet_batch` in
   `skeleton/lake_local.py`, and to `EXPORT_COLUMNS` in `rep-operational-first/operational.py`. The Glue table and
   the model's view are built from those lists.
3. **Change your number.** Change the SQL in `skeleton/number.py` and, for operational first, what `store()` and `number()`
   in `handler.py` keep and add up. Keep a test that compares it with plain Python, as `tests/` does.
4. **Adapt your API.** Routes live in `handler()` in `handler.py`. Each new read should be a lookup by key.
5. **Change your question.** `--question` changes the user message. `system_prompt()` in `genai.py` says what the tool
   reads. A new question about current state gets its own read-only tool (lookups by key, as `count_updates`);
   a question about history goes to the lake, as in `lake_example.py`.
6. **Point your dashboard at the number.** `skeleton/page.py` writes a static page. Point your dashboard at the same query or at
   `GET /number` instead.
