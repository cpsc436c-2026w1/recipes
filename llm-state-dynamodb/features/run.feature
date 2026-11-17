Feature: The main scenario's run, its tables, and the course constraints
  main/run.py with --target local (moto, started by the script) and --target aws (here also inside moto).
  README, "Requirements": R20 to R23, R30 to R33.

  @R21 @R30 @R33
  Scenario: A run against AWS creates the two tables the way the recipe needs them
    When main/run.py runs with "--target aws"
    Then the exit code is 0
    And the account has the tables "test-chat-conversations, test-chat-history"
    And every table of the account is billed per request, has time to live on "expires_at" and the tag "recipe=llm-state-dynamodb"

  @R30
  Scenario: Another PREFIX names other tables
    Given PREFIX is "recipe-ana"
    When main/run.py runs with "--target aws"
    Then the account has the tables "recipe-ana-chat-conversations, recipe-ana-chat-history"

  @R31
  Scenario Outline: The Region of every call
    Given AWS_REGION is "<region>"
    When main/run.py runs with "--target aws"
    Then every DynamoDB call went to "<expected>"

    Examples:
      | region    | expected     |
      | not set   | ca-central-1 |
      | us-west-2 | us-west-2    |

  @R22
  Scenario: A table that an earlier clean-up is still deleting
    Given the table "test-chat-history" is in the DELETING state when the run starts
    When main/run.py runs with "--target aws"
    Then the exit code is 0
    And the run waited until "test-chat-history" was gone and created it again
    And the printed output holds "tables created: test-chat-conversations, test-chat-history"

  @R23
  Scenario: A second run reuses the tables
    When main/run.py runs with "--target aws"
    And main/run.py runs with "--target aws" again
    Then the second run printed "tables created: none (already there)"
    And CreateTable was called 2 times in all
