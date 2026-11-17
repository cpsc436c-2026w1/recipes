# Probe an LLM endpoint before you design on it

## What you build

> ⚠️ Run commands on your local device or in AWS CloudShell. The script runs there and calls the endpoint you select.

Build a script that sends one small request per feature to an OpenAI-compatible endpoint (the course LLM gateway, a provider,
or a model on your laptop) and prints a table: which models answer, and which features work there. It does not
change any setting on the endpoint. Its requests do use tokens on your key.

## Use it when

- Your project has any of the three shapes (lakehouse first, operational first, generative AI first) and one of
  its parts calls a model: a chat on a lakehouse dashboard as much as an agent.
- You start on a new endpoint or a new model, before you design a feature that depends on streaming, tool calls,
  structured output, the prompt cache or embeddings.
- A call that worked yesterday fails today, and you want to know whether the endpoint or your code changed.
- You are about to demo, and want a quick check first.
- To call the course LLM gateway from your app, use [llm-gateway](../llm-gateway/); this recipe only tests an endpoint.

## Main scenario

`main/probe.py` with `--groups main` (L15–L17):

1. `GET /models`: the models the endpoint lists for your key. Without a list the script stops with exit code 1.
   check the URL and the key first.
2. `GET /models` with a made-up key: it must be refused (401 or 403).
3. A chat with a model name that does not exist: it must be refused (4xx), not answered by some other model.
4. For each chat model, send one chat with a system prompt and a user message asking for one word. A model that does not
   answer is skipped for every other probe.

Without `--groups`, every extension runs after step 4 for each chat model, and the embeddings probe runs for each
embedding model (a model with `embed` in its name).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| test first (R10, R15, R16) | probe the endpoint before designing on it, and again before a demo | a feature that is listed may still not work on this endpoint or model. A `no` row lets you change the design before the demo | test what an endpoint supports (L15–L17) |
| refusals first (R13, R14) | a wrong key and an unknown model must be refused | an endpoint that does not refuse a wrong key must not be on the internet. A typo answered by another model hides a bug | L16 (the gateway checks the key and the model list of the key); Generative AI Lens [GENSEC01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gensec01-bp01.html) (least privilege access to model endpoints) |
| small requests (R32, R38) | one-word replies at temperature 0; the run's last lines give the tokens the endpoint reported for it | a whole run used 15,426 input and 707 output tokens in the captured gateway run, so it can run before every demo | course constraint: per-key budget; Agentic AI Lens [AGENTCOST05-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost05-bp01.html) (track token consumption per piece of work, not only on the bill). One run of the probes is a piece of work. |
| keys (R20, R21, R22, R23) | the script reads the key from `LLM_API_KEY` and the address from `LLM_BASE_URL`. It does not read them from a flag, a file or a second variable name. The key, and any piece of it of 8 characters or more, is masked as `[key]` where a reply arrives, before any text is cut or put in the table | an error message can echo the key back, whole or in part, and a note keeps only the first 110 characters of an error: if masking happens after the cut, part of a key that crosses the cut remains visible. A key for another service that happens to be set must not be sent to this endpoint | course constraint: no keys in code or in the repo |
| required checks (R37) | `--require` names the rows your app depends on; a required row that does not say `yes` for every chat model of the run, or was not run, makes the exit code 1 | a script before a demo or a deploy can stop on the exit code; without it the exit code says only that the endpoint listed its models | Agentic AI Lens [AGENTOPS01-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops01-bp03.html) (tests for the failure of a part you depend on, kept as checks that run by themselves). The probe tests the endpoint under the app. |
| compare two runs (R39) | `main/compare.py first.json second.json` lists the rows whose `works` differs, and exits with code 1 when one does | the comparison lists changed rows on the same endpoint over time or between two endpoints. By eye, a changed row among thirty is easy to miss | Agentic AI Lens [AGENTOPS02-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops02-bp02.html) (find drift before it causes an incident; check that two environments behave the same). The endpoint under the agent can change over time. |
| plain HTTP (R33) | each probe is one `httpx` request written out in full | you can copy the request that worked into your own code | L15 |
| one folder per capability (R18, R19) | `ext-streaming`, `ext-reply-control`, `ext-tool-calls`, `ext-structured-output`, `ext-prompt-cache`, `ext-embeddings` | copy only the probes your design depends on, and add your own beside them | course recipe format |

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
runs them offline, against a stand-in endpoint. The requirements of an extension are in its own README, and their
numbers go on from here: [streaming](ext-streaming/README.md#requirements) from 40,
[reply control](ext-reply-control/README.md#requirements) from 50,
[tool calls](ext-tool-calls/README.md#requirements) from 60,
[structured output](ext-structured-output/README.md#requirements) from 70,
[prompt cache](ext-prompt-cache/README.md#requirements) from 80,
[embeddings](ext-embeddings/README.md#requirements) from 90. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Main scenario**

- **R10** (event) When `main/probe.py` runs, the recipe shall print one table row per probe and model, with
  `works` (`yes`, `no` or `skipped`), the seconds and a note.
- **R11** (event) When a run starts, the recipe shall ask the endpoint for its models and list their names in the
  first row.
- **R12** (unwanted) If the endpoint returns no model list, or an empty one, then the recipe shall report `list
  models` as `no`, stop after that row and exit with code 1.
- **R13** (ubiquitous) The recipe shall report `wrong key is refused` as `yes` only when the endpoint answers a
  made-up key with 401 or 403.
- **R14** (ubiquitous) The recipe shall report `unknown model is refused` as `yes` only when the endpoint answers a
  model name that does not exist with a 4xx status.
- **R15** (event) When a chat model answers the one-word chat with text, the recipe shall report `chat` as `yes`
  with the reply and its token counts, reasoning tokens included.
- **R16** (unwanted) If a chat model does not answer the one-word chat (an error status, an empty reply), then the
  recipe shall report `chat` as `no` and skip that model's other probes with one `skipped` row.
- **R17** (ubiquitous) The recipe shall send no chat request to a model with `embed` in its name.
- **R18** (event) When `--groups` or `--models` is given, the recipe shall run only those groups, after the main
  group, and only those models.
- **R19** (unwanted) If `--groups` names a group the recipe does not have, then the recipe shall exit with a
  message that lists the groups, before any request is sent.

**Key**

- **R20** (ubiquitous) The recipe shall read the key from `LLM_API_KEY` and the address from `LLM_BASE_URL` in the
  environment, from no other variable and from no flag.
- **R21** (ubiquitous) The recipe shall send the key only as the bearer key in the `Authorization` header of its
  requests to `LLM_BASE_URL`.
- **R22** (ubiquitous) The recipe shall replace the key, and every piece of it of 8 characters or more, with
  `[key]` wherever it would appear in the printed table or in the `--json` file.
- **R23** (unwanted) If `LLM_BASE_URL` or `LLM_API_KEY` is not set, then the recipe shall exit with a message that
  names both, before any request is sent.

**Requests**

- **R30** (ubiquitous) The recipe shall wait at most 60 seconds for one reply unless `--timeout` names another
  limit.
- **R31** (unwanted) If a request gets no reply within the limit, or a reply with status 200 that is not in the
  expected format, then the recipe shall report that probe as `no` with `no reply` or `malformed reply` in the
  note and go on with the next probe.
- **R32** (ubiquitous) The recipe shall send every chat request with `temperature` 0 and `max_tokens` of 400 or
  less, and the one-word chat with `max_tokens` 200.
- **R33** (ubiquitous) The recipe shall send only `GET /models`, `POST /chat/completions` and `POST /embeddings`.
- **R34** (event) When `--json` names a file, the recipe shall write the rows to it as well.
- **R35** (event) When replies carry one of the headers `server`, `retry-after`, `x-ratelimit-*`,
  `x-litellm-version`, `x-litellm-attempted-retries`, `x-litellm-attempted-fallbacks`, `x-litellm-response-cost`,
  `x-litellm-key-spend` or `x-litellm-key-max-budget`, the recipe shall print its last value under the table.
- **R36** (ubiquitous) The recipe shall write no file except the one `--json` names.
- **R37** (unwanted) If a probe named in `--require` did not run, has a row that does not say `yes`, or (a probe
  of the chat groups) has no row for one of the run's chat models, then the recipe shall exit with code 1 and
  name that probe and model in the last line.
- **R38** (event) When a run ends, the recipe shall print the input and output tokens the endpoint reported for
  the run's requests, streamed replies included.
- **R39** (event) When `main/compare.py` is given two `--json` files, the recipe shall list each probe and model
  whose `works` differs between them, and exit with code 1 when the list is not empty.

The last requirement and the one about the three kinds of request are this recipe's clean-up: it creates nothing
on the endpoint. On disk, it creates only a `--json` file you asked for, so that is all you need to remove after a run if you asked
for.

**Requirements of the extensions**, each in the README of its folder:

- 40 to 42, streaming: [`ext-streaming/README.md`](ext-streaming/README.md#requirements)
- 50 to 52, reply control: [`ext-reply-control/README.md`](ext-reply-control/README.md#requirements)
- 60 to 62, tool calls: [`ext-tool-calls/README.md`](ext-tool-calls/README.md#requirements)
- 70 and 71, structured output: [`ext-structured-output/README.md`](ext-structured-output/README.md#requirements)
- 80 and 81, prompt cache: [`ext-prompt-cache/README.md`](ext-prompt-cache/README.md#requirements)
- 90 and 91, embeddings: [`ext-embeddings/README.md`](ext-embeddings/README.md#requirements)

## Run it

### Where each part runs

Run the script on your own machine or in AWS CloudShell. The script runs in the place you choose and nowhere else. What it calls is the
endpoint you name: the course LLM gateway (in the course's account, never in yours), a provider, or a model on
your laptop. The recipe does not use a Lambda function, a Fargate task or an EC2 instance, and it
does not create an AWS resource.

| Part | Files | Laptop, offline | Laptop, with the gateway or another endpoint | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Comparing two runs | `main/compare.py` | yes: it reads two `--json` files and does not send anything | the same | no |
| Probe script | `main/probe.py`, `main/endpoint.py` | it needs an endpoint: offline it stops with "Set LLM_BASE_URL and LLM_API_KEY" | yes: every request goes to `LLM_BASE_URL` | no |
| Capability groups | `ext-streaming/`, `ext-reply-control/`, `ext-tool-calls/`, `ext-structured-output/`, `ext-prompt-cache/`, `ext-embeddings/` | the same | yes, loaded by `main/probe.py`; they are not run on their own | no |
| Model on your laptop | Ollama, at `http://localhost:11434/v1` | yes: the probes then call only your own machine | not needed | no |
| Endpoint stand-in | `tests/fake_endpoint.py` | yes, inside the tests and scenarios | never | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against a real endpoint | no |

### 1. Laptop, offline: tests and scenarios

Run these from the repository root. They do not need a key or an AWS account, so you do not sign in.

```bash
cd llm-probes
uv sync
uv run pytest -q
uv run behave
```

What you should see: `uv sync` installs the packages. `pytest` ends with a line like `11 passed`. `behave` ends
with `0 failed`. Both use a stand-in endpoint inside the process. Nothing was spent and nothing is left to clean
up.

### 2. Laptop, with the course LLM gateway

Run by us on 2026-11-14 through the course LLM gateway: the three commands below
([output/main-gateway-2026-11-14.txt](output/main-gateway-2026-11-14.txt),
[output/all-probes-gateway-2026-11-14.txt](output/all-probes-gateway-2026-11-14.txt)). Every row says `yes`, as in
the run of 2026-11-11 that called the course's model provider directly. Three things differ through the gateway: the
two refusals carry the gateway's own messages (`HTTP 401: missing or malformed API key`, `HTTP 403: this key may not
use no-such-model. Allowed: ...`); there are no lines "Headers on the last replies", because the gateway passes none
of those headers on; and most requests took 0.2 to 0.7 s longer in this one run.

Run this part in `llm-probes`. It does not use AWS: you do not sign in to AWS or run `aws sts get-caller-identity`. The
sign-in is your course token. Run the main group first. Once the endpoint answers, run every group.

1. Get the course LLM gateway's base URL from the course page, or from your team repo on github.students, where it is a
   repository variable. A repository variable does not reach your terminal: copy its value into the `export`
   line, and keep the quotes.
2. From your local device or AWS CloudShell, run the commands below. `read` does not show a prompt text of its own beyond `LLM API key:`; paste your course MCP token, then press Enter.

```bash
export LLM_BASE_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -rs -p 'LLM API key: ' LLM_API_KEY && echo && export LLM_API_KEY  # it does not show and stays out of your history
uv run python main/probe.py --groups main          # the model list, the two refusals, one chat per model
uv run python main/probe.py                        # every group
uv run python main/probe.py --groups main,tool-calls --models qwen3.6-35b-a3b --timeout 20  # a model from the list models row
```

You should see a table with one row per probe and model
([What you should see](#what-you-should-see)); the first probe command prints four rows for one chat model, the second
all of them. `--json rows.json` also writes the rows to a file.

Clean-up and cost: every group on two chat models and one embedding model used 15,426 input tokens in the captured
run. The three commands together were billed 0.0014 CAD ([Cost and clean-up](#cost-and-clean-up)). The main group
alone used 94 input and 32 output tokens.
The probes create nothing on the endpoint. Delete any `--json` file you wrote and run `unset LLM_API_KEY`.

### 3. Laptop, with another endpoint

**Not run by us yet (2026-11-13)** against Ollama; the captured run in `output/` is the course's model provider,
called directly.

You do not need to sign in to AWS. From your local device, run the commands below. For Ollama on your laptop, any text works as the key
:

```bash
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_API_KEY=ollama
uv run python main/probe.py --groups main
```

What you should see: the same table for the models Ollama has pulled. An endpoint that does not need a key answers the
made-up key too, so `wrong key is refused` says `no` there: that is right for a model on your own machine and
wrong for anything on the internet.

Clean-up and cost: nothing is charged. Run `unset LLM_API_KEY LLM_BASE_URL` when you are done.

## What you should see

One row per probe and model. `works` is `yes`, `no` or `skipped`; the note says what came back. From the captured
run, [output/all-probes-2026-11-11.txt](output/all-probes-2026-11-11.txt) (the course's model provider (LTIC), called directly,
with two chat models and one embedding model, every group):

```
| probe | model | works | seconds | note |
|---|---|---|---:|---|
| list models |  | yes | 0.20 | qwen3-embedding-0.6b, qwen3.6-35b-a3b, qwen3.8-27b |
| wrong key is refused |  | yes | 0.08 | HTTP 401: LiteLLM Virtual Key expected. ... |
| chat | qwen3.6-35b-a3b | yes | 0.31 | replied 'ready'; 29 tokens in, 2 out |
| usage in the stream | qwen3.6-35b-a3b | yes | 0.27 | 29 tokens in, 2 out |
| tool call | qwen3.6-35b-a3b | yes | 0.38 | run_sql('SELECT COUNT(*) FROM trips') |
```

Under the table the run prints its own total: `Tokens the endpoint reported for this run: 15426 in, 707 out` in
the gateway run of every group; the run of 2026-11-11 predates that line. Your times and token counts will differ. On the same endpoint and model the `works` column usually stays the same
from one run to the next; when it changes, read the note: the endpoint or the model may have changed, which is what
the probe is for. If the endpoint is a LiteLLM proxy, the headers at the end show what the last call was billed, the key's
spend so far and its budget.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| streaming | a streamed reply, time to the first token, usage at the end of the stream | [`ext-streaming/`](ext-streaming/) | group `streaming`: two rows per chat model |
| reply control | the same reply twice at temperature 0; `max_tokens` stops a reply | [`ext-reply-control/`](ext-reply-control/) | group `reply-control`: two rows |
| tool calls | a tool call, then the tool result used in the reply | [`ext-tool-calls/`](ext-tool-calls/) | group `tool-calls`: two rows |
| structured output | a reply that fits a JSON schema, and whether the schema wins over the prompt | [`ext-structured-output/`](ext-structured-output/) | group `structured-output`: two rows |
| prompt cache | cached input tokens reported on a repeated long prompt | [`ext-prompt-cache/`](ext-prompt-cache/) | group `prompt-cache`: one row |
| embeddings | vectors that place a similar pair closer than an unrelated pair | [`ext-embeddings/`](ext-embeddings/) | group `embeddings`: one row per embedding model |

## Cost and clean-up

In the captured gateway run, every group for two chat models and one embedding model used 15,426 input tokens and
707 output tokens. The gateway billed
that run and the two smaller runs of the same key 0.00137 CAD together
.
Most of the input tokens are the prompt-cache probe: about 3,300 input tokens per call, two calls per chat model
. In the direct run of 2026-11-11 one of those calls on
`qwen3.8-27b` was billed 0.00035 in the endpoint's currency (the header `x-litellm-response-cost` of the last
reply), so the whole run was a little over a tenth
of a cent there too, about 0.0012 or somewhat more.
The main scenario alone used 94 input and 32 output tokens. The probes
do not create anything on the endpoint. When you finish, delete any `--json` file you wrote and run
`unset LLM_API_KEY`.

## Adapt it

1. **Add your own probe.** Copy a function from an extension (for example `probe_max_tokens` in
   `ext-reply-control/reply_control.py`), change the request to the feature your project depends on (an image in the
   user message, a longer context, a stop sequence), and add it to that module's `PROBES`, or make a new `ext-*`
   folder and add it to `CHAT_GROUPS` in `main/probe.py`.
2. **Run it before every demo or deploy.** Use the following command:
   `uv run python main/probe.py --models your-model --timeout 10 --require "tool call,usage in the stream"` takes a
   few seconds and exits with code 1 when a row your app depends on does not say `yes`; stop the deploy on that.
3. **Compare two endpoints, or one endpoint over time.** Set `LLM_BASE_URL` and `LLM_API_KEY` for the first and
   run with `--json first.json`, then set them for the second and run with `--json second.json`;
   `uv run python main/compare.py first.json second.json` lists the rows whose `works` differs. Compare the
   `seconds` columns by eye: one request is one sample, not a measurement of the endpoint's speed.
