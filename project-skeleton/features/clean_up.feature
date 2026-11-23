Feature: Clean-up removes what the three representatives created, and nothing else
  clean_up.py, started as the command line starts it, inside moto. README, "Requirements": R30 to R37.

  Background:
    Given everything the three representatives create exists for PREFIX "recipe-ana"

  @R30
  Scenario: Clean-up after the runs
    When clean_up.py runs
    Then the exit code is 0
    And the account has nothing of the recipe
    And the folder ".local" is gone
    And no printed line starts with "FAILED"

  @R37
  Scenario: The table's system backup
    When clean_up.py runs
    Then the output holds "removed  DynamoDB table recipe-ana-updates (DynamoDB keeps a system backup of it for 35 days, at no cost)"

  @R31
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then the exit code is 0
    And the second run printed no line that starts with "removed" or "FAILED"

  @R32
  Scenario Outline: AWS refuses a step
    Given AWS refuses every "<operation>" call with "AccessDeniedException"
    When clean_up.py runs
    Then the exit code is 1
    And at least 1 printed line starts with "FAILED   <what>"
    And the output holds "the clean-up is NOT complete"
    And the output holds "removed  <later>"

    Examples:
      | operation            | what                                | later                               |
      | DeleteDeliveryStream | Firehose stream recipe-ana-to-lake  | Kinesis stream recipe-ana-updates   |
      | DeleteTable          | DynamoDB table recipe-ana-updates   | IAM role recipe-ana-api-role        |
      | DeleteBucket         | S3 bucket recipe-ana-lake-123456789012 | IAM role recipe-ana-firehose-role |
      | DeleteFunction       | Lambda function recipe-ana-api      | Glue database recipe_ana_lake       |

  @R33
  Scenario: A teammate's resources stay
    Given the stream "recipe-bo-updates", the table "recipe-ana-updates-archive" and the bucket "recipe-bo-lake-123456789012" of other owners exist
    When clean_up.py runs
    Then the exit code is 0
    And the account has exactly the stream "recipe-bo-updates", the table "recipe-ana-updates-archive" and the bucket "recipe-bo-lake-123456789012" left

  @R34
  Scenario: A role the recipe did not create
    Given the role "recipe-ana-api-role" does not carry the tag "recipe" = "project-skeleton"
    When clean_up.py runs
    Then the exit code is 0
    And the output holds "absent   IAM role recipe-ana-api-role (kept: not created by this recipe)"
    And the account still has the role "recipe-ana-api-role"

  @R35
  Scenario: Resources that an earlier clean-up is still deleting
    Given the stream, the Firehose stream and the table are being deleted
    When clean_up.py runs
    Then the exit code is 0
    And no call was made to delete the stream, the Firehose stream or the table again
    And 3 printed lines hold "(already being deleted)"

  @R36
  Scenario: Only the offline files
    When clean_up.py runs with "--local --yes"
    Then the exit code is 0
    And the folder ".local" is gone
    And no call was made to AWS
    And the account still has the stream "recipe-ana-updates"
