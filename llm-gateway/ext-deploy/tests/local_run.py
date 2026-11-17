"""Run the gateway on your laptop and watch a key spend its budget and get refused.

    uv run python tests/local_run.py

The gateway, a fake upstream and a fake AWS (moto) all run in this one process.
Nothing leaves your machine: no network, no AWS account, no provider key.

    uv run python tests/local_run.py --serve

keeps the gateway running instead, and prints a base URL and a test key for curl
or the OpenAI client. Stop it with Ctrl-C. The key stops working when the process ends.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: E402  sets fake AWS credentials and the region
import httpx  # noqa: E402
from moto import mock_aws  # noqa: E402
from moto.sns.models import sns_backends  # noqa: E402

MODEL = "example-large"
BUDGET = "0.001"
# Each request may produce up to 1,000 output tokens, and the fake upstream reports that it did.
BODY = {"model": MODEL, "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}],
        "x_fake": {"completion_tokens": 1000}}


def start() -> tuple[str, str]:
    """Make the tables, the topic and the secret in moto and start both servers. Returns (gateway, topic)."""
    topic = conftest.create_aws_resources()
    os.environ["TOPIC_ARN"] = topic
    from gateway import aws

    aws.reset()
    _, _, up = conftest.start_server(conftest.fake_upstream.app)
    os.environ["UPSTREAM_BASE_URL"] = up + "/v1"
    from gateway.app import app

    _, _, gw = conftest.start_server(app)
    return gw, topic


def issue(budget: str | None = None) -> tuple[str, str]:
    """Issue a key with gw-admin. Returns (key_id, key)."""
    argv = ["issue", "--owner", "local", "--kind", "person"]
    if budget:
        argv += ["--budget", budget]
    key = conftest.run_admin(*argv).strip().splitlines()[-1]
    return conftest.auth.KEY_RE.match(key).group(1), key


def show_refusal() -> None:
    from gateway import pricing

    gw, topic = start()
    key_id, key = issue(BUDGET)
    budget = pricing.micro_to_amount(int(conftest.key_item(key_id)["budget_micro"]))
    print(f"gateway  {gw}/v1  (fake upstream and fake AWS in this process)")
    print(f"key      {key_id}  owner local  budget {budget} USD")
    print()
    with httpx.Client(base_url=gw, timeout=30) as client:
        for n in range(1, 7):
            r = client.post("/v1/chat/completions", json=BODY, headers=conftest.bearer(key))
            spent = pricing.micro_to_amount(int(conftest.key_item(key_id)["spent_micro"]))
            print(f"request {n}  HTTP {r.status_code}  spent {spent} USD")
            if r.status_code != 200:
                err = r.json()["error"]
                print(f"           {err['code']}: {err['message']}")
    print()
    print("notices published to the SNS topic")
    for n in sns_backends["111122223333"]["ca-central-1"].topics[topic].sent_notifications:
        print(f"  {n[2]}")
    for cmd in ("spend", "list"):
        print()
        print(f"gw-admin {cmd}")
        for line in conftest.run_admin(cmd).strip().splitlines():
            print(f"  {line}")


def serve() -> None:
    gw, _ = start()
    _, key = issue()
    print(f"gateway  {gw}/v1  (fake upstream and fake AWS in this process)")
    print(f"test key {key}")
    print(f"curl -N {gw}/v1/chat/completions -H 'Authorization: Bearer {key}' "
          "-H 'Content-Type: application/json' "
          f"-d '{{\"model\": \"{MODEL}\", \"stream\": true, \"messages\": [{{\"role\": \"user\", \"content\": \"hi\"}}]}}'")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    with mock_aws():
        serve() if "--serve" in sys.argv[1:] else show_refusal()
