Feature: The production path on OpenSearch Serverless
  README, "Requirements": R40 to R48. main/run.py runs with "--backend opensearch"; OpenSearch Serverless and the
  collection's endpoint are stand-ins (features/support.py), and the waits are written down, not waited.

  Background:
    Given OpenSearch Serverless is a stand-in

  @R40 @R41 @R61
  Scenario: The first run creates the group, the collection and its three policies
    When main/run.py runs with "--backend opensearch"
    Then the exit code is 0
    And the collection group "recipe-ana-products" is NEXTGEN with standby replicas ENABLED and 0 to 2 OCU for indexing and for search
    And the collection "recipe-ana-products" is a VECTORSEARCH collection in that group with standby replicas ENABLED
    And the encryption policy of "recipe-ana-products" uses a key AWS owns, for that collection only
    And the network policy of "recipe-ana-products" allows the public endpoint, for that collection only
    And the data access policy of "recipe-ana-products" names only the identity that ran the script
    And every OpenSearch Serverless call went to ca-central-1

  @R42
  Scenario: One index holds the text, filter and vector fields of every product
    When main/run.py runs with "--backend opensearch"
    Then the index "products" maps name and description as text, category and brand as keyword, price_cad as float, in_stock as boolean
    And the index "products" has a knn_vector field "embedding" of dimension 96 with cosine similarity
    And the index "products" holds 30 documents under the product ids

  @R42
  Scenario: With the gateway's embeddings the vector field has 1,024 numbers
    Given GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values
    And the gateway is a stand-in
    When main/run.py runs with "--backend opensearch --embedder gateway"
    Then the index "products" has a knn_vector field "embedding" of dimension 1024 with cosine similarity

  @R43
  Scenario: A hybrid search is one match query and one knn query, fused on ranks
    When main/run.py runs with "--backend opensearch"
    Then every search request to the endpoint is signed for the service "aoss"
    And each hybrid search sent one multi_match query with "name^2" and one knn query, each for 20 results
    And the hybrid results are the reciprocal rank fusion of the two answers, k = 60

  @R44
  Scenario: A run after an earlier run uses what is there
    Given everything of an earlier run is still there
    When main/run.py runs with "--backend opensearch"
    Then the exit code is 0
    And there is 1 collection group, 1 collection and 3 policies

  @R45
  Scenario: A run right after a clean-up waits for the collection that is being deleted
    Given the collection "recipe-ana-products" is being deleted
    When main/run.py runs with "--backend opensearch"
    Then the exit code is 0
    And the collection "recipe-ana-products" was created after the old one was gone

  @R46
  Scenario Outline: A group that vanishes after "already exists" is made again
    Given the group "recipe-ana-products" answers "already exists" and is then gone
    And a collection create that names a missing group is refused with <code>
    When main/run.py runs with "--backend opensearch"
    Then the exit code is 0
    And CreateCollectionGroup was called twice and the collection is in the group

    Examples:
      | code                      |
      | ResourceNotFoundException |
      | ValidationException       |

  @R47
  Scenario: A collection that is not ACTIVE within 15 minutes stops the run
    Given the collection never becomes ACTIVE
    When main/run.py runs with "--backend opensearch"
    Then the exit code is 1
    And the run stopped with "collection recipe-ana-products not active after 900 s"

  @R48
  Scenario Outline: A new collection that refuses at first is tried again every 10 s, for up to 300 s
    Given the collection's endpoint answers 401 to the first <denied> requests
    When main/run.py runs with "--backend opensearch"
    Then the exit code is <code>
    And the recipe waited 10 seconds <waits> times

    Examples:
      | denied | code | waits |
      | 3      | 0    | 3     |
      | 29     | 0    | 29    |
      | 30     | 1    | 29    |
