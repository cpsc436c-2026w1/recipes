Feature: Clean-up removes the files a run wrote, and nothing else
  The recipe creates nothing in AWS: clean_up.py deletes the sample Parquet files and the refunds database.
  README, "Requirements": R70 to R72. Every scenario works on a copy in a temporary folder.

  Background:
    Given a copy of the recipe folder with the files a run writes

  @R70
  Scenario: Clean-up after a run
    When clean_up.py runs
    Then the exit code is 0
    And the printed lines are "deleted data/orders.parquet, deleted data/stores.parquet, deleted output/refunds.duckdb, deleted output/refunds.duckdb.wal"
    And the copy has no Parquet file and no refunds database
    And the captured runs in the copy are as they were

  @R71
  Scenario: A dry run prints the names and deletes nothing
    When clean_up.py runs with "--dry-run"
    Then the exit code is 0
    And 4 printed lines start with "would delete "
    And the copy still has the files a run writes

  @R72
  Scenario: A second clean-up finds nothing left
    When clean_up.py runs
    And clean_up.py runs
    Then the exit code is 0
    And 4 printed lines start with "deleted "
