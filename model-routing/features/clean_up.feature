Feature: Clean-up removes the logs a run wrote, and nothing else
  The recipe creates nothing in AWS: clean_up.py deletes the JSON-lines logs under output/.
  README, "Requirements": R40 to R42. Every scenario works on a copy of output/ in a temporary folder.

  Background:
    Given a copy of the recipe's output folder with the logs a run writes

  @R40
  Scenario: Clean-up after a run
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "deleted output/misroutes.jsonl"
    And the printed output holds "deleted output/eval-misroutes.jsonl"
    And the printed output holds "deleted output/routes.jsonl"
    And the copy has no ".jsonl" file
    And the captured runs in the copy are as they were

  @R41
  Scenario: A dry run prints the names and deletes nothing
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And 3 printed lines start with "would delete output/"
    And the copy still has the logs

  @R42
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then the exit code is 0
    And 3 printed lines start with "deleted output/"
