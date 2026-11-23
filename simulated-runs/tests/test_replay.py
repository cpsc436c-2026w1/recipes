"""replay.py: timing, run-id prefixes, sent.jsonl and each target (moto for Kinesis, SQS and S3)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import generate
import replay

START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


class FakeTime:
    """A clock that moves only when slept on, or by `cost` seconds per send."""

    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def wall(self):
        return START + timedelta(seconds=self.t)

    def sleep(self, s):
        self.t += s


def three():
    base = {"event_type": "trip_requested"}
    rows = [("dl-000001", "ev-000001", "t-0001", 161, "08:59:59", "09:00:00"),
            ("dl-000002", "ev-000002", "t-0002", 48, "09:00:29", "09:00:30"),
            ("dl-000003", "ev-000003", "t-0003", 230, "09:01:39", "09:01:40")]
    return [{**base, "delivery_id": d, "event_id": e, "trip_id": t, "pickup_zone_id": z,
             "event_time": f"2026-11-23T{et}.000Z", "received_time": f"2026-11-23T{rt}.000Z"}
            for d, e, t, z, et, rt in rows]


@pytest.fixture(scope="module")
def sim01(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    generate.generate("SIM01", out=out)
    return replay.load_deliveries("SIM01", out)


def sent_offsets(evidence):
    lines = [json.loads(x) for x in (evidence / "sent.jsonl").read_text().splitlines()]
    first = replay.parse(lines[0]["sent_time"])
    return lines, [(replay.parse(x["sent_time"]) - first).total_seconds() for x in lines]


def test_three_deliveries_at_speed_10(tmp_path):
    got, ft = [], FakeTime()
    sender = replay.LocalSender(got.extend)
    summary = replay.replay(three(), sender, "r1", 10, evidence=tmp_path, clock=ft.clock, wall=ft.wall,
                            sleep=ft.sleep)
    lines, offsets = sent_offsets(tmp_path)
    assert offsets == [0.0, 3.0, 10.0]
    assert [d["trip_id"] for d in got] == ["r1-t-0001", "r1-t-0002", "r1-t-0003"]
    assert lines[0] == {"delivery_id": "r1-dl-000001", "event_id": "r1-ev-000001", "sent_time": "2026-11-23T12:00:00.000Z",
                        "target": "local:local", "result": "ok"}
    assert summary["sent"] == 3 and summary["late_s"] == 0


def test_slow_sends_do_not_drift(tmp_path, sim01):
    """Each call costs 0.2 s; deliveries due meanwhile go in the next call, so none is sent more than 1 s late."""
    ft = FakeTime()

    def slow(batch):
        ft.t += 0.2

    summary = replay.replay(sim01, replay.LocalSender(slow), "r", 60, evidence=tmp_path, clock=ft.clock,
                            wall=ft.wall, sleep=ft.sleep)
    assert summary["sent"] == 2515 and summary["late_s"] <= 1.0
    plan = replay.planned([replay.prefixed(d, "r") for d in sim01], 60)
    lines, offsets = sent_offsets(tmp_path)
    due = {d["delivery_id"]: t for t, d in plan}
    assert all(abs(o - due[x["delivery_id"]]) <= 1.0 for o, x in zip(offsets, lines))


def test_rush_hour_into_kinesis_at_speed_60(aws, tmp_path, sim01):
    kin = aws.client("kinesis")
    kin.create_stream(StreamName="recipe-test-trip-events", ShardCount=1)
    ft = FakeTime()
    sender = replay.make_sender("kinesis", session=aws)
    summary = replay.replay(sim01, sender, "r2", 60, evidence=tmp_path, clock=ft.clock, wall=ft.wall,
                            sleep=ft.sleep)
    assert summary["sent"] == 2515 and max(sender.calls) <= 500
    shard = kin.describe_stream(StreamName="recipe-test-trip-events")["StreamDescription"]["Shards"][0]["ShardId"]
    it = kin.get_shard_iterator(StreamName="recipe-test-trip-events", ShardId=shard,
                                ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    records = []
    while it:
        page = kin.get_records(ShardIterator=it, Limit=10000)
        records += page["Records"]
        it = page.get("NextShardIterator") if page["Records"] else None
    assert len(records) == 2515
    assert {r["PartitionKey"] for r in records} == {json.loads(r["Data"])["trip_id"] for r in records}
    lines, offsets = sent_offsets(tmp_path)
    last = replay.parse(sim01[-1]["received_time"]) - replay.parse(min(d["received_time"] for d in sim01))
    assert abs(offsets[-1] - last.total_seconds() / 60) <= 1.0


class RefusingKinesis:
    """Refuses the second record of the first call, then takes everything."""

    def __init__(self):
        self.calls = []

    def put_records(self, StreamName, Records):
        self.calls.append(len(Records))
        if len(self.calls) == 1:
            return {"FailedRecordCount": 1, "Records": [{"SequenceNumber": "1"},
                    {"ErrorCode": "ProvisionedThroughputExceededException", "ErrorMessage": "slow down"}]
                    + [{"SequenceNumber": "1"}] * (len(Records) - 2)}
        return {"FailedRecordCount": 0, "Records": [{"SequenceNumber": "1"}] * len(Records)}


def test_refused_records_are_sent_again():
    client = RefusingKinesis()
    results = replay.KinesisSender(client, "s", sleep=lambda s: None).send(three())
    assert results == ["ok", "ok", "ok"] and client.calls == [3, 1]


def test_a_record_refused_five_times_is_reported():
    class Never(RefusingKinesis):
        def put_records(self, StreamName, Records):
            self.calls.append(len(Records))
            return {"FailedRecordCount": len(Records),
                    "Records": [{"ErrorCode": "InternalFailure", "ErrorMessage": "x"}] * len(Records)}
    results = replay.KinesisSender(Never(), "s", sleep=lambda s: None).send(three()[:1])
    assert results == ["InternalFailure: x"]


def test_sqs_batches_of_ten(aws, tmp_path):
    sqs = aws.client("sqs")
    url = sqs.create_queue(QueueName="recipe-test-trip-events")["QueueUrl"]
    many = [{**three()[0], "delivery_id": f"dl-{i:06d}", "event_id": f"ev-{i:06d}"} for i in range(25)]
    sender = replay.make_sender("sqs", session=aws)
    assert replay.replay(many, sender, "q", 0, evidence=tmp_path)["sent"] == 25
    assert sender.calls == [10, 10, 5]
    assert sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"]["ApproximateNumberOfMessages"] == "25"


def test_s3_one_object_per_slot(aws, tmp_path, sim01):
    s3 = aws.client("s3")
    s3.create_bucket(Bucket="recipe-test-lake-123456789012",
                     CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    sender = replay.make_sender("s3", run_id="r3", slot_min=1, session=aws)
    assert replay.replay(sim01, sender, "r3", 0, evidence=tmp_path)["sent"] == 2515
    objs = s3.list_objects_v2(Bucket="recipe-test-lake-123456789012", Prefix="raw/trip-events/dt=2026-11-23/")["Contents"]
    assert all(o["Key"].endswith("-r3.jsonl") for o in objs)
    assert objs[0]["Key"] == "raw/trip-events/dt=2026-11-23/20261123T0900Z-r3.jsonl"
    lines = sum(len(s3.get_object(Bucket="recipe-test-lake-123456789012", Key=o["Key"])["Body"].read().splitlines())
                for o in objs)
    assert lines == 2515 and len(objs) == len(sender.calls)


def test_two_replays_do_not_share_ids(sim01):
    got = []
    replay.replay(sim01, replay.LocalSender(got.extend), "r4")
    replay.replay(sim01, replay.LocalSender(got.extend), "r5")
    assert len(got) == 5030 and len({d["trip_id"] for d in got}) == 1674


def test_shift_to_now_moves_every_time_by_one_offset():
    moved = replay.shifted(three(), START)
    assert moved[0]["received_time"] == "2026-11-23T12:00:00.000Z"
    assert moved[2]["event_time"] == "2026-11-23T12:01:39.000Z"


def test_url_sender_signs_each_post(monkeypatch, aws):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(replay.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp())
    sender = replay.UrlSender(aws, "https://abc123.lambda-url.ca-central-1.on.aws/")
    assert sender.send(three()) == ["ok"] * 3
    assert "AWS4-HMAC-SHA256" in seen[0].headers["Authorization"]
    assert json.loads(seen[0].data)["deliveries"][0]["delivery_id"] == "dl-000001"


def test_a_failing_handler_is_written_down(tmp_path):
    def broken(batch):
        raise RuntimeError("table missing")
    summary = replay.replay(three(), replay.LocalSender(broken), "x", evidence=tmp_path)
    assert summary["failed"] == 3
    assert json.loads((tmp_path / "sent.jsonl").read_text().splitlines()[0])["result"] == "RuntimeError: table missing"


def test_cli_writes_evidence(tmp_path, sim01, monkeypatch):
    data = tmp_path / "data"
    generate.generate("SIM03", out=data)
    got = []
    monkeypatch.setattr(replay, "load_handler", lambda spec: got.extend)
    assert replay.main(["SIM03", "--run-id", "c1", "--to", "local", "--data-dir", str(data),
                        "--evidence", str(tmp_path / "ev")]) == 0
    assert len((tmp_path / "ev" / "c1" / "sent.jsonl").read_text().splitlines()) == 60
    assert "commit" in (tmp_path / "ev" / "c1" / "command.txt").read_text()
    assert len(got) == 60


def cp1_style(folder, source="trip-events"):
    """A delivery file as a team's generator writes it: its own fields, times without milliseconds, no delivery_id,
    written out of received_time order."""
    rows = [("ev-000003", "t-0002", "2026-11-23T09:00:40Z"),
            ("ev-000001", "t-0001", "2026-11-23T09:00:01Z"),
            ("ev-000002", "t-0001", "2026-11-23T09:00:20+00:00")]
    lines = [{"event_type": "trip_requested", "trip_id": t, "rider_id": "p-52201", "quoted_fare": 86.45,
              "event_time": rt, "event_id": e, "received_time": rt} for e, t, rt in rows]
    path = folder / "SIM01" / source / "dt=2026-11-23" / "hour=09" / "deliveries.jsonl"
    path.parent.mkdir(parents=True)
    (folder / "SIM01" / "_truth").mkdir()
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return lines


def test_a_team_line_with_extra_fields_and_no_delivery_id(tmp_path, monkeypatch):
    lines = cp1_style(tmp_path / "data")
    got = []
    monkeypatch.setattr(replay, "load_handler", lambda spec: got.extend)
    assert replay.main(["SIM01", "--run-id", "t1", "--to", "local", "--data-dir", str(tmp_path / "data"),
                        "--evidence", str(tmp_path / "ev")]) == 0
    assert [d["event_id"] for d in got] == ["t1-ev-000001", "t1-ev-000002", "t1-ev-000003"]
    by_id = {x["event_id"]: x for x in lines}
    for d in got:                     # the line as it was read, with the run id in front of its ids
        line = by_id[d["event_id"].removeprefix("t1-")]
        assert d == {**line, "event_id": d["event_id"], "trip_id": f"t1-{line['trip_id']}"}
    sent = [json.loads(x) for x in (tmp_path / "ev" / "t1" / "sent.jsonl").read_text().splitlines()]
    assert [x["delivery_id"] for x in sent] == ["t1-line-2026-11-23-09-000002", "t1-line-2026-11-23-09-000003",
                                                "t1-line-2026-11-23-09-000001"]
    assert all(x["result"] == "ok" for x in sent)


def test_a_team_source_folder_named_with_source(tmp_path, capsys):
    cp1_style(tmp_path / "data", source="orders")
    (tmp_path / "data" / "SIM01" / "payments").mkdir()
    args = ["SIM01", "--run-id", "t2", "--to", "local", "--target", "replay:discard",
            "--data-dir", str(tmp_path / "data"), "--evidence", str(tmp_path / "ev")]
    assert replay.main(args) == 2
    assert "2 folders of delivery files (orders, payments): name one with --source" in capsys.readouterr().err
    assert replay.main(args + ["--source", "orders"]) == 0
    assert len((tmp_path / "ev" / "t2" / "sent.jsonl").read_text().splitlines()) == 3


def test_the_one_source_folder_is_the_default(tmp_path):
    cp1_style(tmp_path / "data", source="orders")
    assert len(replay.load_deliveries("SIM01", tmp_path / "data")) == 3


def test_a_line_without_received_time_names_file_and_line(tmp_path, capsys):
    cp1_style(tmp_path / "data")
    path = tmp_path / "data" / "SIM01" / "trip-events" / "dt=2026-11-23" / "hour=09" / "deliveries.jsonl"
    rows = path.read_text().splitlines()
    rows[1] = json.dumps({k: v for k, v in json.loads(rows[1]).items() if k != "received_time"})
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(replay.LineError) as err:
        replay.load_deliveries("SIM01", tmp_path / "data")
    assert (err.value.file, err.value.line) == ("SIM01/trip-events/dt=2026-11-23/hour=09/deliveries.jsonl", 2)
    assert replay.main(["SIM01", "--run-id", "t3", "--to", "local", "--target", "replay:discard",
                        "--data-dir", str(tmp_path / "data"), "--evidence", str(tmp_path / "ev")]) == 2
    assert ("SIM01/trip-events/dt=2026-11-23/hour=09/deliveries.jsonl, line 2: no received_time"
            in capsys.readouterr().err)


def test_a_received_time_without_a_zone_is_refused(tmp_path):
    cp1_style(tmp_path / "data")
    path = tmp_path / "data" / "SIM01" / "trip-events" / "dt=2026-11-23" / "hour=09" / "deliveries.jsonl"
    path.write_text(path.read_text().replace("2026-11-23T09:00:40Z", "2026-11-23T09:00:40"))
    with pytest.raises(replay.LineError, match="line 1: received_time '2026-11-23T09:00:40' is not ISO 8601"):
        replay.load_deliveries("SIM01", tmp_path / "data")


def test_a_team_line_into_kinesis_is_keyed_by_trip_and_sent_without_a_made_up_id(aws, tmp_path):
    cp1_style(tmp_path / "data")
    kin = aws.client("kinesis")
    kin.create_stream(StreamName="recipe-test-trip-events", ShardCount=1)
    deliveries = replay.load_deliveries("SIM01", tmp_path / "data")
    assert replay.replay(deliveries, replay.make_sender("kinesis", session=aws), "k1", evidence=tmp_path)["sent"] == 3
    shard = kin.describe_stream(StreamName="recipe-test-trip-events")["StreamDescription"]["Shards"][0]["ShardId"]
    it = kin.get_shard_iterator(StreamName="recipe-test-trip-events", ShardId=shard,
                                ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    records = kin.get_records(ShardIterator=it)["Records"]
    assert [r["PartitionKey"] for r in records] == ["k1-t-0001", "k1-t-0001", "k1-t-0002"]
    assert all("delivery_id" not in json.loads(r["Data"]) for r in records)
