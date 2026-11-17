# CPSC 436C recipes

A recipe is a whole task you can clone, run and change: a small but complete **main scenario** that runs end to end,
and **extensions** that each add one capability to it. Each recipe shows one or more practices from the course and
names their source (a lecture, a book chapter, a Well-Architected best practice). This shows you why each step is
built the way it is, as well as how.

Recipes are not assignments and are not graded. Use one when your project needs the piece, copy what you need, and
change the names and the data to your own.

## How a recipe is laid out

| Path | What it holds |
|---|---|
| `README.md` | the recipe, under fixed headings: What you build, Use it when, Main scenario, Decisions, Requirements, Run it, What you should see, Extensions, Cost and clean-up, Adapt it |
| `recipe.toml` | the same recipe for tools and coding assistants: task, shapes, lectures, practices with sources, the reusable files, the check command, the commands, and the constraints to keep |
| `pyproject.toml` | a [uv](https://docs.astral.sh/uv/) project: `uv sync` installs everything |
| `main/` | the main scenario |
| `ext-<name>/` | one extension; its README says what it changes in `main/` and why |
| `tests/` | offline tests: `uv run pytest -q` passes after a clone, with no Amazon Web Services (AWS) account and no key |
| `output/` | a captured run, account number masked as 111122223333 |
| `clean_up.py` or `clean_up.sh` | removes everything the recipe created |

The **Decisions** table in each README has one row per choice: what we chose, why, and the course constraint or best
practice behind it. Comments in the code that start with `Decision (...)` point back to that row.

The project skeleton recipe has one `rep-<name>/` folder for each project shape in place of `main/`
and `ext-*`, and its README compares them in one table.

## Course constraints every recipe keeps

1. **Region and names.** Everything runs in `ca-central-1`. Every resource name starts with `$PREFIX` (default
   `recipe`). Teammates who share an account each set their own: `PREFIX=recipe-ana`.
2. **Budgets.** Team sandboxes have budgets. Each recipe states the cost of one run and where the number comes from
   (a measured run, the AWS price list, or assumed). Nothing runs on a schedule unless the README says so, and the
   clean-up stops it.
3. **Clean-up.** Each recipe removes everything it made. Run its clean-up when you are done.
4. **No keys in code or in the repo.** Model calls go through the course LLM gateway with your course MCP token, read
   from the environment as `KB_MCP_TOKEN`. Provider keys never reach students. The gateway's address is
   `COURSE_GATEWAY_URL`, also from the environment: you find it on the course page, or in your team repo on
   github.students as a repository variable. The older names `COURSE_MCP_TOKEN` and `GATEWAY_URL` still work when
   the new ones are not set.
5. **Offline first.** `uv sync && uv run pytest -q` runs without an account (moto or a local stand-in, a fake model).
   Bash recipes also print every command before it runs, and `DRY=1` prints them without running anything.
6. **IAM roles.** A recipe may create the roles it needs, each allowed only what the recipe does, and its clean-up
   removes them. Where the README names an environment variable for an existing role (`ROLE_ARN`,
   `LAMBDA_ROLE_ARN`), set it to use a role you already have; the recipe then leaves that role alone.

## Recipes by project shape

| Recipe | What you build | Lakehouse first | Operational first | Generative AI first | Lectures |
|---|---|:---:|:---:|:---:|---|
| [project-skeleton](project-skeleton/) | live updates arrive, are stored, reach the lake and show as one number, with representatives of the three project shapes | yes | yes | yes | L3, L8, L10–L17 |
| [dynamodb](dynamodb/) | a DynamoDB store keyed by the app's reads, with conditional writes, transactions and an export to the lake | | yes | | L10–L13 |
| [lambda-dynamodb](lambda-dynamodb/) | an HTTPS API (Lambda function URL) over that store, with a least-privilege role | | yes | | L2–L3, L12–L13, L16 |
| [streaming-ingestion](streaming-ingestion/) | Kinesis → Lambda → DynamoDB with the lag measured | yes | yes | | L14 |
| [concurrency-correctness](concurrency-correctness/) | two clients race on one item, a conditional write accepts one, and a retried write applies once | | yes | | L12–L14, L17 |
| [llm-gateway](llm-gateway/) | a client for the course LLM gateway that handles budget and rate refusals, and a model gateway's source and deploy | | | yes | L15–L17 |
| [llm-probes](llm-probes/) | a table of what an LLM endpoint supports, per model | yes | yes | yes | L15–L17 |
| [dashboard-chat](dashboard-chat/) | ask plain-language questions over a team's table and get answers from read-only SQL through the gateway, with a trace for each question. The same feature is available as a web page (Shiny Express with querychat) | yes | yes | yes | L15–L16 |
| [hybrid-retrieval-eval](hybrid-retrieval-eval/) | keyword search and search by embeddings (numeric representations of meaning), combined by result rank, with an evaluation of the retrieved results | | | yes | L17 |
| [cdc-to-lake](cdc-to-lake/) | every change to a DynamoDB table reaches an Iceberg table that Athena queries, with its lag measured | yes | yes | | L11, L13 |
| [zero-etl-search](zero-etl-search/) | a search index kept in step with a DynamoDB table by a Lambda function on its stream, with a search demo; OpenSearch Ingestion as an extension | | yes | yes | L14, L17 |
| [llm-state-dynamodb](llm-state-dynamodb/) | chat history per signed-in user, an answer cache and traces for an LLM app, in DynamoDB | | yes | yes | L12–L13, L16–L17 |
| [limits-and-alarms](limits-and-alarms/) | a budget, reserved concurrency, and alarms on errors, latency and cost per request | yes | yes | yes | L16–L17 |
| [mcp-server-minimal](mcp-server-minimal/) | a small MCP server with a schema tool and a read-only query tool over your tables | yes | yes | yes | L15, L17 |
| [agent-agentcore](agent-agentcore/) | a Strands agent whose caps, approval and once-only write live in hooks, with its tools behind an AgentCore Gateway, hosted on AgentCore Runtime, scored by an Inspect eval | yes | yes | yes | L17–L18 |
| [model-routing](model-routing/) | a small model labels each request and app code picks the path; the large model gets only the hard ones | | | yes | L16–L17 |
| [workflow-patterns](workflow-patterns/) | prompt chain, parallel calls and evaluator-optimizer, one short file each, with app code deciding every next step | | | yes | L17–L18 |
| [prompt-optimization](prompt-optimization/) | one LLM step's instructions tuned with GEPA, an instruction optimizer, and reported on test questions the optimizer never saw | | | yes | L15, L17 |

## Recipes by project extension

| Project extension | Recipe and folder |
|---|---|
| Streaming ingestion | [streaming-ingestion](streaming-ingestion/) `main/` |
| Windowed aggregates with late records | [streaming-ingestion](streaming-ingestion/) `ext-windowed-aggregates/` |
| Fan-out to several consumers | [streaming-ingestion](streaming-ingestion/) `ext-fan-out/` |
| Concurrency correctness | [concurrency-correctness](concurrency-correctness/) `main/`, `ext-idempotent-retry/`; [dynamodb](dynamodb/) `main/`, `ext-riders-and-drivers/`; [lambda-dynamodb](lambda-dynamodb/) `main/` |
| Operational store to the lake | [dynamodb](dynamodb/) `ext-nightly-report/`; [cdc-to-lake](cdc-to-lake/) `main/` |
| Change data capture | [cdc-to-lake](cdc-to-lake/) `main/` |
| Lag monitoring | [cdc-to-lake](cdc-to-lake/) `ext-lag-alarm/` |
| Table maintenance | [cdc-to-lake](cdc-to-lake/) `ext-maintenance/` |
| Rollback or schema evolution | [cdc-to-lake](cdc-to-lake/) `ext-rollback-or-schema/` |
| Chat on the dashboard | [dashboard-chat](dashboard-chat/) `main/`, `ext-shiny-app/` |
| Answer or retrieval cache | [dashboard-chat](dashboard-chat/) `ext-answer-cache/`; [llm-state-dynamodb](llm-state-dynamodb/) `ext-answer-cache/` |
| Guardrails with a red-team log | [dashboard-chat](dashboard-chat/) `ext-guardrails-redteam/` |
| Trace analysis and alarms | [dashboard-chat](dashboard-chat/) `ext-trace-alarms/`; [llm-state-dynamodb](llm-state-dynamodb/) `ext-traces/`; [limits-and-alarms](limits-and-alarms/) `main/`; [lambda-dynamodb](lambda-dynamodb/) `ext-metrics/` |
| Agent under controlled autonomy | [limits-and-alarms](limits-and-alarms/) `ext-runaway-loop/` |
| Hybrid retrieval with an eval | [hybrid-retrieval-eval](hybrid-retrieval-eval/) `main/`, `ext-filters/`; [zero-etl-search](zero-etl-search/) `main/`, `demo/`, `ext-embeddings/` |
| Model routing, cheap model first | [model-routing](model-routing/) `main/`, `ext-eval/`; [hybrid-retrieval-eval](hybrid-retrieval-eval/) `ext-model-routing/` |
| MCP server | [mcp-server-minimal](mcp-server-minimal/) `main/`, `ext-confirmed-write/` |
| Prompt optimisation, with an eval example (part of the generative-AI-first shape, not an extension on the proposal page) | [prompt-optimization](prompt-optimization/) `main/`, `ext-gepa-direct/`, `ext-eval-matrix/` |
| Every model call (any GenAI extension) | [llm-gateway](llm-gateway/) `main/`; check the endpoint first with [llm-probes](llm-probes/) |
| Your own gateway in front of a provider key | [llm-gateway](llm-gateway/) `ext-deploy/` |

## Before you start

- **Clone.** On your laptop or in CloudShell, run `git clone https://github.com/cpsc436c-2026w1/recipes` and `cd`
  into the recipe folder you need (short link to the repository: ilm.wiki/436c/recipes).
- **Account and region.** Run AWS steps in your course sandbox, in `ca-central-1`. AWS CloudShell, opened in your
  sandbox, has the AWS CLI and Python.
- **uv.** Install [uv](https://docs.astral.sh/uv/). It installs Python and each recipe's packages.
- **Try first, read after.** `uv run pytest -q` and `DRY=1` show what a recipe does before it touches your account.

## Packages

The table lists the packages the recipes use, their purposes and the recipes where they appear. Each recipe folder is a uv project with a `pyproject.toml` that lists its dependencies and a lock file, `uv.lock`. Run the commands in the table inside a recipe folder. The recipes' own READMEs explain each package's use.

| Package or file | What recipes use it for | Recipes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) and [`pyproject.toml`](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) | `uv sync` installs the dependency versions recorded in `uv.lock`. `uv run` runs tests and scenarios with commands such as `uv run pytest -q` and `uv run behave`. | every recipe |
| [boto3](https://docs.aws.amazon.com/boto3/latest/) | AWS calls in the scripts and clean-up; see each recipe's README for details | the recipes with an AWS part, listed under their "AWS permissions" |
| [moto](https://docs.getmoto.org/en/latest/) | a stand-in for AWS in the offline tests | the recipes with an AWS part |
| [HTTPX](https://www.python-httpx.org/) | HTTP calls to the course LLM gateway and to other model endpoints | dashboard-chat, hybrid-retrieval-eval, llm-gateway, llm-probes, model-routing, zero-etl-search |
| [DuckDB](https://duckdb.org/docs/2.0/clients/python/overview.html) | SQL over local files in the offline runs | cdc-to-lake, dashboard-chat, mcp-server-minimal, project-skeleton, prompt-optimization |
| [pytest](https://docs.pytest.org/en/stable/) | offline tests; run `uv run pytest -q` inside a recipe folder | every recipe |
| [behave](https://behave.readthedocs.io/en/latest/) | scenarios that check the requirements in the recipe's Requirements section; run `uv run behave` inside a recipe folder | every recipe with a Requirements section |
| [DSPy](https://dspy.ai/) and [GEPA](https://github.com/gepa-ai/gepa) | prompt optimization; see the recipe's README for each package's role | prompt-optimization |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | a Model Context Protocol (MCP) server and a client that connects to the server | mcp-server-minimal |
| [querychat](https://posit-dev.github.io/querychat/) and [Shiny for Python](https://shiny.posit.co/py/) | a chat web page over a data table; see the recipe's README for each package's role | dashboard-chat (`ext-shiny-app/`) |
| [opensearch-py](https://docs.opensearch.org/latest/clients/python-low-level/) | signed requests to an OpenSearch Serverless collection; see the recipes' READMEs for details | hybrid-retrieval-eval, zero-etl-search |
| [PyIceberg](https://py.iceberg.apache.org/) | rollback of the Iceberg table and changes to its schema | cdc-to-lake |

### Worth looking into

The recipes do not use these packages, and no recipe needs them. They are alternatives to compare when a project grows
past a recipe.

| Package | What it is |
|---|---|
| [Pydantic](https://pydantic.dev/docs/validation/latest/get-started/) and [pydantic-settings](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/) | data validation with Python type hints, and settings read from the environment |
| [Pydantic AI](https://pydantic.dev/docs/ai/overview/) and [Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) | an agent framework and an evaluation library from the Pydantic project |
| [Logfire](https://pydantic.dev/docs/logfire/get-started/) | tracing and logs for Python apps; Logfire is built on OpenTelemetry |
| [Inspect](https://inspect.aisi.org.uk/) | a framework for LLM evaluations |
| [Strands Agents](https://strandsagents.com/) | an open source toolkit for building AI agents in Python and TypeScript |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) | a Python framework for agents that hand work to each other and call tools |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | agents and workflows written as a graph of steps with saved state |
| [Agent Development Kit (ADK)](https://adk.dev/) | Google's framework for building and deploying agents |
| [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) | Claude Code's agent loop and tools in a library |
