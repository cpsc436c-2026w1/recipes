Feature: Filters inside the search, and facet counts (ext-filters)
  ext-filters/README.md, "Requirements": R70 to R73.

  @R70 @R71
  Scenario: The stub run of filters_run.py compares filtering inside and after
    When ext-filters/filters_run.py runs with no options
    Then the exit code is 0
    And the filter table is
      | query    | results inside | results after | recall@5 inside | recall@5 after |
      | speaker  | 5              | 2             | 1.00            | 1.00           |
      | two      | 3              | 1             | 1.00            | 0.00           |
      | light    | 2              | 1             | 1.00            | 1.00           |
      | wireless | 4              | 2             | 1.00            | 1.00           |
      | coffee   | 5              | 3             | 1.00            | 1.00           |
      | desk     | 2              | 1             | 1.00            | 1.00           |
      | charger  | 5              | 3             | 1.00            | 1.00           |
    And it printed "mean recall@5: inside 1.00, after 0.86"

  @R70
  Scenario: Every product of a filtered search passes the filter, in both searches
    Given the recipe's catalogue
    When I search hybrid for "two" with the filter category outdoor
    Then every result is in the category "outdoor"
    And the result holds the two-person tent "P16"

  @R72
  Scenario: Facet counts by category, brand and three price bands
    When ext-filters/filters_run.py runs with no options
    Then it printed the facets {'category': {'audio': 2, 'accessories': 2}, 'brand': {'Hush': 2, 'Key': 2}, 'price': {'under 50': 1, '50 to 200': 2, '200 and up': 1}}

  @R72
  Scenario Outline: The price bands' edges
    Given a product priced <price> CAD
    Then its facet price band is "<band>"

    Examples:
      | price  | band       |
      | 49.99  | under 50   |
      | 50     | 50 to 200  |
      | 199.99 | 50 to 200  |
      | 200    | 200 and up |

  @R73
  Scenario: On OpenSearch the filter goes inside the match query and inside the knn query
    Given OpenSearch Serverless is a stand-in
    When ext-filters/filters_run.py runs with "--backend opensearch"
    Then the exit code is 0
    And every multi_match query of a filtered search is in a bool query with the filter clause
    And every knn query of a filtered search carries the filter clause inside the knn query
