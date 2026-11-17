"""The main server plus one write tool, `record_refund`, that changes data only with a person's confirmation code.

Run:  MCP_SERVER_TOKEN=... APPROVAL_SECRET=... uv run python ext-confirmed-write/write_server.py

The flow (ext README, "Main scenario"):
1. The model calls record_refund without a code. Nothing changes; the tool result describes the change and the
   command a person runs to approve it.
2. The person reads the change, runs approve.py, and pastes the code it prints into the chat as a user message.
3. The model calls record_refund again with the same arguments and the code. The server checks the code against
   these exact arguments and writes one row. A repeat of the call with the same request id writes nothing more.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run as a script: `main` is one folder up
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer                     # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError          # noqa: E402
from mcp.types import ToolAnnotations                          # noqa: E402

from confirm import Refund, read_code                          # noqa: E402
from main.config import OUTPUT_DIR, Settings                   # noqa: E402
from main.data import ensure_data                              # noqa: E402
from main.engine import ReadOnlyEngine                         # noqa: E402
from main.server import _record, add_read_tools, build_app     # noqa: E402
from store import RefundStore                                  # noqa: E402

log = logging.getLogger("mcp-server-minimal")

# Not read-only, not destructive (it adds a row and removes nothing), idempotent (same request id, same effect).
# A client may use these hints to ask the person before the call; the code check below holds either way.
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_write_server(engine: ReadOnlyEngine, store: RefundStore, approval_secret: str) -> MCPServer:
    if len(approval_secret) < 16:
        raise SystemExit("APPROVAL_SECRET must be set and at least 16 characters (ext README, 'Run it')")
    mcp = MCPServer("team-data-with-refunds",
                    instructions=("Read tools as in the main server, plus record_refund. record_refund changes "
                                  "data only with a confirmation code that a person gives you; never invent one."))
    add_read_tools(mcp, engine)

    @mcp.tool(annotations=WRITE)
    def record_refund(request_id: str, order_id: int, amount_cad: float, reason: str, confirmation: str = "") -> str:
        """Record a refund for one order. Needs a person's approval.

        Call it first without `confirmation`: nothing changes, and the result says what a person must approve.
        When the person gives you a code, call it again with the same arguments and `confirmation` set to the code.
        `request_id` is yours to choose (for example the date and a short slug); reuse it when you retry, so the
        refund is recorded once.
        """
        started = time.perf_counter()
        refund = Refund(request_id.strip(), int(order_id), round(float(amount_cad), 2), reason.strip())
        problem = _validate(engine, refund)
        if problem:
            _record("record_refund", "invalid", started)
            raise ToolError(problem)
        if not confirmation:
            _record("record_refund", "needs_confirmation", started)
            return json.dumps({"status": "needs_confirmation", "changed": False, "change": json.loads(refund.canonical()),
                               "ask_the_person_to_run": approve_command(refund),
                               "then": "call record_refund again with the same arguments and confirmation=<code>"})
        approver, expired = read_code(approval_secret, confirmation.strip(), refund)
        if approver is None:
            # wrong code, a code for another change, or one the model made up: refused, and nothing changes
            _record("record_refund", "refused", started)
            raise ToolError("confirmation code does not match this change; ask the person to approve it again")
        if expired:
            # the person's own code, used after its time: an approval is for now, not for whenever the chat
            # log is read again
            _record("record_refund", "expired", started, approved_by=approver)
            raise ToolError("confirmation code has expired; ask the person to approve the change again")
        result = store.apply(refund, approver)
        _record("record_refund", result["status"], started, approved_by=approver)
        if result["status"] == "conflict":
            raise ToolError(f"request_id {refund.request_id!r} was already used for a different refund; "
                            "nothing changed")
        return json.dumps({**result, "changed": result["status"] == "applied"})

    return mcp


def _validate(engine: ReadOnlyEngine, refund: Refund) -> str | None:
    """Checks in app code, before any approval is asked for: a person should not be asked to approve nonsense."""
    if not 4 <= len(refund.request_id) <= 64:
        return "request_id must be 4 to 64 characters"
    if not refund.reason or len(refund.reason) > 200:
        return "reason must be 1 to 200 characters"
    row = engine.fetch_one("SELECT amount_cad FROM orders WHERE order_id = ?", [refund.order_id])
    if row is None:
        return f"order {refund.order_id} does not exist"
    if not 0 < refund.amount_cad <= float(row[0]):
        return f"amount must be more than 0 and at most the order's amount, {float(row[0]):.2f} CAD"
    return None


def approve_command(refund: Refund) -> str:
    args = ["uv", "run", "python", "ext-confirmed-write/approve.py", "--request-id", refund.request_id,
            "--order-id", str(refund.order_id), "--amount", f"{refund.amount_cad:.2f}", "--reason", refund.reason]
    return shlex.join(args)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    engine = ReadOnlyEngine(ensure_data(settings.data_dir), max_rows=settings.max_rows,
                            query_timeout_s=settings.query_timeout_s)
    store = RefundStore(Path(os.environ.get("REFUNDS_DB", OUTPUT_DIR / "refunds.duckdb")))
    mcp = build_write_server(engine, store, os.environ.get("APPROVAL_SECRET", ""))
    print(f"MCP server with record_refund on {settings.url} (Ctrl-C stops it)", flush=True)
    uvicorn.run(build_app(settings, mcp), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
