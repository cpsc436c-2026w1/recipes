@ext-backfill
Feature: Last month's trips in the lake beside the live ones
  ext-backfill, the operations analyst's story. Offline, DuckDB stands in for Athena: it reads the source (SIM01's
  sample in a local table, or local files) and writes trips_history as Parquet folders under .local/ext-backfill/,
  and the text of each Athena statement it would send is kept and checked. The example tagged @live runs in Athena.
  The data sets are described in ext-backfill/README.md, section Data.

  Background:
    Given the setting PREFIX is "recipe"

  @R90
  Rule: The team's own files become an external table before anything is copied

    Example: A CSV history with a header line
      Given the simulated data set SIM04 "team-files" is under raw/history/
      And history.toml says format csv, time column request_datetime
      When ext-backfill/backfill.py runs with "--source files --files raw/history/ --from 2024-03-14 --to 2024-03-15"
      Then Athena was sent one CREATE EXTERNAL TABLE IF NOT EXISTS recipe_lake.history_raw with skip.header.line.count 1
      And every column of history_raw is a string, cast to its history.toml type in the CTAS
      And history_raw has 6 rows
      And trips_history has 2 partitions, requested_day=2024-03-14 with 4 rows and requested_day=2024-03-15 with 2 rows

  @R100
  Rule: The backfill copies the given days into trips_history, one CTAS for the first month and one INSERT INTO for each further month

    Example: A month of history
      Given the simulated data set SIM01 "march-sample" is the source
      When ext-backfill/backfill.py runs with "--source course --from 2024-03-01 --to 2024-03-30"
      Then trips_history has 30 partitions, requested_day=2024-03-01 to requested_day=2024-03-30
      And Athena ran 1 CTAS and no INSERT INTO
      And the CTAS went to workgroup recipe-ctas-wg and the counts to workgroup recipe-wg

    Example: Six months
      Given a source with trips on each day from 2024-01-01 to 2024-06-30
      When ext-backfill/backfill.py runs with "--source course --from 2024-01-01 --to 2024-06-30"
      Then Athena ran 1 CTAS for January and 5 INSERT INTO, one for each month from February to June
      And trips_history has 182 partitions

  @R101
  Rule: Every copied row gets a stable trip_id, origin backfill and the run's id

    Example: Two identical rows stay two trips
      Given the simulated data set SIM04 "team-files" is under raw/history/
      When ext-backfill/backfill.py runs with "--source files --files raw/history/ --from 2024-03-14 --to 2024-03-15 --run-id bf-001"
      Then trips_history has 6 distinct trip_id values
      And the two identical rows have trip_ids that differ only in their ends, "-1" and "-2"
      And every row has origin "backfill" and run_id "bf-001"

    Example: A recopied day keeps its ids
      Given the simulated data set SIM04 "team-files" has been copied into trips_history
      When day 2024-03-14 is deleted and copied again
      Then the 4 trip_id values of requested_day=2024-03-14 are the ones the first copy wrote

  @R102
  Rule: A rerun leaves complete days as they are and copies only the others

    Example: The second half of a month added
      Given ext-backfill/backfill.py has run with "--source course --from 2024-03-01 --to 2024-03-15"
      When ext-backfill/backfill.py runs with "--source course --from 2024-03-01 --to 2024-03-30"
      Then Athena ran 1 INSERT INTO, for the 15 days 2024-03-16 to 2024-03-30
      And the files of requested_day=2024-03-01 to requested_day=2024-03-15 are the ones the first run wrote

  @R103
  Rule: A day whose count differs from the source is deleted and copied again

    Example: The short day repaired
      Given the simulated data set SIM02 "short-day" is in trips_history
      When ext-backfill/backfill.py runs with "--source course --from 2024-03-01 --to 2024-03-30"
      Then the files under history/trips/requested_day=2024-03-14/ were deleted before the day was copied again
      And the count of 2024-03-14 in trips_history is 34

  @R104
  Rule: One history has one source; another source stops the run unless it is asked to replace the days

    Example: The team's files after the course catalog
      Given the simulated data set SIM04 "team-files" is under raw/history/
      And ext-backfill/backfill.py has run with "--source course --from 2024-03-14 --to 2024-03-15"
      When ext-backfill/backfill.py runs with "--source files --files raw/history/ --from 2024-03-14 --to 2024-03-15"
      Then the exit code is 1
      And the output holds "trips_history was copied from course, not files"
      And the count of 2024-03-14 in trips_history is 34

  @R110
  Rule: The view shows one row per trip, history before the cutover time and live trips from it

    Example: A partial first live day
      Given trips_history holds these rows:
        | trip_id  | pickup_zone_id | requested_time       | fare  | origin   | run_id | requested_day |
        | h-08aa-1 | 161            | 2026-11-23T08:00:00Z | 18.40 | backfill | bf-001 | 2026-11-23    |
        | h-15bb-1 | 48             | 2026-11-23T15:00:00Z | 9.80  | backfill | bf-001 | 2026-11-23    |
      And trips holds these rows:
        | trip_id | pickup_zone_id | requested_time       | fare  |
        | t-0001  | 230            | 2026-11-23T14:00:00Z | 14.10 |
      When ext-backfill/backfill.py runs with "--view-only --live-from 2026-11-23T14:00:00Z"
      Then trips_all has 2 rows: h-08aa-1 with origin "backfill" and t-0001 with origin "live"
      And the output holds "1 history row at or after the cutover left out"

    Example: Three events of one live trip are one row
      Given the events trip_requested, trip_accepted and trip_completed of trip t-0001 have been merged into trips
      And trip t-0001 was requested in zone 161 with a fare of 18.40
      When view trips_all is read
      Then it has 1 row for t-0001, with pickup_zone_id 161, fare 18.40 and origin "live"

  @R120
  Rule: Each day's count is compared with the source's, and a difference fails the run

    Example: Counts that match, offline
      Given the simulated data set SIM01 "march-sample" is the source
      When ext-backfill/backfill.py runs with "--source course --from 2024-03-01 --to 2024-03-30"
      Then the output holds "30 days, 0 differ"
      And the exit code is 0

    @live
    Example: Counts that match, from the course catalog
      Given the course catalog is registered as the data source course
      When ext-backfill/backfill.py runs with "--target aws --source course --from 2024-03-01 --to 2024-03-30"
      Then trips_history counts 142310, 188010 and 194309 trips for zones 48, 161 and 230
      And the exit code is 0

    Example: A day short by 5 rows
      Given the simulated data set SIM02 "short-day" is in trips_history
      When the count check runs for 2024-03-01 to 2024-03-30
      Then the output holds "2024-03-14: 29 rows, the source has 34"
      And the exit code is 1
