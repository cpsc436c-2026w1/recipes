"""The indexing function: inserts, updates and deletes reach the index; a replayed batch changes nothing; a bad record
goes to the failure queue without holding up the records behind it; the package is one file signed for `aoss`."""

import ast
import io
import json
import zipfile

import pytest

from main import handler
from main.config import Settings
from main.envs import LocalEnv, stream_records
from main.infra import function_zip
from main.mappings import index_body, keyword_query
from main.run import AFTER_BAD, BAD, run_local, write
from main.standin import StandInIndex


@pytest.fixture
def env():
    e = LocalEnv(Settings())
    e.deliver()
    yield e
    e.close()


def doc(env, item_id):
    hits = env.search({"query": {"ids": {"values": [item_id]}}})
    return hits[0]["_source"] if hits else None


def test_starting_items_insert_update_and_delete_reach_the_index(env):
    assert env.count() == 8 and doc(env, "I04")["name"] == "Headlamp HX-200"
    write(env.table, "INSERT", "I09", {"id": "I09", "name": "Camp stove", "description": "Gas stove", "price_cad": 79.0})
    write(env.table, "MODIFY", "I04", {"name": "Headlamp HX-300"})
    write(env.table, "REMOVE", "I08", None)
    assert doc(env, "I09") is None                     # not until the mapping delivers the stream
    env.deliver()
    assert doc(env, "I09")["name"] == "Camp stove" and doc(env, "I09")["price_cad"] == 79
    assert doc(env, "I04")["name"] == "Headlamp HX-300" and doc(env, "I04")["index_version"] == 1
    assert doc(env, "I08") is None and env.count() == 8
    assert "id" not in doc(env, "I04")                 # the key is the document id, not a field


def test_a_replayed_batch_leaves_the_same_documents(env):
    items = [{k: float(v) if hasattr(v, "as_tuple") else v for k, v in env.table.get_item(Key={"id": i})["Item"].items()}
             for i in ("I01", "I02")]
    before = {i: {k: v for k, v in doc(env, i).items() if k != "indexed_at"} for i in ("I01", "I02")}
    assert env.replay(stream_records(items)) == "ok" and env.replay(stream_records(items)) == "ok"
    after = {i: {k: v for k, v in doc(env, i).items() if k != "indexed_at"} for i in ("I01", "I02")}
    assert env.count() == 8 and after == {i: {**before[i], "changed_at": after[i]["changed_at"]} for i in before}


def test_a_replayed_delete_is_not_an_error(env):
    write(env.table, "REMOVE", "I03", None)
    env.deliver()
    gone = [{"eventName": "REMOVE", "dynamodb": {"Keys": {"id": {"S": "I03"}}, "SequenceNumber": "9"}}]
    assert env.replay(gone) == "ok" and doc(env, "I03") is None


def test_the_last_change_of_an_item_in_a_batch_wins():
    recs = stream_records([{"id": "A", "name": "first"}, {"id": "A", "name": "second"}])
    recs.append({"eventName": "REMOVE", "dynamodb": {"Keys": {"id": {"S": "B"}}, "SequenceNumber": "3"}})
    acts = handler.actions(recs, "items-v1", 1, now=0.0)
    assert [(a, i, (s or {}).get("name")) for a, i, s in acts] == [("index", "A", "second"), ("delete", "B", None)]


def test_a_bad_record_goes_to_the_failure_queue_and_the_next_write_still_arrives(env):
    write(env.table, "INSERT", BAD["id"], BAD)
    write(env.table, "INSERT", AFTER_BAD["id"], AFTER_BAD)
    counts = env.deliver()
    assert counts["discarded"] == 1                    # bisected down to the one bad record, then given up on
    assert doc(env, BAD["id"]) is None and doc(env, AFTER_BAD["id"])["name"] == AFTER_BAD["name"]
    failed = env.failures()
    assert len(failed) == 1 and failed[0]["DDBStreamBatchInfo"]["batchSize"] == 1
    assert env.record_keys(failed[0]) == [BAD["id"]]
    assert any("mapper_parsing_exception" in line for line in env.log)
    write(env.table, "MODIFY", "I01", {"name": "Trail shoe"})          # the shard is not blocked behind it
    env.deliver()
    assert doc(env, "I01")["name"] == "Trail shoe"


def test_the_function_raises_when_the_index_refuses_a_document():
    index = StandInIndex("items-v1", index_body(Settings()))
    with pytest.raises(handler.BatchFailed, match="price_cad"):
        handler.apply(stream_records([{"id": "X", "name": "x", "price_cad": "free"}, {"id": "Y", "name": "y"}]),
                      index.post, "items-v1", 1, log=lambda line: None)
    assert index.get("Y") is not None and index.get("X") is None   # the good one was written; the retry repeats it


def test_bm25_ranks_the_name_above_the_description():
    index = StandInIndex("items-v1", index_body(Settings()))
    index.post("/_bulk", handler.bulk_body([("index", "N", {"name": "lamp", "description": "x"}),
                                            ("index", "D", {"name": "x", "description": "lamp"})], "items-v1"))
    hits = index.search(keyword_query("lamp"))
    assert [h["_id"] for h in hits] == ["N", "D"] and hits[0]["_score"] > hits[1]["_score"] > 0


def test_the_package_is_one_file_with_no_dependencies_to_bundle():
    names = zipfile.ZipFile(io.BytesIO(function_zip())).namelist()
    assert names == ["handler.py"]
    tree = ast.parse(zipfile.ZipFile(io.BytesIO(function_zip())).read("handler.py"))
    roots = {(n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
             for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert roots <= {"__future__", "json", "os", "random", "time", "urllib", "decimal", "hashlib", "socket", "boto3",
                     "botocore"}, roots


def test_requests_are_signed_for_aoss(monkeypatch):
    from botocore.credentials import Credentials

    sent = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        sent.update(url=req.full_url, headers=dict(req.header_items()), body=req.data)
        return Resp(json.dumps({"errors": False, "items": [{"index": {"status": 201}}]}).encode())

    monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)
    post = handler.signed_post("https://abc.ca-central-1.aoss.amazonaws.com", "ca-central-1",
                               credentials=Credentials("AKIDEXAMPLE", "secret", "token"))
    post("/_bulk", '{"index": {"_index": "items-v1", "_id": "A"}}\n{"name": "a"}\n')
    import hashlib

    auth = {k.lower(): v for k, v in sent["headers"].items()}
    assert "/ca-central-1/aoss/aws4_request" in auth["authorization"]
    assert auth["x-amz-content-sha256"] == hashlib.sha256(sent["body"]).hexdigest()
    signed = auth["authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert "x-amz-content-sha256" in signed and "host" in signed
    assert auth["x-amz-security-token"] == "token" and sent["url"].endswith("/_bulk")


def test_main_scenario_offline():
    out = run_local(Settings())
    assert "starting items: 8 documents in items-v1" in out
    assert "REMOVE I08: gone from search" in out
    assert "failure queue: 1 message(s), batch size [1], item ['I11']" in out
    assert "documents 9 before, 9 after (9 items in the table that the index takes); one document each for I04 " \
           "and I09: True; I04 unchanged: True" in out


# --- temporary errors are retried; only refused documents are set aside ---------------------------------------

GOOD = [{"id": f"G{n}", "name": f"good {n}", "price_cad": 10.0 + n} for n in range(8)]


class Flaky:
    """The index's _bulk endpoint, failing for the first `n` calls: the whole request (`how="request"`, as a 503,
    a 403 while a policy spreads, or a timeout would) or each item (`how="items"`, a 429 per document)."""

    def __init__(self, index, n: int, how: str = "request"):
        self.index, self.n, self.how, self.calls = index, n, how, 0

    def __call__(self, path, body):
        self.calls += 1
        if self.calls <= self.n:
            if self.how == "request":
                raise handler.Temporary("HTTP 503 from the collection (fake)")
            lines = [json.loads(line) for line in body.splitlines() if line.strip()]
            actions = [next(iter(line)) for line in lines if next(iter(line)) in ("index", "delete")]
            return {"errors": True, "items": [{a: {"status": 429, "error": {"type": "too_many_requests"}}}
                                               for a in actions]}
        return self.index.post(path, body)


@pytest.mark.parametrize("n, how", [(6, "request"), (6, "items"), (30, "request"), (30, "items")])
def test_temporary_errors_lose_no_good_record(env, n, how):
    """Six failing calls in a row are retried inside one invocation; thirty outlast it, and Lambda's retries of
    the reported record carry on from there. Either way every good record reaches the index and the failure queue
    stays empty: nothing is split down to single records and set aside."""
    env.post, before = Flaky(env.index, n, how), env.mapping.invocations
    for item in GOOD:
        write(env.table, "INSERT", item["id"], item)
    counts = env.deliver()
    assert counts["discarded"] == 0 and env.failures() == []
    assert all(doc(env, i["id"])["name"] == i["name"] for i in GOOD)
    assert env.mapping.invocations - before == (1 if n < handler.MAX_ATTEMPTS else 1 + n // handler.MAX_ATTEMPTS)


def test_temporary_errors_that_never_clear_end_in_the_queue_after_the_retries(env):
    """Bounded: after MAX_RETRIES tries the records go to the failure queue (the record age caps it too)."""
    from main.infra import MAX_RETRIES

    env.post, before = Flaky(env.index, 10 ** 6), env.mapping.invocations
    for item in GOOD[:3]:
        write(env.table, "INSERT", item["id"], item)
    counts = env.deliver()
    assert counts["discarded"] == 3 and env.mapping.invocations - before == MAX_RETRIES + 1
    assert len(env.failures()) == 1


def test_a_refused_document_alone_reaches_the_queue(env):
    """Two good records, the refused one, two more good ones, in one batch: the function reports the refused
    record, Lambda sends the rest again, the function raises with it first, Lambda splits; only it is set aside."""
    batch = GOOD[:2] + [BAD] + GOOD[2:4]
    for item in batch:
        write(env.table, "INSERT", item["id"], item)
    counts = env.deliver()
    assert counts["batches"] == 1 and counts["discarded"] == 1
    assert env.mapping.discarded_keys == [BAD["id"]]
    failed = env.failures()
    assert len(failed) == 1 and failed[0]["DDBStreamBatchInfo"]["batchSize"] == 1
    assert env.record_keys(failed[0]) == [BAD["id"]]
    assert all(doc(env, i["id"]) for i in GOOD[:4]) and doc(env, BAD["id"]) is None


def test_the_function_reports_the_earliest_record_it_could_not_write():
    index = StandInIndex("items-v1", index_body(Settings()))
    recs = stream_records([{"id": "A", "name": "a"}, {"id": "B", "name": "b", "price_cad": "free"},
                           {"id": "C", "name": "c"}])
    out = handler.apply(recs, index.post, "items-v1", 1, log=lambda line: None, sleep=lambda s: None)
    assert out == {"batchItemFailures": [{"itemIdentifier": recs[1]["dynamodb"]["SequenceNumber"]}]}
    assert index.get("A") and index.get("C") and index.get("B") is None


def test_retries_stop_before_the_function_times_out():
    """Each failing call takes 10 s of the function's time; no call may start with less than SAFETY_S left. Without
    the time-left check in apply() the fifth call would start after the timeout (checked by removing it)."""
    clock, starts = [40.0], []

    def post(path, body):
        starts.append(clock[0])
        clock[0] -= 10                                    # a slow refusal: the request itself uses up time
        raise handler.Temporary("HTTP 503")

    def sleep(s):
        clock[0] -= s

    out = handler.apply(stream_records([{"id": "A", "name": "a"}]), post, "items-v1", 1, log=lambda line: None,
                        sleep=sleep, time_left=lambda: clock[0], jitter=lambda: 1.0)
    assert out["batchItemFailures"] and len(starts) == 4
    assert min(starts) >= handler.SAFETY_S, starts


def test_the_probe_writes_and_deletes_one_document(monkeypatch):
    monkeypatch.setenv("INDEX", "items-v1")
    index = StandInIndex("items-v1", index_body(Settings()))
    assert handler.probe(index.post)["ok"] is True and index.count() == 0

    def refused(path, body):
        raise handler.Temporary("HTTP 403 from the collection")

    assert handler.probe(refused) == {"ok": False, "detail": "HTTP 403 from the collection"}
