Feature: While the function runs: what it logs
  The handler inside moto, called the way Lambda calls it, with a context that carries a request id. What the
  handler prints is what Lambda sends to the function's log group. README, "Requirements": R71 to R73, R76.

  Background:
    Given the handler with the tables of PREFIX "recipe-ana"
    And the trips "t-0001, t-0002, t-0003" are waiting, each with a fare
    And the drivers "d-1, d-2" are available
    And rider "r-03" has trips requested at "2026-10-01T08:00:00Z, 2026-10-02T08:00:00Z, 2026-10-03T08:00:00Z"

  @R71
  Scenario Outline: One JSON log line per request
    When Lambda calls the handler with request id "req-7" and the body <body>
    Then the handler printed 1 line, a JSON object with exactly the fields "request_id, action, status, outcome"
    And the log line has request_id "req-7", action <action>, status <status> and outcome "<outcome>"

    Examples:
      | body                                                            | action   | status | outcome     |
      | {"action": "get", "trip_id": "t-0001"}                          | "get"    | 200    | found       |
      | {"action": "get", "trip_id": "t-9999"}                          | "get"    | 200    | not_found   |
      | {"action": "rider", "rider_id": "r-03"}                         | "rider"  | 200    | listed      |
      | {"action": "claim", "trip_id": "t-0001", "driver_id": "d-1"}    | "claim"  | 200    | claimed     |
      | {"action": "accept", "trip_id": "t-0002", "driver_id": "d-2"}   | "accept" | 200    | accepted    |
      | {"action": "delete-everything"}                                 | null     | 400    | bad_request |
      | {"action": "get"}                                               | null     | 400    | bad_request |

  @R71
  Scenario: A refused claim and a cancelled accept are named in the log
    Given driver "d-1" sent action "claim" for trip "t-0001"
    And driver "d-2" sent action "accept" for trip "t-0002"
    When Lambda calls the handler with request id "req-8" and the body {"action": "claim", "trip_id": "t-0001", "driver_id": "d-2"}
    Then the log line has request_id "req-8", action "claim", status 200 and outcome "refused"
    When Lambda calls the handler with request id "req-9" and the body {"action": "accept", "trip_id": "t-0003", "driver_id": "d-2"}
    Then the log line has request_id "req-9", action "accept", status 200 and outcome "cancelled"

  @R76
  Scenario Outline: DynamoDB refuses a call: the log line is still written, with the error's code
    Given DynamoDB refuses every "<operation>" call with "<code>"
    When the function fails when Lambda calls the handler with request id "req-5" and the body <body>
    Then the handler printed 1 line, a JSON object with exactly the fields "request_id, action, status, outcome, error"
    And the log line has request_id "req-5", action <action>, status null and outcome "error"
    And the log line's error is "<code>"
    And the handler's output holds none of "<values>"

    Examples:
      | operation          | code                                   | body                                                          | action   | values      |
      | GetItem            | ProvisionedThroughputExceededException | {"action": "get", "trip_id": "t-0001"}                        | "get"    | t-0001      |
      | UpdateItem         | ThrottlingException                    | {"action": "claim", "trip_id": "t-0001", "driver_id": "d-1"}  | "claim"  | t-0001, d-1 |
      | TransactWriteItems | AccessDeniedException                  | {"action": "accept", "trip_id": "t-0002", "driver_id": "d-2"} | "accept" | t-0002, d-2 |

  @R72
  Scenario Outline: Nothing a caller sent is in the log, except a known action
    When Lambda calls the handler with request id "req-7" and the body <body>
    Then the handler's output holds none of "<values>"

    Examples:
      | body                                                                          | values                         |
      | {"action": "get", "trip_id": "t-0001"}                                        | t-0001                         |
      | {"action": "rider", "rider_id": "r-03", "since": "2026-10-02"}                | r-03, 2026-10-02, h-1          |
      | {"action": "claim", "trip_id": "t-0001", "driver_id": "d-1"}                  | t-0001, d-1                    |
      | {"action": "accept", "trip_id": "t-0002", "driver_id": "d-2"}                 | t-0002, d-2                    |
      | {"action": "drop table trips; --", "trip_id": "t-0001", "note": "secret-123"} | drop table, t-0001, secret-123 |

  @R73
  Scenario Outline: The log group is there before the function, with a retention of 7 days
    When "<script>" runs against the fake aws
    Then the exit code is 0
    And the call "logs create-log-group --log-group-name /aws/lambda/recipe-api" comes before the call "logs put-retention-policy --log-group-name /aws/lambda/recipe-api --retention-in-days 7"
    And the call "logs put-retention-policy" comes before the call "lambda create-function "

    Examples:
      | script              |
      | main/run.sh         |
      | ext-redeploy/run.sh |

  @R73
  Scenario: LOG_DAYS names another number
    Given LOG_DAYS is "30"
    When "main/run.sh" runs against the fake aws
    Then 1 call holds "logs put-retention-policy --log-group-name /aws/lambda/recipe-api --retention-in-days 30"

  @R73
  Scenario: A second run keeps the log group and sets the retention again
    Given the fake aws says the API of an earlier run is there
    When "main/run.sh" runs against the fake aws with KEEP=1
    Then the exit code is 0
    And 1 call holds "logs put-retention-policy --log-group-name /aws/lambda/recipe-api --retention-in-days 7"
