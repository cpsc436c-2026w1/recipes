Feature: Running the scripts: dry runs, a second run, a missing table
  README, "Requirements": R17, R19, R20 to R29, R30 to R33. No scenario here needs the AWS CLI: a dry run has an `aws` that
  fails the scenario when called, the other runs an `aws` that logs each call and answers as the scenario says.

  @R20
  Scenario Outline: DRY=1 prints the commands and calls nothing
    When "<script>" runs with DRY=1 and BUCKET "my-bucket"
    Then the exit code is 0
    And aws was not called
    And at least <n> commands that start with "aws " are printed
    And each of the steps "<steps>" is printed with at least one command under it

    Examples:
      | script                        | n  | steps                               |
      | main/run.sh                   | 13 | 1, 2, 3, 4, 5, 6, 7, 8, 9           |
      | ext-riders-and-drivers/run.sh | 17 | 1, 2, 3, 3b, 4, 5, 6, 7, 8, 9, 10   |
      | ext-nightly-report/run.sh     | 5  | 1, 2, 3, 4, 5, 6, 7                 |

  @R20
  Scenario: DRY=1 on the clean-up
    When "clean_up.sh" runs with DRY=1 and BUCKET "my-bucket"
    Then the exit code is 0
    And aws was not called
    And at least 9 commands that start with "aws " are printed

  @R21
  Scenario: Each command of the scenario is printed before it runs
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then every call to aws other than the looks at the table and the waits is one of the printed commands
    And the printed commands are in the order of the calls

  @R21
  Scenario: The heading of the strong read says what the README says
    When "main/run.sh" runs with DRY=1
    Then the heading of step "4" holds "it reflects every write that succeeded before it"

  @R17
  Scenario Outline: A conditional write that fails for another reason is not called refused
    Given the fake aws says every table is "ACTIVE"
    And the fake aws refuses "<call>" with "<code>" from its call number <nth> on
    When "<script>" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And the error output holds "<code>"
    And the error output holds "FAILED: the write failed, and not because its condition refused it"
    And the output does not hold "<not said>"
    And no call holds "delete-table"

    Examples:
      | script                        | call                 | nth | code                                    | not said                    |
      | main/run.sh                   | update-item          | 2   | ProvisionedThroughputExceededException  | refused: d-7 keeps the trip |
      | ext-riders-and-drivers/run.sh | transact-write-items | 4   | ThrottlingException                     | cancelled: one reason       |
      | ext-riders-and-drivers/run.sh | update-item          | 3   | AccessDeniedException                   | refused: the version moved  |
  @R19
  Scenario: DynamoDB returns one item of the first load call as unprocessed
    Given the fake aws returns an unprocessed item from the first 1 load calls
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And 3 calls hold "batch-write-item"
    And load call 2 sends only the item that load call 1 returned as unprocessed
    And the output holds "sending them again in 1 s, call 2 of 4"
    And the recipe waited "1" seconds between the load calls of a batch

  @R19
  Scenario: DynamoDB keeps returning an item as unprocessed
    Given the fake aws returns an unprocessed item from every load call
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And 4 calls hold "batch-write-item"
    And the error output holds "FAILED: DynamoDB still returned unprocessed items after 4 calls"
    And no call holds "get-item"
    And the recipe waited "1, 2, 4" seconds between the load calls of a batch

  @R22
  Scenario: A run without KEEP=1 ends with the clean-up
    When "main/run.sh" runs with DRY=1
    Then the last printed commands delete the tables "recipe-trips, recipe-rider-trips, recipe-drivers" and check that none is left

  @R23
  Scenario: KEEP=1 leaves the tables and names the clean-up
    When "main/run.sh" runs with DRY=1 and KEEP=1
    Then no printed command holds "delete-table"
    And the output holds "bash clean_up.sh"

  @R24
  Scenario: A run right after a clean-up
    Given the fake aws says every table is "DELETING"
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And the call "wait table-not-exists --table-name recipe-trips" comes before the call "create-table --table-name recipe-trips"

  @R25
  Scenario: A second run
    Given the fake aws says every table is "ACTIVE"
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And no call holds "create-table"
    And the output holds "already there: kept as it is"
    And 2 calls hold "batch-write-item"

  @R26
  Scenario Outline: An extension without the table of main
    When "<script>" runs against the fake aws with BUCKET "my-bucket"
    Then the exit code is 1
    And the error output holds "Table recipe-trips not found"
    And the error output holds "KEEP=1 bash main/run.sh"
    And the only call is a describe-table of "recipe-trips"

    Examples:
      | script                        |
      | ext-riders-and-drivers/run.sh |
      | ext-nightly-report/run.sh     |

  @R27
  Scenario: The look for the table of main is refused
    Given the fake aws refuses "describe-table" with "AccessDeniedException"
    When "ext-riders-and-drivers/run.sh" runs against the fake aws
    Then the exit code is 1
    And the error output holds "AccessDeniedException"
    And the error output holds "FAILED: could not look for table recipe-trips"
    And the error output does not hold "Run main first"
    And the only call is a describe-table of "recipe-trips"

  @R27
  Scenario: The look for a table to create is refused
    Given the fake aws refuses "describe-table" with "AccessDeniedException"
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And the error output holds "AccessDeniedException"
    And the error output holds "FAILED: could not look for table recipe-trips"
    And no call holds "create-table"

  @R28
  Scenario Outline: An extension's run ends with the clean-up of the whole recipe
    When "<script>" runs with DRY=1 and BUCKET "my-bucket"
    Then the printed commands end with the deletes of the tables "recipe-trips, recipe-rider-trips, recipe-drivers" and the two last checks
    And 1 printed command holds "aws glue delete-database --name recipe_lake"

    Examples:
      | script                        |
      | ext-riders-and-drivers/run.sh |
      | ext-nightly-report/run.sh     |

  @R29
  Scenario Outline: An AWS CLI that does not know the error format
    Given the fake aws says every table is "ACTIVE"
    And the fake aws refuses "configure list --cli-error-format json" with "UnknownOptions"
    When "<script>" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And the error output holds "does not know --cli-error-format"
    And the error output holds "Update the AWS CLI"
    And no call holds "create-table"
    And no call holds "batch-write-item"

    Examples:
      | script                        |
      | main/run.sh                   |
      | ext-riders-and-drivers/run.sh |

  @R30
  Scenario Outline: Every table name starts with PREFIX
    When "<script>" runs with DRY=1 and PREFIX "<prefix>"
    Then every table the printed commands name starts with "<start>"

    Examples:
      | script                        | prefix     | start       |
      | main/run.sh                   | recipe-ana | recipe-ana- |
      | ext-riders-and-drivers/run.sh | recipe-ana | recipe-ana- |
      | ext-nightly-report/run.sh     | recipe-ana | recipe-ana- |
      | clean_up.sh                   | recipe-ana | recipe-ana- |
      | main/run.sh                   |            | recipe-     |

  @R32
  Scenario Outline: Every table and index has a ceiling on its throughput
    When "<script>" runs with DRY=1
    Then <n> printed commands hold "aws dynamodb create-table"
    And every printed command that holds "aws dynamodb create-table" holds "--on-demand-throughput MaxReadRequestUnits=100,MaxWriteRequestUnits=100 "
    And every index in a printed create-table command has a maximum of 100 read and 100 write request units

    Examples:
      | script                        | n |
      | main/run.sh                   | 1 |
      | ext-riders-and-drivers/run.sh | 2 |

  @R32
  Scenario: MAX_UNITS names another ceiling
    Given MAX_UNITS is "40"
    When "main/run.sh" runs with DRY=1
    Then every printed command that holds "aws dynamodb create-table" holds "--on-demand-throughput MaxReadRequestUnits=40,MaxWriteRequestUnits=40 "
    And every index in a printed create-table command has a maximum of 40 read and 40 write request units

  @R33
  Scenario Outline: Every table carries the two tags
    When "<script>" runs with DRY=1 and PREFIX "recipe-ana"
    Then every printed command that holds "aws dynamodb create-table" holds "--tags Key=purpose,Value=recipe Key=prefix,Value=recipe-ana "

    Examples:
      | script                        |
      | main/run.sh                   |
      | ext-riders-and-drivers/run.sh |

  @R31
  Scenario Outline: The Region of every call
    Given AWS_REGION is "<set>"
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then every call to aws was made with AWS_REGION "<used>"

    Examples:
      | set          | used         |
      | not set      | ca-central-1 |
      | ca-west-1    | ca-west-1    |
