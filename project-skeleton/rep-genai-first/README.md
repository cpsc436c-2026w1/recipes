# Generative AI first

The assistant uses the DynamoDB table of operational first. A user message goes through the course LLM gateway to a model. The model may
call one tool, `count_updates(start, end)`, whose only DynamoDB action is `BatchGetItem` on the table's minute
counters. The tool result goes back to the model, and the model's reply states the number. `grounded()` then checks that
the number in the reply is a count the tool returned.

The assistant reads current state from the table. `lake_example.py` asks the same question of the lake: one fixed
SELECT over the table's export (Athena in AWS, DuckDB offline), without a model.

| File | What it holds |
|---|---|
| `genai.py` | the system prompt, the tool and its checks, the tool's IAM policy, the loop with its step limit, the reply check, the gateway client |
| `stub.py` | a model stand-in with a fixed reply, for tests and `--llm stub` |
| `run.py` | the steps in order: `--target local` (default) or `--target aws`; `--llm stub` or `--llm gateway` |
| `lake_example.py` | the count of updates in the last hour over the lake, with the table's count beside it |

```bash
uv run python rep-genai-first/run.py                      # offline: local table, stub model
uv run python rep-genai-first/lake_example.py             # offline: local export, DuckDB
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'   # from the course page, or your team repo on github.students
export KB_MCP_TOKEN='YOUR-COURSE-MCP-TOKEN'
uv run python rep-genai-first/run.py --llm gateway        # local table, a real model
PREFIX=recipe-yourname uv run python rep-genai-first/run.py --target aws --llm gateway   # after operational first
PREFIX=recipe-yourname uv run python rep-genai-first/lake_example.py --target aws        # after its export
```

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/genai.feature`.

- **R70** (event) When `rep-genai-first/run.py` asks a user message, the recipe shall send the
  course LLM gateway a system prompt with the two times of the window, the user message and the one tool
  `count_updates`, and ask for at most 400 output tokens for every other model (R79 names `qwen3.8-27b`).
- **R71** (optional) Where generative AI first is used, the recipe shall have the tool read the table with
  `BatchGetItem` on minute counters only.
- **R72** (unwanted) If a tool call of the model in `rep-genai-first/run.py` names another tool, or a window that is not
  in whole minutes, not in the form of the example, not ending after it starts or longer than a day, then the
  recipe shall return an error to the model as the tool result, without a call to DynamoDB.
- **R73** (unwanted) If the model of `rep-genai-first/run.py` has not replied after 3 model calls, then the recipe
  shall stop asking and say so.
- **R74** (unwanted) If the reply in `rep-genai-first/run.py` states no number that the tool returned, then the
  recipe shall print that it does not and write no page.
- **R75** (unwanted) If `rep-genai-first/run.py --llm gateway` is asked for without `COURSE_GATEWAY_URL` (or the
  older `GATEWAY_URL`) or `KB_MCP_TOKEN` (or the older `COURSE_MCP_TOKEN`), then the recipe shall stop before it
  calls a model.
- **R76** (event) When `rep-genai-first/run.py --target aws` runs, the recipe shall read the
  table of operational first and create or write nothing in AWS.
- **R77** (event) When `rep-genai-first/lake_example.py` runs, the recipe shall ask the same
  count of the view `events` over the export and print it beside the table's count, without a model call.
- **R78** (unwanted) If DynamoDB leaves counters unread in 5 reads in a row of `count_updates`, then the recipe
  shall return an error to the model as the tool result, and no count.
- **R79** (optional) Where the model is `qwen3.8-27b` (`--model` or `MODEL`), the recipe shall ask for at most 600
  output tokens in every model call of `rep-genai-first/run.py`.
- **R80** (unwanted) If a reply of the model in `rep-genai-first/run.py` stops at `finish_reason` `length` with no
  text and no tool call, or with a tool call whose arguments do not parse, then the recipe shall run no tool, print "the model used its token cap before answering", check no
  reply, write no page and exit with code 1.

Read the Decisions section of the recipe's [README](../README.md) to see why each step is built this way.
