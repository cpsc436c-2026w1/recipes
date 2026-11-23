"""The shared lake bucket `<PREFIX>-lake-<account>` and the shared Athena workgroups.

    uv run python scripts/lake.py up                       # create the bucket if it is missing (else: borrowed)
    uv run python scripts/lake.py workgroup <PREFIX>-wg    # ensure_workgroup from the command line
    uv run python scripts/lake.py down                     # what it would delete; nothing is deleted
    uv run python scripts/lake.py down --yes               # delete it (the teardown runs this last)

`up` creates the bucket with the course tags in the create call, public access blocked, and a lifecycle rule that
deletes `athena-results/` after 7 days and ends incomplete multipart uploads after 1 day. Other recipes borrow it and
delete only their own prefixes.

`down` deletes under `raw/trip-events/` only the files this folder's ledger records as its own: the slot files of a
replay whose run id an `s3-replay` row names (`<slot start>-<run id>.jsonl`, and copies such as `...-<run id>-again`),
a key an `s3-object` row names, and the keys under a prefix an `s3-prefix` row names. Every other file there belongs
to another recipe's run, or to a team's own pipeline, and is kept. Then, `athena-results/` (the workgroups' query
results) goes. On a bucket the ledger records `created`, `down` refuses while any other object is left, and lists
those keys (another recipe has not cleaned up); after that it deletes the shared workgroups `<PREFIX>-wg` and
`<PREFIX>-ctas-wg` that the ledger records `created`, whichever recipe's call made them (it is their only deleter,
and it runs after every recipe's clean-up), and then the bucket. A borrowed bucket is kept, with everything in it
that is not this ledger's own.

`ensure_workgroup(name)` knows three workgroups and their settings; a caller gives the name only:

    <PREFIX>-wg        engine 3, enforced,     results athena-results/          cutoff 1 GB (2^30 bytes) a query
    <PREFIX>-ctas-wg   engine 3, not enforced, results athena-results/          cutoff 8 GB   (a CTAS writes where it says)
    <PREFIX>-chat-wg   engine 3, enforced,     results athena-results/chat-wg/  cutoff 100 MB (dashboard-chat's ext-athena-engine)

A missing one is created with the course tags and recorded `created`. An existing one is compared with these
settings: a difference stops with an error naming each setting; it is never changed (another recipe's queries may
rely on it), and it is recorded `borrowed`.

This file is a shared helper (tools/shared_helpers.toml): copy it unchanged into `scripts/lake.py`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tags import ledger_rows, prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
ENGINE = "Athena engine version 3"
KEEP_PREFIXES = ("raw/trip-events/", "athena-results/")   # what down may delete in a bucket
SHARED_WORKGROUPS = ("wg", "ctas-wg")                       # deleted only by down
WORKGROUPS = {                                              # name suffix -> settings
    "wg": {"cutoff": 2 ** 30, "enforce": True, "results": "athena-results/"},
    "ctas-wg": {"cutoff": 8 * 2 ** 30, "enforce": False, "results": "athena-results/"},
    "chat-wg": {"cutoff": 104_857_600, "enforce": True, "results": "athena-results/chat-wg/"},
}


class WorkgroupMismatch(RuntimeError):
    """An existing workgroup whose settings are not the ones ensure_workgroup gives that name."""


def session(sess=None):
    return sess or boto3.session.Session(region_name=REGION)


def account_id(sess=None) -> str:
    return session(sess).client("sts").get_caller_identity()["Account"]


def bucket_name(sess=None) -> str:
    """`<PREFIX>-lake-<account>`: the account number makes the name unique across AWS."""
    return f"{prefix()}-lake-{account_id(sess)}"


def _settings(name: str) -> dict:
    for suffix, settings in WORKGROUPS.items():
        if name == f"{prefix()}-{suffix}":
            return settings
    known = ", ".join(f"{prefix()}-{s}" for s in WORKGROUPS)
    raise ValueError(f"ensure_workgroup knows {known}; {name} is not one of them (add it to WORKGROUPS in "
                     f"scripts/lake.py, with its settings, in every copy)")


def _workgroup_exists(athena, name: str) -> bool:
    """Looked up in the list: asking for a missing workgroup by name is answered with the same error code as a bad
    request, which says too little to call it absent."""
    kw = {}
    while True:
        page = athena.list_work_groups(**kw)
        if name in [w["Name"] for w in page["WorkGroups"]]:
            return True
        if not page.get("NextToken"):
            return False
        kw = {"NextToken": page["NextToken"]}


def workgroup_differences(config: dict, want: dict, output: str) -> list[str]:
    """Each setting of an existing workgroup's Configuration that differs from `want`, in words."""
    engine = config.get("EngineVersion", {})
    found = {
        "engine": engine.get("EffectiveEngineVersion") or engine.get("SelectedEngineVersion"),
        "enforced": config.get("EnforceWorkGroupConfiguration"),
        "results location": config.get("ResultConfiguration", {}).get("OutputLocation"),
        "cutoff (bytes)": config.get("BytesScannedCutoffPerQuery"),
    }
    expected = {"engine": ENGINE, "enforced": want["enforce"], "results location": output,
                "cutoff (bytes)": want["cutoff"]}
    return [f"{k} is {found[k]!r}, not {expected[k]!r}" for k in expected if found[k] != expected[k]]


def ensure_workgroup(name: str, cutoff: int | None = None, enforce: bool | None = None, *, sess=None) -> str:
    """Create the workgroup `name` with its settings if it is missing, check them if it exists; returns
    "created" or "borrowed". `cutoff` and `enforce` may only repeat the settings (callers give no numbers of their
    own); a different value is refused."""
    want = _settings(name)
    if cutoff is not None and cutoff != want["cutoff"] or enforce is not None and enforce != want["enforce"]:
        raise ValueError(f"{name} has cutoff {want['cutoff']} and enforce {want['enforce']} in scripts/lake.py; "
                         f"callers do not give their own")
    sess = session(sess)
    athena = sess.client("athena")
    output = f"s3://{bucket_name(sess)}/{want['results']}"
    if _workgroup_exists(athena, name):
        config = athena.get_work_group(WorkGroup=name)["WorkGroup"]["Configuration"]
        wrong = workgroup_differences(config, want, output)
        if wrong:
            raise WorkgroupMismatch(f"workgroup {name} exists with other settings, and is not changed: "
                                    + "; ".join(wrong))
        if recorded("athena-workgroup", name) not in ("created", "borrowed"):
            record("athena-workgroup", name, "borrowed", script="lake.py ensure_workgroup")
        return recorded("athena-workgroup", name)
    athena.create_work_group(
        Name=name, Description=f"{name}: recipe workgroup (scripts/lake.py)", Tags=tags("athena"),
        Configuration={"ResultConfiguration": {"OutputLocation": output},
                       "EnforceWorkGroupConfiguration": want["enforce"],
                       "PublishCloudWatchMetricsEnabled": True,
                       "BytesScannedCutoffPerQuery": want["cutoff"],
                       "EngineVersion": {"SelectedEngineVersion": ENGINE}})
    shared = name.removeprefix(f"{prefix()}-") in SHARED_WORKGROUPS
    record("athena-workgroup", name, "created", arn=f"arn:aws:athena:{REGION}:{account_id(sess)}:workgroup/{name}",
           script="lake.py ensure_workgroup",
           clean="scripts/lake.py down" if shared else "the owning recipe's clean_up.py")
    return "created"


def up(sess=None, log=print) -> str:
    """Create the bucket if it is missing; returns "created" or "borrowed"."""
    sess = session(sess)
    s3, name = sess.client("s3"), bucket_name(sess)
    try:
        s3.head_bucket(Bucket=name)
        exists = True
    except ClientError as err:
        if err.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
            raise                                   # 403: the name is taken by another account
        exists = False
    if exists:
        if recorded("s3-bucket", name) != "created":
            record("s3-bucket", name, "borrowed", script="scripts/lake.py up")
            log(f"borrowed s3://{name} (it was there before: down keeps it)")
        else:
            log(f"exists   s3://{name} (created by lake.py up)")
        return recorded("s3-bucket", name)
    config = {"LocationConstraint": REGION, "Tags": tags("s3")}
    try:
        s3.create_bucket(Bucket=name, CreateBucketConfiguration=config)
    except ClientError as err:
        if err.response["Error"]["Code"] != "AccessDenied":
            raise
        # Tags in CreateBucket need s3:TagResource; without it, tag right after the create.
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": tags("s3")})
    record("s3-bucket", name, "created", arn=f"arn:aws:s3:::{name}", script="scripts/lake.py up",
           clean="scripts/lake.py down", bills="monthly")
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration={"Rules": [
        {"ID": "athena-results-7-days", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"},
         "Expiration": {"Days": 7}},
        {"ID": "incomplete-uploads-1-day", "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]})
    log(f"created  s3://{name} (tagged, public access blocked, athena-results/ expire after 7 days)")
    return "created"


def _versions(s3, bucket: str):
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
            yield {"Key": v["Key"], "VersionId": v["VersionId"]}


RAW = "raw/trip-events/"
RESULTS = "athena-results/"
RAW_FILE = re.compile(r"^\d{8}T\d{4}Z-(?P<rest>.+)\.[a-z]+$")    # <slot start>-<run id>[...].jsonl


def own_raw(keys, rows: list[dict] | None = None) -> list[str]:
    """The keys under raw/trip-events/ this ledger records as its own (`s3-replay` run ids, `s3-object` keys,
    `s3-prefix` prefixes; the last line of each name is its state, and only `created` counts)."""
    rows = ledger_rows() if rows is None else rows
    state: dict[tuple[str, str], str] = {}
    for r in rows:
        if r.get("type") in ("s3-replay", "s3-object", "s3-prefix"):
            state[(r["type"], r["name"])] = r["state"]
    mine = {kind: [n for (k, n), s in state.items() if k == kind and s == "created"]
            for kind in ("s3-replay", "s3-object", "s3-prefix")}
    objects, prefixes = set(mine["s3-object"]), tuple(mine["s3-prefix"])
    runs = set(mine["s3-replay"])
    out = []
    for key in keys:
        if not key.startswith(RAW):
            continue
        m = RAW_FILE.match(key.rsplit("/", 1)[-1])
        rest = m["rest"] if m else ""
        if (key in objects or (prefixes and key.startswith(prefixes))
                or any(rest == r or rest.startswith(r + "-") for r in runs)):
            out.append(key)
    return out


def _delete(s3, bucket: str, objs: list[dict]) -> None:
    for i in range(0, len(objs), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True})


def down(sess=None, yes: bool = False, log=print) -> int:
    """Delete the shared workgroups and the bucket this ledger created; 1 when it refused or a step failed."""
    sess = session(sess)
    s3, athena, name = sess.client("s3"), sess.client("athena"), bucket_name(sess)
    state = recorded("s3-bucket", name)
    verb = "removed " if yes else "would remove"
    try:
        objs = list(_versions(s3, name))
    except ClientError as err:
        if err.response["Error"]["Code"] not in ("NoSuchBucket", "404"):
            raise
        objs = None
    own = set(own_raw({o["Key"] for o in objs})) if objs else set()
    mine = [o for o in objs or [] if o["Key"] in own or o["Key"].startswith(RESULTS)]
    if state == "created" and objs:
        left = sorted({o["Key"] for o in objs if o["Key"] not in own and not o["Key"].startswith(RESULTS)})
        if left:
            log(f"REFUSED  s3://{name} holds {len(left)} objects that are not this ledger's own (under {RAW}: "
                f"another run's files): run the clean-up of the recipe that wrote them first")
            for key in left[:50]:
                log(f"         {key}")
            return 1
    for suffix in SHARED_WORKGROUPS:
        wg = f"{prefix()}-{suffix}"
        wg_state = recorded("athena-workgroup", wg)
        if not _workgroup_exists(athena, wg):
            log(f"absent   Athena workgroup {wg}")
            if yes and wg_state == "created":
                record("athena-workgroup", wg, "deleted", script="scripts/lake.py down")
        elif wg_state != "created":
            log(f"kept     Athena workgroup {wg} ({wg_state or 'not in the ledger'})")
        else:
            if yes:
                athena.delete_work_group(WorkGroup=wg, RecursiveDeleteOption=True)
                record("athena-workgroup", wg, "deleted", script="scripts/lake.py down")
            log(f"{verb} Athena workgroup {wg}")
    if objs is None:
        log(f"absent   s3://{name}")
        if yes and state == "created":
            record("s3-bucket", name, "deleted", script="scripts/lake.py down")
        return 0
    if state != "created":
        if yes:
            _delete(s3, name, mine)
        log(f"{verb} {len(mine)} object versions of this ledger's own files under {RAW} and of {RESULTS} in "
            f"s3://{name}; the bucket and every other file are kept ({state or 'not in the ledger'})")
        return 0
    if yes:
        _delete(s3, name, objs)
        s3.delete_bucket(Bucket=name)
        record("s3-bucket", name, "deleted", script="scripts/lake.py down")
    log(f"{verb} s3://{name} and its {len(objs)} object versions")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    d = sub.add_parser("down")
    d.add_argument("--yes", action="store_true", help="delete; without it, only list what would go")
    w = sub.add_parser("workgroup")
    w.add_argument("name")
    args = p.parse_args(argv)
    if args.cmd == "up":
        up()
        return 0
    if args.cmd == "workgroup":
        print(f"{ensure_workgroup(args.name):<8} Athena workgroup {args.name}")
        return 0
    code = down(yes=args.yes)
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())
