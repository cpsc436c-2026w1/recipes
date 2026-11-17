# Answer cache

This recipe serves the extension "Answer or retrieval cache". It builds on `main/`. Read the recipe README first.

## What it changes in the main scenario

Before step 3 (the model call), `cache.cached_answer` looks the question up. If it finds a stored answer, the stored reply, SQL and rows
come back with status `cache_hit`, no model call and no cost. On a miss the main scenario runs and an `answered`
result is stored. Nothing else in `main/` changes.

## How the key is built

| Part | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Question (R80) | Lower case, single spaces, trailing `?.!` removed; otherwise exact | An exact cache matches normalized questions, which can merge different questions. A semantic cache ("orders count" for "how many orders") can match the wrong question and needs its own eval | AI Engineering ch. 10, Step 4. Reduce Latency with Caches (exact caching, semantic caching) |
| Data version (R84) | A hash of the Parquet files' bytes (`main/data.py`, `data_version`) | New data gives new keys. An old answer is never served for new data, so nobody has to clear the stored answers from the cache | L16; course choice |
| Model and prompt (R84) | The model's name and a hash of the system prompt and the tool definition (`main/prompts.py`, `prompt_version`) | After you change the model or the rules, an answer the old ones wrote is not served. Without this part the old answers stay for up to 24 hours | Agentic AI Lens AGENTCOST02-BP03 (a cache is correct only while entries that went stale are no longer served); GENOPS03-BP01 (versioned prompts) |
| Who asks (R81, R86) | `shared` for questions without "I", "me", "my"; `user:<hash>` for those with them | One customer's spending must never reach another customer | GENSEC01-BP03 (least privilege on data); AI Engineering ch. 10 warns that a cached answer built from one user's data can reach another user. The book's advice is not to cache user-specific questions; keeping them under the user's own key is a course choice |
| Clock words (R82) | Questions with "today", "now", "this week" are not cached | Their answer changes with the date, not with the data | AI Engineering ch. 10 (time-sensitive queries); the word list is a course choice |
| What is stored (R83) | Only `answered` results, for 24 hours (course default), with a DynamoDB time to live | Refusals and errors are cheap to repeat and may be fixed by the next deploy | Agentic AI Lens AGENTCOST02-BP03 (caching to avoid repeated model calls; an expiry time that fits how fast the answer goes stale) |
| Where (R85) | DynamoDB table `$PREFIX-chat-cache`, key `cache_key`, on demand | A lookup by key is one read. The reader also checks `expires_at` because DynamoDB deletes expired items within a few days of their expiry time, not at that second | L12–L13 (DynamoDB keys); course constraint |

A provider's prompt cache (GENCOST03-BP03) is a different thing: it makes a repeated prompt prefix cheaper but still
calls the model. This cache skips the call.

**In front of the guardrails.** `cache_run.py` puts the cache in front of the main scenario. If you put it in front
of `guarded_answer` (`ext-guardrails-redteam`), every answer comes from the asking customer's rows alone, so "How
many orders are there?" has a different answer for each customer. Call `cached_answer(..., per_user_rows=True)`:
every result is then kept under the asking user's key, and the cache does not store or serve anything as `shared`. One thing is
still not in the key: the guardrails' checks. A stored answer passed the input and output checks of the day it was
stored; when you change a check, add a version of your checks to `answer_version`, or the old answers are served
unchecked for up to 24 hours.

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/answer_cache.feature`.

- **R80** (optional) Where `ext-answer-cache` is used, the recipe shall answer a question whose key (normalized
  question, data version, model and prompt version, who asks) is stored with the stored result, status `cache_hit`
  and no model call.
- **R81** (optional) Where `ext-answer-cache` is used, the recipe shall keep the result of a question with a personal
  word (for example "I", "me", "my"; the whole list is `_PERSONAL` in `cache.py`) under the asking user's key only.
- **R82** (optional) Where `ext-answer-cache` is used, the recipe shall send a question with a clock word (for
  example "today", "now", "this week"; the whole list is `_CLOCK` in `cache.py`) to the model every time.
- **R83** (optional) Where `ext-answer-cache` is used, the recipe shall store a result only when its status is
  `answered`, for 24 hours.
- **R84** (optional) Where `ext-answer-cache` is used, the recipe shall send a stored question to the model again
  after the Parquet files, the model's name or the system prompt change.
- **R85** (complex) Where `ext-answer-cache` is used, if a read or a write of the cache table fails, then the recipe
  shall answer the question through the model.
- **R86** (optional) Where `ext-answer-cache` is used in front of an engine that holds only the asking user's rows
  (`per_user_rows=True`), the recipe shall keep every result under the asking user's key only.

## Run it

```bash
uv run python ext-answer-cache/cache_run.py --target local --model stub    # offline, cache in memory
uv run python ext-answer-cache/cache_run.py --target aws                   # gateway + $PREFIX-chat-cache
```

It asks every question of `main/questions.txt` three times: twice as `c-0007`, once as `c-0012`.

## What you should see

Check the last lines of `output/cache-stub.txt`:

```
{"user": "c-0012", "question": "How many orders are there?", "status": "cache_hit", "reply": "The answer is 1200."}
{"user": "c-0012", "question": "How much did I spend in total?", "status": "answered", "reply": "The answer is 795.97."}
7 cache hits, 11 questions sent to the model
```

The second user gets the shared answers from the cache but their own spending from a new query. The weather
question is sent every time because it contains a clock word. The rejected DELETE is sent every time because it is not stored. A real run with
`--target aws` (2026-11-12) in [`output/cache-gateway.txt`](../output/cache-gateway.txt) gives the same counts: 7 cache
hits, 11 questions sent to the model.

## Adapt it

Add your users' personal words ("our team", "my store") to `_PERSONAL` and your time words to `_CLOCK` in
`cache.py`. Measure the hit rate on a day of real questions from the traces before you claim a saving.
