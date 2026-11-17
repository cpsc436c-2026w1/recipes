Feature: Tune a prompt against an eval, the main scenario
  GEPA rewrites the instructions on train questions, the winner is chosen on validation questions, and the report
  is on test questions the search never saw.
  README, "Requirements": R10 to R19.

  @R10 @R16
  Scenario: What a run writes
    Given the main scenario has run on the stub
    Then the exit code is 0
    And the run folder holds "answers.json, candidates.json, instructions_after.txt, instructions_before.txt, program.json, report.md, run.json, spend.json"
    And the printed output holds the report and ends with "written to" and the run folder

  @R10
  Scenario: The chosen instructions are the candidate with the best validation score
    Given the main scenario has run on the stub
    Then the candidates' validation scores are "33%, 67%, 100%"
    And instructions_after.txt holds the instructions of the candidate that scored 100%
    And instructions_before.txt holds the seed instructions

  @R11
  Scenario: Only the instructions change between candidates
    Given every call to the stub models is recorded
    When main/run.py runs with "--runs 1"
    Then every task call carried the table description as written
    And the task calls carried 3 different system prompts, one per candidate, each with the same field markers

  @R12
  Scenario: No test question reaches a model before the search has ended
    Given every call to the stub models is recorded
    When main/run.py runs with "--runs 1"
    Then the test questions were loaded once, after the last reflection call
    And no call before that carried a test question
    And every test question was asked after it

  @R13
  Scenario: The report is on the test questions, several runs per prompt
    Given the main scenario has run on the stub
    Then the report's heading says "6 questions, 3 runs per prompt, task model qwen3.6-35b-a3b, offline stub"
    And the report's row "before" is "17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3"
    And the report's row "after" is "83% | 83% / 83% / 83% | 15/18 | wrong value 3"

  @R13
  Scenario: --runs names another number of runs
    When main/run.py runs with "--runs 2"
    Then the report's row "after" is "83% | 83% / 83% | 10/12 | wrong value 2"

  @R14
  Scenario: A reply without SQL is its own failure type
    Given the stub task model returns nothing for "How many orders were paid by card?"
    When main/run.py runs with "--runs 1"
    Then the report's row "before" names the failure "no reply 1"

  @R15
  Scenario: The validation scores are in the report only as what the choice was made on
    Given the main scenario has run on the stub
    Then the report lists the candidates under "## Candidates (validation score, used only to choose)"
    And the test table holds no validation score

  @R18
  Scenario: The report says, per test question, in how many runs each prompt passed
    Given the main scenario has run on the stub
    Then the report's per-question table is headed "runs passed, out of 3 for before and 3 for after"
    And the per-question row "q01" is "0 | 3"
    And the per-question row "q04" is "3 | 3"
    And the per-question row "q06" is "0 | 0"
    And the report holds "With the prompt `after`, 4 of 6 questions pass more often than with `before`, 0 less often, 2 the same."

  @R18
  Scenario: A question the new prompt breaks is counted as passing less often
    Given the stub task model returns nothing for "What is the average tip, in dollars, on cash orders?" once the instructions have a rule added
    When main/run.py runs with "--runs 2"
    Then the per-question row "q04" is "2 | 0"
    And the report holds "1 less often"

  @R19
  Scenario: The run folder says what produced the new instructions
    Given the main scenario has run on the stub
    Then run.json names the task model "qwen3.6-35b-a3b", the reflection model "qwen3.8-27b" and the target "stub"
    And run.json names the versions of dspy and gepa that are installed
    And run.json holds the hash of instructions_after.txt, of the seed and of the two question files
    And run.json holds the caps "max_metric_calls 120, max_reflection_calls 8, max_test_calls 72, max_cad 0.5, runs 3, seed 0"
    And run.json says the search ended at "reflection cap (8 calls)" and gives the test medians of both prompts
    And run.json holds no question text

  @R17
  Scenario: What ended the search is in the report
    Given the main scenario has run on the stub
    Then the report's last line is "The search ended at: reflection cap (8 calls)"
