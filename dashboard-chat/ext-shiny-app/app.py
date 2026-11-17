# Chat on the dashboard as a web page: Shiny Express with querychat over the team's table.
#
#   APP_MODEL=stub uv run shiny run ext-shiny-app/app.py --port 8000      offline: the stub gateway on this machine
#   uv run shiny run ext-shiny-app/app.py --port 8000                     the course LLM gateway
#
# A user types a question in the sidebar. querychat sends it to the LLM with the table's schema; the LLM answers with
# a tool call that holds SQL; source.py checks the SQL (main/sqlcheck.py) and runs it in the recipe's read-only engine
# (main/engine.py); the table and the two boxes show the result. The LLM never sees more than the schema and the rows
# of a result it asked for, and it never types a number that the page shows in the table.
#
# Environment, read at start:
#   COURSE_GATEWAY_URL, KB_MCP_TOKEN   the course LLM gateway (the older GATEWAY_URL, COURSE_MCP_TOKEN still work) and your course MCP token (not needed with APP_MODEL=stub)
#   CHAT_MODEL                      default qwen3.6-35b-a3b
#   APP_MODEL                       "gateway" (default) or "stub"
#   APP_TABLE                       the table the page shows and filters (default orders)
#
# Structure adapted from the CPSC 436C L15 querychat demo, itself adapted from DSCI 532.
#
# This text is a comment, not a docstring: Shiny Express puts a string that stands alone in the file on the page.

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]  # the recipe root, so `main` imports; this folder, for its modules

from client import make_client  # noqa: E402
from querychat.express import QueryChat  # noqa: E402
from shiny.express import render, ui  # noqa: E402
from source import RecipeSource, run_sql_tool  # noqa: E402

from main.config import Settings  # noqa: E402
from main.data import ensure_data  # noqa: E402
from main.engine import Engine  # noqa: E402

APP_MODEL = os.environ.get("APP_MODEL", "gateway")
TABLE = os.environ.get("APP_TABLE", "orders")

GREETING = """\
Ask about the shop's orders. For example:

* <span class="suggestion">Show the cake orders</span>
* <span class="suggestion">Show the orders of more than 40 dollars</span>
* <span class="suggestion">What were total sales by category?</span>
"""


settings = Settings()
engine = Engine(ensure_data())           # the team's tables, in memory, locked (main/engine.py)
source = RecipeSource(engine, TABLE)     # querychat's SQL goes through the recipe's check and engine (source.py)

client = make_client(settings, APP_MODEL, TABLE)   # the gateway, or the stub; at most 4 model calls per question
client.register_tool(run_sql_tool(engine))         # the recipe's run_sql: at most 50 rows reach the LLM (source.py)

qc = QueryChat(
    source,
    TABLE,
    client=client,
    greeting=GREETING,       # a fixed greeting: no model call when the page opens
    tools=("filter",),       # querychat's filter of the table; its own query tool is replaced by run_sql
    extra_instructions="There is no query tool; use run_sql. To answer a question with a number or a small table, "
                       "call the tool run_sql with one SELECT. To show rows on the dashboard, filter it.",
)
qc.sidebar(width=420)

ui.page_opts(title="Chat on the dashboard: the shop's orders", fillable=True)

with ui.layout_columns(col_widths=[6, 6], fill=False):
    with ui.value_box():
        "Orders shown"

        @render.text
        def n_orders():
            return f"{len(qc.df()):,}"

    with ui.value_box():
        "Their amount"

        @render.text
        def amount():
            frame = qc.df()
            return f"{frame['amount_cad'].sum():,.2f} CAD" if "amount_cad" in frame.columns else "-"

with ui.card(fill=True):
    with ui.card_header():

        @render.text
        def title():
            return qc.title() or "All orders"

    @render.data_frame
    def table():
        frame = qc.df()
        dates = [c for c in frame.columns if str(frame[c].dtype).startswith("datetime")]
        return frame.assign(**{c: frame[c].dt.strftime("%Y-%m-%d") for c in dates})  # a date, not a date and 00:00

    with ui.card_footer():

        @render.code
        def sql():
            return qc.sql() or f"SELECT * FROM {TABLE}"
