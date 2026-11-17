Feature: The two variants
  GEPA without DSPy over a prompt string, and the eval matrix over models and prompts.
  The READMEs of the two variants (the folders ext-*), "Requirements": R60 to R63, R70 to R73.

  @R60 @R61
  Scenario: GEPA over a prompt string writes the same table as main
    Given every call to the stub models is recorded
    When ext-gepa-direct/optimize_direct.py runs
    Then the exit code is 0
    And the run folder holds "candidates.json, instructions_after.txt, report.md, spend.json"
    And the report's heading says "6 questions, 3 runs per prompt, gepa library directly, task model qwen3.6-35b-a3b, offline stub"
    And the report's row "before" is "17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3"
    And the report's row "after" is "83% | 83% / 83% / 83% | 15/18 | wrong value 3"
    And every task call's system prompt started with the fixed head of the prompt
    And the test questions were loaded once, after the last reflection call
    And no call before that carried a test question

  @R62
  Scenario: A gateway that refuses the first call leaves no result
    Given a gateway stand-in that refuses every request after the first 0 with HTTP 401
    When ext-gepa-direct/optimize_direct.py runs with "--target gateway --runs 1"
    Then the exit code is 1
    And the error output holds "the search stopped before it had a result, so nothing was written"
    And the error output holds "401"
    And no run folder was written
    And the gateway received fewer than 3 requests

  @R63
  Scenario: A call refused in the search cuts it, and the result is still written
    Given a gateway stand-in that refuses every request after the first 40 with HTTP 429
    When ext-gepa-direct/optimize_direct.py runs with "--target gateway --runs 1"
    Then the exit code is 1
    And the report holds "(every role counted) failed, so the search was cut there (task:"
    And the run folder holds "candidates.json, instructions_after.txt, report.md, spend.json"

  @R70 @R71
  Scenario: The matrix has one row per model and prompt, the seed prompt among them
    Given a tuned prompt saved from the main scenario's stub run
    When ext-eval-matrix/matrix.py runs with the tuned prompt as "after"
    Then the exit code is 0
    And the matrix has 4 rows
    And the matrix row "qwen3.6-35b-a3b (stub) | seed" is "17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3 | 0.000365 | 0.0203"
    And the matrix row "qwen3.6-35b-a3b (stub) | after" is "83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000431 | 0.0239"
    And the matrix row "qwen3.8-27b (stub) | seed" is "50% | 50% / 50% / 50% | 9/18 | wrong unit 6, wrong value 3 | 0.000721 | 0.0400"
    And the matrix row "qwen3.8-27b (stub) | after" is "83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000862 | 0.0479"

  @R71
  Scenario: Without a --prompt, the matrix still has the seed prompt for every model
    When ext-eval-matrix/matrix.py runs with "--runs 1 --models qwen3.6-35b-a3b"
    Then the matrix has 1 rows
    And the matrix row "qwen3.6-35b-a3b (stub) | seed" is "17% | 17% | 1/6 | SQL error 2, wrong unit 2, wrong value 1 | 0.000122 | 0.0203"

  @R72
  Scenario: The matrix makes no search and no reflection call
    Given every call to the stub models is recorded
    When ext-eval-matrix/matrix.py runs with "--runs 1"
    Then 12 calls were made to a stub model, none of them a reflection call

  @R73
  Scenario: A cell that passes the call cap stops the matrix, and the cells that finished are reported
    When ext-eval-matrix/matrix.py runs with "--runs 1 --max-calls 8"
    Then the exit code is 1
    And the error output holds "the matrix stopped at qwen3.8-27b with the prompt 'seed'"
    And the error output holds "8 calls reached the cap of 8"
    And the error output holds "Raise --max-calls or --max-cad, or lower --runs."
    And the matrix has 1 rows
    And the report holds "**The matrix stopped before it was done:** 1 of 2 cells finished."
    And the report holds "Spent in all, the cell that was cut included:"
    And the matrix's spend file counts 8 calls in all, and its money is the total in the report

  @R73
  Scenario: With two prompts, the matrix that is cut keeps the cells of both prompts that finished
    Given a tuned prompt saved from the main scenario's stub run
    When ext-eval-matrix/matrix.py compares the seed with the tuned prompt, with "--runs 1 --max-calls 15"
    Then the exit code is 1
    And the error output holds "the matrix stopped at qwen3.8-27b with the prompt 'seed'"
    And the matrix has 2 rows
    And the report holds "**The matrix stopped before it was done:** 2 of 4 cells finished."
    And the matrix's spend file counts 15 calls in all, and its money is the total in the report

  @R73
  Scenario: A gateway that starts to refuse calls stops the matrix the same way
    Given a gateway stand-in that refuses every request after the first 6 with HTTP 429
    When ext-eval-matrix/matrix.py runs with "--target gateway --runs 1"
    Then the exit code is 1
    And the matrix has 1 rows
    And the report holds "**The matrix stopped before it was done:** 1 of 2 cells finished."
