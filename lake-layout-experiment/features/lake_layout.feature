Feature: Which layout and which engine answer the zone counts
  The requirements are in README.md, section Requirements; the data sets in its section Data. Offline, DuckDB reads
  local Parquet folders in place of Athena and of the Lambda function (the Iceberg tables are a labelled stand-in:
  data files and a metadata file), and moto stands in for S3, Glue, Lambda, IAM and CloudWatch Logs in the clean-up.
  Scenarios tagged live run in AWS with LIVE=1 and are skipped otherwise.

  @R10
  Rule: The four layouts of a size have the same columns and the same rows

    Scenario: Two days of trips, offline
      Given the simulated data set SIM01 "two-days"
      When experiment.py build writes the size of 1 copy from SIM01
      Then the tables trips_hive_flat_1, trips_hive_day_1, trips_ice_flat_1 and trips_ice_day_1 each have 5 rows and the same 9 columns
      And trips_hive_day_1 has 2 folders, dt=2024-03-14 and dt=2024-03-15
      And trips_ice_day_1 has 2 partitions, for 2024-03-14 and 2024-03-15
      And trips_hive_flat_1 and trips_ice_flat_1 have 0 partitions

    @live
    Scenario: DS01 in AWS
      Given the course catalog is registered as the data source "course"
      When experiment.py build runs
      Then the table ds01 has 6045943 rows
      And each of the four tables of 1 copy has 6045943 rows

  @R20
  Rule: The table doubles until the one-day query scans more than 10 MB, then grows 4 times beyond

    Scenario: The probe passes 10 MB at 4 copies
      Given MAX_USD is 2.00
      And the probe and the upper bound of the plan a doubling would lead to are:
        | copies | probe_mb | plan_if_doubled | plan_upper_bound_usd |
        | 1      | 3.1      | 1, 2 and 8      | 0.30                 |
        | 2      | 6.2      | 1, 4 and 16     | 0.60                 |
        | 4      | 12.4     |                 |                      |
      And the upper bound of the plan 1, 4 and 16 copies, computed again before the final tables, is 0.60 USD
      When the build grows the table
      Then the sizes are 1, 4 and 16 copies
      And the summary says "10 MB reached at 4 copies"

    Scenario: The first copy already passes 10 MB
      Given MAX_USD is 2.00
      And the probe at 1 copy is 14.2 MB and the upper bound of the plan 1, 2 and 4 copies is 0.40 USD
      When the build grows the table
      Then the sizes are 1, 2 and 4 copies

    Scenario: Copies that the file format stores once stop the growth
      Given MAX_USD is 2.00
      And the probe and the upper bound of the plan a doubling would lead to are:
        | copies | probe_mb | plan_if_doubled | plan_upper_bound_usd |
        | 1      | 0.16     | 1, 2 and 8      | 0.01                 |
        | 2      | 0.28     | 1, 4 and 16     | 0.02                 |
        | 4      | 0.29     |                 |                      |
      When the build grows the table
      Then no final table is built and the run plan has 0 runs
      And the summary says "no plan: the probe did not grow with the copies (0.28 MB at 2 copies, 0.29 MB at 4 copies)"

  @R30
  Rule: The spend limit on the whole plan stops the doubling first

    Scenario: The rides case at the assumed 0.49 MB a day
      Given MAX_USD is 2.00
      And the probe and the upper bound of the plan a doubling would lead to are:
        | copies | probe_mb | plan_if_doubled | plan_upper_bound_usd |
        | 1      | 0.49     | 1, 2 and 8      | 1.01                 |
        | 2      | 0.98     | 1, 4 and 16     | 1.92                 |
        | 4      | 1.96     | 1, 8 and 32     | 3.75                 |
      And the upper bound of the plan 1, 4 and 16 copies, computed again before the final tables, is 1.92 USD
      When the build grows the table
      Then the sizes are 1, 4 and 16 copies
      And the summary says "10 MB not reached: probe 1.96 MB at 4 copies"

    Scenario: The first doubling is already over the limit
      Given MAX_USD is 0.80
      And at 1 copy the probe is 0.49 MB and the upper bound of the plan 1, 2 and 8 copies is 1.01 USD
      And the upper bound of the plan 1, 2 and 4 copies, computed before the final tables, is 0.64 USD
      When the build grows the table
      Then the sizes are 1, 2 and 4 copies
      And the summary says "10 MB not reached: probe 0.49 MB at 1 copy"

    Scenario: No plan within the limit
      Given MAX_USD is 0.50
      And at 1 copy the probe is 0.49 MB and the upper bound of the plan 1, 2 and 8 copies is 1.01 USD
      And the upper bound of the plan 1, 2 and 4 copies, computed before the final tables, is 0.64 USD
      When the build grows the table
      Then no final table is built and the run plan has 0 runs
      And the summary says "no plan within MAX_USD: upper bound 0.64 USD"

  @R40
  Rule: A dated table of more than 100 days is written 100 days at a time

    Scenario: Four dated copies
      Given 4 dated copies of DS01, from 2024-03-01 to 2024-07-02, 124 days
      When the build statements of trips_hive_day_4 are planned
      Then they are:
        | statement   | first_day  | last_day   |
        | CTAS        | 2024-03-01 | 2024-06-08 |
        | INSERT INTO | 2024-06-09 | 2024-07-02 |

  @R50
  Rule: Every Athena cell runs 10 times, without reused results, in one seeded order

    Scenario: The run plan
      Given 4 layouts, 3 sizes and the queries one-day, one-week and whole-table
      When experiment.py run plans its Athena runs with seed 436
      Then the plan has 360 runs, 10 for each of 36 cells, all in workgroup <PREFIX>-wg
      And every run asks Athena not to reuse results
      And planning again with seed 436 gives the same order

  @R60
  Rule: Each Athena run is stored with its Data scanned, billed MB and times

    Scenario: Two finished queries
      Given Athena reports these finished runs:
        | query_execution_id | data_scanned_bytes | engine_ms | queue_ms | planning_ms | to_last_row_ms |
        | q-0001             | 3145728            | 812       | 95       | 120         | 1290           |
        | q-0002             | 125829121          | 2410      | 60       | 140         | 2930           |
      When the runs are stored
      Then results.duckdb has q-0001 with billed MB 10 and q-0002 with billed MB 121
      And each has its time to the last row beside its engine, queue and planning times

  @R70
  Rule: A failed, cancelled or reused run is kept, left out of the times, and counted in the spend

    Scenario: Three runs of one cell
      Given these runs of the cell hive-by-day, 16 copies, whole-table:
        | query_execution_id | state     | reused_previous_result | data_scanned_bytes | to_last_row_ms |
        | q-0101             | SUCCEEDED | false                  | 524288000          | 3100           |
        | q-0102             | SUCCEEDED | true                   | 0                  | 180            |
        | q-0103             | CANCELLED | false                  | 8589934592         | 9000           |
      When the summary is written
      Then q-0102 is marked excluded with "result reused" and q-0103 with "scan cutoff"
      And the cell's times count 1 run
      And the cell's spend counts 8692 MB of Data scanned

  @R80
  Rule: The summary gives median (IQR) and p90 per cell, with Data scanned beside the billed MB

    Scenario: Ten runs of one cell
      Given the 10 kept runs of the cell hive-flat, 1 copy, one-day have these times to the last row in ms:
        | to_last_row_ms |
        | 1200           |
        | 1100           |
        | 1300           |
        | 1250           |
        | 1150           |
        | 1400           |
        | 1180           |
        | 1220           |
        | 1500           |
        | 1210           |
      When the summary is written, with quantiles interpolated between neighbouring runs
      Then the cell's time to the last row is median 1215.0 ms (IQR 1185.0 to 1287.5), p90 1410.0 ms
      And the cell's row has the median Data scanned and the median billed MB in adjacent columns

  @R90
  Rule: Two cells whose IQRs overlap are marked

    Scenario: Layouts compared at one size and query
      Given these IQRs of the time to the last row, in ms:
        | cell                            | q25    | q75    |
        | hive-flat, 1 copy, one-day      | 1185.0 | 1287.5 |
        | hive-by-day, 1 copy, one-day    | 1200.0 | 1267.5 |
        | hive-flat, 16 copies, one-day   | 1392.5 | 1470.0 |
        | hive-by-day, 16 copies, one-day | 1185.0 | 1287.5 |
      When the summary is written
      Then the pair at 1 copy is marked "ranges overlap"
      And the pair at 16 copies is not marked

  @R100
  Rule: DuckDB in Lambda runs the same queries and stores what each call billed

    Scenario: A cold and a warm call
      Given the function <PREFIX>-duckdb-query has 2048 MB
      And its REPORT lines for two calls of one-day on trips_hive_day_1 are:
        | request_id | duration_ms | billed_duration_ms | init_duration_ms |
        | rq-1       | 849.20      | 2750               | 1900.10          |
        | rq-2       | 419.60      | 420                |                  |
      When the runs are stored
      Then rq-1 is stored as cold with 5.5 billed GB-seconds
      And rq-2 is stored as warm with 0.84 billed GB-seconds

  @R110
  Rule: A DuckDB result whose rows differ by zone from Athena's takes its cell out of the comparison

    Scenario: One zone counted differently
      Given Athena returns zone 161 with 2345 trips and zone 230 with 1802 for one-day on trips_hive_day_1
      And DuckDB returns zone 230 with 1802 trips and zone 161 with 2344 for the same query
      When the summary is written
      Then the DuckDB cell is marked "results differ" and does not get a cost comparison

    Scenario: The same rows in another order
      Given Athena returns zone 161 with 2345 trips and zone 230 with 1802 for one-day on trips_hive_day_1
      And DuckDB returns zone 230 with 1802 trips and zone 161 with 2345 for the same query
      When the summary is written
      Then the DuckDB cell is not marked

  @R120
  Rule: Each cell has a cost of 1,000 queries at the course price sheet

    Scenario: An Athena cell and a DuckDB cell
      Given the Athena cell's median billed MB is 10
      And the DuckDB cell's medians are 850 ms billed at 2048 MB and 40 S3 GET requests
      When the summary is written
      Then 1,000 queries cost 0.0525 USD in the Athena cell and 0.0405 USD in the DuckDB cell

  @R130
  Rule: After one late row, the four tables still agree

    Scenario: A late trip in zone 161, offline
      Given the simulated data set SIM01 "two-days" is built at 1 copy
      And the one-day count of zone 161 on 2024-03-14 is 2 in all four tables
      When experiment.py late inserts the simulated data set SIM02 "late-row"
      Then the one-day count of zone 161 on 2024-03-14 is 3 in all four tables

  @R140 @aws
  Rule: Clean-up deletes what the recipe owns and keeps what it borrowed

    Scenario: The bucket and the workgroups existed before the build
      Given the bucket <PREFIX>-lake-<account>, the data source course, <PREFIX>-ctas-wg and <PREFIX>-wg existed before experiment.py build
      And experiment.py build and experiment.py run have finished
      When experiment.py clean --yes runs
      Then the database <prefix>_layout, the prefix layout/, <PREFIX>-duckdb-query, its log group and <PREFIX>-duckdb-role are gone
      And the bucket, the data source course, the two workgroups and results.duckdb are still there

    Scenario: The build made the bucket, registered the data source and created the workgroups
      Given experiment.py build ran scripts/lake.py up for the bucket <PREFIX>-lake-<account>, registered the data source course and created <PREFIX>-ctas-wg and <PREFIX>-wg
      And the ledger records the two workgroups created and course borrowed
      When experiment.py clean --yes runs
      Then the database <prefix>_layout, the prefix layout/, <PREFIX>-duckdb-query, its log group and <PREFIX>-duckdb-role are gone
      And the bucket, the data source course, <PREFIX>-ctas-wg and <PREFIX>-wg are still there
      When scripts/lake.py down runs after every recipe's clean-up
      Then <PREFIX>-ctas-wg and <PREFIX>-wg are gone

  @R141 @aws
  Rule: A bare clean-up lists what is there and deletes nothing

    Scenario: The list before the delete
      Given the bucket <PREFIX>-lake-<account>, the data source course, <PREFIX>-ctas-wg and <PREFIX>-wg existed before experiment.py build
      And experiment.py build and experiment.py run have finished
      When experiment.py clean runs without --yes
      Then it lists each table of <prefix>_layout and the objects under layout/
      And the database <prefix>_layout and the prefix layout/ are still there
