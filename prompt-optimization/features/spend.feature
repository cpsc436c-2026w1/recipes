Feature: The spend cap in code
  Calls and Canadian dollars are counted per role; a call past a cap is refused, and a refused or failed call
  cuts the search without losing what it found.
  README, "Requirements": R20 to R28.

  @R20 @R28
  Scenario: The default caps: 8 reflection calls end the stub's search
    Given the main scenario has run on the stub
    Then the spend file counts 8 calls for "reflect"
    And the report's last line is "The search ended at: reflection cap (8 calls)"
    And the report's spend table has the roles "reflect, task, test-after, test-before" and a total of 149 calls
    And the report's total in CAD is the sum of the roles in the spend file
    And the spend file holds nothing for calls in flight

  @R20
  Scenario: The budget of metric calls ends a search that has reflection calls left
    When main/run.py runs with "--runs 1 --max-reflection-calls 100"
    Then the report's last line is "The search ended at: the budget of metric calls (--max-metric-calls)"
    And the spend file counts between 120 and 132 calls for "task"

  @R20
  Scenario: The search stops at 60% of the money cap, and the test runs still fit
    # A cap with room, above the 60%, for the calls in flight: four task calls or one reflection call are
    # held at about 0.002 CAD together.
    When main/run.py runs with "--runs 1 --max-cad 0.01 --max-reflection-calls 50 --max-metric-calls 400"
    Then the exit code is 0
    And the report's last line is "The search ended at: money cap (search stops at 60% of 0.01 CAD)"
    And the search spent between 0.006 and 0.0069 CAD
    And the whole run spent less than 0.01 CAD

  @R21 @R22
  Scenario: A call past the money cap is refused, the search is cut, and its result is still written
    # One task call is held at about 0.0005 CAD (its prompt's bytes as input tokens, 4,000 output tokens): the cap
    # has room for one call in flight and not for two, so the second call of the first evaluation is refused.
    Given every call to the stub task model takes 0.05 seconds
    When main/run.py runs with "--runs 1 --max-cad 0.0007"
    Then the report's last line starts with "The search ended at: spend cap, call refused (spent 0.000000 CAD, 0.0004"
    And the report's last line holds "held for calls in flight, this call can cost up to 0.0004"
    And the report's last line holds "cap 0.0007), model call 2 (every role counted)"
    And the report's last line ends with "was refused and the search was cut there"
    And the run folder holds "answers.json, candidates.json, instructions_after.txt, instructions_before.txt, program.json, report.md, run.json, spend.json"
    And the spend file counts 0 calls for "reflect"
    And the spend file counts 1 calls for "task"
    And the whole run spent more than nothing and at most 0.0007 CAD

  @R21
  Scenario: Calls in flight cannot take the run past the money cap
    # 20 test runs per prompt cost more than what the search leaves under the cap. Without the hold, the four
    # threads each passed the check before any of them was charged, and the run ended above the cap.
    Given every call to the stub task model takes 0.01 seconds
    When main/run.py runs with "--runs 20 --max-cad 0.004"
    Then the exit code is 1
    And the report holds "**The test runs stopped before they were done:**"
    And the whole run spent more than nothing and at most 0.004 CAD

  @R21
  Scenario: A cap below the most one call can cost lets no call through
    When main/run.py runs with "--runs 1 --max-cad 0.00002"
    Then the report's last line holds "this call can cost up to 0.0004"
    And the report's last line holds "cap 0.000020), model call 1 (every role counted) was refused"
    And the spend file counts no call and no money

  @R23
  Scenario: Test runs that pass their own cap stop, and the report says what did not run
    When main/run.py runs with "--max-test-calls 10"
    Then the exit code is 1
    And the error output holds "the test runs stopped: test-before: 10 calls reached the cap of 10"
    And the error output holds "Raise --max-cad or --max-test-calls, or lower --runs."
    And the report holds "**The test runs stopped before they were done:**"
    And the report holds "Not tested, or not finished: before, after."
    And the report has no row "before"
    And the spend file counts 10 calls for "test-before"
    And the run folder holds "answers.json, candidates.json, instructions_after.txt, instructions_before.txt, program.json, report.md, run.json, spend.json"

  @R24
  Scenario: The test runs have their own count of calls
    When main/run.py runs with "--runs 1 --max-metric-calls 30"
    Then the exit code is 0
    And the spend file counts 6 calls for "test-before"
    And the spend file counts 6 calls for "test-after"
    And the spend file counts between 30 and 42 calls for "task"

  @R25
  Scenario: A model without a price is refused before any call
    Given every call to the stub models is recorded
    When main/run.py runs with "--task-model some-other-model"
    Then the exit code is 1
    And the error output holds "no price for model 'some-other-model'"
    And no call was made to a stub model
    And no run folder was written

  @R26
  Scenario: A failed model call is not scored as a wrong answer: it cuts the search
    Given the stub task model fails its 20th call with "HTTP Error 429: Too Many Requests"
    When main/run.py runs with "--runs 1"
    Then the report's last line starts with "The search ended at: model call 2"
    And the report's last line holds "(every role counted) failed, so the search was cut there"
    And the report's last line holds "429"
    And the spend file counts 1 calls for "reflect"
    And the report has no row "before" with the failure "SQL error" counted for the failed call
    And the run folder holds "answers.json, candidates.json, instructions_after.txt, instructions_before.txt, program.json, report.md, run.json, spend.json"
    And the spend file holds nothing for calls in flight

  @R26
  Scenario: After a failed call, the other questions of that evaluation are not sent
    # Calls 13 to 18 are the six validation questions of the first new candidate, sent by four threads.
    Given the stub task model fails its 13th call with "HTTP Error 429: Too Many Requests"
    When main/run.py runs with "--runs 1"
    Then the report's last line starts with "The search ended at: model call"
    And the spend file counts between 13 and 16 calls for "task"
    And the spend file holds nothing for calls in flight

  @R27
  Scenario: A model call that fails in the test runs stops them, and the search result stays
    Given the stub task model fails every call once the test questions are loaded
    When main/run.py runs with "--runs 1"
    Then the exit code is 1
    And the error output holds "the test runs stopped: test-before:"
    And the report holds "Not tested, or not finished: before, after."
    And instructions_after.txt holds the instructions of the candidate that scored 100%
    And the spend file holds nothing for calls in flight
