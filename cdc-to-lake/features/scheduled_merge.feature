Feature: ext-scheduled-merge: one trips table and counts per period that stay right when events are late, repeated or changed
  The requirements are in ext-scheduled-merge/README.md. Offline, moto stands in for S3 and DynamoDB and DuckDB
  runs the same MERGE texts Athena runs; each object's LastModified is set by the scenario. Days are UTC; "today"
  is 2026-11-23 unless an example says otherwise. K is 3 days and PERIOD is a day unless an example says otherwise.
  Data sets are described in ext-scheduled-merge/README.md, section Data.

  @R80
  Rule: Each slot of deliveries is written under its arrival day as it arrives

    Example: A slot lands under its arrival day
      Given these deliveries arrive in the slot 2026-11-23T09:05 to 09:06:
        | delivery_id | event_type     | event_id  | trip_id | pickup_zone_id | event_time           | received_time        |
        | dl-000001   | trip_requested | ev-000001 | t-0001  | 161            | 2026-11-23T09:05:10Z | 2026-11-23T09:05:11Z |
      When the slot is written by the replayer's s3 target
      Then one object under raw/trip-events/dt=2026-11-23/ for the slot 09:05 holds delivery dl-000001
      And the replayer sent it less than 300 seconds after the slot was due

  @R81
  Rule: The hourly merge keeps one row per trip, with the state of its furthest event and the fields each kind carries

    Example: A trip requested, accepted and completed in one window
      Given these trip events arrived in the window 09:00 to 10:00:
        | event_type     | event_id  | trip_id | pickup_zone_id | driver_id | fare  | event_time           |
        | trip_requested | ev-000001 | t-0001  | 161            |           |       | 2026-11-23T09:01:10Z |
        | trip_accepted  | ev-000002 | t-0001  |                | d-042     |       | 2026-11-23T09:01:20Z |
        | trip_completed | ev-000003 | t-0001  |                | d-042     | 18.40 | 2026-11-23T09:31:20Z |
      When the merge of that window runs
      Then table trips has 1 row for trip t-0001: state "completed", zone 161, driver "d-042", fare 18.40

    Example: The rush hour with its repeats
      Given the simulated data set SIM01 "rush-hour" has arrived, its late deliveries included
      When the merges of its windows have run
      Then table trips has 837 rows, of which 304, 191 and 342 have zones 48, 161 and 230

    Example: A backlog after a day of failed runs
      Given MAX_OBJECTS is 500 and 1200 objects under raw/trip-events/ are in no merge manifest
      When the schedule fires three times
      Then the three runs merge 500, 500 and 200 objects, oldest first

    Example: More objects with one LastModified than a run takes
      Given MAX_OBJECTS is 500 and 600 objects with LastModified 2026-11-23T09:30:00Z are in no merge manifest
      When the schedule fires twice
      Then the two runs merge 500 and 100 objects, in key order
      And no object is merged by both runs

    Example: An object stamped early that becomes visible after the listing
      Given the run of 10:00 listed raw/trip-events/dt=2026-11-23/ and finished both steps
      And an object with LastModified 2026-11-23T09:20:00Z becomes visible after that listing
      When the run of 11:00 runs
      Then that object is one of the run of 11:00's objects
      And none of the run of 10:00's objects is taken again

  @R82
  Rule: An older event fills only the fields the row does not have

    Example: The request arrives after the completion
      Given table trips has this row:
        | trip_id | trip_state | pickup_zone_id | driver_id | fare  | last_event_time      |
        | t-0002  | completed  |                | d-007     | 22.10 | 2026-11-23T09:40:00Z |
      And this event arrived in the window 10:00 to 11:00:
        | event_type     | event_id  | trip_id | pickup_zone_id | event_time           |
        | trip_requested | ev-000010 | t-0002  | 48             | 2026-11-23T09:12:00Z |
      When the merge of that window runs
      Then the row of t-0002 has state "completed", zone 48, driver "d-007" and fare 22.10

    Example: A corrected completion replaces the fare
      Given table trips has this row:
        | trip_id | trip_state | pickup_zone_id | driver_id | fare  | last_event_time      |
        | t-0003  | completed  | 230            | d-011     | 30.00 | 2026-11-23T09:50:00Z |
      And this event arrived in the window 10:00 to 11:00:
        | event_type     | event_id  | trip_id | driver_id | fare  | event_time           |
        | trip_completed | ev-000020 | t-0003  | d-011     | 35.00 | 2026-11-23T09:55:00Z |
      When the merge of that window runs
      Then the row of t-0003 has fare 35.00

    Example: A corrected request after the completion changes the zone, not the state
      Given table trips has this row:
        | trip_id | trip_state | pickup_zone_id | requested_time       | driver_id | fare  | last_event_time      |
        | t-0004  | completed  | 161            | 2026-11-23T09:10:00Z | d-015     | 12.30 | 2026-11-23T09:35:00Z |
      And this event arrived in the window 10:00 to 11:00:
        | event_type     | event_id  | trip_id | pickup_zone_id | event_time           |
        | trip_requested | ev-000030 | t-0004  | 48             | 2026-11-23T09:40:00Z |
      When the merge of that window runs
      Then the row of t-0004 has state "completed", zone 48 and requested_time 2026-11-23T09:40:00Z

  @R90
  Rule: A merge rebuilds the counts of every period of the last K + 1 days, in one statement

    Example: Requests of the previous day arrive 30 hours late
      Given the simulated data set SIM02 "late-day" was merged on 2026-11-22
      And the counts of 2026-11-22 are 304, 179 and 342 for zones 48, 161 and 230
      When the window with its 12 late requests is merged on 2026-11-23
      Then the counts of 2026-11-22 are 304, 191 and 342 for zones 48, 161 and 230
      And the rebuild was one statement

    Example: A late completion corrects the day before its own
      Given table trips has trip t-0006 of zone 230 requested at 2026-11-22T23:50:00Z, accepted and not completed
      And the counts of 2026-11-22 for zone 230 are 1 requested and 0 completed
      And its completion with event_time 2026-11-23T00:20:00Z arrived in the window 10:00 to 11:00 on 2026-11-23
      When the merge of that window runs
      Then the counts of 2026-11-22 for zone 230 are 1 requested and 1 completed

    Example: A corrected request moves a trip to another zone and day
      Given table trips has trip t-0005 of zone 161 requested at 2026-11-22T23:58:00Z
      And the counts of zone 161 on 2026-11-22 are 1 and of zone 48 on 2026-11-23 are 0
      And a corrected request of t-0005 for zone 48 with event_time 2026-11-23T00:03:00Z arrived on 2026-11-23
      When the merge of the window that holds it runs
      Then zone_counts has no row of zone 161 on 2026-11-22
      And the counts of zone 48 on 2026-11-23 are 1

    Example: Every kind of fault
      Given the simulated data set SIM03 "all-faults" has arrived
      When the merges of its windows have run
      Then the counts of 2026-11-23 are 304, 188 and 342 requested and 834 completed, as the answer from _truth/ holds

    Scenario Outline: The period is a day, a week or a month
      Given PERIOD is "<period>"
      And requests of zone 161 on 2026-11-22 and on 2026-11-23 have been merged
      When the counts of zone 161 are read
      Then they are <rows>

      Examples:
        | period | rows                                  |
        | day    | 1 on 2026-11-22 and 1 on 2026-11-23   |
        | week   | 1 on 2026-11-16 and 1 on 2026-11-23   |
        | month  | 2 on 2026-11-01                       |

  @R91
  Rule: Events older than K + 1 days are merged but do not rebuild their period, and their number is recorded

    Example: Two requests of five days ago
      Given the counts of 2026-11-18 are 300, 190 and 340 for zones 48, 161 and 230
      And 2 requests of zone 161 with event times on 2026-11-18 arrived in the window 09:00 to 10:00 on 2026-11-23
      When the merge of that window runs
      Then table trips has the 2 trips
      And the counts of 2026-11-18 are still 300, 190 and 340
      And the run's item in the run log has old_events 2

  @R100
  Rule: Merging the same objects twice does not change the tables

    Example: The objects of a run whose rebuild failed, merged again
      Given the run of 10:00 merged its 60 objects and its rebuild failed
      When the run of 11:00 merges those 60 objects again
      Then the rows of trips they touch are the same as after the run of 10:00
      And zone_counts equals a count of trips by zone and day

  @R101
  Rule: A failed step leaves the run's objects out of the manifests, so the next run merges them again

    Scenario Outline: The step that fails
      Given the run of 10:00 took 60 objects
      And in that run the <step> fails
      When the run of 11:00 starts
      Then its objects include those 60 objects
      And the run log has the run of 10:00 with run_state "failed", failed_step "<step>" and its error_text

      Examples:
        | step    |
        | merge   |
        | rebuild |

  @R102
  Rule: A run that finds the previous run still running does not start a query

    Example: The previous run's merge is still running in Athena
      Given the run of 10:00 recorded a merge query that is still RUNNING
      When the schedule fires at 11:00
      Then the run of 11:00 is recorded as "skipped"
      And it starts no query

  @R103
  Rule: An object with a line that does not match the delivery model is set aside whole

    Example: A line without a trip_id
      Given an object under raw/trip-events/dt=2026-11-23/ holds 42 lines, one of them without trip_id
      When the merge of its window runs
      Then that object is not one of the run's merged objects
      And a copy of it is under quarantine/trip-events/dt=2026-11-23/
      And the run's item in the run log has quarantined 1 and bad_lines 1

  @R104
  Rule: The hourly schedule can be paused and resumed without deleting anything

    Example: Pause, then resume
      Given the extension is deployed with merge.py up
      When merge.py --target aws pause runs
      Then the hourly schedule is DISABLED, with its rate and target unchanged
      And the merge function is still there
      When merge.py --target aws resume runs
      Then the hourly schedule is ENABLED, with its rate and target unchanged
