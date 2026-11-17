# Reply control: repeat at temperature 0, and max_tokens

Extend the [llm-probes recipe](../README.md) with group `reply-control`, in [reply_control.py](reply_control.py).

## What it adds to main/

For each chat model that answered the main scenario's chat, the group sends the requests in the table:

| Row | Request | If it says `no` |
|---|---|---|
| same reply on repeat | the same one-word request twice at temperature 0 | the same question can get two answers, so an evaluation of this model's answers needs several runs per question |
| max_tokens stops the reply | "count from 1 to 50" with `"max_tokens": 5` | your own limit on reply length does not hold |

**Reasoning tokens.** A reasoning model writes hidden reasoning before the reply, and those tokens count against
`max_tokens` and are billed as output. In the captured run `qwen3.8-27b` spent all 5 allowed tokens on reasoning and
the reply text was empty. Give such a model a larger `max_tokens` than the reply needs.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| limit (R51, R52) | 5 tokens on a reply that needs about 100 | the stop is plain to see: `finish_reason` is `length` | L15 |
| repeat (R50) | two calls, compared as text | a difference at temperature 0 is the cheapest sign that evals need several runs | L15, L17 |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R50** (optional) Where the group `reply-control` is run, the recipe shall report `same reply on repeat` as
  `yes` only when two one-word chats at temperature 0 return the same text.
- **R51** (optional) Where the group `reply-control` is run, the recipe shall report `max_tokens stops the reply`
  as `yes` only when a request with `max_tokens` 5 ends with `finish_reason` `length` and at most 5 completion
  tokens.
- **R52** (complex) Where the group `reply-control` is run, when a reply stopped by `max_tokens` has no text, the
  recipe shall say in the note that all its tokens were reasoning.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,reply-control
```
