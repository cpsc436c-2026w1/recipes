"""Evaluator-optimizer: one model call writes a draft, an evaluator returns pass or fail with feedback, and app
code asks for a new draft until it passes or the round limit is reached.

    uv run python main/evaluator.py                                  # stub, offline
    uv run python main/evaluator.py --model gateway --max-calls 4    # course LLM gateway

Model as evaluator: a second model call judges the draft against written criteria (Anthropic's form of the pattern).
Code check as evaluator: the same loop with the check in app code; use it when a rule can be written as code.
Who decides the next step: app code. The evaluator only says pass or fail; the loop and its limit are below.
Where app code is in control: the round limit (ROUNDS), what the feedback is added to, and reading the verdict
(a verdict that does not start with "pass" counts as a fail).
Sources: Anthropic, "Building effective agents", Workflow: Evaluator-optimizer
(https://www.anthropic.com/engineering/building-effective-agents); AI Engineering ch. 6, "Reflection and error
correction" (https://learning.oreilly.com/library/view/ai-engineering/9781098166298/ch06.html).
"""
from llm import Model, model_from_args

TASK = "Write a shelf label for oat milk, 1 L, 4.50 CAD, from a farm in Abbotsford."
GENERATOR = "You write shelf labels for a grocery shop. Reply with the label text only."
RULES = "The label is at most 12 words and names the price 4.50."
EVALUATOR = f"You check a shelf label. {RULES} Reply 'pass', or 'fail: ' and what to change, in one line."
ROUNDS = 3


def code_check(draft: str) -> tuple[bool, str]:
    """Evaluator in app code: the same rules as RULES."""
    problems = [p for p, bad in ((f"{len(draft.split())} words, at most 12 allowed", len(draft.split()) > 12),
                                 ("the price 4.50 is missing", "4.50" not in draft)) if bad]
    return not problems, "; ".join(problems)


def model_check(model: Model):
    """Evaluator as a model call: returns a function with the same shape as code_check."""
    def check(draft: str) -> tuple[bool, str]:
        verdict = model.chat(EVALUATOR, draft).strip()
        return verdict.lower().startswith("pass"), verdict
    return check


def run(model: Model, check, rounds: int = ROUNDS) -> dict:
    user = TASK
    for n in range(1, rounds + 1):
        draft = model.chat(GENERATOR, user)                       # generator call
        passed, feedback = check(draft)                           # evaluator: a model call or a code check
        if passed:
            break
        user = f"{TASK}\nYour last label: {draft}\nIt was rejected: {feedback}\nWrite it again."
    return {"passed": passed, "rounds": n, "label": draft, "last_feedback": feedback}


def stub(system: str, user: str) -> str:
    """Fixed replies for offline runs: a first draft that is too long, a second one that passes."""
    if system == EVALUATOR:
        passed, problems = code_check(user)
        return "pass" if passed else f"fail: {problems}"
    if "rejected" in user:
        return "Oat milk 1 L, Abbotsford farm, 4.50 CAD"
    return "Creamy oat milk from a family farm in Abbotsford, one litre, only 4.50 CAD today"


if __name__ == "__main__":
    model = model_from_args(stub, max_calls=3 * ROUNDS)           # 2 * ROUNDS for the first loop, ROUNDS for the second
    try:
        for name, check in (("model as evaluator", model_check(model)), ("code check as evaluator", code_check)):
            print(name, *(f"  {key}: {value}" for key, value in run(model, check).items()), sep="\n")
    except RuntimeError as stop:                  # the call cap, or a failed gateway call
        print(f"stopped: {stop}")
    print(f"model calls: {len(model.calls)}")
