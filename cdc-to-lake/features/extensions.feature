Feature: The three extensions
  ext-lag-alarm/README.md, "Requirements": R60 to R64. ext-rollback-or-schema/README.md: R70 to R73.
  ext-maintenance/README.md: R110 to R114.

  @R60 @R64
  Scenario: The probe on AWS
    Given the pipeline of a run with --target aws exists
    And Athena returns the probe's row, changed 91 seconds ago, at its 4th look
    When lag.py runs with "--target aws"
    Then the exit code is 0
    And a probe trip was written to "recipe-ana-trips" and removed again
    And the metric "CdcToLake/ChangeToQueryableSeconds" got one value between 90 and 93 seconds
    And the object "freshness/trip_changes.json" in the lake's bucket names the table, "data_last_updated" and the lag

  @R61
  Scenario Outline: The alarm on the lag
    Given the setting LAG_LIMIT_SECONDS is "<set>"
    When lag.py runs with ""
    Then the alarm "recipe-ana-cdc-lag" goes off when "ChangeToQueryableSeconds" is greater than <limit> in one period of 300 seconds
    And the printed probe "healthy" is under the limit with the alarm "OK"
    And the printed probe "stuck for a while" is over the limit with the alarm "ALARM"

    Examples:
      | set     | limit |
      | not set | 300   |
      | 400     | 400   |

  @R62
  Scenario: The alarm on the stream's age
    When lag.py runs with ""
    Then the alarm "recipe-ana-cdc-iterator-age" goes off when "IteratorAge" of the function "recipe-ana-cdc-to-firehose" is greater than 60000 in 3 of 5 periods of 60 seconds

  @R63
  Scenario: A period without a probe
    When lag.py runs with ""
    Then the alarm "recipe-ana-cdc-lag" treats missing data as "missing"
    And the recipe's own reading of an alarm without a value is "INSUFFICIENT_DATA"

  @R70 @R71
  Scenario: A bad load, found by time travel and undone
    When rollback.py runs with ""
    Then the exit code is 0
    And before the load the table has 5 changes with an average fare under 20
    And after the load the average fare is more than 20 times that
    And time travel to the snapshot before the load still finds 5 changes
    And after the restore the table has 6 changes, the 5 and 1 good change made after the load, with an average fare under 20

  @R72
  Scenario: A column added without a rewrite
    When schema_change.py runs with ""
    Then the exit code is 0
    And the columns end with "payment_type"
    And the output holds "(the old files were not rewritten)"
    And the output holds "old query, same answer for the old trips: True"
    And every change before the new column reads "None" as its payment_type, and the new one "card"

  @R73
  Scenario: The column on AWS
    When schema_change.py runs with "--target aws"
    Then the exit code is 0
    And Athena ran "ALTER TABLE recipe_ana_cdc.trip_changes ADD COLUMNS (payment_type string)"
    And the output names the 3 steps that fill the column

  @R110 @R111
  Scenario: Compaction and expiry, offline
    When maintenance.py runs with ""
    Then the exit code is 0
    And the printed row "after" has fewer data files, fewer snapshots and less data scanned than the row "before"
    And the output holds "same query answer before and after: True"
    And the output holds "time travel to the first snapshot: no longer possible (expired)"

  @R112
  Scenario: Compaction and expiry on AWS, measured
    When maintenance.py runs with "--target aws"
    Then the exit code is 0
    And Athena ran, in this order, the measured query, "OPTIMIZE recipe_ana_cdc.trip_changes REWRITE DATA USING BIN_PACK", "VACUUM recipe_ana_cdc.trip_changes" and the measured query again
    And the output has a row "before" and a row "after" with Data scanned in B and run time in ms
    And no schedule was created

  @R113
  Scenario: The two nightly schedules call the compaction function
    When maintenance.py runs with "--target aws --schedule"
    Then the exit code is 0
    And the group "recipe-ana-maintenance-compaction" has the schedules "recipe-ana-compact-schedule" at "cron(0 3 * * ? *)" and "recipe-ana-lake-vacuum" at "cron(0 4 * * ? *)", in the time zone "America/Vancouver"
    And each schedule calls the function "recipe-ana-compact-days" as the role "recipe-ana-compact-scheduler-role", with no retries

  @R114
  Scenario: How long snapshots are kept
    Given Athena shows the changes of the scenario at its 1st look at trips_current
    When main.run runs with "--target aws"
    Then the table is created with snapshots kept for 604800 seconds and at least 5 of them
