Feature: The consumer: stream records in, one stored update each
  main/consumer.py, called as Lambda calls it: handler(event, context), with boto3's own DynamoDB client against
  moto. README, "Requirements": R20 to R22.

  Background:
    Given the table "recipe-ana-updates" exists and the function is told its name

  @R20
  Scenario: Each update is stored with its three times
    Given the stream records
      | seq | update_id | driver_id | zone_id | event_time               | entered at     |
      | 101 | u-1       | d-0007    | 161     | 2027-01-15T08:00:00.000Z | 1800000000.250 |
      | 102 | u-2       | d-0009    | 164     | 2027-01-15T07:58:30.000Z | 1800000000.300 |
    When the function receives them at 1800000001.5 seconds
    Then the function reports no failed record
    And the table holds 2 updates
    And update "u-1" has event_ms 1800000000000, entered_ms 1800000000250, arrival_ms 1800000001500 and lag_ms 1500
    And update "u-2" has event_ms 1799999910000, entered_ms 1800000000300, arrival_ms 1800000001500 and lag_ms 91500

  @R21
  Scenario: A batch that Lambda delivers a second time
    Given the stream records
      | seq | update_id | driver_id | zone_id | event_time               | entered at     |
      | 101 | u-1       | d-0007    | 161     | 2027-01-15T08:00:00.000Z | 1800000000.250 |
    When the function receives them at 1800000001.5 seconds
    And the function receives them at 1800000099 seconds
    Then the function reports no failed record
    And the table holds 1 updates
    And update "u-1" has event_ms 1800000000000, entered_ms 1800000000250, arrival_ms 1800000001500 and lag_ms 1500
    And the function's last log line counts 0 stored and 1 repeat

  @R22
  Scenario Outline: A record that cannot be stored stops its batch there
    Given the stream records
      | seq | update_id | driver_id | zone_id | event_time               | entered at     |
      | 101 | u-1       | d-0007    | 161     | 2027-01-15T08:00:00.000Z | 1800000000.250 |
      | 102 | <id>      | d-0009    | 164     | <event_time>             | 1800000000.300 |
      | 103 | u-3       | d-0011    | 162     | 2027-01-15T08:00:01.000Z | 1800000000.400 |
    When the function receives them at 1800000001.5 seconds
    Then the function reports the record with sequence number "102" as failed
    And the table holds 1 updates

    Examples:
      | id  | event_time    |
      | u-2 | not a time    |
      | u-2 |               |
