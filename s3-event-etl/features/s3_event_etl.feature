Feature: Every good row of a file in the lake, every bad row kept, and a load that cannot run away
  The analyst's story (each file checked as it lands, its good rows loaded, its bad rows kept with the reason) and
  the team member's (repeats, a failed run, limits on the runs). Offline, moto stands in for S3, DynamoDB and SNS, a
  stub for Glue (StartJobRun, GetJobRun) and for Athena's MERGE runs pipeline/local_load.py's load with DuckDB, and a
  scenario calls the check function with the S3 event an object would send; the example tagged @live runs in AWS.
  The data sets are described in README, Data. ENGINE is glue, ETL_MIN_GAP_MIN 360 and MAX_RUNS_PER_DAY 4 unless an
  example says otherwise.

  Background:
    Given the lake bucket, the run log and the alerts topic of PREFIX recipe exist

  @R10
  Rule: Each new raw object is checked line by line and its good lines are staged

    Example: An object of three good lines
      Given the object raw/trip-events/dt=2026-11-23/0905-run1.jsonl holds these deliveries:
        | delivery_id | event_type     | event_id | trip_id | pickup_zone_id | event_time           | received_time        |
        | dl-0001     | trip_requested | ev-0001  | t-0001  | 161            | 2026-11-23T09:05:10Z | 2026-11-23T09:05:11Z |
        | dl-0002     | trip_requested | ev-0002  | t-0002  | 48             | 2026-11-23T09:05:20Z | 2026-11-23T09:05:21Z |
        | dl-0003     | trip_requested | ev-0003  | t-0003  | 230            | 2026-11-23T09:05:40Z | 2026-11-23T09:05:41Z |
      When S3 sends the object's created event to the check function
      Then stage/trip-events/dt=2026-11-23/0905-run1-<sequencer>.jsonl holds the same 3 lines in the same order
      And the run log item etl-object for the key has good_lines 3 and bad_lines 0

    @live
    Example: The validation latency of the rush hour
      Given the simulated data set SIM01 "rush-hour" has been landed in the lake bucket in AWS
      When every object's event has been handled
      Then 60 staged objects exist, and the p95 of their LastModified minus their raw object's is under 2 min

  @R20
  Rule: A bad line is set aside with its line number and reason, and the object's other lines go on

    Example: The rush hour with three broken lines
      Given the simulated data set SIM02 "bad-lines" has been landed in the lake bucket
      When every object's event has been handled and the load has run
      Then quarantine/trip-events-rows/dt=2026-11-23/ holds 1 object of 3 lines, with line numbers and the reasons "extra field tip", "event_time has no time zone" and "trip_id missing"
      And table trip_events_clean has 2508 rows

  @R30
  Rule: A run loads every staged object that no finished run has loaded, one row per event_id, then writes the manifest

    Example: The rush hour loaded
      Given the simulated data set SIM01 "rush-hour" has been landed and checked
      When the ETL job runs
      Then table trip_events_clean has 2511 rows, one per event_id
      And lake/etl-state/dt=2026-11-23.txt lists the 110 staged keys

  @R40
  Rule: An event that arrives again, or the same deliveries landed again, leave the table unchanged

    Example: One object landed twice and one event sent twice
      Given the simulated data set SIM03 "landed-twice" has been landed and checked
      When the ETL job runs
      Then table trip_events_clean has 2511 rows
      And the run log counts 1 repeat event, for the 09:30 object
      And the staged object of the key …-again.jsonl added 0 rows

  @R50
  Rule: A run starts only when none is running, the gap has passed and the day's limit is not reached

    Scenario Outline: Whether a staged object starts a run
      Given the last run started <minutes> minutes ago and is <state>, and <runs> runs have started today
      When an object is staged
      Then <result>

      Examples:
        | minutes | state    | runs | result                                      |
        | 400     | finished | 1    | a run starts                                |
        | 120     | finished | 2    | no run starts, and the sweep will start one |
        | 400     | running  | 1    | no run starts                               |
        | 400     | finished | 4    | no run starts until the next UTC day        |

  @R60
  Rule: A failed run is recorded and emailed, and the next run loads its objects; three failures in a row hold the job

    Example: A run that fails after its commit
      Given the simulated data set SIM04 "failing-run" has been landed and checked
      When the first run fails after its commit and the sweep runs
      Then the run log records the run failed with failed_step "manifest"
      And topic recipe-etl-alerts received 1 message naming the run
      When the next run starts
      Then table trip_events_clean has 2511 rows

  @R61
  Rule: Three failed runs in a row hold the job until etl.py resume

    Example: Three failed runs in a row
      Given the last 3 runs of the job failed
      When the sweep finds staged objects waiting
      Then no run starts and the run is recorded skipped with error_text "held after 3 failed runs"
      When etl.py resume runs and the sweep runs again
      Then a run starts

  @R70
  Rule: The Glue job runs with 2 workers, 10 minutes at most, no retries and one run at a time

    Example: The job as created
      When etl.py up creates the job
      Then job recipe-etl has worker type G.1X, 2 workers, Timeout 10, MaxRetries 0, MaxConcurrentRuns 1 and run queuing off

  @R80
  Rule: With ENGINE=athena the sweep loads with one MERGE INTO and the same manifest

    Example: The rush hour loaded by Athena
      Given ENGINE is athena
      And the simulated data set SIM01 "rush-hour" has been landed and checked
      When the sweep runs
      Then it started 1 query in workgroup recipe-wg, a MERGE INTO trip_events_clean
      And lake/etl-state/dt=2026-11-23.txt lists the 110 staged keys

  @R90
  Rule: Clean-up removes only this recipe's notification entry and what the recipe owns

    Example: A bucket with another recipe's trigger
      Given the bucket's notification configuration has the entries quality-gate and recipe-s3-event-etl
      When clean_up.py --yes runs
      Then the bucket's notification configuration has only the entry quality-gate
      And functions recipe-etl-check and recipe-etl-sweep, job recipe-etl and the prefix stage/trip-events/ are gone
      And the bucket and raw/trip-events/ are still there
