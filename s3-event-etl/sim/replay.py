"""Replay a simulated data set into a system, at a chosen speed, under a run id.

    uv run python sim/replay.py SIM01 --speed 10 --run-id 20261123T0900-SIM01 --to kinesis [--target <stream>]
    uv run python sim/replay.py SIM01 --speed 0 --run-id r3 --to s3 [--slot-min 1] [--shift-to now]
    uv run python sim/replay.py SIM01 --speed 0 --run-id r1 --to local [--target rides.pipeline:handle]

Each delivery of `sim/data/<id>/trip-events/dt=<day>/hour=<hour>/deliveries.jsonl` (the files in the order
received) is sent (its received_time minus the first received_time) / speed after the start; speed 0 sends as fast
as the target takes them. The run id goes in front of every `event_id`, `trip_id` and `delivery_id` (`r1-t-0001`), so
a second replay into the same system is not taken for repeats of the first. `--shift-to now` moves every time by one offset, so the first received_time is the start of the replay.

Targets (`--target` names it; the default is in brackets):

    kinesis  PutRecords calls of up to 500 records, keyed by trip_id; refused records retried   [<PREFIX>-trip-events]
    sqs      SendMessageBatch calls of up to 10; refused entries retried             [queue <PREFIX>-trip-events]
    url      SigV4-signed POSTs of {"deliveries": [...]} up to 100, to a function URL (AWS_IAM)       [required]
    s3       one JSON Lines object per slot of --slot-min minutes, all of a slot's deliveries at once, at
             raw/trip-events/dt=<UTC day>/<slot start>-<run id>.jsonl, when the slot's last delivery is due
                                                                                        [<PREFIX>-lake-<account>]
    local    calls a function in this process with each batch of up to 500 deliveries   [rides.pipeline:handle]

Every send goes to `evidence/<run-id>/sent.jsonl`, one line per delivery: delivery_id, event_id (both with the run
id in front), sent_time, target, result (`ok`, or the error of a send that failed for good). Data latency is
measured from sent_time, not from the simulated received_time.

This file is a shared helper (tools/shared_helpers.toml): copy it unchanged into `sim/replay.py`. It needs boto3
and, to read the data files, the models of `src/rides/events.py`; senders and `replay()` need only boto3, so a
Lambda function can carry this file alone.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REGION = "ca-central-1"
ROOT = Path(__file__).resolve().parent.parent
MAX_ATTEMPTS = 5          # refused records are sent again at most this many times in all (REL05-BP03)
TARGETS = ("kinesis", "sqs", "url", "s3", "local")


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def prefixed(delivery: dict, run_id: str) -> dict:
    """The delivery as this run sends it: the run id in front of its three ids."""
    out = dict(delivery)
    for key in ("event_id", "trip_id", "delivery_id"):
        out[key] = f"{run_id}-{delivery[key]}"
    return out


def unprefixed(value: str, run_id: str) -> str:
    """An id with this run's id taken off, to compare with the answer from _truth/events.jsonl."""
    return value.removeprefix(f"{run_id}-")


def shifted(deliveries: list[dict], to: datetime) -> list[dict]:
    """Every time moved by one offset, so the first received_time is `to`."""
    first = min(parse(d["received_time"]) for d in deliveries)
    offset = to - first
    return [{**d, "event_time": iso(parse(d["event_time"]) + offset),
             "received_time": iso(parse(d["received_time"]) + offset)} for d in deliveries]


def planned(deliveries: list[dict], speed: float) -> list[tuple[float, dict]]:
    """(seconds after the start, delivery), in the order received; speed 0 plans every send at 0."""
    ordered = sorted(deliveries, key=lambda d: (d["received_time"], d["delivery_id"]))
    if not ordered:
        return []
    first = parse(ordered[0]["received_time"])
    return [((parse(d["received_time"]) - first).total_seconds() / speed if speed else 0.0, d) for d in ordered]


def _backoff(attempt: int, sleep) -> None:
    # Backoff with jitter (REL05-BP03): the limit doubles (0.1, 0.2, 0.4, 0.8 s), the wait is a random part of it.
    sleep(random.uniform(0, 0.1 * 2 ** attempt))


class KinesisSender:
    limit = 500                       # PutRecords takes at most 500 records a call

    def __init__(self, client, stream: str, sleep=time.sleep):
        self.client, self.stream, self.sleep = client, stream, sleep
        self.name = f"kinesis:{stream}"
        self.calls: list[int] = []    # records per PutRecords call, retries included

    def send(self, batch: list[dict]) -> list[str]:
        results = ["ok"] * len(batch)
        pending = list(range(len(batch)))
        for attempt in range(MAX_ATTEMPTS):
            records = [{"Data": json.dumps(batch[i], sort_keys=True).encode(), "PartitionKey": batch[i]["trip_id"]}
                       for i in pending]
            self.calls.append(len(records))
            resp = self.client.put_records(StreamName=self.stream, Records=records)
            failed = {i: f"{r['ErrorCode']}: {r.get('ErrorMessage', '')}"
                      for i, r in zip(pending, resp["Records"]) if r.get("ErrorCode")}
            for i in pending:
                results[i] = failed.get(i, "ok")
            pending = [i for i in pending if i in failed]
            if not pending:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                _backoff(attempt, self.sleep)
        return results


class SqsSender:
    limit = 10                        # SendMessageBatch takes at most 10 entries

    def __init__(self, client, queue: str, sleep=time.sleep):
        self.client, self.sleep = client, sleep
        self.url = queue if queue.startswith("https://") else client.get_queue_url(QueueName=queue)["QueueUrl"]
        self.name = f"sqs:{self.url.rsplit('/', 1)[-1]}"
        self.calls: list[int] = []

    def send(self, batch: list[dict]) -> list[str]:
        results = ["ok"] * len(batch)
        pending = list(range(len(batch)))
        for attempt in range(MAX_ATTEMPTS):
            entries = [{"Id": str(i), "MessageBody": json.dumps(batch[i], sort_keys=True)} for i in pending]
            self.calls.append(len(entries))
            resp = self.client.send_message_batch(QueueUrl=self.url, Entries=entries)
            failed = {int(f["Id"]): f"{f['Code']}: {f.get('Message', '')}" for f in resp.get("Failed", [])}
            for i in pending:
                results[i] = failed.get(i, "ok")
            pending = [i for i in pending if i in failed]
            if not pending:
                break
            if attempt + 1 < MAX_ATTEMPTS:
                _backoff(attempt, self.sleep)
        return results


class UrlSender:
    limit = 100

    def __init__(self, session, url: str, timeout: float = 30):
        self.session, self.url, self.timeout = session, url.rstrip("/"), timeout
        self.name = f"url:{self.url.split('//', 1)[-1].split('.', 1)[0]}"
        self.calls: list[int] = []

    def send(self, batch: list[dict]) -> list[str]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        data = json.dumps({"deliveries": batch}, sort_keys=True).encode()
        req = AWSRequest(method="POST", url=self.url, data=data, headers={"content-type": "application/json"})
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "lambda", REGION).add_auth(req)
        self.calls.append(len(batch))
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, data=data, method="POST",
                                                               headers=dict(req.headers)), timeout=self.timeout):
                return ["ok"] * len(batch)
        except Exception as err:      # an HTTP error or a timeout fails the whole POST
            return [f"{type(err).__name__}: {err}"] * len(batch)


class S3Sender:
    """One object per slot; `send` gets a whole slot."""
    limit = None

    def __init__(self, client, bucket: str, run_id: str, slot_min: int = 1):
        self.client, self.bucket, self.run_id, self.slot_min = client, bucket, run_id, slot_min
        self.name = f"s3:{bucket}"
        self.calls: list[int] = []
        self.keys: list[str] = []

    def slot_of(self, delivery: dict) -> datetime:
        t = parse(delivery["received_time"])
        minute = (t.hour * 60 + t.minute) // self.slot_min * self.slot_min
        return t.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)

    def key(self, slot: datetime) -> str:
        return f"raw/trip-events/dt={slot:%Y-%m-%d}/{slot:%Y%m%dT%H%MZ}-{self.run_id}.jsonl"

    def send(self, batch: list[dict]) -> list[str]:
        key = self.key(self.slot_of(batch[0]))
        body = "".join(json.dumps(d, sort_keys=True) + "\n" for d in batch).encode()
        self.calls.append(len(batch))
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/x-ndjson")
        except Exception as err:      # botocore's ClientError and connection errors
            return [f"{type(err).__name__}: {err}"] * len(batch)
        self.keys.append(key)
        return ["ok"] * len(batch)


class LocalSender:
    limit = 500

    def __init__(self, handler, name: str = "local"):
        self.handler, self.name = handler, f"local:{name}"
        self.calls: list[int] = []

    def send(self, batch: list[dict]) -> list[str]:
        self.calls.append(len(batch))
        try:
            self.handler(batch)
        except Exception as err:      # the team's handler failed: every delivery of the batch is reported
            return [f"{type(err).__name__}: {err}"] * len(batch)
        return ["ok"] * len(batch)


def load_handler(spec: str):
    """`module:function`, for example rides.pipeline:handle."""
    module, _, func = spec.partition(":")
    return getattr(importlib.import_module(module), func or "handle")


def make_sender(to: str, target: str | None = None, *, run_id: str = "", slot_min: int = 1, session=None):
    """The sender for a target kind; `target` names the stream, queue, URL, bucket or `module:function`."""
    if to not in TARGETS:
        raise ValueError(f"--to must be one of {TARGETS}")
    pre = os.environ.get("PREFIX") or "recipe"
    if to == "local":
        spec = target or "rides.pipeline:handle"
        return LocalSender(load_handler(spec), spec)
    if session is None:
        import boto3
        session = boto3.session.Session(region_name=REGION)
    if to == "kinesis":
        return KinesisSender(session.client("kinesis"), target or f"{pre}-trip-events")
    if to == "sqs":
        return SqsSender(session.client("sqs"), target or f"{pre}-trip-events")
    if to == "url":
        if not target:
            raise ValueError("--to url needs --target <function URL>")
        return UrlSender(session, target)
    bucket = target or f"{pre}-lake-{session.client('sts').get_caller_identity()['Account']}"
    return S3Sender(session.client("s3"), bucket, run_id, slot_min)


def _slots(plan: list[tuple[float, dict]], sender: "S3Sender") -> list[tuple[float, list[dict]]]:
    """The s3 target: (due, deliveries) per slot; a slot is sent when its last delivery is due."""
    slots: dict[datetime, list] = {}
    for due, d in plan:
        slots.setdefault(sender.slot_of(d), []).append((due, d))
    return [(items[-1][0], [d for _, d in items]) for _, items in sorted(slots.items())]


def replay(deliveries: list[dict], sender, run_id: str, speed: float = 0.0, *, evidence: Path | None = None,
           shift_to: datetime | None = None, clock=time.monotonic, wall=lambda: datetime.now(timezone.utc),
           sleep=time.sleep) -> dict:
    """Send the deliveries (plain dicts, as in deliveries.jsonl); write sent.jsonl; return a summary.

    A per-delivery target waits for the next delivery's time, then sends it together with every later delivery
    that is due by then, up to the target's limit per call; so a slow call does not push the rest later and later.
    `clock`, `wall` and `sleep` are passed in so a test can play an hour in no time."""
    if shift_to is not None:
        deliveries = shifted(deliveries, shift_to)
    plan = planned([prefixed(d, run_id) for d in deliveries], speed)
    lines, late, start = [], 0.0, clock()

    def send(batch: list[dict], due: float) -> None:
        nonlocal late
        sent_at = wall()
        late = max(late, clock() - start - due)
        for d, result in zip(batch, sender.send(batch)):
            lines.append({"delivery_id": d["delivery_id"], "event_id": d["event_id"], "sent_time": iso(sent_at),
                          "target": sender.name, "result": result})

    def wait_for(due: float) -> None:
        pause = start + due - clock()
        if speed and pause > 0:
            sleep(pause)

    if isinstance(sender, S3Sender):
        for due, batch in _slots(plan, sender):
            wait_for(due)
            send(batch, due)
    else:
        i = 0
        while i < len(plan):
            wait_for(plan[i][0])
            now = clock() - start
            j = i + 1
            while j < len(plan) and j - i < sender.limit and (plan[j][0] <= now or not speed):
                j += 1
            send([d for _, d in plan[i:j]], plan[i][0])
            i = j
    failed = sum(1 for line in lines if line["result"] != "ok")
    summary = {"run_id": run_id, "target": sender.name, "speed": speed, "sent": len(lines) - failed,
               "failed": failed, "calls": len(sender.calls), "seconds": round(clock() - start, 3),
               "late_s": round(late, 3)}
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=True)
        with open(evidence / "sent.jsonl", "w") as f:
            for line in lines:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def load_deliveries(data_set: str, data_dir: Path | None = None) -> list[dict]:
    """The delivery files of a data set (<source>/dt=<day>/hour=<hour>/deliveries.jsonl, read in that order), each
    line validated against its model, as plain dicts. _truth/ is not read: the system never sees it."""
    from rides.events import DELIVERY, read_jsonl
    folder = (data_dir or ROOT / "sim" / "data") / data_set
    paths = sorted(folder.glob("*/dt=*/hour=*/deliveries.jsonl"))
    if not paths:
        raise FileNotFoundError(f"{folder}: no <source>/dt=<day>/hour=<hour>/deliveries.jsonl (run sim/generate.py "
                                f"{data_set} first)")
    return [d.model_dump(mode="json") for path in paths for d in read_jsonl(path, DELIVERY)]


def _record_s3_replay(run_id: str) -> None:
    """A ledger row `s3-replay` for this run id, so `scripts/lake.py down` deletes its slot files (and no other
    recipe's); skipped where the folder has no scripts/tags.py (a Lambda package carries this file alone)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from tags import record
    except ImportError:
        return
    record("s3-replay", run_id, "created", script="sim/replay.py --to s3", clean="scripts/lake.py down")


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("data_set", help="SIM01, SIM03, SIM04 ... (sim/data/<id>/trip-events/dt=*/hour=*/deliveries.jsonl)")
    p.add_argument("--speed", type=float, default=0.0, help="k times the simulated rate; 0 = as fast as it goes")
    p.add_argument("--run-id", required=True)
    p.add_argument("--to", required=True, choices=TARGETS)
    p.add_argument("--target", help="stream, queue, URL, bucket or module:function")
    p.add_argument("--slot-min", type=int, default=1, help="minutes per object for --to s3")
    p.add_argument("--shift-to", choices=["now"], help="move the times so the first delivery is now")
    p.add_argument("--data-dir", type=Path, default=ROOT / "sim" / "data")
    p.add_argument("--evidence", type=Path, default=ROOT / "evidence")
    args = p.parse_args(argv)
    if args.speed < 0:
        p.error("--speed must be 0 or more")
    deliveries = load_deliveries(args.data_set, args.data_dir)
    sender = make_sender(args.to, args.target, run_id=args.run_id, slot_min=args.slot_min)
    if args.to == "s3":
        _record_s3_replay(args.run_id)
    folder = args.evidence / args.run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "command.txt").write_text(
        f"{' '.join(['sim/replay.py'] + (argv if argv is not None else sys.argv[1:]))}\ncommit {_commit()}\n")
    now = datetime.now(timezone.utc) if args.shift_to == "now" else None
    summary = replay(deliveries, sender, args.run_id, args.speed, evidence=folder, shift_to=now)
    print(f"replay {args.data_set} run {args.run_id} to {summary['target']}: {summary['sent']} sent, "
          f"{summary['failed']} failed, {summary['calls']} calls, {summary['seconds']} s, "
          f"latest send {summary['late_s']} s after its planned time -> {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}/sent.jsonl")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
