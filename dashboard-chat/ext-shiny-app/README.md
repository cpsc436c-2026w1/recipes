# Chat as a web page: Shiny Express with querychat

This extension serves "Chat on the dashboard" for every project shape: a page your users open in a browser, over a table your
project already has. Builds on `main/`; read the recipe README first. Structure adapted from the CPSC 436C L15
querychat demo, itself adapted from DSCI 532.

## What it changes in the main scenario

A user asks a question on a web page instead of the command line. The page shows a table, and the chat shows the tool result:

1. `app.py` is a [Shiny Express](https://shiny.posit.co/py/docs/express-vs-core.html) app. It loads the tables
   into the recipe's engine (`main/engine.py`), as step 2 of the main scenario does.
2. [querychat](https://posit-dev.github.io/querychat/py/) puts a chat in the sidebar and sends each question to the
   LLM with the table's schema. The LLM answers with a tool call that holds SQL: querychat's filter for the table on
   the page, or `run_sql`, the recipe's own query tool, whose result is shown in the chat.
3. `source.py` is where that SQL runs. Both tools call `main/sqlcheck.py` (step 4 of the main scenario) and then
   the recipe's engine (step 5). A filter gives the page every row of its result. `run_sql` gives the LLM at most
   50 rows, with text values cut at 200 characters, as on the command line.
4. `client.py` makes the LLM client: the course LLM gateway with `COURSE_GATEWAY_URL` and `KB_MCP_TOKEN` (step 3), or,
   with `APP_MODEL=stub`, `stub_gateway.py`, a stand-in on your own machine that does not call a model. It also keeps
   the limit of 4 model calls per question, which on the command line is in `main/chat.py`.

What the page does not do: it does not write trace items, and it does not use the answer cache or the guardrails.
Every row of the table is shown to everyone who can open the page.

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Page (R120) | Shiny Express and querychat, in about 70 lines | The dashboard is secondary technology in this course: use a library that gives a chat, a filtered table and the tool calls, and spend your time on the data and the checks | L15 (the querychat demo); querychat's documentation |
| Who runs the LLM's SQL (R121) | The recipe's own check and engine, through `source.py`; not querychat's built-in source. A test loads `app.py` and fails if it hands querychat anything else | querychat's DuckDB source refuses a query by its first keyword. The recipe's check parses the query, and its engine refuses a write by itself; one set of rules for the command line and the page | L15 (guarded text-to-SQL); Agentic AI Lens AGENTSEC02-BP02 (validate tool inputs) |
| What the LLM reads of a result (R125) | The recipe's `run_sql` as the page's query tool, in place of querychat's own: at most 50 rows, 200 characters per text value, `truncated` says what was cut | querychat's query tool gives the LLM every row of a result, and the conversation carries them into every later call: `SELECT * FROM orders` would send 1,200 rows again and again. The table on the page still gets every row of a filter; those rows do not go to the LLM | GENCOST03-BP01 (prompt length); Agentic AI Lens AGENTSEC02-BP02 (size limits on tool outputs) |
| Model calls per question (R126) | At most 4, kept in the client: a transport that reads the count from the request and, at 4, answers itself with the step-limit reply | In the page querychat runs the loop of model calls, so the limit of `main/chat.py` is not in force. The request holds the whole conversation, so the count does not need memory. The limit applies to every browser session and every copy querychat makes of the client | GENCOST05-BP01 (stopping conditions) |
| Model calls (R122, R123) | The course LLM gateway through chatlas's client for the OpenAI chat format, token from the environment | The same key, budget and rate limit as the rest of your project; nothing secret in the image or the repo | Course constraint; L16 (model gateway) |
| Offline runs (R124) | `APP_MODEL=stub`: a small HTTP server on 127.0.0.1 that answers in the gateway's format from `stub_replies.json` | The page, querychat and the client run exactly as with the gateway, with no key and no account; a test can ask a question and read the table | Offline tests after a clone (course constraint) |
| Who can open the page on AWS (R128) | The security group lets one address in: yours, at the time of `deploy.sh` | The page does not require sign-in. Anyone who reaches it can ask questions that spend your course key's budget | GENSEC01-BP01 (least privilege access to model endpoints); course choice |
| Where the token is on AWS (R127) | ECS reads the token from a Secrets Manager secret when the task starts. The token is not in the image or the task definition | A task definition can be read by everyone in the account | [Secrets in ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html) |

## Requirements

The form in brackets and the scenarios are explained in the recipe README, [Requirements](../README.md#requirements).
The scenarios are in `features/shiny_app.feature`.

- **R120** (complex) Where `ext-shiny-app` is used, when a user asks a question that the LLM answers with a filter,
  the recipe shall show the rows of that query in the table on the page and their number in the box "Orders shown".
- **R121** (complex) Where `ext-shiny-app` is used, if the LLM's SQL is anything other than one SELECT over the
  team's tables, then the recipe shall give the LLM the rejection as the tool result and leave the table on the
  page as it was.
- **R122** (optional) Where `ext-shiny-app` is used, the recipe shall send every model call of the page to
  `$COURSE_GATEWAY_URL/chat/completions` with `KB_MCP_TOKEN` as the bearer key, `temperature` 0 (none for an OpenAI GPT or o-series model) and the `max_tokens` of
  the command line (R31, R35: 400, or 600 for `qwen3.8-27b`).
- **R123** (complex) Where `ext-shiny-app` is used, if `COURSE_GATEWAY_URL` or `KB_MCP_TOKEN` is not set and
  `APP_MODEL` is not `stub`, then the recipe shall exit with a message that names both, before the page is served.
- **R124** (optional) Where `ext-shiny-app` is used with `APP_MODEL=stub`, the recipe shall answer from a stand-in
  on `127.0.0.1`, without a course token.
- **R125** (optional) Where `ext-shiny-app` is used, the recipe shall pass at most 50 rows of a query's result, with
  at most 200 characters per text value, to the LLM, and say in `truncated` what it cut.
- **R126** (complex) Where `ext-shiny-app` is used, if the LLM asks for a tool in the 4th model call of a question,
  then the recipe shall answer "I could not answer within the step limit." in place of a 5th model call.
- **R127** (complex) Where `ext-shiny-app` is used, when `fargate/deploy.sh` registers the task definition, the
  recipe shall put the course token in it only as a reference to the secret (`secrets[].valueFrom`).
- **R128** (complex) Where `ext-shiny-app` is used, when `fargate/deploy.sh` makes the security group, the recipe
  shall give it one rule: port 8000, from one address (`/32`).
- **R129** (complex) Where `ext-shiny-app` is used, when `fargate/teardown.sh` runs, the recipe shall remove every
  resource that `fargate/deploy.sh` made, and count a look-up that AWS refuses as a failed step, not as a resource
  that is gone.

The scenarios of the last three run the scripts with `DRY=1`: they read the commands the scripts print. One more
runs only the look-up helper of `fargate/common.sh` against a stand-in for the `aws` command. No scenario runs a
script against AWS. The main scenario's time limit of one query (5 seconds) holds for the page's filters too. A
scenario in `features/shiny_app.feature` checks it.

## Run it

On your laptop in `dashboard-chat`, start the page and run the browser check with the commands below. The recipe
README's [Run it](../README.md#run-it) covers running on your laptop offline, on your laptop with the gateway, on
Fargate and on EC2:

```bash
APP_MODEL=stub uv run shiny run ext-shiny-app/app.py --port 8000    # offline; open http://127.0.0.1:8000
uv run python ext-shiny-app/browser_check.py --screenshot app.png    # asks one question in a headless browser
```

## What you should see

The page shows the 1,200 orders and their amount. Type "Show the cake orders" in the sidebar: the first box goes to
285, the card's title to "Cake orders", and the SQL under the table to
`SELECT * FROM orders WHERE category = 'cake'`. `browser_check.py` prints the same:

```
"before": {"Orders shown": "1,200", "Their amount": "27,594.01 CAD"},
"after": {"Orders shown": "285", "Their amount": "6,820.99 CAD"},
"title": "Cake orders",
"sql": "SELECT * FROM orders WHERE category = 'cake'"
```

Type "Delete all the orders": the chat shows "Query the tables: failed", with the reason
`SQL check: only one SELECT is allowed; this is a DELETE statement` when you unfold it, and the table stays as it
was. With the stub, only the five questions of `stub_replies.json` get a query; any other gets "I can only answer
questions about the shop's sales." With the gateway the LLM writes the SQL, so the wording and the queries change
from run to run. The page was run on EC2 with the course LLM gateway on 2026-11-14 (`../output/shiny-ec2.txt`); it has not been run on Fargate by us yet.

A reply cut at the token cap shows on the page as an empty message. chatlas does not show an error for it in a chat,
only a Python warning in the page's log (chatlas 0.23.0, `_chat.py`, `check_finish_reason(..., "warn")`). The
page does not say "the model used its token cap before answering" as the command line does (R36).

## Adapt it

1. Put your Parquet files in `data/` and list them in `TABLES` (`main/data.py`). Set `APP_TABLE` to the table the
   page shows. Change the two boxes in `app.py` to numbers that mean something for your data.
2. Add your stub questions to `stub_replies.json`, with a filter (`SELECT *` with a WHERE) or a query for each question.
3. One table per page: querychat filters one table. To let the LLM join another table of yours, describe it in the
   greeting or in querychat's `data_description`. The SQL check already allows every table in `TABLES`.
4. Rows per user: this page shows every row to everyone who can open it. To restrict users to rows they may see, build the
   engine per signed-in user with `Engine.for_rows_of` (see `ext-guardrails-redteam`).
