@own_moto
Feature: The local runs, each with moto started by the script itself
  `--target local` starts moto inside the script, so these scenarios start nothing themselves, and every network
  connection is refused while they run. README, "Requirements": R20, R32; ext-answer-cache R57; ext-traces R66.

  @R20 @R32
  Scenario: The main scenario's local run, with no network
    When main/run.py runs with "--target local"
    Then the exit code is 0
    And the printed lines after the first are:
      """
      tables created: test-chat-conversations, test-chat-history
      user-ana reads her newest 3 turns, newest first:
        assistant One: Victoria.
        user      And last year?
        assistant Two: Kelowna and Nanaimo.
      user-ben read on user-ana's conversation: refused
      user-ben write on user-ana's conversation: refused
      user-ben reads his own: 1 turn
      """

  @R57
  Scenario: The answer cache's local run
    When ext-answer-cache/cache_run.py runs with "--target local"
    Then the exit code is 0
    And the last printed line is "6 questions, 2 hits, 4 misses, 4 retrievals, 4 model calls"

  @R66
  Scenario: The traces' local run
    When ext-traces/trace_run.py runs with "--target local"
    Then the exit code is 0
    And the printed lines after the request id are:
      """
        1  model  plan           ok     in 420  out 30
        2  tool   search_orders  ok     in 0  out 0
        3  tool   lookup_store   error  in 0  out 0  TimeoutError: store service did not answer in 2 s
        4  model  reply          ok     in 610  out 55
      """
