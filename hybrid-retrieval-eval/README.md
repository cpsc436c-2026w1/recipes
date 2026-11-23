# Hybrid retrieval with an eval: keyword and embedding search, fused, and measured

## What you build

Build a product search over 30 products that runs two searches on the same text: a keyword search ranked by BM25 (a score
from how often the query's words occur in a product and how rare they are) and an embedding search (each text turned
into a vector of numbers; nearby vectors mean similar text). Reciprocal rank fusion (RRF) merges the two ranked lists
by each product's position in each list, not by the two searches' scores. A retrieval eval on 16 labelled queries
reports, per method, recall@5 (the share of a query's relevant products found in the top 5) and MRR (mean reciprocal
rank: 1 divided by the rank of the first relevant product, averaged over the queries). A separate answer eval reports
whether the model's reply names a relevant product that it was shown.

## Use it when

- Your project has the generative AI shape and you chose "Hybrid retrieval with an eval" (2 points); the extensions
  serve label and range filters and "Model routing, cheap model first".
- Your users search free text: product descriptions, documents, help pages, notes.
- Questions about numbers in a table need SQL and do not need retrieval. See [`dashboard-chat`](../dashboard-chat/README.md).
- L17 covers retrieval: BM25, embedding search, reciprocal rank fusion and OpenSearch. Recall@5 and MRR are this
  recipe's eval; L17 does not define them.

## Main scenario

1. `data/products.jsonl` holds the products. `data/queries.jsonl` holds the labelled queries (query, kind,
   relevant product ids).
2. `main/bm25.py` builds the keyword index over each product's name, description, category and brand.
3. `main/embed.py` turns the same text into vectors: the course LLM gateway's embedding model
   (`qwen3-embedding-0.6b`) on a real run, a hashed stand-in offline. `main/vector.py` searches them.
4. `main/fusion.py` fuses the top 20 of each list with reciprocal rank fusion, k = 60.
5. `main/evaluate.py` scores keyword, embedding and hybrid on every labelled query: recall@5 and reciprocal rank.
6. `main/answer.py` gives the hybrid top 5 to the model, asks for a reply that cites product ids, and scores the
   reply apart from retrieval: correct (cites at least one relevant id) and grounded (cites at least one id, and
   only ids it was shown).
   These two checks read the cited ids only, not the rest of the reply.
7. Production path: `main/opensearch_store.py` puts both indexes into one Amazon OpenSearch Serverless index
   (text fields and a `knn_vector` field), sends one `multi_match` and one `knn` query per search, and fuses them
   with the same `rrf()`.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Two retrievers (R10, R12, R13) | BM25 and embedding search over the same text | BM25 finds exact codes and rare words ("HX-200") and does not find anything for a word the catalogue does not use; embeddings find other words for the same thing and typos, and always return neighbours, even for nonsense | L17; [AI Engineering ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), Retrieval Algorithms, "Comparing retrieval algorithms" (embeddings can obscure keywords such as error codes and product names) |
| Fusion (R14) | Reciprocal rank fusion on ranks, k = 60, over the top 20 of each list | A BM25 score and cosine similarity between vectors are on different scales, but ranks are comparable. 60 is the typical value AI Engineering gives for the method of Cormack, Clarke and Buettcher (SIGIR 2009). 20 is deeper than the 5 the eval scores, so a product that one list ranks 6th to 20th can still be lifted by the other | L17; [AI Engineering ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), Retrieval Algorithms, "Combining retrieval algorithms" |
| Fusion in app code (R14, R43) | `rrf()` runs on the laptop and after the two OpenSearch queries | One function, tested offline, for both paths; the eval compares like with like | Course choice. OpenSearch can also fuse inside the engine with a hybrid query and a search pipeline |
| BM25 parameters (R11) | k1 = 1.2, b = 0.75 | The values Lucene, and so OpenSearch, uses by default. The two paths still split text differently. `main/text.py` drops stop words and a plural `s`, but OpenSearch's standard analyzer does not. OpenSearch also weights the name twice. Compare the recall of each query rather than expecting the same lists. In the runs of 2026-11-14, the keyword lists of the two paths held the same products for 8 of 16 queries, with the same recall for all 16 | Lucene's defaults, [`BM25Similarity.java`](https://github.com/apache/lucene/blob/main/lucene/core/src/java/org/apache/lucene/search/similarities/BM25Similarity.java) (`this(1.2f, 0.75f, ...)`), 2026-11-13 |
| Retrieval eval (R20, R21) | Labelled queries grouped by kind (exact code, other words, typo, plain words); recall@5 and MRR per method and kind; a relevant product below rank 10 counts 0 | One average hides which kind of query a method fails. The kinds show where hybrid helps | [GENPERF01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genperf01-bp01.html) (ground truth data set); [GENPERF04-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genperf04-bp01.html) (test embeddings for relevance); [AI Engineering ch. 6](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html), "Comparing retrieval algorithms" (precision, recall, MRR) |
| Labelled queries checked first (R28) | Every query needs an id, its text, a kind and at least one relevant id that is in the catalogue. IDs are unique. If a query fails validation, the run stops before any model call and names the line | A relevant id with a typo can never be found, so it lowers every method's score without a sign; a query without a kind drops out of the per-kind table | Data Analytics Lens, [best practice 1.1](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-1.1---validate-the-data-quality-of-source-systems-before-transferring-data-for-analytics..html) (validate data quality before it is used: missing values, wrong types) |
| What produced the numbers (R29) | The rows file starts with a `run` record: the data files' SHA-256, the backend, the embedding and answer models, k, the RRF k, the depth of each list, k1 and b | Two eval runs can be compared only if you know what changed between them: the catalogue, a label, a model or a parameter | Data Analytics Lens, [best practice 7.3](https://docs.aws.amazon.com/wellarchitected/latest/analytics-lens/best-practice-7.3---trace-data-lineage..html) (trace where data came from and how it was transformed) |
| Recall gate (R27) | `--min-recall X`: exit 1 when hybrid recall@5, as printed to two decimals, is below X, after the tables and the rows are out; X outside 0 to 1 is refused before the run | Run in CI on the stub, or every day on the gateway, it turns the eval into a check that fails when a change of text, model or parameter makes retrieval worse | [GENOPS01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops01-bp01.html) (periodic evaluation against ground truth) |
| What a run spent (R26) | One line after the tables reports embedding requests and tokens, chat calls and tokens, and the cost in CAD at the course LLM gateway's prices | The key has a budget; a team sees what one eval costs before it runs the eval every day, and sees it grow when a prompt or `k` changes | [GENOPS02-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops02-bp02.html) (monitor the model's metrics, cost among them) |
| Answer eval apart (R22 to R24) | Correct and grounded per reply, plus two counts: retrieval hit but answer wrong, retrieval missed | A wrong answer after a good retrieval is a prompt or model problem; after a miss it is a retrieval problem | [AI Engineering ch. 4](https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch04.html), Design Your Evaluation Pipeline, "Step 1. Evaluate All Components in a System"; [GENOPS01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genops01-bp01.html) (evaluate functional performance) |
| Embedding model (R30) | `qwen3-embedding-0.6b` through the course LLM gateway, 1,024 numbers per text | Embedding requests use the same key and budget as the chat calls. They do not need a provider key | Course constraint; L16 |
| Gateway refusals (R32 to R36, R38, R39) | Texts are embedded in batches of 32 and each text once (`main/embed.py`); a rate-limit refusal (HTTP 429 `rate_limit_exceeded` or `upstream_rate_limited`) is waited out for as long as `Retry-After` says (seconds or an HTTP date, since the course LLM gateway forwards the provider's header), at most 4 calls per request (`main/llm.py` `post`); a spent budget, a bad key, any other refusal, a longer wait than 65 s, an answer that is not a chat completion or an embedding list, or 250 s without a byte of a chat answer (60 s for embeddings) stops the run with one line and exit code 1; a call that timed out is not sent again, as it may have been charged | The eval uses each query's embedding three times (embedding, hybrid, answers). Sending one request per query runs into the key's rate limit. Waiting clears a rate limit and does not clear a budget | [`llm-gateway`](../llm-gateway/README.md), Decisions: retries; [GENREL03-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp01.html) (check that the model responded with the expected content; fail gracefully) |
| Offline embeddings (R60) | Hashed words and three-letter pieces with a small synonym table | The offline embeddings make tests deterministic without a network. Their scores show how the eval works, not how good embeddings are | Course constraint |
| Production store (R40 to R42) | OpenSearch Serverless, one vector search collection in a NextGen collection group, minimum 0 and maximum 2 OCU (OpenSearch Compute Unit: 6 GiB of memory with its vCPU) for indexing and for search | Both indexes in one place. With minimum 0, when no collection in the group has had a request for 10 minutes, indexing and search scale to zero OCU and billing for them stops; the 10 minutes cannot be changed, and the first request after that waits 10–30 s. A Classic group cannot go below 1 OCU. 2 is the smallest maximum above 0 (valid values 0, 2, 4, 8, 16, then multiples of 16). Run clean-up anyway: the collection and its policies stay until you delete them | AWS docs, [Scale to zero](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html), [Collection group capacity limits](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/collection-groups-capacity-limits.html) and [Managing capacity limits](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scaling.html); course constraint (sandbox budget) |
| Standby replicas (R40) | `ENABLED` on the group and the collection | A NextGen group refuses `DISABLED`, so there is nothing to choose | Measured: [`output/main-opensearch-refused.txt`](output/main-opensearch-refused.txt) |
| Access (R41, R48) | Public endpoint with SigV4 (IAM) auth; data access for the identity that runs the script only. Right after the collection is ACTIVE its policies may not apply yet (401 or 403); the index create tries again every 10 s for up to 300 s | A sandbox demo does not need a VPC; production would use a VPC endpoint | [GENSEC01-BP03](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gensec01-bp03.html) (least privilege on data stores); for the VPC endpoint, [GENSEC01-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gensec01-bp02.html) (private network communication) |
| Local vector search (R13) | Exact: every vector compared | 30 products; an approximate index (HNSW) pays off only for large catalogues | Simplest index that fits the data (course default) |

## Requirements

What the recipe must do, one sentence each, with "the recipe" as the subject and one "shall". The sentences follow
EARS, the Easy Approach to Requirements Syntax (Mavin et al., RE 2009). The word in brackets
after a requirement's number names its EARS form, one of the forms in this table.

| Form | How the sentence is built | Example |
|---|---|---|
| ubiquitous | The recipe shall ... (it always holds) | The recipe shall start every resource name with `$PREFIX`. |
| event | When <something happens>, the recipe shall ... | When a question ends, the recipe shall write one trace item. |
| state | While <something is the case>, the recipe shall ... | While a table is being deleted, the recipe shall report it as absent. |
| unwanted | If <something goes wrong>, then the recipe shall ... | If a model call fails, then the recipe shall end the question with status `error`. |
| optional | Where <an extension is used>, the recipe shall ... | Where the answer cache is used, the recipe shall store only answered results. |
| complex | two or more of these together | While a table is being deleted, when a run starts, the recipe shall wait until it is gone. |

Each requirement has one or more scenarios in [`features/`](features/), tagged with its number. `uv run behave`
runs them offline. The requirements of an extension are in its own README, and their numbers go on from here:
[filters and facets](ext-filters/README.md#requirements) from 70,
[model routing](ext-model-routing/README.md#requirements) from 80. When you change what the recipe does, change the
requirement and its scenario with it: `python3 tools/check_requirements.py` (repo root) fails when they do not
match.

**Keyword and embedding searches and their fusion**

- **R10** (ubiquitous) The recipe shall give the keyword index and the embedding index the same text for each
  product: its name, description, category and brand.
- **R11** (ubiquitous) The recipe shall rank keyword results by BM25 with k1 = 1.2 and b = 0.75.
- **R12** (unwanted) If no word of a query is in the catalogue, then the recipe shall return no keyword result.
- **R13** (ubiquitous) The recipe shall return as many embedding results as it is asked for, up to the size of the
  catalogue, also for a query that shares no word with the catalogue.
- **R14** (ubiquitous) The recipe shall fuse the top 20 of the keyword list and the top 20 of the embedding list by
  reciprocal rank fusion, each list adding 1 / (60 + rank) to a product's score.

**Retrieval and answer evals**

- **R20** (event) When `main/run.py` runs, the recipe shall print recall@5 and MRR of keyword, embedding and hybrid
  search over the labelled queries, overall and per query kind.
- **R21** (ubiquitous) The recipe shall count a query's reciprocal rank as 0 when no relevant product is in its top
  10.
- **R22** (event) When the answer eval runs, the recipe shall show the answering model the hybrid top 5 of each
  query, each product with its id in square brackets.
- **R23** (ubiquitous) The recipe shall count a reply correct when it cites at least one relevant product id, and
  grounded when it cites at least one id and every id it cites was shown to the model.
- **R24** (event) When the answer eval ends, the recipe shall print the share of correct and of grounded replies,
  the number of queries whose retrieval found a relevant product and whose reply was not correct, and the number
  whose retrieval found none.
- **R25** (event) When `main/run.py` ends, the recipe shall write the rows of every query and method to
  `eval-<backend>-<embedder>-<answers>.json` in the output folder (`--out`, default `output/`).
- **R26** (event) When a gateway run of `main/run.py` ends, the recipe shall print its embedding requests and their
  tokens, its chat calls and their tokens, and their cost in CAD at the gateway's prices.
- **R27** (unwanted) If `--min-recall` is given and hybrid recall@5, to the two decimals it is printed with, is below
  it, then the recipe shall exit with
  code 1 after it has printed both tables and written the rows.
- **R28** (unwanted) If a labelled query has no id, no query text, no kind or no relevant id, shares its id with
  another, or names a relevant id that is not in the catalogue, then the recipe shall exit with code 1 and name
  the line, before any model call.
- **R29** (event) When `main/run.py` writes the rows, the recipe shall record with them the SHA-256 of the
  products and queries files, the embedding and answer models, k, the RRF k, the depth of each list and the BM25
  parameters.

**Model calls**

- **R30** (ubiquitous) The recipe shall send embedding requests to `$COURSE_GATEWAY_URL/embeddings` for
  `qwen3-embedding-0.6b`, with `KB_MCP_TOKEN` as the bearer key.
- **R31** (ubiquitous) The recipe shall send chat calls to `$COURSE_GATEWAY_URL/chat/completions` for `qwen3.6-35b-a3b`,
  with `KB_MCP_TOKEN` as the bearer key, temperature 0 (none for an OpenAI GPT or o-series model) and `max_tokens` 150.
- **R32** (ubiquitous) The recipe shall send at most 32 texts in one embedding request, and each distinct text
  once in a run.
- **R33** (unwanted) If the gateway refuses a request with HTTP 429 and the code `rate_limit_exceeded` or
  `upstream_rate_limited`, then the recipe shall wait as long as its `Retry-After` header says, in seconds or as an
  HTTP date, and send the request again.
- **R34** (unwanted) If the gateway refuses a request in any other way, refuses it a fourth time, or asks for a wait
  longer than 65 seconds, then the recipe shall stop the run with exit code 1 and the HTTP status and error code.
- **R35** (unwanted) If `COURSE_GATEWAY_URL` or `KB_MCP_TOKEN` is not set for a gateway run, then the recipe shall
  exit with a message that names both, before any request is sent.
- **R36** (unwanted) If the gateway returns fewer embeddings than it was sent texts, then the recipe shall stop the
  run with both numbers.
- **R37** (ubiquitous) The recipe shall keep the course token out of everything it prints and writes.
- **R38** (unwanted) If the gateway answers with status 200 and a body that is not a chat completion or a list of
  embeddings, then the recipe shall stop the run with exit code 1 and one line that says so.
- **R39** (unwanted) If the gateway sends no byte of a chat answer for 250 seconds (of an embedding answer for 60), or the connection to it fails, then
  the recipe shall stop the run with exit code 1 and one line that says so, without sending the request again.

**OpenSearch path**

- **R40** (optional) Where the OpenSearch backend is used, the recipe shall create a NextGen collection group with
  standby replicas `ENABLED` and a minimum of 0 and a maximum of 2 OCU for indexing and for search.
- **R41** (optional) Where the OpenSearch backend is used, the recipe shall let only the identity that ran the script
  create, read, write or delete the index.
- **R42** (optional) Where the OpenSearch backend is used, the recipe shall put every product into one index under
  its product id, with text, keyword, number and boolean fields and a `knn_vector` field of the embedder's
  dimension (1,024 for the gateway's model) with cosine similarity.
- **R43** (optional) Where the OpenSearch backend is used, the recipe shall answer a hybrid search with one
  `multi_match` query and one `knn` query of 20 results each, fused by the same reciprocal rank fusion as on the
  laptop.
- **R44** (complex) Where the OpenSearch backend is used, when a policy, the group or the collection already exists
  from an earlier run, the recipe shall use it.
- **R45** (complex) Where the OpenSearch backend is used, while a collection of the same name is being deleted, the
  recipe shall wait until it is gone before it creates the collection.
- **R46** (complex) Where the OpenSearch backend is used, if creating the collection is refused because its group
  does not exist, then the recipe shall create the group again and try again.
- **R47** (complex) Where the OpenSearch backend is used, if the collection is not ACTIVE within 900 seconds, then
  the recipe shall stop the run with a time-out.
- **R48** (unwanted) If the new collection answers 401 or 403 to the index create, then the recipe shall try
  again every 10 seconds for up to 300 seconds.

**Clean-up**

- **R50** (event) When `clean_up.py --yes` runs, the recipe shall delete the collection, the collection group and the
  data access, network and encryption policies named `$PREFIX-products`, and nothing else.
- **R51** (event) When `clean_up.py` runs without `--yes` (or with `--dry-run`), the recipe shall print what it would delete and delete nothing.
- **R52** (event) When `clean_up.py --yes` runs and a resource is already gone, the recipe shall report it `absent` and
  exit with code 0.
- **R53** (unwanted) If a step of `clean_up.py` is refused with an error other than `ResourceNotFoundException`, then
  the recipe shall report that step `FAILED` with the error code, try the remaining steps and exit with code 1.
- **R54** (complex) While the collection is being deleted by an earlier clean-up, when `clean_up.py --yes` runs, the
  recipe shall report it `absent` and wait until it is gone before it deletes the group.
- **R55** (event) When `clean_up.py` deletes the collection group, the recipe shall wait until the group is gone
  before it ends.

**Course constraints**

- **R60** (ubiquitous) The recipe shall run its stub run without an AWS account, a course token or a network
  connection.
- **R61** (ubiquitous) The recipe shall name every AWS resource it creates `$PREFIX-products`, in the Region
  `AWS_REGION` (default `ca-central-1`).

**Requirements of the extensions**, in the README of each folder:

- 70 to 73, filters and facets: [`ext-filters/README.md`](ext-filters/README.md#requirements)
- 80 to 86, model routing: [`ext-model-routing/README.md`](ext-model-routing/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need a key or an AWS account. The course LLM gateway
runs in the course's account: you only send it requests. This recipe does not run any code inside AWS: it does not have a
Lambda function, a Fargate task or an EC2 instance. On the OpenSearch path your laptop creates a collection in your
sandbox and sends it requests.

| Part | Files | Laptop, offline | Laptop, with AWS and the course LLM gateway | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Retrieval eval and answer eval | `main/run.py`, `main/search.py`, `main/evaluate.py`, `main/answer.py` | yes: local indexes, stub embeddings and stub answers (the default) | yes: `--embedder gateway --answers gateway` | no |
| Embeddings and answers | `main/embed.py`, `main/llm.py` | stand-ins | the course LLM gateway | no |
| Indexes | `main/bm25.py`, `main/vector.py` (laptop), `main/opensearch_store.py` (OpenSearch) | in the Python process | in the process, or an OpenSearch Serverless collection in your sandbox with `--backend opensearch` | no: the collection is a managed service, not your code |
| Extensions | `ext-filters/filters_run.py`, `ext-model-routing/routing_eval.py` | yes | yes, with the options in their READMEs | no |
| Clean-up | `clean_up.py` | explains itself with `--help` | deletes the collection, its group and its policies | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS or the course LLM gateway | no |




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. When a script tags a resource as it creates it, that create call also needs the service's tagging action.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `aoss:APIAccessAll`, `aoss:BatchGetCollection`, `aoss:BatchGetCollectionGroup`, `aoss:CreateAccessPolicy`, `aoss:CreateCollection`, `aoss:CreateCollectionGroup`, `aoss:CreateSecurityPolicy`, `aoss:DeleteAccessPolicy`, `aoss:DeleteCollection`, `aoss:DeleteCollectionGroup`, `aoss:DeleteSecurityPolicy`, `aoss:GetAccessPolicy`, `aoss:GetSecurityPolicy`, `iam:GetRole` |

### 1. Laptop, offline

Run these from the repository root. They do not need a key or an AWS account, so you do not sign in.

```bash
cd hybrid-retrieval-eval
uv sync
uv run pytest -q
uv run behave
uv run python -m main.run
```

What you should see: `pytest` ends with a line like `56 passed`; `behave` ends with `0 failed`; the run prints the
two tables of [What you should see](#what-you-should-see) and writes the rows per query to
`output/eval-local-stub-stub.json` (`--out DIR` writes them elsewhere).

Clean-up and cost: nothing was spent and nothing was created; delete `output/*.json` if you like.

### 2. Laptop, with the course LLM gateway

In `hybrid-retrieval-eval`. This part does not use AWS: there is no AWS sign-in and no `aws sts
get-caller-identity`. The sign-in is your course token.

1. Get the course LLM gateway's base URL from the course page or from your team repo on github.students. Replace
   `YOUR-GATEWAY-BASE-URL` with it and keep the quotes.
2. `read` does not show a prompt: paste your course MCP token, then press Enter. The token does not show.

```bash
export COURSE_GATEWAY_URL='https://YOUR-GATEWAY-BASE-URL/v1'
read -s KB_MCP_TOKEN && export KB_MCP_TOKEN
uv run python -m main.run --embedder gateway --answers gateway
```

The older names `GATEWAY_URL` and `COURSE_MCP_TOKEN` still work when the new ones are not set.

What you should see: the same two tables with the numbers of real embeddings and replies, then one line with what
the run sent and spent: two embedding requests (30 products, then 16 queries), 16 chat calls, their tokens and their
cost in CAD. After a rate-limit refusal, the run prints a wait and retries the request. Any other refusal stops the
run with its HTTP status and error code ([`llm-gateway`](../llm-gateway/README.md) says what each one means).

Run by us on 2026-11-14 ([`output/eval-gateway.txt`](output/eval-gateway.txt)): hybrid led with recall@5 0.94 and
MRR 0.94, against 0.81 and 0.77 for keyword and 0.88 and 0.81 for embedding; 0.94 of the answers were correct. The
run sent 2 embedding requests (838 tokens) and 16 chat calls (3,796 input and 474 output tokens) and cost 0.000246
CAD at the gateway's prices.

Clean-up and cost: well under 0.01 CAD of your key's budget ([Cost and clean-up](#cost-and-clean-up)); nothing to
clean up.

### 3. Laptop, against OpenSearch Serverless in your team sandbox

In the same terminal, with `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` still set.

1. Sign in with AWS credentials for your team sandbox; the first command must show that account.
2. Replace `yourname` with a name of your own before you run the `export` line.

```bash
aws sts get-caller-identity                     # the account you expect, before anything is created
export PREFIX=recipe-yourname AWS_REGION=ca-central-1
uv run python -m main.run --backend opensearch --embedder gateway --answers gateway
uv run python clean_up.py                       # lists what it would delete; deletes nothing
uv run python clean_up.py --yes                 # the same day
```

What you should see: the run waits while the collection is created (it waits up to 15 minutes); if the new
collection refuses at first (401 or 403), it prints one line every 10 s while it tries again, for up to 5 minutes
(R48). Then it prints the
same two tables from the OpenSearch indexes. Compare the recall of each query with part 2, not the lists: the
keyword lists of the two paths differ (Decisions, BM25 parameters).

Run by us on 2026-11-14 in a course sandbox ([`output/main-opensearch.txt`](output/main-opensearch.txt),
[`output/clean-up.txt`](output/clean-up.txt)). The whole run took 79 s, the collection included, and did not print a
waiting line: the new collection did not refuse.
The numbers are those of part 2 except keyword MRR (0.78, not 0.77) and hybrid MRR (0.91, not 0.94): the same
recall per kind of query, and the same recall for every single query. The lists themselves differ more than the
tables show. The embedding lists are the same, all 16. The keyword lists hold the same products for 8 of the 16
queries, and the hybrid lists for 9 ([`output/laptop-vs-opensearch.txt`](output/laptop-vs-opensearch.txt)): the
two paths split text differently (Decisions, BM25 parameters), and that changes which products fill the places
after the relevant ones.
A NextGen collection group refuses `standbyReplicas="DISABLED"`. The first sandbox run on 2026-11-12 was refused
for that reason ([`output/main-opensearch-refused.txt`](output/main-opensearch-refused.txt)); the code now sends
`ENABLED`. If creating the collection or a search fails, run `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` with the same `PREFIX`,
and report from the part 2 run until an OpenSearch run succeeds.

Clean-up and cost: `clean_up.py` as in [Cost and clean-up](#cost-and-clean-up); at most about 0.36 USD for a run of
about 10 minutes plus the 10 idle minutes before the group scales to zero.

## What you should see

Two runs of the same command, side by side. Report the second kind, never the first.

The offline stand-in run in `output/eval-stub.txt` uses the stand-in embedder and the stub answerer, so its numbers
come from stand-ins. The numbers are the same on every run, and the R20 and R24 scenarios fail when one changes. They
show that the eval works, not how well embeddings search.

```
| method | recall@5 | MRR | exact code | other words | plain words | typo |
|---|---:|---:|---:|---:|---:|---:|
| keyword | 0.81 | 0.77 | 1.00 | 0.60 | 1.00 | 0.67 |
| embedding | 1.00 | 0.85 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid | 1.00 | 0.92 | 1.00 | 1.00 | 1.00 | 1.00 |

| correct | grounded | retrieval hit, answer wrong | retrieval missed |
|---:|---:|---:|---:|
| 0.88 | 1.00 | 2 | 0 |
```

**Real run on the course LLM gateway, 2026-11-14.** `output/eval-gateway.txt`, made with `--embedder gateway
--answers gateway`: embeddings from `qwen3-embedding-0.6b`, answers from `qwen3.6-35b-a3b`, the laptop indexes.

```
| method | recall@5 | MRR | exact code | other words | plain words | typo |
|---|---:|---:|---:|---:|---:|---:|
| keyword | 0.81 | 0.77 | 1.00 | 0.60 | 1.00 | 0.67 |
| embedding | 0.88 | 0.81 | 1.00 | 0.80 | 1.00 | 0.67 |
| hybrid | 0.94 | 0.94 | 1.00 | 0.80 | 1.00 | 1.00 |

| correct | grounded | retrieval hit, answer wrong | retrieval missed |
|---:|---:|---:|---:|
| 0.94 | 0.94 | 0 | 1 |
```

The keyword row is the same in both, because no model is in it. The embedding row is where they part: the
stand-in finds every relevant product (recall@5 1.00), the real embeddings 0.88, and with them hybrid is at 0.94,
not 1.00.

Look for these results: keyword search does not return anything for "blutooth speeker" (a typo) and misses "comfy couch"
(the catalogue says "sofa"); in the stub run hybrid's MRR is above each single method's, because a product that one
list ranks low the other ranks high. In your own gateway run, check whether hybrid still leads, kind by kind. In the
gateway run above it led on the typo queries (1.00 against 0.67 for each single method) and tied with embedding on
the others. The stub
answerer cites the first product it is shown, so its `correct` is the share of queries whose top hybrid result is
relevant.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Filters and facets | Label and range filters inside both searches; facet counts by category, brand and price band | `ext-filters/` | Steps 2–4 take an allowed set (laptop) or filter clauses (OpenSearch) |
| Model routing, cheap model first | A small model labels each request simple or hard; app code sends simple ones to the cheaper model; accuracy and cost of three policies | `ext-model-routing/` | Step 6 picks its model per request |

## Cost and clean-up

- Gateway: embedding 46 short texts (30 products and 16 queries) was 838 tokens at 0.01 CAD per million; the 16
  answers were 3,796 input tokens at 0.05 CAD per million and 474 output tokens (at most 150 each) at 0.10 CAD per
  million
. The run
  cost 0.000246 CAD: well under 0.01 CAD.
- OpenSearch Serverless: 0.266 USD per OCU-hour for indexing and the same for search in ca-central-1 (AWS bulk price
  list).
  The group's maximum is 2 OCU each, so the group cannot bill more than 4 OCU, 1.06 USD an hour. A run of about 10
  minutes plus the 10 idle minutes before scale to zero is at most about 0.36 USD (4 OCU for
  20 minutes, an upper bound, not a bill). In the run of 2026-11-14 the main scenario, the filters extension and
  the clean-up were done 6 minutes 38 seconds after the first create, 5 of them the clean-up waiting for the
  collection and the group to be deleted: at most 0.12 USD by the same bound.

On your laptop, use the terminal of the run with the same `PREFIX` and sandbox credentials. The dry run lists what it would
delete. Check the list before you run the second line:

```bash
uv run python clean_up.py
uv run python clean_up.py --yes     # collection, group, access policy, network and encryption policies
```

If an earlier clean-up is still deleting a collection, the script reports it `absent (already being deleted)` and waits until the collection is gone;
the script deletes the group after the collection is gone and waits until the group is gone too, so a run right after a clean-up starts clean.

## Adapt it

1. Replace `data/products.jsonl` with your items. Keep `id`, `name`, `description` and add your own fields.
   Replace `doc_text` in `main/config.py` with the text you want searched.
2. Write at least 20 queries your users would type into `data/queries.jsonl`, each with the ids a person says are
   right and a `kind`. Label them before you look at the results.
3. Run the eval with `--embedder gateway` and report the table per kind. Change one thing at a time (the text
   fields, `POOL`, `RRF_K`, the embedding model) and rerun. Once the numbers are where you want them, add
   `--min-recall` with the value you reached to your CI or a scheduled run, so a change that makes retrieval worse
   fails.
4. For a larger catalogue, read the index mapping in `main/opensearch_store.py` before you use the OpenSearch path.
