"""R100 live for ext-scheduled-merge: the day's manifest deleted, the same objects merged again; trips and zone_counts
compared by a fingerprint before and after.

    PREFIX=recipe-yourname uv run python ext-scheduled-merge/r100_live.py     # after live_run.py, the same UTC day
"""
import sys
from datetime import datetime, timezone
sys.path += ["ext-scheduled-merge", "scripts", "sim", "."]
import boto3
import lake
import merge
import live_run

sess = boto3.session.Session(region_name="ca-central-1")
n, athena, s3 = merge.names(), sess.client("athena"), sess.client("s3")
bucket = lake.bucket_name(sess)
day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FP = ("SELECT count(*) AS trips, sum(crc32(to_utf8(concat_ws('|', trip_id, trip_state, cast(fare AS varchar), "
      "coalesce(driver_id, ''), last_event_id)))) AS fingerprint FROM trips")
ZC = ("SELECT count(*) AS rows_, sum(trips_requested) AS req, sum(trips_completed) AS comp FROM zone_counts")


def fingerprint():
    a, _ = live_run.query(athena, FP, n["workgroup"], n["database"])
    b, _ = live_run.query(athena, ZC, n["workgroup"], n["database"])
    return a[0], b[0]


before = fingerprint()
print(f"before {before}")
key = f"lake/merge-state/dt={day}.txt"
lines = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode().splitlines()
print(f"manifest {key}: {len(lines)} lines, deleted")
s3.delete_object(Bucket=bucket, Key=key)
body = merge.run_now(sess, log=lambda *_: None)
print(f"rerun {body.get('run_state')} objects {body.get('objects')} quarantined {body.get('quarantined')}")
for step in ("merge", "rebuild"):
    if body.get(f"{step}_query_id"):
        print(f"{step:<8} {live_run.stats(athena, body[f'{step}_query_id'])}")
after = fingerprint()
print(f"after  {after}")
print(f"R100 the same {body.get('objects')} objects merged again leave trips and zone_counts unchanged: {before == after}")
sys.exit(0 if before == after else 1)
