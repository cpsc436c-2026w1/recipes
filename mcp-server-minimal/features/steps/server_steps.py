"""Steps for the main server, its client and clean_up.py."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket

import httpx2
import uvicorn
from behave import given, then, when
from support import TOKEN, call_tool, free_port, list_tools, patch, run_captured, serve

import clean_up
import main.client as client_module
import main.server as server_module
from main.config import ROOT, Settings
from main.engine import ReadOnlyEngine

RUN_FILES = ("data/orders.parquet", "data/stores.parquet", "output/refunds.duckdb", "output/refunds.duckdb.wal")


# ---- given: a running server ----

@given("the main server is running with the sample tables")
def main_server(context):
    context.engine = ReadOnlyEngine(context.tables, max_rows=Settings().max_rows,
                                    query_timeout_s=Settings().query_timeout_s)     # as main/server.py builds it
    settings = Settings(token=TOKEN, data_dir=context.data_dir)
    context.url = serve(context, server_module.build_app(settings, server_module.build_server(context.engine)))


@given('QUERY_TIMEOUT_S is "{seconds}" and the main server is started from the environment')
def main_server_with_time_limit(context, seconds):
    os.environ["QUERY_TIMEOUT_S"] = seconds
    for server, thread in context.servers:
        server.should_exit = True
        thread.join(timeout=5)
    context.servers.clear()
    context.engine.close()
    main_server(context)


@then("the time limit of one query is {seconds:g} seconds")
def time_limit_is(context, seconds):
    assert context.engine.query_timeout_s == seconds, context.engine.query_timeout_s


@given('MAX_ROWS is "{n}" and the main server is started from the environment')
def main_server_from_env(context, n):
    os.environ["MAX_ROWS"] = n
    for server, thread in context.servers:            # the Background's server: stop it, this one replaces it
        server.should_exit = True
        thread.join(timeout=5)
    context.servers.clear()
    context.engine.close()
    main_server(context)


@given("no server is running at the client's address")
def no_server(context):
    os.environ["MCP_PORT"] = str(free_port())          # a port nothing listens on
    context.url = Settings().url


@given("every connection to an address other than 127.0.0.1 fails the scenario")
def only_localhost(context):
    built_connect = socket.socket.connect
    context.connections = []

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host == "127.0.0.1", f"the run opened a connection to {address}"
        context.connections.append(address)
        return built_connect(self, address)

    patch(context, socket.socket, "connect", connect)


@given("MCP_SERVER_TOKEN is not set")
def token_not_set(context):
    os.environ.pop("MCP_SERVER_TOKEN", None)


@given('MCP_SERVER_TOKEN is "{token}"')
def token_is(context, token):
    os.environ["MCP_SERVER_TOKEN"] = token


@given('MCP_HOST is "{host}" and MCP_PORT is "{port}"')
def host_and_port(context, host, port):
    os.environ["MCP_HOST"], os.environ["MCP_PORT"] = host, port


# ---- when: tool calls ----

@when("a client lists the tools")
def client_lists(context):
    context.tools = list_tools(context.url, TOKEN)


@when("a client calls get_schema")
def client_schema(context):
    context.result = call_tool(context.url, TOKEN, "get_schema", {})


@when('a client calls query with "{sql}"')
def client_query(context, sql):
    context.result = call_tool(context.url, TOKEN, "query", {"sql": sql})


@when("a client calls query with nothing")
def client_query_empty(context):
    context.result = call_tool(context.url, TOKEN, "query", {"sql": "   "})


@when('a request for the tool list is sent with the server\'s token and the header "{name}: {value}"')
def raw_request_with_header(context, name, value):
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}", name: value}
    context.response = httpx2.post(context.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers=headers, timeout=10)


@when("a request for the tool list is sent with the server's token and its own address as the Origin header")
def raw_request_own_origin(context):
    raw_request_with_header(context, "Origin", context.url.rsplit("/", 1)[0])


@when("a request for the tool list is sent with {header}")
def raw_request(context, header):
    headers = {"Accept": "application/json, text/event-stream"}
    if header == "the server's token":
        headers["Authorization"] = f"Bearer {TOKEN}"
    elif header.startswith("the header"):
        headers["Authorization"] = header.split('"')[1]
    elif header.startswith("the server's token under the scheme"):
        headers["Authorization"] = f"{header.split(chr(34))[1]} {TOKEN}"
    else:
        assert header == "no Authorization header", header
    context.response = httpx2.post(context.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers=headers, timeout=10)


# ---- when: the scripts ----

def _start(context, main_fn):
    """Run a server's main() with the environment of the scenario. uvicorn.run is replaced, so that no port is
    opened and the call returns: what the recipe asked uvicorn for is kept."""
    context.uvicorn_calls = []
    patch(context, uvicorn, "run", lambda app, **kw: context.uvicorn_calls.append(kw))
    os.environ["DATA_DIR"] = str(context.data_dir)
    os.environ["REFUNDS_DB"] = str(context.tmp / "refunds.duckdb")
    run_captured(context, main_fn)


@when("main/server.py is started")
def start_main_server(context):
    _start(context, server_module.main)


def _client(context, token: str | None, extra: list[str]):
    if token is None:
        os.environ.pop("MCP_SERVER_TOKEN", None)
    else:
        os.environ["MCP_SERVER_TOKEN"] = token
    run_captured(context, client_module.main, ["--url", context.url, *extra])


@when("main/client.py runs with the server's token")
def client_runs(context):
    _client(context, TOKEN, [])


@when('main/client.py runs with the server\'s token and "{args}"')
def client_runs_args(context, args):
    _client(context, TOKEN, shlex.split(args))


@when('main/client.py runs with the token "{token}"')
def client_runs_token(context, token):
    _client(context, token, [])


@when("main/client.py runs without a token")
def client_runs_no_token(context):
    _client(context, None, [])


# ---- then: tool results ----

@then('the tools are "{names}"')
def tools_are(context, names):
    assert sorted(context.tools) == sorted(n.strip() for n in names.split(",")), sorted(context.tools)


@then("every tool is marked read-only")
def all_read_only(context):
    assert all(a is not None and a.read_only_hint is True for a in context.tools.values()), context.tools


@then('the tool "{name}" is marked read-only')
def tool_read_only(context, name):
    assert context.tools[name].read_only_hint is True


@then('the tool "{name}" is not marked read-only')
def tool_not_read_only(context, name):
    assert context.tools[name].read_only_hint is False


@then("the tool result is not an error")
def not_error(context):
    assert context.result["is_error"] is False, context.result


@then('the tool result is an error that holds "{text}"')
def is_error(context, text):
    assert context.result["is_error"] is True and text in context.result["result"], context.result


@then('the schema lists the table "{table}" with {rows:d} rows and the columns "{columns}"')
def schema_table(context, table, rows, columns):
    t = next(t for t in context.result["result"] if t["table"] == table)
    assert t["rows"] == rows and [c["name"] for c in t["columns"]] == [c.strip() for c in columns.split(",")], t


@then("every column in the schema has a name and a type")
def schema_types(context):
    columns = [c for t in context.result["result"] for c in t["columns"]]
    assert len(columns) == 9 and all(c["name"] and c["type"] for c in columns), columns


@then('the result\'s columns are "{columns}"')
def result_columns(context, columns):
    assert context.result["result"]["columns"] == [c.strip() for c in columns.split(",")]


@then("the result's rows are {rows}")
def result_rows(context, rows):
    assert context.result["result"]["rows"] == json.loads(rows), context.result


@then("the result has {n:d} rows")
def result_row_count(context, n):
    assert len(context.result["result"]["rows"]) == n, len(context.result["result"]["rows"])


@then("the result says it was truncated")
def truncated(context):
    assert context.result["result"]["truncated"] is True


@then("the result says it was not truncated")
def not_truncated(context):
    assert context.result["result"]["truncated"] is False


@then('the table "{table}" still has {n:d} rows')
def table_rows(context, table, n):
    assert call_tool(context.url, TOKEN, "query", {"sql": f"SELECT count(*) FROM {table}"})["result"]["rows"] == [[n]]


# ---- then: the log ----

@then("the log has {n:d} lines")
def log_has(context, n):
    assert len(context.log_lines.json()) == n, context.log_lines.texts


@then('the last log line has tool "{tool}" and status "{status}"')
def last_log(context, tool, status):
    line = context.log_lines.json()[-1]
    assert (line["tool"], line["status"]) == (tool, status) and isinstance(line["ms"], int), line


@then('log line {i:d} has tool "{tool}" and status "{status}"')
def log_line(context, i, tool, status):
    line = context.log_lines.json()[i - 1]
    assert (line["tool"], line["status"]) == (tool, status), line


@then('log line {i:d} has exactly the fields "{fields}"')
def log_fields(context, i, fields):
    assert list(context.log_lines.json()[i - 1]) == [f.strip() for f in fields.split(",")]


@then('no log line holds "{a}" or "{b}"')
def log_without(context, a, b):
    text = "\n".join(context.log_lines.texts)
    assert context.log_lines.texts and a not in text and b not in text, text


def _everything(context) -> str:
    return "\n".join(context.log_lines.texts) + context.printed + context.printed_err


@then("the server's token is in no log line and in no printed line")
def token_nowhere(context):
    assert context.log_lines.texts and context.printed and TOKEN not in _everything(context)


@then('the token "{token}" is in no log line and in no printed line')
def other_token_nowhere(context, token):
    assert token not in _everything(context)


# ---- then: HTTP, start-up, the client's output ----

@then("the HTTP status is {status:d}")
def http_status(context, status):
    assert context.response.status_code == status, context.response.text[:300]


@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed_err[-600:])


@then('the error output holds "{text}"')
def err_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('the error output starts with "{text}"')
def err_starts(context, text):
    assert context.printed_err.startswith(text), context.printed_err


@then('the error output is the one line "{start}" the server\'s address "{end}"')
def err_is(context, start, end):
    assert context.printed_err == f"{start} {context.url}{end}\n", context.printed_err


@then('the printed output holds "{text}"')
def out_holds(context, text):
    assert text in context.printed, context.printed[-600:]


@then("the server was not started")
def not_started(context):
    assert context.uvicorn_calls == []


@then("the server was started")
def started(context):
    assert len(context.uvicorn_calls) == 1


@then('the server was started on host "{host}" and port {port:d}')
def started_on(context, host, port):
    assert [(c["host"], c["port"]) for c in context.uvicorn_calls] == [(host, port)], context.uvicorn_calls


def _steps(context) -> list[dict]:
    return [json.loads(line) for line in context.printed.splitlines() if line.startswith("{")]


@then('{n:d} lines are printed, with the steps "{steps}"')
def printed_steps(context, n, steps):
    got = [s["step"] for s in _steps(context)]
    assert len(got) == n and got == [s.strip() for s in steps.split(",")], got


@then('printed line {i:d} lists the tools "{names}", both read-only')
def printed_tools(context, i, names):
    tools = _steps(context)[i - 1]["tools"]
    assert [t["name"] for t in tools] == [n.strip() for n in names.split(",")] and all(t["read_only"] for t in tools)


@then('printed line {i:d} has the rows of {n:d} cities, "{city}" first with {orders:d} orders')
def printed_rows(context, i, n, city, orders):
    step = _steps(context)[i - 1]
    rows = step["result"]["rows"]
    assert step["is_error"] is False and len(rows) == n and rows[0][:2] == [city, orders], step


@then('printed line {i:d} is the refusal of "{sql}"')
def printed_refusal(context, i, sql):
    step = _steps(context)[i - 1]
    assert step["sql"] == sql and step["is_error"] is True and "only SELECT is allowed" in step["result"], step


@then("at least {n:d} connection to 127.0.0.1 was opened")
def connections_opened(context, n):
    assert len(context.connections) >= n


# ---- clean_up.py ----

def _hashes(folder) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@given("a copy of the recipe folder with the files a run writes")
def recipe_copy(context):
    context.copy = context.tmp / "recipe"
    shutil.copytree(ROOT / "output", context.copy / "output", ignore=shutil.ignore_patterns("*.duckdb*"))
    context.kept_runs = _hashes(context.copy)
    assert context.kept_runs, "the recipe's output folder has no captured run"
    (context.copy / "data").mkdir()
    for name in RUN_FILES:
        (context.copy / name).write_bytes(b"a file a run wrote")


@when("clean_up.py runs")
def clean_up_runs(context):
    run_captured(context, clean_up.main, [], root=context.copy)


@when('clean_up.py runs with "{args}"')
def clean_up_runs_with(context, args):
    run_captured(context, clean_up.main, shlex.split(args), root=context.copy)


@then('the printed lines are "{lines}"')
def printed_lines_are(context, lines):
    assert context.printed.splitlines() == [x.strip() for x in lines.split(",")], context.printed


@then('{n:d} printed lines start with "{text}"')
def printed_lines_start(context, n, text):
    assert sum(line.startswith(text) for line in context.printed.splitlines()) == n, context.printed


@then("the copy has no Parquet file and no refunds database")
def copy_clean(context):
    assert not [name for name in RUN_FILES if (context.copy / name).exists()]


@then("the copy still has the files a run writes")
def copy_still(context):
    assert all((context.copy / name).exists() for name in RUN_FILES)


@then("the captured runs in the copy are as they were")
def captured_same(context):
    assert _hashes(context.copy) == context.kept_runs


# ---- methods, a tool that breaks, sites beyond this machine, queries at the same time ----

@when("a {method} request is sent to the endpoint without a token")
def request_without_token(context, method):
    context.response = httpx2.request(method, context.url, headers={"Accept": "application/json, text/event-stream"},
                                      timeout=10)


@given("the engine breaks on every schema read")
def engine_breaks(context):
    def broken():
        raise RuntimeError("the engine broke")

    patch(context, context.engine, "schema", broken)


@then("the server's token is not in the tool result and in no log line")
def token_not_in_result(context):
    assert TOKEN not in str(context.result["result"]) + "\n".join(context.log_lines.texts)


@given('MCP_ALLOWED_ORIGINS is "{origins}"')
def allowed_origins(context, origins):
    os.environ["MCP_ALLOWED_ORIGINS"] = origins


@given('the main server is running for the site "{origin}" with MCP_HOST "{host}"')
def server_for_site(context, origin, host):
    context.engine = ReadOnlyEngine(context.tables, max_rows=Settings().max_rows)
    settings = Settings(token=TOKEN, data_dir=context.data_dir, host=host, allowed_origins=(origin,))
    context.url = serve(context, server_module.build_app(settings, server_module.build_server(context.engine)))


@when('a tool-list request carries the server\'s token and the headers "{headers}"')
def raw_request_with_headers(context, headers):
    sent = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}
    for pair in headers.split(","):
        name, _, value = pair.strip().partition(": ")
        sent[name] = value
    context.response = httpx2.post(context.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers=sent, timeout=10)


@when("{n:d} clients call query at the same time, each for another store")
def queries_at_once(context, n):
    from concurrent.futures import ThreadPoolExecutor

    def ask(i):
        store = i % 6 + 1
        return store, call_tool(context.url, TOKEN, "query", {
            "sql": f"SELECT store_id, count(*) FROM orders WHERE store_id = {store} GROUP BY store_id"})

    with ThreadPoolExecutor(max_workers=n) as pool:
        context.at_once = list(pool.map(ask, range(n)))


@then("every one of them got the row of its own store")
def own_rows(context):
    assert len(context.at_once) == 12
    for store, result in context.at_once:
        assert result["is_error"] is False and [r[0] for r in result["result"]["rows"]] == [store], (store, result)


@then('the tool result does not hold "{text}"')
def result_lacks(context, text):
    assert text not in str(context.result["result"])
# ---- then: text values ----

@then("the first value of the result's first row has {n:d} characters")
def first_value_length(context, n):
    assert len(context.result["result"]["rows"][0][0]) == n, len(context.result["result"]["rows"][0][0])


@then("the result says a text value was cut")
def text_cut(context):
    assert context.result["result"]["text_cut"] is True


@then("the result says no text value was cut")
def text_not_cut(context):
    assert context.result["result"]["text_cut"] is False


@then("the last log line says a text value was cut")
def log_text_cut(context):
    assert context.log_lines.json()[-1]["text_cut"] is True


@then('the last log line gives the reason "{kind}" and not the SQL')
def log_reason(context, kind):
    line = context.log_lines.json()[-1]
    assert list(line) == ["tool", "status", "ms", "reason"] and line["reason"] == kind, line


# ---- the size of a whole result ----

@then("the result has fewer than {high:d} rows and more than {low:d}")
def rows_between(context, high, low):
    assert low < len(context.result["result"]["rows"]) < high, len(context.result["result"]["rows"])


@then("the rows of the result are at most {n:d} characters as JSON")
def rows_size(context, n):
    assert sum(len(json.dumps(r)) for r in context.result["result"]["rows"]) <= n


@then("the result's first row is for order {order:d}")
def first_row(context, order):
    assert context.result["result"]["rows"][0][0] == order


@then("the last log line says the columns were cut")
def log_columns_cut(context):
    assert context.log_lines.json()[-1].get("columns_cut") is True, context.log_lines.texts[-1]


@then("the last log line has the result's note")
def log_note(context):
    assert context.log_lines.json()[-1].get("note") == context.result["result"]["note"], context.log_lines.texts[-1]


@then("the last log line says the result was truncated")
def log_truncated(context):
    assert context.log_lines.json()[-1]["truncated"] is True


# ---- closure polish: origins, a server that answers 500, the configuration lock, wide results ----

@given("a stand-in at the client's address that answers every request with HTTP 500")
def stand_in_500(context):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def _any(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = b"Internal Server Error"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = _any

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context.add_cleanup(server.shutdown)
    context.url = f"http://127.0.0.1:{server.server_address[1]}/mcp"


@then("the server's token is not in what the client printed")
def token_not_printed(context):
    assert context.printed_err and TOKEN not in context.printed + context.printed_err, context.printed_err


@then('the engine refuses "{statement}" with an error that holds "{text}"')
def engine_locked(context, statement, text):
    try:
        context.engine.con.execute(statement)
    except Exception as e:
        assert text in str(e), str(e)
    else:
        raise AssertionError(f"{statement} was accepted")


def _wide(n: int, width: int, source: str) -> str:
    cells = ", ".join(f"repeat('x', {width}) AS c{i}" for i in range(1, n + 1))
    return f"SELECT {cells} FROM {source}"


@when("a client calls query with a SELECT of {n:d} columns named c1 to c{last:d} from one order")
def wide_query(context, n, last):
    context.result = call_tool(context.url, TOKEN, "query", {"sql": _wide(n, 3, "orders LIMIT 1")})


@when("a client calls query with a SELECT of {n:d} columns of {width:d} characters from every order")
def wide_long_query(context, n, width):
    context.result = call_tool(context.url, TOKEN, "query", {"sql": _wide(n, width, "orders")})


@then("the result's columns are c1 to c{last:d}")
def result_columns(context, last):
    assert context.result["result"]["columns"] == [f"c{i}" for i in range(1, last + 1)]
    assert all(len(r) == last for r in context.result["result"]["rows"])


@then("the result says its columns were cut")
def columns_cut(context):
    assert context.result["result"]["columns_cut"] is True


@then("the result says its columns were not cut")
def columns_not_cut(context):
    assert context.result["result"]["columns_cut"] is False


@then('the result\'s note is "{note}"')
def result_note(context, note):
    assert context.result["result"].get("note") == note, context.result["result"].get("note")


@then("the result has no note")
def result_no_note(context):
    assert "note" not in context.result["result"], context.result["result"]
