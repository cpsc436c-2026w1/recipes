Feature: A whole run checked against the answer the files give
  The team's pipeline is the function the step code names (here the stand-in rides.pipeline); the answer is
  computed from _truth/events.jsonl and _truth/faults.jsonl of the data set (sim/truth.py).

  @R70
  Rule: Offline, the run replays into stand-ins and each named table equals the answer from _truth/

    Example: The rush hour offline
      Given the simulated data set SIM01 "rush-hour"
      When the pipeline processes the run offline
      Then the table of trips requested per zone holds 304, 191 and 342 trips for zones 48, 161 and 230

    Example: Every kind of fault offline
      Given the simulated data set SIM04 "all-faults"
      When the pipeline processes the run offline
      Then the table of trips requested per zone holds 304, 188 and 342 trips for zones 48, 161 and 230
      And the stored fare of each of the 2 changed trips is 5.00 more than in its first trip_completed

  @R80 @live
  Rule: With @live, the same Then is checked against the deployed system, and the output is kept as evidence

    Example: The rush hour on AWS
      Given the simulated data set SIM01 "rush-hour"
      When the pipeline processes the run in the deployed system with run id "20261123T0900-SIM01"
      Then the table of trips requested per zone holds 304, 191 and 342 trips for zones 48, 161 and 230
      And the run's output is in evidence/20261123T0900-SIM01/
