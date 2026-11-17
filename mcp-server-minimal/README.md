# Model Context Protocol (MCP) server over your team's data: schema and read-only query

## What you build

Build a small MCP server that gives an assistant two tools over your team's tables: `get_schema` lists the tables and
columns, and `query` runs one read-only SELECT and returns at most 50 rows. A client script lists the tools and calls them. It shows that a DELETE is refused; a request without the right
token gets 401.

## Use it when

- Use this recipe if your project has any of the three shapes (lakehouse first, operational first, generative AI
  first) and you chose the extension "MCP server for your system" (1 point, in the group "Infrastructure and
  testing"; the proposal page does not tie it to one project shape), or if an agent in your project needs your
  data as a tool.
- The assistant only needs to read. When the assistant must also change data, start here and add
  `ext-confirmed-write/`, where a person confirms each write.
- Your users ask questions in a chat on your own dashboard, and your app code (not an assistant) decides when to
  query: the tool-calling loop in [`dashboard-chat`](../dashboard-chat/README.md) fits better. Use an MCP server when several clients
  (Claude Code, an IDE agent, your own agent) should use the same tools.
- Lectures: L15 (tool calling: tool call and tool result), L16 (MCP as a tool inventory for any model app), L17
  (MCP host, client and server; bounded agents; a person confirms before a write).

## Main scenario

1. `main/data.py` writes two sample tables, `stores` and `orders`, as Parquet in `data/` (your own tables replace
   them in "Adapt it").
2. `main/engine.py` copies them into an in-memory DuckDB, then switches off file and network access and locks the
   settings.
3. `main/server.py` defines the two tools with the official MCP Python software development kit (SDK) (`MCPServer`) and serves them over
   Streamable HTTP at `http://127.0.0.1:8765/mcp`.
4. `main/auth.py` checks the bearer token before a request reaches the endpoint. A request without `MCP_SERVER_TOKEN` gets 401.
5. A client (`main/client.py`, or an assistant) lists the tools. The model reads their descriptions and returns a
   tool call, for example `query` with one SELECT.
6. `main/engine.py` uses DuckDB's parser to check the statement type. Only one SELECT runs. Anything else comes back as a tool
   result marked as an error, with the reason.
7. The rows, at most 50, reach the model as the tool result; `truncated` says when more rows matched, and
   `text_cut` says when a text value longer than 200 characters was cut. `columns_cut` says when the query had more
   than 100 columns and only the first 100 came back.
8. The server writes one log line per tool call: the tool, the outcome, the time taken, the row count.

On your laptop the server answers only requests addressed to it: with `MCP_HOST` at its default, the SDK refuses a
request whose `Origin` header names another site (403) or whose `Host` header is not this machine (421).

## Decisions

| Decision | What we chose | Why | Constraint or best practice (source) |
|---|---|---|---|
| Tool set (R10) | Two read tools: `get_schema` and `query`. No tool that writes, no file or shell access | The model can do only what a tool lets it do. A smaller set is less to misuse and less to test | L17 (tools expose only what the model needs); Generative AI Lens [GENSEC05-BP01](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/gensec05-bp01.html) (least privilege for agentic workflows) |
| Read and write apart (R10, R80) | Writes live in a separate server (`ext-confirmed-write/`), behind a person's approval | A read can run without asking; a change to data waits for a person | Agentic AI Lens [AGENTSEC04-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec04-bp02.html) (as a baseline, read-only operations proceed on their own and writes need a reviewer's approval); L17 |
| Read-only query, first layer (R20) | DuckDB's own parser (`duckdb.extract_statements`): exactly one statement, of type SELECT | The parser that runs the query decides what it is, so a comment or odd spelling cannot hide a second statement; a column called `deleted` still works | L15 (guarded text-to-SQL); Agentic AI Lens [AGENTSEC02-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec02-bp02.html) (validate tool inputs: the model writes the tool's parameters, here the SQL); Generative AI Lens GENSEC02-BP01 (guardrails on model responses) |
| Read-only query, second layer (R22) | In-memory copies of the tables; external access off; settings locked | A SELECT can call `read_csv('/etc/passwd')`, but it cannot do so with external access off. The Parquet files are not open for writing | Defence in depth; Generative AI Lens GENSEC01-BP03 (least privilege for models that access data stores) |
| Read-only query, third layer (R21) | Every query runs in a read-only transaction that is rolled back (`BEGIN TRANSACTION READ ONLY`) | DuckDB's parser types a statement as SELECT when a common table expression (CTE) in it writes (`WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d`). The first layer passes it, but the read-only transaction refuses the write. The in-memory copies stay as loaded | Defence in depth; Generative AI Lens GENSEC01-BP03 (least privilege for models that access data stores) |
| Row cap (R13, R14) | 50 rows per query (`MAX_ROWS`, course default), with `truncated` in the result | Every returned row becomes input tokens on the model's next call. The instructions ask the model to aggregate in SQL instead | Generative AI Lens GENCOST03-BP01 (prompt token length); AGENTSEC02-BP02 (size limits on tool outputs, each truncation logged); L16 (per-key budget) |
| Long text values (R15, R16) | At most 200 characters of a text value (`MAX_CELL_CHARS`, course default), with `text_cut` in the result and in the log line | The row cap bounds how many rows reach the model, not how long each one is: a table with a long text column would still fill the model's context. A result that was cut without a mark is read as the whole result | AGENTSEC02-BP02 (size limits on tool outputs; a truncation belongs in the log, so that the agent does not reason on a partial result without knowing it); GENCOST03-BP01 |
| Size of a whole result (R17, R18, R19) | At most 20,000 characters of rows as JSON (`MAX_RESULT_CHARS`, course default); rows are left out from the end and `truncated` says so. At most 100 columns (`MAX_COLUMNS`), with `columns_cut`. When the first row alone is over the limit, the result does not contain a row and includes a `note` that says so. `truncated`, `columns_cut` and the `note` are in the log line too | The row cap and the cap per value do not bound a wide table: 50 rows of 40 text columns of 200 characters are 400,000 characters | AGENTSEC02-BP02 (size limits on tool outputs, each truncation logged); GENCOST03-BP01 |
| Memory (R24) | The engine gets `memory_limit` 1 GB, set before the settings are locked | A SELECT can build a result of any size in memory (a join of every row with every row, a large `repeat`); with the limit DuckDB ends that query with an error and the server lives on | AGENTSEC02-BP02 (memory limits keep one tool call from exhausting the tool) |
| Time limit (R23) | 5 seconds per query (`QUERY_TIMEOUT_S`, course default); a query that runs longer is stopped, and the tool result says so | A SELECT the model wrote can join every row with every row. The engine runs one query at a time, so without a limit that one query holds every other tool call until someone stops the server | Generative AI Lens [GENREL03-BP02](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/genrel03-bp02.html) (a timeout on the function an agent calls); AGENTSEC02-BP02 (time limits keep one tool call from exhausting the tool) |
| Tool descriptions (R11, R12) | The docstring is the description the model reads: what the tool does, what it returns, what is refused | The model chooses tools from the tool descriptions only | L15 (tool definitions); [MCP specification, tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) |
| Tool annotations (R10) | `read_only_hint=True` on both tools | A client can show the person which tools do not change data; the hint does not enforce anything, the engine does. The specification tells clients to treat annotations as untrusted unless the server is trusted | [MCP specification, tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) (annotations); L17 |
| Errors (R20, R22) | A refusal is a `ToolError`: a tool result marked as an error, with the reason in plain words | The model can correct its SQL and try again; the server keeps running. In MCP a tool's own failure is a result with `isError` true, not a protocol error | L15 (a rejected query goes back as the tool result); MCP specification, tools (error handling) |
| Token auth (R30, R31, R32) | One bearer token from `MCP_SERVER_TOKEN`, at least 16 characters long. The server compares it by SHA-256 in constant time and never logs it | Anyone who reaches the port can call the tools; the token limits that to people you gave it to | Course constraint (no secrets in code or in the repo); [MCP specification, authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization): authorization is optional, and where it is used the token travels as `Authorization: Bearer` on every request |
| Why a small wrapper for auth | `main/auth.py`, about 20 lines | The SDK's `token_verifier` expects an OAuth authorization server that issues tokens; a team with one token does not run one | Course choice; MCP Python SDK, authorization |
| Transport (R33, R34, R35, R36) | Streamable HTTP, stateless, JSON replies, on `127.0.0.1` | Any MCP client can connect by URL. Stateless means the server does not keep a session between requests, so a restart does not lose any session. A request running at that moment fails. Localhost means only this machine can reach the server. There the SDK also refuses a request whose `Origin` or `Host` header names another site | [MCP specification, transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports): a server that runs locally should bind to 127.0.0.1 only, and must validate the `Origin` header |
| Logging (R40) | One JSON line per tool call: tool, status, milliseconds, rows; for a refused query the kind of statement (`DELETE`, `several_statements`, `does_not_parse`). Not the SQL text, not the token | You see what the assistant did and how long it took. A run of refused `DELETE` or `DROP` lines shows that someone is probing. The SQL can hold a user's words | L16 (observability); Generative AI Lens GENOPS03-BP02 (tracing for agents); Agentic AI Lens [AGENTOPS04-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops04-bp02.html) (MCP for tool calls, with a log of every tool call); AGENTSEC02-BP02 (validation failures are logged) |
| Tests (R60) | A real server on a free localhost port, with the same client code making the calls | A passing test means token check, transport and tools work together | Course constraint (offline tests) |

### How the course MCP server differs

The course MCP server (the one your assistant uses for course search) does the same job at a larger scale. These
are the parts it adds:

| Part | This recipe | Course MCP server |
|---|---|---|
| Library | Official MCP Python SDK, `MCPServer` | FastMCP 3, the standalone `fastmcp` package |
| Tokens | One shared token for this local example, from an environment variable | One token per person, each with a name |
| Logging | One line per tool call to the terminal; the caller is not recorded | One line per tool call, logged under your token's name |
| Same key elsewhere | Not applicable | The same token is the student's key for the course LLM gateway, so a student's tool calls and model calls carry one key id |
| Login | Token only | Token, or a GitHub login checked against a list of allowed accounts |
| Where it runs | This laptop, `127.0.0.1` | A hosted container, behind HTTPS |
| Tools | `get_schema`, `query` over tables | Search and fetch over course documents: `query_kb`, `list_kb_topics`, `search_items`, `get_kb_doc`; all read-only, with result counts and snippet lengths capped |

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
runs them offline, against the recipe's own server on a free port of `127.0.0.1`. The requirements of the
extension are in its own README, and their numbers go on from here:
[a write behind a person's confirmation](ext-confirmed-write/README.md#requirements) from 80. When you change what
the recipe does, change the requirement and its scenario with it: `python3 tools/check_requirements.py` (repo
root) fails when they do not match.

**Tools**

- **R10** (ubiquitous) The recipe shall offer a client exactly two tools, `get_schema` and `query`, each marked
  read-only.
- **R11** (event) When `get_schema` is called, the recipe shall return every table with its row count and the name
  and type of each column.
- **R12** (event) When `query` is called with one SELECT, the recipe shall return the columns, the rows,
  `truncated`, `text_cut` and `columns_cut`.
- **R13** (ubiquitous) The recipe shall return at most 50 rows of a query's result unless `MAX_ROWS` names another
  number.
- **R14** (event) When a query matches more rows than the cap, or rows are left out for the size limit, the recipe
  shall set `truncated` to true in the result.
- **R15** (ubiquitous) The recipe shall return at most 200 characters of a text value in a query's result.
- **R16** (event) When a text value of a result is cut, the recipe shall set `text_cut` to true in the result and
  in the log line of the call.
- **R17** (ubiquitous) The recipe shall return at most 20,000 characters of rows (as JSON) in a query's result,
  leaving out rows from the end.
- **R18** (event) When a query's result has more than 100 columns, the recipe shall return the first 100 and set
  `columns_cut` to true in the result and in the log line of the call.
- **R19** (unwanted) If the first row of a result alone is over the size limit, then the recipe shall return no
  row, set `truncated` to true, and say in a `note`, in the result and in the log line of the call, that the row
  was too large and to select fewer columns.

**Read-only**

- **R20** (unwanted) If the text of a `query` call is anything other than one SELECT statement (a write, a second
  statement, text that does not parse, nothing), then the recipe shall return a tool result marked as an error
  with the reason, and leave the text unrun.
- **R21** (ubiquitous) The recipe shall leave its tables as loaded after every `query` call, also after a SELECT
  that holds a write.
- **R22** (unwanted) If a SELECT that passed the check fails in DuckDB (an unknown column or table, a file read),
  then the recipe shall return a tool result marked as an error and go on answering.
- **R23** (unwanted) If a query runs longer than 5 seconds (`QUERY_TIMEOUT_S` names another limit), then the
  recipe shall stop the query and return the time limit as a tool result marked as an error.
- **R24** (ubiquitous) The recipe shall give the engine a memory limit of 1 GB that no query can change.

**Access**

- **R30** (unwanted) If a request carries no bearer token, or one that is not `MCP_SERVER_TOKEN`, then the recipe
  shall answer 401 before any tool runs.
- **R31** (unwanted) If `MCP_SERVER_TOKEN` is not set or is shorter than 16 characters, then the recipe shall
  refuse to start the server, with a message that names the variable.
- **R32** (ubiquitous) The recipe shall keep the token out of every log line and every printed line.
- **R33** (ubiquitous) The recipe shall listen on `127.0.0.1`, port 8765, unless `MCP_HOST` or `MCP_PORT` names
  another address.
- **R34** (unwanted) If a request to the server on `127.0.0.1` carries an `Origin` or a `Host` header that names
  another site, then the recipe shall refuse it (403 for `Origin`, 421 for `Host`) before any tool runs.
- **R35** (unwanted) If `MCP_HOST` is not this machine and `MCP_ALLOWED_ORIGINS` names no site, or names one
  without `http://` or `https://`, then the recipe shall refuse to start the server, with a message that names
  the variable.
- **R36** (unwanted) If a request to a server that listens beyond this machine carries an `Origin` or a `Host`
  header that `MCP_ALLOWED_ORIGINS` does not name, then the recipe shall refuse it (403 for `Origin`, 421 for
  `Host`).

**Logging**

- **R40** (event) When a tool call ends, the recipe shall write one log line with the tool, the status (`ok`,
  `refused`, `failed` or `timeout`), the milliseconds and, for a refused query, the kind of statement, and without
  the SQL text.

**Client**

- **R50** (event) When `main/client.py` runs, the recipe shall print one JSON line per step: the tools, the tables
  of the schema, and the result or the refusal of each query.
- **R51** (unwanted) If the server answers 401 to `main/client.py`, then the recipe shall exit with code 1 and one
  line that says the token is not the server's.
- **R52** (unwanted) If `main/client.py` cannot reach the server, then the recipe shall exit with code 1 and one
  line that says so.
- **R53** (unwanted) If `MCP_SERVER_TOKEN` is not set for `main/client.py`, then the recipe shall exit with a
  message that names the variable, before any request is sent.

**Course constraints**

- **R60** (ubiquitous) The recipe shall run without an AWS account, a model key or a connection to any address
  other than `127.0.0.1`.

**Clean-up**

- **R70** (event) When `clean_up.py` runs, the recipe shall delete the Parquet files in `data/` and the refunds
  database in `output/`, and leave the captured runs there.
- **R71** (event) When `clean_up.py --dry-run` runs, the recipe shall print the names it would delete and delete
  nothing.
- **R72** (event) When `clean_up.py` runs and none of these files is left, the recipe shall exit with code 0.

**Requirements of the extension**, in the README of its folder:

- 80 to 91, a write behind a person's confirmation:
  [`ext-confirmed-write/README.md`](ext-confirmed-write/README.md#requirements)

## Run it

### Where each part runs

> ⚠️ **Where commands run:** Your laptop is your own machine. Everything in this recipe runs there: the server,
> the data and the client. It does not call a model or a gateway, and it does not create anything in AWS:
> no Lambda function, no Fargate task, no EC2 instance.

| Part | Files | Laptop, offline | Laptop, with the course LLM gateway and your team sandbox | In AWS (Lambda, Fargate or EC2) |
|---|---|---|---|---|
| MCP server | `main/server.py`, `main/auth.py` | yes, on `127.0.0.1:8765` | not used: the server does not call a model or an AWS service | not in this recipe; "Adapt it", step 5, says what a container behind HTTPS needs |
| Read-only engine | `main/engine.py` | inside the server's own Python process: it is not a service | the same | the same, inside the server's process |
| Tables | `main/data.py` writes `data/*.parquet` | on your disk | the same | not in this recipe |
| Client | `main/client.py` | yes, in a second terminal | the same | no |
| Assistant as the client | Claude Code, an IDE agent | on your laptop; the assistant's own model calls go to its provider, not through this recipe | the same | no |
| Write server and the person's approval | `ext-confirmed-write/write_server.py`, `approve.py`, `walk.py` | yes; `approve.py` on the approver's own machine | the same | no |
| Clean-up | `clean_up.py` | yes: it deletes local files only | the same | no |
| Tests and scenarios | `tests/`, `features/` | yes, and in CI; they start the server on a free port of `127.0.0.1` | never against AWS | no |

### 1. Laptop, offline: tests and scenarios

Run these from the repository root. They do not need a key or an AWS account, so you do not sign in.

```bash
cd mcp-server-minimal
uv sync
uv run pytest -q
uv run behave
```

What you should see: `uv sync` installs the packages; `pytest` ends with a line like `43 passed`; `behave` ends
with `0 failed`. Both start the recipe's server on a free port of `127.0.0.1` and stop it again. Nothing was
spent, and nothing is left to clean up: they write only to temporary folders.

### 2. Laptop: the server and the client, in two terminals

This part does not use AWS: there is no AWS sign-in and no `aws sts get-caller-identity`. The sign-in is the
token you make in step 1.

1. On your laptop, in the first terminal, in `mcp-server-minimal`, make a token and start the server. It prints
   `MCP server on http://127.0.0.1:8765/mcp`; leave it running:

   ```bash
   export MCP_SERVER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
   echo "$MCP_SERVER_TOKEN"          # copy it for the second terminal
   uv run python -m main.server
   ```

2. Open a second terminal in the same folder (`mcp-server-minimal`). `read` does not show a prompt: paste the token
   from the first terminal, then press Enter. Then run the client; the last command uses a wrong token on purpose:

   ```bash
   read -s MCP_SERVER_TOKEN && export MCP_SERVER_TOKEN    # the token does not show
   uv run python -m main.client
   uv run python -m main.client --sql "SELECT city, count(*) FROM stores GROUP BY city"
   MCP_SERVER_TOKEN=not-the-server-token uv run python -m main.client
   ```

What you should see: four JSON lines from the first client run (the tools, the tables, five cities, the refused
DELETE), three from the second, and `401 Unauthorized ...` from the third; in the server terminal one log line
per tool call ([What you should see](#what-you-should-see)).

Clean-up and cost: stop the server with Ctrl-C. The run cost nothing. It wrote `data/*.parquet`;
`uv run python clean_up.py` deletes them.

### 3. Laptop: an assistant as the client

**Not run by us yet (2026-11-13):** the recipe has no captured run of an assistant that uses the server.

No AWS sign-in here either. With the server of part 2 running, in the second terminal, add the server to your
assistant by URL with the token as a header. In Claude Code, for example:

```bash
claude mcp add --transport http team-data http://127.0.0.1:8765/mcp --header "Authorization: Bearer $MCP_SERVER_TOKEN"
claude mcp list                     # shows team-data
```

What you should see: `claude mcp list` shows `team-data`; in a chat, the assistant can call `get_schema` and
`query`, and each call is one log line in the server terminal.

`claude mcp add --header` writes the header, and so the token, in plain text into the assistant's configuration
file on your machine. Use a token made for this server only, and remove the entry when you
are done.

Clean-up and cost: `claude mcp remove team-data` takes the server, and the stored token, out of the assistant again. The server costs
nothing. The assistant's own model calls are charged by its provider, as always.

## What you should see

From `output/main-local.txt` (a run on a laptop, 2026-11-12):

```
{"step": "list_tools", "tools": [{"name": "get_schema", "read_only": true}, {"name": "query", "read_only": true}]}
{"step": "get_schema", "is_error": false, "tables": ["orders", "stores"]}
{"step": "query", "sql": "SELECT s.city, count(*) AS orders, ...", "is_error": false, "result": {"columns": ["city", "orders", "sales_cad"], "rows": [["Vancouver", 66, 6484.19], ...], "truncated": false}}
{"step": "query", "sql": "DELETE FROM orders", "is_error": true, "result": "Error executing tool query: query refused: only SELECT is allowed; this is DELETE"}
```

And in the server terminal, one line per tool call:

```
mcp-server-minimal {"tool": "query", "status": "ok", "ms": 33, "rows": 5, "truncated": false}
mcp-server-minimal {"tool": "query", "status": "refused", "ms": 0}
```

A refused line now also carries `"reason": "DELETE"`, and an `ok` line `"text_cut": false` and `"columns_cut": false`;
the capture predates them.

Every run has two tools, both read-only. The sample data gives the same rows every time. The DELETE is
refused. A result also carries `text_cut`. The scenarios check that `orders` still has 200 rows after the DELETE.
The client run with the wrong token prints
`401 Unauthorized from http://127.0.0.1:8765/mcp: MCP_SERVER_TOKEN here is not the server's token`. The times in
`ms` change from run to run.

## Extensions

| Extension | What it adds | Folder | What changes in the main scenario |
|---|---|---|---|
| Write behind a person's confirmation | One write tool, `record_refund`, that changes data only with a confirmation code a person gives; one row per request id however often it is called | `ext-confirmed-write/` | A third tool beside the two read tools; a new DuckDB file `output/refunds.duckdb`; a second secret, `APPROVAL_SECRET` |

## Cost and clean-up

One run costs 0 CAD: the server, the data and the client run on your laptop, and the recipe does not call a model
or create anything in AWS. On your laptop, in `mcp-server-minimal`, list the files a run created, check the
list, then delete them:

```bash
uv run python clean_up.py --dry-run    # what would go
uv run python clean_up.py              # deletes data/*.parquet and output/refunds.duckdb
```

## Adapt it

1. Replace `TABLES` in `main/data.py` with your tables, or point `DATA_DIR` at a folder of your Parquet files and
   list their names. Run the tests and the client again: `get_schema` now lists your tables.
2. Rewrite the `instructions` in `main/server.py` and the two docstrings for your data: what a row is, which
   questions the tables answer. The server says what each tool does; your repo's AGENTS.md (or your assistant's
   project file) says when to use it, for example "answer questions about the team's tables with the team-data
   tools, never from memory".
3. Keep `main/engine.py` and `main/auth.py` as they are. Lower `MAX_ROWS` if your rows are wide, and set
   `QUERY_TIMEOUT_S` from your slowest real query.
4. Add a read tool only when the model needs something `query` cannot give. Give it `READ_ONLY` annotations and a
   test. A tool that fetches one item by id or path should raise `ToolError("no item <id>; list or search first")`
   for an id it does not know, not return an empty result: an empty text reads to the model as "the item is
   empty", while a `ToolError` reaches the client as a result with `isError` true, which the model can recover
   from. Test it with a made-up id.
5. To serve teammates from your team sandbox, put HTTPS in front of the server's container (the L16 Fargate
   dashboard is a starting point; this recipe does not include those steps). Set `MCP_HOST=0.0.0.0` to make
   the server listen on every network interface of the container. Run the server in the container. By
   itself the SDK checks the `Origin` and `Host` headers only for a localhost address, and the MCP specification
   requires a server to validate `Origin`. So set `MCP_ALLOWED_ORIGINS` to the site your teammates use, for example
   `https://team.example.org`: the server gives it to the SDK (`TransportSecuritySettings`), answers only requests
   for that site, and does not start without it
. Keep
   the token in Secrets Manager, not in the task definition. For one token per person, keep a name and a SHA-256 hash of each token (never the token itself)
   and write the name in every log line, as the course server does.
