Feature: The extensions: redeploy a changed handler, read logs and metrics
  ext-redeploy/README.md, "Requirements": R80 to R82. ext-metrics/README.md, "Requirements": R90 to R93.

  @R80
  Scenario: Redeploy over a function that is there
    Given the fake aws says the API of an earlier run is there
    When "ext-redeploy/run.sh" runs against the fake aws
    Then the exit code is 0
    And the call "lambda update-function-code --function-name recipe-api" comes before the call "lambda wait function-updated-v2 --function-name recipe-api"
    And the zip the script made holds only "handler.py", the same bytes as main/handler.py
    And no call holds "create-"
    And no call holds "add-permission"
    And no call holds "iam "

  @R81
  Scenario: Redeploy when the function is not there
    When "ext-redeploy/run.sh" runs against the fake aws
    Then the exit code is 0
    And the call that holds "lambda create-function " holds each of "--function-name recipe-api, --runtime python3.12, --architectures arm64, --memory-size 128, --timeout 10, --role arn:aws:iam::111122223333:role/recipe-api-role"
    And no call holds "update-function-code"

  @R81
  Scenario: Redeploy when the function is not there, with ROLE_ARN
    Given ROLE_ARN is "arn:aws:iam::111122223333:role/team-lambda"
    When "ext-redeploy/run.sh" runs against the fake aws
    Then the call that holds "lambda create-function " holds each of "--role arn:aws:iam::111122223333:role/team-lambda"

  @R82
  Scenario: The one call after a redeploy
    Given the fake aws says the API of an earlier run is there
    When "ext-redeploy/run.sh" runs against the fake aws
    Then the last call holds "lambda invoke --function-name recipe-api"
    And the last call holds "{"action":"get","trip_id":"t-0042","strong":true}"
    And the output holds "{"ok": true}"

  @R90
  Scenario: The function's log group
    When "ext-metrics/run.sh" runs against the fake aws
    Then the exit code is 0
    And 1 call holds "logs describe-log-groups --log-group-name-prefix /aws/lambda/recipe-api"

  @R91
  Scenario: The REPORT lines, at most eight
    When "ext-metrics/run.sh" runs against the fake aws
    Then the call that holds "logs filter-log-events" holds each of "--log-group-name /aws/lambda/recipe-api, --filter-pattern "REPORT RequestId", --max-items 8"

  @R92
  Scenario: Calls and their duration in the last hour
    When "ext-metrics/run.sh" runs against the fake aws
    Then the metrics the script asks for are "Invocations, Duration", each for the function "recipe-api"
    And each metric call covers the 3600 seconds before now, as one period

  @R93
  Scenario: The extension only reads
    When "ext-metrics/run.sh" runs against the fake aws
    Then every call is one of "logs describe-log-groups, logs filter-log-events, cloudwatch get-metric-statistics"
