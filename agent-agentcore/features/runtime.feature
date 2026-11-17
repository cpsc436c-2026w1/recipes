Feature: The agent is hosted on AgentCore Runtime

  @R60
  Scenario: The Runtime comes from a zip in S3 and its configuration holds no secret
    Given the recipe is deployed
    Then the Runtime is created from the zip in the code bucket, with entry point agent_entry.py
    And the Runtime's configuration does not hold the course token

  @R61
  Scenario: The course token goes to Parameter Store
    Given the recipe is deployed
    Then the course token is a SecureString parameter

  @R62
  Scenario: An approval in the same session resumes the agent that waits
    Given a Runtime entry point on the stub model
    When session "a" sends '{"mode": "request", "prompt": "Which is shorter, Dune or Emma? Place a hold on the shorter one for me.", "member": "sam"}'
    Then the status is "needs_approval"
    When session "a" sends '{"mode": "approve", "answer": "y"}'
    Then the status is "done"
    And the report counts 4 model calls and 3 tool calls
    And member "sam" has holds on "b02"

  @R63
  Scenario: An approval in a session where no agent waits is an error
    Given a Runtime entry point on the stub model
    When session "a" sends '{"mode": "request", "prompt": "Which is shorter, Dune or Emma? Place a hold on the shorter one for me.", "member": "sam"}'
    And session "b" sends '{"mode": "approve", "answer": "y"}'
    Then the status is "error"
    And the error says "no approval is waiting in this session"
    And member "sam" has no holds

  @R64
  Scenario: A second deploy updates what is there
    Given the recipe is deployed
    When the deploy runs
    Then there is still one Gateway, one target and one Runtime
