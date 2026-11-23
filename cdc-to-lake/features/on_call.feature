Feature: ext-maintenance: a failed or missed night is seen the next morning
  The requirements are in ext-maintenance/README.md. Offline, moto stands in for Glue, CloudWatch and SNS.

  @R140
  Rule: Each night the partitions of the next 7 days are registered at their raw folders

    Example: After the run of 2026-11-23
      Given the partitions of trip_events_parquet up to dt=2026-11-24 are registered
      When the nightly run of 2026-11-23 ends
      Then the partitions dt=2026-11-25 to dt=2026-11-30 are registered, each at its raw/trip-events/ folder
      And the partition dt=2026-11-24 is unchanged

  @R141
  Rule: An error or a night without a run sends an email

    Example: The alarms as created
      When maintenance.py runs with "--target aws --schedule --email oncall@example.com"
      Then the exit code is 0
      And alarm recipe-ana-compact-errors fires on Errors of at least 1 over 86400 s
      And alarm recipe-ana-compact-missed fires on Invocations under 1 over 86400 s, missing data breaching
      And both send to topic recipe-ana-maintenance-alerts, which has oncall@example.com subscribed
