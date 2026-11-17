Feature: The extensions
  ext-embeddings/README.md, "Requirements": R80 to R83; ext-zero-etl/README.md, "Requirements": R90 and R91.

  @R80 @R81
  Scenario: Embeddings kept current, offline
    When ext-embeddings/worker.py runs with no options
    Then the exit code is 0
    And the printed lines are those of "output/embeddings-local.txt"

  @R82 @R83
  Scenario: The vector collection is in a group of its own, and the enqueue function's batches it gives up on go to the dead-letter queue
    Given AWS is a stand-in
    And main/run.py has run with "--target aws --setup"
    When ext-embeddings/worker.py runs with "--setup --target aws"
    Then the exit code is 0
    And the collection "recipe-ana-item-vectors" is a VECTORSEARCH collection in the group "recipe-ana-search-vectors"
    And the mapping of "recipe-ana-reembed-enqueue" has 10 retries, maximum record age 3600, bisect on and the dead-letter queue "recipe-ana-reembed-dlq" as its on-failure destination

  @R90
  Scenario: The pipeline, offline
    When ext-zero-etl/zero_etl.py runs with no options
    Then the exit code is 0
    And the printed lines are those of "output/zero-etl-local.txt"

  @R91
  Scenario: The pipeline's document id, action and version
    When ext-zero-etl/pipeline.py renders the pipeline for the example settings
    Then its sink writes each item under its primary key, with the stream's action, and an external document version
