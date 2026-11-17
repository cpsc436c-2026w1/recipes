# Runaway loop: limits in the loop, and the budget outside it

Read the recipe README first. This recipe serves the extension "Agent under controlled autonomy" and builds on `main/`.

## What it changes in the main scenario

`agent_loop.run_agent` runs an agent-style loop: model call, tool call, tool result back to the model, until the
model returns a reply. Before each model call it checks a step cap and a deadline. A refusal from the gateway (429
for budget or rate) ends the task without a retry. Each task writes one EMF line with its stop reason, its steps and
its spend, so the main scenario's daily-cost alarm can detect when that spend exceeds its threshold. `fake_gateway.py` stands in for the course LLM
gateway's per-key budget and rate.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Step cap (R60) | At most 8 model calls per task | A loop that does not make progress is stopped by count, whatever the model asks for | Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html) (automatic cutoffs at iteration thresholds) |
| Deadline (R61, R65) | The loop does not start a new model call after 20 s. In Lambda, `deadline_for` takes the smaller of that deadline and the time left minus 2 s | The function can still write its reply and its metrics before Lambda stops it at 30 s | AGENTCOST07-BP01; Lambda timeout (main scenario) |
| Where the checks sit (R60, R61) | Before each model call | By the time a check runs after the call, the call has already been paid for | AGENTCOST07-BP01 (budget limits as pre-invocation checks, not alerts after the fact) |
| Cost cap (R69, R70) | The loop does not start a model call that could take the task's spend past 0.01 CAD. It counts the most the call can cost: its context and `max_tokens` of output at the model's price | A step cap counts calls, not money: a task whose context grows can spend much more in its 8 calls than a short one. The cap holds per task, below the key's budget, which holds for every task of the key together | Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html) (cutoffs at iteration or cost thresholds; per-task limits as pre-invocation checks) |
| Context cap (R63) | Only the newest 10 tool results go back to the model | Every token in the context is paid again on each later call | AGENTCOST07-BP01 (memory growth guardrails: "every token in context is paid on every subsequent invocation") |
| Refusals (R62) | A 429 for budget or rate ends the task without a retry | A retry on a spent budget only fails again, and a fast retry loop is itself a runaway | L16 (budget and rate errors); [`llm-gateway`](../../llm-gateway/README.md) |
| Limit outside the loop (R66) | The course LLM gateway's per-key budget: the gateway reserves the highest price a call can reach and refuses the call when that price exceeds the remaining budget | When the cap and the deadline have a bug, the loop still stops, and the key is never overspent | L16; AGENTCOST07-BP01 ("outside the agent's control loop") |
| What the loop reports (R64, R68) | One EMF line per task: `StopReason`, `AgentSteps`, `CostCAD`; steps per stop reason in a second directive with one metric | A task stopped by a limit counts as an error. Per-reason counts show which limit is doing the work and create at most six more custom metrics | [CloudWatch User Guide, Embedded metric format specification](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html) |
| Tests | The tests use a simulated clock and a stand-in gateway. The stand-in gateway has its own example budget (1 CAD) and rate (60 a minute), which are not the course LLM gateway's. The tests use a stub model that always asks for another tool call | A runaway of about 18,900 calls runs in a fraction of a second and does not spend any money | Course constraint |

## Requirements

The extension's requirements go on from the recipe's (in the [recipe README](../README.md#requirements), with the
table of EARS forms). Their scenarios are in [`features/runaway.feature`](../features/runaway.feature).

- **R60** (optional) Where the runaway loop is used, the recipe shall stop a task before its 9th model call.
- **R61** (optional) Where the runaway loop is used, the recipe shall start no model call 20 seconds or more after
  the task started.
- **R62** (complex) Where the runaway loop is used, if the gateway refuses a model call with `budget_exhausted` or
  `rate_limited`, then the recipe shall end the task with that reason and without a second attempt.
- **R63** (optional) Where the runaway loop is used, the recipe shall send at most the newest 10 tool results back
  to the model.
- **R64** (optional) Where the runaway loop is used, the recipe shall write one metric line per task with its stop
  reason, its model calls and its cost, and the model calls per stop reason in a second directive.
- **R65** (complex) Where the runaway loop is used, when it runs in Lambda, the recipe shall end its deadline 2
  seconds before the time Lambda allows, or at 20 seconds if that comes first.
- **R66** (complex) Where the runaway loop is used, if the step cap, the deadline and the cost cap are all turned
  off, then the recipe shall stop the task at the key's budget, without spending past it.
- **R67** (complex) Where the runaway loop is used, when `loop_run.py` runs, the recipe shall print five tasks
  stopped by `done`, `step_cap`, `deadline`, `cost_cap` and `budget_exhausted`.
- **R68** (complex) Where the runaway loop is used, when a task spends more than 0.50 CAD in a day, the recipe shall
  put the daily-cost alarm in `ALARM` on its metric line.
- **R69** (optional) Where the runaway loop is used, the recipe shall start no model call that could take the task's
  spend past 0.01 CAD.
- **R70** (complex) Where the runaway loop is used, if the cost cap is on and the gateway client names no model or
  a model other than the one the cost cap prices, then the recipe shall stop before the first call with a message
  that names both.

## Run it

Run the following commands offline on your laptop in `limits-and-alarms`. This extension does not call AWS or the course
LLM gateway, so you do not need to sign in. Where each part runs: the [recipe README](../README.md#where-each-part-runs).

```bash
uv run python ext-runaway-loop/loop_run.py                       # EMF lines in output/loop-emf.jsonl (--emf FILE: elsewhere)
uv run behave features/runaway.feature
```

Clean-up and cost: nothing was spent; `output/loop-emf.jsonl` is the only file written.

## What you should see

`output/loop-local.txt`:

```
task                               stopped by         steps  spent CAD  seconds
finishes in 3 steps                done                   3    0.00009        3
runaway, step cap                  step_cap               8    0.00029        8
runaway, slow model, deadline      deadline               5    0.00016       20
runaway, cost cap                  cost_cap             190    0.00993      190
runaway, loop limits all off       budget_exhausted     945    0.04995      945
```

The last row has a 0.05 CAD budget so the run stays short; spend stays below it. With the stand-in's budget of 1 CAD,
`features/runaway.feature` (R68) shows the same runaway spending almost all of it and the daily-cost alarm going to
ALARM.
The token counts behind the spend are assumed (`SYSTEM_PROMPT_TOKENS`, `TOOL_RESULT_TOKENS` in `agent_loop.py`).

## Adapt it

1. Pass your model call through the course LLM gateway as `call_model(context_tokens, max_tokens)`, and raise
   `GatewayRefused` on a 429 with the gateway's error code. With the cost cap on, `call_model` must have a `model`
   attribute (the model it asks the course LLM gateway for), and `Limits(model=...)` must name the same one: the loop stops
   before the first call otherwise, since the cap would count the wrong price.
2. Put your tools in the `tools` dict; give each tool call its own timeout, since the deadline is checked between
   calls.
3. In a Lambda handler, set `Limits(deadline_s=deadline_for(context, 20))`.
4. Choose `max_steps` from your eval: the most steps a correct task took, plus a margin.
