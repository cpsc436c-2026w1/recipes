Feature: The function: stream records in, change rows out
  main/handler.py, called as Lambda calls it: handler(event, context). Its Firehose client is boto3's; the
  stand-in answers PutRecordBatch. README, "Requirements": R10 to R15.

  Background:
    Given the function's Firehose stream is "recipe-ana-trip-changes"

  @R10 @R11
  Scenario: One row per record, in the order of the stream
    Given the stream records
      | op     | trip  | seq  | at         | status    | pickup_zone | fare_quote | driver_id |
      | INSERT | t-001 | 101  | 1800000000 | requested | Kitsilano   | 18.50      |           |
      | INSERT | t-002 | 102  | 1800000000 | requested | Downtown    | 12         |           |
      | MODIFY | t-001 | 2007 | 1800000003 | claimed   | Kitsilano   | 18.50      | d-42      |
    When the function receives them at 1800000004.25 seconds
    Then the function reports no failed record
    And 1 PutRecordBatch call went to the stream "recipe-ana-trip-changes", with 3 rows for the trips "t-001, t-002, t-001"
    And row 3 is
      | trip_id | op     | change_time      | arrival_time     | status  | pickup_zone | fare_quote | driver_id |
      | t-001   | MODIFY | 1800000003000000 | 1800000004250000 | claimed | Kitsilano   | 18.5       | d-42      |
    And row 3 has the seq "2007" padded with zeros to 40 digits
    And row 2 has the number 12.0 as "fare_quote" and nothing as "driver_id"

  @R12
  Scenario: A removed item's row carries its last values
    Given the stream records
      | op     | trip  | seq | at         | status    | pickup_zone    | fare_quote | driver_id |
      | REMOVE | t-003 | 300 | 1800000009 | requested | Mount Pleasant | 15.25      |           |
    When the function receives them at 1800000010 seconds
    Then row 1 is
      | trip_id | op     | change_time      | arrival_time     | status    | pickup_zone    | fare_quote | driver_id |
      | t-003   | REMOVE | 1800000009000000 | 1800000010000000 | requested | Mount Pleasant | 15.25      |           |

  @R13
  Scenario: A batch larger than one Firehose call takes
    Given 1201 stream records, one per trip
    When the function receives them at 1800000100 seconds
    Then the PutRecordBatch calls carry "500, 500, 201" rows
    And the function reports no failed record

  @R14
  Scenario: Firehose rejects two records of a call once
    Given 6 stream records, one per trip
    And Firehose rejects the records with seq "3, 5" on its first call
    When the function receives them at 1800000100 seconds
    Then the PutRecordBatch calls carry "6, 2" rows
    And call 2 carries only the records with seq "3, 5"
    And the function reports no failed record
    And the recipe waited "0.2" seconds between its calls

  @R15
  Scenario: Firehose keeps rejecting two records
    Given 6 stream records, one per trip
    And Firehose rejects the records with seq "5, 3" on every call
    When the function receives them at 1800000100 seconds
    Then the PutRecordBatch calls carry "6, 2, 2" rows
    And the function reports the record with seq "3" as the first failed one
    And the recipe waited "0.2, 0.4" seconds between its calls
