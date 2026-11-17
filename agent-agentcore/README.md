# Agent with caps and approval in hooks, tools behind AgentCore Gateway, hosted on AgentCore Runtime

## What you build

A Strands agent that answers one request with tool calls it chooses, while app code keeps the limits: caps on model
calls and tool calls, a deadline, a person's approval before a write, and a write that happens once. The tools are a
Lambda function behind an AgentCore Gateway, the agent runs on AgentCore Runtime, and you can see each request as a
run report, as an Inspect eval score and, behind a flag, as a trace in Logfire.

The example tools are a small library (search the books, read one book, read a member's holds, place a hold). They
are a template: you replace them with the tools of your own system ([Adapt it](#adapt-it)).

## Use it when

Your project has an agent that calls tools over your data and one or more of the tools change something. It serves
the generative-AI-first shape, and the "Agent under controlled autonomy" extension of the other shapes.

- For a worked data example of the same agent (a lake, Athena and a catalogue behind the tools), see the lecture
  repository [l18-agent](https://github.com/cpsc436c-2026w1/l18-agent).
- For tools without an agent of your own (an assistant such as Claude Code calls them), use
  [mcp-server-minimal](../mcp-server-minimal/).
- For a fixed sequence of model calls, where code and not the LLM chooses each step, you do not need an agent
  framework: [dashboard-chat](../dashboard-chat/) and [model-routing](../model-routing/) show that shape.

## Main scenario

1. A member sends a request ("Which is shorter, Dune or Emma? Place a hold on the shorter one for me."). `main/run.py`
   builds the settings object once and starts the agent with that member and a request id.
2. Strands runs the loop: it sends the system prompt, the user message and the tool definitions to the LLM on the
   course LLM gateway, and the LLM returns a tool call or a reply.
3. Before every model call and every tool call, Strands calls the hooks of `main/hooks.py`. `CallCap` counts the call
   and cancels it past the cap. `ContractCheck` validates the arguments of the tool call against the tool's pydantic
   model.
4. A read tool runs. On AWS the call goes to the AgentCore Gateway, which invokes the tools Lambda function; the
   function reads DynamoDB and returns the tool result.
5. Before a write tool, `WriteGuard` sets the member of the request and the request key in the arguments and
   interrupts the agent. The caller gets status `needs_approval` with the book.
6. The person answers. On a yes the same agent resumes and the write runs: one conditional write in DynamoDB, keyed
   by the request key. On a no the LLM gets "The member declined" as the tool result.
7. The LLM returns the reply. The run report has the counts, the tools that ran and the tool calls that were refused.
8. On AgentCore Runtime, steps 1 to 7 are two calls in one Runtime session: a `request` payload, then an `approve`
   payload that resumes the agent that waits in that session's microVM.
9. `clean_up.py` removes everything the deploy created.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Agent framework (R10–R23) | [Strands Agents](https://strandsagents.com/latest/) 1.64.0, pinned | Its hooks run before every model call and tool call, and a hook can interrupt the agent for a person's answer and resume it. It is the SDK run in L18 | L18; Strands docs, "Hooks" and "Interrupts" |
| Alternative frameworks | Not built here. [Pydantic AI](https://pydantic.dev/docs/ai/overview/) validates tool arguments and outputs with pydantic models and recommends Logfire, the two tools this recipe already uses. The OpenAI Agents SDK, LangGraph, Google ADK and the Claude Agent SDK are the others compared in L18 | Ask of any SDK what L18 asked: where do the caps, the approval and the once-only write go, and can you see one request end to end? | L18, "Six agent SDKs compared" |
| Where the caps live (R10–R14) | `CallCap`, a hook in app code: model calls, tool calls, a deadline. Retries are off in the OpenAI client and in Strands, so one counted model call is one request to the gateway. The per-key budget of the course LLM gateway stays outside the app | The LLM does not see a hook and a prompt cannot switch it off. With the default retries, one counted model call sent three requests to a gateway that answered 500. A second limit outside the app still holds when the app's loop has a bug | L17 (bounded agent); Agentic AI Lens AGENTCOST07-BP01 (automated cost controls with cutoffs) |
| Approval before a write (R20–R23) | `WriteGuard` interrupts the agent before a write tool and resumes it with the person's answer; it also sets the member of the request, whatever the LLM wrote | A write changes someone's data, so a person decides, and the LLM cannot choose whose data | Agentic AI Lens AGENTSEC04-BP02 (human in the loop for critical decisions) |
| Policy in AgentCore | Not used. It attaches a policy engine to the Gateway and permits or forbids each tool call with Cedar policies, outside the agent's code | A policy at the Gateway holds for every agent that uses the Gateway; a hook holds for this agent and can ask a person. Add a policy when a second agent shares your Gateway | AgentCore docs, "Policy in Amazon Bedrock AgentCore" |
| Once-only write (R30–R32) | The hook computes a request key from the request id, the member and the book id; the store writes with a condition on that key | A retry, or an LLM that asks twice, sends the same key, and the store refuses the second row itself | L12–L13 (conditional writes); Agentic AI Lens AGENTREL06-BP04 (idempotent task execution) |
| Tool contracts (R40–R42) | One pydantic model per tool's arguments and per result, in `main/contracts.py`. The Gateway's tool definitions are generated from them, and a hook validates each tool call before it leaves the agent | One definition of each tool: the schema the LLM sees and the check cannot drift apart | Agentic AI Lens AGENTSEC02-BP02 (validate tool inputs and outputs) |
| What the tools Lambda function holds (R51) | `handler.py`, `store.py` and `tools.py`: the standard library and boto3 only, no pydantic | The zip stays a few kilobytes and does not need a build step; the arguments were validated on the agent's side | Course standard stack: handlers zipped as plain files |
| Tools behind a Gateway (R50–R52) | An AgentCore Gateway (MCP) with one Lambda target and `AWS_IAM` inbound authorization; the agent signs each request with SigV4 | The agent and any other MCP client get the same tools from one address, and only callers with `bedrock-agentcore:InvokeGateway` on this Gateway reach them | AgentCore docs, "AWS Lambda function targets" and "Set up inbound authorization" |
| Hosting (R60–R64) | AgentCore Runtime, direct code deployment: a zip in S3, Python 3.12. An agent that waits for an approval stays in the memory of its session's microVM | No container to build. AgentCore routes the calls of one session to one microVM, so the second call finds the agent. The trade-off: when the microVM ends (`RUNTIME_IDLE_TIMEOUT_S`), the waiting agent is gone and the approval gets an error | AgentCore docs, "Direct code deployment" and "Use isolated sessions for agents" |
| AgentCore SDK only in the Runtime's zip | `bedrock-agentcore` is in `runtime/requirements.txt`, not in `pyproject.toml` | It needs a newer botocore than Inspect AI's dependencies allow, so the two cannot share one environment | Offline first: the tests run without it, on a stand-in |
| Secrets (R61, R71) | `KB_MCP_TOKEN` and `LOGFIRE_TOKEN` are in `.env` on the laptop and in Parameter Store (SecureString) for the Runtime, which gets the parameters' names | A secret in a Runtime's environment variables can be read by anyone who can describe the Runtime | Course constraint: no keys in code, commits or outputs; course standard stack |
| Settings (R70–R73) | One frozen pydantic-settings object, built once in `main()` and passed on. `.env.example` lists every setting with its default and the source of the default; code does not hold a default | A value that is missing stops the run and names the variable; tests and a second deploy read the same values | Twelve-Factor App, "Config"; course settings standard |
| Traces (R80–R82) | Strands' OpenTelemetry spans go to Logfire when `LOGFIRE_ENABLED=true`; `send_to_logfire='if-token-present'` | Off by default, so an offline run sends nothing. With the flag on and no token, the spans stay on your machine | Logfire docs, "Configuration"; Strands docs, "Traces" |
| AgentCore Observability | Not set up. It delivers the Runtime's spans to CloudWatch after you turn on Transaction Search and add the AWS Distro for OpenTelemetry to the agent | One trace destination is enough for a project; choose CloudWatch when your alarms are there ([limits-and-alarms](../limits-and-alarms/)) | AgentCore docs, "Add observability to your AgentCore resources" |
| Eval (R100–R101) | An Inspect AI task: a dataset of requests with the tools and the holds each should end with, a solver that runs the whole agent, a scorer in code. Offline it runs on a stub model | The requirement and the eval are written before the agent, like the behave scenarios. A scorer in code gives the same score on every run | Course standard stack: an eval beside the EARS requirements and the behave scenarios; [Inspect AI](https://inspect.aisi.org.uk/) |
| Clean-up (R90–R93) | `clean_up.py` deletes by name, in the order the services need (the target before the Gateway), tries every step and exits 1 when one failed | A clean-up that stops at the first refusal leaves resources that bill | Course constraint: every recipe has a clean-up step |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets after a requirement's
number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an option is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline.

Caps:

- **R10** (ubiquitous) The recipe shall count the model calls and the tool calls of one request in a hook and give both counts in the run report.
- **R11** (unwanted) If the LLM asks for a tool call after `MAX_TOOL_CALLS` tool calls of the request, then the recipe shall return a refusal to the LLM as the tool result without running the tool.
- **R12** (unwanted) If a request has made `MAX_MODEL_CALLS` model calls, then the recipe shall cancel the next model call and end the request with status `stopped` and exit code 2.
- **R13** (unwanted) If a request has run longer than `DEADLINE_S` seconds, then the recipe shall cancel the next model call and end the request with status `stopped`.
- **R14** (unwanted) If the course LLM gateway refuses a model call, then the recipe shall end the request with status `error` and exit code 1 without sending that call again.

Approval:

- **R20** (event) When the LLM asks for a write tool, the recipe shall stop the agent before the tool runs and return status `needs_approval` with the book and the member.
- **R21** (event) When the person answers yes, the recipe shall resume the same agent and run the write.
- **R22** (unwanted) If the person answers no, then the recipe shall cancel the write and return "The member declined" to the LLM as the tool result.
- **R23** (ubiquitous) The recipe shall set the member in the arguments of every tool call to the member of the request.

Once-only write:

- **R30** (ubiquitous) The recipe shall give every write a request key computed from the request id, the member and the book id.
- **R31** (unwanted) If a write has a request key that the store already holds, then the recipe shall return the stored row with `duplicate_request` true and not add a second row.
- **R32** (unwanted) If a write arrives without a request key, then the recipe shall refuse it with an error result.

Tool contracts:

- **R40** (ubiquitous) The recipe shall build the tool definitions of the Gateway target from the pydantic models of the tools' arguments.
- **R41** (unwanted) If the arguments of a tool call do not match the tool's model, then the recipe shall return the validation problem to the LLM as the tool result without running the tool.
- **R42** (ubiquitous) The recipe shall return tool results that have the shape of the tool's result model.

Tools behind the Gateway:

- **R50** (event) When `main.run deploy` runs, the recipe shall create the tools Lambda function and a Gateway with protocol MCP, inbound authorization `AWS_IAM` and one Lambda target that lists the tools.
- **R51** (event) When the Gateway invokes the tools function, the recipe shall read the tool's name from the client context and return that tool's result.
- **R52** (state) While the tools are behind the Gateway, the recipe shall apply the same hooks to the tool names `<target>___<tool>`.

Hosting on the Runtime:

- **R60** (event) When `main.run deploy` runs, the recipe shall create the Runtime from a zip in S3 with environment variables that hold no secret value.
- **R61** (event) When `main.run deploy` runs, the recipe shall store the course token as a SecureString parameter in Parameter Store.
- **R62** (complex) While an agent waits for an approval in a Runtime session, when an `approve` payload arrives in that session, the recipe shall resume that agent.
- **R63** (unwanted) If an `approve` payload arrives in a session without a waiting agent, then the recipe shall return status `error`.
- **R64** (complex) While the resources of an earlier deploy exist, when `main.run deploy` runs, the recipe shall update them without creating a second Gateway, target or Runtime.

Settings:

- **R70** (ubiquitous) The recipe shall read every setting of `.env.example` from the environment into one settings object that code cannot change.
- **R71** (unwanted) If a required setting is not set, then the recipe shall stop before its first AWS or model call and name the setting.
- **R72** (ubiquitous) The recipe shall build every AWS resource name from `PREFIX`.
- **R73** (optional) Where `--model gateway` is used, the recipe shall send the model calls to the course LLM gateway at `COURSE_GATEWAY_URL` with `KB_MCP_TOKEN` as the key.

Traces:

- **R80** (state) While `LOGFIRE_ENABLED` is false, the recipe shall run without setting a tracer provider and without opening a network connection for traces.
- **R81** (complex) While `LOGFIRE_ENABLED` is true, when a request runs, the recipe shall produce one trace with a span for each model call and each tool call under the request's span.
- **R82** (state) While `LOGFIRE_ENABLED` is true and `LOGFIRE_TOKEN` is empty, the recipe shall keep the spans on the machine without opening a network connection.

Clean-up:

- **R90** (event) When `clean_up.py` runs, the recipe shall delete every resource the deploy created and print one line for each.
- **R91** (unwanted) If a delete is refused, then the recipe shall try the remaining deletes and exit with code 1.
- **R92** (state) While a role setting names a role, the recipe shall use that role without changing or deleting it.
- **R93** (event) When `clean_up.py --dry-run` runs, the recipe shall list what it would delete without deleting anything.

Eval:

- **R100** (event) When the Inspect task runs, the recipe shall score each request in code on the tools that ran and the holds at the end.
- **R101** (state) While the eval's backend is `stub`, the recipe shall run without a network call.

## Run it

### Where each part runs

| Part | Files | Laptop, offline | Laptop, against your team sandbox | In AWS |
|---|---|---|---|---|
| Agent and hooks | `main/agent.py`, `main/hooks.py`, `main/contracts.py` | yes, on the stub model | yes, `--model gateway` | on AgentCore Runtime |
| Tools | `main/tools.py`, `main/store.py` | yes, in memory | `--tools gateway`: through the Gateway | Lambda function behind the Gateway, two DynamoDB tables |
| Model | `main/model.py` | stub: no network call | the course LLM gateway | the course LLM gateway |
| Deploy and clean-up | `main/infra.py`, `clean_up.py` | no | yes | no |
| Eval | `evals/` | yes, on the stub model | `-T backend=gateway` | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |

### 1. Laptop, offline

No AWS account and no token. The settings are the lines of `.env.example`.

```bash
uv sync
uv run pytest -q
uv run behave
uv run --env-file .env.example python -m main.run ask --model stub --yes
uv run --env-file .env.example python -m main.run ask --model stub --stub runaway --yes
uv run --env-file .env.example inspect eval evals/task.py
```

### 2. Laptop, on the course LLM gateway

1. Copy the settings file: `cp .env.example .env`.
2. In `.env`, set `COURSE_GATEWAY_URL` (from the fuller `.env` in your team repository) and `KB_MCP_TOKEN` (your
   course token).
3. Run one request. It asks you to approve the write, and makes at most `MAX_MODEL_CALLS` model calls.

```bash
uv run --env-file .env python -m main.run ask --model gateway
uv run --env-file .env inspect eval evals/task.py -T backend=gateway     # 6 requests, at most 8 model calls each
```

### 3. Your team sandbox

1. In `.env`, set `PREFIX` to a name of your own (`recipe-<you>`), and leave the three role settings empty to let
   the recipe create the roles.
2. Sign in to the sandbox (`aws sso login`, then `aws sts get-caller-identity`).
3. Deploy, call the Runtime, then clean up.

```bash
uv run --env-file .env python -m main.run deploy
uv run --env-file .env python -m main.invoke                      # a request, your approval, the result
uv run --env-file .env python -m main.run ask --model gateway --tools gateway    # laptop agent, tools behind the Gateway
uv run --env-file .env python clean_up.py
```

To see traces, set `LOGFIRE_ENABLED=true` and `LOGFIRE_TOKEN` in `.env` before the run or the deploy.

### AWS permissions

What the identity that runs the deploy, the calls and the clean-up needs, in `ca-central-1`, on resources whose names
start with your `PREFIX`:

| Service | Actions |
|---|---|
| DynamoDB | `CreateTable`, `DescribeTable`, `PutItem`, `DeleteTable` |
| Lambda | `CreateFunction`, `GetFunction`, `GetFunctionConfiguration`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `DeleteFunction` |
| AgentCore (`bedrock-agentcore`) | `CreateGateway`, `GetGateway`, `ListGateways`, `DeleteGateway`, `CreateGatewayTarget`, `UpdateGatewayTarget`, `GetGatewayTarget`, `ListGatewayTargets`, `DeleteGatewayTarget`, `CreateAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntime`, `ListAgentRuntimes`, `DeleteAgentRuntime`, `InvokeAgentRuntime`, `InvokeGateway` |
| S3 | `CreateBucket`, `PutObject`, `DeleteObject`, `DeleteBucket` |
| Parameter Store (`ssm`) | `PutParameter`, `DescribeParameters`, `DeleteParameter` |
| CloudWatch Logs | `CreateLogGroup`, `PutRetentionPolicy`, `DescribeLogGroups`, `DeleteLogGroup` |
| IAM | `PassRole` for the three roles; and, when the recipe creates them, `CreateRole`, `GetRole`, `PutRolePolicy`, `ListRolePolicies`, `DeleteRolePolicy`, `DeleteRole` |
| STS | `GetCallerIdentity` |

If your sandbox does not let you create roles, ask for three roles with the policies of `role_policies()` in
`main/infra.py` and set `LAMBDA_ROLE_ARN`, `GATEWAY_ROLE_ARN` and `RUNTIME_ROLE_ARN`; the recipe then uses them and
leaves them alone. The roles the recipe creates are allowed only this: the function reads the two tables and writes
holds; the Gateway invokes the function; the Runtime writes its logs, reads the two parameters and invokes this
Gateway.

## What you should see

Offline, on the stub model ([`output/main-local.txt`](output/main-local.txt)):

```text
$ uv run --env-file .env.example python -m main.run ask --model stub --yes --request-id req-1
done: 4 model calls, 3 tool calls (ran: search_books, search_books, place_hold; refused: 0); approvals asked: 1
reply: I placed a hold on Emma for you.
holds: {"member": "sam", "holds": [{"book_id": "b02", "title": "Emma"}]}

$ uv run --env-file .env.example python -m main.run ask --model stub --stub runaway --yes --request-id req-1
stopped: 8 model calls, 6 tool calls (ran: search_books, ... ; refused: 2); approvals asked: 0
reply: Stopped by the app: cap of 8 model calls.
stopped by the app: cap of 8 model calls
```

What holds on every run, with any model: the model calls are at most `MAX_MODEL_CALLS`, the tool calls that ran are
at most `MAX_TOOL_CALLS`, a hold exists only after a yes, and it is on the list of the member who asked. With a real
model the number of calls, the words of the reply and the searches change between runs.

The eval on the stub model ([`output/eval-stub.txt`](output/eval-stub.txt)) ends with `accuracy 1.000` over 11
requests; five of them name a stub behaviour (a model that never stops, a write for another member, the same write
twice, wrong arguments, an instruction inside book text) and pass because a hook decides the outcome.

`output/` holds the offline runs only. The AWS steps pass on stand-ins (moto, and a stand-in for the AgentCore API
that checks every call against the service model in botocore); a capture from a sandbox is not in `output/` yet.

## Extensions

None yet. Two that fit: a Cedar policy on the Gateway (Policy in AgentCore), and AgentCore Memory for a
conversation that lasts longer than one Runtime session.

## Cost and clean-up

One deploy, a few requests and the clean-up cost under one cent in AWS charges.
What is billed:

- AgentCore Runtime: 0.0895 USD per vCPU-hour and 0.00945 USD per GB-hour, for the time a session's microVM is
  active; an idle session ends after `RUNTIME_IDLE_TIMEOUT_S`. AgentCore Gateway: 0.005 USD per 1,000 tool
  invocations.
- Lambda, DynamoDB on demand, S3, Parameter Store and CloudWatch Logs: a few requests each, inside the free tier or
  fractions of a cent.
- Model calls go to the course LLM gateway and count against your key's budget there, not against AWS. One request
  makes at most `MAX_MODEL_CALLS` calls.

Nothing bills while idle except the stored zip, the parameters and the logs. Remove everything:

```bash
uv run --env-file .env python clean_up.py --dry-run
uv run --env-file .env python clean_up.py
```

## Adapt it

- **Your tools.** Replace the library in three files: the pydantic models and the `TOOLS` table in
  `main/contracts.py`, the functions in `main/tools.py`, the tables in `main/store.py` (and `data/books.jsonl`).
  Name your write tools in `WRITE_TOOLS`: each of them then waits for approval and carries a request key. The
  Gateway's tool definitions, the argument check and the Lambda function follow from those files.
- **Your approval.** `WriteGuard` shows the person the book; change the `reason` it passes to `event.interrupt` to
  what your person needs to decide. In a web app, the `needs_approval` reply becomes a confirmation dialog and the
  answer the next call in the same Runtime session.
- **Who the member is.** The example takes the member from the request payload. In your app, take it from the
  signed-in identity, never from the LLM and never from text the user typed.
- **Your limits.** `MAX_MODEL_CALLS`, `MAX_TOOL_CALLS` and `DEADLINE_S` in `.env` are example values; set them from
  the longest task you want the agent to finish.
- **Your eval.** Replace `evals/requests.jsonl` with requests from your project, each with the tools and the end
  state you expect, and extend `evals/scoring.py` with what "correct" means for your data. Keep the checks that hold
  for any model (caps, writes only after a yes).
- **Your model.** `AGENT_MODEL` is any model on the course LLM gateway; run the eval with `-T backend=gateway`
  after you change it.
- **Data agent.** The lecture repository [l18-agent](https://github.com/cpsc436c-2026w1/l18-agent) is the same
  pattern over a data lake. **Other framework.** [Pydantic AI](https://pydantic.dev/docs/ai/overview/) is the
  closest alternative; the jobs of the hooks (caps, approval, request key) are what you would rebuild there.
