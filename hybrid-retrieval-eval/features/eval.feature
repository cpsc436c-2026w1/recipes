Feature: The retrieval eval and the answer eval, reported apart
  README, "Requirements": R20 to R25. The scenarios run main/run.py, the command of "Run it".

  @R20 @R24
  Scenario: The stub run prints both tables
    When main/run.py runs with no options
    Then the exit code is 0
    And the retrieval table is
      | method    | recall@5 | MRR  | exact code | other words | plain words | typo |
      | keyword   | 0.81     | 0.77 | 1.00       | 0.60        | 1.00        | 0.67 |
      | embedding | 1.00     | 0.85 | 1.00       | 1.00        | 1.00        | 1.00 |
      | hybrid    | 1.00     | 0.92 | 1.00       | 1.00        | 1.00        | 1.00 |
    And the answer table is
      | correct | grounded | retrieval hit, answer wrong | retrieval missed |
      | 0.88    | 1.00     | 2                           | 0                |

  @R20
  Scenario Outline: Recall@5 counts the top 5 only
    Given a labelled query "coffee" whose relevant product is <product>
    When main/run.py runs with that query
    Then the embedding row has recall@5 <recall> and the hybrid row has recall@5 <recall>

    Examples:
      | product | recall |
      | P26     | 1.00   |
      | P12     | 0.00   |

  @R21
  Scenario Outline: A relevant product below rank 10 counts 0 in MRR
    Given a labelled query "coffee" whose relevant product is <product>
    When main/run.py runs with that query
    Then the embedding row has MRR <mrr> and the hybrid row has MRR <mrr>

    Examples:
      | product | mrr  |
      | P06     | 0.10 |
      | P13     | 0.00 |

  @R22 @R31
  Scenario: The answering model is shown the hybrid top 5, with ids
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in
    When main/run.py runs with "--embedder gateway --answers gateway"
    Then the exit code is 0
    And 16 chat calls were sent, each listing 5 products with their ids in square brackets

  @R23
  Scenario Outline: A reply is correct when it cites a relevant id, grounded when it cites only ids it was shown
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in whose answers say "<reply>"
    When main/run.py runs with "--answers gateway"
    Then the answer table shows correct <correct> and grounded <grounded>

    Examples:
      | reply                          | correct | grounded |
      | Try [P99].                     | 0.00    | 0.00     |
      | None of these fits.            | 0.00    | 0.00     |
      | Try the first one, the [FIRST] | 0.88    | 1.00     |
      | Try [FIRST] or [P99].          | 0.88    | 0.00     |

  @R25
  Scenario: The rows per query go to the output folder
    When main/run.py runs with no options and the output folder in a temporary folder
    Then the file "eval-local-stub-stub.json" there has 48 retrieval rows and 16 answer rows
    And nothing was written to the recipe's own output folder

  @R26
  Scenario: A gateway run prints what it sent and what it cost
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in
    When main/run.py runs with "--embedder gateway --answers gateway"
    Then it printed "gateway: 2 embedding requests, 230 tokens; 16 chat calls, 6400 input and 320 output tokens; 0.000354 CAD at the gateway's prices"

  @R26
  Scenario: An offline run prints no spend line
    When main/run.py runs with no options
    Then no printed line starts with "gateway:"

  @R27
  Scenario Outline: The recall gate
    Given a labelled query "coffee" whose relevant product is <product>
    When main/run.py runs with that query and "--min-recall <min>"
    Then the exit code is <code>
    And the retrieval table was printed and the rows were written

    Examples:
      | product | min  | code |
      | P26     | 1.0  | 0    |
      | P12     | 0.01 | 1    |
      | P12     | 0    | 0    |

  @R27
  Scenario Outline: The gate compares recall as the table prints it
    Given a labelled query "coffee" whose relevant products are P20, P19 and P13
    When main/run.py runs with that query and "--min-recall <min>"
    Then the hybrid row has recall@5 0.67
    And the exit code is <code>

    Examples:
      | min  | code |
      | 0.67 | 0    |
      | 0.68 | 1    |

  @R27
  Scenario Outline: A threshold outside 0 to 1 is refused before the run
    When main/run.py runs with "--min-recall <min>"
    Then the exit code is 2
    And no table was printed

    Examples:
      | min  |
      | 1.5  |
      | -0.1 |

  @R28
  Scenario Outline: Labelled queries that cannot be scored stop the run before any model call
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in
    And the labelled queries <lines>
    When main/run.py runs with "--embedder gateway --answers gateway" and those queries
    Then the exit code is 1
    And the run stopped with "<says>"
    And the gateway received 0 requests

    Examples:
      | lines                                                   | says                                          |
      | q1 "coffee" relevant P99                                | line 1 (q1): relevant id P99 is not in the catalogue |
      | q1 "coffee" relevant none                               | line 1 (q1): no relevant product ids          |
      | q1 "coffee" relevant P20, then q1 "tea" relevant P20    | line 2 (q1): the id is used twice             |
      | q1 "" relevant P20                                      | line 1 (q1): no query                         |

  @R29
  Scenario: The rows file says what produced its numbers
    Given a labelled query "coffee" whose relevant product is P26
    When main/run.py runs with that query
    Then the rows file's run record names the SHA-256 of "products.jsonl" and of "queries.jsonl" as they are on disk
    And the rows file's run record has embedder "stub", answers "stub", k 5, rrf_k 60, pool 20, bm25_k1 1.2 and bm25_b 0.75
