# Tool calls and tool results

Extend the [llm-probes recipe](../README.md) with group `tool-calls`, in [tool_calls.py](tool_calls.py).

## What it adds to main/

For each chat model that answered the main scenario's chat, the group supplies one tool (`run_sql`, one string argument) and a user
message that needs it. Two rows:

| Row | `yes` means | If it says `no` |
|---|---|---|
| tool call | the model returned a tool call with a usable `query` argument | the model answers in text; tools need a different model or prompt |
| tool result used | after the tool result (48213 rows) was sent back, the reply contains that number | the model ignores what the tool returned |

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| the tool (R60, R62) | one read-only `run_sql` tool, the shape of the course's L15 tool | tools expose only what the model needs | L15, L17 |
| the tool result (R61) | a made-up number the model cannot guess | a reply with that number shows the result was used | L15 |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R60** (optional) Where the group `tool-calls` is run, the recipe shall report `tool call` as `yes` only when
  the reply holds a tool call whose arguments are JSON with a `query`.
- **R61** (optional) Where the group `tool-calls` is run, the recipe shall report `tool result used` as `yes`
  only when the reply to the tool result contains the number 48213.
- **R62** (complex) Where the group `tool-calls` is run, if the reply holds no usable tool call, then the recipe
  shall report `tool call` as `no` and send no tool result.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,tool-calls
```
