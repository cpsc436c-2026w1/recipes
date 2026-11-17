Feature: Course constraints
  README, "Requirements": R60 and R61.

  @R60
  Scenario: The stub run needs no AWS account, no course token and no network
    Given every network connection fails the scenario
    And no course token and no gateway address in the environment
    When main/run.py runs with no options
    Then the exit code is 0

  @R61
  Scenario: Names come from PREFIX and the Region from AWS_REGION
    Given OpenSearch Serverless is a stand-in
    And PREFIX is "team7-ana" and AWS_REGION is "us-west-2"
    When main/run.py runs with "--backend opensearch"
    Then the collection, the group and the three policies are named "team7-ana-products"
    And every OpenSearch Serverless call went to us-west-2
