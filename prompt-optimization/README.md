# Tune a prompt against your eval, and report it on questions the optimizer never saw

## What you build

Build a DSPy program for one LLM step: a question goes in and one SQL query comes out. DSPy is a Python library that
writes the prompt from a declared input and output. Build a metric that scores each answer and says how it failed, and a GEPA run (a
prompt optimizer in which a second model reads the failures and rewrites the instructions) that rewrites the step's
instructions using feedback on train questions and chooses the best rewrite by scoring candidates on validation questions. At the end you get a table with the pass rate before and after on held-out test questions that the optimizer did not see,
and what the run spent in Canadian dollars.

- **Practice:** tune a prompt against a metric with feedback, choose on validation, report on held-out test
  questions (L17, the evaluator-optimizer pattern with GEPA and DSPy
;
  [AI Engineering ch. 4](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch04.html), Design Your Evaluation Pipeline, and
  [ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), Agents, Reflection and error correction
).
- **Instrument:** `main/program.py` (the signature and module), `main/metric.py` (score and feedback),
  `main/optimize.py` (the GEPA run), `main/report.py` (the test table), `main/models.py` (gateway calls and the
  spend cap).
- **Validation:** `uv run pytest -q` runs the loop end to end on a stub model, checks that the test questions never
  reach the optimizer, and checks each spend cap.

## Use it when

- Your project has the generative-AI-first shape: an LLM step with an eval (as in L15: dataset, solver, scorer),
  and the step fails often enough that you want a better prompt. Prompt optimization is part of that shape, and
  so is the eval example in [`ext-eval-matrix/`](ext-eval-matrix/README.md). Neither is an extension on the
  proposal page. A team uses prompt optimization and the eval example to improve a step it already has.
- You can write 20 or more cases with a correct answer each, and a check that says how an answer failed.

Use another approach when:

- The failure can be fixed in code. In the L15 run most of the gain was fewer SQL errors, and the demo's
  `sql_adherence` eval fixes the same failure by cutting the reply after the first statement, with no prompt change.
- You have fewer than about 15 cases: splitting them three ways leaves too few test questions
  to compare.
- You are choosing a model, not a prompt: start with `ext-eval-matrix/`.

## Main scenario

1. `main/data.py` loads the train and validation questions from `data/questions.yml` and a small orders table into
   DuckDB.
2. `main/program.py` declares the LLM step as a DSPy signature: inputs `question` and `tables`, output `sql`.
3. `main/optimize.py` runs `dspy.GEPA`: the task model answers 3 train questions, `main/metric.py` scores each answer
   and writes a feedback text, and the reflection model reads the feedback and writes new instructions.
4. GEPA runs a new candidate on all validation questions only if it does better on those 3 (GEPA's default
   acceptance rule, `StrictImprovementAcceptance` in `gepa/core/engine.py`, gepa 0.1.4
); it keeps the candidates that
   are best on at least one validation question and stops at its call budget or a spend cap.
5. Only then does `main/run.py` load the test questions (`data/test.yml`) and run the seed prompt and the chosen
   prompt on them several times.
6. `main/report.py` writes the pass rate per run, the failure types and the spend per role to `runs/<time>/report.md`;
   the run prints that path at the end. The captured stub run is kept in `output/main/`.

Every model call goes through the course LLM gateway, or through the offline stub (`--target stub`, the default).
The bundled data has 9 train, 6 validation and 6 test questions. "The L15 demo" in the tables below is the GEPA
experiment in the L15 demo repository, [`evals/gepa/`](https://github.com/cpsc436c-2026w1/l15-genai-querychat/tree/main/evals/gepa).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| One call as a signature (R11) | A DSPy signature (`question`, `tables` → `sql`) inside a module (`main/program.py`) | DSPy writes the system prompt and the user message from the signature and reads the reply back into `sql`, so GEPA has one named text to change | DSPy and GEPA (L17) |
| What GEPA may change (R11) | Only the signature's instructions; the field names and the table description stay as written | The comparison is then about instructions only. The L15 run changed only the text after `</data_description>` for the same reason | L15 demo, `evals/gepa/README.md` |
| Metric with feedback (R14) | Score 1 or 0, and a sentence naming the failure type (SQL error, wrong unit, wrong value) with the reference SQL | A score says how often the step fails. The reflection model also needs to know how it failed | Evaluator-optimizer (L17); [AI Engineering ch. 4](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch04.html), Design Your Evaluation Pipeline, Step 2. Create an Evaluation Guideline (criteria, and scoring rubrics with examples) |
| Three splits (R10, R12, R15) | Train for reflection, validation for choosing, test only for the report, in its own file that `main/optimize.py` does not load | The reflection model copies what it reads into the prompt. In the L15 run, two of the three queries at the end of the new prompt were reference SQL of train questions. The winner's validation score is biased upward because it was chosen for being highest there | L15 demo, `evals/gepa/README.md` "Questions and splits"; [AI Engineering ch. 4](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch04.html), Model Selection, "Data contamination with public benchmarks" (a model evaluated on data it was trained on scores higher than it should; the book says this of models and benchmarks, and applying it to a prompt tuned on its own test questions is a course choice) |
| Two models (R32) | Task model `qwen3.6-35b-a3b`, reflection model `qwen3.8-27b`, both on the course LLM gateway | The task model answers every question in the search and the test runs, so the cheaper one keeps the run within a small budget. Reflection calls are few: in the L15 run they were US$0.09 of the US$0.62 the optimization spent | DSPy's `dspy.GEPA` docs ask for a strong reflection model; prices from the gateway's price table |
| Search settings (R10) | 3 train questions per reflection, candidates kept by Pareto front, no merge | The settings of `dspy.GEPA(auto="light")` and of the L15 run; one search move keeps the log readable | `dspy/teleprompt/gepa/gepa.py` (DSPy 3.5.0); L15 demo `evals/gepa/optimize.py` |
| Spend cap in code (R20, R21, R22, R23, R24, R25, R26, R27, R28) | GEPA's `max_metric_calls` (default 120), a reflection cap (default 8), a money cap for the whole run (default 0.50 CAD); the search stops at 60% of the money cap. A call is refused when what is spent, what the calls in flight can still cost and the most this call can cost would together pass the money cap; the most a call can cost is one input token per byte of its prompt and its output cap: 4,000 tokens for a task-model call, `REFLECTION_MAX_TOKENS` (16,384) for a reflection call. A run therefore does not end above the cap, and a cap needs room for the calls in flight: about 0.002 CAD for the four task calls of an evaluation. The search's task-model calls are refused past `--max-metric-calls` plus one iteration; the test runs have their own allowance, `--max-test-calls` (default: twice 2 prompts x runs x test questions; the doubling is room for a retried answer). A refused call, or a call the gateway refuses or fails, cuts the search: the search does not make a reflection call afterward and the other questions of that evaluation are not sent, the candidates found so far, `instructions_after.txt` and the report are still written, and the report names the call that cut the search and why | Team budgets are small and the gateway key has a per-key budget; the test runs must still fit after the search, and a search ended by its own cap must still be tested. The L15 run stopped GEPA at US$3.00 under a US$4.00 hard cap. GEPA and DSPy catch an exception inside an evaluation and go on (`gepa/core/engine.py`, gepa 0.1.4; DSPy scores it 0), so a refusal must reach the stop condition, or the search repeats the refused call or rewrites a prompt whose answers were never given | Course constraint (state the cost of a run, per-key budget); L15 demo `evals/gepa/program.py` |
| Calls through the course LLM gateway (R30, R31, R33, R34, R35) | `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` from the environment (the older `GATEWAY_URL` and `COURSE_MCP_TOKEN` still work); `dspy.LM("openai/<model>", model_type="chat", num_retries=0, ...)`: one call is one request; a GPT or o-series model gets `temperature` 1.0 | The code and the repo do not contain a key. A retry would send a request again under the one amount held for it against the money cap, and a gateway may charge each sent attempt that times out or fails; with one request per call the cap counts what the key can be charged. DSPy 3.5 picks the Responses API by default for `openai/` models (its `model_type` "auto"). The gateway serves chat completions only. GPT and o-series models accept only the default temperature, 1, and DSPy refuses `max_tokens` under 16,000 for its gpt-5 and o-series names. A gateway may clamp the output-token limit lower | Course constraint (model calls through the course LLM gateway); [`llm-gateway`](../llm-gateway/README.md) |
| Plain text replies (R11) | DSPy's ChatAdapter for the task call and the reflection call | Plain text field markers work on any chat model. DSPy's default for reflection is the JSONAdapter, which may ask the endpoint for structured output | Test what an endpoint supports before designing on it: [`llm-probes`](../llm-probes/README.md) (L15–17) |
| Report on test, several runs (R13, R17, R18) | 3 runs per prompt; the median with every run and the failure types; a table per test question with the runs each prompt passed | Replies vary: in the L15 run the same prompt passed 33% to 75% of the same 12 questions over 8 runs. A median can go up while one question goes from always right to always wrong: the table per question shows which questions the new prompt fixed and which it broke (the L15 results have the same table: 11 questions passed more often, 0 less often) | L15 demo, `evals/gepa/results.md`; Agentic AI Lens [AGENTOPS02-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops02-bp03.html), Implement agent behavior versioning and rollback capabilities (compare two versions of a prompt on the same test questions over several runs, and tie a change in quality to the version that caused it) |
| No response cache (R13) | `cache=False` on every LM | A repeated test run must be a real call, or the runs would not vary | DSPy LM option |
| Read the new prompt (R16) | `runs/<time>/instructions_after.txt` is written next to the report | The L15 prompt told the model to return a `value` field and a numeric `confidence` that the answer schema does not have | L15 demo, `evals/gepa/README.md` "What changed in the prompt" |
| What produced a prompt (R19) | `runs/<time>/run.json`: the two models, the versions of DSPy and GEPA, a hash of each question file and of both instruction files, the caps, what ended the search, the test medians. No key and no question text | A prompt that you copy into your app is a version of your app's behaviour. Three weeks later the question is which run made it, on which questions and with which model; the hash of `instructions_after.txt` answers it | Generative AI Lens [GENOPS03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops03-bp01.html) (version a prompt together with the settings it was evaluated with, and with its baseline); AGENTOPS02-BP03, Implement agent behavior versioning and rollback capabilities (each version carries a reference to its evaluation results) |
| Offline stub (R40) | `main/stub.py`: the task model writes fixed SQL with habits that a rule in the system prompt stops; the reflection model adds one rule per failure type in the feedback | `uv run pytest -q` must pass with no key and no spend; the stub shows that the loop works, not how much a real prompt improves | Course constraint (offline tests, a local stand-in) |

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
runs them offline, on the stub or against a stand-in for the gateway on `127.0.0.1`. The requirements of a
variant (a folder `ext-*`) are in its own README, and their numbers go on from here:
[GEPA without DSPy](ext-gepa-direct/README.md#requirements) from 60,
[eval matrix](ext-eval-matrix/README.md#requirements) from 70. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Main scenario**

- **R10** (event) When `main/run.py` runs, the recipe shall search for better instructions on the train questions,
  choose the candidate with the best validation score, and write its instructions to `instructions_after.txt`
  beside the seed's in `instructions_before.txt`.
- **R11** (ubiquitous) The recipe shall change only the instructions between candidates, so that the table
  description and the field markers reach the task model as written.
- **R12** (ubiquitous) The recipe shall keep every test question away from both models until the search has ended.
- **R13** (event) When the search has ended, the recipe shall run the seed prompt and the chosen prompt on every
  test question 3 times each (`--runs` names another number) and report for each prompt the median pass rate,
  every run, the answers passed and the failure types.
- **R14** (ubiquitous) The recipe shall name the failure type of each failed answer: `SQL error`, `wrong unit`,
  `wrong value` or `no reply`.
- **R15** (ubiquitous) The recipe shall report the candidates' validation scores apart from the test table, under
  a heading that says they were used only to choose.
- **R16** (event) When a run ends, the recipe shall print the report and the folder that holds it, the two
  instruction files, `answers.json`, `candidates.json`, `program.json`, `run.json` and `spend.json`.
- **R17** (event) When a run ends, the recipe shall say in the last line of the report what ended the search.
- **R18** (event) When the test runs have ended, the recipe shall report for each test question in how many of its
  runs each prompt passed, and how many questions pass more often, less often and the same with the new prompt.
- **R19** (event) When a run ends, the recipe shall write `run.json` with what produced the new instructions: the
  two models, the versions of DSPy and GEPA, the hashes of the question files and of both instruction files, the
  caps, what ended the search and the test medians.

**Spend cap**

- **R20** (ubiquitous) The recipe shall end the search at 8 reflection calls, at 120 metric calls or at 60% of the
  money cap, whichever comes first, unless an option names another limit.
- **R21** (unwanted) If a model call would pass the cap on the calls of its role, or could take the run past the
  money cap (what is spent, what the calls in flight can still cost, and the most this call can cost), then the
  recipe shall refuse the call before it is made.
- **R22** (unwanted) If a call of the search is refused by a cap, then the recipe shall cut the search at that
  call, write the result it has, and name the refused call in the report.
- **R23** (unwanted) If a call of the test runs is refused by a cap, then the recipe shall write a report that
  names the prompts not tested, keep the search result, and exit with code 1.
- **R24** (ubiquitous) The recipe shall count the calls of the test runs apart from the calls of the search.
- **R25** (unwanted) If a model has no price in the recipe's price table, then the recipe shall exit before any
  model call.
- **R26** (unwanted) If a model call of the search fails (the endpoint refuses it, or it breaks), then the recipe
  shall cut the search at that call and make no further task-model or reflection call of the search. The
  questions of that evaluation that got no answer count as failed for the candidate under evaluation (DSPy
  scores them 0), so that candidate can lose to an earlier one by them and cannot win by them.
- **R27** (unwanted) If a model call of the test runs fails, then the recipe shall stop the test runs, keep the
  search result, and exit with code 1.
- **R28** (event) When a run ends, the recipe shall write the calls, the tokens and the Canadian dollars of each
  role to `spend.json` and to the report, with their total, and write to `spend.json` the amount still held for
  calls in flight, which is 0 once every call has ended.

**Model calls**

- **R30** (ubiquitous) The recipe shall send every model call of a gateway run to
  `$COURSE_GATEWAY_URL/chat/completions` with `KB_MCP_TOKEN` as the bearer key, or with the older names
  `GATEWAY_URL` and `COURSE_MCP_TOKEN` when the new ones are not set.
- **R31** (unwanted) If neither `COURSE_GATEWAY_URL` nor `GATEWAY_URL`, or neither `KB_MCP_TOKEN` nor
  `COURSE_MCP_TOKEN`, is set for a gateway run, then the recipe shall exit with a message that names
  `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN`, before any request is sent.
- **R32** (ubiquitous) The recipe shall ask the gateway for `qwen3.6-35b-a3b` as the task model and `qwen3.8-27b`
  as the reflection model unless `--task-model` or `--reflection-model` names another.
- **R33** (ubiquitous) The recipe shall cap the output of every task-model call at 4,000 tokens and of every
  reflection call at `REFLECTION_MAX_TOKENS` (16,384 unless set).
- **R34** (unwanted) If the gateway starts to refuse calls during a run, then the recipe shall end the run with
  exit code 1 and a report of what ran.
- **R35** (ubiquitous) The recipe shall send each model call to the gateway once, with no second attempt after a
  refusal or a failure.
- **R36** (unwanted) If a reflection reply has no text, then the recipe shall count it as a failed call that cuts
  the search, and name it in the report with `REFLECTION_MAX_TOKENS`.

**Course constraints**

- **R40** (ubiquitous) The recipe shall run its stub run without an AWS account, a course token or a network
  connection.

**Clean-up**

- **R50** (event) When `clean_up.py` runs, the recipe shall remove the folder `runs/` and leave the captured runs
  in `output/`.
- **R51** (complex) While there is no `runs/` folder, when `clean_up.py` runs, the recipe shall print
  `nothing to remove`.

**Requirements of the variants**, each in the README of its folder:

- 60 to 63, GEPA without DSPy: [`ext-gepa-direct/README.md`](ext-gepa-direct/README.md#requirements)
- 70 to 73, eval matrix: [`ext-eval-matrix/README.md`](ext-eval-matrix/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need a key or an AWS account. The course LLM gateway
runs in the course's account, never in yours: you only send it requests. The recipe does not create AWS resources: no
Lambda function, no Fargate task, no EC2 instance, and it does not create an AWS resource.

| Part | Files | Laptop, offline | Laptop, with the gateway | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Search and test report | `main/run.py`, `main/optimize.py`, `main/report.py` | yes, the default (`--target stub`) | yes, `--target gateway` | no |
| Program and metric | `main/program.py`, `main/metric.py`, `main/data.py` | inside the run's own Python process; the metric runs each query in DuckDB in memory | the same | no |
| Task and reflection models | `main/models.py`, `main/stub.py` | a stub: fixed SQL per question, one rule per failure type | the course LLM gateway | no |
| Spend cap | `Budget` in `main/models.py` | counts the stub's calls at the gateway's prices | counts every gateway call. The gateway tracks its own per-key budget separately | no |
| GEPA without DSPy | `ext-gepa-direct/optimize_direct.py` | yes, the default | yes, `--target gateway` | no |
| Eval matrix | `ext-eval-matrix/matrix.py` | yes, the default | yes, `--target gateway` | no |
| Run folders | `runs/<time>/` | on your disk | on your disk | no |
| Clean-up | `clean_up.py` | yes: it removes `runs/` only | the same | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against the gateway | no |

### 1. Laptop, offline: the dry run

From your local device (laptop) in a terminal at the repository root:

These commands do not need a key or an AWS account, so you do not sign in.

```bash
cd prompt-optimization
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run
uv run python ext-gepa-direct/optimize_direct.py
uv run python ext-eval-matrix/matrix.py
```

What you should see, line by line:

1. `uv sync` installs the packages (DSPy and its dependencies take a minute the first time).
2. `pytest` ends with a line like `44 passed`.
3. `behave` ends with `0 failed`.
4. The stub run prints the report of [What you should see](#what-you-should-see) and `written to runs/<time>`.
5. The direct run prints the same before and after table and `written to runs/direct-<time>`.
6. The matrix prints one row per model with the seed prompt and `written to runs/matrix-<time>`.

Clean-up and cost: nothing was spent. `uv run python clean_up.py` removes `runs/`.

### 2. Laptop, with the course LLM gateway

Run the commands on your laptop in `prompt-optimization`. This part does not use AWS: there is no AWS sign-in and no
`aws sts get-caller-identity`. Your course token authenticates gateway requests.

1. Get the gateway's base URL from the course page or from your team repo on github.students. Replace
   `YOUR-GATEWAY-BASE-URL` with it and keep the quotes.
2. `read` does not show a prompt: paste your course MCP token, then press Enter. The token does not show.
3. In the matrix line, replace `<time>` with the folder name the first command printed.

```bash
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN
uv run python -m main.run --target gateway --max-cad 0.20
uv run python ext-eval-matrix/matrix.py --target gateway --max-cad 0.20 --prompt after=runs/<time>/instructions_after.txt
unset KB_MCP_TOKEN
```

What you should see: the report with the two rows, `before` and `after`, on the six test questions, the spend by
role, and what ended the search; then the matrix with four rows. The runs differ from each other and from our run.
If the gateway refuses a call, the run ends with one line that says which call and why, and with exit code 1; what
was found until then is in the run folder.

Options: `--runs 5` for more test runs per prompt, `--max-metric-calls 200` for a longer search,
`--task-model qwen3.8-27b` to tune the larger model's prompt, `--verbose` for GEPA's log of every iteration.

Clean-up and cost: our two runs spent 0.0099 and 0.0063 CAD of the key's budget
([Cost and clean-up](#cost-and-clean-up)); yours will differ. `--max-cad` is the most a run can spend: a call
that could take the run past it is refused before it is made. The last line of the block takes the token out of
the shell. Save any report you need, then `uv run python clean_up.py`.

## What you should see

**Stand-in run, offline: not a model's numbers** (`output/main/report.md`, the stub model):

```
| prompt | median | per run | answers passed | failures | test cost (CAD) |
|---|---:|---|---:|---|---:|
| before | 17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3 | 0.000365 |
| after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000431 |
```

**Real run on the course LLM gateway, 2026-11-12** (`output/main-gateway/report.md`, `qwen3.6-35b-a3b`)
:

```
| prompt | median | per run | answers passed | failures | test cost (CAD) |
|---|---:|---|---:|---|---:|
| before | 83% | 83% / 83% / 100% | 16/18 | wrong value 2 | 0.000396 |
| after | 100% | 100% / 100% / 83% | 17/18 | wrong value 1 | 0.000856 |
```

The 17% and the 83% of the first table are the stub's own; the real model started at 83%.

Below the table the report lists each test question with the runs it passed under each prompt, and ends that
section with `With the prompt after, 4 of 6 questions pass more often than with before, 0 less often, 2 the same.`

With the stub, the runs are identical and the numbers are the stub's: they show that the loop works. The stub does not make
a paid call. Its `test cost (CAD)` column is the gateway price applied to the stub's token counts. Relations that
hold:

- In the stub run, after passes more than before. The rules GEPA added are in `output/main/instructions_after.txt`
  (in your own run, `runs/<time>/instructions_after.txt`).
- One test question (q06) stays wrong: the stub writes a wrong query for it whatever the prompt says. A real run
  also keeps failures that a prompt does not fix.
- The chosen candidate scored 100% on validation and 83% on test (`output/main/candidates.json`). The validation
  score was used to choose, so it is not the number to report.
- The new prompt is longer, so each test call has more input tokens and spends a little more.

Through the gateway the runs differ from each other. A run of step 3 on the course LLM gateway (2026-11-12, defaults,
`--max-cad 0.20`) is in `output/main-gateway/`: test median 83% before and 100% after on `qwen3.6-35b-a3b`, 171
calls, 0.0099 CAD by the recipe's own estimate. That capture
predates the table per question, which it does not have. For a real result, see the L15 run (measured, in
[`evals/gepa/results.md`](https://github.com/cpsc436c-2026w1/l15-genai-querychat/tree/main/evals/gepa/results.md) of the L15 demo repository): Qwen 3.8 27B passed a median of 58% of 12 test questions with
the original prompt and 83% with GEPA's, over 3 runs each (88% over 8 runs); splits of 23 train, 12 validation and
12 test questions; reflection model GPT-5.4 mini; US$1.00 for the whole experiment, about US$0.62 of it for the
optimization. Only the instructions after `</data_description>` changed.

## Extensions

The two folders here are variants of the main scenario. They are not extensions on the proposal page.

| Variant | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| GEPA without DSPy | The same loop with the `gepa` library over an existing system prompt string, as the L15 demo ran it | `ext-gepa-direct/` | Steps 2 and 3: the program is a prompt string and one chat call; GEPA gets an adapter with `evaluate` and `make_reflective_dataset` instead of a five-argument metric |
| Eval matrix | The same eval over several models and prompts, with pass rate and spend per cell | `ext-eval-matrix/` | Steps 3 and 4 are left out: each cell is one model with one prompt, run on the test questions |

## Cost and clean-up

The stub run does not spend anything. One run through the course LLM gateway with the defaults and `--max-cad 0.20`
(2026-11-12) made 171 model calls and spent 0.0099 CAD by the recipe's own count: 0.0053 for the search's 128
task-model calls, 0.0033 for 4 reflection calls, 0.0013 for the 39 test calls
. The matrix run of the same day (two models, two prompts, 3
runs) spent 0.0063 CAD.
Both at the gateway's prices: 0.05 and 0.10 CAD per million tokens in and out for `qwen3.6-35b-a3b`, 0.10 and 0.20
for `qwen3.8-27b`. The reflection model is a
reasoning model: its 4 calls wrote 13,348 output tokens, more than twice the 6,252 it read.
The money cap stops the run at
`--max-cad` either way (the test runs count their calls apart from the search's, under `--max-test-calls`, but share the money cap); the cap counts the recipe's own estimate (tokens times the gateway's price table), so the
gateway's per-key budget remains the bill that counts. Every run writes `spend.json` and a spend table in its report.

The recipe does not create AWS resources. Save any report you need, then remove the local run folders (everything under
`runs/`), in the recipe folder:

```bash
uv run python clean_up.py
```

## Adapt it

1. **Your data.** Replace `data/orders.csv`, the `CREATE TABLE` line and `SCHEMA` in `main/data.py`. If your step's
   output is not SQL, change `run_sql()` into the check that runs or parses your output.
2. **Your cases.** Put 20 or more cases in `data/questions.yml` with `split: train` or `split: val`, and the cases you
   report on in `data/test.yml`. Never copy a test case into the other file; `tests/test_splits.py` checks it.
3. **Your step.** Change the fields and the docstring of `QuestionToSQL` in `main/program.py`. The docstring is the
   seed that GEPA starts from.
4. **Your failure types.** Change `check()` in `main/metric.py` so that its feedback names the ways your step fails.
   The feedback text, not the score, is what GEPA learns from: the more specific the sentence, the more the
   reflection model has to work with.
5. **Your limits.** Set `--max-metric-calls`, `--max-reflection-calls`, `--max-test-calls` and `--max-cad` to fit your team's budget. The
   L15 run made 467 model calls in all.
6. **Read the new prompt** in `instructions_after.txt` before you put it in your app, and report the test table,
   not the validation score. Commit `run.json` beside the prompt you copy: it says which run made it.
   - Read every candidate in `candidates.json`, each with its validation score (candidate 0 is the seed), to see
     which rule raised the score and which did not.
   - Check each rule the new prompt states (column names, codes, value ranges) against the data itself, with a
     query. A test score that does not exercise a rule does not test it: a perfect held-out score can sit beside
     a rule that is false.
   - One search is one draw. Before you adopt a prompt, run the search with two or three `--seed` values and
     compare the test tables.
7. **Your models.** Leave `temperature` unset for an OpenAI GPT or o-series model outside this recipe (the recipe
   sends 1.0, the only value they accept). A thinking reflection model uses output tokens to reason before writing instructions. Raise
   `REFLECTION_MAX_TOKENS` if the report names an empty reflection reply, and know that a gateway may clamp the cap
   to its own per-key limit without an error.
