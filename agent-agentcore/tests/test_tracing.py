"""Logfire behind the flag: off sends nothing and sets nothing up; on gives one trace per request; on without a
token still sends nothing."""
import json
import os
import subprocess
import sys

from tests.conftest import ROOT


def probe(**env) -> dict:
    env = {**os.environ, **env}
    env.pop("LOGFIRE_SEND_TO_LOGFIRE", None)       # the test override is off here: the recipe's own setting decides
    done = subprocess.run([sys.executable, str(ROOT / "tests" / "trace_probe.py")], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_with_the_flag_off_no_span_is_made_and_nothing_is_sent():
    seen = probe(LOGFIRE_ENABLED="false", LOGFIRE_TOKEN="made-up-token-that-must-not-be-used")
    assert seen["status"] == "done" and seen["traced"] is False
    assert seen["spans"] == [] and seen["connections"] == []
    assert seen["provider"] == "ProxyTracerProvider"          # OpenTelemetry's default: no provider was set


def test_with_the_flag_on_one_request_is_one_trace_with_model_and_tool_spans():
    seen = probe(LOGFIRE_ENABLED="true", LOGFIRE_TOKEN="")
    assert seen["traced"] is True and seen["traces"] == 1 and seen["roots"] == ["member request"]
    assert seen["spans"].count("chat") == 4                                   # one span per model call
    assert [n for n in seen["spans"] if n.startswith("execute_tool")] == [
        "execute_tool search_books", "execute_tool search_books", "execute_tool place_hold"]


def test_the_probe_does_see_a_connection_when_the_flag_is_on_and_a_token_is_set():
    """The control for the two "nothing is sent" tests: with a token, Logfire tries to send, and the probe sees it."""
    seen = probe(LOGFIRE_ENABLED="true", LOGFIRE_TOKEN="made-up-logfire-token")
    assert len(seen["spans"]) > 0 and len(seen["connections"]) >= 1


def test_with_the_flag_on_and_no_token_the_spans_stay_on_this_machine():
    seen = probe(LOGFIRE_ENABLED="true", LOGFIRE_TOKEN="")
    assert len(seen["spans"]) > 0 and seen["connections"] == []
