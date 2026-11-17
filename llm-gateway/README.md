# Call the course LLM gateway with your course token

## What you build

A small Python client calls the course LLM gateway with your course Model Context Protocol (MCP) token. It lists the models your key may
call, sends one chat, and turns each refusal (budget, rate, token, model) into a plain message. Every request is
written out in full in [main/call_gateway.py](main/call_gateway.py), so you can copy it into your own code.

## Use it when

- Your project calls a model at all: every **generative AI first** feature, and any GenAI extension, goes through the
  course LLM gateway. The provider key does not reach a student.
- You want to know why a call was refused, and what to do about it.
- To read or run the gateway itself (its budget, rate limit and deploy), see the extension
  [`ext-deploy/`](ext-deploy/README.md). The course LLM gateway you call is deployed from the course's own version
  of this code, which can differ from `ext-deploy/`.

## Main scenario

`main/call_gateway.py`, on your laptop or in CloudShell (L16–L17):

1. Read `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` from the environment (the older names `GATEWAY_URL` and
   `COURSE_MCP_TOKEN` still work when the new ones are not set). The token goes only into the `Authorization`
   header.
2. Use `GET /models` to list the models your key may call.
3. Use `POST /chat/completions` with `max_tokens` (the most output tokens the reply may use) set to get the reply
   and the tokens it used.
4. On a refusal, read the error `code` and act on it: wait once for `Retry-After` after `rate_limit_exceeded` or
   `upstream_rate_limited`; stop on
   `budget_exhausted`, `invalid_api_key`, `key_revoked`, `key_expired` and `model_not_allowed`, because waiting does
   not change them.

For each request, the gateway checks your token, counts the request in your
key's current minute, and **reserves** the highest price the request can reach (its input, and `max_tokens` of
output) against your key's budget, in one conditional write. After the reply it charges the tokens the call really used and releases the rest of the
reserve.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| keys (R20, R21, R22) | your course MCP token from the environment, sent as the bearer key; never printed, logged or committed | the provider key stays in the gateway; your token is yours and can be revoked alone | course constraint: no keys in code; provider keys stay central (L16–L17; [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), Step 3. Add Model Router and Gateway, "Gateway") |
| max_tokens (R11, R14) | always sent (300 by default); a reply with no text and `finish_reason` `length` is printed as an error, not as an empty reply | the gateway reserves the price of `max_tokens` output tokens before the call; without it, it reserves 4,096, so a key near the end of its budget is refused sooner than it needs to be. `qwen3.8-27b` reasons before it answers, and a reply that spent the cap on reasoning does not have any text: printed as `''`, it reads as the model's answer | per-key budget with a reserve before the call ([ext-deploy/README.md](ext-deploy/README.md), "How a request flows") |
| retries (R31, R32) | one wait on a rate-limit refusal, the gateway's own (`rate_limit_exceeded`) or the model service's (`upstream_rate_limited`): wait for the seconds in `Retry-After`, at most 65. If the refusal does not name a wait, wait 5 seconds. Make one more attempt. No retry on any other refusal | the gateway's rate limit clears at the next minute; a spent budget or a bad token does not clear by waiting, and a retry loop on it is the runaway the limit exists to stop | per-key rate limit outside the caller's code (L16; Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html): budget limits as checks before a call, enforced outside the loop they bound) |
| model (R12) | `qwen3.6-35b-a3b`, the cheaper chat model, by default (`CHAT_MODEL` or `--model` changes it) | start with the cheaper model and move up only when an evaluation shows that its quality for the task is not acceptable | L16–L17; Agentic AI Lens [AGENTCOST02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp01.html) (the cheapest model of acceptable quality for a task) |
| what a call cost (R13) | when the gateway names the charge in the reply header `x-gateway-cost-micro`, the client prints it under the token counts | the tokens do not tell you the price: each model has its own, and the gateway rounds up. The gateway in `ext-deploy` sends the header for a reply that is not streamed; the course LLM gateway is a separate deployment and may not send it, and then the client does not print a cost line | Agentic AI Lens [AGENTCOST05-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost05-bp01.html) (know what each piece of work cost, not only the total on the bill). The cost line is a first step toward that: one call's charge, where the page asks for cost per task and per agent |
| no answer (R33) | a gateway that cannot be reached, does not send a byte for 250 seconds (the limit is on each wait for bytes, not on the whole reply; the gateway itself waits up to 240 s for a thinking model), or answers with something that is not JSON, ends the run with one line and exit code 1; nothing is sent again | a stack trace does not tell a student what to check; a chat that timed out may still have been charged, so sending it again without being asked could spend twice | Generative AI Lens [GENREL03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp01.html) (a layer between the user and the model that handles errors and fails gracefully) |
| plain HTTP (R10, R30) | `httpx`, one request per function | you see the exact request; any OpenAI client works the same way with the base URL and the token as its API key | L15 |

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
runs them offline, against the gateway's own code on `127.0.0.1`. The requirements of the extension are in its own
README, and their numbers go on from here: [the gateway itself](ext-deploy/README.md#requirements) from 50. When
you change what the recipe does, change the requirement and its scenario with it:
`python3 tools/check_requirements.py` (repo root) fails when they do not match.

**Main scenario**

- **R10** (event) When `main/call_gateway.py` runs, the recipe shall list the models the key may call, send one
  chat, and print the reply, its token counts and the request id.
- **R11** (ubiquitous) The recipe shall send `max_tokens` with every chat: 300 unless `--max-tokens` names another
  number.
- **R12** (ubiquitous) The recipe shall ask for the model `qwen3.6-35b-a3b` unless `CHAT_MODEL` or `--model` names
  another.
- **R13** (event) When a reply carries the header `x-gateway-cost-micro`, the recipe shall print what the gateway
  charged for the call.
- **R14** (unwanted) If a reply has no text and `finish_reason` `length`, then the recipe shall print "the model
  used its token cap before answering" in place of the reply, and exit with code 1.
- **R15** (ubiquitous) The recipe shall send `temperature` 0 with every chat, except to an OpenAI GPT or o-series
  model, which gets no `temperature`.

**Token**

- **R20** (ubiquitous) The recipe shall send the course token only as the bearer key in the `Authorization` header
  of its requests to `COURSE_GATEWAY_URL`.
- **R21** (unwanted) If `COURSE_GATEWAY_URL` or `KB_MCP_TOKEN` is not set, then the recipe shall exit with a message
  that names both, before any request is sent.
- **R22** (ubiquitous) The recipe shall keep the token out of every printed line.
- **R23** (unwanted) If `KB_MCP_TOKEN` or `COURSE_GATEWAY_URL` is not set, then the recipe shall read the older
  name in its place: `COURSE_MCP_TOKEN` for the token, `GATEWAY_URL` for the address.

**Refusals**

- **R30** (unwanted) If the gateway refuses a request, then the recipe shall print the HTTP status, the error
  code, the gateway's message and what to do about that code, and exit with code 1.
- **R31** (unwanted) If the gateway refuses a chat because of a rate limit (`rate_limit_exceeded`,
  `upstream_rate_limited`), then the recipe shall wait once, for the seconds in `Retry-After` (at most 65; 5 when
  the refusal names none), and send the chat once more.
- **R32** (unwanted) If the gateway refuses a request for any other reason (`budget_exhausted`, `invalid_api_key`,
  `key_revoked`, `key_expired`, `model_not_allowed`, `upstream_rejected`, `upstream_key_refused`,
  `model_allowance_used`, `model_allowance_ended`), then the recipe
  shall make no second attempt.
- **R33** (unwanted) If the gateway cannot be reached, sends no byte for 250 seconds, or answers with a body that
  is not JSON, then the recipe shall print one line that says so and what to check, and exit with code 1 without
  a second attempt.

**Course constraints**

- **R40** (ubiquitous) The recipe shall create no AWS resource and no file in its main scenario.
- **R41** (ubiquitous) The recipe shall run its tests and scenarios without a connection to any address other than
  `127.0.0.1`.

The main scenario does not need a clean-up step, because it does not create anything (the first course constraint above). The
extension's clean-up is in its own README.

**Requirements of the extension**, in the README of its folder:

- 50 to 121, the gateway itself: [`ext-deploy/README.md`](ext-deploy/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need a key or an AWS account. The course LLM gateway
runs in the course's account, never in yours: you only send it requests. The main scenario does not create anything in AWS.
The extension is the one part of this recipe that runs in AWS, and only if you deploy it.

| Part | Files | Laptop, offline | Laptop, with the course LLM gateway | In AWS |
|---|---|---|---|---|
| Client | `main/call_gateway.py` | against the gateway's own code on `127.0.0.1`, inside the tests and scenarios | yes: `COURSE_GATEWAY_URL` and your course token (`KB_MCP_TOKEN`) | no: it is a client. In your project it runs wherever your app runs |
| Course LLM gateway | not in your hands | no | in the course's account; you send it requests | not in your account |
| Gateway of your own | `ext-deploy/src/gateway/`, `ext-deploy/run.sh` | yes: `ext-deploy/tests/local_run.py` runs it with a fake upstream and moto | no | yes, if you deploy it: one Lambda function (`run.sh` starts `src/gateway/app.py` under uvicorn, behind the Lambda Web Adapter and a function URL), two DynamoDB tables, an SNS topic, a secret |
| Admin tool | `ext-deploy/src/gateway/admin.py` (`gw-admin`) | inside the tests and scenarios | no | it runs on your laptop with your sandbox credentials and calls DynamoDB, SNS and Lambda |
| Tests and scenarios | `tests/`, `features/`, `ext-deploy/tests/` | yes, and in CI | never against the course LLM gateway | no |

### 1. Laptop, offline: tests and scenarios

Run these from the repository root. They do not need a key or an AWS account, so you do not sign in.

```bash
cd llm-gateway
uv sync
uv run pytest -q
uv run behave
```

What you should see: `uv sync` installs the packages, the gateway's own among them. `pytest` ends with a line like
`13 passed`; `behave` ends with `0 failed`. Both run the client against the gateway's own code in this process.
The run did not spend anything and did not leave anything to clean up.

### 2. Laptop, with the course LLM gateway

Run the commands in `llm-gateway` on your laptop or in CloudShell. This part does not use your AWS account: there is no AWS
sign-in and no `aws sts get-caller-identity`. The sign-in is your course token.

1. Get `COURSE_GATEWAY_URL` (the gateway's base URL) and the model names from the course page or your team repo on
   github.students, where they are a repository variable. A repository variable does not reach your terminal:
   copy its value into the `export` line.
2. Paste your course MCP token at the prompt, then press Enter. It does not show.

```bash
export COURSE_GATEWAY_URL='PASTE-THE-BASE-URL'     # from the course page or your team repo; it ends in /v1
read -rs -p 'Course MCP token: ' KB_MCP_TOKEN && echo && export KB_MCP_TOKEN
uv run python main/call_gateway.py
uv run python main/call_gateway.py --model qwen3.8-27b --ask "Name one AWS region in Canada."
```

What you should see: the models your key may call, then one chat with its reply, its token counts and a request id
([What you should see](#what-you-should-see)). The code reads both values from the environment and never has the
URL written in.

When you are done, run `unset KB_MCP_TOKEN`. The two commands spend a few millionths of a CAD of the key's budget
([Cost and clean-up](#cost-and-clean-up)). Nothing was created.

### 3. Gateway of your own, in your team sandbox

The extension: [`ext-deploy/README.md`](ext-deploy/README.md) has its own "Where each part runs", its offline run
and its deploy, with the sign-in, the cost and the clean-up.

## What you should see

An offline run against the gateway's own code is in [output/main-offline.txt](output/main-offline.txt). Its models
and prices are the examples in `ext-deploy`. On the course LLM gateway you see the course models and CAD amounts. The
run below is refused on purpose (its test key has 0.0001 USD left); with your course key the chat usually prints a
reply and its token counts instead, as in the real run on the course LLM gateway in
[output/main.txt](output/main.txt) (2026-11-12, both commands above).

```
1. models this key may call
   example-embed, example-large, example-small
2. one chat on example-large, max_tokens 1000
   refused: HTTP 429 budget_exhausted
   budget spent: 0.000000 of 0.000100 USD used. Ask the person who issued your key for more.
   what to do: this request does not fit what is left of your budget: lower max_tokens, or stop and ask the teaching team
```

In the offline run a chat that is answered ends with one more line, `charged to your key: 0.000004 in your key's
currency (the gateway's own count for this call)`: the gateway of `ext-deploy` names the charge in a reply header. The course LLM gateway
is a separate deployment. If it does not send the header, that line is absent, as in `output/main.txt`.

Lower `max_tokens` to reserve less on the course LLM gateway. A request refused at 1000 can fit at 100; in the
offline run it did. The gateway's own rate refusal carries `Retry-After: 60`. A refusal does not use any budget.
Your reply text, token counts and request ids differ from run to run.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| deploy your own gateway | the gateway's source (Lambda function URL, DynamoDB keys and usage tables, SNS alerts), its 137 offline tests, and the SAM deploy steps | [`ext-deploy/`](ext-deploy/README.md) | Leave `main/` unchanged. Point `COURSE_GATEWAY_URL` at your deploy and use a key from `gw-admin issue`. |

## Cost and clean-up

The gateway charges a call's tokens against your key's budget. In the captured run the first chat on `qwen3.6-35b-a3b`
used 18 input tokens and 2 output tokens. At 0.05 and 0.10 CAD per million
tokens that is about one millionth of a CAD;
the gateway rounds the input part and the output part up to a millionth each, so the key was charged 0.000002 CAD
.
The second chat, on `qwen3.8-27b` with 59 tokens in and 73 out, cost 0.000021 CAD the same way. A request
that was sent and then times out, or gets a 5xx other than 503 or 529, keeps its whole reserve as spent in the
gateway of `ext-deploy`: the provider may have generated and billed up to `max_tokens`. That is one more reason to
set `max_tokens` to what the reply needs. The
main scenario does not create anything in AWS, so you do not need to clean anything up. `ext-deploy` states its own cost and clean-up.

`qwen3.8-27b` reasons before it answers, and the reasoning counts against `max_tokens`. The 300 of this script was
enough for the one-line question above. For a longer question the reply can come back empty, with `finish_reason`
`length`: a run of the `hybrid-retrieval-eval` recipe on 2026-11-14 got empty replies from this model with a cap of
150 and answers with 600.
The script then prints "error: the model used its token cap before answering" in place of the reply and exits with
code 1 (R14); run it again with `--max-tokens 600`.

## Adapt it

1. Copy `Gateway` from `main/call_gateway.py` into your app, or give an OpenAI client `base_url=COURSE_GATEWAY_URL` and
   `api_key=KB_MCP_TOKEN`. For Inspect, set `PROXY_BASE_URL` and `PROXY_API_KEY` and use the model name
   `openai-api/proxy/<model>`.
2. Set `max_tokens` to what your reply needs, not more: it decides how many requests fit in your budget.
3. Keep the rule in `chat_with_retry`: wait on the rate, never loop on the budget.
4. Show `ADVICE[code]` (or your own text for each code) to the person using your app.
