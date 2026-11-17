Feature: Two searches over the same text, fused on ranks
  README, "Requirements": R10 to R14. The searches are the recipe's LocalSearch, the class main/run.py builds.

  @R10
  Scenario: Both indexes see a product's name, description, category and brand
    Given a catalogue of the products
      | id | name      | description           | category | brand  |
      | P1 | Desk lamp | Bright light          | lighting | Glow   |
      | P2 | Kettle    | Boils water           | kitchen  | Brew   |
    When I search keyword and embedding for "Glow"
    Then the keyword search returns "P1" first
    And the text the embedder was given for P1 is "Desk lamp. Bright light lighting Glow"

  @R11
  Scenario: Keyword results are ranked by BM25 with k1 = 1.2 and b = 0.75
    Given a catalogue of the products
      | id | name       | description                                  | category | brand |
      | P1 | Desk lamp  | A lamp                                       | lighting | Glow  |
      | P2 | Floor lamp | A tall lamp with a long cord and a heavy base | lighting | Glow  |
      | P3 | Kettle     | Boils water                                  | kitchen  | Brew  |
    When I search keyword for "lamp"
    Then the keyword results are "P1" with score 0.6951 and "P2" with score 0.5666

  @R12
  Scenario: A query with no word of the catalogue gets no keyword result
    Given the recipe's catalogue
    When I search keyword for "blutooth speeker"
    Then there is no keyword result

  @R13
  Scenario: Embedding search returns as many results as asked for, also for a query that matches nothing
    Given the recipe's catalogue
    When I search keyword for "qwzx"
    And I ask the embedding search for 10 results for "qwzx"
    Then there is no keyword result
    And the embedding search returns 10 results

  @R14
  Scenario: The fused score is 1 / (60 + rank) from each list, over the top 20 of each
    Given a catalogue of 25 products that all match "lamp"
    When I fuse the searches for "lamp", asking for 25 results
    Then every product in the top 20 of both lists has the fused score 1/(60 + keyword rank) + 1/(60 + embedding rank)
    And every product below the keyword top 20 gets only its embedding vote, and none when it is below the embedding top 20 too
