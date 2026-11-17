Feature: The lake: a change log, and the latest state of each trip
  `python -m main.run`, the offline run: moto for DynamoDB and its stream, the recipe's own stand-ins for
  Firehose and Athena (main/lake.py: a real Iceberg table on disk, DuckDB). README, "Requirements": R20 to R27.

  @R20 @R21 @R22 @R25
  Scenario: The offline run
    When main.run runs with ""
    Then the exit code is 0
    And the printed trips_current has exactly the trips "t-001, t-002"
    And the printed trip "t-001" has status "claimed" and driver "d-42"
    And the output holds "changes in the log: 5 for 3 trips"
    And the output holds a line "data last updated (newest change in the lake): " with a time of today
    And the output holds a line "longest change-to-arrival: " with a number of seconds

  @R23
  Scenario: Nothing is queryable while the rows wait in the buffer
    When main.run runs with ""
    Then the output holds "rows queryable before the buffer was written: 0"

  @R23
  Scenario: The buffer interval decides when rows show
    Given a local lake and a local Firehose with a buffer of 60 seconds, on a clock the scenario moves
    And the stream records
      | op     | trip  | seq | at         | status    | pickup_zone | fare_quote | driver_id |
      | INSERT | t-001 | 101 | 1800000000 | requested | Kitsilano   | 18.50      |           |
    When the function sends them to the local Firehose
    And 59 seconds pass
    Then the lake returns 0 rows for the change log
    When 1 second passes
    Then the lake returns 1 rows for the change log

  @R24
  Scenario: Two changes of one trip in the same second
    Given a local lake and a local Firehose with a buffer of 0 seconds, on a clock the scenario moves
    And the stream records
      | op     | trip  | seq  | at         | status    | pickup_zone | fare_quote | driver_id |
      | INSERT | t-009 | 900  | 1800000000 | requested | Fairview    | 9.75       |           |
      | MODIFY | t-009 | 1000 | 1800000000 | claimed   | Fairview    | 9.75       | d-7       |
      | MODIFY | t-009 | 950  | 1800000000 | offered   | Fairview    | 9.75       |           |
    When the function sends them to the local Firehose
    And 1 second passes
    Then trips_current shows trip "t-009" with status "claimed"
    And the lake returns 3 rows for the change log

  @R27
  Scenario: A batch that Lambda delivers twice
    Given a local lake and a local Firehose with a buffer of 0 seconds, on a clock the scenario moves
    And the stream records
      | op     | trip  | seq  | at         | status    | pickup_zone | fare_quote | driver_id |
      | INSERT | t-001 | 101  | 1800000000 | requested | Kitsilano   | 18.50      |           |
      | MODIFY | t-001 | 2007 | 1800000003 | claimed   | Kitsilano   | 18.50      | d-42      |
      | INSERT | t-002 | 102  | 1800000000 | requested | Downtown    | 12         |           |
    When the function sends them to the local Firehose
    And the function sends them to the local Firehose
    And 1 second passes
    Then the lake returns 6 rows for the change log
    And the freshness query counts 3 changes for 2 trips
    And trips_current has 2 rows

  @R26
  Scenario: The offline run needs no account
    Given no AWS credentials and no Region in the environment
    When main.run runs with ""
    Then the exit code is 0
    And no call was made to Athena, Firehose, Glue, S3, Lambda or IAM
