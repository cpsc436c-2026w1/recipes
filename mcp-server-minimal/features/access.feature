Feature: Who can reach the tools, and how the client says what went wrong
  One bearer token in front of the endpoint; the server listens on this machine only.
  README, "Requirements": R30 to R36, R50 to R53, R60.

  @R30
  Scenario Outline: A request without the server's token gets 401 and reaches no tool
    Given the main server is running with the sample tables
    When a request for the tool list is sent with <header>
    Then the HTTP status is 401
    And the log has 0 lines

    Examples:
      | header                                   |
      | no Authorization header                  |
      | the header "Bearer a-wrong-token-of-enough-length" |
      | the server's token under the scheme "Basic" |

  @R30
  Scenario Outline: The token is asked for whatever the method
    Given the main server is running with the sample tables
    When a <method> request is sent to the endpoint without a token
    Then the HTTP status is 401
    And the log has 0 lines

    Examples:
      | method |
      | GET    |
      | DELETE |
      | PUT    |

  @R30
  Scenario: A request with the server's token is let through
    Given the main server is running with the sample tables
    When a request for the tool list is sent with the server's token
    Then the HTTP status is 200

  @R34
  Scenario Outline: A request that names another site in its Origin or Host header is refused
    Given the main server is running with the sample tables
    When a request for the tool list is sent with the server's token and the header "<header>"
    Then the HTTP status is <status>
    And the log has 0 lines

    Examples:
      | header                      | status |
      | Origin: http://evil.example | 403    |
      | Host: evil.example          | 421    |

  @R34
  Scenario: A request that names this server in its Origin header is let through
    Given the main server is running with the sample tables
    When a request for the tool list is sent with the server's token and its own address as the Origin header
    Then the HTTP status is 200

  @R31
  Scenario Outline: The server does not start without a token of 16 characters
    Given MCP_SERVER_TOKEN is <token>
    When main/server.py is started
    Then the exit code is 1
    And the error output holds "MCP_SERVER_TOKEN"
    And the server was not started

    Examples:
      | token             |
      | not set           |
      | "fifteen-chars-1" |

  @R31
  Scenario: A token of 16 characters is enough
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    When main/server.py is started
    Then the exit code is 0
    And the server was started

  @R32
  Scenario: The token is in no log line and in no printed line
    Given the main server is running with the sample tables
    When main/client.py runs with the server's token
    And main/client.py runs with the token "a-wrong-token-of-enough-length"
    Then the log has 3 lines
    And the server's token is in no log line and in no printed line
    And the token "a-wrong-token-of-enough-length" is in no log line and in no printed line

  @R32
  Scenario: A tool that breaks does not give the token away
    Given the main server is running with the sample tables
    And the engine breaks on every schema read
    When a client calls get_schema
    Then the tool result is an error that holds "Error executing tool get_schema"
    And the tool result does not hold "the engine broke"
    And the server's token is not in the tool result and in no log line

  @R33
  Scenario: Where the server listens by default
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    When main/server.py is started
    Then the server was started on host "127.0.0.1" and port 8765
    And the printed output holds "MCP server on http://127.0.0.1:8765/mcp"

  @R32
  Scenario: A server that answers 500 does not get the token into the client's error
    Given a stand-in at the client's address that answers every request with HTTP 500
    When main/client.py runs with the server's token
    Then the exit code is 1
    And the error output starts with "MCP call failed:"
    And the server's token is not in what the client printed

  @R33
  Scenario: MCP_HOST and MCP_PORT name another address
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    And MCP_HOST is "0.0.0.0" and MCP_PORT is "9000"
    And MCP_ALLOWED_ORIGINS is "https://team.example.org"
    When main/server.py is started
    Then the server was started on host "0.0.0.0" and port 9000

  @R35
  Scenario: A server beyond this machine does not start without the sites it answers
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    And MCP_HOST is "0.0.0.0" and MCP_PORT is "9000"
    When main/server.py is started
    Then the exit code is 1
    And the error output holds "MCP_ALLOWED_ORIGINS"
    And the server was not started

  @R35
  Scenario Outline: A site written without http:// or https:// stops the start
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    And MCP_HOST is "0.0.0.0" and MCP_PORT is "9000"
    And MCP_ALLOWED_ORIGINS is "<origins>"
    When main/server.py is started
    Then the exit code is 1
    And the error output holds "MCP_ALLOWED_ORIGINS: '<bad>' is not a site: write it with http:// or https://"
    And the server was not started

    Examples:
      | origins                                    | bad                  |
      | team.example.org                           | team.example.org     |
      | https://team.example.org, team.example.org | team.example.org     |
      | ftp://team.example.org                     | ftp://team.example.org |

  @R36
  Scenario Outline: A server beyond this machine answers only the sites it was given
    Given the main server is running for the site "https://team.example.org" with MCP_HOST "0.0.0.0"
    When a tool-list request carries the server's token and the headers "<headers>"
    Then the HTTP status is <status>

    Examples:
      | headers                                                      | status |
      | Host: team.example.org                                       | 200    |
      | Host: team.example.org, Origin: https://team.example.org     | 200    |
      | Host: team.example.org:8443                                  | 200    |
      | Host: team.example.org, Origin: https://evil.example         | 403    |
      | Host: evil.example                                           | 421    |
      | Host: evil.example, Origin: https://team.example.org         | 421    |

  @R50
  Scenario: The client lists the tools and calls them
    Given the main server is running with the sample tables
    When main/client.py runs with the server's token
    Then the exit code is 0
    And 4 lines are printed, with the steps "list_tools, get_schema, query, query"
    And printed line 1 lists the tools "get_schema, query", both read-only
    And printed line 3 has the rows of 5 cities, "Vancouver" first with 66 orders
    And printed line 4 is the refusal of "DELETE FROM orders"

  @R50
  Scenario: The client sends the queries it is given
    Given the main server is running with the sample tables
    When main/client.py runs with the server's token and "--sql 'SELECT count(*) FROM stores'"
    Then 3 lines are printed, with the steps "list_tools, get_schema, query"
    And the printed output holds "[[6]]"

  @R51
  Scenario: The client with a wrong token
    Given the main server is running with the sample tables
    When main/client.py runs with the token "a-wrong-token-of-enough-length"
    Then the exit code is 1
    And the error output is the one line "401 Unauthorized from" the server's address ": MCP_SERVER_TOKEN here is not the server's token"

  @R52
  Scenario: The client when no server is running
    Given no server is running at the client's address
    When main/client.py runs with the token "a-token-of-enough-length-0000"
    Then the exit code is 1
    And the error output starts with "cannot reach http://127.0.0.1:"

  @R53
  Scenario: The client without a token
    Given the main server is running with the sample tables
    When main/client.py runs without a token
    Then the exit code is 1
    And the error output holds "MCP_SERVER_TOKEN"
    And the log has 0 lines

  @R60
  Scenario: A run opens no connection beyond this machine
    Given every connection to an address other than 127.0.0.1 fails the scenario
    And the main server is running with the sample tables
    When main/client.py runs with the server's token
    Then the exit code is 0
    And at least 1 connection to 127.0.0.1 was opened
