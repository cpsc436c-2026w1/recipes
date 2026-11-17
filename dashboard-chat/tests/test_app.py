"""The Shiny app (ext-shiny-app): its data source, the stub gateway, its client, its start, its Fargate scripts.

Offline: the LLM is the stand-in of ext-shiny-app/stub_gateway.py on 127.0.0.1. The page itself is checked in a
browser by features/shiny_app.feature (uv run behave), where Chromium for Playwright is installed.
"""

import json
import os
import stat
import subprocess
import sys
import urllib.request

import pytest

from main.config import ROOT, Settings
from main.sqlcheck import SqlRejected


@pytest.fixture
def source(engine):
    from source import RecipeSource

    return RecipeSource(engine, "orders")


def test_source_answers_through_the_recipes_check_and_engine(source, plain_duckdb):
    frame = source.execute_query("SELECT category, COUNT(*) AS n FROM orders GROUP BY category ORDER BY category;")
    expected = plain_duckdb.execute("SELECT category, COUNT(*) FROM orders GROUP BY category ORDER BY 1").fetchall()
    assert [tuple(r) for r in frame.itertuples(index=False)] == expected
    assert len(source.get_data()) == 1200  # the whole table, not the 50 rows the command-line chat passes on
    assert len(source.test_query("SELECT * FROM orders", require_all_columns=True)) == 1
    assert "amount_cad" in source.get_schema(categorical_threshold=20)


@pytest.mark.parametrize("sql,why", [
    ("DELETE FROM orders", "only one SELECT"),
    ("SELECT 1; DROP TABLE orders", "exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
    ("SELECT * FROM customers", "unknown table"),
])
def test_source_refuses_what_the_sql_check_refuses(source, sql, why):
    for run in (source.execute_query, source.test_query):
        with pytest.raises(SqlRejected, match=why):
            run(sql)
    assert len(source.get_data()) == 1200


def test_a_filter_must_return_every_column(source):
    from querychat._datasource import MissingColumnsError

    with pytest.raises(MissingColumnsError, match="customer_id"):
        source.test_query("SELECT order_id FROM orders", require_all_columns=True)


def test_stub_gateway_speaks_the_chat_format_with_and_without_streaming():
    import stub_gateway

    url, server = stub_gateway.start(script={"How many?": {"query": "SELECT COUNT(*) FROM orders"}})
    try:
        def post(body):
            request = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(request, timeout=10).read().decode()

        tools = [{"type": "function", "function": {"name": "run_sql", "parameters": {
            "type": "object", "properties": {"sql": {"type": "string"}}}}}]
        ask = {"model": "m", "messages": [{"role": "user", "content": "how many"}], "tools": tools}
        call = json.loads(post(ask))["choices"][0]
        assert call["finish_reason"] == "tool_calls"
        assert call["message"]["tool_calls"][0]["function"]["name"] == "run_sql"
        assert json.loads(call["message"]["tool_calls"][0]["function"]["arguments"]) == {
            "sql": "SELECT COUNT(*) FROM orders"}
        stream = post({**ask, "stream": True})
        assert stream.count("data: ") == 4 and stream.rstrip().endswith("data: [DONE]")
        other = json.loads(post({"model": "m", "messages": [{"role": "user", "content": "the weather?"}]}))
        assert other["choices"][0]["message"]["content"] == stub_gateway.DECLINE
    finally:
        server.shutdown()


def test_app_py_gives_querychat_the_recipes_source_and_the_capped_query_tool(monkeypatch):
    """Load ext-shiny-app/app.py itself and look at what it hands to querychat. querychat's Express class needs a
    Shiny session, so a recorder stands in for it; the file runs up to its first page element."""
    import importlib.util

    import querychat.express
    from source import RecipeSource

    from main.engine import Engine

    seen = {}

    class Recorder:
        def __init__(self, data_source, table_name, **kwargs):
            seen.update(data_source=data_source, table_name=table_name, **kwargs)

        def sidebar(self, **kwargs):
            pass

    monkeypatch.setattr(querychat.express, "QueryChat", Recorder)
    monkeypatch.setenv("APP_MODEL", "stub")
    spec = importlib.util.spec_from_file_location("page_app", ROOT / "ext-shiny-app" / "app.py")
    with pytest.raises(RuntimeError, match="page_opts"):  # the first page element, outside a Shiny app
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    source = seen["data_source"]
    assert type(source) is RecipeSource and isinstance(source.engine, Engine)  # the recipe's check and engine
    assert seen["tools"] == ("filter",)  # querychat's own query tool, which sends every row, is not registered
    assert [tool.name for tool in seen["client"].get_tools()] == ["run_sql"]
    with pytest.raises(SqlRejected):
        source.execute_query("DELETE FROM orders")


def test_run_sql_tool_gives_the_llm_at_most_50_rows_and_says_what_it_cut(engine):
    from source import run_sql_tool

    run_sql = run_sql_tool(engine).func
    cut = run_sql("SELECT * FROM orders;").value
    assert len(cut["rows"]) == 50 and cut["truncated"] == "rows"
    assert run_sql("SELECT COUNT(*) AS n FROM orders").value == {"columns": ["n"], "rows": [[1200]], "truncated": None}
    assert len(run_sql("SELECT repeat('x', 500) AS note").value["rows"][0][0]) == 200
    refused = run_sql("DELETE FROM orders")
    assert refused.error is not None and str(refused.value).startswith("SQL check: only one SELECT")
    assert str(run_sql("SELECT nope FROM orders").value).startswith("DuckDB:")


@pytest.mark.parametrize("roles,calls", [
    (["system", "user"], 0),
    (["system", "user", "assistant", "tool"], 1),
    (["system", "user", "assistant", "tool", "assistant", "user", "assistant", "tool"], 1),  # a new question
    (["system", "user"] + ["assistant", "tool"] * 4, 4),
])
def test_model_calls_of_the_question_in_flight_are_counted_from_the_request(roles, calls):
    from client import model_calls_so_far

    assert model_calls_so_far([{"role": r} for r in roles]) == calls


def test_step_limit_answers_itself_at_the_fifth_call_and_survives_a_deep_copy():
    import asyncio
    import copy

    import httpx
    from client import StepLimit

    from main.chat import STEP_LIMIT_REPLY

    sent = []

    async def gateway(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    async def post(transport, n_assistant, stream=False):
        messages = [{"role": "user", "content": "q"}] + [{"role": "assistant"}, {"role": "tool"}] * n_assistant
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.post("https://gw.example/v1/chat/completions",
                                   json={"model": "m", "messages": messages, "stream": stream})

    limit = copy.deepcopy(StepLimit(httpx.MockTransport(gateway)))  # querychat copies the client for each session
    assert asyncio.run(post(limit, 3)).json() == {"ok": True} and len(sent) == 1  # the 4th call goes out
    refused = asyncio.run(post(copy.deepcopy(limit), 4))
    assert refused.json()["choices"][0]["message"]["content"] == STEP_LIMIT_REPLY and len(sent) == 1
    assert STEP_LIMIT_REPLY in asyncio.run(post(copy.deepcopy(limit), 4, stream=True)).text and len(sent) == 1


def test_client_without_the_two_settings_exits_and_names_them(monkeypatch):
    from client import make_client

    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("COURSE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("COURSE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("KB_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="COURSE_GATEWAY_URL and KB_MCP_TOKEN"):
        make_client(Settings(), "gateway")


def test_app_starts_with_the_stub_and_serves_the_page():
    import browser_check

    app, port = browser_check.start_app_on_free_port()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
        assert "Chat on the dashboard" in page
        assert "Environment, read at start" not in page  # the file's opening comment is not on the page
    finally:
        app.terminate()


@pytest.mark.parametrize("script", ["deploy.sh", "start.sh", "stop.sh", "teardown.sh"])
def test_fargate_scripts_dry_run_prints_commands_and_runs_none(tmp_path, script):
    """DRY=1: every command is printed, and aws, docker and curl are never started (stand-ins on PATH record a call)."""
    calls = tmp_path / "calls.log"
    for tool in ("aws", "docker", "curl"):
        fake = tmp_path / tool
        fake.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{calls}"\nexit 99\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "DRY": "1", "PREFIX": "recipe-ana",
           "GATEWAY_URL": "https://gw.example/v1", "COURSE_MCP_TOKEN": "tok-123"}
    env.pop("EXEC_ROLE_ARN", None)
    done = subprocess.run(["bash", str(ROOT / "ext-shiny-app" / "fargate" / script)], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not calls.exists(), calls.read_text()
    out = done.stdout
    assert "tok-123" not in out + done.stderr  # the token is never printed
    assert "DRY=1: nothing is run" in out and "recipe-ana-chat-app" in out
    wanted = {"deploy.sh": ["aws ecr create-repository", "docker build", "aws ecs register-task-definition",
                            "--cidr 203.0.113.10/32", '"valueFrom"'],
              "start.sh": ["aws ecs run-task", "assignPublicIp=ENABLED"],
              "stop.sh": ["aws ecs stop-task"],
              "teardown.sh": ["aws ecs delete-cluster", "aws ecr delete-repository", "aws secretsmanager delete-secret",
                              "aws iam delete-role", "aws ecs delete-task-definitions",
                              "done: everything deploy.sh made"]}[script]
    for text in wanted:
        assert text in out, text
    assert "aws ecs run-task" not in out or script == "start.sh"  # only start.sh starts a task


@pytest.mark.parametrize("course_vpc, default_vpc, public, vpc, subnet", [
    ("vpc-0course", "None", "subnet-0public", "vpc-0course", "subnet-0public"),    # a course sandbox: no default VPC
    ("None", "vpc-0default", "subnet-0public", "vpc-0default", "subnet-0public"),  # another account: its default VPC
    ("vpc-0course", "vpc-0default", "subnet-0public", "vpc-0course", "subnet-0public"),  # both: the course VPC
    ("None", "None", "subnet-0public", "FAILED", ""),                              # neither: the script stops
    ("vpc-0course", "None", "None", "vpc-0course", "FAILED"),                      # a VPC without a public subnet
])
def test_fargate_scripts_find_the_course_vpc_then_the_default_vpc(tmp_path, course_vpc, default_vpc, public, vpc,
                                                                    subnet):
    """Without DRY, against a stand-in for aws: the VPC tagged cpsc436c-vpc, else the default VPC, and a subnet of
    it that gives a public address. Only the two look-ups of fargate/common.sh run; no script of the folder does."""
    fake = tmp_path / "aws"
    fake.write_text(f"""#!/bin/bash
echo "$*" >> "{tmp_path}/calls.log"
case "$*" in
  *describe-vpcs*tag:Name,Values=cpsc436c-vpc*) echo {course_vpc} ;;
  *describe-vpcs*is-default,Values=true*) echo {default_vpc} ;;
  *describe-subnets*map-public-ip-on-launch,Values=true*) echo {public} ;;
  *) echo "unexpected call: $*" >&2; exit 99 ;;
esac
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("DRY", None)
    env.pop("COURSE_VPC_NAME", None)
    script = ('source ext-shiny-app/fargate/common.sh; V=$(vpc_id) || { echo "vpc FAILED"; exit 0; }; echo "vpc $V"; '
              'S=$(public_subnet "$V") || { echo "subnet FAILED"; exit 0; }; echo "subnet $S"')
    done = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[0] == f"vpc {vpc}", (done.stdout, done.stderr)
    if vpc == "FAILED":
        assert "no VPC to run in" in done.stderr and "cpsc436c-vpc" in done.stderr
        return
    assert done.stdout.split("\n")[1] == f"subnet {subnet}", (done.stdout, done.stderr)
    assert f"Name=vpc-id,Values={vpc}" in (tmp_path / "calls.log").read_text()  # the subnet is asked of that VPC
    if subnet == "FAILED":
        assert "no subnet that gives a public address" in done.stderr


def test_dockerfile_copies_every_module_the_app_imports():
    dockerfile = (ROOT / "ext-shiny-app" / "Dockerfile").read_text()
    for module in ("app.py", "chatformat.py", "client.py", "source.py", "stub_gateway.py", "stub_replies.json"):
        assert f"ext-shiny-app/{module}" in dockerfile, module
    assert "COPY main/ main/" in dockerfile and "--no-dev" in dockerfile
    assert sys.version_info >= (3, 12)


def test_deploy_takes_the_new_names_and_kb_mcp_token_wins(tmp_path):
    """deploy.sh DRY=1 with COURSE_GATEWAY_URL and KB_MCP_TOKEN: it passes its checks; with COURSE_MCP_TOKEN set too,
    the secret is still made from KB_MCP_TOKEN (the dry run names no token, so the check is the script's own echo)."""
    env = {k: v for k, v in os.environ.items() if k not in ("GATEWAY_URL", "COURSE_MCP_TOKEN", "EXEC_ROLE_ARN")}
    for tool in ("aws", "docker", "curl"):
        fake = tmp_path / tool
        fake.write_text("#!/bin/bash\nexit 99\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env |= {"PATH": f"{tmp_path}:{os.environ['PATH']}", "DRY": "1", "PREFIX": "recipe-ana",
            "COURSE_GATEWAY_URL": "https://gw.example/v1", "KB_MCP_TOKEN": "tok-new", "COURSE_MCP_TOKEN": "tok-old"}
    script = ROOT / "ext-shiny-app" / "fargate" / "deploy.sh"
    probe = f'source "{script.parent}/common.sh" >/dev/null 2>&1; set +e; ' + \
            "".join(line + "; " for line in script.read_text().splitlines()
                    if line.startswith(("GATEWAY_URL=", "COURSE_MCP_TOKEN="))) + 'echo "$GATEWAY_URL $COURSE_MCP_TOKEN"'
    done = subprocess.run(["bash", "-c", probe], env=env, capture_output=True, text=True, timeout=60)
    assert done.stdout.split()[-2:] == ["https://gw.example/v1", "tok-new"], done.stdout + done.stderr
    done = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "tok-new" not in done.stdout + done.stderr and "tok-old" not in done.stdout + done.stderr
