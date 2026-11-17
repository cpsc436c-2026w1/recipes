Feature: Clean-up removes what the recipe and both extensions created, and nothing else
  clean_up.py, run as the command line runs it, inside moto. README, "Requirements": R60 to R64.

  Background:
    Given everything the main scenario and both extensions create exists for PREFIX "recipe-ana"

  @R60
  Scenario: Clean-up after kept runs
    When clean_up.py runs
    Then the exit code is 0
    And the account has nothing of the recipe left
    And no printed line starts with "FAILED"

  @R61
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then the exit code is 0
    And the second run printed no line that starts with "removed" or "FAILED"

  @R62
  Scenario Outline: AWS refuses a step
    Given AWS refuses every "<operation>" call of "<service>" with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And at least 1 printed line starts with "FAILED   <what>"
    And the output holds "the clean-up is NOT complete"
    And the output holds "removed  <later>"

    Examples:
      | service  | operation      | what                          | later                           |
      | lambda   | DeleteFunction | function recipe-ana-consumer  | stream recipe-ana-updates       |
      | sqs      | DeleteQueue    | queue recipe-ana-consumer-failures | table recipe-ana-updates   |
      | kinesis  | DeleteStream   | stream recipe-ana-updates     | function recipe-ana-history     |
      | dynamodb | DescribeTable  | table recipe-ana-updates      | stream recipe-ana-updates       |

  @R63
  Scenario: A teammate's resources stay
    Given the stream "recipe-bo-updates", the table "recipe-ana-updates-archive" and the queue "recipe-bo-consumer-failures" of other owners exist
    When clean_up.py runs
    Then the exit code is 0
    And the account has exactly the stream "recipe-bo-updates", the table "recipe-ana-updates-archive" and the queue "recipe-bo-consumer-failures" left

  @R64
  Scenario: Resources that an earlier clean-up is still deleting
    Given the stream, the tables and the event source mappings are being deleted
    When clean_up.py runs
    Then the exit code is 0
    And no call was made to delete the stream, a table or a mapping again
    And 9 printed lines hold "(already being deleted)"
