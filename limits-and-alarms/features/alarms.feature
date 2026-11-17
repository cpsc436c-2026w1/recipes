Feature: Three alarms, one topic, one dashboard, and the fire test
  main/run.py --target aws and main/fire.py, against moto. README, "Requirements": R30 to R39. moto stores alarms
  and does not evaluate them: whether a condition fires is checked offline by main/alarm_eval.py (local_runs.feature),
  and fire.py's waits return at once here.

  @R30 @R31 @R39
  Scenario: Five alarms that notify one topic
    When main/run.py runs with "--target aws"
    Then the alarms of the account are "test-app-daily-cost, test-app-errors, test-app-lambda-errors, test-app-lambda-throttles, test-app-p95-latency"
    And every alarm notifies the topic "test-limits-alarms" on ALARM and on OK
    And every alarm treats missing data as "notBreaching"
    And the alarms have these settings:
      | alarm                | metric    | statistic | comparison                     | threshold | period | periods | to alarm |
      | test-app-errors      | Errors    | Sum       | GreaterThanOrEqualToThreshold  | 5         | 300    | 1       | 1        |
      | test-app-p95-latency | LatencyMs | p95       | GreaterThanThreshold           | 5000      | 60     | 3       | 2        |
      | test-app-daily-cost  | CostCAD   | Sum       | GreaterThanThreshold           | 0.5       | 86400  | 1       | 1        |

  @R39
  Scenario: Two alarms on Lambda's own metrics of the function
    When main/run.py runs with "--target aws"
    Then the alarms have these settings:
      | alarm                     | metric    | statistic | comparison                    | threshold | period | periods | to alarm |
      | test-app-lambda-errors    | Errors    | Sum       | GreaterThanOrEqualToThreshold | 1         | 300    | 1       | 1        |
      | test-app-lambda-throttles | Throttles | Sum       | GreaterThanOrEqualToThreshold | 1         | 300    | 1       | 1        |
    And the alarms "test-app-lambda-errors, test-app-lambda-throttles" watch the namespace "AWS/Lambda" with FunctionName "test-app"

  @R33
  Scenario: One dashboard with the metrics, the throttles, the loop's stop reasons and the alarms
    When main/run.py runs with "--target aws"
    Then the dashboard "test-app" has the widgets "Requests and errors (5 min); Latency p50 and p95 (ms, 1 min); Model spend (CAD per hour); Lambda throttles and concurrency; Loop tasks by stop reason (per hour); Alarms"
    And the stop-reason widget has one line for each of "done, step_cap, deadline, cost_cap, budget_exhausted, rate_limited"
    And the dashboard's alarm widget shows the three alarms

  @R34
  Scenario: The fire test sets each alarm to ALARM and back
    Given a run with "--target aws" has set up everything for PREFIX "test"
    And the alarm history of each alarm shows its SNS action
    When main/fire.py runs with "--target aws"
    Then the exit code is 0
    And each alarm was set to "ALARM" and then to "OK"
    And the printed output has 5 lines with "state ALARM  SNS action sent"

  @R35
  Scenario: The synthetic fire test sends breaching datapoints for three minutes
    Given a run with "--target aws" has set up everything for PREFIX "test"
    When main/fire.py runs with "--target aws --synthetic"
    Then the exit code is 0
    And PutMetricData was called 3 times, 60 seconds apart
    And each PutMetricData call sent Errors 5 and 20 LatencyMs values above 10000 in "test/app"
    And the printed output holds "test-app-errors"
    And the printed output holds "test-app-p95-latency"

  @R36
  Scenario: Only CloudWatch and AWS Budgets of this account may publish to the topic
    When main/run.py runs with "--target aws"
    Then the topic's policy lets "budgets.amazonaws.com, cloudwatch.amazonaws.com" publish, each only for this account

  @R37
  Scenario Outline: An email address for the alarms
    Given ALARM_EMAIL is "<email>"
    When main/run.py runs with "--target aws"
    Then the topic has <n> email subscriptions

    Examples:
      | email           | n |
      | ana@example.com | 1 |
      | not set         | 0 |
