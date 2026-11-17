"""Traces behind a flag: Strands' OpenTelemetry spans go to Logfire only when LOGFIRE_ENABLED is true.

Strands opens a span for every agent invocation, every loop cycle, every model call and every tool call
(Strands docs, "Traces"). With the flag off, nothing here imports Logfire or sets a tracer provider, so those
spans go nowhere. With the flag on, `logfire.configure()` sets the provider; `send_to_logfire="if-token-present"`
sends spans to Logfire only when LOGFIRE_TOKEN is set, so a run without a token still does not send anything
(Logfire docs, "Configuration").
"""
from __future__ import annotations

from contextlib import contextmanager

from main.config import Settings


def configure(s: Settings, span_processors: list | None = None) -> bool:
    """Call once, in the entry point, before the agent is built. Returns whether tracing is on.
    `span_processors`: extra OpenTelemetry span processors; the tests pass one that keeps spans in memory."""
    if not s.logfire_enabled:
        return False
    import logfire

    logfire.configure(send_to_logfire="if-token-present", token=s.logfire_token or None,
                      service_name=f"{s.prefix}-agent", console=False, inspect_arguments=False,
                      additional_span_processors=span_processors)
    return True


@contextmanager
def request_span(enabled: bool, request_id: str, member: str):
    """The parent span of one member request: the model and tool spans below it carry the same trace id."""
    if not enabled:
        yield
        return
    import logfire

    with logfire.span("member request", request_id=request_id, member=member):
        yield
