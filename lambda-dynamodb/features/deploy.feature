Feature: Deploying the API: main/run.sh
  The script runs dry, or for real against a stand-in `aws` that logs each call and answers as the scenario says;
  `curl` and `sleep` write down their arguments and do nothing. README, "Requirements": R20 to R28, R30 to R33, R40 to R43,
  R50, R51.

  @R20
  Scenario: The function the script creates
    When "main/run.sh" runs against the fake aws
    Then the exit code is 0
    And 1 call holds "lambda create-function "
    And the call that holds "lambda create-function " holds each of "--function-name recipe-api, --runtime python3.12, --architectures arm64, --memory-size 128, --timeout 10, --handler handler.lambda_handler, Variables={PREFIX=recipe}"
    And the zip the script made holds only "handler.py", the same bytes as main/handler.py
    And the call "lambda create-function " comes before the call "lambda wait function-active-v2"

  @R21
  Scenario: What the function's role may do
    When "main/run.sh" runs against the fake aws with PREFIX "recipe-ana"
    Then the role's policy allows on DynamoDB only the actions "dynamodb:GetItem, dynamodb:Query, dynamodb:UpdateItem"
    And the role's policy names on DynamoDB only the tables "recipe-ana-trips, recipe-ana-rider-trips, recipe-ana-drivers"
    And the role's policy allows on logs only the log group "/aws/lambda/recipe-ana-api"
    And the role's policy has no action and no resource that is "*"
    And the role's trust policy lets only "lambda.amazonaws.com" use it

  @R22
  Scenario: The two statements a public URL needs
    When "main/run.sh" runs against the fake aws
    Then 1 call holds "create-function-url-config --function-name recipe-api --auth-type NONE"
    And 1 call holds "add-permission --function-name recipe-api --statement-id url-public --action lambda:InvokeFunctionUrl --principal * --function-url-auth-type NONE"
    And 1 call holds "add-permission --function-name recipe-api --statement-id url-invoke --action lambda:InvokeFunction --principal * --invoked-via-function-url"
    And 2 calls hold "add-permission"

  @R43
  Scenario: Step 1 runs the handler in the terminal, before anything is created
    When "main/run.sh" runs against the fake aws with PREFIX "recipe-ana"
    Then python3 ran main/handler.py once, before the first call that creates something
    And the output of step "1" holds "$ env PREFIX=recipe-ana python3 "

  @R23
  Scenario: The five requests an app would send
    When "main/run.sh" runs against the fake aws
    Then curl was called 5 times, each with a POST to "https://abc123.lambda-url.ca-central-1.on.aws/"
    And the actions of the curl calls are "get, rider, claim, accept, delete-everything"

  @R24
  Scenario: What each request was billed
    When "main/run.sh" runs against the fake aws
    Then the call "lambda add-permission" comes before the call "logs filter-log-events"
    And the call that holds "logs filter-log-events" holds each of "--log-group-name /aws/lambda/recipe-api, --filter-pattern "REPORT RequestId""

  @R25
  Scenario: A run without KEEP=1 removes the API, the URL first
    When "main/run.sh" runs against the fake aws
    Then the deletes are, in this order, "lambda delete-function-url-config, lambda delete-function, logs delete-log-group, iam delete-role-policy, iam delete-role"
    And no call holds "dynamodb delete"

  @R26
  Scenario: KEEP=1 leaves the API up and names the clean-up
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And no call holds "delete-"
    And the output holds "KEEP=1: the API stays up and public. Remove it with: bash clean_up.sh"

  @R27 @R28
  Scenario: With ROLE_ARN the function runs as that role, and no role is made or removed
    Given ROLE_ARN is "arn:aws:iam::111122223333:role/team-lambda"
    When "main/run.sh" runs against the fake aws
    Then the exit code is 0
    And the call that holds "lambda create-function " holds each of "--role arn:aws:iam::111122223333:role/team-lambda"
    And no call holds "iam "
    And the output holds "(ROLE_ARN is set: your own role stays)"

  @R30 @R31
  Scenario: A second run after KEEP=1 replaces the code and keeps the rest
    Given the fake aws says the API of an earlier run is there
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And no call holds "lambda create-function "
    And the call "lambda update-function-code --function-name recipe-api" comes before the call "lambda wait function-updated-v2"
    And 5 printed lines hold "already there from an earlier run: kept"

  @R32
  Scenario: Lambda refuses a create while another change is in progress, twice
    Given the fake aws refuses the first 2 add-permission calls as in progress
    When "main/run.sh" runs against the fake aws
    Then the exit code is 0
    And 4 calls hold "add-permission"
    And 2 printed lines hold "in progress: trying again"
    And the recipe waited 5 seconds before each of the 2 new attempts

  @R32
  Scenario: The change stays in progress
    Given the fake aws refuses the first 99 add-permission calls as in progress
    When "main/run.sh" runs against the fake aws
    Then the exit code is not 0
    And 6 calls hold "add-permission"
    And the error output holds "An update is in progress"
    And the output does not hold "already there"
    And no call holds "logs filter-log-events"

  @R33
  Scenario Outline: A create that AWS refuses for another reason stops the run
    Given the fake aws refuses "<call>" with "<code>" and the message "<message>"
    When "main/run.sh" runs against the fake aws
    Then the exit code is not 0
    And the error output holds "<message>"
    And the output does not hold "already there"
    And no call holds "<next>"

    Examples:
      | call                       | code                      | message                 | next                     |
      | iam create-role            | AccessDeniedException     | not allowed             | lambda create-function   |
      | create-function-url-config | ResourceConflictException | something else is wrong | add-permission           |
      | lambda create-function     | InvalidParameterValueException | the role cannot be assumed | create-function-url-config |

  @R40
  Scenario Outline: A table of the dynamodb recipe is not there
    Given the fake aws says the tables of the dynamodb recipe are not found
    When "<script>" runs against the fake aws
    Then the exit code is 1
    And the error output holds "Table recipe-trips not found. Run KEEP=1 bash ../dynamodb/main/run.sh first."
    And the only call is a describe-table of "recipe-trips"

    Examples:
      | script      |
      | main/run.sh |

  @R41
  Scenario: The look for a table is refused
    Given the fake aws refuses "describe-table" with "AccessDeniedException" and the message "not allowed to describe"
    When "main/run.sh" runs against the fake aws
    Then the exit code is 1
    And the error output holds "AccessDeniedException"
    And the error output holds "FAILED: could not look for table recipe-trips"
    And the error output does not hold "Run KEEP=1"
    And the only call is a describe-table of "recipe-trips"

  @R42
  Scenario Outline: DRY=1 prints the commands and calls nothing
    When "<script>" runs with DRY=1
    Then the exit code is 0
    And neither aws nor curl was called
    And at least <n> commands that start with "aws " are printed

    Examples:
      | script              | n  |
      | main/run.sh         | 12 |
      | ext-redeploy/run.sh | 3  |
      | ext-metrics/run.sh  | 4  |
      | clean_up.sh         | 8  |

  @R42
  Scenario: A dry run prints the five requests of step 8 with <url> in place of the address
    When "main/run.sh" runs with DRY=1
    Then neither aws nor curl was called
    And step "8" prints 5 commands, each "curl -X POST <url> -d" with one of the actions "get, rider, claim, accept, delete-everything"
    And the steps "1, 4, 5, 6, 7, 8, 9, 10" are all printed

  @R50
  Scenario: Every name starts with PREFIX
    When "main/run.sh" runs against the fake aws with PREFIX "recipe-ana"
    Then every function, role and log group the calls name is one of "recipe-ana-api, recipe-ana-api-role, /aws/lambda/recipe-ana-api"

  @R51
  Scenario Outline: The Region of every call and of every name in the policy
    Given AWS_REGION is "<set>"
    When "main/run.sh" runs against the fake aws
    Then every call to aws was made with AWS_REGION "<used>"
    And every ARN in the role's policy is in the Region "<used>"

    Examples:
      | set       | used         |
      | not set   | ca-central-1 |
      | ca-west-1 | ca-west-1    |
