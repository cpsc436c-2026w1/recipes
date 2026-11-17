# Prompt cache: are cached input tokens reported?

Extend the [llm-probes recipe](../README.md) with group `prompt-cache`, in [prompt_cache.py](prompt_cache.py).

## What it adds to main/

For each chat model that answered the main scenario's chat, the group sends the same request twice. Each request has a system prompt of about 3,300
tokens of fixed text (3,309 and 3,345 input tokens for the two models of the captured run), then a short question. One row, `prompt cache reported`: `yes` when the second reply reports
`cached_tokens` in its usage. The note gives both times. If it says `no`, cached tokens are not reported; the endpoint
may still cache.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| prompt order (R81) | the fixed part first, the question last | a provider's cache reuses a shared start of the prompt | prompt cache (L16); GENCOST03-BP03 (cache the part of the prompt that repeats) |
| length (R81) | about 3,300 tokens | providers cache only a start longer than a minimum. On Amazon Bedrock, for example, Claude 3.7 Sonnet requires 1,024 tokens per cache checkpoint. Its cache lasts five minutes after its last hit | L16 (prompt caching); Generative AI Lens [GENCOST03-BP03](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gencost03-bp03.html) |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R80** (optional) Where the group `prompt-cache` is run, the recipe shall report `prompt cache reported` as
  `yes` only when the second reply reports more than 0 cached input tokens.
- **R81** (optional) Where the group `prompt-cache` is run, the recipe shall send the same request twice: a fixed
  system prompt of more than 10,000 characters first, a short question last.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,prompt-cache
```
