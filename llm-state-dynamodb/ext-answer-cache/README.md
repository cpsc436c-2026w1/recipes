# Answer cache before retrieval

> ⚠️ Run commands on your local device or in AWS CloudShell. Offline runs use moto; sandbox runs use a DynamoDB table.

Read the recipe README first. This is the extension "Answer or retrieval cache", and it builds on `main/`.

## What it changes in the main scenario

Before retrieval and the model call, `answer_cache.answer_with_cache` looks the question up in the table
`$PREFIX-answer-cache`. On a hit the stored reply comes back, and the app does not retrieve or call the model. On a miss the app
retrieves, calls the model, and stores the answer. History, keys and the owner check in `main/` do not change.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Kind of cache (R51) | Exact: lower case, single spaces, trailing `?.!` removed, otherwise the same words | An exact cache matches the question's words after normalization. A semantic cache matches by meaning and can serve an answer to a differently worded question. It needs its own evaluation. The Agentic AI Lens counts exact-match lookups as an anti-pattern, because questions that mean the same and are worded differently miss: this recipe takes those misses on purpose | [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "Step 4. Reduce Latency with Caches" (exact and semantic caching; the value of a semantic cache is "more dubious"); Agentic AI Lens [AGENTCOST02-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp03.html) (caching to reduce redundant model calls; its second anti-pattern is exact-match only) |
| Version in the key (R52) | The search index version (`idx-1`, `idx-2`, ...) is part of the hashed key | After re-indexing, increase the index version. Old answers stop matching, without anyone having to clear them from the cache | L17 (versioned indexes); AGENTCOST02-BP03 (invalidate entries when the source data changes) |
| Who asks (R53, R54) | Lookup tries the user's own key, then the shared key. A question with a personal word ("my", "I", "our", ...) tries only the user's key | Shared answers are reused across users. Personal ones stay with their user. The book goes further: a user-specific question is unlikely to be asked again by anyone else and "shouldn't be cached". This recipe stores it for its own user. Leave such answers out of the cache if your hit rate says they never come back | AGENTSEC01-BP01 (memory isolation); [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "Exact caching" |
| What is stored as shared (R53) | Only an answer to a question without personal words whose retrieved documents have no `owner` | A question without "my" can still retrieve one user's private document; that answer must not reach another user | [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "Exact caching": a seemingly generic question answered from user X's data and cached reaches user Y; AGENTSEC01-BP01 |
| User id in the table (R56) | The key's scope contains a hash of the user id. The cache stores the question, reply and sources, but not the user id | The cache can be read and debugged without a list of who asked what. The hash is SHA-256 without a salt, an extra value added before hashing. It hides an id that cannot be guessed, such as a Cognito `sub`. It does not hide an email address or a student number, which anyone can hash and compare | Course choice |
| Where the lookup sits (R50) | Before retrieval | A hit skips the search and the model call, which is where the time and the money go | AGENTCOST02-BP03 |
| Expiry (R55, R58) | 24 hours for a shared answer, 1 hour for an answer stored under one user's key; TTL on `expires_at`, and the reader checks the time | The version bump accounts for new data in the index. The TTL bounds everything else, such as a fixed prompt. A user's own data (an order, an account) changes without a re-index, so their answers go stale sooner. DynamoDB deletes an expired entry days later, so the reader does not trust TTL alone | [DynamoDB Developer Guide, Using time to live (TTL)](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html); Agentic AI Lens [AGENTCOST02-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp03.html) (its third anti-pattern: one cache TTL for every kind of entry); [AI Engineering ch. 10](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html), "Exact caching" (time-sensitive queries make less sense to cache) |
| Hit rate (R59) | `AnswerCache` counts every lookup as one hit or one miss. The run prints both. A handler emits the hit or miss of each request, since a cache object kept between Lambda requests keeps counting | A cache saves money only when questions come back. Emit the counts as a metric and compare them with the cost of the table | Agentic AI Lens [AGENTCOST02-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost02-bp03.html) (track cache hit rates per caching layer) |
| Table | `$PREFIX-answer-cache`, partition key `cache_key`, on demand | A lookup is one GetItem | L12 (read by key) |

A provider's prompt cache makes a repeated prompt prefix cheaper, and the model is still called. The answer cache
skips the model call.

## Requirements

The extension's requirements go on from the recipe's (in the [recipe README](../README.md#requirements), with the
table of EARS forms). Their scenarios are in [`features/answer_cache.feature`](../features/answer_cache.feature)
and [`features/local_runs.feature`](../features/local_runs.feature).

- **R50** (optional) Where the answer cache is used, the recipe shall look a question up before retrieval and, on
  a hit, return the stored answer with no retrieval and no model call.
- **R51** (optional) Where the answer cache is used, the recipe shall treat two questions as the same when they
  differ only in capitals, in runs of spaces and in a trailing `?`, `.` or `!`.
- **R52** (optional) Where the answer cache is used, the recipe shall return an answer only for the index version
  it was stored with.
- **R53** (complex) Where the answer cache is used, if a question has a personal word or a retrieved document has
  an owner, then the recipe shall never return the answer built from them to another user.
- **R54** (complex) Where the answer cache is used, when a question has a personal word, the recipe shall look it
  up only under the asking user's key.
- **R55** (complex) Where the answer cache is used, while an entry stored under the shared key is 24 hours old or
  older, the recipe shall treat it as a miss.
- **R56** (optional) Where the answer cache is used, the recipe shall store no user id in any item of the cache
  table.
- **R57** (complex) Where the answer cache is used, when `cache_run.py --target local` runs, the recipe shall print
  6 questions with 2 hits, 4 misses, 4 retrievals and 4 model calls.
- **R58** (complex) Where the answer cache is used, while an entry stored under a user's key is 1 hour old or older,
  the recipe shall treat it as a miss.
- **R59** (optional) Where the answer cache is used, the recipe shall count every lookup as one hit or one miss.


## Run it

Read the [recipe README](../README.md#run-it) for where each part runs and how to sign in for the AWS run.

`--target aws` was run by us on 2026-11-14, with the one-hour expiry of personal answers and the hit and miss
counts (`output/ext-answer-cache.txt`): 2 hits and 4 misses, as offline.

From your local device or AWS CloudShell, in `llm-state-dynamodb`, run the offline command or the sandbox command:

```bash
uv run python ext-answer-cache/cache_run.py --target local    # offline, moto
uv run python ext-answer-cache/cache_run.py --target aws      # your sandbox: the table $PREFIX-answer-cache
```

Clean-up and cost: `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` removes the table with the recipe's other tables; one run makes about
twenty DynamoDB requests.

## What you should see

`output/ext-answer-cache.txt` (a run on AWS, 2026-11-14; the offline run prints the same lines):

```
user-ana  idx-1  miss  shared  'What is the return policy?' -> Items can be returned within 30 days with a receipt.
user-ben  idx-1  hit   shared  'what is the  return policy' -> Items can be returned within 30 days with a receipt.
user-ana  idx-1  miss  user    'Where is my order?' -> Order 1042 shipped yesterday.
user-ben  idx-1  miss  user    'Where is my order?' -> Order 2077 is waiting for stock.
user-ana  idx-1  hit   user    'Where is my order?' -> Order 1042 shipped yesterday.
user-ben  idx-2  miss  shared  'What is the return policy?' -> Items can be returned within 30 days with a receipt.
6 questions, 2 hits, 4 misses, 4 retrievals, 4 model calls
```

`user-ben` gets the shared answer from the cache and his own order from a new retrieval; after the bump to `idx-2`
the same question misses.

## Adapt it

1. Replace `stub_retrieve` with your search. Each document it returns needs `id`, `text` and `owner` (`""` when
   public).
2. Replace `stub_generate` with your model call through the course LLM gateway.
3. Read the index version from where your indexing job records it, and bump it on every re-index.
4. Add your users' personal words to `_PERSONAL` in `answer_cache.py`. Emit one hit or one miss per request from
   your handler (the second value `answer_with_cache` returns). Do not emit `cache.hits` and `cache.misses` per request:
   in Lambda an `AnswerCache` made at module level keeps counting across requests. Measure the hit rate on real questions before
   you claim a saving.
