@ext-backfill
Feature: A past trip by its id
  ext-backfill, the support agent's story. Offline, moto stands in for DynamoDB and S3, and a stub for ImportTable
  (features/steps/backfill_steps.py) loads the import files into a moto table and reports the counts an example
  gives. trips_history is written offline as in features/backfill.feature.

  Background:
    Given the setting PREFIX is "recipe"

  @R130
  Rule: A new table is created by an import from S3 of the history as DynamoDB JSON, which keeps each column's type

    Example: No table yet
      Given table recipe-trips-by-id does not exist
      And trips_history holds the simulated data set SIM01 "march-sample" of 1000 rows
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then the files under imports/trips-by-id/ hold 1000 lines, each an Item in DynamoDB JSON
      And one ImportTable call with input format DYNAMODB_JSON created recipe-trips-by-id with key trip_id
      And no BatchWriteItem call was made

    Example: A fare stays a number
      Given table recipe-trips-by-id does not exist
      And trips_history holds these rows:
        | trip_id  | pickup_zone_id | requested_time       | fare  | origin   | run_id | requested_day |
        | h-01a2-1 | 161            | 2024-03-14T08:01:10Z | 18.40 | backfill | bf-001 | 2024-03-14    |
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then item h-01a2-1 has fare 18.40 as a number
      And an UpdateItem that adds 1.00 to fare leaves 19.40

    Example: CSV on request
      Given table recipe-trips-by-id does not exist
      And trips_history holds these rows:
        | trip_id  | pickup_zone_id | requested_time       | fare  | origin   | run_id | requested_day |
        | h-01a2-1 | 161            | 2024-03-14T08:01:10Z | 18.40 | backfill | bf-001 | 2024-03-14    |
      When ext-backfill/backfill.py runs with "--to-dynamodb --import-format csv"
      Then item h-01a2-1 has fare "18.40" as text

  @R131
  Rule: An existing table gets batches of at most 25 items without a repeated key, and unprocessed items are sent again

    Example: Unprocessed items on the first two attempts
      Given table recipe-trips-by-id exists
      And the simulated data set SIM03 "throttled-writes" returns 5 unprocessed items per call on the first 2 attempts
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then every BatchWriteItem call carried at most 25 items, each trip_id once
      And table recipe-trips-by-id has 60 items
      And the script waited twice, under limits of 0.1 s and 0.2 s

  @R132
  Rule: Items still unprocessed after the 5th attempt stop the run with their number and the first id

    Example: Unprocessed every time
      Given table recipe-trips-by-id exists
      And the simulated data set SIM03 "throttled-writes" returns 5 unprocessed items per call on every attempt
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then the run stops with an error that holds "unprocessed after 5 attempts" and the first unprocessed trip_id
      And the exit code is 1

  @R133
  Rule: The items loaded are counted exactly and compared with the rows read

    Example: An import that matches
      Given table recipe-trips-by-id does not exist
      And trips_history holds the simulated data set SIM01 "march-sample" of 1000 rows
      And the import reports ImportedItemCount 1000 and ErrorCount 0
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then the output holds "1000 items imported, 1000 rows read, 0 errors"
      And the exit code is 0

    Example: An import with errors
      Given table recipe-trips-by-id does not exist
      And trips_history holds the simulated data set SIM01 "march-sample" of 1000 rows
      And the import reports ImportedItemCount 998 and ErrorCount 2
      When ext-backfill/backfill.py runs with "--to-dynamodb"
      Then the output holds "998 items imported, 1000 rows read, 2 errors"
      And the exit code is 1

    Example: Batch writes into a table with live items
      Given table recipe-trips-by-id exists and holds 40 live items
      And trips_history holds 60 rows of the simulated data set SIM03 "throttled-writes" without its fault
      When ext-backfill/backfill.py runs with "--to-dynamodb --load-id ld-002"
      Then the count scan read every page with ConsistentRead and counted the items of load_id ld-002
      And the output holds "60 items of load ld-002, 60 rows read"
      And the exit code is 0
