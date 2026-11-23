"""How the steps run the recipe's scripts and read what they printed. Nothing here reaches AWS."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from harness import FAILING_AWS, FAKE_AWS, RECIPE, SLEEP_LOG, base_env, commands, put_on_path, put_tool

TIMEOUT_S = 240        # a script that waits for ever fails its scenario instead of hanging the run


@dataclass
class Run:
    script: str
    code: int
    out: str
    err: str
    calls: list[str] = field(default_factory=list)      # fake runs: every call the script made to aws
    regions: list[str] = field(default_factory=list)    # fake runs: AWS_REGION of each call

    @property
    def cmds(self) -> list[str]:
        return commands(self.out)

    def step(self, n: str) -> str:
        """What the script printed under the heading `== <n>. ...`, up to the next heading."""
        m = re.search(rf"^== {re.escape(n)}\. .*?$(.*?)(?=^== |\Z)", self.out, flags=re.M | re.S)
        assert m, f"the run printed no step {n}:\n{self.out[-2000:]}"
        return m.group(1)

    def step_cmds(self, n: str) -> list[str]:
        return commands(self.step(n))

    def step_json(self, n: str):
        """The first JSON value a step printed as the answer of AWS (not the JSON inside a printed command)."""
        lines = [line for line in self.step(n).splitlines() if not line.startswith("$ ")]
        text = "\n".join(lines)
        m = re.search(r"^[\[{]", text, flags=re.M)
        assert m, f"step {n} printed no JSON:\n{text}"
        return json.JSONDecoder().raw_decode(text[m.start():])[0]


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    # the clean-up contract: clean_up.sh deletes only with --yes (with DRY=1 it still only prints)
    args = ["--yes"] if script == "clean_up.sh" else []
    return subprocess.run(["bash", str(RECIPE / script), *args], env=env, capture_output=True, text=True,
                          timeout=TIMEOUT_S)


def dry(context, script: str, **extra) -> Run:
    """DRY=1; any call to aws exits 99 and writes a line the steps look for."""
    bin_dir = put_on_path(context.tmp, FAILING_AWS)
    env = base_env(context.tmp) | {"DRY": "1", "PATH": f"{bin_dir}:{os.environ['PATH']}"} | context.env | extra
    p = _run(script, env)
    context.run = Run(script, p.returncode, p.stdout, p.stderr)
    return context.run


def fake(context, script: str, **extra) -> Run:
    """For real (no DRY), against the stand-in `aws` of tests/harness.py. `sleep` is a stand-in too: it writes the
    wait to sleep.log and does not wait. unset: names of variables the run must not have at all."""
    unset = extra.pop("unset", ())
    bin_dir, log = put_on_path(context.tmp, FAKE_AWS), context.tmp / "aws.log"
    put_tool(bin_dir, "sleep", SLEEP_LOG)
    log.write_text("")
    Path(str(log) + ".region").write_text("")
    (context.tmp / "sleep.log").write_text("")
    env = (base_env(context.tmp) | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(log),
                                    "SLEEP_LOG": str(context.tmp / "sleep.log")} | context.env | extra)
    for name in unset:
        env.pop(name, None)
    p = _run(script, env)
    context.run = Run(script, p.returncode, p.stdout, p.stderr, log.read_text().splitlines(),
                      Path(str(log) + ".region").read_text().splitlines())
    return context.run


def local(context, script: str, tmp: Path | None = None, **extra) -> Run:
    """For real, with the real AWS CLI against the moto server of the feature."""
    assert context.moto_url, "this step runs a script against the local stand-in: tag the feature @moto"
    env = base_env(tmp or context.tmp) | {"AWS_ENDPOINT_URL": context.moto_url} | context.env | extra
    p = _run(script, env)
    context.run = Run(script, p.returncode, p.stdout, p.stderr)
    return context.run


def shared_run(context, key: str, script: str, **extra) -> Run:
    """A run that the scenarios of one feature share: made by the first scenario that asks, read by the others."""
    if key not in context.shared:
        context.shared[key] = local(context, script, tmp=context.shared_tmp, **extra)
    context.run = context.shared[key]
    return context.run


def reset_moto(url: str) -> None:
    urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST"), timeout=10)


def ddb(context):
    """A boto3 client on the moto server, for looking at what a script left there."""
    import boto3

    return boto3.client("dynamodb", region_name="ca-central-1", endpoint_url=context.moto_url,
                        aws_access_key_id="testing", aws_secret_access_key="testing")


def items(context, table: str) -> list[dict]:
    """Every item of a table on the moto server, with DynamoDB's types taken off."""
    found = ddb(context).scan(TableName=table, ConsistentRead=True)["Items"]
    return [{k: next(iter(v.values())) for k, v in item.items()} for item in found]


def item(context, table: str, **key) -> dict | None:
    return next((i for i in items(context, table) if all(i.get(k) == v for k, v in key.items())), None)


def request_items(cmd: str) -> dict:
    """The --request-items value of a printed batch-write-item command."""
    m = re.search(r"--request-items (\{.*?\}) --(?:return-consumed-capacity|query)", cmd)
    assert m, cmd[:200]
    return json.loads(m.group(1))
