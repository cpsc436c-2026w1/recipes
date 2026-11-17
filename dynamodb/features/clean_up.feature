Feature: Clean-up removes what the recipe made, and nothing else
  clean_up.sh deletes the three tables, the Athena table and database of ext-nightly-report and, with BUCKET, the
  export and the Athena results in S3. README, "Requirements": R40 to R48.

  @R40
  Scenario: What a clean-up deletes
    Given the fake aws says every table is "ACTIVE"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And 1 call holds "delete-table --table-name recipe-trips "
    And 1 call holds "delete-table --table-name recipe-rider-trips "
    And 1 call holds "delete-table --table-name recipe-drivers "
    And 3 calls hold "dynamodb delete-table"
    And 1 call holds "glue delete-table --database-name recipe_lake --name trips_export"
    And 1 call holds "glue delete-database --name recipe_lake"

  @R41
  Scenario: With BUCKET, the export and the Athena results go too
    When "clean_up.sh" runs against the fake aws with BUCKET "my-bucket"
    Then the exit code is 0
    And 1 call holds "s3 rm --recursive --quiet s3://my-bucket/recipe/exports/"
    And 1 call holds "s3 rm --recursive --quiet s3://my-bucket/recipe/athena-results/"
    And 2 calls hold "s3 rm"

  @R42
  Scenario: Without BUCKET nothing in S3 is touched, and the clean-up says so
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And no call holds "s3 "
    And the output holds "BUCKET is not set, so nothing was deleted from S3"
    And the output holds "recipe/exports/"

  @R43
  Scenario Outline: AWS refuses a step
    Given the fake aws says every table is "ACTIVE"
    And the fake aws refuses "<step>" with "<code>"
    When "clean_up.sh" runs against the fake aws with BUCKET "my-bucket"
    Then the exit code is 1
    And the error output holds "FAILED: <what> was not deleted"
    And the error output holds "The clean-up is NOT complete"
    And the output does not hold "<what> not found"
    And every other step of the clean-up was still called

    Examples:
      | step                                   | code                    | what                                              |
      | delete-table --table-name recipe-trips | AccessDeniedException   | table recipe-trips                                |
      | glue delete-database                   | AccessDeniedException   | database recipe_lake                              |
      | recipe/exports/                        | AccessDenied            | the export in s3://my-bucket/recipe/exports/      |
      | delete-table --table-name recipe-trips | ResourceInUseException  | table recipe-trips                                |

  @R44
  Scenario Outline: What is already gone is not a failure
    Given the fake aws refuses "<step>" with "<code>"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And <n> printed lines hold "not found: already gone"
    And the error output does not hold "FAILED"

    Examples:
      | step                 | code                      | n |
      | dynamodb delete-     | ResourceNotFoundException | 3 |
      | glue delete-         | EntityNotFoundException   | 2 |

  @R45
  Scenario: A table that an earlier clean-up is still deleting
    Given the fake aws says every table is "DELETING"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And no call holds "dynamodb delete-table"
    And 3 calls hold "wait table-not-exists"
    And 3 printed lines hold "already being deleted"

  @R46
  Scenario: Each deleted table is waited out
    Given the fake aws says every table is "ACTIVE"
    When "clean_up.sh" runs against the fake aws
    Then the call after each "dynamodb delete-table" is a "wait table-not-exists" for the same table

  @R47
  Scenario Outline: Something of this recipe is still there after the deletes
    Given the fake aws answers the last check "<check>" with "<name>"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 1
    And the error output holds "FAILED: still there: <what> <name>"

    Examples:
      | check         | name           | what     |
      | list-tables   | recipe-drivers | table    |
      | get-databases | recipe_lake    | database |

  @R47
  Scenario: The last checks ask for the exact names of this PREFIX
    When "clean_up.sh" runs against the fake aws
    Then 1 call holds "@=='recipe-trips' || @=='recipe-rider-trips' || @=='recipe-drivers'"
    And 1 call holds "DatabaseList[?Name=='recipe_lake']"
    And the output holds "(no table left)"
    And the output holds "(no database left)"
