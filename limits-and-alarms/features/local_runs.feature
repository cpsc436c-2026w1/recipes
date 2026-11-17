@own_moto
Feature: The local run, with moto started by the script itself
  `--target local` starts moto inside the script, so this scenario starts nothing itself, and every network
  connection is refused while it runs. README, "Requirements": R32, R38, R42.

  @R32 @R42
  Scenario: The local run checks each alarm against ten minutes of synthetic traffic
    When main/run.py runs with "--target local"
    Then the exit code is 0
    And the printed lines from "reserved concurrency" on are:
      """
      reserved concurrency 5 on test-app
      alarms test-app-errors, test-app-p95-latency, test-app-daily-cost, test-app-lambda-errors, test-app-lambda-throttles
      dashboard test-app: https://ca-central-1.console.aws.amazon.com/cloudwatch/home?region=ca-central-1#dashboards/dashboard/test-app
      alarm states on ten minutes of synthetic traffic (main/alarm_eval.py):
        traffic    errors        p95-latency   daily-cost
        normal     OK            OK            OK
        errors     ALARM         OK            OK
        slow       OK            ALARM         OK
        expensive  OK            OK            ALARM
      """

  @R38
  Scenario Outline: The offline check counts breaching minutes as the alarm does: 2 of the last 3
    Given ten minutes of traffic, 10 requests a minute, with 2 slow requests in each of the last <k> minutes
    When main/alarm_eval.py checks the p95 latency alarm against it
    Then the state is "<state>"

    Examples:
      | k | state |
      | 1 | OK    |
      | 2 | ALARM |
