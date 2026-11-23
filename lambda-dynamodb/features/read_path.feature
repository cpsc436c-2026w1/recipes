Feature: ext-read-path: my trip on the screen, also after a quiet time
  ext-read-path/README.md, "Requirements": R220 to R320. Data sets: ext-read-path/README.md, "Data". Offline, a
  fake URL answers the calls with main/handler.py against moto, adds a request id and writes a REPORT line to a fake
  log group; a fake clock stands in for the waits. @live examples run against the function of main/run.sh.

  @R220
  Rule: Each variant gets 200 calls, one at a time, from a fixed list of keys

    Example: The read mix
      Given the variants of ext-read-path/variants.toml
      When ext-read-path/run.sh plans its calls with seed 436
      Then the plan has 600 calls: 200 get-eventual, 200 get-strong and 200 rider
      And each of the trips t-0000 to t-0049 is read 4 times as get-eventual and 4 times as get-strong
      And each of the riders r-00 to r-04 is read 40 times
      And the same seed gives the same order

    Example: The calls go one at a time
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      When ext-read-path/run.sh runs against the fake URL
      Then 600 calls are stored, each with its read units, caller time, HTTP status and request id
      And no call was sent before the answer of the call before it

    Example: Two key designs of a team
      Given variants.toml names the variants topic and topic-author with the same 50 keys
      When ext-read-path/run.sh plans its calls with seed 436
      Then the plan has 400 calls: 200 topic and 200 topic-author

  @R230
  Rule: Each call is joined with its REPORT line

    Example: Two REPORT lines of the captured run
      Given calls with these request ids and REPORT lines:
        | request_id                           | duration_ms | billed_duration_ms | memory_mb | init_duration_ms |
        | 3b93f44a-7fe9-40ee-b778-c5e1937b2692 | 316.02      | 810                | 128       | 493.18           |
        | f52c5468-b161-4c17-837e-5269740c6a08 | 24.70       | 25                 | 128       |                  |
      When the calls are stored and joined
      Then results.duckdb has both calls with their duration, billed duration, memory size and init duration
      And their billed GB-seconds are 0.10125 and 0.003125

  @R240
  Rule: A call with an Init Duration is cold, a call without one is warm

    Example: The same two calls
      Given calls with these request ids and REPORT lines:
        | request_id                           | duration_ms | billed_duration_ms | memory_mb | init_duration_ms |
        | 3b93f44a-7fe9-40ee-b778-c5e1937b2692 | 316.02      | 810                | 128       | 493.18           |
        | f52c5468-b161-4c17-837e-5269740c6a08 | 24.70       | 25                 | 128       |                  |
      When the calls are stored and joined
      Then call 3b93f44a-7fe9-40ee-b778-c5e1937b2692 is cold and call f52c5468-b161-4c17-837e-5269740c6a08 is warm

  @R250
  Rule: The idle round sends one call per start, only after its gap

    Example: Started too early
      Given the last call to the URL was at 10:00:00 and the next gap of the plan is 15 minutes
      When ext-read-path/run.sh --idle is started at 10:09:00
      Then no call is sent
      And it prints "next call due at 10:15:00"

    Example: Started after the gap
      Given the last call to the URL was at 10:00:00 and the next gap of the plan is 15 minutes
      And the REPORT line of the next call will have an Init Duration
      When ext-read-path/run.sh --idle is started at 10:16:00
      Then one get is sent and stored as "after idle" with gap 15 and class cold
      And the next gap of the plan is 30 minutes

  @R260
  Rule: The fresh-copy round changes the function's configuration before each call

    Example: Offline, three rounds
      Given the fake function's update finishes after 2 status reads
      When ext-read-path/run.sh --fresh 3 --no-plan runs against the fake aws
      Then READ_PATH_ROUND is set to 1, 2 and 3, each before one get
      And each get is sent only after LastUpdateStatus is Successful
      And the 3 calls are stored as "fresh copy", each cold
      And READ_PATH_ROUND is removed at the end, and PREFIX is kept

  @R270
  Rule: The busy rider is written before the calls and deleted after them

    Example: Offline, the writes and deletes
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      When ext-read-path/run.sh --busy-rider --no-plan runs against the fake URL
      Then 300 trips of r-busy were written before the first rider call for r-busy and are deleted after the last
      And 200 calls are stored as rider for r-busy

    @live
    Example: The busy rider's Query reads more units than a quiet rider's
      Given the API of main/run.sh is up with KEEP=1
      When ext-read-path/run.sh --busy-rider runs in the sandbox
      Then the median read units are 3.0 for rider r-busy and 0.5 for rider r-03

  @R280
  Rule: With the writer, the busy rider's calls run again while updates go on

    Example: Offline, the writer's pace and its throttled writes
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      And the fake DynamoDB throttles 4 of the writer's updates once each
      When ext-read-path/run.sh --busy-rider --writer 5 --no-plan runs against the fake URL
      Then 400 busy-rider calls are stored, 200 with writer off and 200 with writer on
      And the writer sent no more than 5 updates in any second
      And the writer reports 4 throttled and 4 retried writes, and 300 updates done

  @R290
  Rule: The summary gives p50, p95 and p99 per row, each only from enough calls

    Example: Two hundred warm calls of one variant
      Given the warm get-eventual calls have these caller times:
        | caller_ms | calls |
        | 100       | 190   |
        | 300       | 8     |
        | 900       | 2     |
      When the summary is written
      Then the row get-eventual, warm has 200 calls, p50 100.0 ms, p95 110.0 ms and p99 306.0 ms

    Example: Ten calls are too few for p95 and p99
      Given the warm get-strong calls have these caller times and read units:
        | caller_ms | read_units |
        | 120       | 1.0        |
        | 95        | 1.0        |
        | 101       | 1.0        |
        | 99        | 1.0        |
        | 140       | 1.0        |
        | 97        | 1.0        |
        | 102       | 1.0        |
        | 98        | 1.0        |
        | 610       | 1.0        |
        | 100       | 1.0        |
      When the summary is written
      Then the row get-strong, warm has 10 calls, p50 100.5 ms and median read units 1.0
      And its p95 and p99 say "fewer than 20 calls" and "fewer than 100 calls"

    Example: After-idle calls are listed one by one
      Given 4 after-idle calls with gaps 5, 15, 30 and 60 and caller times 98, 101, 812 and 840 ms
      When the summary is written
      Then the summary lists 4 after-idle lines with those gaps and times, below the warm and cold rows

  @R300
  Rule: Each variant has a cost of 1,000 calls, warm and cold

    Example: Medians of the captured run
      Given the medians of get-eventual are billed 25 ms at 128 MB and 0.5 read units
      And the medians of get-strong are billed 25 ms at 128 MB and 1.0 read unit
      And a cold get-eventual is billed 810 ms at 128 MB and 0.5 read units
      When the summary is written
      Then 1,000 calls cost 0.000310 USD for get-eventual, 0.000379 USD for get-strong and 0.001619 USD for a cold get-eventual

  @R310
  Rule: A call that does not answer 200 is counted as failed and left out

    Example: Two throttled calls
      Given 200 calls of rider, of which 2 answered HTTP 429
      When the summary is written
      Then the row rider counts 198 calls in its percentiles and 2 failed calls

  @R320
  Rule: The run changes only its own items and its one variable, and undoes both

    Example: A run with every option, offline, and one call fails
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      And the fake URL fails one call
      When ext-read-path/run.sh --busy-rider --writer 5 --fresh 3 runs against the fake aws
      Then the only items written, updated or deleted are the 300 trips of r-busy, and none of them is left
      And the only change to the function is READ_PATH_ROUND, which is removed at the end
      And the only files it wrote are results.duckdb and its ledger lines, which end with both undone

    Example: The busy rider's trips are deleted also when the calls stop with an error
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      And the fake URL breaks on the 10th busy-rider call
      When ext-read-path/run.sh --busy-rider --no-plan runs against the fake URL
      Then the run ends with an error
      And none of the 300 trips of r-busy is left

  @R321
  Rule: A run that would overwrite what it did not make stops before its first call

    Example: The busy rider and the variable are there already
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      And rider r-busy already has one trip in the fake tables
      And the fake function already has READ_PATH_ROUND
      When ext-read-path/run.sh --busy-rider --fresh 3 runs against the fake aws
      Then the run stops with exit code 1 before its first call
      And it prints "already has items"
      And it prints "already has the variable READ_PATH_ROUND"
      And the trip of r-busy and READ_PATH_ROUND are as they were

    Example: What a stopped run of this recipe left is taken over and undone
      Given the simulated data set SIM01 "stored-trips" in the fake tables
      And a stopped run left one of the 300 trips of r-busy in the fake tables
      And the fake function already has READ_PATH_ROUND
      And the ledger records both as created by an earlier run
      When ext-read-path/run.sh --busy-rider --fresh 3 --no-plan runs against the fake aws
      Then none of the 300 trips of r-busy is left
      And READ_PATH_ROUND is removed at the end, and PREFIX is kept
