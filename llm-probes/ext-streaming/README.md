# Streaming and usage in the stream

Extend the [llm-probes recipe](../README.md) with group `streaming`, in [streaming.py](streaming.py).

## What it adds to main/

For each chat model that answered the main scenario's chat, the group sends one request with `"stream": true` and
`"stream_options": {"include_usage": true}`. Two rows:

| Row | `yes` means | If it says `no` |
|---|---|---|
| streaming | text arrived chunk by chunk. The note gives the time to the first token | the reply comes only as a whole; a chat screen waits for all of it |
| usage in the stream | the last chunk carried the token counts | you cannot count the tokens of a streamed reply, so you cannot price it |

A reasoning model streams its reasoning first; the note counts those chunks ("26 reasoning chunks came first").

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| ask for usage (R40, R41) | `stream_options.include_usage` | without it a streamed reply does not have token counts. The course LLM gateway asks for them too | L16 |
| first token (R40, R42) | timed from the request to the first content chunk | that is the wait a person sees | L16 |

## Requirements

The form of these sentences and the six EARS forms are explained in the recipe README,
[Requirements](../README.md#requirements). The numbers go on from the recipe's. The scenarios are in
`features/extensions.feature`.

- **R40** (optional) Where the group `streaming` is run, the recipe shall send the one-word chat with `stream` true
  and usage asked for in the stream, and report `streaming` as `yes` when text arrived, with the time to the
  first token and the number of reasoning chunks that came before it.
- **R41** (optional) Where the group `streaming` is run, the recipe shall report `usage in the stream` as `yes`
  only when a chunk of the stream carried the token counts.
- **R42** (complex) Where the group `streaming` is run, if the streamed request fails (an error status, no reply
  in time), then the recipe shall report `streaming` as `no` with the reason and go on with the next probe.

## Run it

From your local device, run `main/probe.py` against the endpoint in `LLM_BASE_URL`. The recipe does not run anything in AWS
(the recipe README, [Where each part runs](../README.md#where-each-part-runs)). In `llm-probes`, with the two
variables set:

```bash
uv run python main/probe.py --groups main,streaming
```
