Feature: Outcomes per group, and a paired test of the difference between two cells
  The eval matrix with groups (ext-eval-matrix/README.md, "Requirements": R80 to R88). The offline examples read
  per-run results written by hand, or run the rides case on the stub SQL step and the trips of one morning hour.
  In the run columns, "right" and "wrong" are the value a query returned, "refused" is CANNOT_ANSWER.

  @R80
  Scenario: One case without a group stops the matrix before any model call
    Given a gateway stand-in that answers as the stub does
    And a rides test file with these cases:
      | id  | group    | question                                    |
      | q01 | one-zone | How many trips were requested in zone 161? |
      | q02 |          | How many trips were requested before 08:00? |
    When the rides matrix runs with "--target gateway"
    Then the exit code is 2
    And the error output holds "case q02"
    And the error output holds "has no group"
    And the gateway received 0 requests
    And no run folder was written

  @R81
  Scenario: Each run gets one of three outcomes, each case is counted once, and the outcomes are reported per group
    Given cell "qwen3.6-35b-a3b/seed" has these runs:
      | case | group       | answerable | run 1   | run 2   | run 3   |
      | c01  | one-zone    | yes        | right   | right   | right   |
      | c02  | one-zone    | yes        | right   | wrong   | wrong   |
      | c03  | one-zone    | yes        | refused | refused | right   |
      | c04  | one-zone    | yes        | right   | right   | wrong   |
      | c05  | one-zone    | no         | refused | refused | wrong   |
      | c06  | time-window | yes        | right   | right   | right   |
      | c07  | time-window | yes        | wrong   | wrong   | wrong   |
      | c08  | time-window | no         | wrong   | wrong   | refused |
      | c09  | time-window | yes        | right   | right   | wrong   |
      | c10  | time-window | yes        | right   | right   | right   |
    When the cell's report is written
    Then its group rows are:
      | group       | correct | correct refusal | wrong | of them refused | cases passed |
      | one-zone    | 2       | 1               | 2     | 1               | 3 of 5       |
      | time-window | 3       | 0               | 2     | 0               | 3 of 5       |
      | all         | 5       | 1               | 4     | 1               | 6 of 10      |

  @R81
  Scenario: How citation-set scores answers
    Given the metric is "citation-set"
    And a case has reference_ids "d-01, d-02, d-03" and required_ids "d-01"
    When the metric scores each of these cited sets:
      | cited_ids  | outcome |
      | d-01       | correct |
      | d-01, d-03 | correct |
      | d-02, d-03 | wrong   |
      | d-01, d-07 | wrong   |
      |            | wrong   |
    Then each cited set gets the outcome in its row

  @R81 @R86
  Scenario: The rides matrix on the stub writes a group table per cell and a line per run
    When the rides matrix runs with "--prompt after=ext-eval-matrix/data/after-example.txt"
    Then the exit code is 0
    And the matrix has 5 rows
    And the group row "all" of cell "qwen3.6-35b-a3b/seed" is "8 | 0 | 12 | 0 | 8 of 20"
    And the group row "zone-compare" of cell "qwen3.6-35b-a3b/seed" is "0 | 0 | 5 | 0 | 0 of 5"
    And the group row "zone-compare" of cell "qwen3.8-27b/seed" is "4 | 1 | 0 | 0 | 5 of 5"
    And the group row "all" of cell "no-model/templates" is "8 | 3 | 9 | 8 | 11 of 20"
    And cases.jsonl has 300 lines, 60 for each cell

  @R82
  Scenario: A difference that holds on all cases but not in either group alone
    Given cells "qwen3.6-35b-a3b/seed" and "qwen3.6-35b-a3b/after" have these runs:
      | case | group        | runs passed of 3, first | runs passed of 3, second |
      | c06  | time-window  | 3                       | 3                        |
      | c07  | time-window  | 1                       | 2                        |
      | c08  | time-window  | 3                       | 3                        |
      | c09  | time-window  | 0                       | 0                        |
      | c10  | time-window  | 3                       | 3                        |
      | c11  | zone-compare | 0                       | 3                        |
      | c12  | zone-compare | 1                       | 2                        |
      | c13  | zone-compare | 0                       | 3                        |
      | c14  | zone-compare | 1                       | 3                        |
      | c15  | zone-compare | 0                       | 2                        |
    When the saved runs are compared with "--compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after"
    Then the comparison rows are:
      | group        | first passed | second passed | second only | first only | difference | p      | mark          |
      | time-window  | 3 of 5       | 4 of 5        | 1           | 0          | +0.20      | 1.0000 | too few cases |
      | zone-compare | 0 of 5       | 5 of 5        | 5           | 0          | +1.00      | 0.0625 | too few cases |
      | all          | 3 of 10      | 9 of 10       | 6           | 0          | +0.60      | 0.0312 |               |
    And the all-cases line gives the difference in runs passed "+0.40" with the descriptive interval "+0.27 to +0.53"

  @R82
  Scenario: The per-group interval stays off unless it is asked for
    Given cells "qwen3.6-35b-a3b/seed" and "qwen3.6-35b-a3b/after" have these runs:
      | case | group        | runs passed of 3, first | runs passed of 3, second |
      | c06  | time-window  | 3                       | 3                        |
      | c07  | time-window  | 1                       | 2                        |
      | c11  | zone-compare | 0                       | 3                        |
      | c12  | zone-compare | 1                       | 2                        |
    When the saved runs are compared with "--compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after"
    Then the printed output does not hold "interval (descriptive)"
    When the saved runs are compared with "--compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after --group-intervals"
    Then the printed output holds "interval (descriptive)"

  @R83
  Scenario: A small group of fare questions is marked
    Given cells "qwen3.6-35b-a3b/seed" and "qwen3.8-27b/seed" have these runs:
      | case | group    | runs passed of 3, first | runs passed of 3, second |
      | c16  | fare-sum | 0                       | 3                        |
      | c17  | fare-sum | 3                       | 3                        |
      | c18  | fare-sum | 3                       | 3                        |
    When the saved runs are compared with "--compare qwen3.6-35b-a3b/seed,qwen3.8-27b/seed"
    Then the comparison row "fare-sum" has the difference "+0.33", the p "1.0000" and the mark "too few cases"

  @R84
  Scenario: The same runs compared twice with the same seed give the same table
    Given cells "qwen3.6-35b-a3b/seed" and "qwen3.6-35b-a3b/after" have these runs:
      | case | group    | runs passed of 3, first | runs passed of 3, second |
      | c01  | one-zone | 3                       | 2                        |
      | c02  | one-zone | 1                       | 3                        |
      | c03  | one-zone | 2                       | 0                        |
      | c04  | one-zone | 0                       | 3                        |
      | c05  | one-zone | 3                       | 3                        |
    When the saved runs are compared twice with "--compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after --seed 0"
    Then the comparison row "one-zone" has the difference "+0.20", the p "1.0000" and the mark "too few cases"
    And the two comparison tables are the same, line for line

  @R84
  Scenario: The matrix's own comparison and a comparison of its saved runs give the same table
    When the rides matrix runs with "--models qwen3.6-35b-a3b --prompt after=ext-eval-matrix/data/after-example.txt --compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after"
    And the run's cases.jsonl is compared with "--compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after"
    Then the report holds the printed comparison, line for line

  @R85
  Scenario: The spend cap stops the second cell, and its comparison is left out
    Given every stub call of the rides SQL step costs 0.0001 CAD
    When the rides matrix runs with "--models qwen3.6-35b-a3b,qwen3.8-27b --max-cad 0.009 --compare qwen3.6-35b-a3b/seed,qwen3.8-27b/seed"
    Then the exit code is 1
    And the report holds "comparison left out: cell qwen3.8-27b/seed was cut"
    And the error output holds "comparison left out: cell qwen3.8-27b/seed was cut"
    And the matrix has 1 rows

  @R86
  Scenario: How the metric scores answers of the SQL step
    When the value metric scores each of these answers:
      | reference | the step returned | value     | outcome         |
      | 19074     | a query           | 19074     | correct         |
      | 19074     | a query           | 19075     | wrong           |
      | 1234.56   | a query           | 1234.5649 | correct         |
      | 1234.56   | a query           | 1234.58   | wrong           |
      | none      | CANNOT_ANSWER     |           | correct refusal |
      | 7661      | CANNOT_ANSWER     |           | wrong           |
      | none      | a query           | 4345      | wrong           |
    Then each answer gets the outcome in its row

  @R86
  Scenario: The eval gate of trip-sql passes the stub's tuned cells only
    When the rides matrix runs with "--prompt after=ext-eval-matrix/data/after-example.txt"
    Then the eval row "qwen3.6-35b-a3b/after" is "57 of 60 (need 48) | 19 of 20 (need 16) | none | passes"
    And the eval row "qwen3.8-27b/seed" is "48 of 60 (need 48) | 16 of 20 (need 16) | time-window 2 of 5 | fails"
    And the eval row "no-model/templates" is "33 of 60 (need 48) | 11 of 20 (need 16) | zone-compare 1 of 5 | fails"

  @R86 @eval @live
  Scenario: Eval of trip SQL on the course LLM gateway
    Given the course LLM gateway from the environment
    When the rides matrix runs with "--target gateway --models qwen3.6-35b-a3b --max-cad 0.02"
    Then the eval row "qwen3.6-35b-a3b/seed" passes

  @R87
  Scenario: Templates answer what they match and refuse the rest, without a model call
    Given a gateway stand-in that answers as the stub does
    When the cell "no-model/templates" answers these questions:
      | question                                                           | the step returned | value |
      | How many trips were requested in zone 161 between 08:00 and 09:00? | a query           | 191   |
      | Which zone had the most trips?                                     | CANNOT_ANSWER     |       |
    Then each answer has what the step returned and the value in its row
    When the rides matrix runs with "--target gateway --models no-model"
    Then the exit code is 0
    And the gateway received 0 requests

  @R87
  Scenario: Each model cell is compared with the cell without a model
    When the rides matrix runs with "--models qwen3.6-35b-a3b,qwen3.8-27b,no-model"
    Then the report has the comparisons "no-model/templates against qwen3.6-35b-a3b/seed, no-model/templates against qwen3.8-27b/seed"

  @R88
  Scenario: Six answers of one cell
    Given cell "qwen3.8-27b/seed" has 6 answers that took "1.2, 1.4, 1.5, 2.0, 2.2, 9.0" s and cost 0.000600 CAD together
    When the cell's cost and time are written
    Then its row of the cost and time table is "6 | 0.000600 | 0.000100 | 1.75 | 9.00"

  @R88
  Scenario: The matrix reports cost and time for every cell
    Given every answer of the rides matrix takes 0.25 seconds
    When the rides matrix runs with "--models qwen3.6-35b-a3b,no-model"
    Then the cost and time row of "no-model/templates" is "60 | 0.000000 | 0.000000 | 0.25 | 0.25"
    And the cost and time row of "qwen3.6-35b-a3b/seed" has a CAD per answer above 0
