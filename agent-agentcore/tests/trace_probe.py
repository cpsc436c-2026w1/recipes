"""Run by tests/test_tracing.py in a process of its own: one request on the stub model, with every network
connection refused and recorded, and an in-memory OpenTelemetry exporter beside Logfire's. Prints what it saw.

A process of its own because OpenTelemetry's tracer provider is set once per process: the flag-off and the flag-on
case cannot share one.
"""
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

connections = []


def refuse(self, address):
    connections.append(str(address))
    raise OSError("offline")


socket.socket.connect = refuse

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from main import tracing  # noqa: E402
from main.agent import run_request  # noqa: E402
from main.config import load_settings  # noqa: E402
from main.model import StubModel  # noqa: E402
from main.store import MemoryStore  # noqa: E402
from main.tools import LibraryTools  # noqa: E402

s = load_settings()
exporter = InMemorySpanExporter()
traced = tracing.configure(s, [SimpleSpanProcessor(exporter)])
with tracing.request_span(traced, "req-1", "sam"):
    run = run_request(s, "Which is shorter, Dune or Emma? Place a hold on the shorter one for me.", "sam",
                      StubModel(), LibraryTools(MemoryStore()), lambda approval: True, request_id="req-1")
if traced:
    import logfire

    logfire.force_flush()
spans = exporter.get_finished_spans()
root = [sp for sp in spans if sp.parent is None]
print(json.dumps({"traced": traced, "status": run["status"], "spans": [sp.name for sp in spans],
                  "traces": len({sp.context.trace_id for sp in spans}), "roots": [sp.name for sp in root],
                  "provider": type(trace.get_tracer_provider()).__name__, "connections": connections}))
