"""Shared fixtures: moto for the whole session, the fake upstream and the gateway
as two real uvicorn servers in background threads, and helpers to issue keys."""

import contextlib
import io
import os
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.update(
    AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_SESSION_TOKEN="testing",
    AWS_DEFAULT_REGION="ca-central-1", AWS_REGION="ca-central-1", MOTO_ACCOUNT_ID="111122223333",
    TABLE_KEYS="llm-gateway-keys", TABLE_USAGE="llm-gateway-usage", TABLE_TEXTS="llm-gateway-texts",
    SECRET_ARN="llm-gateway/upstream-key",
    DISCONNECT_GRACE_S="0.5",
)
sys.path.insert(0, str(Path(__file__).parent))

import boto3  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from moto import mock_aws  # noqa: E402

import fake_upstream  # noqa: E402
from gateway import admin, auth, aws, budget, secrets, textlog  # noqa: E402

FAKE_UPSTREAM_KEY = "upstream-test-key-not-real"

# DynamoDB applies one UpdateItem as a whole. moto does not: it stores a new row with only its keys and
# fills in the attributes afterwards. The gateway settles in its own thread, so a test that reads the usage
# row at that moment sees a row with attributes missing. The lock makes the read wait until a settle that
# has started is finished. It changes nothing in the gateway: settle runs the same code, one call at a time.
_settle_lock = threading.Lock()
_settle = budget.settle


def _settle_whole(*args, **kwargs):
    with _settle_lock:
        return _settle(*args, **kwargs)


budget.settle = _settle_whole

# The same for the text log's one UpdateItem: a test reads a text row only when no write is under way.
_text_lock = threading.Lock()
_text_write = textlog.write


def _text_write_whole(*args, **kwargs):
    with _text_lock:
        return _text_write(*args, **kwargs)


textlog.write = _text_write_whole


def create_aws_resources() -> str:
    """Tables, topic and secret as the SAM template makes them. Returns the topic ARN."""
    ddb = boto3.client("dynamodb", region_name="ca-central-1")
    ddb.create_table(
        TableName="llm-gateway-keys", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "key_hash", "AttributeType": "S"},
                              {"AttributeName": "key_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{"IndexName": "by_key_id",
                                 "KeySchema": [{"AttributeName": "key_id", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "KEYS_ONLY"}}],
    )
    ddb.create_table(
        TableName="llm-gateway-usage", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "key_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "key_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
    )
    ddb.create_table(
        TableName="llm-gateway-texts", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "key_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "key_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
    )
    topic = boto3.client("sns", region_name="ca-central-1").create_topic(Name="llm-gateway-alerts")["TopicArn"]
    boto3.client("secretsmanager", region_name="ca-central-1").create_secret(
        Name="llm-gateway/upstream-key", SecretString=FAKE_UPSTREAM_KEY)
    return topic


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(app) -> tuple[uvicorn.Server, threading.Thread, str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                           log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def topic_arn():
    with mock_aws():
        arn = create_aws_resources()
        os.environ["TOPIC_ARN"] = arn
        aws.reset()
        secrets.reset_for_tests()
        yield arn


@pytest.fixture(scope="session")
def upstream_url(topic_arn):
    server, thread, url = start_server(fake_upstream.app)
    yield url
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def gateway_url(topic_arn, upstream_url):
    os.environ["UPSTREAM_BASE_URL"] = upstream_url + "/v1"
    from gateway.app import app

    server, thread, url = start_server(app)
    yield url
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def client(gateway_url):
    with httpx.Client(base_url=gateway_url, timeout=30) as c:
        yield c


def run_admin(*argv: str) -> str:
    """Run gw-admin in process and return what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        admin.main(list(argv))
    return out.getvalue()


@pytest.fixture
def issue(topic_arn):
    """issue(**opts) -> (key_id, key). Options become gw-admin issue flags."""

    def _issue(owner="ana", kind="person", **opts):
        argv = ["issue", "--owner", owner, "--kind", kind]
        for k, v in opts.items():
            argv += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
        text = run_admin(*argv)
        key = text.strip().splitlines()[-1]
        return auth.KEY_RE.match(key).group(1), key

    return _issue


def key_item(key_id: str) -> dict:
    return admin._by_id(key_id)


def usage_rows(key_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    with _settle_lock:
        rows = aws.usage_table().query(KeyConditionExpression=Key("key_id").eq(key_id))["Items"]
    return [r for r in rows if not r["sk"].startswith("min#")]


def text_row(key_id: str, rid: str) -> dict | None:
    """The text log's item for one request: the two values every log line carries are its whole key."""
    with _text_lock:
        return aws.texts_table().get_item(Key={"key_id": key_id, "sk": rid}).get("Item")


def text_rows(key_id: str) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    with _text_lock:
        return aws.texts_table().query(KeyConditionExpression=Key("key_id").eq(key_id))["Items"]


def wait_for_usage(key_id: str, timeout: float = 10.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = usage_rows(key_id)
        if rows:
            return rows
        time.sleep(0.05)
    return []


def set_attr(key_id: str, name: str, value) -> None:
    item = key_item(key_id)
    aws.keys_table().update_item(Key={"key_hash": item["key_hash"]}, UpdateExpression="SET #n = :v",
                                 ExpressionAttributeNames={"#n": name}, ExpressionAttributeValues={":v": value})


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def chat_body(**kw) -> dict:
    body = {"model": "example-large", "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return body
