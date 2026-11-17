"""Steps for the two newest extensions: the Shiny app (ext-shiny-app) and the user's verdict (ext-feedback)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys

import boto3
import duckdb
from behave import given, then, when
from support import GATEWAY_URL, TOKEN, in_moto, patch, run_captured, set_env

from main.config import ROOT, Settings
from main.infra import ensure_table
from main.traces import DynamoTraces

assert GATEWAY_URL


# ---- the Shiny app, without a browser: querychat's chat, the recipe's source, a gateway stand-in ----

def app_chat(context, mode: str, script: dict | None = None):
    """What ext-shiny-app/app.py builds at start, minus the page: the client (client.py), the data source
    (source.py) and querychat's chat with its tools. `filtered` collects what the page would show."""
    import querychat
    import stub_gateway
    from client import make_client
    from source import RecipeSource, run_sql_tool

    if mode == "gateway":  # the app's gateway client, pointed at a stand-in on this machine
        url, context.stub_server = stub_gateway.start("orders", script)
        set_env(context, "GATEWAY_URL", url)
        set_env(context, "COURSE_MCP_TOKEN", TOKEN)
        client = make_client(Settings(), "gateway")
    else:
        started = []
        real_start = stub_gateway.start
        patch(context, stub_gateway, "start", lambda *a, **k: started.append(real_start(*a, **k)) or started[-1])
        client = make_client(Settings(), "stub")
        context.stub_url, context.stub_server = started[0]
    client.register_tool(run_sql_tool(context.engine))  # as app.py does: the recipe's capped query tool
    context.filtered = []
    qc = querychat.QueryChat(RecipeSource(context.engine, "orders"), "orders", client=client, greeting="Ask.",
                             tools=("filter",))
    context.app_client = qc.client(update_dashboard=context.filtered.append)  # querychat's copy of the client
    context.loop = asyncio.new_event_loop()  # the page's calls are asynchronous; one loop for the scenario
    context.add_cleanup(context.loop.close)
    context.add_cleanup(context.stub_server.shutdown)


def stub_requests(context) -> list[dict]:
    return context.stub_server.RequestHandlerClass.requests


@given("the app's chat over a gateway stand-in with the fixed replies of ext-shiny-app/stub_replies.json")
def app_chat_default(context):
    app_chat(context, "gateway")


@given('the app\'s chat over a gateway stand-in that answers "{question}" with the query "{sql}"')
def app_chat_scripted(context, question, sql):
    app_chat(context, "gateway", {question: {"query": sql}})


@when('the app\'s chat is made with APP_MODEL "{mode}"')
def app_chat_mode(context, mode):
    app_chat(context, mode)


@given('the app\'s chat over a gateway stand-in that asks for the tool on every call of "{question}"')
def app_chat_looping(context, question):
    app_chat(context, "gateway", {question: {"always": "SELECT COUNT(*) FROM orders"}})


@given('the app\'s chat over a gateway stand-in that filters "{question}" with "{sql}"')
def app_chat_filter(context, question, sql):
    app_chat(context, "gateway", {question: {"filter": sql, "title": question}})


@when('a user of the app asks "{question}" and the answer comes within {seconds:d} seconds')
def app_user_asks_in_time(context, question, seconds):
    import threading

    worker = threading.Thread(target=app_user_asks, args=(context, question), daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        context.engine.con.interrupt()  # stop the query that nothing else stopped, so the run can end
        worker.join(30)                 # and let the chat finish, so the scenario's event loop can be closed
        raise AssertionError(f"no answer after {seconds} seconds: the query ran without the engine's time limit")


@given('a stand-in for the aws command that answers "{answer}"')
def aws_stand_in(context, answer):
    fake = context.tmp / "aws"
    if answer == "(the thing)":
        fake.write_text('#!/bin/bash\necho \'{"found": true}\'\n')
    else:  # the last line the AWS CLI prints for a refused call, and its exit code
        fake.write_text(f'#!/bin/bash\necho "An error occurred ({answer}) when calling the operation: refused by '
                        f'the stand-in" >&2\nexit 254\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


@when("teardown's look-up helper asks whether a thing is there, without DRY")
def helper_asks(context):
    # Only the helper of fargate/common.sh is run, against the stand-in; no script of the folder is.
    script = ('source ext-shiny-app/fargate/common.sh; FAILED=0; '
              'if present aws iam get-role --role-name x; then echo there; else echo gone; fi; echo "FAILED=$FAILED"')
    env = {**os.environ, "PATH": f"{context.tmp}:{os.environ['PATH']}"}
    env.pop("DRY", None)
    context.helper = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)


@then('the helper\'s answer is "{means}" and the step counts as failed: "{failed}"')
def helper_answer(context, means, failed):
    lines = context.helper.stdout.split()
    assert lines == [means, f"FAILED={'1' if failed == 'yes' else '0'}"], (lines, context.helper.stderr)
    assert ("FAILED look-up" in context.helper.stderr) == (failed == "yes"), context.helper.stderr


@when('a user of the app asks "{question}"')
def app_user_asks(context, question):
    async def ask():  # the asynchronous call a Shiny page makes
        reply = await context.app_client.chat_async(question, echo="none")
        return (await reply.get_content()).strip()

    before = len(stub_requests(context))
    context.app_reply = run_captured(context, context.loop.run_until_complete, ask())
    context.requests_of_question = len(stub_requests(context)) - before


def tool_results(context) -> list:
    """Every tool result in the last request the stand-in received: what the LLM would read."""
    return [m["content"] for m in stub_requests(context)[-1]["body"]["messages"] if m["role"] == "tool"]


@then('the LLM stand-in received a tool result of {n:d} rows that says "{what}" were cut')
def stub_received_cut_rows(context, n, what):
    result = json.loads(tool_results(context)[-1])
    assert len(result["rows"]) == n and result["truncated"] == what, (len(result["rows"]), result["truncated"])


@then('the LLM stand-in received a tool result whose first value has {n:d} characters and that says "{what}" was cut')
def stub_received_cut_text(context, n, what):
    result = json.loads(tool_results(context)[-1])
    assert len(result["rows"][0][0]) == n and result["truncated"] == what, result["truncated"]


@then("no request of the app holds a result of more than {n:d} rows")
def no_request_over(context, n):
    for request in stub_requests(context):
        for message in request["body"]["messages"]:
            if message["role"] == "tool" and message["content"].startswith("{"):
                assert len(json.loads(message["content"])["rows"]) <= n


@then("the gateway stand-in was called {n:d} times for that question")
def stub_called_for_question(context, n):
    assert context.requests_of_question == n, context.requests_of_question


@then('the app\'s reply is "{text}"')
def app_reply_is(context, text):
    assert context.app_reply == text, context.app_reply


@then('the dashboard is filtered by "{sql}" with the title "{title}"')
def dashboard_filtered(context, sql, title):
    [update] = context.filtered
    assert (update["query"], update["title"]) == (sql, title), update


@then('the filtered table has the count of the check query "{sql}"')
def filtered_count(context, sql):
    from source import RecipeSource

    frame = RecipeSource(context.engine, "orders").execute_query(context.filtered[0]["query"])
    assert len(frame) == check_count(context, sql), len(frame)


@then("the dashboard is not filtered")
def dashboard_not_filtered(context):
    assert context.filtered == [], context.filtered


@then('the LLM stand-in received the tool result "{text}"')
def stub_received_tool_result(context, text):
    results = tool_results(context)
    assert results and text in results[-1], results


@then('every request of the app went to "{path}" with the Authorization header "{value}"')
def app_requests_went_to(context, path, value):
    requests = stub_requests(context)
    assert requests and {(r["path"], r["authorization"]) for r in requests} == {(path, value)}, requests[0]


@then('every request of the app has "max_tokens" {tokens:d}, "temperature" {temperature:d} and "model" "{model}"')
def app_request_bodies(context, tokens, temperature, model):
    for r in stub_requests(context):
        body = r["body"]
        assert (body["max_tokens"], body["temperature"], body["model"]) == (tokens, temperature, model), body


@then('the LLM stand-in listens on "{host}"')
def stub_listens_on(context, host):
    assert context.stub_server.server_address[0] == host and context.stub_url.startswith(f"http://{host}:")


@when("the app is started without GATEWAY_URL, COURSE_MCP_TOKEN and APP_MODEL")
def app_started_without_settings(context):
    env = {k: v for k, v in os.environ.items() if k not in ("GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN",
                                                              "APP_MODEL")}
    context.app_run = subprocess.run(
        [sys.executable, "-m", "shiny", "run", "ext-shiny-app/app.py", "--port", "0"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120)


@then("the app stops with an exit code that is not 0")
def app_stopped(context):
    assert context.app_run.returncode != 0, context.app_run.stdout[-300:]


@then('its output holds "{first}" and "{second}"')
def app_output_holds(context, first, second):
    out = context.app_run.stdout + context.app_run.stderr
    assert first in out and second in out, out[-600:]


def check_count(context, sql: str) -> int:
    con = duckdb.connect()
    for table, path in context.paths.items():
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{path}')")
    return con.execute(sql).fetchone()[0]


# ---- the Shiny app, in a browser ----

@given("a browser for the page, or this scenario is skipped")
def browser_or_skip(context):
    import browser_check

    if not browser_check.browser_available():
        context.scenario.skip("no Chromium for Playwright here: uv run playwright install chromium")


@when('the app is opened with the stub LLM and a user asks "{question}"')
def app_in_browser(context, question):
    import browser_check

    context.page = browser_check.check(question, screenshot=context.tmp / "app.png")


@then('the box "{box}" goes from "{before}" to the count of the check query "{sql}"')
def box_goes(context, box, before, sql):
    assert context.page["before"][box] == before, context.page["before"]
    assert context.page["after"][box] == f"{check_count(context, sql):,}", context.page["after"]


@when('the app is opened with the stub LLM, a user asks "{question}" and waits for the reply "{reply}"')
def app_in_browser_no_filter(context, question, reply):
    import browser_check

    context.page = browser_check.check(question, screenshot=context.tmp / "app.png", wait_for_reply=reply)


@then('the chat on the page holds "{text}"')
def chat_on_page_holds(context, text):
    assert text in context.page["chat"], context.page["chat"]


@then('the box "{box}" stays at "{value}"')
def box_stays(context, box, value):
    assert context.page["before"][box] == value == context.page["after"][box], context.page


# ---- the Fargate scripts, with DRY=1: every command is printed, none is run ----

def dry_run(context, script: str) -> str:
    """Run one script of ext-shiny-app/fargate with DRY=1. Stand-ins for aws, docker and curl are first on the
    PATH; if the script started one of them, the scenario fails."""
    calls = context.tmp / "calls.log"
    for tool in ("aws", "docker", "curl"):
        fake = context.tmp / tool
        fake.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{calls}"\nexit 99\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{context.tmp}:{os.environ['PATH']}", "DRY": "1", "PREFIX": "recipe-ana",
           "GATEWAY_URL": GATEWAY_URL, "COURSE_MCP_TOKEN": TOKEN}
    env.pop("EXEC_ROLE_ARN", None)
    done = subprocess.run(["bash", str(ROOT / "ext-shiny-app" / "fargate" / script)], env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not calls.exists(), f"a command was run in a dry run: {calls.read_text()}"
    return done.stdout + done.stderr


def commands(output: str) -> list[str]:
    return [line[2:] for line in output.splitlines() if line.startswith("$ ")]


@when('"{script}" is run with DRY=1 and the course token "{token}"')
def script_dry_run(context, script, token):
    assert token == TOKEN
    context.dry = getattr(context, "dry", {})
    context.dry[script] = dry_run(context, script)


@then('the task definition it would register holds the token only as "secrets[].valueFrom", a reference to the secret')
def task_definition_token(context):
    out = context.dry["deploy.sh"]
    text = out[out.index("the task definition that would be registered:"):]
    td = json.loads(text[text.index("{"):text.index("\n$ ")])
    [container] = td["containerDefinitions"]
    assert container["secrets"] == [{"name": "COURSE_MCP_TOKEN",
                                     "valueFrom": container["secrets"][0]["valueFrom"]}], container["secrets"]
    assert ":secretsmanager:" in container["secrets"][0]["valueFrom"] and "recipe-ana-chat-app/token" in \
        container["secrets"][0]["valueFrom"]
    assert "COURSE_MCP_TOKEN" not in {e["name"] for e in container["environment"]}
    context.task_definition = td


@then('"{token}" is nowhere in what the script printed')
def token_nowhere(context, token):
    for script, out in context.dry.items():
        assert token not in out, f"the token is in the output of {script}"


@then("the security group it would make gets one rule: port {port:d}, from one address")
def one_ingress_rule(context, port):
    rules = [c for c in commands(context.dry["deploy.sh"]) if "authorize-security-group-ingress" in c]
    assert len(rules) == 1, rules
    assert f"--protocol tcp --port {port} " in rules[0] and re.search(r"--cidr \d+\.\d+\.\d+\.\d+/32$", rules[0]), rules[0]
    assert "0.0.0.0/0" not in context.dry["deploy.sh"]


CREATED_BY = {  # what deploy.sh makes -> the command of teardown.sh that removes it
    "aws secretsmanager create-secret": "aws secretsmanager delete-secret",
    "aws iam create-role": "aws iam delete-role ",
    "aws iam attach-role-policy": "aws iam detach-role-policy",
    "aws iam put-role-policy": "aws iam delete-role-policy",
    "aws ecr create-repository": "aws ecr delete-repository",
    "aws logs create-log-group": "aws logs delete-log-group",
    "aws ecs create-cluster": "aws ecs delete-cluster",
    "aws ec2 create-security-group": "aws ec2 delete-security-group",
    "aws ecs register-task-definition": "aws ecs delete-task-definitions",
}


@then('"teardown.sh" has a removing command for every thing "deploy.sh" would make, with the same name')
def teardown_covers_deploy(context):
    made, removed = commands(context.dry["deploy.sh"]), commands(context.dry["teardown.sh"])
    creating = [c for c in made if re.match(r"aws \S+ (create|register|attach|put-role)", c)]
    assert len(creating) == len(CREATED_BY), creating  # a new creating command needs a line in CREATED_BY
    for create, remove in CREATED_BY.items():
        assert any(c.startswith(create) for c in made), create
        matches = [c for c in removed if c.startswith(remove)]
        assert matches, f"teardown.sh has no '{remove}' for '{create}'"
    for name in ("recipe-ana-chat-app/token", "recipe-ana-chat-app-exec", "/ecs/recipe-ana-chat-app",
                 "--repository-name recipe-ana-chat-app", "--cluster recipe-ana-chat-app",
                 ":task-definition/recipe-ana-chat-app:"):
        assert any(name in c for c in removed), name
    assert "aws ecs run-task" not in "\n".join(made + removed)  # neither script starts a task


@then('the card\'s title is "{title}"')
def card_title(context, title):
    assert context.page["title"] == title, context.page["title"]


@then('the page shows the SQL "{sql}"')
def page_shows_sql(context, sql):
    assert context.page["sql"] == sql, context.page["sql"]


# ---- a user's verdict (ext-feedback) ----

def feedback_store(context):
    from feedback import DynamoFeedback, LocalFeedback

    if getattr(context, "traces_table", None):
        return DynamoFeedback(context.traces_table, Settings().region)
    return LocalFeedback(context.trace_path)


def marked_item(context) -> dict:
    return next(i for i in feedback_store(context).items() if i["request_id"] == context.answer.request_id)


@given('traces go to the DynamoDB table "{name}"')
def traces_to_table(context, name):
    in_moto(context)
    ensure_table(name, Settings().region, key="request_id")
    context.traces_table = name
    context.traces = DynamoTraces(name, Settings().region)


@when('the user marks that answer "{verdict}"')
def user_marks(context, verdict):
    feedback_store(context).mark(context.answer.request_id, verdict, context.answer.user_id)


def try_mark(context, request_id, verdict, user):
    from feedback import NoSuchTrace

    try:
        feedback_store(context).mark(request_id, verdict, user)
        context.refused = None
    except NoSuchTrace as e:
        context.refused = e


@when('the user marks the request "{request_id}" "{verdict}"')
def user_marks_request(context, request_id, verdict):
    try_mark(context, request_id, verdict, "c-0007")


@when('customer "{user}" marks that answer "{verdict}"')
def other_user_marks(context, user, verdict):
    try_mark(context, context.answer.request_id, verdict, user)


@when("the trace item of that answer passes its expiry time before time to live removes it")
def item_expires(context):
    if getattr(context, "traces_table", None):
        table = boto3.resource("dynamodb", region_name="ca-central-1").Table(context.traces_table)
        table.update_item(Key={"request_id": context.answer.request_id},
                          UpdateExpression="SET expires_at = :t", ExpressionAttributeValues={":t": 1})
    else:
        items = [json.loads(line) for line in context.trace_path.read_text().splitlines()]
        for item in items:
            item["expires_at"] = 1
        context.trace_path.write_text("".join(json.dumps(i) + "\n" for i in items))


def feedback_cli(context, *args):
    import feedback_run

    patch(context, feedback_run, "OUTPUT_DIR", context.tmp)  # the scenario's traces.jsonl
    run_captured(context, feedback_run.main, list(args))


@when('feedback_run.py is asked to mark that answer "{verdict}"')
def cli_marks_answer(context, verdict):
    feedback_cli(context, "mark", context.answer.request_id, verdict)


@when('feedback_run.py is asked to mark the request "{request_id}" "{verdict}"')
def cli_marks_request(context, request_id, verdict):
    feedback_cli(context, "mark", request_id, verdict)


@when("feedback_run.py prints its report")
def cli_report(context):
    feedback_cli(context, "report")


@then('the trace item of that answer has the verdict "{verdict}" and the time it was given')
def item_has_verdict(context, verdict):
    item = marked_item(context)
    assert item["feedback"] == verdict and item["feedback_at"].endswith("Z"), item


@then("the trace item of that answer has no verdict")
def item_has_no_verdict(context):
    assert "feedback" not in marked_item(context)


@then("the trace item of that answer still has its question, SQL and expiry time")
def item_still_whole(context):
    item = marked_item(context)
    assert item["question"] == context.answer.question and item["sql"] == context.answer.sql
    assert int(item["expires_at"]) > 0 and item["status"] == "answered", item


@then("the verdict is refused because no trace item has that request id")
def verdict_refused(context):
    assert context.refused is not None and "no trace item of this user for request" in str(context.refused)


@then("the verdict is refused with the same words as for a request id that does not exist")
def verdict_refused_same_words(context):
    from feedback import NoSuchTrace

    try:
        feedback_store(context).mark("no-such-request", "up", "c-0012")
    except NoSuchTrace as e:
        unknown = str(e).replace("no-such-request", context.answer.request_id)
    assert context.refused is not None and str(context.refused) == unknown, (str(context.refused), unknown)


@then('the table "{name}" holds {n:d} items')
def table_holds(context, name, n):
    table = boto3.resource("dynamodb", region_name="ca-central-1").Table(name)
    assert table.scan()["Count"] == n


@then('the first printed line is "{text}"')
def first_line_is(context, text):
    assert context.printed.splitlines()[0] == text, context.printed.splitlines()[0]


@then("{n:d} lines are printed")
def n_lines_printed(context, n):
    lines = [line for line in context.printed.splitlines() if line.strip()]
    assert len(lines) == n, lines
    json.loads(lines[-1])
