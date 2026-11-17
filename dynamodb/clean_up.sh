#!/bin/bash
# dynamodb: delete everything main/ and the extensions created. Deleting a table also ends its PITR charges.
#   BUCKET=<your bucket> bash clean_up.sh
# Only names built from $PREFIX are deleted, so a teammate's tables with another prefix stay (README, Decisions: names).
# Every step is attempted, also after one fails. A step that AWS refused is printed as FAILED and the script then
# ends with exit code 1; "not found" means already gone and is not a failure.
source "$(dirname "$0")/lib.sh"
DB=${PREFIX//-/_}_lake
FAILED=0

# one delete: $1 what it removes (for the messages), then the command.
# Returns 0 when it was deleted now, 1 when it was already gone or the delete was refused.
remove() {
  local what=$1 out; shift
  echo; echo "\$ $*"
  [ -n "${DRY:-}" ] && return 0
  if out=$("$@" 2>&1); then
    [ -z "$out" ] || echo "$out"
    return 0
  fi
  if grep -q -e ResourceNotFoundException -e EntityNotFoundException <<<"$out"; then
    echo "($what not found: already gone)"
  else
    echo "${out:-(the command failed without a message)}" >&2
    echo "FAILED: $what was not deleted" >&2
    FAILED=1
  fi
  return 1
}

# one final check: $1 what it looks for, then a command that prints the exact names of this recipe that are still
# there (empty when none is). Anything printed, or a check that AWS refused, is a FAILED line.
left() {
  local what=$1 out; shift
  echo; echo "\$ $*"
  [ -n "${DRY:-}" ] && return 0
  if ! out=$("$@" 2>&1); then
    echo "$out" >&2
    echo "FAILED: could not check for the $what" >&2
    FAILED=1
  elif [ -n "$(tr -d '[:space:]' <<<"$out")" ]; then
    echo "$out"
    echo "FAILED: still there: $what $out" >&2
    FAILED=1
  else
    echo "(no $what left)"
  fi
}

say "1. Delete the three tables, and wait until each is gone (a run right after this can then create them again)"
for t in "$TRIPS" "$RIDER_TRIPS" "$DRIVERS"; do
  if [ -z "${DRY:-}" ] && [ "$(aws dynamodb describe-table --table-name "$t" --query Table.TableStatus --output text 2>/dev/null)" = DELETING ]; then
    echo; echo "(table $t is already being deleted by an earlier clean-up)"
    aws dynamodb wait table-not-exists --table-name "$t" || { echo "FAILED: table $t is still there" >&2; FAILED=1; }
  elif remove "table $t" aws dynamodb delete-table --table-name "$t" --query TableDescription.TableStatus; then
    [ -n "${DRY:-}" ] || aws dynamodb wait table-not-exists --table-name "$t" || { echo "FAILED: table $t is still there" >&2; FAILED=1; }
  fi
done

say "2. Delete what ext-nightly-report made: the Athena table and its database, the export and the Athena results in S3"
remove "Athena table $DB.trips_export" aws glue delete-table --database-name "$DB" --name trips_export || true
remove "database $DB" aws glue delete-database --name "$DB" || true
if [ -n "${BUCKET:-}" ]; then
  remove "the export in s3://$BUCKET/$PREFIX/exports/" aws s3 rm --recursive --quiet "s3://$BUCKET/$PREFIX/exports/" || true
  remove "the Athena results in s3://$BUCKET/$PREFIX/athena-results/" aws s3 rm --recursive --quiet "s3://$BUCKET/$PREFIX/athena-results/" || true
else
  echo
  echo "(BUCKET is not set, so nothing was deleted from S3. If you ran ext-nightly-report, run this again with"
  echo " BUCKET=<your bucket>: the export under $PREFIX/exports/ and the results under $PREFIX/athena-results/ stay until then)"
fi

say "3. Check: none of this recipe's tables and no Athena database of it left (each check prints \"(no ... left)\")"
left "table" aws dynamodb list-tables --output text \
  --query "TableNames[?@=='$TRIPS' || @=='$RIDER_TRIPS' || @=='$DRIVERS']"
left "database" aws glue get-databases --output text --query "DatabaseList[?Name=='$DB'].Name"

if [ "$FAILED" != 0 ]; then
  echo >&2
  echo "The clean-up is NOT complete: see the FAILED lines above, fix what they say, and run clean_up.sh again." >&2
  exit 1
fi
