Feature: Extension: one trace item per model call or tool call
  Tracer and calls_of in ext-traces/trace_store.py, the file a team copies, and trace_run.py.
  ext-traces/README.md, "Requirements": R60 to R66. Against moto, with a clock the scenario moves.

  Background:
    Given the traces table exists

  @R60 @R63
  Scenario: The calls of one request come back in the order they started
    Given "user-ana" and "user-ben" send requests at the same time
    When user-ana's request makes the calls "model plan, tool search, model reply"
    And user-ben's request makes the calls "model plan"
    And the calls of user-ana's request are read back
    Then they are "1 model plan ok, 2 tool search ok, 3 model reply ok"

  @R61
  Scenario: A call that raises is traced with its error, and the error reaches the caller
    Given "user-ana" sends a request
    When user-ana's request makes a tool call "lookup_store" that raises TimeoutError "store did not answer in 2 s"
    And the calls of user-ana's request are read back
    Then the caller got the TimeoutError
    And they are "1 tool lookup_store error"
    And the error of call 1 is "TimeoutError: store did not answer in 2 s"

  @R61
  Scenario: A long error text is cut at 200 characters
    Given "user-ana" sends a request
    When user-ana's request makes a tool call "lookup_store" that raises TimeoutError with 500 characters
    And the calls of user-ana's request are read back
    Then the error of call 1 has 200 characters

  @R62
  Scenario: A trace write that DynamoDB refuses does not fail the call
    Given "user-ana" sends a request
    And AWS refuses PutItem on the traces table with "AccessDeniedException"
    When user-ana's request makes the calls "model plan"
    Then the call returned normally
    And the request printed "trace write failed"

  @R63
  Scenario: A request with more calls than one page holds
    Given "user-ana" sends a request
    And DynamoDB returns at most 2 items per Query page
    When user-ana's request makes the calls "model a, model b, model c, model d, model e"
    And the calls of user-ana's request are read back
    Then they are numbered 1 to 5

  @R64
  Scenario: A trace keeps a hash in place of the user id, and no prompt
    Given "user-ana" sends a request
    When user-ana's request makes the calls "model plan"
    Then no trace item holds "user-ana"
    And every trace item has the fields "request_id, seq, kind, name, started_at, duration_ms, status, error, tokens_in, tokens_out, cost_cad, user_hash, conversation_id, expires_at" and no other

  @R65
  Scenario: A trace item expires 7 days after its call ended
    Given "user-ana" sends a request
    When user-ana's request makes the calls "model plan"
    Then trace item 1 expires 7 days from now
