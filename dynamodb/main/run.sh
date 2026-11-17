#!/bin/bash
# dynamodb, main scenario: a store for a ride app's live trips, end to end.
# Create the table, load trips, read one the cheap way and the strong way, let two drivers race for one trip,
# see what a read with no key costs and what an index makes of it, then clean up. README.md, "Main scenario" and "Decisions", explains each step.
#   bash main/run.sh            (cleans up at the end)
#   KEEP=1 bash main/run.sh     (keeps the table for ext-riders-and-drivers and ext-nightly-report)
#   DRY=1 bash main/run.sh      (prints the commands, runs nothing)
source "$(dirname "$0")/../lib.sh"
need_cli
K='{"trip_id":{"S":"t-0042"}}'

# Decision (keys): the key comes from the read the app makes most often. Every screen names one trip, so trip_id.
# Decision (a question with no key): the dispatcher's question gets a key of its own, a global secondary index on
# pickup zone and status. It holds the keys only; DynamoDB keeps it up to date after each write to the table.
WAITING=waiting-by-zone
say "1. Create the trips table. Every screen of the app names a trip by its id, so trip_id is the partition key; the index $WAITING answers the dispatcher (step 9)"
table "$TRIPS" AttributeName=trip_id,KeyType=HASH \
  '[{"AttributeName":"trip_id","AttributeType":"S"},{"AttributeName":"pickup_zone","AttributeType":"S"},{"AttributeName":"status","AttributeType":"S"}]' \
  "[{\"IndexName\":\"$WAITING\",\"KeySchema\":[{\"AttributeName\":\"pickup_zone\",\"KeyType\":\"HASH\"},{\"AttributeName\":\"status\",\"KeyType\":\"RANGE\"}],\"Projection\":{\"ProjectionType\":\"KEYS_ONLY\"},\"OnDemandThroughput\":{\"MaxReadRequestUnits\":$MAX_UNITS,\"MaxWriteRequestUnits\":$MAX_UNITS}}]"

say "2. Load 50 requested trips, 25 per call (the most batch-write-item takes). One write unit per trip up to 1 KB, and one more for its entry in the index"
for b in 0 1; do
  items=$(python3 -c "
import json, sys
t, b = sys.argv[1], int(sys.argv[2]); zones = ['Midtown', 'Harlem', 'Astoria', 'SoHo', 'JFK']
print(json.dumps({t: [{'PutRequest': {'Item': {'trip_id': {'S': f't-{i:04d}'}, 'status': {'S': 'requested'},
  'pickup_zone': {'S': zones[i % 5]}, 'fare_quote': {'N': str(12 + i % 30)}}}} for i in range(b * 25, b * 25 + 25)]}))" "$TRIPS" $b)
  batch_write "$items"     # sends unprocessed items again (lib.sh)
done

say "3. The rider's screen shows trip t-0042: one item by its key, eventually consistent (the default)"
run aws dynamodb get-item --table-name "$TRIPS" --key "$K" --return-consumed-capacity TOTAL \
  --query '{trip: Item.trip_id.S, status: Item.status.S, zone: Item.pickup_zone.S, read_units: ConsumedCapacity.CapacityUnits}'

# Decision (consistency): eventual reads by default; a strong read only where a stale answer would be wrong (step 7).
say "4. The same read, strongly consistent: it reflects every write that succeeded before it, for twice the read units"
run aws dynamodb get-item --table-name "$TRIPS" --key "$K" --consistent-read --return-consumed-capacity TOTAL \
  --query '{status: Item.status.S, read_units: ConsumedCapacity.CapacityUnits}'

# Decision (conditional write): the condition and the write are one step on the server, so two drivers cannot both win.
# Without the condition the second write replaces the first (a lost update).
accept() {  # $1 driver; the write happens only if no driver is set yet
  run aws dynamodb update-item --table-name "$TRIPS" --key "$K" \
    --update-expression 'SET driver_id = :d, #s = :s' --condition-expression 'attribute_not_exists(driver_id)' \
    --return-values-on-condition-check-failure ALL_OLD --cli-error-format json \
    --expression-attribute-names '{"#s":"status"}' --expression-attribute-values "{\":d\":{\"S\":\"$1\"},\":s\":{\"S\":\"accepted\"}}" \
    --return-consumed-capacity TOTAL --query '{write_units: ConsumedCapacity.CapacityUnits}'
}
say "5. Two drivers tap Accept on t-0042. d-7 is first: a conditional write, only if no driver is set"
accept d-7
say "6. d-9, a moment later, sends the same conditional write: refused, and the error carries the item that blocked it"
or_refused "refused: d-7 keeps the trip; the app tells d-9 the trip is taken. A refused write reports no units" accept d-9

say "7. The rider's screen after the accept: a strong read, so it cannot show the trip as still waiting"
run aws dynamodb get-item --table-name "$TRIPS" --key "$K" --consistent-read --query '{driver: Item.driver_id.S, status: Item.status.S}'

# Decision (a question with no key): on the table, no key names those trips, so the question is a Scan, which reads
# every item. Shown on purpose, next to the same question through the index (step 9).
say "8. A dispatcher asks for every trip waiting at JFK. On the table no key names them, so Scan reads all 50 items and filters"
run aws dynamodb scan --table-name "$TRIPS" --filter-expression 'pickup_zone = :z AND #s = :s' \
  --expression-attribute-names '{"#s":"status"}' --expression-attribute-values '{":z":{"S":"JFK"},":s":{"S":"requested"}}' \
  --select COUNT --return-consumed-capacity TOTAL --query '{returned: Count, read: ScannedCount, read_units: ConsumedCapacity.CapacityUnits}'

say "9. The same question through the index $WAITING: Query reads the waiting JFK trips and no others"
run aws dynamodb query --table-name "$TRIPS" --index-name "$WAITING" --key-condition-expression 'pickup_zone = :z AND #s = :s' \
  --expression-attribute-names '{"#s":"status"}' --expression-attribute-values '{":z":{"S":"JFK"},":s":{"S":"requested"}}' \
  --select COUNT --return-consumed-capacity TOTAL --query '{returned: Count, read: ScannedCount, read_units: ConsumedCapacity.CapacityUnits}'

finish
