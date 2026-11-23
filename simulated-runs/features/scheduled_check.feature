Feature: The deployed system checked every day, and every commit tested offline
  The probe is SIM03 (README, Data). Times are Vancouver time; CHECK_DELAY_MIN is 90, from a data latency of 75 min
  at p95 (assumed) plus 15. Offline, the deployed system is the stand-in pipeline in moto.

  @R90
  Rule: Every day the probe is sent, and CHECK_DELAY_MIN later its count is read

    Example: A day on which the system works
      Given the deployed system stores what it is sent
      When the send schedule fires at 06:00 and the read schedule at 07:30
      Then the check sent the simulated data set SIM03 "probe" with 60 events
      And it read 20 probe trips of the day and put 1 to the metric LiveCheck/CheckPassed

    Example: The probe delivered twice
      Given the deployed system stores what it is sent
      And the send schedule delivers its call twice
      When the read schedule fires at 07:30
      Then it read 20 probe trips of the day and put 1 to the metric LiveCheck/CheckPassed

    Example: The read time follows the setting
      Given CHECK_CRON is 06:00 every day and CHECK_DELAY_MIN is 90
      When scripts/check.py up creates the schedules
      Then the read schedule fires at 07:30 every day

    Example: A read that would land after midnight is refused
      Given CHECK_CRON is 23:00 every day and CHECK_DELAY_MIN is 90
      When scripts/check.py up creates the schedules
      Then it stops with an error that holds "past midnight" and creates nothing

  @R100
  Rule: A wrong count, a failed read or a missing report is emailed within an hour after that day ends

    Scenario Outline: What the verdict of 2026-11-24 sends
      Given the check of 2026-11-24 <outcome>
      When the verdict schedule fires at 2026-11-25T00:15 Vancouver time
      Then <message>

      Examples:
        | outcome                    | message                                                                    |
        | read 20 probe trips        | nothing is published to recipe-check-alerts                                |
        | read 19 probe trips        | recipe-check-alerts gets "2026-11-24: 19 probe trips, not 20"              |
        | failed to read the table   | recipe-check-alerts gets "2026-11-24: " and the read's error_text          |
        | did not run                | recipe-check-alerts gets "2026-11-24: no check"                            |

    Example: A weekly check is judged only on the day it is due
      Given CHECK_CRON is "cron(0 6 ? * MON *)"
      And the check of 2026-11-30 read 20 probe trips
      When the verdict schedule fires at 00:15 on each of the 7 days from 2026-11-25 to 2026-12-01
      Then nothing is published to recipe-check-alerts
      And the verdict judged 1 day and passed over 6

    Example: A weekly check that did not run on its day
      Given CHECK_CRON is "cron(0 6 ? * MON *)"
      When the verdict schedule fires at 00:15 on each of the 7 days from 2026-11-25 to 2026-12-01
      Then recipe-check-alerts gets "2026-11-30: no check"

    Example: The date of a read near midnight in a time zone that changes its clocks
      Given CHECK_TZ is America/New_York
      When the read phase runs at 2026-11-02T04:30:00Z
      Then its item in the run log has run_id "2026-11-01"

    Example: The function may write the day's item and publish the verdict, and nothing more there
      When scripts/check.py up creates the function's role
      Then its policy allows "dynamodb:PutItem" and "dynamodb:GetItem" on the table recipe-runs only
      And its policy allows "sns:Publish" on the topic recipe-check-alerts only

    @R110
    Scenario: The backstop for a verdict that does not run
      When scripts/check.py alarm creates the alarm recipe-check-failed
      Then its metric is LiveCheck/CheckPassed with the statistic Minimum and a period of 3600 s
      And it needs 26 of 26 periods below 1, with missing data treated as breaching
      And its action is the topic recipe-check-alerts

    @live
    Example: The deployed function sends the probe, reads it and judges a day without a check
      Given the check is deployed with the lake bucket as its target and its query
      When the deployed function sends the probe and then reads it
      Then the day's item in the run log is finished with 20 probe trips
      And the deployed verdict of 2026-11-20, a day without an item, publishes "2026-11-20: no check" to the check's topic

  @R120
  Rule: Every push runs the tests and the offline scenarios, without AWS credentials

    Example: The workflow file
      When .github/workflows/offline.yml is read
      Then it runs "uv run pytest" and "uv run behave" on every push
      And it does not hold an AWS secret or a role to assume

    Example: The default run leaves out what spends money
      When behave.ini is read
      Then default_tags is "not (@live or @eval)"
