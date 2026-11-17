Feature: Clean-up removes the run folders, and nothing else
  The recipe creates nothing in AWS: clean_up.py removes runs/ and leaves the captured runs in output/.
  README, "Requirements": R50 and R51. Every scenario works on a copy in a temporary folder.

  @R50
  Scenario: Clean-up after two runs
    Given a copy of the recipe folder with two run folders under runs/
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "removed"
    And the copy has no runs folder
    And the captured runs in the copy's output folder are as they were

  @R51
  Scenario: Clean-up with nothing to remove
    Given a copy of the recipe folder without a runs folder
    When clean_up.py runs
    Then the exit code is 0
    And the printed output holds "nothing to remove"
    And the captured runs in the copy's output folder are as they were
