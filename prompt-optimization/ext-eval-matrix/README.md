# Eval matrix: the same eval over several models and prompts

Use this to choose which model and which prompt your feature runs with. Each cell of the matrix is one model with
one prompt. Each model and prompt combination is run several times on the test questions. The table gives the pass rate and the spend of each cell. The
practice is to use the simplest model that passes your eval (L16–17; Agentic AI Lens
[AGENTCOST02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp01.html): match
each task to the cheapest model of acceptable quality, and compare models on your own tasks
).
This is an eval example for the generative-AI-first shape. It is not an extension on the proposal page.

## What changes in the main scenario

| Step in `main/` | Here | Why |
|---|---|---|
| 3 and 4. GEPA searches for a prompt | Left out: no prompt changes. The prompts come from files (`--prompt NAME=FILE`). The seed prompt is always a row | A matrix only measures. Because nothing is tuned on them, the test questions can be used for every cell |
| 5. One task model | Every model in `--models` (default: both chat models on the course LLM gateway) | The cheaper model with a tuned prompt may pass as often as the larger model, at a lower price per answer |
| 6. Before and after rows | One row per cell: median pass rate, every run, failure types, CAD spent, CAD per 1,000 answers | Compare the pass rate and spend for each model and prompt when making the choice |

Unchanged: the program (`main/program.py`), the metric, the spend cap (`--max-calls`, `--max-cad`). A call that a
cap refuses, or that the gateway refuses or fails, stops the matrix: the report has a row for every cell that
finished and a line that says where it stopped, and the exit code is 1. The report does not have a row for the cell that was cut, because a
row from part of its runs would not compare with the others (Generative AI Lens
[GENREL03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp01.html): on an
error, fail gracefully
).

Choose on a matrix run with several runs per cell. If you then tune a prompt on its results, the test questions
have been used to choose: write new test questions for the number you report.

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The requirement numbering continues from the recipe's numbering. The scenarios are in
`features/extensions.feature`.

- **R70** (optional) Where `ext-eval-matrix` is used, the recipe shall report one row per model and prompt, with
  the median pass rate, every run, the answers passed, the failure types, the Canadian dollars spent and the
  Canadian dollars per 1,000 answers.
- **R71** (optional) Where `ext-eval-matrix` is used, the recipe shall include the seed prompt as a row for every
  model.
- **R72** (optional) Where `ext-eval-matrix` is used, the recipe shall make no search and no reflection call.
- **R73** (complex) Where `ext-eval-matrix` is used, if a model call is refused by `--max-calls` or `--max-cad`, or
  fails at the endpoint, then the recipe shall stop the matrix, report the cells that finished and what was
  spent in all (the cell that was cut included), and exit with code 1.

## Run it

Run these commands on your laptop, offline on the stub or with the course LLM gateway. Nothing runs in AWS (the recipe
README, [Where each part runs](../README.md#where-each-part-runs)). In `prompt-optimization`, run the first command
below. Replace `<time>` with the folder name it prints before running the matrix commands:

```bash
uv run python -m main.run                     # writes runs/<time>/instructions_after.txt
uv run python ext-eval-matrix/matrix.py --prompt after=runs/<time>/instructions_after.txt
uv run python ext-eval-matrix/matrix.py --target gateway --prompt after=runs/<time>/instructions_after.txt
```

The stub run is in `output/ext-eval-matrix/report.md`. With the stub, the larger model passes more questions with the
seed prompt, both models pass the same questions with the tuned prompt, and the larger model spends twice as much
per answer (the gateway's price table, not a measurement). A run on the course LLM gateway (2026-11-12, `--prompt
after=` the main scenario's gateway run, `--max-cad 0.20`) is in `output/ext-eval-matrix-gateway/report.md`:
`qwen3.6-35b-a3b` went from a median of 83% with the seed prompt to 100% with the tuned one, `qwen3.8-27b` was at
100% with both, and the larger model spent 3.4 to 6.5 times as much per answer (0.1375 against 0.0408 CAD per
1,000 answers with the tuned prompt, 0.1441 against 0.0221 with the seed)
.

## Adapt it

Add models with `--models a,b,c` (the model ids the course LLM gateway lists) and prompts with more `--prompt`
flags. Set `--max-calls` and `--max-cad` before a gateway run. Each cell makes `runs x test questions` calls.
