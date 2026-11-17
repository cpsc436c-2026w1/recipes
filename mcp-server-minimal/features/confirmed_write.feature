Feature: A write behind a person's confirmation
  record_refund changes data only with a code that a person made for exactly that change.
  ext-confirmed-write/README.md, "Requirements": R80 to R89, R90, R91.

  Background:
    Given the write server is running with the sample tables and an empty refunds database

  @R80
  Scenario: The tools a client is offered
    When a client lists the tools
    Then the tools are "get_schema, query, record_refund"
    And the tool "record_refund" is not marked read-only
    And the tool "query" is marked read-only

  @R81
  Scenario: A call without a code changes nothing and says what a person must approve
    When the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late"
    Then the tool result is not an error
    And the result has status "needs_confirmation" and changed false
    And the result's change is request "req-0001", order 1042, amount "12.50", reason "arrived two days late"
    And the result names the command "uv run python ext-confirmed-write/approve.py --request-id req-0001 --order-id 1042 --amount 12.50 --reason 'arrived two days late'"
    And the refunds table has 0 rows

  @R82
  Scenario Outline: A code that does not fit this exact change is refused
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana"
    When the model calls record_refund for request "req-0001", order 1042, <amount> CAD, reason "<reason>" with <code>
    Then the tool result is an error that holds "confirmation code does not match this change"
    And the refunds table has 0 rows

    Examples:
      | amount | reason                | code                                   |
      | 12.50  | arrived two days late | the code "ana.9999999999.0123456789abcdef" |
      | 12.50  | arrived two days late | the person's code with a later expiry  |
      | 40.00  | arrived two days late | the person's code                      |
      | 12.51  | arrived two days late | the person's code                      |
      | 12.50  | arrived a week late   | the person's code                      |
      | 12.50  | arrived two days late | the person's code under the name "bob" |
      | 12.50  | arrived two days late | the code "not a code"                  |

  @R83
  Scenario: A call with the person's code writes one row
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana"
    When the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    Then the result has status "applied" and changed true
    And the refunds table has 1 row
    And the row of "req-0001" has order 1042, amount 12.50, approver "ana" and a UTC time

  @R84
  Scenario: The same call again writes no second row
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana"
    When the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    And the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    Then the result has status "already_applied" and changed false
    And the refunds table has 1 row

  @R85
  Scenario: An applied request id used for a different change
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana"
    And the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    And a person approved request "req-0001", order 1042, 30.00 CAD, reason "arrived two days late" as "ana"
    When the model calls record_refund for request "req-0001", order 1042, 30.00 CAD, reason "arrived two days late" with the person's code
    Then the tool result is an error that holds "was already used for a different refund; nothing changed"
    And the refunds table has 1 row
    And the row of "req-0001" has order 1042, amount 12.50, approver "ana" and a UTC time

  @R86
  Scenario Outline: A change that cannot be right is refused before anyone is asked to approve
    When the model calls record_refund for request "<request>", order <order>, <amount> CAD, reason "<reason>"
    Then the tool result is an error that holds "<problem>"
    And the last log line has tool "record_refund" and status "invalid"
    And the refunds table has 0 rows

    Examples:
      | request  | order | amount   | reason | problem                               |
      | req-0001 | 9     | 1.00     | late   | order 9 does not exist                |
      | req-0001 | 1042  | 0.00     | late   | amount must be more than 0            |
      | req-0001 | 1042  | 10000.00 | late   | at most the order's amount, 120.98 CAD |
      | abc      | 1042  | 1.00     | late   | request_id must be 4 to 64 characters |
      | <65 x>   | 1042  | 1.00     | late   | request_id must be 4 to 64 characters |
      | req-0001 | 1042  | 1.00     | <201 x> | reason must be 1 to 200 characters   |

  @R86
  Scenario Outline: The longest request id and reason, and the order's whole amount, are accepted
    When the model calls record_refund for request "<request>", order 1042, <amount> CAD, reason "<reason>"
    Then the result has status "needs_confirmation" and changed false

    Examples:
      | request | amount | reason  |
      | abcd    | 120.98 | late    |
      | <64 x>  | 1.00   | <200 x> |

  @R87
  Scenario: A person who does not answer y gets no code
    Given APPROVAL_SECRET is the server's approval secret
    When approve.py runs for request "req-0001", order 1042, 12.50 CAD, reason "late" as "ana" and the person answers "n"
    Then the exit code is 1
    And the printed output holds "Refund 12.50 CAD on order 1042"
    And the printed output holds "Not approved; no code."
    And no confirmation code is printed

  @R87
  Scenario: A person who answers y gets the code for that change
    Given APPROVAL_SECRET is the server's approval secret
    When approve.py runs for request "req-0001", order 1042, 12.50 CAD, reason "late" as "ana" and the person answers "y"
    Then the exit code is 0
    And the printed code is accepted by record_refund for request "req-0001", order 1042, 12.50 CAD, reason "late"

  @R88
  Scenario Outline: The write server does not start without an approval secret of 16 characters
    Given MCP_SERVER_TOKEN is "sixteen-chars-12"
    And APPROVAL_SECRET is <secret>
    When ext-confirmed-write/write_server.py is started
    Then the exit code is 1
    And the error output holds "APPROVAL_SECRET"
    And the server was not started

    Examples:
      | secret            |
      | not set           |
      | "fifteen-chars-1" |

  @R89
  Scenario: The approval secret is in no tool result and in no log line
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana"
    When ext-confirmed-write/walk.py runs against the server
    Then the exit code is 0
    And 5 lines are printed, with the steps "no code, made-up code, code for a larger amount, with code, same call again"
    And the approval secret is in no log line and in no printed line

  @R90
  Scenario: A code used after its time is refused
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana", 16 minutes ago
    When the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    Then the tool result is an error that holds "confirmation code has expired"
    And the last log line has tool "record_refund" and status "expired"
    And the refunds table has 0 rows

  @R90
  Scenario: A code used one minute before its time is accepted
    Given a person approved request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" as "ana", 14 minutes ago
    When the model calls record_refund for request "req-0001", order 1042, 12.50 CAD, reason "arrived two days late" with the person's code
    Then the result has status "applied" and changed true

  @R91
  Scenario: The code approve.py prints works for 15 minutes
    Given APPROVAL_SECRET is the server's approval secret
    When approve.py runs for request "req-0001", order 1042, 12.50 CAD, reason "late" as "ana" and the person answers "y"
    Then the printed code expires 15 minutes from now
    And the printed output holds "It works for 15 minutes, for this change only."
