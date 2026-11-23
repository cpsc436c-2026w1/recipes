@aws
Feature: Clean-up removes what the recipe created, and nothing else
  clean_up.py deletes the three tables, the three alarms and the SNS topic of this PREFIX.
  README, "Requirements": R60, R70 to R75. Every scenario runs against moto, a local stand-in for AWS.

  Background:
    Given everything a real run creates exists for PREFIX "test"

  @R60 @R70
  Scenario: Clean-up after a real run
    Then every table, alarm and topic of the account has a name that starts with "test-"
    When clean_up.py runs
    Then clean-up reports 7 removed, 0 absent and 0 failed
    And the account has 0 tables, 0 alarms, 0 topics and 0 subscriptions

  @R71
  Scenario: A dry run prints the names and calls nothing
    Given any call to AWS fails the scenario
    When clean_up.py runs from the command line with "--dry-run"
    Then the exit code is 0
    And 7 printed lines start with "would remove, if it exists:"
    And the printed output holds "dynamodb table test-chat-traces"

  @R71
  Scenario: Without --yes nothing is deleted
    Given any call to AWS fails the scenario
    When clean_up.py runs from the command line
    Then the exit code is 0
    And 7 printed lines start with "would remove, if it exists:"
    And the printed output holds "Nothing deleted. Run again with --yes to delete these."

  @R70
  Scenario: With --yes it deletes
    When clean_up.py runs from the command line with "--yes"
    Then the exit code is 0
    And the printed output holds "7 resources removed for PREFIX=test"
    And the account has 0 tables, 0 alarms, 0 topics and 0 subscriptions

  @R72
  Scenario: A table that is still being deleted
    Given the DynamoDB table "test-chat-traces" is in the DELETING state
    When clean_up.py runs
    Then clean-up reports "dynamodb table test-chat-traces (already being deleted)" as absent
    And clean-up did not ask DynamoDB to delete "test-chat-traces" again
    And clean-up reports 6 removed, 1 absent and 0 failed

  @R73
  Scenario: AWS refuses a step
    Given AWS refuses every DescribeTable call with "AccessDeniedException"
    When clean_up.py runs from the command line with "--yes"
    Then the exit code is 1
    And 3 error lines start with "FAILED dynamodb table"
    And the printed output holds "removed sns topic test-chat-alarms"
    And the account has 3 tables, 0 alarms, 0 topics and 0 subscriptions

  @R74
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then clean-up reports 0 removed, 7 absent and 0 failed

  @R75
  Scenario: A team's own alarm with the same prefix stays
    Given the team has its own alarm "test-chat-p99"
    When clean_up.py runs
    Then the only alarm left is "test-chat-p99"
