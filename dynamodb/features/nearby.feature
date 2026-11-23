Feature: ext-nearby: drivers near a pickup point, and the nearest weather stations
  The requirements are in ext-nearby/README.md. Offline, moto stands in for DynamoDB inside the test process. Distances
  are great-circle distances with an Earth radius of 6,371,008.8 m, rounded to the metre. Cells are geohashes,
  computed by ext-nearby/nearby.py. The data sets are described in ext-nearby/README.md, section Data.

  Background:
    Given a local stand-in for DynamoDB with the table "recipe-drivers" of ext-riders-and-drivers
    And nearby.py has made its tables

  @R80
  Rule: A position is stored under its cell and car type

    Example: A standard car near Times Square
      Given no position is stored for driver d-101
      When driver d-101, car type standard, sends the position 40.7585, -73.9850 at 2026-11-23T09:10:00Z
      Then table recipe-drivers-by-cell has driver d-101 under the key "dr5ru7#standard" with that position and time

  @R81
  Rule: A move to another cell, or a change of car type, deletes the old item in the same transaction

    Example: Driver d-101 drives north-east into the next cell
      Given driver d-101, car type standard, is stored at 40.7585, -73.9850 in cell dr5ru7 at 2026-11-23T09:10:00Z
      When driver d-101 sends the position 40.7640, -73.9790 at 2026-11-23T09:10:30Z
      Then "dr5ru7#standard" does not hold driver d-101
      And "dr5rus#standard" holds driver d-101 at 40.7640, -73.9790
      And table recipe-drivers has cell "dr5rus" for driver d-101
      And the three writes were one transaction

    Example: Driver d-101 changes to an xl car in the same cell
      Given driver d-101, car type standard, is stored at 40.7585, -73.9850 in cell dr5ru7 at 2026-11-23T09:10:00Z
      When driver d-101 sends car type xl and the position 40.7585, -73.9850 at 2026-11-23T09:11:00Z
      Then "dr5ru7#standard" does not hold driver d-101
      And "dr5ru7#xl" holds driver d-101 at 40.7585, -73.9850

  @R82
  Rule: A refused transaction keeps a newer position, and retries from the stored driver otherwise

    Example: A position delivered late
      Given driver d-101, car type standard, is stored at 40.7640, -73.9790 in cell dr5rus at 2026-11-23T09:10:30Z
      When the position 40.7585, -73.9850 of 2026-11-23T09:10:00Z arrives for driver d-101
      Then the call reports "older, kept"
      And driver d-101 is still stored in "dr5rus#standard" at 40.7640, -73.9790 with the time 2026-11-23T09:10:30Z

    Example: Two positions read the same old cell, the later one commits second
      Given driver d-101, car type standard, is stored at 40.7585, -73.9850 in cell dr5ru7 at 2026-11-23T09:10:00Z
      And writer A and writer B have both read driver d-101 in cell dr5ru7
      When writer A commits the position 40.7640, -73.9790 of 2026-11-23T09:10:30Z
      And writer B then commits the position 40.7700, -73.9750 of 2026-11-23T09:10:40Z
      Then writer B's first transaction is refused, it reads cell dr5rus, and its retry commits
      And only "dr5rut#standard" holds driver d-101, and "dr5ru7#standard" and "dr5rus#standard" do not

    Example: Two positions read the same old cell, the earlier one commits second
      Given driver d-101, car type standard, is stored at 40.7585, -73.9850 in cell dr5ru7 at 2026-11-23T09:10:00Z
      And writer A and writer B have both read driver d-101 in cell dr5ru7
      When writer A commits the position 40.7640, -73.9790 of 2026-11-23T09:10:30Z
      And writer B then commits the position 40.7700, -73.9750 of 2026-11-23T09:10:20Z
      Then writer B reports "older, kept"
      And only "dr5rus#standard" holds driver d-101, and "dr5ru7#standard" and "dr5rut#standard" do not

  @R90
  Rule: A request reads its cell and the 8 around it, and answers the drivers within 500 m, nearest first

    Example: Standard drivers near Times Square
      Given only these drivers are stored:
        | driver_id | car_type | lat     | lon      |
        | d-101     | standard | 40.7585 | -73.9850 |
        | d-102     | standard | 40.7605 | -73.9830 |
        | d-103     | xl       | 40.7578 | -73.9860 |
        | d-104     | standard | 40.7550 | -73.9890 |
        | d-105     | standard | 40.7630 | -73.9800 |
        | d-106     | standard | 40.7575 | -73.9858 |
      When a nearby request asks for car type standard at 40.7580, -73.9855
      Then the nearby answer is, in this order:
        | driver_id | distance_m |
        | d-106     | 61         |
        | d-101     | 70         |
        | d-102     | 349        |
        | d-104     | 445        |
      And the request sent 9 Query calls from a pool of 9 threads, for the cells dr5ruh, dr5ruk, dr5rus, dr5ru5, dr5ru7, dr5rue, dr5ru4, dr5ru6 and dr5rud, and 0 Scan calls
      And d-102 was read from the neighbour cell dr5ruk, not from the point's cell dr5ru7

    Example: Never more than 5 drivers
      Given 7 standard drivers are stored within 100 m of 40.7580, -73.9855
      When a nearby request asks for car type standard at 40.7580, -73.9855
      Then the nearby answer holds 5 drivers, nearest first

  @R91
  Rule: A point outside the service area is an error, not an empty list

    Example: A request at the airport
      Given the six drivers of the example above are stored
      When a nearby request asks for car type standard at 40.6413, -73.7781
      Then the nearby answer is the error "outside coverage"
      And the request made 0 Query calls

    Example: Points at the edge of coverage
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      When the 5 requests of SIM03 "edge-of-coverage" outside the service area are sent
      Then each nearby answer is the error "outside coverage"

  @R92
  Rule: An unknown car type is an error

    Example: A car type the system does not have
      Given the six drivers of the example above are stored
      When a nearby request asks for car type limo at 40.7580, -73.9855
      Then the nearby answer is the error "unknown car type"
      And the request made 0 Query calls

  @R93
  Rule: No driver within 500 m inside the service area is an empty list with the radius

    Example: A quiet street in the north of the area
      Given the six drivers of the example above are stored
      When a nearby request asks for car type standard at 40.8600, -73.9300
      Then the nearby answer is an empty list with radius 500 m

    Example: The points of SIM03 inside the area, far from every driver
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      When the 5 requests of SIM03 "edge-of-coverage" inside the service area are sent
      Then each nearby answer is an empty list with radius 500 m

  @R94
  Rule: With no write in flight, every answer equals a check against all stored drivers

    Example: The check answers are computed before the run
      Then nearby_expected.jsonl holds what nearby.py expect computes from SIM01 and SIM02 alone, 837 lines

    Example: The rush hour against moving drivers
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      When SIM02's positions and SIM01's 837 requests are replayed in time order, with strongly consistent Queries
      Then 837 of the 837 answers equal their line in nearby_expected.jsonl
      And 837 of the 837 answers have the same drivers, in the same order, as a strongly consistent Scan of every stored driver of that car type
      And the replay reported as many positions "older, kept" as the answer from SIM02's _truth/ counts, 69
      And every Scan call was made by the check, and every request made 9 Query calls

  @R95
  Rule: The bench sends the requests one at a time and holds the p99 against 1,000 ms

    Example: Nine slow answers in 837 still pass
      Given 837 response times, of which the 9 slowest are 1200 ms and the others 100 ms
      Then the bench reports p99 804 ms and pass

    Example: Ten slow answers in 837 fail
      Given 837 response times, of which the 10 slowest are 1200 ms and the others 100 ms
      Then the bench reports p99 1200 ms and fail

    Example: A failed request counts as over 1 s
      Given 827 response times of 100 ms and 10 failed requests
      Then the bench reports p99 inf ms and fail

    Example: The bench against the stand-in
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      When the bench runs SIM01 with a clock that advances 1 ms each time it is read
      Then the bench sent 837 requests one at a time, with 7533 eventually consistent Query calls and 0 Scan calls
      And the bench reports pass
      And the summary of the bench has its p50, p95 and p99, and the command prints its read units before its verdict

  @R100
  Rule: The precision of static points comes from their spacing

    Example: Four weather stations
      Given the simulated data set SIM04 "weather-stations"
      When nearby.py load stores the points
      Then the median distance to the nearest other station is 13019 m
      And the precision is 4, whose cell's shorter side is 19546 m
      And s-cpk, s-lga and s-ewr are stored under "dr5r#weather" and s-jfk under "dr5x#weather"

  @R101
  Rule: A nearest-N request reads rings of cells until its N points lie within the covered distance

    Example: The three stations nearest Times Square
      Given the simulated data set SIM04 "weather-stations" is loaded
      When a nearest request asks for 3 points of type weather at 40.7580, -73.9855
      Then the nearest answer is, in this order:
        | point_id | distance_m |
        | s-cpk    | 2699       |
        | s-lga    | 9173       |
        | s-ewr    | 17626      |
      And the request read ring 1 only, with 9 Query calls, covering 19546 m

    Example: Four stations need the second ring
      Given the simulated data set SIM04 "weather-stations" is loaded
      When a nearest request asks for 4 points of type weather at 40.7580, -73.9855
      Then the nearest answer ends with s-jfk at 23036 m
      And the request read rings 1 and 2, with 25 Query calls, covering 39092 m

  @R102
  Rule: When the rings run out, the answer says it is not complete

    Example: Four stations with one ring allowed
      Given the simulated data set SIM04 "weather-stations" is loaded and MAX_RINGS is 1
      When a nearest request asks for 4 points of type weather at 40.7580, -73.9855
      Then the nearest answer is s-cpk, s-lga and s-ewr with complete false and covered distance 19546 m
      And s-jfk, read at 23036 m, is not in the nearest answer

  @R110
  Rule: nearby.py makes its two tables on demand, with a ceiling, tagged, and keeps a table that is there

    Example: The first run creates both tables
      Then the tables "recipe-drivers-by-cell" and "recipe-points-by-cell" have partition key "cell_type", on-demand capacity and a ceiling of 100 read and 100 write units a second
      And both tables have the tags Project=436c, Team=recipe, purpose=recipe and prefix=recipe, and a tag Owner
      And the ledger records both tables "created" and "recipe-drivers" "borrowed"

    Example: A second run keeps the tables
      When nearby.py makes its tables again
      Then no table was created the second time, and the ledger records each table "created" once

  @R111
  Rule: Without the drivers table, nothing is created

    Example: ext-riders-and-drivers was not run first
      Given a local stand-in for DynamoDB without the table "recipe-drivers"
      When "nearby.py tables" runs
      Then it exits with code 1 and names the command to run first, "KEEP=1 bash ext-riders-and-drivers/run.sh"
      And the stand-in has no table "recipe-drivers-by-cell"

  @R112
  Rule: A dry run prints its commands and runs none

    Example: DRY=1
      When "ext-nearby/run.sh" runs with DRY=1 and KEEP=1
      Then aws was not called
      And the output holds "nearby.py replay --speed 5"
      And the output holds "nearby.py bench"
      And the output does not hold "seeded"

  @R113
  Rule: clean_up.py deletes what its ledger created and removes its attributes from the borrowed table

    Example: A clean-up after a run
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      And driver d-1 of ext-riders-and-drivers, status available, has a position stored
      And the simulated data set SIM04 "weather-stations" is loaded
      When the nearby clean-up "clean_up.py --yes" runs
      Then it exits with code 0
      And the stand-in has neither table "recipe-drivers-by-cell" nor "recipe-points-by-cell"
      And the stand-in still has the table "recipe-drivers", with driver d-1 available and without a cell, and no item of a SIM02 driver
      And the ledger records both tables "deleted"

    Example: Without --yes it deletes nothing
      When the nearby clean-up "clean_up.py" runs
      Then it exits with code 0
      And the stand-in still has the tables "recipe-drivers-by-cell" and "recipe-points-by-cell"

    Example: A table another run made is kept
      Given the ledger is empty
      When the nearby clean-up "clean_up.py --yes" runs
      Then the stand-in still has the tables "recipe-drivers-by-cell" and "recipe-points-by-cell"

    Example: Another recipe's table in the same ledger is kept
      Given the ledger also records the table "recipe-runs" as created by another recipe
      When the nearby clean-up "clean_up.py --yes" runs
      Then the stand-in still has the table "recipe-runs"

    Example: A second clean-up writes nothing
      Given the drivers of SIM02 "drivers-moving" are stored at their start positions
      And the nearby clean-up "clean_up.py --yes" has run
      When the nearby clean-up "clean_up.py --yes" runs
      Then it exits with code 0
      And the second clean-up made no write calls

  @R114
  Rule: The whole recipe's clean-up runs the extension's first

    Example: clean_up.sh with a ledger of ext-nearby
      Given the ledger of ext-nearby has a line
      When "clean_up.sh" runs with DRY=1
      Then the output holds "ext-nearby/clean_up.py --yes"
      And aws was not called

  @R115
  Rule: A run without KEEP=1 ends with the whole recipe's clean-up

    Example: The end of a run
      When "ext-nearby/run.sh" runs with DRY=1
      Then the output holds "== Clean up"
      And the output holds "delete-table --table-name recipe-drivers"

    Example: KEEP=1
      When "ext-nearby/run.sh" runs with DRY=1 and KEEP=1
      Then the output holds "Done. KEEP=1"
      And the output does not hold "delete-table"
