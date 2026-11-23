Feature: ext-maintenance: the 30-day count stays right and fast while small files arrive
  The requirements are in ext-maintenance/README.md. "Today" is 2026-11-23 and the nightly run starts at
  2026-11-23T11:00:00Z (03:00 Vancouver time) unless an example says otherwise. Defaults: MAX_FILES_PER_QUERY 3000,
  QUERY_DAYS 30, CLOSE_AFTER_HOURS 1. Offline, moto stands in for S3, Glue and DynamoDB, and a recording stand-in for
  Athena returns the row and file counts an example gives (tests/athena_stub.py); examples tagged @live run in AWS.
  Data sets are described in ext-maintenance/README.md, section Data.

  @R120
  Rule: Each night the data files of each day are counted and written to the run log

    Example: Three days of trip_events_parquet
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      When the nightly schedule fires
      Then the run log has 1440 files for 2026-11-21, 80 for 2026-11-22 and 600 for 2026-11-23 in trip_events_parquet

    Example: A table without a day partition
      Given ICEBERG_TABLES also names table daily_totals, which does not have a day partition, and 3200 data files
      When the nightly schedule fires
      Then the run log has 3200 files for the whole of daily_totals

  @R121
  Rule: A closed day with more files than a query may read per day is compacted, at most 10 a run, and no other day

    Scenario Outline: Which days are compacted
      Given day <day> of trip_events_parquet holds <files> files
      And trip_events_parquet is filed by <filed_by> day, with CLOSE_AFTER_HOURS <close_after>
      When the nightly run decides
      Then day <day> is <decision>

      Examples:
        | day        | files | filed_by | close_after | decision      |
        | 2026-11-21 | 1440  | arrival  | 1           | compacted     |
        | 2026-11-22 | 80    | arrival  | 1           | not compacted |
        | 2026-11-22 | 101   | arrival  | 1           | compacted     |
        | 2026-11-22 | 100   | arrival  | 1           | not compacted |
        | 2026-11-23 | 600   | arrival  | 1           | not compacted |
        | 2026-11-22 | 1440  | event    | 48          | not compacted |
        | 2026-11-20 | 1440  | event    | 48          | compacted     |

    Example: A year of 15-minute files
      Given QUERY_DAYS is 365
      And day 2026-11-22 of trip_events_parquet holds 96 files
      When the nightly run decides
      Then the limit is 8 files a day
      And day 2026-11-22 is compacted

    Example: Twelve days of backlog
      Given days 2026-11-11 to 2026-11-22 of trip_events_parquet hold 1440 files each
      When the nightly run decides
      Then days 2026-11-11 to 2026-11-20 are compacted
      And days 2026-11-21 and 2026-11-22 wait for the next night

  @R122
  Rule: An Iceberg day is compacted by OPTIMIZE with a WHERE that selects that day only

    Example: The busy day of the change log
      Given the simulated data set SIM03 "change-log-day" is in trip_changes
      When the nightly run compacts trip_changes
      Then Athena was sent one OPTIMIZE of trip_changes, with WHERE change_time >= TIMESTAMP '2026-11-22 00:00:00' AND change_time < TIMESTAMP '2026-11-23 00:00:00'
      And Athena was sent no OPTIMIZE for 2026-11-21

    Example: The live trips table
      Given day 2026-11-22 of trips holds 720 data files and day 2026-11-21 holds 90
      When the nightly run compacts trips
      Then Athena was sent one OPTIMIZE of trips, with WHERE requested_time >= TIMESTAMP '2026-11-22 00:00:00' AND requested_time < TIMESTAMP '2026-11-23 00:00:00'

    Example: A table without a day partition, compacted whole
      Given ICEBERG_TABLES also names table daily_totals, which does not have a day partition, and 3200 data files
      When the nightly run compacts daily_totals
      Then Athena was sent the statement "OPTIMIZE recipe_ana_lake.daily_totals REWRITE DATA USING BIN_PACK"

    Example: The day bounds refused
      Given day 2026-11-22 of trip_changes holds 1440 data files
      And Athena refuses the WHERE of the OPTIMIZE of trip_changes
      When the nightly run compacts trip_changes
      Then Athena was sent no OPTIMIZE of trip_changes without a WHERE
      And the run log has the run with failed_step "optimize" and the refusal as error_text
      And the run ends with an error that names recipe_ana_cdc.trip_changes

    @live
    Example: The day bounds are accepted on a 2-day copy
      Given a 2-day copy of SIM03 "change-log-day" as table trip_changes_probe
      When the nightly run compacts trip_changes_probe
      Then the OPTIMIZE with the WHERE of 2026-11-22 succeeds
      And day 2026-11-22 of trip_changes_probe has fewer than 100 data files
      And day 2026-11-21 of trip_changes_probe still has 40 data files

  @R130
  Rule: A trip_events_parquet table with partition projection on, or with files that are not Parquet, is refused before anything changes

    Example: Projection still on
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      And trip_events_parquet has the table property projection.enabled = true
      When the nightly schedule fires
      Then the run stops with the error "trip_events_parquet uses partition projection: run maintenance.py --register-partitions first"
      And no partition of trip_events_parquet changed and no file under raw/trip-events/ was deleted

    Example: The JSON Lines table of scheduled-merge
      Given RAW_TABLE names recipe_ana_lake.trip_events, which reads JSON Lines files
      When the nightly schedule fires
      Then the run stops with the error "trip_events is not Parquet: a CTAS would write another format into it"
      And no partition of trip_events changed

  @R131
  Rule: Switching to registered partitions registers every existing day and the next 7

    Example: Thirty days of folders
      Given the simulated data set SIM01 "thirty-days" is in trip_events_parquet
      And trip_events_parquet has the table property projection.enabled = true
      When maintenance.py runs with "--target aws --register-partitions" on 2026-11-23
      Then trip_events_parquet has the table property projection.enabled = false
      And the partitions dt=2026-10-24 to dt=2026-11-29 are registered, each at its raw/trip-events/ folder
      And the output holds "registered 37 partitions"

  @R132
  Rule: A plain Parquet day is copied by one CTAS under a new prefix and its partition switched there when the counts agree

    Example: The 1,440-file day
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      And the CTAS of day 2026-11-21 writes 17280 rows
      When the nightly run compacts trip_events_parquet
      Then maintenance/inputs/dt=2026-11-21/ holds the list of the day's 1440 files
      And the partition dt=2026-11-21 of trip_events_parquet points at a prefix under compacted/trip-events/dt=2026-11-21/
      And the partition dt=2026-11-22 still points at raw/trip-events/dt=2026-11-22/
      And the CTAS ran in the workgroup recipe-ana-ctas-wg

    @live
    Example: The 1,440-file day in Athena
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      When the nightly run compacts trip_events_parquet
      Then the count of trip_events_parquet for dt=2026-11-21 is 17280, all of its rows read from compacted/trip-events/
      And that prefix holds fewer than 100 files

  @R133
  Rule: A failed or short copy leaves the partition at its old location and is recorded

    Example: The copy is 12 rows short
      Given the simulated data set SIM04 "failed-copy" is in trip_events_parquet
      When the nightly run compacts trip_events_parquet
      Then the partition dt=2026-11-21 still points at raw/trip-events/dt=2026-11-21/
      And compacted/trip-events/dt=2026-11-21/ is empty
      And the run log has the failure of 2026-11-21 with "17268 rows, expected 17280"

    Example: The count after the switch differs
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      And the count of dt=2026-11-21 through trip_events_parquet after the switch is 17279
      When the nightly run compacts trip_events_parquet
      Then the partition dt=2026-11-21 points at raw/trip-events/dt=2026-11-21/ again
      And compacted/trip-events/dt=2026-11-21/ is empty

    Example: A file lands while the day is copied
      Given the simulated data set SIM02 "three-days" is in trip_events_parquet
      And a 1441st file lands under raw/trip-events/dt=2026-11-21/ after the file list is recorded and before the switch
      When the nightly run compacts trip_events_parquet
      Then the partition dt=2026-11-21 still points at raw/trip-events/dt=2026-11-21/
      And compacted/trip-events/dt=2026-11-21/ is empty
      And the run log has the failure of 2026-11-21 with "files changed during the copy"

  @R134
  Rule: A file that lands in a moved day's raw folder is copied into the day's location before any raw file is deleted

    Example: The next two nights
      Given the partition dt=2026-11-21 of trip_events_parquet was moved at 2026-11-23T11:05:00Z
      When the nightly run starts at 2026-11-24T11:00:00Z
      Then raw/trip-events/dt=2026-11-21/ still holds its 1440 files
      When the nightly run starts at 2026-11-25T11:00:00Z
      Then raw/trip-events/dt=2026-11-21/ is empty

    Example: A Firehose retry after the move
      Given the simulated data set SIM05 "late-file" is in trip_events_parquet
      When the nightly run starts at 2026-11-24T11:00:00Z
      Then the day's current location holds 1 file named late-<file name>
      And the run log records 1 late file copied for 2026-11-21
      And raw/trip-events/dt=2026-11-21/ holds the 1440 listed files and not the late one

    Example: A day whose files the merge has not all merged keeps its raw files
      Given the partition dt=2026-11-21 of trip_events_parquet was moved at 2026-11-23T11:05:00Z
      And MERGE_READS_RAW is 1, and the merge's manifest of dt=2026-11-21 lists 1439 of the day's 1440 files as merged
      When the nightly run starts at 2026-11-25T11:00:00Z
      Then raw/trip-events/dt=2026-11-21/ still holds its 1440 files

    @live
    Example: The late file is counted
      Given the simulated data set SIM05 "late-file" is in trip_events_parquet
      When the nightly run starts at 2026-11-24T11:00:00Z
      Then the count of trip_events_parquet for dt=2026-11-21 is 17292

  @R135
  Rule: With --measure, the cost is estimated first, then the 30-day count is run before and after, and the answer does not change

    Example: An estimate over the limit
      Given the simulated data set SIM01 "thirty-days" is in trip_events_parquet
      And MAX_MEASURE_USD is 0.05
      When maintenance.py runs with "--target aws --measure --all-days"
      Then the output holds "estimated 0.08 USD, over MAX_MEASURE_USD 0.05"
      And Athena was sent no statement
      And the exit code is 1

    @live
    Example: Thirty days of small files, compacted
      Given the simulated data set SIM01 "thirty-days" is in trip_events_parquet
      When maintenance.py runs with "--target aws --measure --all-days"
      Then before and after, the count by zone over 30 days is 9150, 5730 and 10350 for zones 48, 161 and 230
      And the output has a row "before" with 43200 files and a row "after" with fewer than 3000, each with Data scanned and run time
