"""gw-admin: the command line for keys, spend and the kill switch.

It is for the person who runs the gateway. It runs with that person's own AWS credentials (the boto3 default chain) and
talks to DynamoDB, SNS and Lambda directly. It never calls the gateway. A key
secret is printed to the terminal once, at issue, and stored nowhere. `import` adds keys that another system made:
it reads their SHA-256 hex from a file and never sees the keys.
"""

import argparse
import base64
import json
import os
import re
import secrets as pysecrets
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from . import aws, pricing, settings
from .auth import hash_key

B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DUMP_VERSION = 1


def amount_to_micro(amount: str) -> int:
    try:
        d = Decimal(amount)
    except InvalidOperation:
        sys.exit(f"budget {amount!r} is not a number")
    if not d.is_finite() or d < 0:
        sys.exit(f"budget {amount!r} must be a number of 0 or more")
    return int(d * 1_000_000)


def new_key() -> tuple[str, str]:
    """Return (key_id, key). The key is key_id, a dash and 43 url-safe characters."""
    key_id = settings.KEY_PREFIX + "-" + "".join(pysecrets.choice(B32) for _ in range(8))
    return key_id, f"{key_id}-{pysecrets.token_urlsafe(32)}"


def _expiry(date: str) -> int:
    try:
        d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        sys.exit(f"--expires {date!r} is not a date in the form YYYY-MM-DD")
    end = int(d.timestamp()) + 86_399  # end of that day, UTC
    if end <= time.time():
        sys.exit(f"--expires {date} is in the past")
    return end


def _all_keys() -> list[dict]:
    table = aws.keys_table()
    items, kw = [], {}
    while True:
        resp = table.scan(**kw)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _by_id(key_id: str) -> dict:
    resp = aws.keys_table().query(IndexName="by_key_id", KeyConditionExpression=Key("key_id").eq(key_id))
    if not resp["Items"]:
        sys.exit(f"no key with id {key_id}")
    return aws.keys_table().get_item(Key={"key_hash": resp["Items"][0]["key_hash"]}, ConsistentRead=True)["Item"]


def _key_terms(a) -> tuple[int | None, set[str], str]:
    """Budget in micro-units, allowed models and last day, from the flags `issue` and `import` share."""
    budget = settings.DEFAULT_BUDGET_MICRO[a.kind]
    if a.budget is not None:
        budget = amount_to_micro(a.budget)
        if budget > 50_000_000 and not a.yes:
            sys.exit(f"budget above 50 {settings.CURRENCY} needs --yes")
    if a.models is None:
        models = set(pricing.ALL_MODELS)
    else:
        models = {m.strip() for m in a.models.split(",") if m.strip()}
        if not models:
            sys.exit("--models is empty. Leave it out for every model")
    unknown = models - set(pricing.PRICES)
    if unknown:
        sys.exit(f"unknown models: {', '.join(sorted(unknown))}")
    default_end = datetime.now(UTC) + timedelta(days=settings.DEFAULT_EXPIRES_DAYS)
    return budget, models, a.expires or default_end.strftime("%Y-%m-%d")


def _key_item(a, key_hash: str, key_id: str, owner: str, team: str | None,
              budget: int | None, models: set[str], expires_at: int) -> dict:
    item = {
        "key_hash": key_hash, "key_id": key_id, "kind": a.kind, "owner": owner,
        "spent_micro": 0, "tokens_in": 0, "tokens_out": 0, "requests": 0,
        "expires_at": expires_at, "allowed_models": models, "state": "active",
        "max_rpm": getattr(a, "rpm", None) or settings.DEFAULT_MAX_RPM, "max_tokens_cap": settings.DEFAULT_MAX_TOKENS_CAP,
        "max_body_bytes": settings.DEFAULT_MAX_BODY_BYTES, "created_at": int(time.time()),
        "created_by": a.created_by,
    }
    if team:
        item["team"] = team
    if budget is not None:
        item["budget_micro"] = budget
    if a.note:
        item["note"] = a.note
    return item


def _budget_txt(budget: int | None) -> str:
    return "unlimited" if budget is None else f"{pricing.micro_to_amount(budget)} {settings.CURRENCY}"


def cmd_issue(a) -> None:
    budget, models, expires = _key_terms(a)
    key_id, key = new_key()
    item = _key_item(a, hash_key(key), key_id, a.owner, a.team, budget, models, _expiry(expires))
    aws.keys_table().put_item(Item=item, ConditionExpression="attribute_not_exists(key_hash)")
    print(f"issued {key_id} for {a.owner} ({a.kind}), budget {_budget_txt(budget)}, expires {expires}")
    print("key (shown once, not stored):")
    print(key)


def imported_key_id(key_hash: str) -> str:
    """Key id of an imported key: the prefix and the first 40 bits of its hash, as 8 base32 characters."""
    return settings.IMPORTED_KEY_PREFIX + "-" + base64.b32encode(bytes.fromhex(key_hash)[:5]).decode()


def cmd_import(a) -> None:
    try:
        hashes = json.loads(Path(a.file).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {a.file}: {e}")
    if not isinstance(hashes, dict) or not hashes or not all(
            isinstance(o, str) and o and HASH_RE.match(h) for h, o in hashes.items()):
        sys.exit(f"{a.file} must hold one JSON object: {{SHA-256 hex of a key: owner}}")
    budget, models, expires = _key_terms(a)
    expires_at = _expiry(expires)
    stored = {k["key_hash"]: k for k in _all_keys()}
    # Two hashes with the same first 40 bits would share one key id, and with it one rate limit and one usage row.
    ids = {k["key_id"]: h for h, k in stored.items()}
    for h in hashes:
        if ids.setdefault(imported_key_id(h), h) != h:
            sys.exit(f"key id {imported_key_id(h)} would stand for two keys. Nothing was imported")
    added = 0
    for h, owner in hashes.items():
        if h in stored:
            continue
        team = owner.split(a.team_sep, 1)[0] if a.team_sep and a.team_sep in owner else None
        item = _key_item(a, h, imported_key_id(h), owner, team, budget, models, expires_at)
        aws.keys_table().put_item(Item=item, ConditionExpression="attribute_not_exists(key_hash)")
        added += 1
    revoked = 0
    if a.revoke_missing:
        prefix = settings.IMPORTED_KEY_PREFIX + "-"
        for h, k in stored.items():
            if k["key_id"].startswith(prefix) and h not in hashes and k.get("state") == "active":
                aws.keys_table().update_item(
                    Key={"key_hash": h}, UpdateExpression="SET #s = :r, revoked_at = :now, revoke_reason = :why",
                    ExpressionAttributeNames={"#s": "state"},
                    ExpressionAttributeValues={":r": "revoked", ":now": int(time.time()),
                                               ":why": "not in the imported file"})
                revoked += 1
    total = "unlimited" if budget is None else f"{pricing.micro_to_amount(budget * added)} {settings.CURRENCY}"
    print(f"imported {added} keys ({a.kind}), budget {_budget_txt(budget)} each, {total} in all, expires {expires}")
    print(f"{len(hashes) - added} keys of the file were already stored and are unchanged; {revoked} revoked")


def cmd_revoke(a) -> None:
    item = _by_id(a.key_id)
    expr, vals = "SET #s = :r, revoked_at = :now", {":r": "revoked", ":now": int(time.time())}
    if a.reason:
        expr += ", revoke_reason = :why"
        vals[":why"] = a.reason
    aws.keys_table().update_item(Key={"key_hash": item["key_hash"]}, UpdateExpression=expr,
                                 ExpressionAttributeNames={"#s": "state"}, ExpressionAttributeValues=vals)
    print(f"revoked {a.key_id}")


def cmd_list(a) -> None:
    rows = _all_keys()
    rows = [r for r in rows if (not a.kind or r.get("kind") == a.kind)
            and (not a.owner or r.get("owner") == a.owner) and (not a.state or r.get("state") == a.state)]
    for r in sorted(rows, key=lambda r: (r.get("kind", ""), r.get("owner", ""))):
        budget = r.get("budget_micro")
        spent = pricing.micro_to_amount(int(r.get("spent_micro", 0)))
        btxt = "unlimited" if budget is None else pricing.micro_to_amount(int(budget))
        exp = datetime.fromtimestamp(int(r.get("expires_at", 0)), UTC).strftime("%Y-%m-%d")
        print(f"{r['key_id']}  {r.get('owner', '')}  {r.get('kind', '')}  {r.get('team', '-')}  "
              f"{spent} / {btxt} {settings.CURRENCY}  {exp}  {r.get('state', '')}")


def _usage_rows() -> list[dict]:
    table = aws.usage_table()
    items, kw = [], {}
    while True:
        resp = table.scan(**kw)
        items += [i for i in resp["Items"] if not str(i["sk"]).startswith("min#")]
        if "LastEvaluatedKey" not in resp:
            return items
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _team_of(k: dict) -> str:
    return str(k.get("team") or (k.get("owner") if k.get("kind") == "team" else "-"))


def cmd_spend(a) -> None:
    keys = {k["key_id"]: k for k in _all_keys()}
    if a.reconcile:
        return _reconcile(keys)
    rows = _usage_rows()
    if a.since:
        rows = [r for r in rows if str(r["sk"]).split("#")[0] >= a.since]
    if a.key:
        rows = [r for r in rows if r["key_id"] == a.key]
    if a.team:
        rows = [r for r in rows if _team_of(keys.get(r["key_id"], {})) == a.team]
    detail: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        group = r["key_id"] if a.by == "key" else _team_of(keys.get(r["key_id"], {}))
        day, model = str(r["sk"]).split("#", 1)
        d = detail[(group, day, model)]
        for i, f in enumerate(("requests", "tokens_in", "tokens_out", "cost_micro")):
            d[i] += int(r.get(f, 0))
        totals[group] += int(r.get("cost_micro", 0))
    for (group, day, model), (n, tin, tout, c) in sorted(detail.items()):
        print(f"{group}  {day}  {model}  requests {n}  in {tin}  out {tout}  "
              f"{pricing.micro_to_amount(c)} {settings.CURRENCY}")
    for group, c in sorted(totals.items()):
        print(f"total {group}  {pricing.micro_to_amount(c)} {settings.CURRENCY}")


def _reconcile(keys: dict) -> None:
    """Set spent_micro to the sum of the usage rows. Run it when no request is in flight."""
    sums: dict[str, int] = defaultdict(int)
    for r in _usage_rows():
        sums[r["key_id"]] += int(r.get("cost_micro", 0))
    for key_id, k in sorted(keys.items()):
        want, have = sums.get(key_id, 0), int(k.get("spent_micro", 0))
        if want != have:
            aws.keys_table().update_item(Key={"key_hash": k["key_hash"]}, UpdateExpression="SET spent_micro = :s",
                                         ExpressionAttributeValues={":s": want})
            print(f"{key_id}  spent {pricing.micro_to_amount(have)} -> {pricing.micro_to_amount(want)} "
                  f"{settings.CURRENCY}")
    print("reconcile done")


def cmd_budget_set(a) -> None:
    new = amount_to_micro(a.amount)
    if new > 50_000_000 and not a.yes:
        sys.exit(f"budget above 50 {settings.CURRENCY} needs --yes")
    item = _by_id(a.key_id)
    pct = int(item.get("spent_micro", 0)) * 100 // new if new else 100
    stale = {t for t in item.get("notified", set()) if int(t) > pct}
    expr, vals = "SET budget_micro = :b", {":b": new}
    if stale:
        expr += " DELETE notified :stale"
        vals[":stale"] = stale
    aws.keys_table().update_item(Key={"key_hash": item["key_hash"]}, UpdateExpression=expr,
                                 ExpressionAttributeValues=vals)
    print(f"{a.key_id} budget {pricing.micro_to_amount(new)} {settings.CURRENCY}, now at {pct}%")


def cmd_rate_set(a) -> None:
    """Requests a minute for one key, or for every active key when the id is `all`."""
    if a.rpm < 1:
        sys.exit("the limit is at least 1 request a minute")
    items = [r for r in _all_keys() if r.get("state") == "active"] if a.key_id == "all" else [_by_id(a.key_id)]
    for item in items:
        aws.keys_table().update_item(Key={"key_hash": item["key_hash"]}, UpdateExpression="SET max_rpm = :r",
                                     ExpressionAttributeValues={":r": a.rpm})
    print(f"{len(items)} keys now have a limit of {a.rpm} requests a minute")



def cmd_tokens_set(a) -> None:
    """Largest max_tokens one key may ask for, or every active key's when the id is `all`."""
    if a.cap < 1:
        sys.exit("the cap is at least 1 token")
    items = [r for r in _all_keys() if r.get("state") == "active"] if a.key_id == "all" else [_by_id(a.key_id)]
    for item in items:
        aws.keys_table().update_item(Key={"key_hash": item["key_hash"]}, UpdateExpression="SET max_tokens_cap = :c",
                                     ExpressionAttributeValues={":c": a.cap})
    print(f"{len(items)} keys now have a max_tokens cap of {a.cap}")

def cmd_dump(a) -> None:
    """Both tables to one file, for a copy outside the account. The per-minute rate counters are left out.

    Values are written in DynamoDB's type notation ({"N": "2000000"}, {"SS": [...]}), so `load` puts back
    the same numbers and string sets. The file holds key hashes and owners: mode 600, and not for a repository.
    """
    ser = TypeSerializer()
    doc = {"version": DUMP_VERSION, "dumped_at": int(time.time())}
    for name, rows in (("keys", _all_keys()), ("usage", _usage_rows())):
        doc[name] = [{k: ser.serialize(v) for k, v in row.items()} for row in rows]
    tmp = f"{a.file}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(doc, f)
    os.replace(tmp, a.file)  # a dump that stops halfway leaves the previous file whole
    print(f"{len(doc['keys'])} keys and {len(doc['usage'])} usage rows written to {a.file}")


def _pages(call, **kw) -> list[dict]:
    items = []
    while True:
        resp = call(**kw)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def cmd_texts(a) -> None:
    """Rows of the text log to a file, one JSON object a line. Only counts are printed.

    The file holds what people sent and what the models answered: mode 600, and not for a repository.
    """
    if not settings.load().table_texts:
        sys.exit("set TABLE_TEXTS to the stack output TextsTableName. Without it the text log is off")
    table = aws.texts_table()
    if a.key:
        ids = [a.key]
    elif a.team:
        ids = sorted(k["key_id"] for k in _all_keys() if _team_of(k) == a.team)
    else:
        ids = None  # every key: a scan of the whole table
    if ids is None:
        rows = _pages(table.scan)
    else:
        rows = []
        for key_id in ids:
            cond = Key("key_id").eq(key_id)
            rows += _pages(table.query, KeyConditionExpression=cond & Key("sk").eq(a.request) if a.request else cond)
    if a.request:
        rows = [r for r in rows if r["sk"] == a.request]
    if a.since:
        rows = [r for r in rows if str(r.get("ts", "")) >= a.since]
    rows.sort(key=lambda r: str(r.get("ts", "")))
    tmp = f"{a.file}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=lambda v: int(v) if isinstance(v, Decimal) else str(v)))
            f.write("\n")
    os.replace(tmp, a.file)
    by_source: dict[str, int] = defaultdict(int)
    for r in rows:
        by_source[str(r.get("source", "-"))] += 1
    counts = ", ".join(f"{n} {source}" for source, n in sorted(by_source.items())) or "none"
    print(f"{len(rows)} rows of {len({r['key_id'] for r in rows})} keys written to {a.file} ({counts})")


def cmd_load(a) -> None:
    """Put the rows of a dump back. A row in the file replaces the stored row with the same key; other rows stay."""
    try:
        doc = json.loads(Path(a.file).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {a.file}: {e}")
    if not isinstance(doc, dict) or doc.get("version") != DUMP_VERSION or not {"keys", "usage"} <= doc.keys():
        sys.exit(f"{a.file} is not a gw-admin dump of version {DUMP_VERSION}")
    if not a.overwrite and aws.keys_table().scan(Limit=1)["Items"]:
        sys.exit("the keys table already holds keys. Their spend would go back to the dump's numbers: "
                 "close the URL with `gw-admin kill` and pass --overwrite if that is what you want")
    de = TypeDeserializer()
    for table, rows in ((aws.keys_table(), doc["keys"]), (aws.usage_table(), doc["usage"])):
        # One PutItem per row, not a batch writer: BatchWriteItem is a separate IAM action, and the deploy policy
        # (infra/iam/deploy-admin-policy.json, AdminKeysAndUsage) allows gw-admin PutItem only.
        for row in rows:
            table.put_item(Item={k: de.deserialize(v) for k, v in row.items()})
    when = datetime.fromtimestamp(int(doc.get("dumped_at", 0)), UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{len(doc['keys'])} keys and {len(doc['usage'])} usage rows loaded from the dump of {when}")


def cmd_notify(a) -> None:
    topic = settings.load().topic_arn
    if not topic:
        sys.exit("set TOPIC_ARN to the stack output AlertsTopicArn")
    aws.sns().subscribe(TopicArn=topic, Protocol="email", Endpoint=a.email,
                        Attributes={"FilterPolicy": json.dumps({"owner": [a.owner]})})
    print(f"subscribed {a.email} to notices for {a.owner}. SNS sends a confirmation email.")


def cmd_kill(a) -> None:
    fn = settings.load().function_name
    boto3.client("lambda", region_name=settings.load().region).put_function_concurrency(
        FunctionName=fn, ReservedConcurrentExecutions=0)
    print(f"{fn} reserved concurrency 0. The URL refuses every request.")


def cmd_open(a) -> None:
    fn = settings.load().function_name
    boto3.client("lambda", region_name=settings.load().region).put_function_concurrency(
        FunctionName=fn, ReservedConcurrentExecutions=a.concurrency)
    print(f"{fn} reserved concurrency {a.concurrency}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gw-admin", description="Manage gateway keys and spend.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("issue", help="make a key and print it once")
    s.add_argument("--owner", required=True)
    s.add_argument("--kind", required=True, choices=["person", "team", "admin"])
    s.add_argument("--team")
    s.add_argument("--budget")
    s.add_argument("--expires", help="last day the key works, YYYY-MM-DD; default DEFAULT_EXPIRES_DAYS from today")
    s.add_argument("--models", help="comma separated model ids, default all")
    s.add_argument("--note")
    s.add_argument("--rpm", type=int, help="requests a minute, default DEFAULT_MAX_RPM")
    s.add_argument("--created-by", default="admin")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_issue)
    s = sub.add_parser("import", help="add keys that another system made, by their SHA-256 hex")
    s.add_argument("file", help="JSON object {SHA-256 hex of a key: owner}")
    s.add_argument("--kind", default="person", choices=["person", "team", "admin"])
    s.add_argument("--team-sep", help="an owner written team<sep>person gets the part before <sep> as its team")
    s.add_argument("--budget", help="budget of each new key")
    s.add_argument("--expires", help="last day the keys work, YYYY-MM-DD; default DEFAULT_EXPIRES_DAYS from today")
    s.add_argument("--models", help="comma separated model ids, default all")
    s.add_argument("--note")
    s.add_argument("--rpm", type=int, help="requests a minute of each new key, default DEFAULT_MAX_RPM")
    s.add_argument("--created-by", default="import")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--revoke-missing", action="store_true",
                   help="revoke imported keys whose hash is no longer in the file")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("revoke", help="revoke a key")
    s.add_argument("key_id")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("list", help="one line per key")
    s.add_argument("--kind")
    s.add_argument("--owner")
    s.add_argument("--state")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("spend", help="spend per key or per team")
    s.add_argument("--by", choices=["key", "team"], default="key")
    s.add_argument("--since")
    s.add_argument("--key")
    s.add_argument("--team")
    s.add_argument("--reconcile", action="store_true")
    s.set_defaults(fn=cmd_spend)
    s = sub.add_parser("budget", help="change a budget")
    bsub = s.add_subparsers(dest="budget_cmd", required=True)
    b = bsub.add_parser("set")
    b.add_argument("key_id")
    b.add_argument("amount")
    b.add_argument("--yes", action="store_true")
    b.set_defaults(fn=cmd_budget_set)
    s = sub.add_parser("rate", help="change the requests a minute of a key")
    rsub = s.add_subparsers(dest="rate_cmd", required=True)
    r = rsub.add_parser("set")
    r.add_argument("key_id", help="a key id, or `all` for every active key")
    r.add_argument("rpm", type=int)
    r.set_defaults(fn=cmd_rate_set)
    s = sub.add_parser("tokens", help="change the largest max_tokens a key may ask for")
    tsub = s.add_subparsers(dest="tokens_cmd", required=True)
    t = tsub.add_parser("set")
    t.add_argument("key_id", help="a key id, or `all` for every active key")
    t.add_argument("cap", type=int)
    t.set_defaults(fn=cmd_tokens_set)
    s = sub.add_parser("dump", help="write both tables to a file, for a copy outside the account")
    s.add_argument("file")
    s.set_defaults(fn=cmd_dump)
    s = sub.add_parser("load", help="put the rows of a dump back")
    s.add_argument("file")
    s.add_argument("--overwrite", action="store_true", help="load although the keys table already holds keys")
    s.set_defaults(fn=cmd_load)
    s = sub.add_parser("texts", help="write rows of the text log to a file (needs TABLE_TEXTS)")
    s.add_argument("file")
    s.add_argument("--key", help="one key id")
    s.add_argument("--team", help="every key of one team")
    s.add_argument("--request", help="one request id")
    s.add_argument("--since", help="rows from this day on, YYYY-MM-DD (UTC)")
    s.set_defaults(fn=cmd_texts)
    s = sub.add_parser("notify", help="email one owner their own budget notices")
    s.add_argument("--owner", required=True)
    s.add_argument("--email", required=True)
    s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("kill", help="set reserved concurrency to 0")
    s.set_defaults(fn=cmd_kill)
    s = sub.add_parser("open", help="restore reserved concurrency")
    s.add_argument("--concurrency", type=int, default=200)
    s.set_defaults(fn=cmd_open)
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
