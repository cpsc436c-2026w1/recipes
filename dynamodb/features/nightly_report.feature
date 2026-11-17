Feature: Nightly report from an export
  ext-nightly-report/run.sh. Its export and its Athena queries have no local stand-in, so these scenarios read
  the commands of a dry run and the calls of a run against the fake aws.
  ext-nightly-report/README.md, "Requirements": R60 to R65.

  @R60
  Scenario: Point-in-time recovery is on before the export starts
    Given the fake aws says every table is "ACTIVE"
    When "ext-nightly-report/run.sh" runs against the fake aws with BUCKET "my-bucket" and KEEP=1
    Then the exit code is 0
    And the call "update-continuous-backups --table-name recipe-trips --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true" comes before the call "export-table-to-point-in-time"

  @R61
  Scenario: Where the export and the Athena results go
    Given the fake aws says every table is "ACTIVE"
    When "ext-nightly-report/run.sh" runs against the fake aws with BUCKET "my-bucket" and KEEP=1
    Then 1 call holds "export-table-to-point-in-time"
    And 1 call holds "--s3-bucket my-bucket --s3-prefix recipe/exports "
    And every call that holds "athena start-query-execution" holds "OutputLocation=s3://my-bucket/recipe/athena-results/"
    And 4 calls hold "athena start-query-execution"

  @R62
  Scenario: Without BUCKET the extension stops before any call
    When "ext-nightly-report/run.sh" runs against the fake aws
    Then the exit code is not 0
    And the error output holds "set BUCKET to an S3 bucket you own, in ca-central-1"
    And aws was called 0 times

  @R63
  Scenario: The Athena database carries the PREFIX
    When "ext-nightly-report/run.sh" runs with DRY=1 and PREFIX "recipe-ana"
    Then the output holds "$ athena: CREATE DATABASE IF NOT EXISTS recipe_ana_lake"
    And the output holds "FROM recipe_ana_lake.trips_export GROUP BY 1 ORDER BY 1"
    And 1 printed command holds "aws glue delete-database --name recipe_ana_lake"

  @R65
  Scenario Outline: An export that does not complete
    Given the fake aws says every table is "ACTIVE"
    And the fake aws ends the export with the status "<status>"
    When "ext-nightly-report/run.sh" runs against the fake aws with BUCKET "my-bucket"
    Then the exit code is 1
    And the error output holds "FAILED: the export ended with status <status>, not COMPLETED"
    And the error output holds "BUCKET=my-bucket bash clean_up.sh"
    And no call holds "athena start-query-execution"
    And no call holds "delete-table"

    Examples:
      | status |
      | FAILED |

  @R64
  Scenario Outline: An Athena query that does not succeed
    Given the fake aws says every table is "ACTIVE"
    And the fake aws ends every Athena query in the state "<state>"
    When "ext-nightly-report/run.sh" runs against the fake aws with BUCKET "my-bucket"
    Then the exit code is 1
    And the error output holds "FAILED: the Athena query ended in state <state>"
    And the error output holds "BUCKET=my-bucket bash clean_up.sh"
    And 1 call holds "athena start-query-execution"
    And no call holds "delete-table"

    Examples:
      | state     |
      | FAILED    |
      | CANCELLED |
