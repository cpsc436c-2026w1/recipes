Feature: Watching the sync, and a backup of the source
  README, "Requirements": R70 to R74.

  Background:
    Given AWS is a stand-in

  @R70
  Scenario: An alarm on the function's iterator age
    When main/run.py runs with "--target aws --setup"
    Then the alarm "recipe-ana-search-iterator-age" watches the Maximum of AWS/Lambda IteratorAge for the function "recipe-ana-search-indexer" and goes off above 300000 over 1 period of 60 seconds

  @R71
  Scenario: An alarm on the failure queue
    When main/run.py runs with "--target aws --setup"
    Then the alarm "recipe-ana-search-failed-records" watches the Maximum of AWS/SQS ApproximateNumberOfMessagesVisible for the queue "recipe-ana-search-indexer-failed" and goes off at 1 or more over 1 period of 60 seconds

  @R72
  Scenario: Clean-up deletes both alarms, and a second clean-up finds them absent
    Given main/run.py has run with "--target aws --setup"
    When clean_up.py runs
    Then no alarm of "recipe-ana" is left
    And it printed "removed  alarm recipe-ana-search-iterator-age"
    When clean_up.py runs
    Then the exit code is 0
    And it printed "absent   alarm recipe-ana-search-failed-records"

  @R73
  Scenario: Point-in-time recovery on the table, 7 days
    When main/run.py runs with "--target aws --setup"
    Then the table "recipe-ana-items" has point-in-time recovery with a recovery period of 7 days

  @R74
  Scenario Outline: A new table whose backups are not available yet is tried again every 10 s, for up to 60 s
    Given DynamoDB answers UpdateContinuousBackups with ContinuousBackupsUnavailableException <n> times
    When main/run.py runs with "--target aws --setup"
    Then the exit code is <code>
    And UpdateContinuousBackups was called <calls> times

    Examples:
      | n  | code | calls |
      | 2  | 0    | 3     |
      | 5  | 0    | 6     |
      | 6  | 1    | 6     |
