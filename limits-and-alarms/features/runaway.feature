@own_moto
Feature: Extension: a runaway loop, stopped by its own limits and by the budget outside it
  run_agent in ext-runaway-loop/agent_loop.py, the function a team copies, with a simulated clock and a stand-in for
  the course LLM gateway's per-key budget and rate (fake_gateway.py). ext-runaway-loop/README.md, "Requirements":
  R60 to R70. Nothing here calls AWS, so no moto is started, and every network connection is refused.

  @R60
  Scenario: The step cap stops a runaway before its 9th model call
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with the default limits
    Then the task stopped with "step_cap" after 8 model calls

  @R61
  Scenario: No model call starts 20 seconds or more after the task started
    Given a model that always asks for another tool call and takes 4 seconds a call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with a step cap of 100
    Then the task stopped with "deadline" after 5 model calls
    And every model call started less than 20 seconds after the task started

  @R62
  Scenario Outline: A refusal by the gateway ends the task, with no second attempt
    Given a model that always asks for another tool call, behind a stand-in gateway that refuses every call with "<code>"
    When the loop runs one task with the default limits
    Then the task stopped with "<code>" after 0 model calls
    And the gateway was asked 1 time

    Examples:
      | code             |
      | budget_exhausted |
      | rate_limited     |

  @R63
  Scenario: Only the newest 10 tool results go back to the model
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with a step cap of 15
    Then the context of model call 11 holds 10 tool results
    And the context of model call 15 holds 10 tool results
    And the context of model call 3 holds 2 tool results

  @R64
  Scenario: One metric line per task, with its stop reason
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with the default limits
    Then the task wrote 1 metric line with StopReason "step_cap", AgentSteps 8 and Errors 1
    And the line has a second directive with the dimensions "Service, StopReason" and the one metric "AgentSteps"

  @R65
  Scenario Outline: In Lambda, the deadline leaves 2 seconds before the function's timeout
    Given Lambda has <remaining> ms left for the function
    Then the loop's deadline for a cap of 20 seconds is <deadline> seconds

    Examples:
      | remaining | deadline |
      | 30000     | 20       |
      | 9000      | 7        |
      | 1000      | 0        |

  @R66
  Scenario: With every limit in the loop turned off, the budget stops the loop
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 0.05 CAD
    When the loop runs one task with no step cap, no deadline and no cost cap
    Then the task stopped with "budget_exhausted"
    And the task spent at most 0.05 CAD

  @R67
  Scenario: The extension's run
    When ext-runaway-loop/loop_run.py runs with an EMF file in a temporary folder
    Then the exit code is 0
    And the printed lines are:
      """
      task                               stopped by         steps  spent CAD  seconds
      finishes in 3 steps                done                   3    0.00009        3
      runaway, step cap                  step_cap               8    0.00029        8
      runaway, slow model, deadline      deadline               5    0.00016       20
      runaway, cost cap                  cost_cap             190    0.00993      190
      runaway, loop limits all off       budget_exhausted     945    0.04995      945
      """
    And the EMF file has 5 lines

  @R68
  Scenario: A runaway that spends past 0.50 CAD in a day puts the daily-cost alarm in ALARM
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with no step cap, no deadline and no cost cap
    Then the daily-cost alarm, checked against that task's metric line, is "ALARM"

  @R69
  Scenario: The cost cap stops a task before a call that could take it past 0.01 CAD
    Given a model that always asks for another tool call, behind a stand-in gateway with a budget of 1 CAD
    When the loop runs one task with no step cap and no deadline
    Then the task stopped with "cost_cap"
    And the task spent at most 0.01 CAD
    And the next model call could have taken the task past 0.01 CAD

  @R70
  Scenario: A gateway client for another model than the one the cost cap prices is refused before any call
    Given a model that always asks for another tool call, behind a stand-in gateway for "qwen3.8-27b" with a budget of 1 CAD
    When the loop runs one task with the default limits
    Then the loop stopped before any call with a message that names "qwen3.6-35b-a3b" and "qwen3.8-27b"

  @R70
  Scenario: A gateway client that names no model is refused before any call while the cost cap is on
    Given a gateway client for the runaway model that has no model attribute
    When the loop runs one task with the default limits
    Then the loop stopped before any call with a message that names "qwen3.6-35b-a3b" and "no .model"
