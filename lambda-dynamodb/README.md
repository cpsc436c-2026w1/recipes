# HTTPS API in front of the trip store

## What you build

A Lambda function that runs [main/handler.py](main/handler.py) behind a public HTTPS address, with a role that may do
only what the handler does. You call it with `curl` the way an app would, and read what each call was billed.

## Use it when

- First create the tables of the [dynamodb recipe](../dynamodb/) with `KEEP=1` and the same `PREFIX` (step 1 of
  "Run it"). This recipe reads those tables.
- Your project is **operational first**, or any shape where a screen or a dashboard needs an address to call instead
  of your terminal.
- Your project takes the **concurrency correctness** extension (2 points): the handler's writes carry the conditions
  from that recipe.

## Main scenario

Words used here. A **Lambda function** is code that AWS runs on each request and bills per request and per GB-second
of configured memory. Its **role** (an IAM role; IAM is AWS Identity and Access Management) says what the code may do
in your account. A **function URL** is an HTTPS address for the function.

`main/run.sh` does these steps in your sandbox (L2–L3, L12); "Run it" has the commands:

1. Run the handler in your terminal first, against your tables (Python and boto3, the AWS library for Python). The
   scripts set `AWS_DEFAULT_REGION` to the Region of `AWS_REGION`, because boto3 reads the first and the AWS CLI the
   second.
2. Create a role whose trust policy lets the Lambda service use it (IAM). To use a role you already have, set
   `ROLE_ARN`. Steps 2 and 3 are then skipped. The role must already allow what step 3 lists. Clean-up leaves
   that role alone.
3. Give the role an inline policy: `GetItem`, `Query` and `UpdateItem` on the three tables, and writing its own logs.
4. Create the function's log group with a retention of 7 days (CloudWatch Logs), then package `handler.py` and
   create the function: Python 3.12 on arm64, 128 MB, 10 s timeout (Lambda).
5. Call it once directly, with an event, the way another AWS service would.
6. Give it a function URL with auth type `NONE`: anyone who has the URL can call it.
7. Add the two resource-policy statements a public URL needs.
8. Call it with `curl` as the app would: a trip, a rider's history, a refused claim, a cancelled accept, and an
   unknown action (HTTP 400).
9. Read the `REPORT` line Lambda logs after every call (CloudWatch Logs). The handler's own line, one per request, is
   in the same log group.
10. Remove the URL first, then the function, its log group and its role, unless `KEEP=1`. The tables stay.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| role (R21) | three DynamoDB actions on three named tables, and the function's own log group; no wildcard | the function can do what the handler does but cannot do anything else. A leaked role cannot read other tables | least privilege (L2–L3; Well-Architected [SEC03-BP02 Grant least privilege access](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/sec_permissions_least_privileges.html)) |
| who makes the role (R27, R28, R64) | the script creates `$PREFIX-api-role`; `ROLE_ARN` names an existing role instead | students can create roles in a course sandbox, so the role is part of the recipe and is removed with it. A team with a shared role keeps using it | course constraint (clean-up step for everything created) |
| transactions in the policy (R15, R21) | no extra action | the `Update` actions of a transaction are governed by the permission for `UpdateItem` on each item's table. The examples AWS gives do not include a separate permission for `TransactWriteItems` | [DynamoDB transactions and IAM](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html) |
| writes (R13, R14, R15, R16) | the conditional write and the transaction from the dynamodb recipe, unchanged | the API must not lose an update when two drivers tap at once. A refusal is an answer (who holds the trip), not an error | conditional writes (L12–L13) |
| endpoint (R22) | a function URL, not API Gateway | one function and one address; there is no API Gateway to create or pay for, and Lambda bills the calls as usual | L3 (a web entry point; the lecture's is API Gateway) |
| public URL (R22) | two statements: `lambda:InvokeFunctionUrl` with auth `NONE`, and `lambda:InvokeFunction` only via the URL | without both, callers get 403 Forbidden. The console adds both statements, but the CLI does not | [function URL auth](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html) |
| API shape (R17, R70) | a fixed list of actions, each with the fields it needs; anything else gets 400 before DynamoDB is called | the URL is public, so the function must not run code it does not know. Without the check a request that leaves out `trip_id` ends as an error inside the function: the caller gets a server error with no hint, the logs get a stack trace, and the call is billed | L3; Serverless Applications Lens SEC 3: [validate and sanitize inbound events](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/data-protection.html); which status a function URL answers with when the function raises (502 in our reading) is not checked against a page |
| size (R20) | arm64, 128 MB, 10 s timeout (course default) | Lambda bills per GB-second of configured memory, not of memory used. In ca-central-1 a GB-second on arm64 is 0.0000133334 USD, and on x86 it is 0.0000166667 | [Lambda pricing](https://aws.amazon.com/lambda/pricing/); AWS bulk price list |
| log line (R71, R72) | one JSON line per request: the Lambda request id, the action, the HTTP status and the outcome in one word (`claimed`, `refused`, `accepted`, `cancelled`, `found`, `not_found`, `listed`, `bad_request`); no id of a rider, a driver or a trip, and nothing else the caller sent | the `REPORT` line says what a call was billed and not what it did: how many claims were refused today is not in the logs. A JSON line can be filtered and counted by field. The request id links it to the `REPORT` line of the same call. Ids stay out, because log lines are kept, read by more people than the table, and not covered by the table's access rules | Serverless Applications Lens OPS 1: [centralized and structured logging](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/opex-logging.html); [data protection](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/data-protection.html): no sensitive data in the standard output of a function |
| log retention (R73) | the script creates the log group `/aws/lambda/$PREFIX-api` itself, before the function, and sets its retention to 7 days (course default, set in `LOG_DAYS`) | a log group that Lambda creates on the first call keeps its lines for ever, and stored log lines are billed by the GB-month until someone deletes them. After a `KEEP=1` run the API can stay up for weeks | Serverless Applications Lens, cost optimization: [set log retention periods](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/logging-ingestion-and-storage.html); by default CloudWatch Logs stores log data indefinitely ([change log data retention](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html)). An account-level retention policy, where the account has one, overrides the group's own setting |
| order of clean-up (R25, R60) | the URL first | nobody can call the function while the rest is removed | course constraint: a clean-up step for everything created |
| a second run (R30, R31, R32, R33) | what is there is kept and the code is replaced; a create that Lambda refuses because another change is in progress is tried again, at most 6 times, 5 s apart (course default); any other refusal stops the run | Lambda answers `ResourceConflictException` both for "already exists" and for "another operation is in progress", so the code alone does not tell them apart | [CreateFunction, Errors](https://docs.aws.amazon.com/lambda/latest/api/API_CreateFunction.html); the wording the scripts look for in the message ("already exists", "in progress") is from our runs |
| what counts as gone (R40, R41, R62) | only the API's own "not found" code: `ResourceNotFoundException` for a table, the function, its URL and its log group, `NoSuchEntity` for the role and its policy | any other refusal (no permission, an expired sign-in) would otherwise be read as "already gone" or "run the dynamodb recipe first" | [Lambda CreateFunction, Errors](https://docs.aws.amazon.com/lambda/latest/api/API_CreateFunction.html), [DeleteLogGroup](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_DeleteLogGroup.html), [IAM DeleteRole](https://docs.aws.amazon.com/IAM/latest/APIReference/API_DeleteRole.html) |
| a new role (R20) | a wait of 10 s (course default, set in `ROLE_WAIT`) between creating the role and creating the function | a role made seconds ago may not be usable by Lambda yet | course choice |

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
[redeploy](ext-redeploy/README.md#requirements) from 80, [metrics](ext-metrics/README.md#requirements) from 90,
[dashboard page](ext-dashboard-page/README.md#requirements) from 100, [read path](ext-read-path/README.md#requirements)
from 220.
When you change what the recipe does, change the requirement and its scenario with it:
Run `python3 tools/check_requirements.py` from the repo root to check that they match. The check fails when they do not match.

**API** (`main/handler.py`)

- **R10** (event) When a caller sends the action `get` with a trip id, the recipe shall return HTTP 200 with the
  trip and the read units of the read.
- **R11** (event) When a caller sends the action `get` with `strong` true, the recipe shall read the trip with a
  strongly consistent read.
- **R12** (event) When a caller sends the action `rider` with a rider id, the recipe shall return that rider's trips
  requested at or after `since` in time order, from one Query.
- **R13** (event) When a driver sends the action `claim` for a trip that has no driver, the recipe shall give the
  trip to that driver and answer `claimed: true`.
- **R14** (unwanted) If a driver sends the action `claim` for a trip that has a driver, then the recipe shall leave
  the trip as it is and answer HTTP 200 with `claimed: false` and the driver who holds the trip.
- **R15** (event) When a driver who is available sends the action `accept` for a trip that has no driver, the recipe
  shall change the trip and the driver in one transaction and answer `accepted: true`.
- **R16** (unwanted) If the driver of an `accept` is busy or its trip has a driver, then the recipe shall leave the
  trip and the driver as they were and answer HTTP 200 with `accepted: false` and the reason for each of the two.
- **R17** (unwanted) If a caller sends an action other than `get`, `rider`, `claim` and `accept`, then the recipe
  shall answer HTTP 400 without a call to DynamoDB.
- **R18** (ubiquitous) The recipe shall return a DynamoDB number as a JSON number.

**Deploying the API** (`main/run.sh`)

- **R20** (event) When `main/run.sh` runs, the recipe shall create the function `$PREFIX-api` from `main/handler.py`
  with Python 3.12 on arm64, 128 MB of memory and a timeout of 10 seconds.
- **R21** (ubiquitous) The recipe shall let the function read and update its three tables and write its own log
  group, and nothing else.
- **R22** (event) When `main/run.sh` creates the function URL, the recipe shall let a caller without AWS credentials
  call the function only through that URL.
- **R23** (event) When the URL is up, the recipe shall send it the five requests of an app: a trip, a rider's
  history, a claim, an accept, an unknown action.
- **R24** (event) When the five requests are sent, the recipe shall print the `REPORT` lines of the function's log
  group.
- **R25** (event) When `main/run.sh` ends without `KEEP=1`, the recipe shall remove the URL, the function, its log
  group and its role, the URL first.
- **R26** (optional) Where `KEEP=1` is set, the recipe shall leave the API in place and print the clean-up command.
- **R27** (optional) Where `ROLE_ARN` is set, the recipe shall run the function as that role.
- **R28** (optional) Where `ROLE_ARN` is set, the recipe shall neither create nor delete a role.

**Second run**

- **R30** (event) When `main/run.sh` runs and the function of an earlier run exists, the recipe shall replace the
  function's code.
- **R31** (event) When `main/run.sh` runs and the function of an earlier run exists, the recipe shall keep its role,
  its URL and its two permissions.
- **R32** (unwanted) If Lambda refuses a create because another change to the function is in progress, then the
  recipe shall try again 5 seconds later, in at most 6 attempts, and stop with Lambda's error after the 6th.
- **R33** (unwanted) If AWS refuses a create for a reason other than "already exists" and "in progress", then the
  recipe shall stop with that error before the next step.

**Starting a script**

- **R40** (unwanted) If DynamoDB answers that a table of the dynamodb recipe is not found, then the recipe shall
  exit with code 1 and print the command to run first.
- **R41** (unwanted) If AWS refuses the look for a table of the dynamodb recipe with any other error, then the
  recipe shall print that error and exit with code 1.
- **R42** (event) When a script runs with `DRY=1`, the recipe shall print its commands, also the five requests to
  the URL with `<url>` in its place, without a call to AWS or to the URL.
- **R43** (event) When `main/run.sh` starts, the recipe shall run the handler in the terminal against the tables
  of the dynamodb recipe before it creates anything.

**Course constraints**

- **R50** (ubiquitous) The recipe shall start the name of the function, of its role and of its log group with
  `$PREFIX` (default `recipe`).
- **R51** (ubiquitous) The recipe shall send its calls, and those of the handler it runs in the terminal, to
  `ca-central-1` unless `AWS_REGION` names another Region.

**Clean-up**

- **R60** (event) When `clean_up.sh --yes` runs, the recipe shall delete the URL, the function, its log group, the role's
  policy and the role of this `PREFIX`, the URL first.
- **R61** (unwanted) If AWS refuses a delete of `clean_up.sh`, then the recipe shall exit with code 1 after it has
  tried every other delete and printed `FAILED` for the refused one.
- **R62** (event) When `clean_up.sh --yes` runs and AWS answers "not found" for a delete, the recipe shall report that
  part as already gone, not as a failure.
- **R63** (unwanted) If the function, the log group or the role of this `PREFIX` is still there after the deletes,
  then the recipe shall print `FAILED: still there` with its name and exit with code 1.
- **R64** (optional) Where `ROLE_ARN` is set, the recipe shall leave that role in place in a clean-up.
- **R65** (ubiquitous) The recipe shall leave the tables of the dynamodb recipe in place.

**While the function runs**

- **R70** (unwanted) If a request's body is not JSON or not a JSON object, then the recipe shall answer HTTP 400
  without a call to DynamoDB.
- **R71** (event) When a request ends, the recipe shall write one JSON log line with the Lambda request id, the
  action, the HTTP status and the outcome in one word.
- **R72** (ubiquitous) The recipe shall keep the ids of riders, drivers and trips, and every other value a caller
  sent, out of its log lines.
- **R73** (ubiquitous) The recipe shall keep the function's log lines for 7 days unless `LOG_DAYS` names another
  number.
- **R74** (unwanted) If a request lacks a field its action needs, or has it in another form than a string of 1 to
  64 characters, then the recipe shall answer HTTP 400 with the name of the field, without a call to DynamoDB.
- **R75** (unwanted) If the `since` of a `rider` request is not a string of at most 64 characters, then the recipe
  shall answer HTTP 400 without a call to DynamoDB.
- **R76** (unwanted) If DynamoDB refuses a call of a request, then the recipe shall write the request's log line
  with the outcome `error` and the code of the error before the function fails.

**Requirements of the extensions**, each in the README of its folder:

- 80 to 82, redeploy: [`ext-redeploy/README.md`](ext-redeploy/README.md#requirements)
- 90 to 93, metrics: [`ext-metrics/README.md`](ext-metrics/README.md#requirements)
- 100 to 210, dashboard page: [`ext-dashboard-page/README.md`](ext-dashboard-page/README.md#requirements)
- 220 to 320, read path: [`ext-read-path/README.md`](ext-read-path/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine. "Sandbox terminal" is a terminal signed in to your team sandbox: AWS CloudShell, or
your laptop with AWS CLI v2, Python 3 and boto3. One file of this recipe runs inside AWS: `main/handler.py`, in
Lambda. Everything else runs in your terminal and sends requests.

| Part | Files | Laptop, offline | Sandbox terminal | Runs in AWS |
|---|---|---|---|---|
| Handler | `main/handler.py` | in the tests and scenarios, against moto | step 1 of `main/run.sh` runs it in your terminal against your tables | yes: Lambda runs this file, zipped as `handler.py`, for the direct call in step 5 and each request to the function URL in step 8 |
| Deploying | `main/run.sh`, `lib.sh` | `DRY=1` prints the commands | yes: it creates the role, the function and the URL, and calls the URL with `curl` | no |
| Redeploy | `ext-redeploy/run.sh` | `DRY=1` | yes: it uploads the handler again | no |
| Logs and metrics | `ext-metrics/run.sh` | `DRY=1` | yes: it reads CloudWatch | no |
| Dashboard page | `ext-dashboard-page/` | the tests and scenarios against moto, the page in headless Chromium; `DRY=1` | yes: `run.sh` deploys, loads the counts, opens and closes the URL; `serve_local.py` shows the page | yes: `page.py` behind its URL, and in the lake variant `refresh.py` every hour |
| Read path | `ext-read-path/` | the tests and scenarios against a fake URL; `DRY=1` | yes: `run.sh` calls the URL with `curl` and reads the `REPORT` lines | no: it calls `main/handler.py` |
| Clean-up of the extensions | `clean_up.py` | the tests against moto | yes, with the `PREFIX` of the run | no |
| Clean-up | `clean_up.sh` | `DRY=1` | yes, with the `PREFIX` of the run | no |
| Tables | `../dynamodb/` | that recipe's dry run | that recipe's scripts, with `KEEP=1` | no: DynamoDB is a service AWS runs |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

This recipe does not have a container or an instance. For a page on Fargate or EC2, see
[`dashboard-chat`](../dashboard-chat/README.md).




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:UpdateItem`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:ListRoles`, `iam:PassRole`, `iam:PutRolePolicy`, `lambda:AddPermission`, `lambda:CreateFunction`, `lambda:CreateFunctionUrlConfig`, `lambda:DeleteFunction`, `lambda:DeleteFunctionUrlConfig`, `lambda:GetFunction`, `lambda:GetFunctionUrlConfig`, `lambda:InvokeFunction`, `lambda:ListFunctions`, `lambda:UpdateFunctionCode`, `logs:CreateLogGroup`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `logs:FilterLogEvents`, `logs:PutRetentionPolicy` |
| `ext-metrics/` adds | `cloudwatch:GetMetricStatistics` |
| `ext-dashboard-page/` adds, with `clean_up.py` (recorded by `tests/test_dashboard_page.py::test_operations_the_extension_calls`; a second run adds the two updates, tags and `PassRole` go with the creates) | `athena:CreateWorkGroup`, `athena:ListWorkGroups`, `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:ListTables`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:TagResource`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListRoles`, `iam:PassRole`, `iam:PutRolePolicy`, `iam:TagRole`, `lambda:AddPermission`, `lambda:CreateFunction`, `lambda:CreateFunctionUrlConfig`, `lambda:DeleteFunction`, `lambda:DeleteFunctionUrlConfig`, `lambda:GetFunction`, `lambda:GetFunctionUrlConfig`, `lambda:ListFunctions`, `lambda:PutFunctionEventInvokeConfig`, `lambda:RemovePermission`, `lambda:TagResource`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `logs:CreateLogGroup`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `logs:PutRetentionPolicy`, `logs:TagResource`, `scheduler:CreateSchedule`, `scheduler:CreateScheduleGroup`, `scheduler:DeleteSchedule`, `scheduler:DeleteScheduleGroup`, `scheduler:GetScheduleGroup`, `scheduler:ListScheduleGroups`, `scheduler:TagResource`, `sts:GetCallerIdentity`; `clean_up.py --check` also `tag:GetResources` (a refusal is fine: it lists by name instead); `bench.py` also `lambda:GetFunctionConfiguration` and `lambda:UpdateFunctionConfiguration` for its cold read |
| `ext-read-path/` adds | `dynamodb:BatchWriteItem`, `dynamodb:UpdateItem` and `dynamodb:Query` on `$PREFIX-rider-trips`, `lambda:GetFunctionConfiguration`, `lambda:UpdateFunctionConfiguration` |

### 1. Laptop, offline: the dry run

These commands do not need an AWS account. They need [uv](https://docs.astral.sh/uv/).

From your local device (laptop), in a terminal at the root of your clone:

```bash
cd lambda-dynamodb
uv sync
uv run pytest -q
uv run behave
DRY=1 bash main/run.sh
```

What you should see, line by line:

1. `uv sync` installs boto3, pytest, behave and moto (a library that stands in for AWS on your machine), and for the
   extensions pydantic, DuckDB and playwright.
2. `pytest` ends with a line like `100 passed` (28 of them the main scenario's).
3. `behave` ends with `0 failed`. One feature runs the dynamodb recipe's scripts and the handler against moto and
   takes about half a minute; without the AWS CLI it is skipped. The extensions' scenarios take about two minutes
   more; the two `@browser` examples are skipped unless Chromium is installed (`uv run playwright install chromium`).
4. The dry run prints the ten steps with every command under them, the five `curl` requests of step 8 with
   `<url>` where the function's address will be, and does not call anything.

Clean-up and cost: these commands do not create anything and are not billed.

### 2. Sandbox terminal: the tables, then the API

Run by us on 2026-11-14 in a course sandbox (`output/main.txt`, `output/clean-up.txt`).
Sign in to your team sandbox first and check that the account is the one you expect.

In a sandbox terminal:

```bash
aws sso login                                 # or the sign-in your team uses; not needed in CloudShell
aws sts get-caller-identity                   # the account you expect, before anything is created
```

1. In the sandbox terminal, choose one prefix for both recipes (replace `ana` with your name) and create the tables,
   keeping them:

   ```bash
   export PREFIX=recipe-ana AWS_REGION=ca-central-1
   cd dynamodb && KEEP=1 bash main/run.sh && KEEP=1 bash ext-riders-and-drivers/run.sh && cd ..
   ```

2. Go to this recipe, read the commands of a dry run, then run it. To keep the API up for the extensions, run
   `KEEP=1 bash main/run.sh` instead of the last line:

   ```bash
   cd lambda-dynamodb
   DRY=1 bash main/run.sh             # prints every command, runs nothing
   bash main/run.sh                   # removes the API at the end
   ```

   To run the function as a role you already have, put
   `ROLE_ARN=arn:aws:iam::111122223333:role/your-role` (your role's ARN) in front of it.

What you should see: the ten steps of [What you should see](#what-you-should-see). The two log-group commands of
step 4 (`create-log-group`, `put-retention-policy`) should print nothing. The script waits three times:
10 s for the new role, 5 s for the new URL, 15 s for the logs.

Clean-up and cost: the run without `KEEP=1` removes the API itself. After a `KEEP=1` run, in the same terminal with
the same `PREFIX`: `bash clean_up.sh` (it lists what it would delete), then `bash clean_up.sh --yes`, and `bash ../dynamodb/clean_up.sh` (it lists what it would delete), then `bash ../dynamodb/clean_up.sh --yes` when your team is done with the tables.
While the API is up it is public: anyone who has the URL can claim trips, and nothing limits how many copies of the
function run at once. A flood of calls is served, and billed, up to the account's own limit on concurrent runs
,
so do not leave it up overnight. One run is well under one cent
([Cost and clean-up](#cost-and-clean-up)).

### 3. Sandbox terminal: the extensions

Each extension's README has its own part, with the sign-in, the commands, what you should see, the clean-up and the
cost: [redeploy](ext-redeploy/README.md#run-it) and [metrics](ext-metrics/README.md#run-it). Both start from
`KEEP=1 bash main/run.sh`. Both were run by us on 2026-11-14 in a course sandbox, after a main run of the same day
(`output/ext-redeploy.txt`, `output/ext-metrics.txt`).

The extensions [dashboard page](ext-dashboard-page/README.md#run-it-live) and
[read path](ext-read-path/README.md#run-it-live) have their own "Run it live" parts; one run of each is captured in `output/ext-dashboard-page.txt`,
`output/ext-read-path.txt` and their clean-ups `output/clean-up-ext-*.txt`. Both write the ledger `state/ledger.jsonl`.
`uv run python clean_up.py --ext dashboard-page` (or `--ext read-path`) lists one extension's resources, and with
`--yes` removes them and keeps `main/`'s API; `uv run python clean_up.py --yes` removes what both made, then runs
`clean_up.sh --yes`, which removes `main/`'s API.

## What you should see

The captured run is [output/main.txt](output/main.txt) (2026-11-14, run with `KEEP=1` from the repository root;
account masked as 111122223333, the function URL's id as `<url-id>`).

| Step | Captured |
|---|---|
| 1 | five lines: the trip (0.5 and 1.0 read units), r-03's history, `claimed: false, held_by: d-7`, `accepted: false` with the driver's reason |
| 2, 3 | `role/recipe-api-role`; the policy as above |
| 4 | the two log-group commands do not print anything. The function output shows python3.12, arm64, 128 MB and `"Pending"` until ready |
| 5 | status 200 and the trip as JSON |
| 6, 7 | `https://<url-id>.lambda-url.ca-central-1.on.aws/`, auth `NONE`; two statements |
| 8 | HTTP 200 for the trip, the history, the refused claim and the cancelled accept; HTTP 400 for the unknown action |
| 9 | one `REPORT` line per call |

Reading the `REPORT` lines of that run:

```
Duration: 316.02 ms  Billed Duration: 810 ms  Memory Size: 128 MB  Max Memory Used: 92 MB  Init Duration: 493.18 ms
Duration:  24.70 ms  Billed Duration:  25 ms  Memory Size: 128 MB  Max Memory Used: 92 MB
Duration:   1.63 ms  Billed Duration:   2 ms  Memory Size: 128 MB  Max Memory Used: 93 MB
```

The first call started a new environment. **Init Duration** is that start-up (loading Python and boto3, creating the
clients), and it is billed, so 316 ms of work was billed as 810 ms; the start-up varies between runs (285 ms in the
earlier captured run, 493 ms here). Later calls
reused the environment and were billed for their own time only, rounded up to the next millisecond. Billing is on the
configured 128 MB, not the 92 to 93 MB used
([execution environment](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html): you are
charged for the start-up of a new environment).

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| redeploy | deploys a changed `main/handler.py` to the function, keeping its URL, role and logs | [`ext-redeploy/`](ext-redeploy/) | nothing; after `KEEP=1 bash main/run.sh` and after each edit of the handler, run `bash ext-redeploy/run.sh` |
| metrics | the function's log group, its `REPORT` lines, and the `Invocations` and `Duration` metrics of the last hour | [`ext-metrics/`](ext-metrics/) | nothing; after `KEEP=1 bash main/run.sh`, run `bash ext-metrics/run.sh` |
| dashboard page | a page of requests by zone with each zone's freshness, from its own function `$PREFIX-page`; a lake variant whose hourly job runs the Athena query; a public URL only during a demonstration; an optional slot list | [`ext-dashboard-page/`](ext-dashboard-page/) | nothing; it makes its own function, role and table |
| read path | p50, p95 and p99 of each read through the URL, warm, cold, after a quiet time and on a fresh copy, with a writer beside it; results in DuckDB | [`ext-read-path/`](ext-read-path/) | nothing; after `KEEP=1 bash main/run.sh`, it calls the URL and changes only its own test items and one variable, both undone |

## Cost and clean-up

Lambda in ca-central-1, arm64: 0.20 USD per million requests plus 0.0000133334 USD per GB-second. At 128 MB, a call
billed 50 ms is 0.00625 GB-seconds: about 0.28 USD per million calls in all, plus the DynamoDB units each call uses.
One run: well under one cent. The function's log lines are billed too: 0.55 USD per GB sent to CloudWatch Logs and
0.033 USD per GB-month stored, until the log group is deleted; one run writes a few kilobytes.
Prices from the AWS bulk price lists for `ca-central-1` (AWSLambda, AmazonCloudWatch).

In the terminal of the AWS run, in `lambda-dynamodb`, with the same `PREFIX`:

```bash
bash clean_up.sh                   # lists what it would delete; deletes nothing
bash clean_up.sh --yes             # after a KEEP=1 run: URL, function, log group, role
bash ../dynamodb/clean_up.sh       # lists what it would delete; deletes nothing
bash ../dynamodb/clean_up.sh --yes # the tables, when your team is done with them
```

After the extensions, `uv run python clean_up.py --yes` removes what their ledger lists (the page's URL first) and
then runs `clean_up.sh`; `--check` lists what is left by tag, schedule group and name.

The last checks look for the exact names this recipe made and print `(no ... left)`; a name still there is a
`FAILED` line. Captured: [output/clean-up.txt](output/clean-up.txt) (2026-11-14, after a second run of the same day).
Every delete is attempted: one that AWS refuses is printed as `FAILED`, and the script then ends with exit code 1.
Only the API's own "not found" counts as already gone.

**A run soon after a clean-up shows the earlier run too.** We ran the recipe twice on 2026-11-14 with the same
`PREFIX`, the second run seven minutes after the first run's clean-up. In the second run:

- `Invocations` for the last hour was 14, not 7. CloudWatch keeps a function's metrics under its name
  (`FunctionName=recipe-api`) after the function is deleted, and the new function has the same name: 7 calls of
  the first run (one direct call, five through the URL, one after the redeploy) and 7 of the second, inside the
  same hour.
- the new log group held the first run's `REPORT` lines. Step 2 of `ext-metrics` printed eight lines, and the
  first seven carry the request ids of the first run: six of them in `output/main.txt`, the seventh in
  `output/ext-metrics.txt`.

What we did not keep: the output of the first run's clean-up, and the main output of the second run, where step 9
printed 13 `REPORT` lines (the first run's seven, then its own six). `output/clean-up.txt` is the second run's.

Why the earlier lines were there is not known. The two explanations we can think of, and what speaks about each:

- The first clean-up did not delete the log group. Against it: its last check printed `(no log group left)`, and
  the second run's `create-log-group` would then have printed `(already there from an earlier run: kept)`; its
  main output had 106 lines against the first run's 99, which is the seven extra `REPORT` lines and no such line.
  Neither output is kept, so this is our reading of the run, not a capture.
- CloudWatch Logs returned the events of a deleted log group to `filter-log-events` on a new group of the same
  name. We did not find this in the CloudWatch Logs documentation.

If you need a clean log group for a measurement, use a new `PREFIX` for the next run.

## Adapt it

1. Copy `main/handler.py`. Write one Python function per access pattern of your app; keep a condition on every
   write two callers can race on.
2. Add an action to `lambda_handler` for each new Python function, with its fields in `FIELDS`, and nothing else.
3. Change the policy in `main/run.sh` step 3 to the actions and tables your handler uses. If you add an action to the handler
   without its IAM action, the action fails with `AccessDeniedException` because the role allows only what the policy names.
4. Before the URL leaves your team: keep it out of public pages and repositories, remove it when the demo is over, or
   switch to auth type `AWS_IAM` (callers sign their requests). With `NONE`, anyone with the URL can claim trips.
