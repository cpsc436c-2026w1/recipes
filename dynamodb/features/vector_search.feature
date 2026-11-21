Feature: Vector search over the riders' messages
  ext-vector-search/run.sh. The local stand-in does not have SearchVectors or vector indexes, so these scenarios
  read the commands of a dry run and the calls of a run against the fake aws.
  ext-vector-search/README.md, "Requirements": R70 to R76.

  @R70
  Scenario: The vector index is added to the trips table
    When "ext-vector-search/run.sh" runs with DRY=1 and KEEP=1
    Then the exit code is 0
    And 1 printed command holds "aws dynamodb update-table --table-name recipe-trips"
    And 1 printed command holds "AttributeName=fare_quote,AttributeType=N"
    And 1 printed command holds "{"AttributeName":"pickup_zone","SearchSchemaElementType":"INLINE_FILTER"}"
    And 1 printed command holds "{"AttributeName":"fare_quote","SearchSchemaElementType":"INLINE_FILTER"}"
    And 1 printed command holds ""Dimensions":1024"
    And no printed command holds "create-table"

  @R70
  Scenario: A second run keeps the index that is there
    Given the fake aws says every table is "ACTIVE"
    When "ext-vector-search/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And the output holds "(index trip-messages is already there: kept as it is)"
    And no call holds "update-table"

  @R71
  Scenario: An index that does not become ACTIVE
    Given the fake aws says every table is "CREATING"
    And WAIT_MINUTES is "1"
    When "ext-vector-search/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And the error output holds "FAILED: index trip-messages is not ACTIVE after 1 minutes"
    And no call holds "update-item"
    And no call holds "search-vectors --table-name"

  @R72
  Scenario: Without a course token the shipped vectors are used
    When "ext-vector-search/run.sh" runs with DRY=1 and KEEP=1
    Then the output holds "No course token set: use the vectors shipped in ext-vector-search/vectors.json"
    And the output does not hold "POST $COURSE_GATEWAY_URL/embeddings"
    And the output holds "dimensions: 1024"

  @R72
  Scenario: With a course token the vectors are made through the course LLM gateway
    Given a course token and a gateway address are set
    When "ext-vector-search/run.sh" runs with DRY=1 and KEEP=1
    Then the output holds "POST $COURSE_GATEWAY_URL/embeddings  (model qwen3-embedding-0.6b, 52 texts, your KB_MCP_TOKEN)"
    And the output does not hold "test-token"
    And the output does not hold "127.0.0.1"

  @R73
  Scenario: Every vector write asks for the capacity of every index
    When "ext-vector-search/run.sh" runs with DRY=1 and KEEP=1
    Then 50 printed commands hold "aws dynamodb update-item --table-name recipe-trips"
    And every printed command that holds "update-item" holds "--return-consumed-capacity INDEXES"
    And every printed command that holds "update-item" holds "VectorWriteRequestBytes"

  @R74
  Scenario: The filtered search uses BETWEEN and IN
    When "ext-vector-search/run.sh" runs with DRY=1 and KEEP=1
    Then 3 printed commands hold "aws dynamodb search-vectors --table-name recipe-trips --index-name trip-messages"
    And 1 printed command holds "--search-condition-expression fare_quote BETWEEN :lo AND :hi AND pickup_zone IN (:z1, :z2)"
    And every printed command that holds "search-vectors" holds "--return-consumed-capacity TOTAL"

  @R75
  Scenario: An AWS CLI that does not know search-vectors
    Given the fake aws says every table is "ACTIVE"
    And the fake aws refuses "search-vectors --generate-cli-skeleton" with "ParamValidation"
    When "ext-vector-search/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 1
    And the error output holds "does not know search-vectors. Update to AWS CLI v2 2.36.16 or later"
    And no call holds "update-table"
    And no call holds "update-item"

  @R76
  Scenario: The run ends with the bytes and their price
    Given the fake aws says every table is "ACTIVE"
    When "ext-vector-search/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And the call "update-item" comes before the call "search-vectors --table-name"
    And the output holds ""bytes_billed""
    And the output holds ""usd""
