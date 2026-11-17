"""The MCP server: two read tools over the team's tables, behind a bearer token.

Run:  MCP_SERVER_TOKEN=... uv run python -m main.server      (README, "Run it")

What the model sees is only what is defined here: two tools, their descriptions and their arguments. It does not
see the files, the connection or any other function (README, "Decisions": tool set).
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from main.auth import BearerTokenAuth
from main.config import Settings
from main.data import ensure_data
from main.engine import QueryRefused, QueryTimedOut, ReadOnlyEngine

log = logging.getLogger("mcp-server-minimal")

# Hints a client shows the person or uses to decide what needs approval. They describe the tool; they do not
# enforce anything: the engine's checks do (README, "Decisions": tool annotations).
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _record(tool: str, status: str, started: float, **fields) -> None:
    """One log line per tool call: which tool, the outcome, how long. Never the token."""
    log.info(json.dumps({"tool": tool, "status": status, "ms": round((time.perf_counter() - started) * 1000),
                         **fields}))


def build_server(engine: ReadOnlyEngine, name: str = "team-data") -> MCPServer:
    mcp = MCPServer(
        name,
        instructions=("Read-only access to the team's tables. Call get_schema first, then query with one "
                      f"SELECT statement. At most {engine.max_rows} rows come back; aggregate in SQL instead "
                      "of asking for many rows."),
    )
    add_read_tools(mcp, engine)
    return mcp


def add_read_tools(mcp: MCPServer, engine: ReadOnlyEngine) -> None:
    """The two read tools. ext-confirmed-write reuses this and adds its write tool beside them."""

    @mcp.tool(annotations=READ_ONLY)
    def get_schema() -> str:
        """List the tables you can query, with each column's name and type and each table's row count."""
        started = time.perf_counter()
        tables = engine.schema()
        _record("get_schema", "ok", started, tables=len(tables))
        return json.dumps(tables)

    @mcp.tool(annotations=READ_ONLY)
    def query(sql: str) -> str:
        """Run one read-only SQL SELECT (DuckDB dialect) over the tables from get_schema.

        Returns JSON with columns, rows, truncated (true when more rows matched than were returned), text_cut
        (true when a text value was longer than 200 characters and was cut) and columns_cut (true when the query
        had more than 100 columns and only the first 100 are returned). A result whose first row alone is too
        large has no rows and a note that says so.
        Any other statement (INSERT, UPDATE, DELETE, CREATE, COPY, ATTACH, SET, several statements) is refused.
        """
        started = time.perf_counter()
        try:
            result = engine.query(sql)
        except QueryRefused as e:
            # the kind of statement, never its text: a log that shows DELETE after DELETE is someone probing,
            # and the SQL itself can hold a user's words
            _record("query", "refused", started, reason=e.kind)
            # ToolError reaches the model as a tool result marked as an error, so it can correct its query
            raise ToolError(f"query refused: {e}") from None
        except QueryTimedOut as e:
            _record("query", "timeout", started)
            raise ToolError(f"query failed: {e}") from None
        except Exception as e:  # a SELECT that fails in DuckDB: unknown column, blocked file read, ...
            _record("query", "failed", started)
            raise ToolError(f"query failed: {type(e).__name__}: {str(e)[:300]}") from None
        # Every truncation goes in the log line as well as in the result (AGENTSEC02-BP02): a cut that only the model
        # sees cannot be found afterwards. The note is the recipe's own text, never a value from the rows.
        marks = {"note": result.note} if result.note else {}
        _record("query", "ok", started, rows=len(result.rows), truncated=result.truncated, text_cut=result.text_cut,
                columns_cut=result.columns_cut, **marks)
        reply = {"columns": result.columns, "rows": result.rows, "truncated": result.truncated,
                 "text_cut": result.text_cut, "columns_cut": result.columns_cut}
        if result.note:
            reply["note"] = result.note
        return json.dumps(reply)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """Which Host and Origin headers the server answers (README, "Decisions": transport).

    On this machine the SDK's own default applies: only this machine's names. Beyond it (MCP_HOST=0.0.0.0 in a
    container) the SDK checks nothing unless it is told what to allow, and the MCP specification says a server
    must validate Origin. So the sites come from MCP_ALLOWED_ORIGINS, and without them the server does not start."""
    if settings.host in LOOPBACK:
        return None
    if not settings.allowed_origins:
        raise SystemExit(f"MCP_HOST is {settings.host}, not this machine: set MCP_ALLOWED_ORIGINS to the sites the "
                         "server answers, for example https://team.example.org (README, 'Adapt it')")
    hosts = []
    for origin in settings.allowed_origins:
        site = urlsplit(origin)
        if site.scheme not in ("http", "https") or not site.hostname:
            # "team.example.org" without a scheme parses to no host at all: the server would start and then
            # refuse every request, its own site included.
            raise SystemExit(f"MCP_ALLOWED_ORIGINS: {origin!r} is not a site: write it with http:// or https://, "
                             "for example https://team.example.org")
        hosts += [site.netloc, f"{site.hostname}:*"]       # the name as the proxy sends it, with or without a port
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=list(settings.allowed_origins))


def build_app(settings: Settings, mcp: MCPServer):
    # stateless_http: each request stands alone, so the server keeps no session per client and can restart or run
    # as several copies; json_response: plain JSON replies, simple to read in a log.
    inner = mcp.streamable_http_app(stateless_http=True, json_response=True, host=settings.host,
                                    transport_security=transport_security(settings))
    return BearerTokenAuth(inner, settings.token)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    engine = ReadOnlyEngine(ensure_data(settings.data_dir), max_rows=settings.max_rows,
                            query_timeout_s=settings.query_timeout_s)
    app = build_app(settings, build_server(engine))
    print(f"MCP server on {settings.url} (Ctrl-C stops it)", flush=True)
    # 127.0.0.1 by default: reachable from this machine only. Serving it to others needs HTTPS in front.
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
