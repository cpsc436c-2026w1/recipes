Feature: Lakehouse first: stream, Firehose, Parquet in S3, Athena
  rep-lakehouse-first/run.py --target aws, called inside moto. Firehose's files and Athena's answer are stated by
  the scenario (features/support.py). rep-lakehouse-first/README.md, "Requirements": R40 to R49.

  Background:
    Given Firehose has written 2 Parquet files
    And Athena answers the count with 143, after scanning 6967 bytes

  @R40
  Scenario: The stream and the Firehose stream
    When rep-lakehouse-first/run.py runs with "--target aws --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the stream "recipe-ana-updates" was created with 1 shard, in provisioned mode
    And the Firehose stream "recipe-ana-to-lake" reads that stream and writes to the bucket "recipe-ana-lake-123456789012" under "updates/dt=!{timestamp:yyyy-MM-dd}/"
    And it writes a file after 60 seconds or 64 MB, as Parquet with "SNAPPY", with the schema of the Glue table "updates"
    And the output holds "updates in the last hour: 143"

  @R41
  Scenario: The Glue table
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the Glue table "updates" of "recipe_ana_lake" has the columns "event_id, entity_id, event_time, value"
    And its partition "dt" is worked out by partition projection from "s3://recipe-ana-lake-123456789012/updates/dt=${dt}/"

  @R42
  Scenario: The workgroup's scan limit
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the workgroup "recipe-ana-wg" cancels a query that would scan more than 1000000000 bytes
    And it writes results under "s3://recipe-ana-lake-123456789012/athena-results/" whatever the query asks for
    And every query went to the workgroup "recipe-ana-wg"

  @R43
  Scenario: What Firehose's role may do
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the role "recipe-ana-firehose-role" may read the stream "recipe-ana-updates", write the bucket "recipe-ana-lake-123456789012" and read the Glue table "recipe_ana_lake/updates"
    And no statement of its policy has "*" as a resource or in an action
    And the role can be assumed by "firehose.amazonaws.com" only

  @R43
  Scenario: A role that is passed in
    Given FIREHOSE_ROLE_ARN names the role "team-firehose" of the account
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the exit code is 0
    And no role was created
    And the Firehose stream acts as "arn:aws:iam::123456789012:role/team-firehose"

  @R44
  Scenario: More updates than one call takes
    When rep-lakehouse-first/run.py runs with "--target aws --n 1200"
    Then the PutRecords calls carry "500, 500, 205" records
    And every record has its entity_id as partition key

  @R45
  Scenario: The stream refuses 3 records of the first call, once
    Given the stream refuses the first 3 records of its first 1 calls
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the exit code is 0
    And the PutRecords calls carry "305, 3" records
    And the output holds "3. sent 305 records"

  @R46
  Scenario: The stream keeps refusing 3 records
    Given the stream refuses the first 3 records of every call
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the run stops with the error "3 records still failing after 5 tries"
    And the PutRecords calls carry "305, 3, 3, 3, 3" records

  @R47
  Scenario: The query over the lake
    When rep-lakehouse-first/run.py runs with "--target aws --as-of 2026-11-12T16:00:30Z"
    Then the count Athena was asked for is of distinct event_id, from the folder "dt=2026-11-12" on, with event times from "2026-11-12 15:00:00.000" up to "2026-11-12 16:00:00.000"
    And the output holds "Data scanned 6967 bytes"

  @R47
  Scenario: Offline, files land under the day they arrived
    When rep-lakehouse-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then every Parquet file is under ".local/lakehouse/updates/dt=<today>/"

  @R48
  Scenario: The stream of an earlier run is still being deleted
    Given the stream "recipe-ana-updates" is in the DELETING state until the recipe has waited for it
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the exit code is 0
    And the recipe asked for the stream 2 times, and looked whether it was gone in between

  @R48
  Scenario: The Firehose stream of an earlier run is still being deleted
    Given the Firehose stream is in the DELETING state until the recipe has waited for it
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the exit code is 0
    And the Firehose stream was created after it was gone

  @R49
  Scenario: The Firehose stream does not start
    Given the Firehose stream's status is "CREATING_FAILED"
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the run stops with the error "did not start: CREATING_FAILED"
    And no record was put on the stream
