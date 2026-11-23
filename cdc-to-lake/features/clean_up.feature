Feature: Clean-up removes what the recipe and its extensions created, and nothing else
  clean_up.py, run as the command line runs it, inside moto. README, "Requirements": R50 to R57.

  Background:
    Given everything a run and its three extensions create exists for PREFIX "recipe-ana"

  @R50
  Scenario: Clean-up after a run
    When clean_up.py runs with "--yes"
    Then the exit code is 0
    And the account has no schedule, alarm, mapping, function, log group, queue, Firehose stream, Glue database, bucket, table or role left
    And the output holds "resources removed for PREFIX=recipe-ana in ca-central-1"

  @R51
  Scenario: A dry run deletes nothing
    Given the account is noted
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And the account is as it was noted
    And at least 10 printed lines start with "would remove "
    And no call whose name starts with "Delete" was made

  @R52
  Scenario Outline: AWS refuses a step
    Given AWS refuses every "<operation>" call with "AccessDeniedException"
    When clean_up.py runs with "--yes"
    Then the exit code is 1
    And 1 printed line starts with "FAILED   <what>"
    And the output holds "the clean-up is NOT complete"
    And the output holds "removed  <later>"

    Examples:
      | operation            | what                                       | later                               |
      | DeleteSchedule       | schedule recipe-ana-lake-optimize          | DynamoDB table recipe-ana-trips     |
      | DeleteFunction       | Lambda function recipe-ana-cdc-to-firehose | Firehose stream recipe-ana-trip-changes |
      | DeleteDeliveryStream | Firehose stream recipe-ana-trip-changes    | IAM role recipe-ana-cdc-lambda-role |
      | HeadBucket           | bucket recipe-ana-cdc-lake-123456789012    | DynamoDB table recipe-ana-trips     |

  @R53
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs with "--yes"
    And clean_up.py runs with "--yes"
    Then the exit code is 0
    And the output holds "0 resources removed for PREFIX=recipe-ana"
    And the second run printed no line that starts with "FAILED"

  @R54
  Scenario: What is not the recipe's stays
    Given the alarm "recipe-ana-cdc-p99" and the role "recipe-ana-cdc-lambda-role-2" of a teammate exist
    And the role "recipe-ana-cdc-scheduler-role" does not carry the recipe's tag
    When clean_up.py runs with "--yes"
    Then the exit code is 0
    And the only alarm left is "recipe-ana-cdc-p99"
    And the roles left are "recipe-ana-cdc-lambda-role-2, recipe-ana-cdc-scheduler-role"

  @R54
  Scenario: A bucket that was passed in is emptied of the recipe's prefixes only
    Given the recipe's objects are in the bucket "team-shared-bucket", passed in as BUCKET, beside the object "reports/q3.csv"
    When clean_up.py runs with "--yes"
    Then the exit code is 0
    And the bucket "team-shared-bucket" is still there, with only the object "reports/q3.csv"

  @R55
  Scenario: Resources that an earlier clean-up is still deleting
    Given the table, the Firehose stream and the event source mapping are being deleted
    When clean_up.py runs with "--yes"
    Then the exit code is 0
    And no call was made to delete the table, the Firehose stream or the mapping again

  @R56
  Scenario: S3 does not delete some objects
    Given S3 answers that it could not delete 1 object under "lake/"
    When clean_up.py runs with "--yes"
    Then the exit code is 1
    And 1 printed line starts with "FAILED   "
    And the output holds "objects not deleted"
    And the bucket "recipe-ana-cdc-lake-123456789012" is still there
