Feature: Operational first: an API over DynamoDB, and an export for the lake
  The function is called through its URL: rep-operational-first/run.py --target aws inside moto deploys it, and the
  URL stand-in of features/support.py runs the deployed package for each signed request.
  rep-operational-first/README.md, "Requirements": R50 to R62.

  @R50
  Scenario: POST /updates stores each update, its minute counter and the key's state
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries
      | event_id | entity_id | event_time               | value |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
      | ev-2     | st-07     | 2026-11-12T15:04:50.000Z | 9     |
      | ev-3     | st-02     | 2026-11-12T15:30:00.000Z | 4     |
    Then the API answers 200 with stored 3, duplicates 0 and older 0
    And the table holds the items "event#ev-1, event#ev-2, event#ev-3, key#st-02, key#st-07, minute#2026-11-12T15:04, minute#2026-11-12T15:30"
    And the counter "minute#2026-11-12T15:04" is 2
    And each event item and its counter were written in one transaction
    And each event item expires 7 days after its event time

  @R51
  Scenario: A copy of an update that is already stored
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries
      | event_id | entity_id | event_time               | value |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
    And a signed POST /updates carries
      | event_id | entity_id | event_time               | value |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
    Then the API answers 200 with stored 0, duplicates 1 and older 0
    And the counter "minute#2026-11-12T15:04" is 1

  @R52
  Scenario: An update that is older than the key's state
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries
      | event_id | entity_id | event_time               | value |
      | ev-2     | st-07     | 2026-11-12T15:10:00.000Z | 9     |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
    Then the API answers 200 with stored 1, duplicates 0 and older 1
    And GET /current for "st-07" answers value 9 from "ev-2"
    And the counter "minute#2026-11-12T15:04" is 1

  @R53
  Scenario Outline: A request that cannot be stored
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries the body <body>
    Then the API answers 400 with an error that holds "<says>"
    And the table holds no item

    Examples:
      | body                                                                                              | says                |
      | not JSON                                                                                          | body must be JSON   |
      | {"updates": []}                                                                                   | send 1 to 100       |
      | 101 updates                                                                                       | send 1 to 100       |
      | {"updates": [{"event_id": "a", "entity_id": "s", "event_time": "2026-11-12T15:04:05.123Z"}]}      | missing value       |
      | {"updates": [{"event_id": "a", "entity_id": "s", "event_time": "yesterday", "value": 1}]}         | event_time must     |
      | {"updates": [{"event_id": "a", "entity_id": "s", "event_time": "2026-11-12T15:04:05.123Z", "value": "many"}]} | value must be a number |
      | {"updates": [{"event_id": 7, "entity_id": "s", "event_time": "2026-11-12T15:04:05.123Z", "value": 1}]}        | event_id must be a string of 1 to 64 characters |
      | {"updates": [{"event_id": "a", "entity_id": "", "event_time": "2026-11-12T15:04:05.123Z", "value": 1}]}       | entity_id must be a string of 1 to 64 characters |
      | an entity_id of 65 characters                                                                             | entity_id must be a string of 1 to 64 characters |

  @R53
  Scenario: One bad update in a request stores none of them
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries one good update and then one without an event_id
    Then the API answers 400 with an error that holds "update 1: missing event_id"
    And the table holds no item

  @R54
  Scenario: GET /number adds up the 60 minute counters before the current minute
    Given the API of PREFIX "recipe-ana" is deployed
    And updates were stored at "2026-11-12T14:59:59.999Z, 2026-11-12T15:00:00.000Z, 2026-11-12T15:59:59.999Z, 2026-11-12T16:00:00.000Z"
    When a signed GET /number asks as of "2026-11-12T16:00:30.000Z"
    Then the API answers 200 with updates_last_hour 2, window_start "2026-11-12T15:00:00.000Z" and window_end "2026-11-12T16:00:00.000Z"
    And the function read 60 counters in one BatchGetItem call, eventually consistent
    And the function made no Scan and no Query

  @R55
  Scenario Outline: GET /number with a time not in the form of the example
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed GET /number asks as of "<as_of>"
    Then the API answers 400 with an error that holds "as_of must look like"

    Examples:
      | as_of                    |
      | yesterday                |
      | 2026-11-12T16:00:00.000  |
      | 2026-11-12T16:00:00+00:00 |
      | 0001-01-01T00:30:00.000Z |

  @R56
  Scenario: GET /current for a key that has no update
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed GET /current asks for "st-99"
    Then the API answers 404 with an error that holds "no such key"

  @R58
  Scenario: DynamoDB keeps leaving counters unread
    Given the API of PREFIX "recipe-ana" is deployed
    And updates were stored at "2026-11-12T15:00:00.000Z, 2026-11-12T15:30:00.000Z"
    And DynamoDB leaves the counters of every read unread
    When a signed GET /number asks as of "2026-11-12T16:00:30.000Z"
    Then the API answers 503 with an error that holds "the table is busy"
    And the function asked for the counters 5 times

  @R58
  Scenario: DynamoDB leaves counters unread once
    Given the API of PREFIX "recipe-ana" is deployed
    And updates were stored at "2026-11-12T15:00:00.000Z, 2026-11-12T15:30:00.000Z"
    And DynamoDB leaves the counters of the first read unread
    When a signed GET /number asks as of "2026-11-12T16:00:30.000Z"
    Then the API answers 200 with updates_last_hour 2, window_start "2026-11-12T15:00:00.000Z" and window_end "2026-11-12T16:00:00.000Z"
    And the function asked for the counters 2 times

  @R57
  Scenario: One log line per request, without ids
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed POST /updates carries
      | event_id | entity_id | event_time               | value |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
      | ev-1     | st-07     | 2026-11-12T15:04:05.123Z | 11    |
    Then the function wrote one log line: {"request_id": "request-1", "method": "POST", "route": "/updates", "status": 200, "stored": 1, "duplicates": 1, "older": 0}
    And the log line holds none of "ev-1, st-07"

  @R57
  Scenario Outline: The log line of a request that is refused or not found
    Given the API of PREFIX "recipe-ana" is deployed
    When a signed GET <path> asks with "<query>"
    Then the function wrote one log line: <line>
    And the log line holds none of "<typed>"

    Examples:
      | path             | query           | typed      | line                                                                             |
      | /number          | as_of=yesterday | yesterday  | {"request_id": "request-1", "method": "GET", "route": "/number", "status": 400}  |
      | /current         | entity_id=st-99 | st-99      | {"request_id": "request-1", "method": "GET", "route": "/current", "status": 404} |
      | /drivers/d-0007  | x=1             | d-0007     | {"request_id": "request-1", "method": "GET", "route": "other", "status": 404}    |

  @R60
  Scenario: The table, the function and its URL
    When rep-operational-first/run.py runs with "--target aws --skip-export --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And the table "recipe-ana-updates" is on demand, with point-in-time recovery on and time to live on "expires_at"
    And the function "recipe-ana-api" was created from "handler.py" alone, with "python3.12" on "arm64", 256 MB and a timeout of 10 seconds
    And its URL takes signed requests only: "AWS_IAM"
    And every request of the run to the URL was signed, in batches of at most 100 updates
    And the log group "/aws/lambda/recipe-ana-api" keeps its lines 7 days
    And the output holds "updates in the last hour: API 143"

  @R60
  Scenario: A request that is not signed
    Given the API of PREFIX "recipe-ana" is deployed
    When an unsigned GET /number is sent
    Then the URL answers 403 and the function does not run

  @R61
  Scenario: What the function's role may do
    When rep-operational-first/run.py runs with "--target aws --skip-export"
    Then the role "recipe-ana-api-role" may get, put, update, batch-get and condition-check items of the table "recipe-ana-updates" and write the log group "/aws/lambda/recipe-ana-api"
    And no statement of its policy has "*" as a resource or in an action
    And the role can be assumed by "lambda.amazonaws.com" only

  @R61
  Scenario: A role that is passed in
    Given API_ROLE_ARN names the role "team-api" of the account
    When rep-operational-first/run.py runs with "--target aws --skip-export"
    Then the exit code is 0
    And no role was created
    And the function "recipe-ana-api" runs as "arn:aws:iam::123456789012:role/team-api"

  @R62
  Scenario: The export and the lake's number
    Given Athena answers the count with 143, after scanning 9366 bytes
    When rep-operational-first/run.py runs with "--target aws --as-of 2026-11-12T16:00:30Z"
    Then the exit code is 0
    And a full export of the table "recipe-ana-updates" went to the bucket "recipe-ana-lake-123456789012" under "exports"
    And the Athena table "updates_export" was made anew over the data folder of that export
    And the count Athena was asked for is of items of kind "event" with event times from "2026-11-12T15:00:00.000Z" up to "2026-11-12T16:00:00.000Z"
    And the output holds "updates in the last hour: API 143, lake 143"

  @R62
  Scenario: Point-in-time recovery is not ready yet
    Given Athena answers the count with 143, after scanning 9366 bytes
    And DynamoDB answers "PointInTimeRecoveryUnavailableException" to the first 2 export requests
    When rep-operational-first/run.py runs with "--target aws"
    Then the exit code is 0
    And the recipe asked for the export 3 times, 30 seconds apart
