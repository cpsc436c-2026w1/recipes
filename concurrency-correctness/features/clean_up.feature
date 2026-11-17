Feature: Clean-up removes the recipe's two tables, and nothing else
  clean_up.py, run as the command line runs it, with moto in place of DynamoDB. README, "Requirements": R40 to R44.

  Background:
    Given the two tables of PREFIX "recipe-ana" exist

  @R40
  Scenario: Clean-up after a kept run
    When clean_up.py runs
    Then the exit code is 0
    And the account has no tables
    And 2 printed lines start with "removed  table recipe-ana-race-"
    And the recipe waited for each table to be gone after its delete

  @R41
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then the exit code is 0
    And 2 printed lines start with "absent   table recipe-ana-race-"

  @R42
  Scenario Outline: AWS refuses a step
    Given AWS refuses the "<operation>" call for "recipe-ana-race-trips" with "<code>"
    When clean_up.py runs
    Then the exit code is 1
    And 1 printed line starts with "FAILED   table recipe-ana-race-trips: <code>"
    And the output holds "removed  table recipe-ana-race-requests"
    And the output holds "the clean-up is NOT complete"
    And the account has exactly the tables "recipe-ana-race-trips"

    Examples:
      | operation     | code                  |
      | DeleteTable   | AccessDeniedException |
      | DescribeTable | AccessDeniedException |
      | DeleteTable   | LimitExceededException |

  @R43
  Scenario: A table that an earlier clean-up is still deleting
    Given the table "recipe-ana-race-trips" is in the DELETING state until the recipe has waited for it
    When clean_up.py runs
    Then the exit code is 0
    And the output holds "absent   table recipe-ana-race-trips (was already being deleted)"
    And 1 DeleteTable call was made, for "recipe-ana-race-requests"

  @R44
  Scenario: Other tables stay, also ones with the same beginning
    Given the tables "recipe-ana-race-trips-old, recipe-bo-race-trips, trips" of other owners exist
    When clean_up.py runs
    Then the account has exactly the tables "recipe-ana-race-trips-old, recipe-bo-race-trips, trips"
