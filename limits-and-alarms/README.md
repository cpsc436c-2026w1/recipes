# Limits and alarms for your app: budget, concurrency, errors, latency and cost

## What you build

Your app's Lambda function writes one log line per request that CloudWatch turns into metrics: errors, latency and
the estimated model cost of that request. Three alarms watch those metrics and one dashboard shows them. Reserved
concurrency and a timeout set limits the app's own code cannot change, and an AWS Budgets budget sends alerts on the
account's spend (it does not stop anything).

## Use it when

- Your app makes model calls or runs in Lambda, and you want an alarm when it fails, slows down or spends faster
  than planned. Every project shape can use it. If your app does not run in Lambda, copy the logging and the alarms
  and leave out the function.
- The extension "Trace analysis and alarms" (the alarm half; traces per call are in
  `llm-state-dynamodb/ext-traces/`), and "Agent under controlled autonomy" (`ext-runaway-loop/`).
- Lectures: L16 (controlled autonomy, observability, per-key budgets), L17 (agent loops).
- For the per-key budget and rate limit themselves, read `llm-gateway`; this recipe relies on them and does
  not rebuild them.

## Main scenario

1. `main/alarms.py` creates the Amazon Simple Notification Service (SNS) topic `$PREFIX-limits-alarms`. The topic receives messages published by CloudWatch and AWS Budgets.
2. `main/limits.py` creates a monthly AWS Budgets cost budget, `$PREFIX-monthly`, with alerts at 50, 80 and 100 % of
   actual spend and 100 % of forecast spend. AWS Budgets sends the alerts to the topic.
3. `main/limits.py` creates the Identity and Access Management (IAM) role `$PREFIX-app-role`, which may only write the function's logs (or uses
   the role in `LAMBDA_ROLE_ARN`), deploys the app as the Lambda function `$PREFIX-app` with a 30 s timeout and
   reserves a concurrency of 5 for it.
4. On every request, `main/app.py` writes one line in embedded metric format (EMF: a JSON log line that also names
   the metrics in it) to standard output with `main/emf.py`, in a `finally` block, so requests that raise an error
   are counted too. A request that Lambda stops at its timeout does not reach the `finally` block and does not write
   the line.
5. CloudWatch Logs receives the line and CloudWatch turns its fields into the metrics `Requests`, `Errors`,
   `LatencyMs`, `CostCAD`, `TokensIn` and `TokensOut` in the namespace `$PREFIX/app`.
6. `main/alarms.py` creates three alarms on those metrics (error count, p95 latency and daily cost) and two on
   Lambda's own metrics of the function (`Errors` and `Throttles` in `AWS/Lambda`), which count a request that ends
   without its line. Each one notifies the topic.
7. `main/dashboard.py` creates the dashboard `$PREFIX-app` with the metrics, the Lambda throttles, the agent loop's
   tasks by stop reason (empty until the loop of `ext-runaway-loop/` runs) and the alarm states.
8. `main/fire.py` shows each alarm's notification in your sandbox, and the errors and latency conditions with
   `--synthetic`. Offline, `main/alarm_eval.py` checks the three alarms on the app's own metrics against synthetic
   metric lines. The two alarms on Lambda's own metrics are not checked offline: only Lambda writes those metrics.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Limits outside the code path (R20 to R23) | Budget, reserved concurrency and timeout live in account settings; the key's budget and the key's rate limit live in the course LLM gateway | A bug or a prompt that changes what the code does cannot raise them | L16 (controlled autonomy); Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html) ("cost controls outside the agent's control loop") |
| Fast stop for a runaway loop | The course LLM gateway's per-key budget and rate limit | The course LLM gateway refuses a call before it starts, in the same second | L16; [`llm-gateway`](../llm-gateway/README.md); AGENTCOST07-BP01 (budget limits as pre-invocation checks, not alerts after the fact) |
| What AWS Budgets is for (R20) | A monthly account budget of 25 USD sends alerts at 50, 80 and 100 % of actual spend and 100 % of forecast spend. The budget does not stop spending | Budgets reads billing data that is updated at least once a day, so an alert can come a day after the spend. It catches spend that goes on for days | [AWS Billing User Guide, Budgets best practices](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html) (billing data "updated at least once per day") |
| Concurrency (R22) | Reserved concurrency 5 | Excess requests are throttled instead of running up to the account's default of 1,000 copies of the function. Throttles show on the dashboard | [Lambda Developer Guide, reserved concurrency](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html) |
| When concurrency cannot be reserved (R23) | `main/limits.py` reads `GetAccountSettings` first and says so instead of failing | Lambda keeps 100 units unreserved, so a reservation of 5 needs 105 unreserved. A course sandbox reported a total of 400, 200 of them unreserved, on 2026-11-12; an account whose total is below 105 cannot reserve any | [PutFunctionConcurrency](https://docs.aws.amazon.com/lambda/latest/api/API_PutFunctionConcurrency.html) ("leave at least 100 simultaneous executions unreserved") |
| Run time (R21) | Lambda timeout 30 s | Lambda stops the function at the timeout, whatever the code does | Lambda Developer Guide |
| How metrics leave the app (R10, R15) | Embedded metric format: one JSON line per request on standard output | The request does not make a separate CloudWatch API call. The same line is also a log record you can search | [CloudWatch User Guide, Embedded metric format specification](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html); Serverless Applications Lens, [Metrics and alerts](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/opex-metrics-and-alerts.html) (EMF for custom metrics from Lambda) |
| Dimensions (R13) | The only dimension is `Service`. The request id stays a plain field | Every distinct dimension value creates one more custom metric, and each is billed as a custom metric | [CloudWatch User Guide, Optimizing and reducing costs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch_billing.html) (embedded metrics are billed as custom metrics, `MetricMonitorUsage`) |
| Estimated model cost per request (R12, R14) | Computed in app code from the tokens and the course LLM gateway's price table (`main/config.py`). A request for a model that is not in the table is refused before the work and counted in `Errors` | Model spend becomes a metric you can alarm on. It is an estimate from that table, not the bill, which is read the next day | Generative AI Lens [GENOPS02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops02-bp01.html) (monitor every layer of the application); AGENTCOST07-BP01 |
| Errors alarm (R30) | Sum of `Errors` over 5 minutes at 5 or more | Set the threshold from your app's normal error count | L16 |
| Latency alarm (R30) | p95 of `LatencyMs` (the latency that 95 % of requests stay under) above 5,000 ms in 2 of the last 3 one-minute periods | The slowest requests are the ones users wait for. The 2 of 3 setting tolerates a minute whose log lines arrived late | [CloudWatch User Guide, Setting alarms on metrics created with the embedded metric format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Alarms.html) ("M out of N" alarms where Lambda decides when logs are sent) |
| Daily cost alarm (R30) | Sum of `CostCAD` over 1 day above 0.50 CAD | You hear about a fast spend before the key's budget runs out. The alarm sums the whole app's estimated cost. It does not read any one key's balance. The course LLM gateway still enforces the key's budget | AGENTCOST07-BP01 (per-day limits) |
| Who may publish to the topic (R36) | The topic policy lets the services `cloudwatch.amazonaws.com` and `budgets.amazonaws.com` publish, each with the condition `aws:SourceAccount` = this account | Without the condition, a service acting for another account could publish to the topic. The policy does not stop people: a role or user of the same account whose own IAM policy allows `sns:Publish` on the topic can still publish to it | SNS Developer Guide, topic access policies |
| Lambda's own metrics (R39) | Two more alarms, on the function's `Errors` and `Throttles` in the namespace `AWS/Lambda`, each at 1 or more in 5 minutes | A request that Lambda stops at its timeout, or a handler that fails to import, does not write an EMF line, so the app's own `Errors` never counts it. Lambda counts it in its own `Errors`. A throttle means reserved concurrency turned a request away | Serverless Applications Lens, [Metrics and alerts](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/opex-metrics-and-alerts.html) (alarm on Lambda `Errors`, `Throttles`, `Duration` and `ConcurrentExecutions`) |
| Dashboard (R33) | One dashboard: requests and errors, p50 and p95 latency, spend, Lambda throttles and concurrency, the loop's tasks by stop reason, and the alarm states | Inspect the stop reasons to see which limit does the work. If a loop's tasks end at `step_cap` or `cost_cap` every hour, redesign the loop instead of raising a cap | Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html) (dashboards of budget use and cutoff activations); the dashboard holds 13 metrics, within the 50 of a free dashboard |
| Missing data (R31) | `notBreaching` | No traffic is not an outage | Course choice |
| IAM (R24, R25, R26) | The recipe creates `$PREFIX-app-role`, allowed only to write the function's own log group; an existing role of that name without the recipe's tag is refused, not reused; set `LAMBDA_ROLE_ARN` to use a role you already have instead | A role that can do one thing cannot be misused for another; a role someone else made may allow anything. `clean_up.py` deletes only the role this recipe created | IAM User Guide, grant least privilege; course policy: recipes may create IAM roles, since students have the IsbUsersPS administrator permission set within the organization's service control policy |
| What the recipe may change or delete (R24, R52, R55) | The function `$PREFIX-app`, the budget `$PREFIX-monthly`, the role `$PREFIX-app-role` and the topic `$PREFIX-limits-alarms` carry the tag `recipe=limits-and-alarms` from their creation (Lambda `Tags`, AWS Budgets `ResourceTags`, IAM and SNS `Tags`). A second run updates, and `clean_up.py` deletes, only one that carries it; one with the name and without the tag is refused, with a line that says why, and the function's log group stays with it | A name alone does not say who made a resource: a team's own function, budget or role with the same name would otherwise get this recipe's code or be deleted. AWS Budgets keeps tags on a budget and lists them by its ARN `arn:aws:budgets::<account>:budget/<name>`, so no local state file is needed. A budget made by an earlier version of this recipe has no tag: delete it in the console, or tag it with `aws budgets tag-resource` | AWS Budgets API Reference, [CreateBudget](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CreateBudget.html) (`ResourceTags`) and [ListTagsForResource](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_ListTagsForResource.html); Lambda [GetFunction](https://docs.aws.amazon.com/lambda/latest/api/API_GetFunction.html) (tags only for a caller allowed `lambda:ListTags`, so the recipe reads them with ListTags) |
| Tests (R38, R42) | The tests use moto for AWS. `main/alarm_eval.py` decides ALARM or OK the way a metric alarm does. The scenarios run the handler in-process for each Invoke | moto stores alarms but does not evaluate them, and would run a Lambda function in a Docker container. The tests still need to show each condition firing | Course constraint (offline tests) |

## Requirements

Each requirement is one sentence with "the recipe" as the subject and one "shall". The sentences follow
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
runs them offline. The requirements of the extension are in its own README, and their numbers go on from here:
[runaway loop](ext-runaway-loop/README.md#requirements) from 60. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Metrics from the app** (`main/app.py`, `main/emf.py`)

- **R10** (event) When a request to the function ends, with or without an error, unless Lambda stops it at its
  timeout, the recipe shall write one line in embedded metric format with `Requests`, `Errors`, `LatencyMs`,
  `CostCAD`, `TokensIn` and `TokensOut` in the namespace `$PREFIX/app`.
- **R11** (unwanted) If the work of a request raises an error, then the recipe shall answer with status 500 and
  count 1 in `Errors`.
- **R12** (unwanted) If a request names a model that has no price in the recipe's price table, then the recipe
  shall refuse it before the work and count it in `Errors`.
- **R13** (ubiquitous) The recipe shall give its metrics the one dimension `Service` and keep the request id a
  plain field of the line.
- **R14** (ubiquitous) The recipe shall price a request's tokens at the gateway's price of its model.
- **R15** (ubiquitous) The recipe shall write each metric line as one JSON object with nothing before or after it
  on its line.

**Limits** (`main/limits.py`, `main/run.py --target aws`)

- **R20** (event) When a run with `--target aws` sets up, the recipe shall create a monthly cost budget of 25 USD
  with alerts at 50, 80 and 100 % of actual spend and 100 % of forecast spend, sent to the alarm topic.
- **R21** (event) When a run with `--target aws` sets up, the recipe shall deploy the function `$PREFIX-app` with
  a timeout of 30 seconds.
- **R22** (event) When a run sets up and reserving 5 leaves at least 100 units of the account's concurrency
  unreserved, the recipe shall reserve a concurrency of 5 for the function.
- **R23** (unwanted) If reserving 5 would leave fewer than 100 units unreserved, then the recipe shall leave the
  function without a reservation and print `reserved concurrency NOT set`.
- **R24** (unwanted) If a function, a budget, a role or a topic with the recipe's name exists without the tag
  `recipe=limits-and-alarms`, then the recipe shall stop without changing or using it and say why.
- **R25** (event) When `LAMBDA_ROLE_ARN` is not set, the recipe shall create a role that only Lambda may assume and
  that may only write the function's own log group.
- **R26** (event) When `LAMBDA_ROLE_ARN` names a role, the recipe shall deploy the function with that role and
  create no role.
- **R27** (event) When a run with `--target aws` has set up, the recipe shall send 12 requests to the function,
  every sixth one meant to fail, and print how many it sent.
- **R28** (unwanted) If a request of the run breaks the function, then the recipe shall stop with the number of
  broken requests and the function's own error.

**Alarms, topic and dashboard** (`main/alarms.py`, `main/dashboard.py`, `main/fire.py`)

- **R30** (event) When a run sets up, the recipe shall create the alarms on errors (sum over 5 minutes, 5 or more),
  p95 latency (above 5,000 ms in 2 of 3 one-minute periods) and daily cost (above 0.50 CAD in a day), each
  notifying the topic on `ALARM` and on `OK`.
- **R31** (ubiquitous) The recipe shall treat missing data as not breaching in every alarm.
- **R32** (event) When `main/run.py --target local` runs, the recipe shall show, for the three alarms on the app's
  own metrics, each kind of synthetic trouble putting its own alarm, and no other, in `ALARM`, and normal traffic
  putting none in `ALARM`.
- **R33** (event) When a run sets up, the recipe shall create one dashboard with the request, latency, cost,
  Lambda throttle and loop stop-reason graphs and its alarms.
- **R34** (event) When `main/fire.py` runs, the recipe shall set each alarm to `ALARM`, report its state and
  whether its SNS action was sent, and set it back to `OK`.
- **R35** (event) When `main/fire.py --synthetic` runs, the recipe shall send breaching datapoints for the errors
  and latency alarms once a minute for 3 minutes and then report their states.
- **R36** (ubiquitous) The recipe shall grant the services CloudWatch and AWS Budgets permission to publish to the
  alarm topic only on behalf of the topic's own account.
- **R37** (event) When `ALARM_EMAIL` is set, the recipe shall subscribe that address to the alarm topic.
- **R38** (ubiquitous) The recipe shall decide an alarm's state offline as a metric alarm does: `ALARM` when at
  least its datapoints to alarm of its last evaluation periods breach, `OK` otherwise.
- **R39** (event) When a run sets up, the recipe shall create alarms on the function's own `Errors` and `Throttles`
  in `AWS/Lambda`, each at 1 or more in 5 minutes, notifying the topic.

**Course constraints**

- **R40** (ubiquitous) The recipe shall start the name of every resource it creates with `$PREFIX` (default
  `recipe`).
- **R41** (ubiquitous) The recipe shall create its resources in `ca-central-1` unless `AWS_REGION` names another
  Region, and its budget through the one AWS Budgets endpoint.
- **R42** (ubiquitous) The recipe shall run its local run and its scenarios without an AWS account, a network
  connection or a container.
- **R43** (ubiquitous) The recipe shall tag the function, the topic, the budget and the role it creates with
  `recipe=limits-and-alarms`.

**Clean-up**

- **R50** (event) When `clean_up.py` runs, the recipe shall delete the five alarms, the dashboard, the function,
  its log group, the budget, the topic and the role that this run created for this `PREFIX`.
- **R51** (event) When `clean_up.py --dry-run` runs, the recipe shall print what it would delete and delete
  nothing.
- **R52** (unwanted) If the function `$PREFIX-app` does not carry the recipe's tag, then the recipe shall keep it
  and its log group, print `FAILED` with the reason and exit with code 1 after the other steps.
- **R53** (unwanted) If AWS refuses a step of `clean_up.py`, then the recipe shall exit with code 1 after it has
  tried every other step and printed `FAILED` for the refused one.
- **R54** (event) When `clean_up.py` runs and a resource is already gone, the recipe shall report it as `absent`
  and exit with code 0.
- **R55** (unwanted) If the role `$PREFIX-app-role` or the topic `$PREFIX-limits-alarms` does not carry the
  recipe's tag, then the recipe shall keep it and print `FAILED` with the reason.
- **R56** (ubiquitous) The recipe shall leave in place every alarm other than its own five, also one whose name
  starts with the name of one of them.

**Requirements of the extension**, in the README of its folder:

- 60 to 68, runaway loop: [`ext-runaway-loop/README.md`](ext-runaway-loop/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need an AWS account: moto, a library that stands in
for AWS inside the Python process, answers every call. The course LLM gateway is not called by this recipe: the
function's "model call" is a stand-in that waits and counts tokens.

| Part | Files | Laptop, offline | Laptop, against your team sandbox | In AWS (Lambda) |
|---|---|---|---|---|
| Set-up: role, topic, budget, function, concurrency, alarms, dashboard | `main/run.py`, `main/limits.py`, `main/alarms.py`, `main/dashboard.py` | yes, `--target local` (moto) | yes, `--target aws` | no: it runs on your laptop and creates these in your sandbox |
| App | `main/app.py`, `main/emf.py`, `main/config.py` | in the scenarios, the handler runs in the scenario's own process | the 12 requests of `main/run.py` | yes: these three files are the zip of the function `$PREFIX-app`, handler `app.handler` |
| Alarm conditions | `main/alarm_eval.py` | yes: decides ALARM or OK from synthetic log lines | no: CloudWatch evaluates the alarms | CloudWatch, not a function of yours |
| Fire test | `main/fire.py` | `--target local` shows only the state change | yes, `--target aws`, with `--synthetic` for the conditions | no |
| Runaway loop | `ext-runaway-loop/` | yes: a stand-in gateway, a simulated clock | no part of it calls AWS or the course LLM gateway | not deployed by the recipe; in your project it runs in your handler |
| Clean-up | `clean_up.py` | no: there is nothing to remove | yes | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. The roles the recipe creates for AWS services have their own permissions, which the Decisions table describes. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `iam:TagRole`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `budgets:ListTagsForResource`, `budgets:ModifyBudget`, `budgets:TagResource`, `budgets:ViewBudget`, `cloudwatch:DeleteAlarms`, `cloudwatch:DeleteDashboards`, `cloudwatch:DescribeAlarmHistory`, `cloudwatch:DescribeAlarms`, `cloudwatch:ListDashboards`, `cloudwatch:ListTagsForResource`, `cloudwatch:PutDashboard`, `cloudwatch:PutMetricAlarm`, `cloudwatch:PutMetricData`, `cloudwatch:SetAlarmState`, `iam:CreateRole`, `iam:DeleteRole`, `iam:DeleteRolePolicy`, `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListRoleTags`, `iam:PassRole`, `iam:PutRolePolicy`, `lambda:CreateFunction`, `lambda:DeleteFunction`, `lambda:GetAccountSettings`, `lambda:GetFunction`, `lambda:GetFunctionConcurrency`, `lambda:InvokeFunction`, `lambda:ListTags`, `lambda:PutFunctionConcurrency`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `logs:DeleteLogGroup`, `logs:DescribeLogGroups`, `logs:ListTagsForResource`, `sns:CreateTopic`, `sns:DeleteTopic`, `sns:ListTagsForResource`, `sns:ListTopics`, `sns:SetTopicAttributes`, `sns:Subscribe` |

### 1. Laptop, offline: the dry run

Run these from the root of your clone. They do not need an AWS account, so you do not sign in.

```bash
cd limits-and-alarms
uv sync
uv run pytest -q
uv run behave
uv run python main/run.py --target local
uv run python ext-runaway-loop/loop_run.py
```

What you should see:

1. `uv sync` installs the packages.
2. `pytest` ends with a line like `45 passed`.
3. `behave` ends with `0 failed`.
4. The local run prints the set-up lines and the table under [What you should see](#what-you-should-see).
5. The loop prints the five tasks of the [extension's README](ext-runaway-loop/README.md#what-you-should-see).

Clean-up and cost: the offline run does not create or spend anything.

### 2. Laptop, against your team sandbox

Run on 2026-11-14 in a course sandbox, without `ALARM_EMAIL` (`output/main.txt`, `output/fire.txt`,
`output/fire-synthetic.txt`, `output/clean-up.txt`), with the two alarms on Lambda's own `Errors` and `Throttles`:
five alarms were made, each one's SNS action was sent, and `clean_up.py` removed eleven resources.

1. Sign in to your team sandbox: on your laptop, `aws sso login --profile <your sandbox profile>` and
   `export AWS_PROFILE=<your sandbox profile>` (your profile's name, without the angle brackets); or open AWS
   CloudShell, install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and clone the repository there.
2. In `limits-and-alarms`, check the account, then set your names and run the scenario. Replace `ana` with your own name
   first; to get the alarms by email, take the `#` off the `ALARM_EMAIL` line and put your address in it.

```bash
aws sts get-caller-identity                            # stop if this is not your team sandbox's account
export PREFIX=recipe-ana AWS_REGION=ca-central-1       # a prefix nobody else in the account uses
# optional: export LAMBDA_ROLE_ARN=arn:aws:iam::111122223333:role/your-role   otherwise the run creates $PREFIX-app-role
# optional: export ALARM_EMAIL=ana@example.com         then confirm the email SNS sends
uv run python main/run.py --target aws                 # sets everything up, sends 12 requests
uv run python main/fire.py --target aws                # each alarm's notification path
uv run python main/fire.py --target aws --synthetic    # the errors and latency conditions, about 5 minutes
```

What you should see: the set-up lines of `output/main.txt`, ending with `sent 12 requests (2 failing)`; then one
line per alarm with `state ALARM  SNS action sent`; then, after three minutes of breaching datapoints, the errors and
latency alarms in `ALARM`. `fire.py` without `--synthetic` uses `set-alarm-state`: it shows that the topic and your
email receive the alarm, not that the threshold is right, and the alarm goes back to its real state quickly, often
within seconds ([SetAlarmState](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_SetAlarmState.html)).
`--synthetic` sends breaching datapoints and waits for CloudWatch to evaluate them.

Clean-up and cost: [Cost and clean-up](#cost-and-clean-up). Run the clean-up the same day: the alarms and the custom
metrics are billed by the hour while they exist.

## What you should see

`output/main-local.txt` (offline run):

```
reserved concurrency 5 on recipe-app
alarms recipe-app-errors, recipe-app-p95-latency, recipe-app-daily-cost, recipe-app-lambda-errors, recipe-app-lambda-throttles
alarm states on ten minutes of synthetic traffic (main/alarm_eval.py):
  traffic    errors        p95-latency   daily-cost
  normal     OK            OK            OK
  errors     ALARM         OK            OK
  slow       OK            ALARM         OK
  expensive  OK            OK            ALARM
```

On the synthetic traffic of the offline run, each kind of trouble fires its own alarm and no other one, and normal
traffic does not fire any alarms. The two alarms on Lambda's own metrics are not in the table: only Lambda writes those metrics. In a sandbox, an alarm also needs the log lines to reach CloudWatch in time: check the alarm
states and the email after `fire.py --synthetic`. A run in a course sandbox on 2026-11-14 (no `ALARM_EMAIL`) is in
`output/main.txt`, `output/fire.txt`, `output/fire-synthetic.txt` and `output/clean-up.txt`: reserved concurrency was
set, every alarm's SNS action was sent, and after three minutes of breaching datapoints the errors and latency alarms
were in `ALARM`. That run used `fire.py` first, so the synthetic run started from alarms already set to `ALARM`.

In a sandbox, the first line says whether reserved concurrency was set. It is not set when the account's unreserved
concurrency is below 105; a course sandbox reported 200 unreserved on 2026-11-12, and the run of 2026-11-14 set it (`reserved concurrency 5 on recipe-app`). When it says `NOT set`, the
function runs without this limit, so do not rely on it to stop a flood of requests.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Runaway loop | An agent-style loop with a step cap, a deadline, a cost cap and a context cap in app code. When those limits fail, the stand-in gateway's per-key budget stops the loop. The stand-in represents the course LLM gateway | `ext-runaway-loop/` | The loop writes one EMF line per task with its stop reason; the daily-cost alarm sees a runaway's spend |

## Cost and clean-up

A sandbox run cleaned up the same day costs cents.
Prices in `ca-central-1`:

- A standard alarm is 0.10 USD per alarm metric month, billed by the hour while the alarm exists; the recipe makes
  five ([CloudWatch User Guide, alarm costs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch_billing.html)).
- A custom metric is 0.30 USD per metric-month for the first 10,000, prorated by the hour. EMF metrics are billed as
  custom metrics, and the log lines as log data ingested (0.55 USD per GB). This recipe creates up to twelve metrics:
  the six of each request line, and `AgentSteps` for each of the six stop reasons of the runaway loop.
- The free tier covers 10 custom metrics, 10 standard alarm metrics, 5 GB of log data ingested and 3 dashboards of
  up to 50 metrics each; a dashboard after those is 3.00 USD a month.
- A budget without actions is free; a budget with actions is free for the first two (0.10 USD a day after that), and
  this recipe's budget has none.
- Lambda requests are 0.20 USD per million, with the first million each month free; a run sends 12.

The function does not run on a schedule. In the terminal of the AWS run, with the same `PREFIX`, list what clean-up
would delete, check that every name starts with your prefix, then delete it:

```bash
uv run python clean_up.py --dry-run    # lists what it would delete
uv run python clean_up.py              # alarms, dashboard, function, log group, budget, topic, role
```

CloudWatch does not offer a call that deletes metric data: the metrics stop when the function is gone, and CloudWatch
keeps their datapoints for 15 months.

## Adapt it

1. Copy `main/emf.py` into your app and call `emit(request_line(...))` once per request, in a `finally` block, with
   your model's tokens. Keep the request id out of `Dimensions`.
2. Set the limits and thresholds in `main/config.py`: `MONTHLY_BUDGET_USD` to your sandbox's budget,
   `RESERVED_CONCURRENCY` to the traffic you expect, the three `ALARMS` to what your users would notice.
3. Point `ensure_function` in `main/limits.py` at your own function, or call only `ensure_concurrency` on it.
4. Add an alarm by adding a row to `ALARMS`; the tests then check that the app emits its metric.
5. Keep the model list and prices in `main/config.py` the same as those of the course LLM gateway, or the cost alarm measures the
   wrong thing.
