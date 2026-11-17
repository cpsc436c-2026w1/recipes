"""Main scenario: the table is the source of truth; a Lambda function on its stream keeps the index in step.

  uv run python -m main.run                    offline: moto table and stream, stand-in mapping and index
  uv run python -m main.run --target aws       your sandbox (main/infra.py creates what it needs)
  uv run python -m main.run --target aws --setup   create or update everything, write nothing to the table
                                               (step 1 of an index move: README, Adapt it)

Steps: the starting items reach the index; inserts, updates and deletes each reach it, timed from the table write
to the first search that shows them; a record the index refuses goes to the failure queue and the write after it
still arrives; the same batch given to the function twice leaves the same documents.
"""

from __future__ import annotations

import argparse
import statistics
import time

from main.config import Settings, load_items
from main.envs import AwsEnv, LocalEnv, stream_records
from main.infra import _decimals
from main.mappings import keyword_query

QUERY = "waterproof"
NEW = {
    "I09": {"id": "I09", "name": "Camp stove", "description": "Compact gas stove that boils a litre in four minutes",
            "category": "gear", "brand": "Cascade", "price_cad": 79.0, "in_stock": True},
    "I10": {"id": "I10", "name": "Dry bag", "description": "Roll-top waterproof bag, 20 litres", "category": "gear",
            "brand": "Fernwood", "price_cad": 29.0, "in_stock": True},
}
# (what, item id, attributes): one change at a time, each timed until a search shows it.
CHANGES = [
    ("INSERT", "I09", NEW["I09"]),
    ("INSERT", "I10", NEW["I10"]),
    ("MODIFY", "I04", {"name": "Headlamp HX-300",
                       "description": "Rechargeable headlamp, 600 lumens, red night mode, waterproof"}),
    ("MODIFY", "I02", {"in_stock": False}),
    ("MODIFY", "I09", {"description": "Compact gas stove with a wind screen, boils a litre in four minutes"}),
    ("REMOVE", "I08", None),
    ("REMOVE", "I10", None),
]
# A price the mapping cannot take (price_cad is a float field): OpenSearch refuses the document.
BAD = {"id": "I11", "name": "Gift card", "description": "Store credit", "category": "gift", "brand": "Northline",
       "price_cad": "free", "in_stock": True}
AFTER_BAD = {"id": "I12", "name": "Bear canister", "description": "Hard case that keeps food from bears",
             "category": "gear", "brand": "Cascade", "price_cad": 99.0, "in_stock": True}


def write(table, what: str, item_id: str, attrs: dict | None) -> None:
    if what == "INSERT":
        table.put_item(Item=_decimals(attrs))
    elif what == "MODIFY":
        table.update_item(Key={"id": item_id}, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
                          ExpressionAttributeNames={f"#{k}": k for k in attrs},
                          ExpressionAttributeValues={f":{k}": v for k, v in _decimals(attrs).items()})
    else:
        table.delete_item(Key={"id": item_id})


def shows(env, item_id: str, attrs: dict | None):
    """The hit for item_id when a search shows the change (attrs None: shows the delete), else None."""
    hits = env.search({"query": {"ids": {"values": [item_id]}}, "size": 1})
    if attrs is None:
        return {"_id": item_id, "_source": {}} if not hits else None
    if hits and all(hits[0]["_source"].get(k) == v for k, v in attrs.items() if k != "id"):
        return hits[0]
    return None


def wait_for(env, check, timeout_s: float = 300, every_s: float = 0.5):
    """(seconds until check() is true, its value). Offline the change is delivered first and there is no clock to
    read: seconds is None."""
    start = time.time()
    while True:
        env.deliver()
        value = check()
        if value:
            return (time.time() - start if env.live else None), value
        if not env.live or time.time() - start > timeout_s:
            raise TimeoutError(f"not searchable after {timeout_s:.0f} s")
        time.sleep(every_s)


def secs(x) -> str:
    return "on the next delivery" if x is None else f"{x:.1f} s"


def ranked(env, text: str) -> str:
    return ", ".join(f"{h['_id']} ({h['_score']:.2f})" for h in env.search(keyword_query(text)))


def spread(lags: list[float]) -> str:
    if len(lags) < 2:
        return "not timed offline"
    q1, med, q3 = statistics.quantiles(lags, n=4, method="inclusive")
    return f"median {med:.1f} s (IQR {q1:.1f} to {q3:.1f} s), max {max(lags):.1f} s, n = {len(lags)}"


class Lines(list):
    """The scenario's output lines; with echo, each line is also printed the moment it is known, so a run that stops
    half way still shows what it measured."""

    def __init__(self, echo: bool = False):
        super().__init__()
        self.echo = echo

    def append(self, line: str) -> None:
        super().append(line)
        if self.echo:
            print(line, flush=True)


def scenario(env, s: Settings, n_items: int, echo: bool = False) -> tuple[list[str], dict]:
    lines, lags = Lines(echo), []
    t, _ = wait_for(env, lambda: env.count() >= n_items, timeout_s=600, every_s=2)
    cold = "; the first requests to a new or idle collection wait for capacity" if env.live else ""
    lines.append(f"starting items: {env.count()} documents in {s.index}, {secs(t)} after the table writes{cold}")
    meta = env.mapping_meta()
    lines.append(f"index version recorded in the mapping: {meta['index_version']} (source table {meta['source_table']})")
    lines.append(f"search {QUERY!r}: {ranked(env, QUERY)}")

    for what, item_id, attrs in CHANGES:
        write(env.table, what, item_id, attrs)
        written = time.time()
        t, hit = wait_for(env, lambda i=item_id, a=attrs: shows(env, i, a))
        if t is not None:
            lags.append(t)
        sent = hit["_source"].get("indexed_at")
        to_fn = f"; the function sent it {sent - written:.1f} s after the write" if sent and env.live else ""
        state = "gone from search" if what == "REMOVE" else "searchable"
        lines.append(f"{what:<6} {item_id}: {state} {secs(t)}{to_fn}")
    lines.append(f"lag, table write to searchable: {spread(lags)}")
    lines.append(f"search {QUERY!r}: {ranked(env, QUERY)}")

    # One bad record, and a good write right behind it on the same shard.
    write(env.table, "INSERT", BAD["id"], BAD)
    write(env.table, "INSERT", AFTER_BAD["id"], AFTER_BAD)
    t, _ = wait_for(env, lambda: shows(env, AFTER_BAD["id"], AFTER_BAD), timeout_s=600)
    failed = []
    # Lambda spaces its retries out: on 2026-11-13 the refused record's 11 tries (1 + MAX_RETRIES) took 2 min 45 s
    # (output/main-aws.txt, function log), so wait up to 10 minutes for its failure message.
    deadline = time.time() + (600 if env.live else 0)
    while True:
        failed += env.failures(wait_s=10 if env.live else 0)
        if failed or time.time() > deadline:
            break
    keys = [k for m in failed for k in env.record_keys(m)]
    sizes = [m["DDBStreamBatchInfo"]["batchSize"] for m in failed]
    lines.append(f"bad record {BAD['id']} (price_cad 'free'): in the index {bool(shows(env, BAD['id'], {}))}; "
                 f"failure queue: {len(failed)} message(s), batch size {sizes}, item {keys}")
    lines.append(f"INSERT {AFTER_BAD['id']} written right after it: searchable {secs(t)} (behind the retries)")

    # Replay: the same records again, twice, as Lambda does when it retries a batch. The index's document count
    # trails a search by id (measured 2026-11-12), so first wait until it matches the table, then wait until the
    # replay itself is searchable (I04's indexed_at moves on) before counting again.
    in_table = env.table.scan(Select="COUNT")["Count"] - len(set(keys) | {BAD["id"]})   # the refused item stays out
    wait_for(env, lambda: env.count() == in_table, timeout_s=120, every_s=1)
    before = env.count()
    current = [env.table.get_item(Key={"id": i})["Item"] for i in ("I04", "I09")]
    current = [{k: float(v) if hasattr(v, "as_tuple") else v for k, v in it.items()} for it in current]
    old_sent = shows(env, "I04", {})["_source"]["indexed_at"]
    outcomes = [env.replay(stream_records(current)) for _ in range(2)]
    if env.live:
        wait_for(env, lambda: shows(env, "I04", {})["_source"]["indexed_at"] > old_sent)
        time.sleep(5)                                   # and give the count the same few seconds
    ids_ok = all(len(env.search({"query": {"ids": {"values": [i]}}})) == 1 for i in ("I04", "I09"))
    hit = shows(env, "I04", {k: v for k, v in current[0].items() if k != "id"})
    lines.append(f"replayed one batch (I04, I09) twice: {outcomes}; documents {before} before, {env.count()} after "
                 f"({in_table} items in the table that the index takes); one document each for I04 and I09: "
                 f"{ids_ok}; I04 unchanged: {hit is not None}")
    return lines, {"lags": lags, "failed_keys": keys, "count": env.count(), "after_bad": t}


def run_local(s: Settings) -> str:
    env = LocalEnv(s)
    try:
        lines, _ = scenario(env, s, len(load_items()))
    finally:
        env.close()
    return "[local: moto DynamoDB and Streams, stand-in event source mapping and index]\n" + "\n".join(lines)


def run_aws(s: Settings) -> str:
    """Prints each line as it is known (a run that stops half way keeps what it measured); returns ""."""
    from main.infra import MAX_OCU, MIN_OCU, create_all

    print(f"[aws {s.region}; function {s.function}]", flush=True)
    started = time.time()
    info = create_all(s, load_items())
    print(f"created in {time.time() - started:.0f} s: table, stream, failure queue, role, collection group "
          f"(min {MIN_OCU}, max {MAX_OCU} OCU), collection, index {s.index}, function, event source mapping", flush=True)
    scenario(AwsEnv(s, info), s, len(load_items()), echo=True)
    return ""


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--setup", action="store_true", help="aws: create or update, no scenario, no table writes")
    args = p.parse_args(argv)
    s = Settings()
    if args.setup:
        from main.infra import create_all

        if args.target != "aws":
            raise SystemExit("--setup is for --target aws")
        info = create_all(s, [])
        print(f"[aws {s.region}] function {s.function} now writes to {s.index}; index version recorded: {info['meta']}")
        return
    if args.target == "aws":
        run_aws(s)
    else:
        print(run_local(s))


if __name__ == "__main__":
    main()
