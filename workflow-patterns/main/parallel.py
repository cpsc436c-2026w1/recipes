"""Parallel calls: app code starts several model calls at the same time and combines their replies in code.

    uv run python main/parallel.py                    # stub, offline
    uv run python main/parallel.py --model gateway    # course LLM gateway, at most 5 model calls

Sectioning: different, independent parts of one request run side by side (here: a screen of the message and a
reply to it); app code combines them (the reply is shown only when the screen says "ok").
Voting: the same task runs VOTES times; app code takes the most common label and reports how many agreed.
Who decides the next step: app code. It chooses which calls run and how many; no call reads another call's reply.
Where app code is in control: the thread pool, the rule that combines the replies, and the stock reply.
Sources: Anthropic, "Building effective agents", Workflow: Parallelization
(https://www.anthropic.com/engineering/building-effective-agents); AI Engineering ch. 10, "AI Pipeline
Orchestration" (https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch10.html).
"""
import itertools
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from llm import Model, model_from_args

MESSAGE = "My oat milk arrived two days late and the carton was open. Do you still have rye bread?"
SCREEN = "Is this message to a grocery shop about groceries or an order? Reply with one word: ok or off_topic."
REPLY = "You answer for a grocery shop that sells oat milk, whole milk, rye bread and butter. Reply in one short sentence."
LABEL = "Label the customer's message with one word: complaint, question or order."
STOCK_REPLY = "I can only help with groceries and orders."
VOTES = 3


def sectioning(model: Model, message: str = MESSAGE) -> dict:
    """Two different calls on one message, at the same time; app code decides what the customer sees."""
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        screen = pool.submit(model.chat, SCREEN, message)
        reply = pool.submit(model.chat, REPLY, message)
        screen, reply = screen.result(), reply.result()
    ok = screen.strip().lower().strip(".\"'") == "ok"
    return {"screen": screen, "shown": reply if ok else STOCK_REPLY, "seconds": round(time.monotonic() - started, 1)}


def voting(model: Model, message: str = MESSAGE, votes: int = VOTES) -> dict:
    """The same call `votes` times, at the same time; app code counts the labels."""
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=votes) as pool:      # temperature above 0, so the replies can differ
        labels = list(pool.map(lambda _: model.chat(LABEL, message, 0.7).strip().lower().strip(".\"'"), range(votes)))
    label, count = Counter(labels).most_common(1)[0]
    return {"labels": sorted(labels), "label": label if count > votes / 2 else "no_majority",
            "agreed": f"{count} of {votes}", "seconds": round(time.monotonic() - started, 1)}


def make_stub(seconds: float = 0.2):
    """Fixed replies for offline runs, each after `seconds`; the three votes are two "complaint" and one "question"."""
    votes = itertools.cycle(["complaint", "question", "complaint"])

    def stub(system: str, user: str) -> str:
        time.sleep(seconds)                                  # a stand-in for the time a model call takes
        if system == SCREEN:
            return "ok"
        return next(votes) if system == LABEL else "Sorry about the late, open carton; yes, we have rye bread."
    return stub


if __name__ == "__main__":
    model = model_from_args(make_stub(), max_calls=2 + VOTES)
    try:
        for name, pattern in (("sectioning", sectioning), ("voting", voting)):
            print(name, *(f"  {key}: {value}" for key, value in pattern(model).items()), sep="\n")
    except RuntimeError as stop:                  # the call cap, or a failed gateway call
        print(f"stopped: {stop}")
    print(f"model calls: {len(model.calls)}")
