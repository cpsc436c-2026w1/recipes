"""Prompt chain: app code runs model calls in a fixed order and passes each reply into the next call.

    uv run python main/chain.py                    # stub, offline
    uv run python main/chain.py --model gateway    # course LLM gateway, at most 3 model calls

Steps: 1 rewrite the follow-up question so it stands alone -> 2 pick products from the list -> gate -> 3 write the reply.
Who decides the next step: app code. The order is written below; the model fills each step and never chooses one.
Where app code is in control: the order, the prices (read from PRODUCTS, not from the model), and the gate, a code
check between calls 2 and 3. A pick that names a product the shop does not sell stops the chain: call 3 is not made.
Sources: Anthropic, "Building effective agents", Workflow: Prompt chaining
(https://www.anthropic.com/engineering/building-effective-agents); AI Engineering ch. 5, "Break Complex Tasks into
Simpler Subtasks" (https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch05.html).
"""
from llm import Model, model_from_args

PRODUCTS = {"oat milk": 4.50, "whole milk": 3.20, "almond milk": 4.80, "rye bread": 5.00, "butter": 6.10}
HISTORY = ["Customer: Which milks do you have?", "Shop: Oat milk, whole milk and almond milk."]
QUESTION = "Which of them have no dairy?"

REWRITE = "Rewrite the customer's last question so that it can be read without the chat history. Reply with the question only."
PICK = ("Pick the products that answer the question. Use only names from this list: " + ", ".join(PRODUCTS) +
        ". Reply with the names only, separated by commas.")
ANSWER = "Write one short sentence for the customer that answers the question with these products and prices."


def gate(pick: str) -> tuple[list[str], list[str]]:
    """The code check between calls 2 and 3: (names in the pick, names that are not in PRODUCTS)."""
    names = [n.strip().lower().rstrip(".") for n in pick.split(",") if n.strip()]
    return names, [n for n in names if n not in PRODUCTS]


def run(model: Model, history: list[str] = HISTORY, question: str = QUESTION) -> dict:
    standalone = model.chat(REWRITE, "\n".join(history) + f"\nCustomer: {question}")            # call 1
    pick = model.chat(PICK, standalone)                                                         # call 2 reads reply 1
    names, unknown = gate(pick)
    if unknown or not names:                                                                    # the gate stops the chain
        return {"standalone": standalone, "pick": pick, "gate": "stopped", "not_listed": unknown, "reply": None}
    priced = "; ".join(f"{n}: {PRODUCTS[n]:.2f} CAD" for n in names)                            # prices come from app code
    reply = model.chat(ANSWER, f"Question: {standalone}\nProducts: {priced}")                   # call 3 reads reply 1 and the checked pick
    return {"standalone": standalone, "pick": pick, "gate": "passed", "not_listed": [], "reply": reply}


def stub(system: str, user: str) -> str:
    """Fixed replies for offline runs, one per step."""
    if system == REWRITE:
        return "Which of oat milk, whole milk and almond milk have no dairy?"
    if system == PICK:
        return "oat milk, almond milk"
    return "Oat milk (4.50 CAD) and almond milk (4.80 CAD) have no dairy."


if __name__ == "__main__":
    model = model_from_args(stub, max_calls=3)
    try:
        for key, value in run(model).items():
            print(f"{key}: {value}")
    except RuntimeError as stop:                  # the call cap, or a failed gateway call
        print(f"stopped: {stop}")
    print(f"model calls: {len(model.calls)}")
