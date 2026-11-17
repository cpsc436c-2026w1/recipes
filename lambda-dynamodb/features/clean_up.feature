Feature: Clean-up removes the API and leaves the tables
  clean_up.sh, for real against the stand-in `aws`. README, "Requirements": R60 to R65.

  @R60 @R65
  Scenario: What a clean-up deletes, and in which order
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And the deletes are, in this order, "lambda delete-function-url-config, lambda delete-function, logs delete-log-group, iam delete-role-policy, iam delete-role"
    And every delete names "recipe-api", "/aws/lambda/recipe-api" or "recipe-api-role"
    And no call holds "dynamodb"

  @R61
  Scenario Outline: AWS refuses a delete
    Given the fake aws refuses "<call>" with "AccessDeniedException" and the message "not allowed"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 1
    And the error output holds "FAILED: the <what> was not deleted"
    And the error output holds "The clean-up is NOT complete"
    And the output does not hold "(no <what>: already gone)"
    And all 5 deletes and the 3 last checks were still called

    Examples:
      | call                                   | what      |
      | lambda delete-function-url-config      | URL       |
      | lambda delete-function --function-name | function  |
      | logs delete-log-group                  | log group |
      | iam delete-role --role-name            | role      |

  @R62
  Scenario Outline: What is already gone is not a failure
    Given the fake aws refuses "<call>" with "<code>" and the message "it is not there"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And <n> printed lines hold "already gone"
    And the error output does not hold "FAILED"

    Examples:
      | call          | code                      | n |
      | lambda delete | ResourceNotFoundException | 2 |
      | logs delete   | ResourceNotFoundException | 1 |
      | iam delete    | NoSuchEntity              | 2 |

  @R63
  Scenario Outline: Something of this recipe is still there after the deletes
    Given the fake aws answers the last check "<check>" with "<name>"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 1
    And the error output holds "FAILED: still there: <what> <name>"

    Examples:
      | check               | name                   | what      |
      | list-functions      | recipe-api             | function  |
      | describe-log-groups | /aws/lambda/recipe-api | log group |
      | list-roles          | recipe-api-role        | role      |

  @R63
  Scenario: The last checks ask for the exact names of this PREFIX
    When "clean_up.sh" runs against the fake aws
    Then 1 call holds "Functions[?FunctionName=='recipe-api'].FunctionName"
    And 1 call holds "logGroups[?logGroupName=='/aws/lambda/recipe-api'].logGroupName"
    And 1 call holds "Roles[?RoleName=='recipe-api-role'].RoleName"
    And the output holds "(no function left)"
    And the output holds "(no log group left)"
    And the output holds "(no role left)"

  @R64
  Scenario: With ROLE_ARN the role stays and is not looked for
    Given ROLE_ARN is "arn:aws:iam::111122223333:role/team-lambda"
    When "clean_up.sh" runs against the fake aws
    Then the exit code is 0
    And no call holds "iam "
    And the output holds "(ROLE_ARN is set: your own role stays)"
