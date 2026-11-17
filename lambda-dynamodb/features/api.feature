Feature: The API: what main/handler.py answers
  The handler is loaded inside moto with the three tables of the dynamodb recipe, and called the way a function
  URL calls it: the request is the JSON body. README, "Requirements": R10 to R18, R70, R74, R75.

  Background:
    Given the handler with the tables of PREFIX "recipe-ana"
    And the trips "t-0001, t-0002, t-0003" are waiting, each with a fare
    And the drivers "d-1, d-2" are available
    And rider "r-03" has trips requested at "2026-10-01T08:00:00Z, 2026-10-02T08:00:00Z, 2026-10-03T08:00:00Z"

  @R10 @R18
  Scenario: One trip by its id
    When a caller sends action "get" with trip_id "t-0002"
    Then the status is 200
    And the reply's item has trip_id "t-0002"
    And the reply's item has the fare as a JSON number
    And the reply names the read units of the call
    And the handler read the table "recipe-ana-trips" with ConsistentRead false

  @R11
  Scenario: The same trip with a strong read
    When a caller sends action "get" with trip_id "t-0002" and strong true
    Then the status is 200
    And the handler read the table "recipe-ana-trips" with ConsistentRead true

  @R12
  Scenario: A rider's trips, all of them and from a time on
    When a caller sends action "rider" with rider_id "r-03"
    Then the reply has 3 items in time order
    When a caller sends action "rider" with rider_id "r-03" from "2026-10-02" on
    Then the reply has 2 items in time order
    And every item of the reply was requested at or after "2026-10-02"
    And the handler made 1 call to DynamoDB, a Query

  @R13
  Scenario: A driver claims a trip that has no driver
    When driver "d-1" sends action "claim" for trip "t-0001"
    Then the status is 200
    And the reply is {"claimed": true}
    And trip "t-0001" has driver "d-1" and status "accepted"

  @R14
  Scenario: A second driver claims the same trip
    Given driver "d-1" sent action "claim" for trip "t-0001"
    When driver "d-2" sends action "claim" for trip "t-0001"
    Then the status is 200
    And the reply is {"claimed": false, "held_by": "d-1"}
    And trip "t-0001" has driver "d-1" and status "accepted"

  @R15
  Scenario: A driver accepts a trip: the trip and the driver change together
    When driver "d-2" sends action "accept" for trip "t-0002"
    Then the reply is {"accepted": true}
    And trip "t-0002" has driver "d-2" and status "accepted"
    And driver "d-2" has trip "t-0002" and status "busy"
    And the handler made 1 call to DynamoDB, a TransactWriteItems

  @R16
  Scenario: A busy driver accepts a second trip
    Given driver "d-2" sent action "accept" for trip "t-0002"
    When driver "d-2" sends action "accept" for trip "t-0003"
    Then the status is 200
    And the reply is {"accepted": false, "reasons": {"trip": "None", "driver": "ConditionalCheckFailed"}}
    And trip "t-0003" has no driver and status "requested"
    And driver "d-2" has trip "t-0002" and status "busy"

  @R16
  Scenario: A free driver accepts a trip that has a driver
    Given driver "d-2" sent action "accept" for trip "t-0002"
    When driver "d-1" sends action "accept" for trip "t-0002"
    Then the reply is {"accepted": false, "reasons": {"trip": "ConditionalCheckFailed", "driver": "None"}}
    And trip "t-0002" has driver "d-2" and status "accepted"
    And driver "d-1" has no trip and status "available"

  @R17
  Scenario Outline: An action the API does not have
    When a caller sends the body <body>
    Then the status is 400
    And the reply's error holds "unknown action"
    And the handler made 0 calls to DynamoDB

    Examples:
      | body                                            |
      | {"action": "delete-everything"}                 |
      | {"action": "scan"}                              |
      | {"trip_id": "t-0001"}                           |
      | {"action": "GET", "trip_id": "t-0001"}          |
      | {"action": ["get"], "trip_id": "t-0001"}        |

  @R74
  Scenario Outline: A request that lacks a field its action needs, or has it in the wrong form
    When a caller sends the body <body>
    Then the status is 400
    And the reply's error holds "<field>"
    And the handler made 0 calls to DynamoDB

    Examples:
      | body                                                         | field                                            |
      | {"action": "get"}                                            | trip_id must be a string of 1 to 64 characters   |
      | {"action": "get", "trip_id": ""}                             | trip_id must be a string of 1 to 64 characters   |
      | {"action": "get", "trip_id": 42}                             | trip_id must be a string of 1 to 64 characters   |
      | {"action": "rider"}                                          | rider_id must be a string of 1 to 64 characters  |
      | {"action": "claim", "trip_id": "t-0001"}                     | driver_id must be a string of 1 to 64 characters |
      | {"action": "accept", "driver_id": "d-1"}                     | trip_id must be a string of 1 to 64 characters   |
      | {"action": "claim", "trip_id": "t-0001", "driver_id": {"S": "d-1"}} | driver_id must be a string of 1 to 64 characters |

  @R75
  Scenario Outline: A since that is not a short string
    When a caller sends the body <body>
    Then the status is 400
    And the reply's error holds "since must be a string of at most 64 characters"
    And the handler made 0 calls to DynamoDB

    Examples:
      | body                                                                    |
      | {"action": "rider", "rider_id": "r-03", "since": 20261002}              |
      | {"action": "rider", "rider_id": "r-03", "since": ["2026-10-02"]}        |
      | {"action": "rider", "rider_id": "r-03", "since": "2026-10-02T08:00:00Z-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"} |

  @R74
  Scenario: An id of 64 characters passes and one of 65 does not
    When a caller sends action "get" with a trip_id of 64 characters
    Then the status is 200
    When a caller sends action "get" with a trip_id of 65 characters
    Then the status is 400
    And the handler made 0 calls to DynamoDB

  @R70
  Scenario Outline: A body that is not a JSON object
    When a function URL passes on the raw body <raw>
    Then the status is 400
    And the reply's error holds "<error>"
    And the handler made 0 calls to DynamoDB

    Examples:
      | raw             | error                         |
      | not json at all | the body is not JSON          |
      | {"action": "get | the body is not JSON          |
      | ["get"]         | the body is not a JSON object |
      | "get"           | the body is not a JSON object |
