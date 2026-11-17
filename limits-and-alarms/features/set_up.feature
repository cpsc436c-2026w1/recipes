Feature: Limits outside the app's code, and the run that sets them up
  main/run.py --target aws as the command line runs it, against moto. README, "Requirements": R20 to R28, R40, R41,
  R43. GetAccountSettings, which moto lacks, answers a total of 400 with 200 unreserved unless a scenario says
  otherwise; Invoke runs the handler in-process.

  @R20
  Scenario: A monthly budget with four alerts to the alarm topic
    When main/run.py runs with "--target aws"
    Then the exit code is 0
    And the budget "test-monthly" is 25.00 USD a month, of cost
    And the budget's alerts are "ACTUAL 50, ACTUAL 80, ACTUAL 100, FORECASTED 100", each sent to the topic "test-limits-alarms"

  @R21
  Scenario: The function has a timeout of 30 seconds
    When main/run.py runs with "--target aws"
    Then the function "test-app" has a timeout of 30 seconds

  @R22 @R23
  Scenario Outline: Reserved concurrency is set only when 100 units stay unreserved
    Given the account reports a total of 400 with <unreserved> unreserved
    When main/run.py runs with "--target aws"
    Then the exit code is 0
    And the printed output holds "<line>"
    And the reserved concurrency of "test-app" is <reserved>

    Examples:
      | unreserved | line                                | reserved |
      | 105        | reserved concurrency 5 on test-app  | 5        |
      | 104        | reserved concurrency NOT set        | none     |

  @R24
  Scenario Outline: A function, a budget, a role or a topic with the recipe's name and without its tag is left alone
    Given the team has its own <kind> "<name>" without the tag
    When main/run.py runs with "--target aws"
    Then the exit code is 1
    And the error output holds "<name> exists but does not carry the tag recipe=limits-and-alarms"
    And <untouched>

    Examples:
      | kind            | name         | untouched                          |
      | lambda function | test-app     | UpdateFunctionCode was not called  |
      | budget          | test-monthly | CreateBudget was not called        |
      | iam role        | test-app-role | CreateFunction was not called     |
      | sns topic       | test-limits-alarms | SetTopicAttributes was not called |

  @R25
  Scenario: The role the run creates may write only the function's own log group
    When main/run.py runs with "--target aws"
    Then the role "test-app-role" may be assumed only by "lambda.amazonaws.com"
    And the role's only permissions are "logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents" on the log group "/aws/lambda/test-app"
    And the function "test-app" runs with the role "test-app-role"

  @R26
  Scenario: A role named in LAMBDA_ROLE_ARN is used, and no role is created
    Given the team's role "team-lambda-role" exists and LAMBDA_ROLE_ARN names it
    When main/run.py runs with "--target aws"
    Then the exit code is 0
    And CreateRole was not called
    And the function "test-app" runs with the role "team-lambda-role"

  @R27
  Scenario: The run sends twelve requests, two of them meant to fail
    When main/run.py runs with "--target aws"
    Then the function was invoked 12 times
    And the invocations wrote 12 metric lines whose Errors add up to 2
    And the printed output holds "sent 12 requests (2 failing)"

  @R28
  Scenario: A function that breaks on every request stops the run
    Given every Invoke of the function ends with FunctionError "Unhandled"
    When main/run.py runs with "--target aws"
    Then the exit code is 1
    And the error output holds "12 of 12 requests broke the function test-app and wrote no metric line"

  @R40 @R43
  Scenario: Every resource name starts with PREFIX, and the recipe's tag is on each
    Given PREFIX is "recipe-ana"
    When main/run.py runs with "--target aws"
    Then the alarms, the dashboard, the function, the topic, the budget and the role all start with "recipe-ana-"
    And the function, the topic, the budget and the role carry the tag "recipe=limits-and-alarms"

  @R41
  Scenario Outline: The Region of every call
    Given AWS_REGION is "<region>"
    When main/run.py runs with "--target aws"
    Then every Lambda, CloudWatch and SNS call went to "<expected>"
    And every AWS Budgets call went to its one endpoint, "aws-global"

    Examples:
      | region    | expected     |
      | not set   | ca-central-1 |
      | us-west-2 | us-west-2    |
