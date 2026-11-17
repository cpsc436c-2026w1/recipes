# Workflow patterns: prompt chain, parallel calls, evaluator-optimizer

## What you build

Three short Python files, one per workflow pattern: a prompt chain with a code check between calls, parallel calls
(sectioning and voting), and an evaluator-optimizer loop (a model call or a code check as the evaluator). Each file
runs offline on a stub or against the course LLM gateway and prints every step, so you can see which calls were made
and what app code did with the replies.

## Use it when

Your project sends more than one model call for one request and you want the smallest working form of a pattern
before you build your own: generative-AI-first projects, and any project with a GenAI extension. In all three
patterns app code decides the next step; the model fills a step and never chooses one. That is what Anthropic's
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) calls a workflow.
When the model should choose the next tool call itself, start from the bounded agents in the lecture demo instead.

### Patterns in other recipes

| Pattern | Where | What to read |
|---|---|---|
| Augmented LLM | [dashboard-chat](../dashboard-chat/) | One model call with a tool: the LLM writes SQL, app code checks and runs it |
| Evaluator-optimizer, code check as evaluator | [dashboard-chat](../dashboard-chat/) `main/chat.py` | A query the SQL check rejects goes back to the LLM as the tool result, and the LLM writes it again within a step limit |
| Routing | [model-routing](../model-routing/) | A small model returns a label; app code runs the path written for that label |
| Orchestrator-workers | [l18-planning-agent](https://github.com/cpsc436c-2026w1/l18-planning-agent) `storeplan/`, `plain/orchestrator.py` | A planner call returns the subtasks; app code checks the plan and runs a worker for each |
| Bounded agent | [l18-planning-agent](https://github.com/cpsc436c-2026w1/l18-planning-agent) `plain/agent.py`, `triage/agent.py` | The model chooses each next tool call; call caps and an approval step stop or pause the loop |
| Prompt chain, parallel calls, evaluator-optimizer with a model as evaluator | this recipe | `main/chain.py`, `main/parallel.py`, `main/evaluator.py` |

## Main scenario

A small grocery shop answers customers. Each file is one pattern on that shop.

1. `main/chain.py`: call 1 rewrites a follow-up question ("Which of them have no dairy?") with the chat history so
   that it stands alone. Call 2 reads that question and picks products from the shop's list. A code check (the gate)
   stops the chain when the pick names a product that is not in the list. Call 3 writes the reply from the question
   and the picked products, with prices that app code read from the list.
2. `main/parallel.py`, sectioning: two different calls on one customer message run at the same time in a thread
   pool, a screen ("ok" or "off_topic") and a reply. App code shows the reply only when the screen says "ok".
3. `main/parallel.py`, voting: the same labelling call runs three times at the same time. App code takes the label
   that more than half of the votes name, or reports `no_majority`.
4. `main/evaluator.py`, model as evaluator: a generator call writes a shelf label, an evaluator call replies "pass"
   or "fail: ..." against written rules, and app code sends the feedback to the next generator call, for at most
   three rounds.
5. `main/evaluator.py`, code check as evaluator: the same loop, with the rules checked in app code.
6. `main/llm.py` makes every model call, on the stub or on the course LLM gateway, and refuses the call over the
   cap of the run.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Who decides the next step (R10, R20, R22, R30) | App code, in all three files | The order, the calls that run side by side and the end of the loop stay in code you can read and test | Anthropic, [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents), "Workflows" (predefined code paths); L17 |
| Chain steps (R10) | Three calls: rewrite, pick, answer | Each call has one narrow job, and the reply of one is the input of the next | [AI Engineering ch. 5](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch05.html), "Break Complex Tasks into Simpler Subtasks"; Building effective agents, "Workflow: Prompt chaining" |
| Gate (R11) | A code check between calls 2 and 3: every picked name is in the product list, else the chain stops and call 3 is not made | A product the shop does not sell never reaches the reply, and a stopped chain does not pay for another call | Building effective agents, "Workflow: Prompt chaining" (a programmatic check on an intermediate step) |
| Prices (R10) | App code reads them from the product list and puts them in call 3's user message | The model is not asked to remember a number that app code already has | [dashboard-chat](../dashboard-chat/) R10 (every value in a result comes from a query, not from the model) |
| Concurrency (R20, R22) | A thread pool around a blocking `httpx` call | A model call is a wait on the network, so threads are enough, and the chain and the loop stay plain sequential code | [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "AI Pipeline Orchestration"; Building effective agents, "Workflow: Parallelization" |
| Combining sections (R21) | App code: the reply is shown only when the screen call says "ok", else a stock reply | The rule that joins the two replies is one line of code, not a third model call | Building effective agents, "Workflow: Parallelization" (sectioning: one call answers, another screens) |
| Combining votes (R23, R24) | Three votes at temperature 0.7; the label needs more than half of the votes, else `no_majority` | At temperature 0 the three replies would be the same; a split vote is a result your app can act on (ask a person) | Building effective agents, "Workflow: Parallelization" (voting); three votes and 0.7 are example values, not measured |
| Evaluator (R30, R31) | Both forms behind one function shape `(draft) -> (passed, feedback)`: a model call with written rules, or a code check | A rule that code can check (a word count, a price) is exact and does not add a model call; a model call is for criteria code cannot check | [AI Engineering ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), "Reflection and error correction"; Building effective agents, "Workflow: Evaluator-optimizer" (its evaluator is a model call; the code check is the form [dashboard-chat](../dashboard-chat/) uses) |
| Verdict (R30) | A verdict that does not start with "pass" counts as a fail | An unclear verdict leads to one more round, not to an unchecked draft | A course default for this recipe |
| Loop limit (R32) | Three rounds, then the last draft is returned with `passed: False` | A loop that the evaluator never passes still ends; three is an example value | L17, L18 (limits outside the model) |
| Cap on model calls (R40) | `Model(max_calls=...)` counts calls under a lock and refuses the next one; `--max-calls N` sets it for a run | The cap holds for calls from several threads, and a mistake in a loop cannot spend the budget | L17, L18 (limits outside the model); [limits-and-alarms](../limits-and-alarms/) |
| Output tokens | `max_tokens` 1500 per call | `qwen3.8-27b` reasons before it answers, and the reasoning counts against the cap: a one-word label took 546 output tokens, 541 of them reasoning, and with a cap of 600 at least one of three votes came back empty | [dashboard-chat](../dashboard-chat/) Decisions, "Room to reason" |
| Settings | `pydantic-settings`: `COURSE_GATEWAY_URL`, `KB_MCP_TOKEN` (then `LLM_API_KEY`, `COURSE_MCP_TOKEN`), `LLM_MODEL`, from the environment or `.env` | No key in code; the token is a `SecretStr`, so printing the settings does not show it | Course constraint (AGENTS.md) |
| Shared file | The model call, the settings and the cap are in `main/llm.py`, not in each pattern file | Each pattern file stays under 80 lines and shows only its pattern | — |
| Failed call | The run prints `stopped: <reason>` and ends; no retry | A retry on a spent budget only fails again | [llm-gateway](../llm-gateway/) |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets after a requirement's
number names its EARS form.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall refuse the model call over the cap. |
| event | When <something happens>, the recipe shall ... | When voting runs, the recipe shall have three calls in flight. |
| state | While <something is the case>, the recipe shall ... | While the code check is the evaluator, the recipe shall make generator calls only. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If no draft passes, then the recipe shall stop the loop. |
| optional | Where <an extension is used>, the recipe shall ... | not used here: the recipe has no extensions |
| complex | two or more of these together | not used here |

Each requirement has a scenario in [`features/patterns.feature`](features/patterns.feature), tagged with its
number. `uv run behave` runs them offline on the stub.

**Prompt chain**

- **R10** (event) When the chain runs, the recipe shall make the rewrite call, the pick call and the answer call in
  that order, with the rewrite call's reply in the user message of the pick call and of the answer call.
- **R11** (unwanted) If the pick names a product that is not in the product list, then the recipe shall stop the
  chain before the answer call.

**Parallel calls**

- **R20** (event) When sectioning runs, the recipe shall have the screen call and the reply call in flight at the
  same time.
- **R21** (unwanted) If the screen call's reply is not "ok", then the recipe shall show the stock reply.
- **R22** (event) When voting runs, the recipe shall have three calls with the same system prompt and user message
  in flight at the same time.
- **R23** (event) When more than half of the votes name one label, the recipe shall report that label and how many
  votes agreed.
- **R24** (unwanted) If no label has more than half of the votes, then the recipe shall report `no_majority`.

**Evaluator-optimizer**

- **R30** (event) When the evaluator fails a draft, the recipe shall put the evaluator's feedback in the user
  message of the next generator call.
- **R31** (state) While the code check is the evaluator, the recipe shall make generator calls only.
- **R32** (unwanted) If no draft passes in three rounds, then the recipe shall stop the loop and report
  `passed: False`.

**Every file**

- **R40** (ubiquitous) The recipe shall refuse the model call that is over the run's cap on model calls.

## Run it

Everything runs on your laptop (or in CloudShell). Nothing of this recipe runs in AWS, and it does not create an AWS
resource. Offline, on the stub (no key, no network):

```bash
cd workflow-patterns
uv sync
uv run pytest -q
uv run behave
uv run python main/chain.py
uv run python main/parallel.py
uv run python main/evaluator.py
```

On the course LLM gateway, with your own course token. Copy `.env.example` to `.env` and fill it in, or export the
two names. The cap of each run is in its first printed line: 3, 5 and 4 model calls.

```bash
export COURSE_GATEWAY_URL=<base URL of the course LLM gateway, ending in /v1>
export KB_MCP_TOKEN=<your course MCP token>
uv run python main/chain.py --model gateway
uv run python main/parallel.py --model gateway
uv run python main/evaluator.py --model gateway --max-calls 4
```

### AWS permissions

None: the recipe does not call AWS.

## What you should see

`pytest` ends with `28 passed` and `behave` with `12 scenarios passed, 0 failed`. The stub runs print the same text
on every run ([`output/`](output/) has them as `*-stub.txt`); only `seconds` can differ.

On the course LLM gateway (`qwen3.8-27b`, 2026-11-17; the files `output/*-gateway.txt`). The wording of a reply
changes between runs. What holds: the order of the steps, the gate result for a pick from the list, and the number
of model calls up to the cap.

`main/chain.py --model gateway`:

```text
model: gateway; at most 3 model calls
standalone: Which of your milks are dairy-free?
pick: oat milk, almond milk
gate: passed
not_listed: []
reply: Our dairy-free milks are oat milk for 4.50 CAD and almond milk for 4.80 CAD.
model calls: 3
```

`main/parallel.py`, offline (each stub call sleeps 0.2 s, so two calls in 0.2 s and three calls in 0.2 s show that
they ran at the same time):

```text
model: stub; at most 5 model calls
sectioning
  screen: ok
  shown: Sorry about the late, open carton; yes, we have rye bread.
  seconds: 0.2
voting
  labels: ['complaint', 'complaint', 'question']
  label: complaint
  agreed: 2 of 3
  seconds: 0.2
model calls: 5
```

`main/parallel.py --model gateway`, sectioning (two calls, 3.7 s together):

```text
sectioning
  screen: ok
  shown: We're sorry about the oat milk, and yes, we still have rye bread.
  seconds: 3.7
```

Voting on the gateway is not captured yet. In the run of 2026-11-17 the three votes were sent, and at least one
came back empty because the model used its 600 output tokens on reasoning; the run stopped there
(`output/parallel-gateway.txt`: the lines the run printed, with the Python traceback cut to its last line under a
`voting` heading). The cap is now 1500 (Decisions, "Output tokens"), and voting has not been run on the gateway
since.

`main/evaluator.py --model gateway --max-calls 4` (the first draft passed both evaluators, so each loop ended after
one round: two calls with the model as evaluator, one with the code check; the model wrote the label on four lines):

```text
model: gateway; at most 4 model calls
model as evaluator
  passed: True
  rounds: 1
  label: Oat Milk
1 L
$4.50 CAD
From a farm in Abbotsford
  last_feedback: pass
code check as evaluator
  passed: True
  rounds: 1
  label: Oat Milk
1 L
$4.50 CAD
From a farm in Abbotsford
  last_feedback: 
model calls: 3
```

Offline, the stub's first draft is too long, so you see the loop: `rounds: 2` in both forms and `model calls: 6`
(`output/evaluator-stub.txt`).

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| none | | | |

## Cost and clean-up

One run of the three files on the course LLM gateway is at most 12 model calls and less than 0.004 CAD of your
gateway budget: an upper bound from the cap of 1500 output tokens per call and the price of `qwen3.8-27b` in
[model-routing](../model-routing/) (0.20 CAD per million output tokens), not a measured value. The recipe
does not create an AWS resource and does not write a file, so there is nothing to remove.

## Adapt it

- Your own chain: change `PRODUCTS`, the three system prompts and `gate()` in `main/chain.py`. Keep a code check
  wherever a wrong intermediate reply would make the next call pointless.
- Your own sections: add a `pool.submit(...)` per independent call in `sectioning()` and change the rule that
  combines them. For votes, change `VOTES`, the temperature and the majority rule.
- Your own evaluator: write the rules once (`RULES`) and choose the form: code when the rule can be written as
  code, a model call otherwise. Keep `ROUNDS` and decide what your app does with `passed: False`.
- Every example value (three votes, temperature 0.7, three rounds, 1500 tokens) is a starting point: measure on your
  own requests before you rely on it.
