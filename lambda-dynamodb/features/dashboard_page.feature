Feature: ext-dashboard-page: requests by zone on a page, and how fresh each zone is
  ext-dashboard-page/README.md, "Requirements": R100 to R210. Data sets: ext-dashboard-page/README.md, "Data".
  Offline, moto stands in for DynamoDB, Lambda, IAM and EventBridge Scheduler, a fake Athena answers the summary
  query, and the handler is called with function URL events (payload format 2.0). @browser examples open the page
  in headless Chromium against the same handler, with a stand-in for the chart library; they are skipped when
  Chromium is not installed.

  @R100
  Rule: The page at / is HTML whose script draws one chart from the JSON

    Example: The HTML answer
      Given the simulated data set "rush-hour" has been counted
      When a browser sends GET / at 2026-11-23T10:00:30Z
      Then the answer is HTTP 200 with content type "text/html"
      And the page's script reads /api/zone-counts

    @browser
    Example: The page drawn in a browser
      Given the simulated data set "rush-hour" has been counted
      And the latest items of zones 161 and 230 are 2026-11-23T09:54:59Z and 2026-11-23T09:58:12Z, and zone 48 has none
      When the page is opened in a headless browser at 2026-11-23T10:00:30Z and its first read of /api/zone-counts has answered
      Then the page has 1 chart with 3 series, for zones 48, 161 and 230, of 12 periods each
      And the freshness line reads "data as of 09:58:12 UTC, read at 10:00:30 UTC"
      And the series of zone 48 is drawn dashed with "not reporting"

  @R110
  Rule: The JSON has a value for every zone and completed period, and null where nothing was counted

    Example: Two stored counts in zone 48 and one in zone 161
      Given only these counts are stored:
        | zone_id | period_start         | requested | newest_event_time    |
        | 48      | 2026-11-23T09:20:00Z | 5         | 2026-11-23T09:24:40Z |
        | 48      | 2026-11-23T09:35:00Z | 6         | 2026-11-23T09:39:10Z |
        | 161     | 2026-11-23T09:25:00Z | 4         | 2026-11-23T09:29:55Z |
        | 161     | 2026-11-23T10:00:00Z | 1         | 2026-11-23T10:00:12Z |
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then the periods start at 09:00, 09:05 and so on to 09:55
      And zone 48 has 5 for 09:20, null for 09:25 and 09:30, and 6 for 09:35
      And zone 161 has 4 for 09:25 and null for the other 11 periods, without the period of 10:00
      And zone 230 has null for all 12 periods

    Example: Zone 48 quiet for 10 minutes of a simulated hour
      Given the simulated data set "quiet-zone" has been counted
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then zone 48 has null for 09:25 and 09:30
      And the values of zones 48, 161 and 230 add up to 244, 191 and 342

  @R111
  Rule: bench.py first forces a cold start and judges that read on its own

    Example: A cold read over 1 s fails the check, though the p95 of the reads after it passes
      Given the page's function "recipe-page" has the variables PREFIX and COUNTS_TABLE
      When bench.py runs and the first read takes 1,200 ms and the next 100 take 100 ms each
      Then the function's variables are PREFIX, COUNTS_TABLE and a new BENCH_COLD_START
      And bench.py reports the cold read 1200.0 ms and the p95 100.0 ms, and fails

    Example: A cold read under 1 s passes
      Given the page's function "recipe-page" has the variables PREFIX and COUNTS_TABLE
      When bench.py runs and the first read takes 640 ms and the next 100 take 100 ms each
      Then bench.py reports the cold read 640.0 ms and the p95 100.0 ms, and passes

  @R120
  Rule: Each zone carries the time of its newest request counted, and the page the newest of all

    Example: Two zones fresh, one never counted
      Given only these latest items are stored:
        | zone_id | newest_event_time    |
        | 161     | 2026-11-23T09:54:59Z |
        | 230     | 2026-11-23T09:58:12Z |
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then the JSON has data_as_of 2026-11-23T09:58:12Z and read_at 2026-11-23T10:00:30Z
      And zone 161 has as_of 09:54:59, age 5 minutes, not stale
      And zone 230 has as_of 09:58:12, age 2 minutes, not stale
      And zone 48 has as_of null and is stale
      And the page is not stale

    Example: A borrowed counts table without latest items
      Given only these counts are stored:
        | zone_id | period_start         | requested | newest_event_time    |
        | 161     | 2026-11-23T09:50:00Z | 7         | 2026-11-23T09:54:40Z |
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then zone 161 has as_of 09:54:40, age 5 minutes, not stale

  @R130
  Rule: Data older than STALE_MINUTES is marked stale with its age

    Example: The newest request counted is 19 minutes old
      Given only these latest items are stored:
        | zone_id | newest_event_time    |
        | 48      | 2026-11-23T09:41:05Z |
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then zone 48 is stale with age 19 minutes
      And the page is stale

    Example: The feed stops at 09:40
      Given the simulated data set "feed-stops" has been counted
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then every zone is stale with an age of at least 20 minutes
      And the page is stale

    Example: An outage longer than the hour
      Given only these latest items are stored:
        | zone_id | newest_event_time    |
        | 48      | 2026-11-23T08:12:05Z |
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then every zone has null for all 12 periods
      And zone 48 has as_of 08:12:05 and is stale with age 108 minutes
      And the JSON has data_as_of 2026-11-23T08:12:05Z and the page is stale

  @R140
  Rule: With nothing ever counted, freshness is unknown and the page is stale

    Example: An empty table
      Given no item is stored
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then the JSON has data_as_of null and the page is stale
      And the JSON's message is "no data; freshness unknown"

  @R150
  Rule: Anything but the read routes is refused before DynamoDB is called

    Example: A write and an unknown path
      Given the simulated data set "rush-hour" has been counted
      When these requests are sent to the page:
        | method | path             |
        | POST   | /api/zone-counts |
        | GET    | /api/trips       |
        | DELETE | /                |
      Then the answers are 405, 404 and 405
      And the page called DynamoDB 0 times

  @R160
  Rule: The counts are read with one Query per zone, and no Scan

    Example: One JSON read
      Given the simulated data set "rush-hour" has been counted
      When GET /api/zone-counts is read at 2026-11-23T10:00:30Z
      Then the page called DynamoDB with 3 Query calls, each eventually consistent, and 0 Scan calls

  @R161
  Rule: No route of the page starts an Athena query

    Example: The lake variant's page
      Given the lake job has written its items
      When GET / and GET /api/zone-counts are read
      Then Athena was called 0 times by the page's function

  @R170
  Rule: The lake job writes the query's values and each zone's newest event time

    Example: One run of the job, offline
      Given the fake Athena answers the summary query with these rows:
        | pickup_zone_id | period_start | trips_requested | newest_requested_time |
        | 48             | 2026-11-21   | 4710            | 2026-11-22T23:59:02Z  |
        | 48             | 2026-11-22   | 4688            | 2026-11-22T23:59:02Z  |
        | 161            | 2026-11-22   | 6301            | 2026-11-22T23:57:15Z  |
      When the schedule starts the lake job at 2026-11-23T01:00:00Z
      Then the counts table has 3 count items and the latest items 48 at 2026-11-22T23:59:02Z and 161 at 2026-11-22T23:57:15Z
      And the run log has job dash-refresh, run 2026-11-23T01:00:00Z, run_state finished
      And the job ran its query in the workgroup "recipe-wg"

    Example: The page reads the lake items back by day
      Given the fake Athena answers the summary query with these rows:
        | pickup_zone_id | period_start | trips_requested | newest_requested_time |
        | 48             | 2026-11-21   | 4710            | 2026-11-22T23:59:02Z  |
        | 48             | 2026-11-22   | 4688            | 2026-11-22T23:59:02Z  |
        | 161            | 2026-11-22   | 6301            | 2026-11-22T23:57:15Z  |
      And the schedule has started the lake job at 2026-11-23T01:00:00Z
      And PERIOD is "1d" and STALE_MINUTES is "1560"
      When GET /api/zone-counts is read at 2026-11-23T01:05:00Z
      Then the periods start at 2026-11-11, 2026-11-12 and so on to 2026-11-22
      And zone 48 has 4710 for 2026-11-21, 4688 for 2026-11-22 and null for the other 10 days
      And zone 48 has as_of 2026-11-22T23:59:02Z, age 65 minutes, not stale
      And zone 161 has 6301 for 2026-11-22, as_of 2026-11-22T23:57:15Z, age 67 minutes, not stale
      And zone 230 has null for all 12 periods, as_of null, and is stale

    Example: A late job replaces neither a newer time nor newer counts
      Given the latest item of zone 48 is 2026-11-22T23:59:02Z
      And the count item of zone 48 for 2026-11-22 has requested 6410 and generation 2026-11-23T00:00:05Z
      And a run of the lake job that started at 2026-11-22T23:00:04Z finishes now with zone 48's newest time 2026-11-22T22:10:00Z and 6120 requested for 2026-11-22
      When it writes the count items and the latest items
      Then the latest item of zone 48 is still 2026-11-22T23:59:02Z
      And the count item of zone 48 for 2026-11-22 still has requested 6410
      And the run counts 1 latest item kept, 1 count item kept for zone 48 and 0 errors

    Example: A failed query is recorded with its step
      Given the fake Athena fails the summary query with "TABLE_NOT_FOUND"
      When the schedule starts the lake job at 2026-11-23T01:00:00Z
      Then the run log has run 2026-11-23T01:00:00Z with run_state failed, failed_step "query" and an error_text with "TABLE_NOT_FOUND"
      And the run log has no lock held for dash-refresh

  @R180
  Rule: A job that is still running is not started twice

    Example: The previous run is still waiting for Athena
      Given the run log has job dash-refresh, run lock, with lock_until 2026-11-23T01:02:00Z
      When the schedule starts the lake job at 2026-11-23T01:01:00Z
      Then Athena was called 0 times by that run
      And the run log has run 2026-11-23T01:01:00Z with run_state skipped

  @R190
  Rule: The open page reads the data every 60 seconds

    @browser
    Example: Three minutes with the page open
      Given the simulated data set "rush-hour" has been counted
      When the page is open in a headless browser for 180 seconds on a fake clock
      Then the page has read /api/zone-counts 4 times: on load and at 60, 120 and 180 seconds

  @R200
  Rule: The public URL exists only for the time asked

    Example: A three-hour demonstration
      Given the page is deployed against the fake aws
      When run.sh url-on 3 runs at 2026-12-10T14:00:00Z against the fake aws
      Then the page function has a function URL with auth type NONE
      And the group recipe-dashboard-page has a one-time schedule at 2026-12-10T17:00:00 that deletes that URL

    Example: The schedule cannot be made
      Given the page is deployed against the fake aws
      And the fake aws refuses CreateSchedule
      When run.sh url-on 3 runs at 2026-12-10T14:00:00Z against the fake aws
      Then run.sh url-on fails
      And the page function does not have a function URL

  @R201
  Rule: url-off removes the URL and its schedule at once

    Example: Ending early
      Given the page is deployed against the fake aws
      And run.sh url-on 3 ran at 2026-12-10T14:00:00Z against the fake aws
      When run.sh url-off runs against the fake aws
      Then the page function does not have a function URL
      And the group recipe-dashboard-page does not hold a one-time schedule

  @R210
  Rule: With the slot list, in either variant, the page lists every slot of a day with its state

    Example: A day read at 09:50
      Given SLOTS_TABLE is "recipe-feed-slots"
      And only these slot items are stored for day 2026-11-22:
        | slot_start           | slot_state |
        | 2026-11-22T08:00:00Z | loaded     |
        | 2026-11-22T08:15:00Z | late       |
        | 2026-11-22T08:30:00Z | loading    |
        | 2026-11-22T08:45:00Z | failed     |
      When GET /api/slots?day=2026-11-22 is read at 2026-11-22T09:50:00Z
      Then the JSON has 96 slots in slot order
      And slots 08:00, 08:15, 08:30 and 08:45 are loaded, late, loading and failed
      And slot 09:00 is missing and slots 09:15 and 09:30 are not due
      And 33 slots are missing and 59 are not due
      And the page called DynamoDB with 1 Query call, strongly consistent, and 0 Scan calls

    Example: A day that is not a date
      Given SLOTS_TABLE is "recipe-feed-slots"
      When GET /api/slots?day=yesterday is read at 2026-11-22T09:50:00Z
      Then the answer is HTTP 400
      And the page called DynamoDB 0 times

    Example: Without SLOTS_TABLE there is no slot route
      Given the simulated data set "rush-hour" has been counted
      When GET /api/slots?day=2026-11-22 is read at 2026-11-22T09:50:00Z
      Then the answer is HTTP 404
      And the page called DynamoDB 0 times

  @R100 @R111 @R160 @R170 @live
  Rule: Live, in a sandbox: the page, the job and the URL against AWS (not run yet)

    Example: The deployed page answers within the SLO
      Given the page is deployed with "bash ext-dashboard-page/run.sh --lake" and "run.sh load rush-hour" has run
      When "run.sh url-on 0.1" runs and bench.py reads the URL 100 times
      Then the cold read and the p95 are under 1,000 ms and the URL is gone six minutes later
