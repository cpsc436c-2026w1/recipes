Feature: Extension: the chat as a web page (Shiny Express with querychat)
  A user types a question in the page. querychat sends it to the LLM; the LLM's SQL goes through the recipe's
  SQL check and its read-only engine; the table on the page shows the result.
  README, "Requirements": R120 to R129 are in ext-shiny-app/README.md.
  Every scenario runs on this machine: the LLM is a stand-in on 127.0.0.1 that speaks the gateway's format.

  Background:
    Given the shop's tables are loaded in DuckDB

  @browser @R120
  Scenario: A question in the page filters the table
    Given a browser for the page, or this scenario is skipped
    When the app is opened with the stub LLM and a user asks "Show the cake orders"
    Then the box "Orders shown" goes from "1,200" to the count of the check query "SELECT COUNT(*) FROM orders WHERE category = 'cake'"
    And the card's title is "Cake orders"
    And the page shows the SQL "SELECT * FROM orders WHERE category = 'cake'"

  @R120
  Scenario: The filter the page applies is the LLM's query, run by the recipe's engine
    Given the app's chat over a gateway stand-in with the fixed replies of ext-shiny-app/stub_replies.json
    When a user of the app asks "Show the orders of more than 40 dollars"
    Then the dashboard is filtered by "SELECT * FROM orders WHERE amount_cad > 40" with the title "Orders of more than 40 dollars"
    And the filtered table has the count of the check query "SELECT COUNT(*) FROM orders WHERE amount_cad > 40"

  @R121
  Scenario Outline: SQL that is not one SELECT over the team's tables does not reach the table on the page
    Given the app's chat over a gateway stand-in that answers "Do it" with the query "<sql>"
    When a user of the app asks "Do it"
    Then the LLM stand-in received the tool result "<result>"
    And the dashboard is not filtered
    And the table "orders" still has 1200 rows

    Examples:
      | sql                                   | result                                                    |
      | DELETE FROM orders                    | only one SELECT is allowed; this is a DELETE statement    |
      | SELECT 1; DROP TABLE orders           | send exactly one statement; this SQL holds 2              |
      | SELECT * FROM read_csv('/etc/passwd') | the function read_csv is not allowed                      |
      | SELECT * FROM customers               | unknown table customers                                   |

  @browser @R121
  Scenario: A write the stub asks for is refused, and the page says so
    Given a browser for the page, or this scenario is skipped
    When the app is opened with the stub LLM, a user asks "Delete all the orders" and waits for the reply "I could not run a query for that question."
    Then the chat on the page holds "SQL check: only one SELECT is allowed; this is a DELETE statement"
    And the chat on the page holds "I could not run a query for that question."
    And the box "Orders shown" stays at "1,200"

  @R125
  Scenario: A query with more rows than the limit: the LLM gets 50, and the same 50 again with the next question
    Given the app's chat over a gateway stand-in that answers "List every order" with the query "SELECT * FROM orders"
    When a user of the app asks "List every order"
    Then the LLM stand-in received a tool result of 50 rows that says "rows" were cut
    When a user of the app asks "And the weather?"
    Then no request of the app holds a result of more than 50 rows

  @R125
  Scenario: A text value longer than the limit
    Given the app's chat over a gateway stand-in that answers "Show the longest note" with the query "SELECT repeat('x', 500) AS note FROM orders LIMIT 1"
    When a user of the app asks "Show the longest note"
    Then the LLM stand-in received a tool result whose first value has 200 characters and that says "text" was cut

  @R126
  Scenario: An LLM that asks for the tool on every call of a question
    Given the app's chat over a gateway stand-in that asks for the tool on every call of "Count the orders"
    When a user of the app asks "Count the orders"
    Then the gateway stand-in was called 4 times for that question
    And the app's reply is "I could not answer within the step limit."
    When a user of the app asks "Count the orders"
    Then the gateway stand-in was called 4 times for that question

  @R34
  Scenario: A filter that runs longer than the time limit of one query
    Given the engine stops a query after 0.2 seconds
    And the app's chat over a gateway stand-in that filters "Show every pair of pairs" with "SELECT a.* FROM orders a, orders b, orders c, orders d ORDER BY a.amount_cad + b.amount_cad + c.amount_cad + d.amount_cad"
    When a user of the app asks "Show every pair of pairs" and the answer comes within 30 seconds
    Then the LLM stand-in received the tool result "query stopped after 0.2 s"
    And the dashboard is not filtered

  @R127
  Scenario: Where the course token is in what deploy.sh would do
    When "deploy.sh" is run with DRY=1 and the course token "tok-123"
    Then the task definition it would register holds the token only as "secrets[].valueFrom", a reference to the secret
    And "tok-123" is nowhere in what the script printed

  @R128
  Scenario: Who can reach the page that deploy.sh would set up
    When "deploy.sh" is run with DRY=1 and the course token "tok-123"
    Then the security group it would make gets one rule: port 8000, from one address

  @R129
  Scenario: teardown.sh removes what deploy.sh makes
    When "deploy.sh" is run with DRY=1 and the course token "tok-123"
    And "teardown.sh" is run with DRY=1 and the course token "tok-123"
    Then "teardown.sh" has a removing command for every thing "deploy.sh" would make, with the same name

  @R129
  Scenario Outline: A look-up that AWS refuses is a failed step; a look-up that finds nothing is a thing that is gone
    Given a stand-in for the aws command that answers "<answer>"
    When teardown's look-up helper asks whether a thing is there, without DRY
    Then the helper's answer is "<means>" and the step counts as failed: "<failed>"

    Examples:
      | answer                      | means | failed |
      | (the thing)                 | there | no     |
      | NoSuchEntity                | gone  | no     |
      | RepositoryNotFoundException | gone  | no     |
      | ResourceNotFoundException   | gone  | no     |
      | AccessDeniedException       | gone  | yes    |
      | ThrottlingException         | gone  | yes    |

  @R122
  Scenario: What a request of the app to the gateway holds
    Given the app's chat over a gateway stand-in with the fixed replies of ext-shiny-app/stub_replies.json
    When a user of the app asks "How many orders are there?"
    Then every request of the app went to "/v1/chat/completions" with the Authorization header "Bearer tok-123"
    And every request of the app has "max_tokens" 16384, "temperature" 0 and "model" "qwen3.6-35b-a3b"

  @R122
  Scenario: The page gives a model that reasons before it answers the cap of the command line
    Given CHAT_MODEL is "qwen3.8-27b"
    And the app's chat over a gateway stand-in with the fixed replies of ext-shiny-app/stub_replies.json
    When a user of the app asks "How many orders are there?"
    Then every request of the app has "max_tokens" 16384, "temperature" 0 and "model" "qwen3.8-27b"

  @R123
  Scenario: The app without the gateway's two settings
    When the app is started without GATEWAY_URL, COURSE_MCP_TOKEN and APP_MODEL
    Then the app stops with an exit code that is not 0
    And its output holds "COURSE_GATEWAY_URL" and "KB_MCP_TOKEN"

  @R124
  Scenario: The app with the stub needs no token and stays on this machine
    Given no course token and no gateway address in the environment
    When the app's chat is made with APP_MODEL "stub"
    And a user of the app asks "What were total sales by category?"
    Then the LLM stand-in listens on "127.0.0.1"
    And the LLM stand-in received the tool result "sales_cad"
    And the gateway stand-in was called 2 times for that question
