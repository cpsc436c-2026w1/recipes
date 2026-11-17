"""main/call_gateway.py against the gateway's own code, offline.

The gateway from ext-deploy/, a fake model service and moto (a stand-in for DynamoDB, SNS and Secrets Manager) run in
this process, the same way `ext-deploy/tests/local_run.py` runs them. So the refusals the client sees here are the
gateway's real refusals, with their real codes. Nothing leaves the machine: no AWS account, no token, no model key.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
sys.path.insert(0, str(RECIPE / "ext-deploy" / "tests"))
import conftest as gw_harness  # noqa: E402  ext-deploy's harness: fake AWS settings, servers, gw-admin in process

spec = importlib.util.spec_from_file_location("call_gateway", RECIPE / "main" / "call_gateway.py")
client_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = client_mod  # dataclasses looks the module up by name while the class is built (Python 3.14)
spec.loader.exec_module(client_mod)
Gateway, GatewayRefused = client_mod.Gateway, client_mod.GatewayRefused

HI = [{"role": "user", "content": "hi"}]


@pytest.fixture(scope="module")
def base_url():
    import os

    from moto import mock_aws

    with mock_aws():
        os.environ["TOPIC_ARN"] = gw_harness.create_aws_resources()
        from gateway import aws, secrets

        aws.reset()
        secrets.reset_for_tests()
        up_server, up_thread, up = gw_harness.start_server(gw_harness.fake_upstream.app)
        os.environ["UPSTREAM_BASE_URL"] = up + "/v1"
        from gateway.app import app

        server, thread, url = gw_harness.start_server(app)
        yield url + "/v1"
        server.should_exit = up_server.should_exit = True
        thread.join(5)
        up_thread.join(5)


def issue(*flags) -> tuple[str, str]:
    """A key as the course issues one, with gw-admin. Returns (key id, token)."""
    out = gw_harness.run_admin("issue", "--owner", "student", "--kind", "person", *flags)
    token = out.strip().splitlines()[-1]
    return gw_harness.auth.KEY_RE.match(token).group(1), token


def test_models_and_one_chat(base_url):
    _, token = issue()
    gw = Gateway(base_url, token)
    assert gw.models() == ["example-embed", "example-large", "example-small"]
    reply = gw.chat(HI, model="example-small", max_tokens=50)
    assert reply.text and reply.tokens_in > 0 and reply.tokens_out > 0 and reply.request_id


def test_unknown_token_is_refused_and_not_retried(base_url):
    slept = []
    gw = Gateway(base_url, "gw-NOTAKEY.not-a-real-token", sleep=slept.append)
    with pytest.raises(GatewayRefused) as e:
        gw.chat_with_retry(HI, model="example-small")
    assert (e.value.status, e.value.code) == (401, "invalid_api_key") and slept == []


def test_model_outside_the_list_is_refused(base_url):
    _, token = issue("--models", "example-small")
    with pytest.raises(GatewayRefused) as e:
        Gateway(base_url, token).chat(HI, model="example-large")
    assert (e.value.status, e.value.code) == (403, "model_not_allowed")


def test_max_tokens_decides_whether_a_request_fits_the_budget(base_url):
    # Decision (max_tokens): the gateway reserves the price of max_tokens before the call. With 0.0001 left,
    # a request that may write 1,000 tokens does not fit; the same request capped at 100 tokens does.
    _, token = issue("--budget", "0.0001")
    gw = Gateway(base_url, token)
    with pytest.raises(GatewayRefused) as e:
        gw.chat(HI, model="example-large", max_tokens=1000)
    assert (e.value.status, e.value.code) == (429, "budget_exhausted")
    assert gw.chat(HI, model="example-large", max_tokens=100).text


def test_spent_budget_is_not_retried(base_url):
    slept = []
    _, token = issue("--budget", "0.0001")
    with pytest.raises(GatewayRefused) as e:
        Gateway(base_url, token, sleep=slept.append).chat_with_retry(HI, model="example-large", max_tokens=1000)
    assert e.value.code == "budget_exhausted" and slept == []  # waiting does not refill a budget


def test_rate_limit_waits_for_retry_after_once(base_url):
    key_id, token = issue()
    gw_harness.run_admin("rate", "set", key_id, "2")
    slept = []
    gw = Gateway(base_url, token, sleep=slept.append)
    gw.chat(HI, model="example-small", max_tokens=20)
    gw.chat(HI, model="example-small", max_tokens=20)
    with pytest.raises(GatewayRefused) as e:
        gw.chat(HI, model="example-small", max_tokens=20)
    assert (e.value.status, e.value.code, e.value.retry_after) == (429, "rate_limit_exceeded", 60)
    # chat_with_retry waits the Retry-After the gateway sent, then tries once more. The test does not really sleep,
    # so the retry lands in the same minute and is refused again, unless the minute turned over in between.
    try:
        gw.chat_with_retry(HI, model="example-small", max_tokens=20)
    except GatewayRefused as again:
        assert again.code == "rate_limit_exceeded"
    assert slept == [60]


def test_command_line_prints_no_token(base_url, monkeypatch, capsys):
    _, token = issue()
    monkeypatch.setenv("COURSE_GATEWAY_URL", base_url)
    monkeypatch.setenv("KB_MCP_TOKEN", token)
    assert client_mod.main(["--model", "example-small"]) == 0
    out = capsys.readouterr().out
    assert "example-large" in out and "reply:" in out and token not in out


def test_command_line_explains_a_refusal(base_url, monkeypatch, capsys):
    _, token = issue("--budget", "0.0001")
    monkeypatch.setenv("COURSE_GATEWAY_URL", base_url)
    monkeypatch.setenv("KB_MCP_TOKEN", token)
    assert client_mod.main(["--model", "example-large", "--max-tokens", "1000"]) == 1
    out = capsys.readouterr().out
    assert "budget_exhausted" in out and "what to do: this request does not fit what is left" in out


def test_every_refusal_code_the_gateway_sends_has_advice():
    src = "".join(p.read_text() for p in (RECIPE / "ext-deploy" / "src" / "gateway").glob("*.py"))
    for code in ("invalid_api_key", "key_revoked", "key_expired", "model_not_allowed", "rate_limit_exceeded",
                 "budget_exhausted", "upstream_rate_limited", "upstream_error", "upstream_rejected",
                 "upstream_key_refused"):
        assert f'"{code}"' in src and code in client_mod.ADVICE


def test_kb_mcp_token_wins_and_the_older_names_still_work(monkeypatch):
    for name in ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_URL", "http://old.example/v1/")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "old-token")
    assert client_mod.gateway_env() == ("http://old.example/v1", "old-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "http://new.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    assert client_mod.gateway_env() == ("http://new.example/v1", "new-token")


def test_command_line_with_the_older_names(base_url, monkeypatch, capsys):
    _, token = issue()
    for name in ("COURSE_GATEWAY_URL", "KB_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_URL", base_url)
    monkeypatch.setenv("COURSE_MCP_TOKEN", token)
    assert client_mod.main(["--model", "example-small"]) == 0


def test_no_temperature_for_gpt_models():
    assert client_mod.sampling("qwen3.6-35b-a3b") == {"temperature": 0}
    for model in ("gpt-4o", "openai/gpt-4.1", "o3", "o4-mini"):
        assert client_mod.sampling(model) == {}


def test_the_client_waits_longer_than_the_gateway():
    # the gateway waits 240 s for the upstream (ext-deploy settings); a client that gave up first would see no
    # answer for a call that is charged anyway
    assert Gateway("http://127.0.0.1:9/v1", "t").http.timeout.read == 250


def test_allowance_codes_of_another_gateway_have_advice_and_are_not_retried(monkeypatch):
    # the course LLM gateway sends these; ext-deploy has no allowances, so they are not in its source
    slept = []
    gw = Gateway("http://127.0.0.1:9/v1", "t", sleep=slept.append)
    for code in ("model_allowance_used", "model_allowance_ended"):
        assert "pick another model" in client_mod.ADVICE[code]
        calls = []

        def refuse(*a, **kw):
            calls.append(1)
            raise GatewayRefused(403, code, "no calls left", None)

        monkeypatch.setattr(gw, "chat", refuse)
        with pytest.raises(GatewayRefused):
            gw.chat_with_retry(HI)
        assert calls == [1] and slept == []
