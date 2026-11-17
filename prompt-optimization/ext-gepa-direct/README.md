# GEPA without DSPy: optimize an existing system prompt string

Use this when your app already sends a system prompt of its own (written by hand, or by a library such as
querychat) and you are not using DSPy. The `gepa` library is the optimizer that `dspy.GEPA` wraps. Here it is called
directly, as the L15 demo did ([`evals/gepa/optimize.py`](https://github.com/cpsc436c-2026w1/l15-genai-querychat/tree/main/evals/gepa/optimize.py) in the L15 demo repository).

## What changes in the main scenario

| Step in `main/` | Here | Why |
|---|---|---|
| 2. The step is a DSPy signature | The step is a prompt string and one chat call: `render(instructions)` builds the system prompt, the question is the user message, `extract_sql()` takes the query from the reply | The text being optimized is the very system prompt your app sends, through your app's own call. `dspy.GEPA` would put DSPy's own prompt format around it |
| 3. GEPA calls a five-argument metric | GEPA calls `PromptAdapter`: `evaluate()` runs a batch and returns outputs and scores. For each train question, `make_reflective_dataset()` returns the input, the output and the metric's feedback | The `gepa` library requires these methods in an adapter for the program |
| Reflection through DSPy | GEPA's own reflection prompt, sent as one user message; the reply carries the new instructions in a ``` block | No adapter: the `gepa` library reads the reply text itself |

Unchanged: the data and splits (`main/data.py`), the metric (`main/metric.py`), the spend cap (`Budget` in
`main/models.py`, with `stop_callbacks=[budget.stop]`, which also ends the search after a refused or failed call), the test report (`main/report.py`).

As in the L15 run, the system prompt has two parts: `HEAD` (the role line and the tables) stays as written, and
GEPA rewrites only `SEED_INSTRUCTIONS`.

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's; the scenarios are in
`features/extensions.feature`.

- **R60** (optional) Where `ext-gepa-direct` is used, the recipe shall rewrite only the instructions at the end of
  the system prompt string and leave its head as written.
- **R61** (optional) Where `ext-gepa-direct` is used, the recipe shall write the same before and after table on the
  test questions as the main scenario, with the test questions kept away from both models until the search has
  ended.
- **R62** (complex) Where `ext-gepa-direct` is used, if the first call of the search is refused or fails, then the
  recipe shall exit with one line that says so and write nothing.
- **R63** (complex) Where `ext-gepa-direct` is used, if a later call of the search is refused or fails, then the
  recipe shall cut the search at that call and still write the result it has.

## Run it

Where it runs: on your laptop, offline on the stub or with the course LLM gateway; nothing runs in AWS (the recipe
README, [Where each part runs](../README.md#where-each-part-runs)). In `prompt-optimization`:

```bash
uv run python ext-gepa-direct/optimize_direct.py                     # offline, stub models
uv run python ext-gepa-direct/optimize_direct.py --target gateway    # needs COURSE_GATEWAY_URL and KB_MCP_TOKEN
```

It writes `runs/direct-<time>/report.md` with the same before and after table as `main/`; the stub run is in
`output/ext-gepa-direct/`. `--verbose` prints GEPA's log of every iteration.

## Adapt it

Replace `HEAD`, `SEED_INSTRUCTIONS` and `extract_sql()` with your app's prompt and reply parsing. If your app sends
the prompt through a library, call the library inside `PromptAdapter.evaluate()` with the candidate's instructions,
so that the optimizer tests the prompt exactly as your app sends it.
