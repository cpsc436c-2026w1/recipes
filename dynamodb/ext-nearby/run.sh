#!/bin/bash
# ext-nearby: drivers near a pickup point, and the nearest weather stations, in DynamoDB. README.md of this folder.
#   KEEP=1 bash main/run.sh && KEEP=1 bash ext-riders-and-drivers/run.sh     first: the table $PREFIX-drivers
#   DRY=1 bash ext-nearby/run.sh                                               prints every command, runs nothing
#   bash ext-nearby/run.sh                                                     the run, then the whole recipe's clean-up
# SPEED (default 5): how many times faster than real time the hour is replayed (README, Decisions: replay speed).
# The AWS work is in nearby.py (boto3), so that one module holds what a Lambda handler would call.
source "$(dirname "$0")/../lib.sh"
PY=${PYTHON:-python3}
N="$HERE/ext-nearby/nearby.py"
SPEED=${SPEED:-5}

say "1. Create $PREFIX-drivers-by-cell and $PREFIX-points-by-cell (kept when there); borrow $DRIVERS"
run "$PY" "$N" tables

say "2. The check answers of SIM01, from SIM01 and SIM02 alone, before AWS is called"
run "$PY" "$N" expect

say "3. Put SIM02's 60 drivers at their start positions (one transaction of 2 items each)"
run "$PY" "$N" seed

say "4. Replay SIM02's 7,200 positions and SIM01's 837 requests at speed $SPEED; check each answer (about 12 minutes at speed 5)"
FAILED_STEPS=""
run "$PY" "$N" replay --speed "$SPEED" || FAILED_STEPS="$FAILED_STEPS replay"

say "5. Store SIM04's four weather stations under cells of a precision chosen from their spacing"
run "$PY" "$N" load

say "6. The 3, then the 4 stations nearest to Times Square; then 4 with MAX_RINGS=1"
run "$PY" "$N" nearest 40.7580 -73.9855 weather 3
run "$PY" "$N" nearest 40.7580 -73.9855 weather 4
run env MAX_RINGS=1 "$PY" "$N" nearest 40.7580 -73.9855 weather 4

say "7. Errors before any read: a point at the airport, and a car type the system does not have"
run "$PY" "$N" nearby 40.6413 -73.7781 standard
run "$PY" "$N" nearby 40.7580 -73.9855 limo

say "8. Bench: SIM01's 837 requests one at a time; p50, p95, p99 (against 1,000 ms) and read units (run it in ca-central-1)"
run "$PY" "$N" bench || FAILED_STEPS="$FAILED_STEPS bench"

# a failed check still ends with the clean-up, so nothing is left billing; the exit code says it failed
finish
if [ -n "$FAILED_STEPS" ]; then echo "FAILED:$FAILED_STEPS (see above)" >&2; exit 1; fi
