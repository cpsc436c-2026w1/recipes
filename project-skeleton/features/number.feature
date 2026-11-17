Feature: One number, the same in every representative
  The scripts a team runs, started as the command line starts them, offline: moto, DuckDB and the stub model.
  README, "Requirements": R10 to R15, R20 to R24.

  @R10
  Scenario Outline: The last hour is the 60 whole minutes before the current minute
    Given a file of updates with the event times
      | event_id | event_time               |
      | a        | 2026-11-12T14:59:59.999Z |
      | b        | 2026-11-12T15:00:00.000Z |
      | c        | 2026-11-12T15:59:59.999Z |
      | d        | 2026-11-12T16:00:00.000Z |
      | e        | 2026-11-12T16:00:20.000Z |
    When <script> runs with that file and "--as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the output holds "<line>"

    Examples:
      | script                          | line                                      |
      | rep-lakehouse-first/run.py      | updates in the last hour: 2               |
      | rep-operational-first/run.py    | updates in the last hour: API 2, lake 2   |
      | rep-genai-first/run.py          | tool result 2                             |
      | rep-genai-first/lake_example.py | updates in the last hour: lake 2, table 2 |

  @R10
  Scenario: An update that is sent twice is counted once
    Given a file of updates with the event times
      | event_id | event_time               |
      | a        | 2026-11-12T15:10:00.000Z |
      | b        | 2026-11-12T15:20:00.000Z |
      | a        | 2026-11-12T15:10:00.000Z |
    When rep-lakehouse-first/run.py runs with that file and "--as-of 2026-11-12T16:00:30Z"
    And rep-operational-first/run.py runs with that file and "--as-of 2026-11-12T16:00:30Z"
    Then the output holds "updates in the last hour: 2 "
    And the output holds "stored 2, older 0, duplicates 1"
    And the output holds "updates in the last hour: API 2, lake 2"

  @R11
  Scenario: The three representatives agree on the synthetic updates
    When rep-lakehouse-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    And rep-operational-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    And rep-genai-first/run.py runs with "--as-of 2026-11-12T16:00:30Z"
    And rep-genai-first/lake_example.py runs with "--as-of 2026-11-12T16:00:30Z"
    Then the output holds "1. 305 updates to send (keys: 20)"
    And the output holds "stored 300, older 0, duplicates 5"
    And the four runs print the same number of updates in the last hour, the one plain Python counts

  @R12
  Scenario Outline: The number on a page for users
    When <script> runs with "--as-of 2026-11-12T16:00:30Z"
    Then the page ".local/<page>" shows the number the run printed

    Examples:
      | script                       | page                   |
      | rep-lakehouse-first/run.py   | lakehouse-first.html   |
      | rep-operational-first/run.py | operational-first.html |
      | rep-genai-first/run.py       | genai-first.html       |

  @R13
  Scenario: Updates from a stream-sim file
    Given a stream-sim file of 3 updates in the window and 1 before it, with the payload field "bikes"
    When rep-lakehouse-first/run.py runs with that stream-sim file and "--value-field bikes --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the output holds "1. 4 updates to send (keys: 2)"
    And the output holds "updates in the last hour: 3"

  @R14
  Scenario: A file whose update lacks a field
    Given a file of updates where one has no entity_id
    When rep-operational-first/run.py runs with that file and "--as-of 2026-11-12T16:00:30Z"
    Then the run stops with the error "lacks ['entity_id']"
    And no table was created

  @R15
  Scenario Outline: The offline runs need no account and no token
    Given no AWS credentials, no Region and no gateway settings in the environment
    When <script> runs with "--as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And no gateway client was made

    Examples:
      | script                          |
      | rep-lakehouse-first/run.py      |
      | rep-operational-first/run.py    |
      | rep-genai-first/run.py          |
      | rep-genai-first/lake_example.py |

  @R20
  Scenario Outline: Every name starts with PREFIX
    Given the setting PREFIX is "<prefix>"
    And Firehose has written 2 Parquet files
    And Athena answers the count with 143, after scanning 6967 bytes
    When rep-lakehouse-first/run.py runs with "--target aws"
    And rep-operational-first/run.py runs with "--target aws --skip-export"
    Then the exit code is 0
    And every stream, Firehose stream, table, function, log group, bucket, workgroup and role of the account starts with "<start>"
    And the bucket is "<start>lake-123456789012" and the Glue database "<database>"

    Examples:
      | prefix    | start      | database       |
      | recipe-bo | recipe-bo- | recipe_bo_lake |
      | not set   | recipe-    | recipe_lake    |

  @R21
  Scenario Outline: A PREFIX that cannot be a name
    Given the setting PREFIX is "<prefix>"
    When rep-operational-first/run.py runs with "--target aws --skip-export"
    Then the exit code is 1
    And the output holds "use letters, digits and hyphens, at most 30 characters"
    And the account has nothing of the recipe

    Examples:
      | prefix                           |
      | recipe_ana                       |
      | recipe-ana-and-a-very-long-name1 |

  @R22
  Scenario: The Region of every call
    Given the setting AWS_REGION is "us-west-2"
    And the setting AWS_DEFAULT_REGION is "us-west-2"
    And Firehose has written 2 Parquet files
    And Athena answers the count with 143, after scanning 6967 bytes
    When rep-lakehouse-first/run.py runs with "--target aws"
    And rep-operational-first/run.py runs with "--target aws"
    And rep-genai-first/run.py runs with "--target aws --llm stub"
    And rep-genai-first/lake_example.py runs with "--target aws"
    Then the exit code is 0
    And every call went to the Region "ca-central-1"

  @R24
  Scenario Outline: Athena's result files do not pile up
    Given Firehose has written 2 Parquet files
    And Athena answers the count with 143, after scanning 6967 bytes
    When <script> runs with "--target aws"
    Then the bucket "recipe-ana-lake-123456789012" has S3 delete objects under "athena-results/" after 7 days
    And the bucket has no other lifecycle rule

    Examples:
      | script                       |
      | rep-lakehouse-first/run.py   |
      | rep-operational-first/run.py |

  @R23
  Scenario: The bucket name belongs to another account
    Given S3 answers "BucketAlreadyExists" when the bucket is created
    When rep-lakehouse-first/run.py runs with "--target aws"
    Then the run stops with the error "BucketAlreadyExists"
    And no stream and no Firehose stream was created
