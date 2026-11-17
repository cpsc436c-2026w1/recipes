# Structured output: does the reply fit a JSON schema?

Extend the [llm-probes recipe](../README.md) with group `structured-output`, in
[structured_output.py](structured_output.py).

## What it adds to main/

For each chat model that answered the main scenario's chat, the group sends two requests with the same `response_format`.
The format is a JSON schema, which specifies the fields and types expected in a JSON reply, with `city` and `population`:

| Row | User message | If it says `no` |
|---|---|---|
| output fits a JSON schema | "Extract the city and its population." | parse and check the reply yourself, and retry on a miss |
| schema wins over the prompt | "Reply with one plain English sentence, not JSON." | the schema is a request the model may ignore, so keep checking the reply |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| second request (R71) | the prompt asks for prose while the schema asks for JSON | JSON on both shows the endpoint enforces the schema while the model generates; prose shows it does not | output format (L15) |
| check (R70) | parse the reply and compare its keys and types | a reply that looks like JSON is not enough | L15 |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R70** (optional) Where the group `structured-output` is run, the recipe shall report `output fits a JSON
  schema` as `yes` only when the reply parses as JSON with exactly `city` and `population`, the population an
  integer.
- **R71** (optional) Where the group `structured-output` is run, the recipe shall send the same schema with a
  user message that asks for a plain sentence, and report `schema wins over the prompt` by the same check.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,structured-output
```
