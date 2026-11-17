Feature: Chat history per signed-in user, with an owner check and expiry
  ChatHistory in main/history.py, the class a team copies into its app. README, "Requirements": R10 to R19.
  Every scenario runs against moto, a local stand-in for DynamoDB, with a clock the scenario moves.

  Background:
    Given the tables of the main scenario exist for PREFIX "test"

  @R10
  Scenario: A new conversation belongs to the user who started it
    When "user-ana" starts a conversation
    Then the conversation item has the owner "user-ana"
    And the conversation expires 30 days from now

  @R11
  Scenario: Each new turn moves the conversation's expiry forward
    Given "user-ana" has a conversation
    When 20 days pass
    And "user-ana" adds the turn "Still there?"
    Then the conversation expires 30 days from now
    And the turn "Still there?" expires 30 days from now

  @R12
  Scenario: The owner reads her newest turns, newest first
    Given "user-ana" has a conversation with the turns "turn 0, turn 1, turn 2, turn 3, turn 4", one second apart
    When "user-ana" asks for her newest 3 turns
    Then the turns are "turn 4, turn 3, turn 2"

  @R12
  Scenario Outline: The turns returned hold at most 4,000 characters, and always the newest
    Given "user-ana" has a conversation with turns of <sizes> characters, one second apart
    When "user-ana" asks for her newest 10 turns
    Then the turns returned have <returned> characters

    Examples:
      | sizes            | returned    |
      | 2, 1999, 2000    | 2000, 1999  |
      | 1, 1999, 2000    | 2000, 1999, 1 |
      | 10, 5000         | 5000        |

  @R13
  Scenario: Another user's read is refused
    Given "user-ana" has a conversation with the turns "my address is 12 Oak St", one second apart
    When "user-ben" asks for the newest 10 turns of that conversation
    Then the call is refused with NotYourConversation
    And no turn was returned
    And the turns of the conversation were not queried

  @R14
  Scenario: Another user's write is refused and writes nothing
    Given "user-ana" has a conversation with the turns "hello", one second apart
    When 1 day passes
    And "user-ben" adds the turn "injected turn"
    Then the call is refused with NotYourConversation
    And the history table holds only the turns "hello"
    And the conversation's expiry did not change

  @R15
  Scenario Outline: Missing, expired and foreign conversations get the same refusal
    Given "user-ana" has a conversation
    When <what happens>
    And "<user>" asks for the newest 10 turns of <which>
    Then the call is refused with NotYourConversation
    And the refusal names only the conversation id

    Examples:
      | what happens  | user     | which                             |
      | 0 days pass   | user-ana | the conversation "no-such-id"     |
      | 31 days pass  | user-ana | that conversation                 |
      | 0 days pass   | user-ben | that conversation                 |

  @R16
  Scenario: An expired turn that DynamoDB still stores is left out
    Given "user-ana" has a conversation with the turns "old turn", one second apart
    And the turn "old turn" expired one second ago
    When "user-ana" adds the turn "new turn"
    And "user-ana" asks for her newest 10 turns
    Then the turns are "new turn"
    And the history table still stores 2 turns

  @R17
  Scenario: A page that holds only expired turns does not end the read
    Given "user-ana" has a conversation with the turns "kept, expired 0, expired 1, expired 2, expired 3", one second apart
    And the turns "expired 0, expired 1, expired 2, expired 3" expired one second ago
    When "user-ana" asks for her newest 2 turns
    Then the turns are "kept"
    And the turns were read in 3 Query pages

  @R18
  Scenario: A write cancelled by a conflict is a failed write, not a refusal
    Given "user-ana" has a conversation
    And DynamoDB cancels the next transaction with the reasons "TransactionConflict, None"
    When "user-ana" adds the turn "hello"
    Then the call fails with TransactionCanceledException
    And the call is not refused with NotYourConversation

  @R19
  Scenario: Two turns in the same millisecond are both kept
    Given "user-ana" has a conversation
    When "user-ana" adds the turn "a"
    And "user-ana" adds the turn "b"
    And "user-ana" asks for her newest 10 turns
    Then the turns are "b, a" in any order
