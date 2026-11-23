# Model routing: a small model labels the request, app code picks the path

## What you build

A router for a shop's help chat: the small model labels each request `lookup`, `search`, `hard` or
`out_of_scope`, and app code runs one fixed path per label. An order lookup and an out-of-scope question each use one
small-model call and never reach the large model. When the router's reply is not a label, or a path cannot do its
job, app code sends the request to `hard` and writes one line to a misroute log (a JSON-lines file). A valid label
that is wrong for the request is not logged. The routing eval finds those by comparing router labels with hand labels.

## Use it when

- Your project has the generative AI shape and you chose the extension "Model routing, cheap model first"
  (2 points),
  or you want a cheaper model for most requests, with the large model kept for the requests that need it.
- Your requests fall into a few kinds that need different work: some need a database read, some a search, some
  reasoning, some only a polite refusal.
- To route inside a retrieval app (cheap model first, strong model for hard questions, with answer accuracy
  measured per policy), see [`hybrid-retrieval-eval/ext-model-routing`](../hybrid-retrieval-eval/ext-model-routing/README.md). This recipe provides labels, fixed paths, a misroute log and a routing eval for any app.
- Lectures: L16 (model gateway, per-key budget), L17 (routing; workflows, where app code sets the paths).

## Main scenario

1. `main/run.py` reads the requests in `main/requests.txt` (or `--request`).
2. `main/app.py` sends each request as the user message, with the label definitions as the system prompt, to the
   small model through the course LLM gateway (`main/model.py`). The reply is capped at 8 tokens.
3. `main/router.py` turns the reply into one of four labels. Any other reply becomes `hard`.
4. `main/paths.py` runs the path for the label: `lookup` reads the order in app code with no model call; `search`
   matches the catalog and the small model writes the reply; `hard` sends the catalog and the policy to the large
   model; `out_of_scope` returns a stock reply with no further call.
5. A path that cannot complete the request, such as a lookup without an order number, returns a reason. App code sends the request to
   `hard` instead. The `hard` path does not have the order either: its system prompt allows only the catalog and the
   policy, and tells the model to say when they do not answer the question.
6. Every unknown label and every path that gave up writes one line to `output/misroutes.jsonl`. Every request,
   misrouted or not, writes one line without its text to `output/routes.jsonl`.
7. Each request prints its label, the path that ran, the model calls with their tokens and cost, and the reply.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Who decides (R10, R11) | The model returns a label. App code maps the label to a path and a model | The paths stay in code you can read, test and change. The model answers one narrow question | L17 (a workflow: app code sets the paths); [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), Step 3. Add Model Router and Gateway, "Router" |
| Which model routes (R10, R21) | The small model (`qwen3.6-35b-a3b`), temperature 0, reply capped at 8 tokens | The router runs on every request, so it must cost much less than the call it saves. In the run of 2026-11-12 every router reply of `qwen3.6-35b-a3b` came back as one of the four labels under the cap of 8 (`output/main-gateway.txt`, `output/eval-gateway.md`). A separate probe that day got `lookup` in 2 completion tokens with no reasoning tokens, at caps of 8 and 64 | Agentic AI Lens [AGENTCOST02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp01.html) (tiered model selection; a small model as pre-classifier, which must cost less than the price difference between the tiers) |
| Which model answers (R13, R14, R15, R16) | `lookup` and `out_of_scope`: none. `search`: small. `hard`: large (`qwen3.8-27b`) | The simplest model that does the job: a read by key does not need a model. A list of matches needs a short reply. Only reasoning needs the large model | AGENTCOST02-BP01 (match each task to the cheapest model of acceptable quality); L16 |
| Labels (R11) | Four, each defined in one line of the system prompt | Few labels with plain definitions are easy for a small model and easy to label by hand for the eval | [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), Step 3. Add Model Router and Gateway, "Router" (an intent classifier; routers should be fast and cheap) |
| Unknown reply (R12) | Becomes `hard`, and a misroute line is written | A reply that is not a label then costs one large-model call instead of reaching the wrong path; the log shows how often it happens. A valid but wrong label is not caught here: the routing eval measures those | Fail toward the path that can answer anything (course default) |
| Path that gives up (R17) | Returns a reason. The request goes to `hard`, and a misroute line is written. Three cases: a lookup without an order number, a search that does not match a product, a search whose reply from the small model is empty (that call still counts in the request's cost) | A lookup without an order number must not guess an order; an empty reply would reach the customer as a blank answer | Course choice; AGENTCOST02-BP01 (on a failure, move up a tier instead of repeating the same call); GENREL03-BP01 (check that the model responded with the expected content) |
| Out of scope (R16) | Stock reply, no further model call | The request does not reach the large model, and the reply cannot drift off topic | [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "Router": an intent classifier keeps the system out of out-of-scope conversations, and a stock response answers without a model call; AGENTCOST02-BP01 |
| Misroute log (R12, R17, R31) | One JSON line per misroute: time, request id, request, label, reason, where it was sent | Each line is a request your prompt or your paths do not cover yet; the eval set grows from them | AGENTCOST02-BP01 (refresh routing decisions with data and exclude intuition); Generative AI Lens [GENOPS03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops03-bp02.html) (tracing of each step, to find where a workflow decides badly) |
| Route log (R32) | One JSON line per request, also for the ones that went well: time, request id, label, path, misrouted or not, error or not, large-model calls, cost, duration. No request text and no reply | The misroute log shows only what failed. How often each path runs, what each costs and how often the large model is reached need every request; a file without the users' words can be kept longer and shown to more people than the misroute log | Agentic AI Lens [AGENTOPS05-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html) (structured logs with named fields; logging only errors is its second anti-pattern; no personal data in logs); AGENTCOST02-BP01 (cost by tier, not only in total) |
| Output caps (R10, R14, R15) | Router 8, search 200, hard 600 tokens (course default) | A cap bounds what one call can cost; 600 leaves room for a model that reasons before it answers | Generative AI Lens [GENCOST03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp02.html) (a hard limit on response length; a short keyed answer where full text is not needed); L16 (per-key budget) |
| Model calls (R20, R22, R23, R24, R26, R28, R29) | Through the course LLM gateway with `KB_MCP_TOKEN` from the environment; an error status (401, 403, 429, 5xx), or 250 seconds without a byte of the answer (a time limit between bytes, not for the whole call; the gateway itself waits up to 240 s for a thinking model), ends that request with path `error` and the reason in `error`; the app goes on with the next request, `main/run.py` exits with code 1 at the end, and nothing is retried. A reply with status 200 that is not a chat completion (a proxy's error page, an `error` object, no choices) is a failed call too. After a 401 or 403 `main/run.py` stops: the key, or the model for this key, is wrong for every later request. 401 is a missing or unknown key, 403 a revoked or expired key or a model the key may not use, 400 `upstream_rejected` a parameter the model refused (change the request, do not resend it), 429 the rate limit or a spent budget | One key per student with its own budget and rate limit; a retry on a spent budget only fails again; a person in the chat should get an answer about the next request, not a stack trace; a call that timed out may still have been charged, so it is not sent again | Course constraint; L16 (model gateway); [`llm-gateway`](../llm-gateway/README.md); Generative AI Lens [GENREL03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp01.html) (on an error, retry or fail gracefully) |
| Cost per call (R18, R19, R25, R27) | Tokens times the gateway's price table (`main/model.py`), router included; a model that does not have a price there stops the run before any call | Routing pays only when the router's cost is smaller than what it saves; a model counted at 0 CAD would make every total and the eval's comparison wrong without a sign of it | AGENTCOST02-BP01 (track cost across tiers) |
| Tests (R30) | A stub model with hand-written labels (`main/stub_replies.json`); the gateway client against a fake transport | Tests pass offline, with no key; the real request shape is still checked | Offline tests after a clone (course constraint) |

## Requirements

Each requirement states what the recipe must do in one sentence, with "the recipe" as the subject and one "shall". The sentences follow
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
[routing eval](ext-eval/README.md#requirements) from 50. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Main scenario**

- **R10** (event) When a request arrives, the recipe shall send it to the small model as the user message, with
  the label definitions as the system prompt and `max_tokens` 8.
- **R11** (event) When the router's reply is one of the four labels (`lookup`, `search`, `hard`, `out_of_scope`),
  in any capitals and with punctuation or markup around it (`lookup!`, `(lookup)`, `**lookup**`), the recipe shall
  run the path of that label.
- **R12** (unwanted) If the router's reply is not one of the four labels, then the recipe shall run the `hard`
  path and write one misroute line with the first 80 characters of the reply.
- **R13** (event) When the label is `lookup`, the recipe shall answer from the order table in app code, with no
  model call after the router's.
- **R14** (event) When the label is `search`, the recipe shall answer with one call to the small model, with
  `max_tokens` 200 and at most 3 matching products in the user message.
- **R15** (event) When the label is `hard`, the recipe shall answer with one call to the large model, with
  `max_tokens` 600 and the catalog and the policy in the system prompt.
- **R16** (event) When the label is `out_of_scope`, the recipe shall answer with the stock reply, with no model
  call after the router's.
- **R17** (unwanted) If a path cannot do its job (a lookup without a four-digit order number, a search that matches
  no product, a search whose reply from the small model is empty), then the recipe shall run the `hard` path
  instead and write one misroute line with the reason.
- **R18** (event) When a request ends, the recipe shall return its label, the path that ran, the reply, the error
  (empty when there was none), every model call with its model, tokens and cost in CAD, the number of large-model
  calls and the total cost.
- **R19** (event) When a run of `main/run.py` ends, the recipe shall print the number of requests, the large-model
  calls, the total cost in CAD and the number of misroutes with the path of the log.

**Model calls**

- **R20** (ubiquitous) The recipe shall send every model call of a gateway run to `$COURSE_GATEWAY_URL/chat/completions`
  with `KB_MCP_TOKEN` as the bearer key and `temperature` 0 (none for an OpenAI GPT or o-series model, which takes
  only its default).
- **R21** (ubiquitous) The recipe shall ask the gateway for `qwen3.6-35b-a3b` as the small model and `qwen3.8-27b`
  as the large model unless `SMALL_MODEL` or `LARGE_MODEL` names another.
- **R22** (unwanted) If the gateway answers a model call with an error status (401, 403, 429 or a 5xx), or with a
  reply that is not a chat completion, then the recipe shall end the request with path `error` and the reason in
  `error`, without a second attempt.
- **R23** (unwanted) If `COURSE_GATEWAY_URL` or `KB_MCP_TOKEN` is not set for a gateway run, then the recipe shall exit
  with a message that names both, before any request is sent.
- **R24** (unwanted) If the gateway sends no byte of an answer for 250 seconds, then the recipe shall end the
  request with path `error`.
- **R25** (ubiquitous) The recipe shall price a model call from its token counts at the gateway's price of the
  model id the call asked for.
- **R26** (ubiquitous) The recipe shall keep the course token out of every result, misroute line and printed line.
- **R27** (unwanted) If `SMALL_MODEL` or `LARGE_MODEL` names a model that has no price in the recipe's price table,
  then the recipe shall exit with a message that names the model, before any model call.
- **R28** (unwanted) If one or more requests of a run of `main/run.py` end in error, then the recipe shall exit
  with code 1 after it has handled the remaining requests.
- **R29** (unwanted) If the gateway answers a model call with 401 or 403, then the recipe shall stop the run of
  `main/run.py` at that request and say how many requests were not sent.

**Course constraints**

- **R30** (ubiquitous) The recipe shall run its stub run without an AWS account, a course token or a network
  connection.
- **R31** (event) When a run writes a misroute line, the recipe shall add it to the end of the log and leave the
  earlier lines in place.
- **R32** (event) When a request of a run of `main/run.py` ends, the recipe shall add one line to the route log
  with the time, the request id, the label, the path, the large-model calls, the cost and the duration, and
  without the request's text.

**Clean-up**

- **R40** (event) When `clean_up.py --yes` runs, the recipe shall delete the `.jsonl` logs in `output/` and leave the
  captured runs there.
- **R41** (event) When `clean_up.py` runs without `--yes` (or with `--dry-run`), the recipe shall print the names it would delete and delete
  nothing.
- **R42** (event) When `clean_up.py --yes` runs and no log is left, the recipe shall exit with code 0.

**Requirements of the extension**, in the README of its folder:

- Requirements 50 to 57 cover the routing eval: [`ext-eval/README.md`](ext-eval/README.md#requirements).

## Run it

### Where each part runs

"Laptop" means your own machine. You can also use AWS CloudShell. "Offline" does not need a key or an AWS account. The course LLM gateway
runs in the course's account, never in yours: you only send it requests. The recipe does not create anything in AWS: it
does not have a Lambda function, a Fargate task or an EC2 instance, and it does not create an AWS resource.

| Part | Files | Laptop, offline | Laptop, with the gateway | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Routed app on the command line | `main/run.py`, `main/app.py` | yes, the default (`--model stub`) | yes, `--model gateway` | no |
| Router and paths | `main/router.py`, `main/paths.py`, `main/data.py` | inside the app's own Python process: they are not a service | the same | no; in your project they run wherever your app runs |
| Model | `main/model.py` | a stub: labels written by hand, no model | the course LLM gateway | no |
| Misroute log and route log | written by `main/app.py` | `output/misroutes.jsonl` and `output/routes.jsonl` on your disk | the same files | no |
| Routing eval | `ext-eval/routing_eval.py` | yes, stub labels | yes, `--model gateway` | no |
| Clean-up | `clean_up.py` | yes: it deletes local files only | the same | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against the gateway | no |

### 1. Laptop, offline: the dry run

Run these from the repository root. They do not need a key or an AWS account, so you do not sign in.

```bash
cd model-routing
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run
uv run python ext-eval/routing_eval.py
```

What you should see, line by line:

1. `uv sync` installs the packages.
2. `pytest` ends with a line like `31 passed`.
3. `behave` ends with `0 failed`.
4. The stub run prints one JSON line per request and a last line with the total, and writes two lines to
   `output/misroutes.jsonl` and six to `output/routes.jsonl`.
5. The eval prints `Label accuracy: 0.88` and two tables, and writes the same report over the captured
   `output/eval-stub.md` (`--out FILE` writes it elsewhere). With `--model gateway` it writes over
   `output/eval-gateway.md`, the captured real run: give `--out` if you want to keep that one.

Clean-up and cost: nothing was spent. `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` deletes the logs.

### 2. Laptop, with the course LLM gateway

From your laptop or AWS CloudShell, in a terminal in `model-routing`, run the commands below. The gateway does not require AWS sign-in or `aws sts get-caller-identity`. The
sign-in is your course token.

1. Get the gateway's base URL from the course page or from your team repo on github.students. Replace
   `YOUR-GATEWAY-BASE-URL` with it and keep the quotes.
2. `read` does not show a prompt: paste your course MCP token, then press Enter.

```bash
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN    # the token does not show and is not saved in the shell history
uv run python -m main.run --model gateway
uv run python -m main.run --model gateway --request "Is order 1043 packed yet?"
uv run python ext-eval/routing_eval.py --model gateway
```

The older names `GATEWAY_URL` and `COURSE_MCP_TOKEN` still work when the new ones are not set.

What you should see: the six lines of the offline run with labels and replies in the model's own words, then the
one line for order 1043, then the eval's report with the accuracy of the real router. If the gateway refuses a
call, that request's line has `"path": "error"` and an `error` field with the HTTP status and the gateway's
message. The run goes on with the next request and ends with exit code 1.
[`llm-gateway`](../llm-gateway/README.md) says what each refusal means. The eval stops at the first failed call
and does not write a report: a refused request does not have a label to compare.

The cap of 8 tokens was checked for `qwen3.6-35b-a3b` (Decisions). If you change `SMALL_MODEL`, check how the new
model replies to a short request with [`llm-probes`](../llm-probes/README.md) first: a model that reasons before it
answers can use the whole cap and return an empty reply.

Clean-up and cost: the three commands spent about 0.004 CAD of the key's budget in our run
([Cost and clean-up](#cost-and-clean-up)); `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` deletes the logs.

## What you should see

From `output/main-stub.txt` (stub model, offline), shortened:

```
{"request": "What is the status of order 1042?", "label": "lookup", "path": "lookup", "large_model_calls": 0, "reply": "Order 1042 is shipped: 2 item(s), 54.00 CAD.", ...}
{"request": "What will the weather be in Whistler tomorrow?", "label": "out_of_scope", "path": "out_of_scope", "large_model_calls": 0, "reply": "I can help with your orders and with our products. ...", ...}
{"request": "Where is my order?", "label": "lookup", "path": "hard", "large_model_calls": 1, ...}
model: stub; 6 requests; large-model calls 3; total 0.000183 CAD (stub: tokens estimated from characters); 2 misroutes logged in output/misroutes.jsonl
```

and in `output/misroutes.jsonl`:

```
{"ts": "...", "request_id": "...", "request": "Where is my order?", "label": "lookup", "reason": "lookup without an order number", "sent_to": "hard"}
{"ts": "...", "request_id": "...", "request": "Can I return boots I wore once on a hike?", "label": null, "router_reply": "Policy question", "reason": "router reply is not a label", "sent_to": "hard"}
```

and in `output/routes.jsonl`, one line per request, without its text:

```
{"ts": "...", "request_id": "...", "label": "lookup", "path": "lookup", "misrouted": false, "error": false, "large_model_calls": 0, "cost_cad": 6.15e-06, "ms": 0}
{"ts": "...", "request_id": "...", "label": "lookup", "path": "hard", "misrouted": true, "error": false, "large_model_calls": 1, "cost_cad": 5.015e-05, "ms": 0}
```

What holds on every run: the `lookup` and `out_of_scope` paths do not make a large-model call; a request labelled `lookup`
that the path cannot serve goes on to `hard` (the third line above); every reply that is not a label and every path
that gave up ends on `hard` and has a line in the log. The stub's labels are written by hand to show each branch;
they are not model output. With the gateway, the labels, the replies and the token counts come from the model and
change between runs. A real run of both gateway commands (2026-11-12) is in
[`output/main-gateway.txt`](output/main-gateway.txt): 6 requests, 2 large-model calls, 0.000304 CAD, one misroute
logged ("Where is my order?"). In that run the model labelled "Can I return boots I wore once on a hike?"
`out_of_scope`; a valid label is not logged, so that mistake shows only in the eval.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Routing eval | Label accuracy on 24 hand-labelled requests, per label; cost per request with every request on the large model against routed; an accuracy gate for CI (continuous integration) | `ext-eval/` | No file in `main/` changes: the eval calls `main.app.handle` and adds the all-large baseline, where the large model answers every request with the hard path's prompt |

## Cost and clean-up

One run of the six requests on the gateway (2026-11-12) made 9 model calls, 2 of them to the large model, and cost
0.000304 CAD of the key's budget. The eval on the gateway made 58
calls, 29 of them to the large model, and cost 0.0035 CAD (0.000843 routed and 0.002659 for the all-large baseline)
. Both at the gateway's prices: `qwen3.6-35b-a3b` 0.05 CAD per million
input tokens and 0.10 per million output; `qwen3.8-27b` 0.10 and 0.20
. The recipe does not create anything in AWS. On your laptop, in
`model-routing`, run these commands to list the local logs, check the list, then delete them:

```bash
uv run python clean_up.py    # what would go
uv run python clean_up.py --yes              # deletes the .jsonl logs in output/: misroutes, routes, eval-misroutes
```

## Adapt it

1. Write your labels and their one-line definitions into `ROUTER_SYSTEM` in `main/router.py`, and list them in
   `LABELS`. Keep them few.
2. Write one function per label in `main/paths.py` and add it to `PATHS`. A path that reads data does it in app
   code. A path that calls a model names which one.
3. Make each path return `misroute` with a reason when it cannot complete the request, such as a lookup without an order number.
4. Replace `main/data.py` with your data, or call your DynamoDB table or your MCP server from the paths.
5. Label 20 or more of your users' real requests by hand into `ext-eval/labelled.jsonl`. Run
   `uv run python ext-eval/routing_eval.py --model gateway` before and after every change to the router prompt, and
   compare the accuracy per label and the cost per request.
6. In your app, call `main.app.handle(text, models, log)` and show `reply`. Keep the misroute log and read the requests and reasons in it.
