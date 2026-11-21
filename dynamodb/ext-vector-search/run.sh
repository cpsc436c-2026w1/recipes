#!/bin/bash
# dynamodb, extension ext-vector-search: search the riders' messages by meaning, in the trips table itself.
# Each trip gets the rider's message and its vector (embedding); a vector index on the table answers "which messages
# are like this question", with a filter on fare and pickup zone applied during the search. The run prints the bytes
# DynamoDB bills for vector writes and searches, and their price. README.md in this folder says what changes against main/.
#   KEEP=1 bash main/run.sh                     (first, once)
#   bash ext-vector-search/run.sh               (cleans up everything at the end; KEEP=1 to keep)
#   KB_MCP_TOKEN and COURSE_GATEWAY_URL set: the vectors are made anew through the course LLM gateway (step 1);
#   without them the run uses the vectors shipped in vectors.json, made the same way on 2026-11-21.
source "$(dirname "$0")/../lib.sh"
X=$HERE/ext-vector-search
need_table "$TRIPS" main
need_cli
# SearchVectors needs AWS CLI 2.36.16 or later (README, Decisions: AWS CLI). --generate-cli-skeleton reads the CLI's
# own model: it makes no call to AWS.
if [ -z "${DRY:-}" ] && ! aws dynamodb search-vectors --generate-cli-skeleton >/dev/null 2>&1; then
  echo "FAILED: this AWS CLI ($(aws --version 2>&1 | head -1)) does not know search-vectors. Update to AWS CLI v2 2.36.16 or later (we ran 2.39.3), then run again" >&2
  exit 1
fi
INDEX=trip-messages
VECTORS=$X/vectors.json
LOG=${TMPDIR:-/tmp}/$PREFIX-vector-bytes.jsonl
WORK=${TMPDIR:-/tmp}/$PREFIX-vector
mkdir -p "$WORK"; : > "$LOG"

# Decision (embeddings): the course LLM gateway's embedding model, through the student's own token; the same
# vectors ship in vectors.json, so the extension also runs without a token.
TOKEN=${KB_MCP_TOKEN:-${COURSE_MCP_TOKEN:-}}
GATEWAY=${COURSE_GATEWAY_URL:-${GATEWAY_URL:-}}
if [ -n "$TOKEN" ] && [ -n "$GATEWAY" ]; then
  say "1. Make a vector for each of the 50 messages and the 2 questions: one call to the course LLM gateway, model qwen3-embedding-0.6b"
  echo; echo "\$ POST \$COURSE_GATEWAY_URL/embeddings  (model qwen3-embedding-0.6b, 52 texts, your KB_MCP_TOKEN)"
  if [ -z "${DRY:-}" ]; then
    python3 "$X/vectors.py" embed "$WORK/vectors.json"
    VECTORS=$WORK/vectors.json
  fi
else
  say "1. No course token set: use the vectors shipped in ext-vector-search/vectors.json (qwen3-embedding-0.6b, made through the course LLM gateway)"
fi
DIM=$(python3 "$X/vectors.py" dim "$VECTORS")
echo "dimensions: $DIM"

# Decision (index on the trips table): UpdateTable adds a vector index to a table that is there. Inline filters on
# pickup_zone and fare_quote, no vector index partition key, so one search covers every zone (README, Decisions).
say "2. Add the vector index $INDEX to $TRIPS: the vector in message_vector ($DIM numbers), cosine distance, pickup_zone and fare_quote as inline filters"
look() {  # the index's status and whether it is backfilling, as DescribeTable reports them; "None" when there is no index
  aws dynamodb describe-table --table-name "$TRIPS" --output text \
    --query "Table.VectorIndexes[?IndexName=='$INDEX'] | [0].[IndexStatus, Backfilling]"
}
state=$([ -n "${DRY:-}" ] || look)
if [ -n "$state" ] && [ "${state%%[[:space:]]*}" != None ]; then
  echo; echo "(index $INDEX is already there: kept as it is)"
else
  run aws dynamodb update-table --table-name "$TRIPS" \
    --attribute-definitions AttributeName=pickup_zone,AttributeType=S AttributeName=fare_quote,AttributeType=N \
    --vector-index-updates "[{\"Create\":{\"IndexName\":\"$INDEX\",\"VectorAttribute\":{\"AttributeName\":\"message_vector\"},\"SearchSchema\":[{\"AttributeName\":\"pickup_zone\",\"SearchSchemaElementType\":\"INLINE_FILTER\"},{\"AttributeName\":\"fare_quote\",\"SearchSchemaElementType\":\"INLINE_FILTER\"}],\"Projection\":{\"ProjectionType\":\"ALL\"},\"Dimensions\":$DIM,\"DistanceFunction\":\"COSINE\"}}]" \
    --query "TableDescription.VectorIndexes[?IndexName=='$INDEX'] | [0].IndexStatus"
fi
# A new index is CREATING, then CREATING and backfilling, then ACTIVE. It cannot be searched before ACTIVE
# (developer guide, Adding a vector index to an existing table). A look every 15 s, at most 4 a minute for
# WAIT_MINUTES (default 30); counted in looks, not in clock time, so the offline tests can stand in for sleep.
if [ -z "${DRY:-}" ]; then
  start=$SECONDS last="" looks=0
  while true; do
    state=$(look | tr '\t' ' ')
    [ "$state" = "$last" ] || echo "($((SECONDS - start)) s: index $state)"
    last=$state looks=$((looks + 1))
    case "$state" in ACTIVE|"ACTIVE None"|"ACTIVE False") break;; esac
    if [ "$looks" -ge $((4 * ${WAIT_MINUTES:-30})) ]; then
      echo "FAILED: index $INDEX is not ACTIVE after ${WAIT_MINUTES:-30} minutes. Run again later with KEEP=1 (the index is kept), or clean up: bash clean_up.sh" >&2
      exit 1
    fi
    sleep 15
  done
  echo "(index $INDEX is ACTIVE after $((SECONDS - start)) s)"
fi

# Decision (cost): each write asks for the capacity of every index (INDEXES), the only level that reports the
# vector write bytes. UpdateItem, not a batch Put: the trips keep the status, driver and fare that main/ gave them.
say "3. Write each trip's message and its vector: 50 update-item calls, each printing its write units and its vector write bytes"
for i in $(seq 0 49); do
  trip=$(printf 't-%04d' "$i")
  [ -n "${DRY:-}" ] || python3 "$X/vectors.py" item "$VECTORS" "$trip" "$WORK/$trip.json"
  cmd=(aws dynamodb update-item --table-name "$TRIPS" --key "{\"trip_id\":{\"S\":\"$trip\"}}"
    --update-expression 'SET message = :m, message_vector = :v' --expression-attribute-values "file://$WORK/$trip.json"
    --return-consumed-capacity INDEXES
    --query "{write_units: ConsumedCapacity.CapacityUnits, vector_write_bytes: ConsumedCapacity.VectorIndexes.\"$INDEX\".VectorWriteRequestBytes}")
  echo; echo "\$ ${cmd[*]}"
  [ -n "${DRY:-}" ] && continue
  out=$("${cmd[@]}")
  echo "$out"
  python3 "$X/vectors.py" record write "$LOG" <<<"$out"
done

# $1 step, $2 question, the rest: the filter, if any. Search results are eventually consistent, and a newly ACTIVE
# index can answer ValidationException, or ResourceNotFoundException before it is ACTIVE (API reference), for a
# short while (developer guide): the first search is tried again, up to
# 20 times, 15 s apart. Any other error stops the run.
READY=""
search() {
  local name=$1 cmd err out try=1; shift
  [ -n "${DRY:-}" ] || python3 "$X/vectors.py" query "$VECTORS" "$name" "$WORK/q-$name.json"
  cmd=(aws dynamodb search-vectors --table-name "$TRIPS" --index-name "$INDEX" --search-vector "file://$WORK/q-$name.json"
    --top-k 5 --projection-expression 'trip_id, pickup_zone, fare_quote, message' "$@" --return-consumed-capacity TOTAL
    --query '{results: SearchResults[].[Item.trip_id.S, Item.pickup_zone.S, Item.fare_quote.N, Score, Item.message.S], search_bytes: ConsumedCapacity.VectorSearchRequestBytes}')
  echo; echo "\$ ${cmd[*]}"
  [ -n "${DRY:-}" ] && return 0
  err=$(mktemp)
  until out=$("${cmd[@]}" 2>"$err"); do
    if [ -z "$READY" ] && grep -q -e ValidationException -e ResourceNotFoundException "$err" && [ "$try" -lt 20 ]; then
      echo "(the index is not searchable yet: $(grep -o -E '(ValidationException|ResourceNotFoundException).*' "$err" | head -1); trying again in 15 s, try $((try + 1)) of 20)"
      sleep 15; try=$((try + 1))
    else
      cat "$err" >&2; rm -f "$err"
      echo "FAILED: the search was refused; the error above says why. Clean up with: bash clean_up.sh" >&2
      exit 1
    fi
  done
  rm -f "$err"; READY=1
  echo "$out"
  python3 "$X/vectors.py" record search "$LOG" <<<"$out"
}

say "4. A rider writes \"I think I left something in the car.\" The 5 messages nearest in meaning, from every zone and fare"
search lost-item
say "5. Another rider asks \"Why did I pay more than the price I was quoted?\" The 5 nearest, with no filter"
search fare-question
# Decision (filters during the search): BETWEEN on a Number and IN on a String, inline filters of the index; the
# search considers only the trips that match, so it still returns up to 5 of them.
say "6. The same question, for trips with a fare quote from 20 to 30 picked up at JFK or SoHo: the filter applies during the search"
search fare-question --search-condition-expression 'fare_quote BETWEEN :lo AND :hi AND pickup_zone IN (:z1, :z2)' \
  --expression-attribute-values '{":lo":{"N":"20"},":hi":{"N":"30"},":z1":{"S":"JFK"},":z2":{"S":"SoHo"}}'

say "7. What this run's vector requests cost: the bytes DynamoDB reported, each request billed at 1 KB at least, times the ca-central-1 prices"
if [ -n "${DRY:-}" ]; then
  echo "(DRY=1: no bytes were measured)"
else
  python3 "$X/vectors.py" cost "$LOG"
  run aws dynamodb describe-table --table-name "$TRIPS" \
    --query "Table.VectorIndexes[?IndexName=='$INDEX'] | [0].{items: ItemCount, index_bytes: IndexSizeBytes}"
  echo "(DynamoDB updates the index's item count and size from time to time, not after every write: a new index can show 0)"
fi

finish
