"""ext-sdk-lambda offline, the parts around the handler: settings, package, deploy and clean-up on moto, signing,
the p95, the eval's scoring."""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.sdk_lambda_fakes import ext_env, load, trips_file


@pytest.fixture
def es(tmp_path, monkeypatch):
    ext_env()
    monkeypatch.setenv("LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("OWNER", "tester")
    return load("settings").load_settings()


@pytest.fixture
def built(tmp_path):
    site = tmp_path / "site"
    (site / "somelib").mkdir(parents=True)
    (site / "somelib" / "__init__.py").write_text("")
    zip_path = tmp_path / "agent-sdk.zip"
    sizes = load("deploy", "sdk_deploy").build_zip(site, trips_file(tmp_path), zip_path)
    return zip_path, sizes


def test_the_function_gets_the_token_parameters_name_and_not_the_token(es):
    env = es.deployed_env()
    assert env["KB_MCP_TOKEN_PARAM"] == "test-agent-sdk-token" and "KB_MCP_TOKEN" not in env
    assert es.kb_mcp_token not in json.dumps(env) and "AWS_REGION" not in env
    assert env["TRIPS_PATH"] == "trips.parquet" and env["AGENT_SDK"] == "strands"


def test_a_missing_setting_stops_and_names_it(monkeypatch):
    ext_env()
    monkeypatch.delenv("AGENT_SDK")
    with pytest.raises(SystemExit, match="AGENT_SDK"):
        load("settings").load_settings()


def test_the_zip_holds_the_functions_files_the_data_and_the_libraries(built):
    zip_path, sizes = built
    names = set(zipfile.ZipFile(zip_path).namelist())
    deploy = load("deploy", "sdk_deploy")
    assert set(deploy.FUNCTION_FILES) | {"trips.parquet", "somelib/__init__.py"} == names
    assert not {"deploy.py", "clean_up.py", "invoke_url.py", "make_trips.py", "trip_eval.py"} & names
    assert sizes["zipped"] > 0 and sizes["unzipped"] >= sizes["zipped"] // 2


def test_the_package_leaves_out_what_the_runtime_has():
    deploy = load("deploy", "sdk_deploy")
    assert deploy.LEFT_OUT == ("boto3", "botocore", "s3transfer")
    assert "duckdb==2.0.0" in deploy.REQUIREMENTS and "pydantic-ai-slim[openai]==2.71.0" in deploy.REQUIREMENTS


def test_the_role_may_write_its_logs_and_read_its_one_parameter(es):
    policy = load("deploy", "sdk_deploy").role_policy(es, "111122223333")
    actions = sorted(a for st in policy["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str)
                                                               else st["Action"]))
    assert actions == ["logs:CreateLogStream", "logs:PutLogEvents", "ssm:GetParameter"]
    assert policy["Statement"][1]["Resource"].endswith(":parameter/test-agent-sdk-token")


def test_deploy_twice_then_clean_up_on_moto(es, built):
    from moto import mock_aws

    deploy, clean = load("deploy", "sdk_deploy"), load("clean_up", "sdk_clean_up")
    with mock_aws():
        url = deploy.up(es, built[0])
        assert deploy.up(es, built[0]) == url                      # the second run updates what is there
        dry = clean.clean(es, yes=False)
        assert dry.failed == [] and len(dry.removed) >= 6
        import boto3
        assert boto3.client("lambda", region_name="ca-central-1").list_functions()["Functions"]
        done = clean.clean(es, yes=True)
        assert done.failed == [], done.failed
        again = clean.clean(es, yes=True)
        assert again.failed == [] and again.removed == []
    rows = [json.loads(line) for line in open(os.environ["LEDGER"])]
    created = [r["type"] for r in rows if r["state"] == "created"]
    assert created.count("lambda-function") == 1 and created.count("iam-role") == 1     # not made twice
    assert {"lambda-function", "iam-role", "log-group", "ssm-parameter", "lambda-function-url"} <= \
        {r["type"] for r in rows if r["state"] == "deleted"}


def test_a_signed_request_carries_sigv4_headers():
    from botocore.credentials import Credentials

    inv = load("invoke_url", "sdk_invoke_url")
    req = inv.signed_request("https://abc.lambda-url.ca-central-1.on.aws/", b'{"question": "q"}',
                             Credentials("AKIDEXAMPLE", "secret", "session"))
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/ca-central-1/lambda/aws4_request" in headers["authorization"]
    assert "x-amz-date" in headers and headers["x-amz-security-token"] == "session"


def test_the_p95_of_36_times_is_the_35th_in_order():
    inv = load("invoke_url", "sdk_invoke_url")
    assert inv.p95_nearest_rank([float(i) for i in range(36, 0, -1)]) == 35.0
    assert len(inv.questions()) == 12


def test_the_eval_scores_answers_and_refusals_apart(tmp_path):
    te = load("trip_eval", "sdk_trip_eval")
    eng = load("engine").Engine(trips_file(tmp_path))
    cases = te.load_cases()
    refs = te.references(cases, eng)
    assert len(cases) == 12 and sum(r is None for r in refs.values()) == 2
    assert refs["day-total"] == 19074 and refs["zone161-08"] == 191 and refs["compare-161-230"] == 7661 - 7068
    good = {"status": "answered", "value": 191, "rows": [{"count_star()": 191}]}
    assert te.correct(good, 191)
    assert not te.correct({**good, "rows": [{"n": 190}]}, 191)                    # the log must hold the value
    assert not te.correct({**good, "value": 190}, 191)
    assert te.correct({"status": "did_not_answer", "reason": "no_value"}, None)
    assert not te.correct({"status": "did_not_answer", "reason": "gateway_refused"}, None)
    results = {c["id"]: [good if refs[c["id"]] == 191 else {"status": "error"}] * 3 for c in cases}
    report = te.score(results, refs)
    n = sum(r == 191 for r in refs.values())
    assert (report["runs_correct"], report["cases_passed"], report["passed"]) == (3 * n, n, False)


def test_a_live_scenario_runs_on_the_shell_settings_and_an_offline_one_on_the_example(monkeypatch):
    # live run 2026-11-22: every @live scenario got .env.example's PREFIX=test, made-up AWS keys and no token
    # parameter, and the first one ended behave with "Set KB_MCP_TOKEN_PARAM"
    import importlib.util
    from types import SimpleNamespace

    from tests.offline_env import ROOT

    spec = importlib.util.spec_from_file_location("behave_environment", ROOT / "features" / "environment.py")
    env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(env)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("PREFIX", "team-live")
    monkeypatch.setenv("AWS_PROFILE", "sandbox")
    context = SimpleNamespace()
    env.before_all(context)
    live = SimpleNamespace(effective_tags=["live"], skip=lambda reason: pytest.fail(reason))
    offline = SimpleNamespace(effective_tags=["R110"], skip=lambda reason: pytest.fail(reason))
    try:
        env.before_scenario(context, offline)
        assert os.environ["PREFIX"] == "test" and "AWS_PROFILE" not in os.environ
        env.before_scenario(context, live)
        assert os.environ["PREFIX"] == "team-live" and os.environ["AWS_PROFILE"] == "sandbox"
    finally:
        env.after_all(context)
