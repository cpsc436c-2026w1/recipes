Feature: Simulated data whose right answer is written down
  The generator and the files it writes. A simulated data set is named by its id and described in the README,
  section Data.

  @R10
  Rule: The generator writes the deliveries by hour of receipt, and the events and the injected faults under _truth/

    Example: Generating the rush hour
      When the generator runs with the data set SIM01 "rush-hour" and the seed 436
      Then sim/data/SIM01/_truth/events.jsonl has 2511 lines
      And sim/data/SIM01/_truth/faults.jsonl has 6 lines
      And the delivery files of SIM01 hold 2515 lines in 3 folders trip-events/dt=2026-11-23/hour=<hour>/

  @R20
  Rule: The same data set and seed give byte-identical files

    Example: Two runs with the seed 436
      Given the generator has run with the data set SIM01 "rush-hour" and the seed 436
      When the generator runs again with the data set SIM01 "rush-hour" and the seed 436 into another folder
      Then the two folders hold the same files with the same bytes

  @R30
  Rule: _truth/faults.jsonl lists every injected fault, and the count of each kind is the data set's

    Scenario Outline: The faults of two data sets
      Given the generator has run with the data set <data_set> and the seed 436
      When _truth/faults.jsonl is read
      Then it lists <twice> "twice", <late> "late", <never> "never" and <changed> "changed"

      Examples:
        | data_set | twice | late | never | changed |
        | SIM01    | 4     | 2    | 0     | 0       |
        | SIM04    | 4     | 2    | 9     | 2       |

    Example: Each late delivery is late by the data set's delay
      Given the generator has run with the data set SIM01 "rush-hour" and the seed 436
      When _truth/faults.jsonl is read
      Then each "late" delivery is a line of the delivery files with a received_time 6 to 10 minutes after its event_time

    Example: The faults of SIM04 leave SIM01's trips alone
      Given the generator has run with the data set SIM04 "all-faults" and the seed 436
      When _truth/faults.jsonl is read
      Then its "twice" and "late" deliveries carry the same events as those of SIM01
      And none of its "never" or "changed" trips is a trip that SIM01's faults touch

  @R40
  Rule: The answer from _truth/ counts each event once, late ones included, and leaves out the events that never arrive

    Example: The rush hour, whose late requests count
      Given the generator has run with the data set SIM01 "rush-hour" and the seed 436
      When the answer is computed from _truth/
      Then it holds 2511 events and 837 trips, requested per zone 304, 191 and 342 for zones 48, 161 and 230

    Example: Every kind of fault
      Given the generator has run with the data set SIM04 "all-faults" and the seed 436
      When the answer is computed from _truth/
      Then it holds 2504 events and 834 trips, requested per zone 304, 188 and 342 for zones 48, 161 and 230
      And 834 trips are completed
      And the fare of each of the 2 changed trips is 5.00 more than in its first trip_completed

  @R50
  Rule: A line that does not match its model stops the read with the file, the line and the field

    Scenario Outline: Broken lines of the bad-lines data set
      Given the simulated data set SIM02 "bad-lines"
      When line <line> of _truth/events.jsonl is validated against its model
      Then the validation stops with an error naming "events.jsonl", line <line> and the field "<field>"

      Examples:
        | line | field      |
        | 4    | tip        |
        | 7    | event_time |
        | 9    | trip_id    |
