"""ext-embeddings: only text changes are queued; the worker re-reads DynamoDB; failures end in the dead-letter queue."""

import json

import boto3
from moto import mock_aws

from embed import StubEmbedder
from enqueue import handler as enqueue
from enqueue import message_for
from main.config import Settings, load_items
from main.infra import create_table
from worker import StandInVectors, create_queues, drain, handle, run_local, vector_index_body


def rec(name, new=None, old=None, seq="1"):
    d = {"SequenceNumber": seq, "Keys": {"id": {"S": "I01"}}}
    if new:
        d["NewImage"] = {k: {"S": v} for k, v in new.items()}
    if old:
        d["OldImage"] = {k: {"S": v} for k, v in old.items()}
    return {"eventName": name, "dynamodb": d}


def test_only_text_changes_are_queued():
    a = {"id": "I01", "name": "Shoe", "description": "light", "price_cad": "1"}
    assert message_for(rec("MODIFY", new={**a, "price_cad": "2"}, old=a)) is None
    m = message_for(rec("MODIFY", new={**a, "description": "heavy"}, old=a))
    assert m["op"] == "UPSERT" and m["text_hash"]
    assert message_for(rec("REMOVE", old=a))["op"] == "REMOVE"
    assert message_for(rec("INSERT", new=a))["op"] == "UPSERT"


def test_vector_index_records_model_and_version():
    body = vector_index_body(Settings(), 1024, "qwen3-embedding-0.6b")
    assert body["mappings"]["properties"]["embedding"]["dimension"] == 1024
    assert body["mappings"]["_meta"]["embedding_model"] == "qwen3-embedding-0.6b"


def test_worker_reads_the_source_of_truth_and_skips_repeats():
    s = Settings()
    with mock_aws():
        create_table(boto3.client("dynamodb"), s, load_items())
        table = boto3.resource("dynamodb").Table(s.table)
        emb, store = StubEmbedder(), StandInVectors()
        item = table.get_item(Key={"id": "I01"})["Item"]
        from enqueue import text_hash

        msg = {"id": "I01", "op": "UPSERT", "text_hash": text_hash(item)}
        assert handle(msg, table, emb, store) == "embedded"
        assert handle(msg, table, emb, store) == "already" and emb.calls == 1  # a repeat does not call the model
        table.update_item(Key={"id": "I01"}, UpdateExpression="SET description = :d",
                          ExpressionAttributeValues={":d": "changed again"})
        assert handle(msg, table, emb, store) == "stale"  # the queued text is no longer the item's text
        assert handle({"id": "I01", "op": "REMOVE"}, table, emb, store) == "removed" and store.get("I01") is None


def test_failing_message_moves_to_the_dead_letter_queue():
    s = Settings()
    with mock_aws():
        create_table(boto3.client("dynamodb"), s, load_items())
        table = boto3.resource("dynamodb").Table(s.table)
        sqs = boto3.client("sqs")
        url, dlq = create_queues(sqs, s, visibility_s=0)
        item = table.get_item(Key={"id": "I08"})["Item"]
        from enqueue import text_hash

        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(
            {"id": "I08", "op": "UPSERT", "text_hash": text_hash(item)}))
        counts = drain(sqs, url, table, StubEmbedder(fail_for={"Trekking"}), StandInVectors())
        assert counts == {"failed": 3}
        dead = sqs.receive_message(QueueUrl=dlq).get("Messages", [])
        assert [json.loads(m["Body"])["id"] for m in dead] == ["I08"]


def test_enqueue_batches_to_sqs():
    s = Settings()
    with mock_aws():
        sqs = boto3.client("sqs")
        url, _ = create_queues(sqs, s)
        a = {"id": "I01", "name": "Shoe"}
        out = enqueue({"Records": [rec("INSERT", new=a, seq=str(i)) for i in range(12)]}, None, sqs=sqs,
                      queue_url=url)
        assert out == {"queued": 12, "skipped": 0}


def test_extension_offline():
    out = run_local(Settings())
    price_line = next(line for line in out.splitlines() if line.startswith("price change on I03"))
    assert "queued 0, skipped 1" in price_line
    assert "dead-letter queue: ['I08']" in out


def test_gateway_embedder_reads_kb_mcp_token_first_and_the_older_names_still_work(monkeypatch):
    from embed import GatewayEmbedder

    monkeypatch.setenv("GATEWAY_URL", "https://older.example/v1/")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "older-token")
    e = GatewayEmbedder()
    assert (e.url, e.token) == ("https://older.example/v1", "older-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    e = GatewayEmbedder()
    assert (e.url, e.token) == ("https://gateway.example/v1", "new-token")
