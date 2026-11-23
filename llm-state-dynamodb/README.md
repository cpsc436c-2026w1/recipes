# Chat history, answer cache and traces for an LLM app in DynamoDB

## What you build

> ⚠️ Run commands on your local device or in AWS CloudShell. Offline runs use a DynamoDB stand-in; sandbox runs create and use DynamoDB tables in AWS.

Build a chat app that keeps each conversation in DynamoDB, one item per turn, and reads back the newest turns for the user who
owns the conversation. Another user who asks for that conversation is refused, and turns past their expiry date are
not returned, even while DynamoDB still stores them. The recipe stores conversations. It does not sign users in, so
your app verifies the user before it calls `ChatHistory`.

## Use it when

- Your project has the generative AI shape and your app needs memory between messages: the last few turns go into
  the next model call as user messages or replies.
- Your project takes the extension "Answer or retrieval cache" (start from `ext-answer-cache/`) or "Trace analysis
  and alarms" (`ext-traces/` writes the traces; the alarms are in `limits-and-alarms`).
- Review L12 and L13 (keys, conditional writes, transactions), L16 (traces), and L17 (memory, caching).
- For the keys and reads of an ordinary operational table, start with `dynamodb`. For a cache and traces
  built into a text-to-SQL chat, see `dashboard-chat`, which uses the same ideas for its own tables.

## Main scenario

1. `main/tables.py` creates two on-demand tables, `$PREFIX-chat-conversations` and `$PREFIX-chat-history`, and turns
   on time to live (TTL) on the attribute `expires_at`.
2. Your sign-in layer verifies the user and passes their user id to `ChatHistory.start` in `main/history.py`, which
   writes one conversation item with that user as `owner`.
3. Store each turn as one item in `$PREFIX-chat-history`. The partition key is `conversation_id`, and the sort key
   is `at` (UTC time with milliseconds and a short random suffix). The item also stores `role`, `text` and `expires_at`.
4. `ChatHistory.append` writes the turn and checks the owner in one DynamoDB transaction: when the user is not the
   owner, the transaction does not write anything.
5. `ChatHistory.recent` reads the conversation item first and refuses a user who is not its owner.
6. For the owner, a Query on `conversation_id` returns the newest turns first. A filter drops turns whose
   `expires_at` has passed. `recent` asks for more pages until it has enough turns.
7. DynamoDB deletes expired items on its own, some time after they expire.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Unit of memory (R10, R12) | One conversation per user and session, one item per turn | A turn is written once and never changed. A conversation can grow without one item reaching DynamoDB's 400 KB item limit | L17 (memory); Agentic AI Lens [AGENTSEC01-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec01-bp01.html) (memory partitioned per session and per user) |
| Keys (R12, R19) | Partition key `conversation_id`, sort key `at` (time as text, plus a short random suffix) | The access pattern is "the newest turns of one conversation": one Query, `ScanIndexForward=False`; the suffix keeps two turns in the same millisecond apart | L12–L13 (keys from access patterns) |
| Who may read a conversation (R13, R14) | Before every read and write, the app compares the owner on the conversation item with the signed-in user id | Conversation ids can leak through logs, URLs or a shared screen. Check ownership instead of relying on a random id for access control | AGENTSEC01-BP01 (each principal reaches only its own memory; its first anti-pattern is memory shared across sessions by default) |
| What a refusal says (R15) | The same `NotYourConversation`, naming only the conversation id, for a missing, expired or foreign conversation | A different message would tell a caller that someone else's conversation exists | Course choice |
| Owner check on writes (R14, R18) | `TransactWriteItems`: an Update of the conversation with the condition `owner = :u`, and the Put of the turn. Only a failed condition on the conversation is a refusal. A cancellation for any other reason (a conflict with another write) is raised as a failed write | Both happen or neither does. With a separate check and write, ownership can change between them. A transaction cannot hold a ConditionCheck and an Update of the same item, so the Update carries the condition | L12 (all or nothing); [DynamoDB Developer Guide, transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html) (no two actions on one item; the cancellation reasons come in the order of the actions) |
| Where the user id comes from | App code passes the user id from the verified sign-in token (for example the `sub` of a Cognito token) | A user id in the request body is whatever the caller types | AGENTSEC01-BP01; course constraint (no trust in client input) |
| Expiry (R10, R11) | TTL on `expires_at` (seconds since 1970-01-01 UTC, as a Number), 30 days for history; each new turn moves the conversation's expiry forward | DynamoDB deletes expired items itself and does not consume write throughput for it; a sandbox does not pay to store old memory for long. 30 days is a course choice | [DynamoDB Developer Guide, Using time to live (TTL)](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html); course constraint (sandbox budget) |
| Expired items in reads (R16) | Every read filters on `expires_at > now` | DynamoDB deletes expired items "typically within a few days after their expiration", so they can still come back from a Query; the guide says to filter them out | [Using time to live (TTL)](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html) |
| Memory sent to the model (R12) | `recent` returns at most `n` turns and at most 4,000 characters of their text, newest first; when the newest turn alone is longer, only that turn | Every turn in the history is paid again on every later model call; a long conversation grows the cost of each reply. The newest turn is kept even when it alone is longer, so the model never gets an empty history after a long message | Agentic AI Lens [AGENTCOST07-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentcost07-bp01.html) (memory growth guardrails: every token in context is paid on every later call) |
| Short pages (R17) | `recent` keeps paging until it has `n` turns or the conversation ends | `Limit` caps the items read before the filter, so a page of expired turns can come back empty and still have a next page | [DynamoDB Developer Guide, paginating Query results](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html); L12 |
| Capacity mode (R21) | On demand | A course app has a few requests a minute; on demand charges per request and does not charge for idle capacity. Stored data is billed in both modes | Course constraint (sandbox budget) |
| Stronger isolation | Not built: an IAM policy with the `dynamodb:LeadingKeys` condition needs the user id in the partition key | The key is the conversation. App code does the check. If your users reach DynamoDB with their own credentials, put the user id first in the key | [DynamoDB fine-grained access control](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html); AGENTSEC01-BP01 |
| Tests (R32) | moto stands in for DynamoDB; a clock the tests move forward | Tests pass offline, without an account. Expiry is tested without waiting 30 days. moto, like DynamoDB in the days after expiry, keeps an expired item: it never deletes by TTL | Course constraint |

## Requirements

Each requirement states what the recipe must do in one sentence, with "the recipe" as the subject and one "shall". The sentences follow
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
[answer cache](ext-answer-cache/README.md#requirements) from 50, [traces](ext-traces/README.md#requirements) from
60. When you change what the recipe does, change the requirement and its scenario with it:
`python3 tools/check_requirements.py` (repo root) fails when they do not match.

**Chat history** (`ChatHistory` in `main/history.py`)

- **R10** (event) When a signed-in user starts a conversation, the recipe shall store it with that user as its
  owner and an expiry 30 days later.
- **R11** (event) When the owner adds a turn, the recipe shall store the turn and set the expiry of the turn and of
  the conversation to 30 days after that moment.
- **R12** (event) When the owner asks for the newest `n` turns, the recipe shall return, newest first, the newest
  turns that have not expired, at most `n` of them and at most 4,000 characters together, unless the newest turn
  alone is longer, in which case only that turn.
- **R13** (unwanted) If a user who is not the owner asks for a conversation's turns, then the recipe shall refuse
  with `NotYourConversation` and return no turn.
- **R14** (unwanted) If a user who is not the owner adds a turn, then the recipe shall refuse with
  `NotYourConversation` and change nothing in either table.
- **R15** (ubiquitous) The recipe shall give the same refusal, naming only the conversation id, for a conversation
  that does not exist, one that has expired and one that belongs to another user.
- **R16** (state) While a turn has expired and DynamoDB still stores it, the recipe shall leave it out of every read.
- **R17** (event) When a page of a read holds fewer unexpired turns than asked for and the conversation has more,
  the recipe shall read the next page until it has `n` turns or the conversation ends.
- **R18** (unwanted) If DynamoDB cancels the write of a turn for a reason other than the owner check, then the
  recipe shall raise the cancellation as a failed write and not as a refusal.
- **R19** (ubiquitous) The recipe shall keep two turns written in the same millisecond as two turns.

**Run and its tables**

- **R20** (event) When `main/run.py --target local` runs, the recipe shall print `user-ana`'s newest 3 turns,
  newest first, and `refused` for `user-ben`'s read and write on her conversation.
- **R21** (event) When a run with `--target aws` starts and a table of the run is missing, the recipe shall create
  it with on-demand capacity and time to live on `expires_at`.
- **R22** (complex) While a table of the recipe is in the `DELETING` state, when a run starts, the recipe shall wait
  until the table is gone and create it again.
- **R23** (event) When a run starts and its tables exist, the recipe shall use them and print
  `tables created: none (already there)`.

**Course constraints**

- **R30** (ubiquitous) The recipe shall start the name of every table it creates with `$PREFIX` (default `recipe`).
- **R31** (ubiquitous) The recipe shall create its tables in `ca-central-1` unless `AWS_REGION` names another Region.
- **R32** (ubiquitous) The recipe shall run its local runs without an AWS account or a network connection.
- **R33** (ubiquitous) The recipe shall tag every table it creates with `recipe=llm-state-dynamodb`.

**Clean-up**

- **R40** (event) When `clean_up.py --yes` runs, the recipe shall delete the four tables of this `PREFIX` that exist.
- **R41** (event) When `clean_up.py` runs without `--yes` (or with `--dry-run`), the recipe shall print the tables it would delete and delete
  none.
- **R42** (state) While a table is in the `DELETING` state, the recipe shall report it as `absent` in a clean-up and
  delete the other tables.
- **R43** (unwanted) If AWS refuses a step of `clean_up.py`, then the recipe shall exit with code 1 after it has
  tried every other table and printed `FAILED` for the refused one.
- **R44** (event) When `clean_up.py --yes` runs and a table is already gone, the recipe shall report it as `absent` and
  exit with code 0.
- **R45** (ubiquitous) The recipe shall leave in place every table other than its own four, also one whose name
  starts with the name of one of them.

**Requirements of the extensions**, each in the README of its folder:

- 50 to 59, answer cache: [`ext-answer-cache/README.md`](ext-answer-cache/README.md#requirements)
- 60 to 66, traces: [`ext-traces/README.md`](ext-traces/README.md#requirements)

## Run it

### Where each part runs

"Laptop" is your own machine, or AWS CloudShell. "Offline" does not need an AWS account: moto, a library that stands in
for DynamoDB inside the Python process, answers every call. Nothing of this recipe runs inside AWS: it does not have a
Lambda function, a Fargate task or an EC2 instance. Its only AWS resources are DynamoDB tables, which the scripts
on your laptop create and use.

| Part | Files | Laptop, offline | Laptop, against your team sandbox | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| Chat history | `main/history.py`, `main/tables.py`, `main/run.py` | yes, `--target local` | yes, `--target aws`: two tables | no; in your project it runs wherever your app runs |
| Answer cache | `ext-answer-cache/` | yes, `--target local` | yes, `--target aws`: one table | no |
| Traces | `ext-traces/` | yes, `--target local` | yes, `--target aws`: one table | no |
| Model and retriever | stubs in `ext-answer-cache/cache_run.py`, `ext-traces/trace_run.py` | stubs | the same stubs: no model call, no gateway | no |
| Clean-up | `clean_up.py` | no: there is nothing to remove | yes | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI | never against AWS | no |




### AWS permissions

Your sign-in needs these IAM actions to run the scripts and the clean-up. When a script tags a resource as it creates it, that create call also needs the service's tagging action, such as `dynamodb:TagResource`.

| Part | IAM actions |
|---|---|
| Main scenario and clean-up | `dynamodb:CreateTable`, `dynamodb:DeleteTable`, `dynamodb:DescribeTable`, `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:Query`, `dynamodb:UpdateItem`, `dynamodb:UpdateTimeToLive` |

### 1. Laptop, offline: the dry run

Run these from the root of your clone. They do not need an AWS account, so you do not sign in.

```bash
cd llm-state-dynamodb
uv sync
uv run pytest -q
uv run behave
uv run python main/run.py --target local
uv run python ext-answer-cache/cache_run.py --target local
uv run python ext-traces/trace_run.py --target local
```

What you should see:

1. `uv sync` installs the packages.
2. `pytest` ends with a line like `27 passed`.
3. `behave` ends with `0 failed`.
4. The three runs print the lines under [What you should see](#what-you-should-see) and in the two extension
   READMEs. The request id of the traces run changes every run.

Clean-up and cost: the offline run does not create or spend anything.

### 2. Laptop, against your team sandbox

Run on 2026-11-14 in a course sandbox (`output/`): the main run, the two extensions and the clean-up, each with
exit code 0. The main run printed the same lines as the run of 2026-11-12.

1. Sign in to your team sandbox: on your laptop, `aws sso login --profile <your sandbox profile>` and
   `export AWS_PROFILE=<your sandbox profile>` (your profile's name, without the angle brackets); or open AWS
   CloudShell, install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and clone the repository there.
2. From your local device or AWS CloudShell, in `llm-state-dynamodb`, check the account, then set your prefix and run the scenario and the extensions.
   Replace `ana` with your own name first.

```bash
aws sts get-caller-identity                           # stop if this is not your team sandbox's account
export PREFIX=recipe-ana AWS_REGION=ca-central-1      # a prefix nobody else in the account uses
uv run python main/run.py --target aws
uv run python ext-answer-cache/cache_run.py --target aws
uv run python ext-traces/trace_run.py --target aws
```

What you should see: the same lines as offline, after a first line `target aws  prefix recipe-ana  region
ca-central-1`; on a second run with the same prefix the main run says `tables created: none (already there)`. In
the DynamoDB console, four tables whose names start with your prefix.

Clean-up and cost: [Cost and clean-up](#cost-and-clean-up). The three runs make a few dozen requests, far below one
cent; the tables cost nothing while they hold almost no data, and clean-up removes them.

## What you should see

`output/main.txt` (a run on AWS, 2026-11-14; the offline run prints the same lines):

```
tables created: recipe-chat-conversations, recipe-chat-history
user-ana reads her newest 3 turns, newest first:
  assistant One: Victoria.
  user      And last year?
  assistant Two: Kelowna and Nanaimo.
user-ben read on user-ana's conversation: refused
user-ben write on user-ana's conversation: refused
user-ben reads his own: 1 turn
```

What holds on every run: the turns come back newest first, and both lines for `user-ben` on `user-ana`'s
conversation say `refused`. A run in a sandbox prints the same lines with table names using your prefix. On a second run with the same prefix the first
line says the tables are already there.

The scenarios show what a single run cannot: `uv run behave features/history.feature` checks that an expired turn
is still stored and is not returned (R16), that a refused write does not change anything (R14), and that a write cancelled by
a conflict is not mistaken for a refusal (R18).

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Answer cache | Exact cache keyed on the normalized question, the index version and who asks; looked up before retrieval | `ext-answer-cache/` | A table `$PREFIX-answer-cache`; a hit skips retrieval and the model call |
| Traces | One item per model call or tool call of a request, read back by request in call order | `ext-traces/` | A table `$PREFIX-llm-traces`; each call in your request handler runs inside `tracer.call(...)` |

## Cost and clean-up

The main scenario and the two extensions together make a few dozen on-demand requests, far below one cent
. On-demand prices in `ca-central-1`: 0.6875 USD per
million write request units and 0.1375 USD per million read request units; storage is free for the first 25
GB-months, then 0.275 USD per GB-month.
A transaction uses two write units per item, one to prepare it and one to commit it, also when it is cancelled: a
refused write costs as much as an accepted one
([transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)). The recipe
does not schedule anything.

From your local device or AWS CloudShell, in the terminal of the AWS run, with the same `PREFIX`, list the tables clean-up would delete, check that every name
starts with your prefix, then delete them:

```bash
uv run python clean_up.py    # lists the tables it would delete
uv run python clean_up.py --yes              # deletes the tables of this PREFIX: main and both extensions, up to four
```

## Adapt it

1. Pass the user id from your sign-in layer into `ChatHistory` calls. Do not read it from the request body.
2. Set `HISTORY_TTL_DAYS`, `CACHE_TTL_HOURS` and `TRACE_TTL_DAYS` in `main/config.py` to what your users need.
3. Set `HISTORY_MAX_CHARS` in `main/config.py` to the share of your model's context you want history to take.
   To build the next model call, take `recent(user, conversation, n)`, reverse it to oldest first and send each turn
   as a user message or a reply in your request to the course LLM gateway.
4. To list a user's conversations, add a global secondary index on `owner` to `$PREFIX-chat-conversations` in
   `main/tables.py`.
5. Keep `_check_owner`, the transaction in `append` and the `expires_at` filter in `recent` as they are. The tests in
   `tests/test_history.py` fail when one of them is removed.
