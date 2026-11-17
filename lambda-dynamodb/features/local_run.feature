@moto_server
Feature: The handler on your machine, against the tables the dynamodb recipe makes
Step 1 of main/run.sh runs `python3 main/handler.py` in the terminal. Here the tables are made by the dynamodb
  recipe's own scripts (../dynamodb, KEEP=1), with the real AWS CLI against a moto server on localhost. Then
  main/run.sh itself runs: its step 1 starts the handler against the same server, and its `aws`, `curl` and `sleep`
  are the stand-ins, so nothing is deployed anywhere. README, "Requirements": R43, R51.

  @R43 @R51
  Scenario: The five answers of the local run
    Given the dynamodb recipe's main and riders-and-drivers scripts have run with KEEP=1 and PREFIX "recipe-ana"
    When main/run.sh runs with PREFIX "recipe-ana" and no Region set, its step 1 against the moto server and its aws a stand-in
    Then the exit code is 0
    And step 1 printed 5 lines, one per event, before the first call that creates something
    And printed line 1 answers "get" with trip "t-0042" held by "d-7"
    And printed line 3 answers "rider" with every trip of "r-03" from "2026-10-08" on that the table holds, in time order
    And printed line 4 is the answer {"claimed": false, "held_by": "d-7"} to the action "claim"
    And printed line 5 is the answer {"accepted": false, "reasons": {"trip": "None", "driver": "ConditionalCheckFailed"}} to the action "accept"
