"""The Lambda function on its own: stream records in, change rows out, partial failures reported."""

import datetime as dt
import json

from main.handler import change_row, handler, to_micros


def record(name, seq, new=None, old=None, at=1_800_000_000):
    d = {"ApproximateCreationDateTime": at, "SequenceNumber": seq, "Keys": {"trip_id": {"S": "t-1"}}}
    if new:
        d["NewImage"] = new
    if old:
        d["OldImage"] = old
    return {"eventName": name, "dynamodb": d}


IMAGE = {"trip_id": {"S": "t-1"}, "status": {"S": "claimed"}, "fare_quote": {"N": "18.5"},
         "pickup_zone": {"S": "Downtown"}}


def test_times_in_microseconds():
    assert to_micros(1_800_000_000) == 1_800_000_000_000_000
    assert to_micros(dt.datetime(2027, 1, 15, 8, 0, tzinfo=dt.UTC)) == 1_800_000_000_000_000


def test_modify_row_is_typed_and_ordered():
    row = change_row(record("MODIFY", "123", new=IMAGE), arrival_us=7)
    assert row["op"] == "MODIFY" and row["fare_quote"] == 18.5 and row["driver_id"] is None
    assert row["arrival_time"] == 7 and row["change_time"] == 1_800_000_000_000_000
    assert len(row["seq"]) == 40 and row["seq"].endswith("123")
    assert change_row(record("MODIFY", "99", new=IMAGE), 0)["seq"] < row["seq"]  # text order = number order


def test_remove_keeps_the_last_values():
    row = change_row(record("REMOVE", "5", old=IMAGE), 0)
    assert row["op"] == "REMOVE" and row["status"] == "claimed"


class Firehose:
    def __init__(self, fail_first=0):
        self.fail_first, self.got = fail_first, []

    def put_record_batch(self, DeliveryStreamName, Records):
        out, failed = [], 0
        for r in Records:
            if self.fail_first:
                self.fail_first -= 1
                failed += 1
                out.append({"ErrorCode": "ServiceUnavailableException"})
            else:
                self.got.append(json.loads(r["Data"]))
                out.append({"RecordId": "x"})
        return {"FailedPutCount": failed, "RequestResponses": out}


def test_failed_records_are_retried():
    fh = Firehose(fail_first=2)
    out = handler({"Records": [record("INSERT", str(i), new=IMAGE) for i in range(3)]}, None, firehose=fh,
                  stream="s", clock=lambda: 1.0)
    assert out == {"batchItemFailures": []}
    assert sorted(r["seq"][-1] for r in fh.got) == ["0", "1", "2"]


def test_lasting_failure_reports_the_first_failed_record():
    fh = Firehose(fail_first=10**6)
    out = handler({"Records": [record("INSERT", s, new=IMAGE) for s in ("10", "9", "11")]}, None, firehose=fh,
                  stream="s", clock=lambda: 1.0)
    assert out == {"batchItemFailures": [{"itemIdentifier": "9"}]}
