#!/bin/bash
# dynamodb, extension ext-riders-and-drivers: grow the trip store from main/ into more of the app.
# Riders want their trip history (a second way to find trips: by rider, in time order); drivers have a status that must
# agree with the trips they hold. New tables, a sort key, and transactions that keep tables in agreement.
# README.md in this folder says what changes against main/.
#   KEEP=1 bash main/run.sh                          (first, once)
#   bash ext-riders-and-drivers/run.sh               (cleans up everything at the end; KEEP=1 to keep)
source "$(dirname "$0")/../lib.sh"
need_table "$TRIPS" main
need_cli

# Decision (keys): a second access pattern (one rider, in time order) gets its own table and key, written with the first.
say "1. A table for each rider's trips in time order: partition key rider_id, sort key requested_at"
table "$RIDER_TRIPS" '[{"AttributeName":"rider_id","KeyType":"HASH"},{"AttributeName":"requested_at","KeyType":"RANGE"}]' \
  '[{"AttributeName":"rider_id","AttributeType":"S"},{"AttributeName":"requested_at","AttributeType":"S"}]'
say "2. A table of drivers and their status"
table "$DRIVERS" AttributeName=driver_id,KeyType=HASH AttributeName=driver_id,AttributeType=S

say "3. Load history: 50 past trips of 5 riders over 10 days, and 5 available drivers"
python3 - "$RIDER_TRIPS" "$DRIVERS" > "${TMPDIR:-/tmp}/$PREFIX-items.json" <<'EOF'
import json, sys
rider_trips, drivers = sys.argv[1:]
zones = ["Midtown", "Harlem", "Astoria", "SoHo", "JFK"]
r = [{"PutRequest": {"Item": {"rider_id": {"S": f"r-{i % 5:02d}"},
      "requested_at": {"S": f"2026-10-{1 + i // 5:02d}T{8 + i % 5:02d}:15:00Z"},
      "trip_id": {"S": f"h-{i:04d}"}, "pickup_zone": {"S": zones[i % 5]}}}} for i in range(50)]
d = [{"PutRequest": {"Item": {"driver_id": {"S": f"d-{i}"}, "status": {"S": "available"}}}} for i in range(1, 6)]
print(json.dumps([{rider_trips: r[:25]}, {rider_trips: r[25:]}, {drivers: d}]))
EOF
for i in 0 1 2; do
  b=$(python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))[int(sys.argv[2])]))" "${TMPDIR:-/tmp}/$PREFIX-items.json" $i)
  batch_write "$b"         # sends unprocessed items again (lib.sh)
done

# Restored from the captured run (output/ext-riders-and-drivers.txt, step 3b): makes the extension safe to run twice.
say "3b. Start clean: remove trips t-0100 and t-0101 left by an earlier run, and make d-3 available"
run aws dynamodb delete-item --table-name "$TRIPS" --key '{"trip_id":{"S":"t-0100"}}'
run aws dynamodb delete-item --table-name "$TRIPS" --key '{"trip_id":{"S":"t-0101"}}'
run aws dynamodb update-item --table-name "$DRIVERS" --key '{"driver_id":{"S":"d-3"}}' \
  --update-expression 'REMOVE trip_id SET #s = :s' --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":s":{"S":"available"}}'

say "4. Rider r-03 opens their history for Oct 3 to Oct 6: Query, one partition key and a sort-key range, in time order"
run aws dynamodb query --table-name "$RIDER_TRIPS" \
  --key-condition-expression 'rider_id = :r AND requested_at BETWEEN :a AND :b' \
  --expression-attribute-values '{":r":{"S":"r-03"},":a":{"S":"2026-10-03"},":b":{"S":"2026-10-06T23:59:59Z"}}' \
  --return-consumed-capacity TOTAL \
  --query '{trips: Items[].[requested_at.S, trip_id.S], read: ScannedCount, read_units: ConsumedCapacity.CapacityUnits}'

say "5. ...and their latest trip only: newest first, one item"
run aws dynamodb query --table-name "$RIDER_TRIPS" --key-condition-expression 'rider_id = :r' \
  --expression-attribute-values '{":r":{"S":"r-03"}}' --no-scan-index-forward --limit 1 \
  --query 'Items[0].[requested_at.S, trip_id.S]'

transact() {  # $1 file with the transaction; prints units, or the reason per item when it is cancelled
  run aws dynamodb transact-write-items --transact-items "file://$1" --return-consumed-capacity TOTAL --cli-error-format json \
    --query 'ConsumedCapacity[].{table: TableName, write_units: CapacityUnits}'
}
TX=${TMPDIR:-/tmp}/$PREFIX-tx.json

# Decision (transaction): two items that must agree change in one TransactWriteItems, all or none.
say "6. Rider r-03 requests a new trip. It must appear in BOTH tables or in neither: one transaction, two Puts"
cat > "$TX" <<EOF
[{"Put": {"TableName": "$TRIPS", "ConditionExpression": "attribute_not_exists(trip_id)",
   "Item": {"trip_id": {"S": "t-0100"}, "status": {"S": "requested"}, "pickup_zone": {"S": "SoHo"},
            "fare_quote": {"N": "21"}, "rider_id": {"S": "r-03"}, "version": {"N": "1"}}}},
 {"Put": {"TableName": "$RIDER_TRIPS",
   "Item": {"rider_id": {"S": "r-03"}, "requested_at": {"S": "2026-10-21T18:30:00Z"}, "trip_id": {"S": "t-0100"},
            "pickup_zone": {"S": "SoHo"}}}}]
EOF
transact "$TX"

accept_tx() {  # $1 trip, $2 driver: the trip gets the driver AND the driver becomes busy, or nothing changes
  cat > "$TX" <<EOF
[{"Update": {"TableName": "$TRIPS", "Key": {"trip_id": {"S": "$1"}},
   "UpdateExpression": "SET driver_id = :d, #s = :accepted", "ConditionExpression": "attribute_not_exists(driver_id)",
   "ExpressionAttributeNames": {"#s": "status"},
   "ExpressionAttributeValues": {":d": {"S": "$2"}, ":accepted": {"S": "accepted"}}}},
 {"Update": {"TableName": "$DRIVERS", "Key": {"driver_id": {"S": "$2"}},
   "UpdateExpression": "SET #s = :busy, trip_id = :t", "ConditionExpression": "#s = :available",
   "ExpressionAttributeNames": {"#s": "status"},
   "ExpressionAttributeValues": {":busy": {"S": "busy"}, ":available": {"S": "available"}, ":t": {"S": "$1"}}}}]
EOF
  transact "$TX"
}
say "7. Driver d-3 accepts t-0100: the trip is free and d-3 is available, so both items change (2 write units each)"
accept_tx t-0100 d-3
say "8. Rider r-01 requests trip t-0101, the same way as step 6"
cat > "$TX" <<EOF
[{"Put": {"TableName": "$TRIPS", "ConditionExpression": "attribute_not_exists(trip_id)",
   "Item": {"trip_id": {"S": "t-0101"}, "status": {"S": "requested"}, "pickup_zone": {"S": "JFK"},
            "fare_quote": {"N": "40"}, "rider_id": {"S": "r-01"}, "version": {"N": "1"}}}},
 {"Put": {"TableName": "$RIDER_TRIPS",
   "Item": {"rider_id": {"S": "r-01"}, "requested_at": {"S": "2026-10-21T18:31:00Z"}, "trip_id": {"S": "t-0101"},
            "pickup_zone": {"S": "JFK"}}}}]
EOF
transact "$TX"
say "9. d-3, still busy with t-0100, accepts t-0101: the driver's condition fails, so NEITHER item changes"
or_refused "cancelled: one reason per item, in order; the driver's says ConditionalCheckFailed" accept_tx t-0101 d-3
run aws dynamodb get-item --table-name "$TRIPS" --key '{"trip_id":{"S":"t-0101"}}' --consistent-read \
  --query '{trip: Item.trip_id.S, status: Item.status.S, driver: Item.driver_id.S}'

# Decision (optimistic locking): each writer sends the version it read; a write with an old version is refused.
say "10. The fare of t-0100 changes on two screens at once. Each sends the version it read; only the first write lands"
fare() {  # $1 new fare, $2 version the screen read
  run aws dynamodb update-item --table-name "$TRIPS" --key '{"trip_id":{"S":"t-0100"}}' \
    --update-expression 'SET fare_quote = :f, version = version + :one' --condition-expression 'version = :seen' \
    --expression-attribute-values "{\":f\":{\"N\":\"$1\"},\":seen\":{\"N\":\"$2\"},\":one\":{\"N\":\"1\"}}" \
    --return-values UPDATED_NEW --query 'Attributes.{fare: fare_quote.N, version: version.N}'
}
fare 24 1
or_refused "refused: the version moved on; this screen reads the trip again, then decides" fare 26 1

finish
