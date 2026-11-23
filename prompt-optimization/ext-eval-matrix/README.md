# Eval matrix: the same eval over several models and prompts, per group of questions

Use this to choose which model and which prompt your feature runs with. Each cell of the matrix is one model with
one prompt. Each model and prompt combination is run several times on the test questions. The table gives the pass rate and the spend of each cell. The
practice is to use the simplest model that passes your eval (L16–17; Agentic AI Lens
[AGENTCOST02-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp01.html): match
each task to the cheapest model of acceptable quality, and compare models on your own tasks
).
This is an eval example for the generative-AI-first shape. It is not an extension on the proposal page.

Use the groups and the paired comparison (from [Groups and the paired comparison](#groups-and-the-paired-comparison) on) when your system has a model step you evaluate, and you compare alternatives
(models, prompts, or a version without the model) per group of questions and on the same cases. You need it for the
final hand-in (Tue Dec 15), where the experiment is re-run with its code and tests.

## What changes in the main scenario

An eval (evaluation) scores a model step on fixed test questions, by code. In `main/`, GEPA (a prompt optimizer of
DSPy) starts from the **seed prompt**, the step's first prompt, and searches for a better one, the **after** prompt.

| Step in `main/` | Here | Why |
|---|---|---|
| 3 and 4. GEPA searches for a prompt | Left out: no prompt changes. The prompts come from files (`--prompt NAME=FILE`). The seed prompt is always a row | A matrix only measures. Because nothing is tuned on them, the test questions can be used for every cell |
| 5. One task model | Every model in `--models` (default: both chat models on the course LLM gateway) | The cheaper model with a tuned prompt may pass as often as the larger model, at a lower price per answer |
| 6. Before and after rows | One row per cell: median pass rate, every run, failure types, CAD spent, CAD per 1,000 answers; then the outcomes per group, the cost and time per answer, and the comparisons | Compare the pass rate and spend for each model and prompt when making the choice, and see which kind of question a cell fails |

Unchanged: the program (`main/program.py`), the metric, the spend cap (`--max-calls`, `--max-cad`). A call that a
cap refuses, or that the gateway refuses or fails, stops the matrix: the report has a row for every cell that
finished and a line that says where it stopped, and the exit code is 1. The report does not have a row for the cell that was cut, because a
row from part of its runs would not compare with the others (Generative AI Lens
[GENREL03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp01.html): on an
error, fail gracefully
).

Choose on a matrix run with several runs per cell. If you then tune a prompt on its results, the test questions
have been used to choose: write new test questions for the number you report.

## Groups and the paired comparison

Two cells that pass about as often on all the test questions can fail on different kinds of question. The matrix
therefore reports every cell group by group, counts each case once, and compares two cells on the same cases.

- **Groups.** Every test case carries a `group`, a kind of question you name in the test file (`data/test.yml` for
  the cafe, `ext-eval-matrix/data/test.yml` for the rides case). A case without a group stops the matrix before any
  model call, with exit code 2.
- **Three outcomes.** A run is `correct` (its value equals the reference), `correct refusal` (the case has no
  reference, `answerable: false`, and the step returned `CANNOT_ANSWER`) or `wrong` (anything else). A refusal of a
  case that has a reference is `wrong`, and the report shows how many of the wrong cases were refusals.
- **Each case once.** A case passes when it passes in at least 2 of its 3 runs. The runs of one case share its
  question and its difficulty, so 60 runs of 20 cases are not 60 independent tests.
- **The paired comparison** (`--compare A,B`). For each group and for all cases: the cases each cell passed, the
  cases only B passed and only A passed (the *discordant* cases, where the two cells differ), the difference in the
  share of cases passed (B minus A), and the exact McNemar p. The McNemar test asks whether, among the discordant
  cases, one cell wins more often than a coin would give; its exact form is a two-sided binomial test of the
  B-only count out of all discordant cases at one half (`scipy.stats.binomtest`).
  A row with fewer than 6 discordant cases is marked `too few cases`: with 5 discordant cases all one way, p is
  0.0625, so no p under 0.05 is possible <!--src: ours: the binomial distribution at n = 5 and n = 6 -->. A group of 5
  cases is therefore always marked: the group rows say where a difference comes from; the all-cases row gives the
  exact p, which says how often a split at least this uneven would come from a coin. A large p does not show that
  the two cells are equally good.
- **A descriptive interval on all cases.** Each case's share of passing runs (0, ⅓, ⅔ or 1) in A and in B, and a
  bootstrap (resampling the cases with replacement, many times, to see how much the mean difference moves)
  stratified by group (each resample keeps the group sizes of the test file): 10,000 resamples, the same resample
  for both cells, seed 0, the 2.5th and 97.5th percentiles. It is printed as descriptive, beside the exact test.
- **A cell without a model**, `no-model/templates` (rides case): fixed SQL templates in `templates.py`, picked by a
  regular expression on the question, `CANNOT_ANSWER` for a question no template matches. It costs 0 CAD, runs 3
  times like every cell, and each model cell is compared with it.
- **Cost and time.** For each cell: CAD per answer, and the median and the p95 (nearest rank) time from sending the
  question to the scored answer.
- **The eval gate** `trip-sql` (rides case): a cell passes with at least 48 of its 60 runs, 16 of its 20 cases and 3
  of the 5 cases of each group passed. It is a regression check over 20 cases, not an accuracy estimate.

Every run's result is written to `cases.jsonl` in the run's folder (cell, case, group, run, outcome, refused,
seconds, CAD, the reply): the folder `--out` names, or `runs/matrix-` and the start time without it. `groups.py`
compares two cells of a saved run again without a model call, as in Run it:
`uv run python ext-eval-matrix/groups.py runs/matrix-rides/cases.jsonl --compare A,B`.

### The rides case

`--case rides` runs the SQL step of a rides chat: a question about the trips of one day in three Manhattan pickup
zones goes in, one DuckDB query (or `CANNOT_ANSWER`) comes out. The table is `trips` (`trip_id`, `trip_state`,
`pickup_zone_id`, `requested_time`, `driver_id`, `fare`, `trip_miles`), with DS01 rows: the TLC High Volume For-Hire
Vehicle trips of Uber picked up in Manhattan in March 2024.

- Offline the table is `data/trips_hour.csv` (written by `data/make_trips_hour.py`): the 837 trips requested in zones 48, 161 and 230 on 2024-03-14 between
  08:00 and 09:00, the DS01 rows of the course's Checkpoint 1 example
  ([cp1-example](https://github.com/cpsc436c-2026w1/cp1-example), `sim/seed/ds01_sample.csv`), in the columns above.
  It is one morning hour of the day the questions are about, not the whole day.
- The commands on this page use that hour. For the whole day, `data/make_trips_day.sql` builds SIM01 from DS01
  (19,074 trips: 4,345, 7,661 and 7,068 in the three zones <!--src: measured: DuckDB over DS01 by request day, 2026-11-21 -->)
  as `data/trips.parquet`, reading the month's DS01 file (about 500 MB) over HTTPS. From `prompt-optimization/`:
  `uvx --from duckdb-cli duckdb < ext-eval-matrix/data/make_trips_day.sql`. Check its rows with
  `uvx --from duckdb-cli duckdb -c "SELECT count(*) FROM 'ext-eval-matrix/data/trips.parquet'"` (19,074), then add `--trips ext-eval-matrix/data/trips.parquet` to the matrix (`run_live.sh` adds it when the file is
  there). The reference values are queries, so they are computed on the table that is loaded.
- 20 test cases, 5 in each group: `one-zone` (trips of one zone), `time-window` (trips in a time window),
  `zone-compare` (two zones compared), `fare-sum` (fares and miles summed or averaged). In each group 4 cases have a
  reference query and 1 asks for something the table does not have (shared rides, another day, pickup waits, tips).
- Offline the SQL step is a stub (`rides.py`, `data/stub_rides.yml`) with three habits a rule in the system prompt
  stops: `BETWEEN` for a time window, one zone where two were asked, a query where the table has no column. The
  larger stub model has only the first. `data/after-example.txt` is a prompt with the three rules, **written by
  hand** for the stub; for your own `after` prompt, run `main/` on your questions.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Count each case once (R81) | A case passes when it passes in at least 2 of its 3 runs | The runs of one case share its question and its difficulty, so they are not independent tests: counting each case once keeps the number of tests at the number of cases | ours |
| Three outcomes (R81) | `correct`, `correct refusal`, `wrong`; a refusal of an answerable case counted within `wrong` and shown | A step that refuses what it cannot answer is better than one that guesses; the cases without a reference are what show it | ours |
| An exact test for each group and for all cases (R82, R83) | The exact McNemar test on the paired cases; a bootstrap interval only on all cases, labelled descriptive | With 5 binary cases a percentile bootstrap says "more than chance" where the exact test does not: with 3 of 5 cases won by B and the rest tied, a resample shows no difference with probability 0.4^5 ≈ 0.010, so the interval leaves 0 out, while p is 0.25 <!--src: ours: the binomial distribution --> | ours; the exact form is `scipy.stats.binomtest(b, a + b, 0.5)`, checked against the binomial sum in `tests/test_groups.py` |
| `too few cases` under 6 discordant cases (R83) | Mark the row; still print its difference and p | 6 is the smallest count at which a two-sided p under 0.05 can happen (0.03125 at 6 of 6) | ours |
| The bootstrap stratified by group, on each case's share of runs (R82) | Resample within each group, the same draw for both cells | The group sizes stay those of the test file in every resample, and the share keeps the difference between 3 of 3 and 2 of 3 that the vote throws away | ours |
| The comparison reads saved results (R84) | `cases.jsonl` and `groups.py`, never a second matrix run | The comparison then does not add to the bill, and the same runs with the same seed give the same table | ours |
| Whole numbers exactly, other values within 0.01 (R86) | The rides metric in `score.py`; the cafe keeps `main/metric.py` | A count is right or wrong (19,075 against 19,074 is wrong); two correct fare queries differ only below the cent. `main/metric.py` keeps its relative tolerance (1e-6) for the cafe's exact data | ours |
| A cell without a model (R87) | `no-model/templates`, scored on the same cases with the same metric | The stub replays fixed model replies; the templates are a version of the step that could ship without a model, so the matrix shows what the model adds over it | ours |
| CAD per answer and time per cell (R88) | CAD per answer, median and p95 time beside the pass rate | A cell that passes as often at a tenth of the cost, or without a model, is the cheaper choice | AGENTCOST02-BP01 |
| Groups written by the team (R80) | A `group` field in the test file | A group the analyst named is one the team can act on; a cluster found by code may not be | ours |
| The rides data offline (R86) | The 837 DS01 trips of 08:00 to 09:00 that the course's Checkpoint 1 example ships, and a script for the whole day | The tests need the table without a download; the questions are written so that their references hold on the hour and on the day | Course constraint (offline first) |
| The per-group interval (R82) | Off; `--group-intervals` turns it on | On a group of 5 cases a percentile bootstrap can leave 0 out where the exact test gives p 0.25 (the row above), so an interval on a group row would read as a difference; read the group's counts and its exact p | ours |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The requirement numbering continues from the recipe's numbering. The
scenarios of the first four are in `features/extensions.feature`, those of the groups and the paired comparison in
`features/eval_by_group.feature`.

- **R70** (optional) Where `ext-eval-matrix` is used, the recipe shall report one row per model and prompt, with
  the median pass rate, every run, the answers passed, the failure types, the Canadian dollars spent and the
  Canadian dollars per 1,000 answers.
- **R71** (optional) Where `ext-eval-matrix` is used, the recipe shall include the seed prompt as a row for every
  model.
- **R72** (optional) Where `ext-eval-matrix` is used, the recipe shall make no search and no reflection call.
- **R73** (complex) Where `ext-eval-matrix` is used, if a model call is refused by `--max-calls` or `--max-cad`, or
  fails at the endpoint, then the recipe shall stop the matrix, report the cells that finished and what was
  spent in all (the cell that was cut included), and exit with code 1.

**Groups and the paired comparison**

- **R80** (unwanted) If a test case of the matrix does not have a group, then the recipe shall stop before the first
  model call with exit code 2 and name the case.
- **R81** (event) When a cell of the matrix finishes, the recipe shall score each run as `correct`, `correct refusal`
  or `wrong`, count each case once as passed when it passed in at least 2 of its 3 runs, and report for the cell and
  for each group the cases in each outcome out of the cases.
- **R82** (event) When the matrix is run with `--compare A,B`, the recipe shall report for each group and for all
  cases the cases passed by each cell, the cases passed by B only and by A only, the difference in the share of cases
  passed (B minus A) and the exact McNemar p, and for all cases also a descriptive 95% interval of the difference in
  the share of runs passed from a bootstrap stratified by group with 10,000 resamples, the same resample for both
  cells and seed 0.
- **R83** (unwanted) If a row of the comparison has fewer than 6 cases that pass in one cell only, then the recipe
  shall print its difference and p with the mark `too few cases`.
- **R84** (event) When the same runs are compared again with the same seed, the recipe shall print the same table.
- **R85** (unwanted) If the matrix stops before a compared cell finishes, then the recipe shall leave the comparison
  out, name the cell that was cut, and exit with code 1.
- **R86** (event) When the SQL step of the rides case answers a test question, the recipe shall score the answer
  `correct` when its query's value equals the reference value (a whole number exactly, any other value within
  0.01), `correct refusal` when it returns `CANNOT_ANSWER` for a case without a reference, and `wrong` otherwise,
  and report for each cell whether it passes the eval `trip-sql` (at least 48 of its 60 runs, 16 of its 20 cases
  and 3 of the 5 cases of each group).
- **R87** (optional) Where the matrix has the cell `no-model/templates`, the recipe shall answer each case with the
  SQL templates of `templates.py` without a model call, score it with the same metric, and compare each model cell
  with it.
- **R88** (event) When a cell of the matrix finishes, the recipe shall report its Canadian dollars per answer and
  the median and p95 time of its answers.

## Run it

Run these commands on your laptop, in `prompt-optimization/`, offline on the stub: no token, no gateway call, nothing
in AWS (the recipe README, [Where each part runs](../README.md#where-each-part-runs)). `--out` names each run's
folder, so the next command can find it. The cafe case, with the after prompt the main scenario writes:

```bash
uv run python -m main.run --out runs/tune     # writes runs/tune/instructions_after.txt
uv run python ext-eval-matrix/matrix.py --prompt after=runs/tune/instructions_after.txt --out runs/matrix-cafe
```

The rides case, with the hand-written example prompt and two comparisons (each model cell is also compared with
`no-model/templates`), then the same comparison again from the saved runs, with no model call:

```bash
uv run python ext-eval-matrix/matrix.py --case rides --prompt after=ext-eval-matrix/data/after-example.txt \
  --compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after --compare qwen3.6-35b-a3b/after,qwen3.8-27b/after \
  --out runs/matrix-rides
uv run python ext-eval-matrix/groups.py runs/matrix-rides/cases.jsonl --compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after
```

Each matrix run prints its report (What you should see) and the folder it wrote; `groups.py` prints the comparison
table again, the same as in the report.

Options: `--models qwen3.6-35b-a3b,no-model` for fewer cells, `--seed 1` for another bootstrap draw, `--cases FILE`
for your own test file, `--trips FILE` for another table, `--group-intervals` for the per-group interval (off;
Decisions), `--pace 2.1` for a key under 60 requests a minute (Run it live). `--runs N` changes the runs per case: a
case then passes with two thirds of its runs, rounded up (4 of 5), and the gate `trip-sql` keeps its shares (80% of
the runs and of the cases, 60% of each group); the counts on this page are for the default of 3.

### Run it live

The rides case through the course LLM gateway, on your laptop, in `prompt-optimization/` (no AWS sign-in; your
course token authenticates the requests). Every model call is paid from your team's gateway pool (Cost and
clean-up). Set the gateway's address and your token as in the recipe README,
[Laptop, with the course LLM gateway](../README.md#2-laptop-with-the-course-llm-gateway), then:

```bash
bash ext-eval-matrix/run_live.sh             # the rides matrix with the bundled after-example.txt; about 0.014 CAD
```

`run_live.sh` runs the matrix with `--max-cad 0.03`, two comparisons, prints the report, and copies the report,
`cases.jsonl` and `spend.json` to `output/ext-eval-matrix-rides-gateway/`, in place of the recipe's capture there
(`git restore output/ext-eval-matrix-rides-gateway` brings it back). Its one argument, a prompt file, replaces
`after-example.txt`: pass only a prompt written for the rides SQL step, never the cafe run's
`runs/tune/instructions_after.txt`, which is written for the cafe questions.

The cafe case on the gateway, the same matrix as offline with `--target gateway`:

```bash
uv run python ext-eval-matrix/matrix.py --target gateway --prompt after=runs/tune/instructions_after.txt --out runs/matrix-cafe-gateway
```

Optional: the `@live` scenario runs one model on the gateway, 60 more calls, paced (about 0.0011 CAD), and checks the
eval gate `trip-sql`. Skip it when the matrix report is all you need:

```bash
LIVE_COURSE_GATEWAY_URL="$COURSE_GATEWAY_URL" LIVE_KB_MCP_TOKEN="$KB_MCP_TOKEN" uv run behave --tags=@live
unset KB_MCP_TOKEN
```

The matrix sends its calls one after another, about one a second, and does not retry a refused call (R73). A key
may send 120 requests a minute, all models together (price sheet, "Models: the course LLM gateway"), and teammates
who share a key share that rate. A run that meets a lower limit stops with HTTP 429. For a limit of L requests a
minute, space the calls by more than 60/L seconds: `MATRIX_PACE_S=2.1 bash ext-eval-matrix/run_live.sh` for 30 a
minute (or `--pace 2.1`; the wait is not counted in an answer's time). After a 429, wait a minute for the window to
clear before you run again. A paced run of 240 calls takes about 12 minutes.

## What you should see

The cafe stub run is in `output/ext-eval-matrix/report.md`. With the stub, the larger model passes more questions with the
seed prompt, both models pass the same questions with the tuned prompt, and the larger model spends twice as much
per answer (the gateway's price table, not a measurement). A run on the course LLM gateway (2026-11-12, `--prompt
after=` the main scenario's gateway run, `--max-cad 0.20`) is in `output/ext-eval-matrix-gateway/report.md`:
`qwen3.6-35b-a3b` went from a median of 83% with the seed prompt to 100% with the tuned one, `qwen3.8-27b` was at
100% with both, and the larger model spent 3.4 to 6.5 times as much per answer (0.1375 against 0.0408 CAD per
1,000 answers with the tuned prompt, 0.1441 against 0.0221 with the seed)
. That capture predates the group tables; the column "CAD per 1,000 answers" divides by the calls, which can be
more than the answers when DSPy calls again after a reply it could not parse (0.000419 CAD for 18 answers is 0.0233
per 1,000 answers, not 0.0221). The new "CAD per answer" column divides by the answers.

**The rides case, offline: the stub's numbers, not a model's** (`output/ext-eval-matrix-rides/report.md`):

```
| no-model | templates | 55% | 55% / 55% / 55% | 33/60 | answered an unanswerable case 3, refused an answerable case 24 | ...
| qwen3.6-35b-a3b (stub) | seed | 40% | 40% / 40% / 40% | 24/60 | answered an unanswerable case 12, wrong value 24 | ...
| qwen3.6-35b-a3b (stub) | after | 95% | 95% / 95% / 95% | 57/60 | wrong value 3 | ...
| qwen3.8-27b (stub) | seed | 80% | 80% / 80% / 80% | 48/60 | wrong value 12 | ...
```

**The rides case on the course LLM gateway, 2026-11-23** (`output/ext-eval-matrix-rides-gateway/report.md`,
`run_live.sh` with the hand-written `after-example.txt`, every call asking for 16,384 output tokens, no pace):

```
| no-model | templates | 55% | 55% / 55% / 55% | 33/60 | answered an unanswerable case 3, refused an answerable case 24 | 0.000000 | 0.0000 |
| qwen3.6-35b-a3b | seed | 90% | 80% / 90% / 90% | 52/60 | SQL error 1, answered an unanswerable case 5, wrong value 2 | 0.001014 | 0.0169 |
| qwen3.6-35b-a3b | after | 100% | 100% / 100% / 95% | 59/60 | answered an unanswerable case 1 | 0.001252 | 0.0209 |
| qwen3.8-27b | seed | 90% | 90% / 85% / 95% | 54/60 | wrong value 6 | 0.007390 | 0.1232 |
| qwen3.8-27b | after | 100% | 100% / 95% / 100% | 59/60 | SQL error 1 | 0.004245 | 0.0707 |
```

- Every model cell passes the gate `trip-sql`; the cell without a model fails it (11 of 20 cases, `zone-compare`
  1 of 5). The real models do not have the stub's habits: the cheaper model with the seed prompt passes 18 of 20
  cases where its stub passes 8.
- Seed against after on the cheaper model: 2 cases passed by `after` only, 0 by `seed` only, p 0.50: on these 20
  cases the hand-written prompt is not shown to be better. Its gain is also in refusals: 1 run answered an
  unanswerable case, against 5 with the seed prompt.
- Each model cell against the cell without a model: p 0.0039 to 0.039 on all cases, except `qwen3.8-27b/seed`
  (9 cases passed by it only, 2 by the templates only, p 0.065); these are the only rows not marked `too few cases`.
- The larger model is a reasoning model: with the seed prompt it wrote 28,076 output tokens for 60 answers, and its
  p95 time is 18.8 s against under 1 s for the cheaper model. Its costliest answer (`t1`, seed prompt, passed) wrote
  about 5,400 output tokens: above 4,000, the task model's cap before the course rule of 16,384.
- The run before (same day, task-model cap 4,000, paced at 2.1 s) had the medians 90% and 95% for the cheaper
  model, seed and after, and 95% and 100% for the larger; this run has 90% and 100% for both. Every model cell
  passed the gate in both runs; three runs of 20 cases do not tell the two runs apart.

The cell without a model runs first: it costs nothing, so a spend cap that stops the matrix never cuts the baseline
of the comparisons.

In the offline stub run with the bundled data and the default settings, these hold; they are not results to expect
from a gateway run or from changed inputs:

- The larger stub model passes 16 of 20 cases with the seed prompt, but fails the gate: `time-window` has 2 of its
  5 cases. The group rows show where a cell fails; the all-cases number alone would pass it.
- The cell without a model passes 4 of 5 `time-window` cases and 1 of 5 `zone-compare` cases: a fixed template is
  as good as a model on the questions it was written for, and refuses the rest (8 of its wrong cases are refusals).
- Seed against after on the cheaper model: 11 cases passed by `after` only, 0 by `seed` only, p 0.0010 on all cases;
  every group row is marked `too few cases`.
- The stub's runs do not vary, so each case's runs are all the same and the interval is narrow; through the gateway
  they vary. The times in the cost and time table are the stub's, not a model's.

## Cost and clean-up

Nothing runs in AWS, so nothing goes to the group lease of 30 USD (0%). Prices: the course LLM gateway's rates, from
the course price sheet, page `436c-student/prices.md` on the course server, section "Models: the course LLM
gateway" (read 2026-11-19): 0.05 and 0.10 CAD per million tokens in and out for `qwen3.6-35b-a3b`, 0.10 and 0.20 for
`qwen3.8-27b`. The gateway's charges are in CAD and come from the team's gateway pool, apart from the AWS lease. 1 GB = 2^30 bytes. The
counts and the CAD are *measured*: the live run of the rides case, 2026-11-23, every call asking for 16,384 output
tokens, as the gateway recorded it for the key (requests, tokens, CAD per model; the key's usage before and after
the run) <!--src: measured: gateway usage rows of the run's key, 2026-11-23, before/after delta -->, with
the recipe's own count per cell from `output/ext-eval-matrix-rides-gateway/spend.json`. The 30-day and year columns
are *computed* from one run. USD at 1.45 CAD, the course's planning rate.

| Part | One matrix run (2 models × 2 prompts × 20 cases × 3 runs = 240 model answers, and 60 template answers) | 30 days at one run a day | A year at one run a day |
|---|---|---|---|
| `qwen3.6-35b-a3b`, both prompts: 120 requests, 36,996 tokens in, 4,157 out (recipe's count: `seed` 0.001014, `after` 0.001252 CAD) | 0.002370 CAD | 0.071 CAD | 0.87 CAD |
| `qwen3.8-27b`, both prompts: 120 requests, 41,316 tokens in, 37,516 out (recipe's count: `seed` 0.007390, `after` 0.004245 CAD) | 0.011737 CAD | 0.35 CAD | 4.28 CAD |
| `no-model/templates`, the counts and the tests, on the laptop | 0 | 0 | 0 |
| **Total** | **0.0141 CAD (0.0097 USD)** | **0.42 CAD (0.29 USD)** | **5.15 CAD (3.55 USD)** |

The gateway's figure is 1.5% above the recipe's own count (0.014107 against 0.013901 CAD): the gateway rounds
each request up to the next millionth of a dollar, so the key's budget is the bill that counts. The estimate before
the run, from the cafe questions' rates, was 0.021 CAD: the larger model's rides answers cost less than its cafe
answers. A run is about 0.19% of a team's gateway pool of 7.25 CAD (5 USD; same section of the price sheet), a month of daily runs about 5.8% and
a year about 71%: run the matrix daily only while you choose, and drop the other model's cells once you have
chosen. `run_live.sh` sets `--max-cad 0.03`, about twice a run. The `@live` scenario (60 calls on
`qwen3.6-35b-a3b`, paced) spent 0.0011 CAD on 2026-11-23 and passed.

Clean-up, on your laptop in `prompt-optimization/`: the matrix and `main.run` write under `runs/`, and `run_live.sh`
also copies its report into `output/ext-eval-matrix-rides-gateway/`. Save any report you need from both, then run
`uv run python clean_up.py`: it lists what it would delete (the folder `runs/`) and deletes nothing. Read the list,
then `uv run python clean_up.py --yes`. It keeps `output/`; restore the recipe's capture there with `git restore
output/ext-eval-matrix-rides-gateway` if you do not keep yours.

## Adapt it

Add models with `--models a,b,c` (the model ids the course LLM gateway lists) and prompts with more `--prompt`
flags. Set `--max-calls` and `--max-cad` before a gateway run. Each model cell gives `runs × test questions`
answers; a reply DSPy cannot parse makes it call again, so budget for more requests than answers (the caps count
requests).

1. **Your groups.** Give every test case a `group`: the kinds of question your users ask, 5 or more cases each.
   Mark with `answerable: false` the cases your data cannot answer, so that a refusal is counted as right.
2. **Your table and step.** Change `rides.py` in your copy of the recipe: `SCHEMA`, `SEED_INSTRUCTIONS` and the
   table it loads; keep `run_sql` the way your app runs a query. `matrix.py` imports it by name (`import rides`), so
   a copy under another name is not used until you change that import. Run the stub matrix once with `--runs 1`
   and read a few lines of its `cases.jsonl` before you trust the report.
3. **Your version without a model.** Write the templates of `templates.py` for the questions your users ask most.
   A step that returns documents scores with `score.score(case, cited_ids, "citation-set")`: a case lists
   `reference_ids` and `required_ids`, and its search's top k results are its answer (`no-model/top-k`).
4. **Read the group rows** for where a difference comes from, and report the all-cases p beside the paired counts
   and the difference; a large p does not show that the cells are equal. A group of 5 cases is always marked
   `too few cases`.

## Known limits

- The rides `after` prompt is written by hand for the stub; a run of `main/` on rides questions would replace it.
- The bundled table is one hour (08:00 to 09:00), not the whole day the questions name; the references hold on both.
