Feature: Model routing, the main scenario
  The small model labels each request; app code runs one fixed path per label, sends the large model only what
  needs it, and logs every route that did not work out.
  README, "Requirements": R10 to R19.

  Background:
    Given the stub model with the labels of main/stub_replies.json

  @R10
  Scenario: The router call is one short call to the small model
    Given a gateway stand-in whose router replies "lookup"
    When the app handles "What is the status of order 1042?"
    Then gateway request 1 asks the model "qwen3.6-35b-a3b" with max_tokens 8
    And gateway request 1 has the label definitions as its system prompt
    And gateway request 1 has "What is the status of order 1042?" as its user message

  @R11 @R13
  Scenario: A lookup reads the order in app code
    When the app handles "What is the status of order 1042?"
    Then the label is "lookup" and the path is "lookup"
    And the reply is "Order 1042 is shipped: 2 item(s), 54.00 CAD."
    And the model calls are "router"
    And nothing is in the misroute log

  @R11
  Scenario Outline: A label in other capitals or with punctuation is still that label
    Given a gateway stand-in whose router replies "<reply>"
    When the app handles "Do you carry trekking poles for order 1042?"
    Then the label is "<label>" and the path is "<label>"
    And nothing is in the misroute log

    Examples:
      | reply        | label        |
      | Search.      | search       |
      | OUT-OF-SCOPE | out_of_scope |
      | "hard"       | hard         |
      | lookup!      | lookup       |
      | lookup?      | lookup       |
      | (lookup)     | lookup       |
      | **lookup**   | lookup       |
      | `search`     | search       |

  @R12
  Scenario: A router reply that is not a label
    When the app handles "Can I return boots I wore once on a hike?"
    Then the label is "hard" and the path is "hard"
    And the request made 1 large-model call
    And the misroute log has 1 line
    And the last misroute line has reason "router reply is not a label" and was sent to "hard"
    And the last misroute line has the router's reply "Policy question"

  @R12
  Scenario: A long router reply is cut in the log
    Given a gateway stand-in whose router replies with 200 characters
    When the app handles "Do you carry trekking poles?"
    Then the path is "hard"
    And the router's reply in the last misroute line has 80 characters

  @R13
  Scenario: An order number the shop does not have
    Given the stub router labels "Where is order 1099?" as "lookup"
    When the app handles "Where is order 1099?"
    Then the path is "lookup"
    And the reply is "I could not find order 1099. Please check the number on your receipt."
    And the model calls are "router"

  @R14
  Scenario: A search lists at most three products for the small model
    Given a gateway stand-in whose router replies "search"
    When the app handles "Anything light and waterproof for running or hiking?"
    Then the path is "search"
    And gateway request 2 asks the model "qwen3.6-35b-a3b" with max_tokens 200
    And gateway request 2 lists 3 products in its user message
    And the gateway received 2 requests

  @R15
  Scenario: A hard request goes to the large model with the catalog and the policy
    Given a gateway stand-in whose router replies "hard"
    When the app handles "Compare the rain jacket and the insulated jacket."
    Then the path is "hard"
    And gateway request 2 asks the model "qwen3.8-27b" with max_tokens 600
    And gateway request 2 has the catalog and the policy in its system prompt
    And the gateway received 2 requests

  @R16
  Scenario: An out-of-scope request gets the stock reply
    When the app handles "What will the weather be in Whistler tomorrow?"
    Then the label is "out_of_scope" and the path is "out_of_scope"
    And the reply is the stock reply
    And the model calls are "router"
    And the request made 0 large-model calls

  @R17
  Scenario: A lookup without an order number goes to the hard path
    When the app handles "Where is my order?"
    Then the label is "lookup" and the path is "hard"
    And the request made 1 large-model call
    And the misroute log has 1 line
    And the last misroute line has reason "lookup without an order number" and was sent to "hard"

  @R17
  Scenario: An order number has four digits: three are not one
    Given the stub router labels "Where is order 104?" as "lookup"
    When the app handles "Where is order 104?"
    Then the label is "lookup" and the path is "hard"
    And the last misroute line has reason "lookup without an order number" and was sent to "hard"

  @R17
  Scenario: A search that matches no product goes to the hard path
    Given the stub router labels "Do you sell kayaks?" as "search"
    When the app handles "Do you sell kayaks?"
    Then the label is "search" and the path is "hard"
    And the model calls are "router, hard"
    And the last misroute line has reason "search found no matching product" and was sent to "hard"

  @R17
  Scenario: A search whose reply is empty goes to the hard path, and its call still counts
    Given a gateway stand-in whose router replies "search" and whose small model then replies with nothing
    When the app handles "Do you have waterproof jackets?"
    Then the label is "search" and the path is "hard"
    And the model calls are "router, search, hard"
    And the reply is "A reply from the large model."
    And the last misroute line has reason "search reply was empty" and was sent to "hard"
    And the result's cost is the sum of its calls' costs

  @R18
  Scenario: What a handled request returns
    When the app handles "Do you have waterproof jackets?"
    Then the result has the fields "request_id, request, label, path, reply, error, calls, large_model_calls, cost_cad, ms"
    And the result's error is empty
    And every model call in the result has "part, model, tokens_in, tokens_out, cost_cad"
    And the result's cost is the sum of its calls' costs

  @R19
  Scenario: The last line of a run
    When main/run.py runs on the stub
    Then the exit code is 0
    And 6 printed lines are JSON with "request, label, path, large_model_calls, reply, cost_cad"
    And the last printed line starts with "model: stub; 6 requests; large-model calls 3; total 0.000183 CAD"
    And the last printed line ends with "2 misroutes logged in" and the path of the log
