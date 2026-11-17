"""Steps for the three pattern files. The stubs are the ones the files ship with, or a reply set by the step."""
import threading

from behave import then, when

import chain
import evaluator
import parallel
from llm import CallCapReached, Model

NAMES = {chain.REWRITE: "rewrite", chain.PICK: "pick", chain.ANSWER: "answer",
         evaluator.GENERATOR: "generator", evaluator.EVALUATOR: "evaluator"}


def together(n, reply):
    """A stub whose calls each wait until n of them are in flight; calls one after the other would time out."""
    barrier = threading.Barrier(n, timeout=5)

    def stub(system, user):
        barrier.wait()
        return reply(system, user)
    return stub


def in_turn(replies):
    """A stub that gives the next of `replies` to each call, whichever thread asks."""
    lock, left = threading.Lock(), iter(replies)

    def stub(system, user):
        with lock:
            return next(left)
    return stub


@when("the chain runs on its stub")
def step(context):
    context.model = Model(chain.stub, max_calls=3)
    context.out = chain.run(context.model)


@when('the chain runs with the pick "{pick}"')
def step(context, pick):
    context.model = Model(lambda s, u: pick if s == chain.PICK else chain.stub(s, u), max_calls=3)
    context.out = chain.run(context.model)


@when("the chain runs on its stub with a cap of {cap:d} model calls")
def step(context, cap):
    context.model = Model(chain.stub, max_calls=cap)
    try:
        context.out = chain.run(context.model)
    except CallCapReached as stop:
        context.error = str(stop)


@when("sectioning runs on a stub that answers only when {n:d} calls are in flight")
def step(context, n):
    context.model = Model(together(n, parallel.make_stub(0)), max_calls=n)
    context.out = parallel.sectioning(context.model)


@when('sectioning runs with the screen reply "{screen}"')
def step(context, screen):
    context.model = Model(lambda s, u: screen if s == parallel.SCREEN else "a reply", max_calls=2)
    context.out = parallel.sectioning(context.model)


@when("voting runs on a stub that answers only when {n:d} calls are in flight")
def step(context, n):
    context.model = Model(together(n, parallel.make_stub(0)), max_calls=n)
    context.out = parallel.voting(context.model)


@when('voting runs with the votes "{votes}"')
def step(context, votes):
    context.model = Model(in_turn(votes.split(", ")), max_calls=3)
    context.out = parallel.voting(context.model)


def _check(context, evaluator_name):
    return evaluator.model_check(context.model) if evaluator_name == "a model call" else evaluator.code_check


@when("the evaluator loop runs on its stub with {name} as the evaluator")
def step(context, name):
    context.model = Model(evaluator.stub, max_calls=6)
    context.out = evaluator.run(context.model, _check(context, name))


@when("the evaluator loop runs with {name} as the evaluator and drafts that never pass")
def step(context, name):
    never = lambda s, u: "fail: still wrong" if s == evaluator.EVALUATOR else "a label without the price"
    context.model = Model(never, max_calls=20)
    context.out = evaluator.run(context.model, _check(context, name))


@then('the model calls are "{names}"')
def step(context, names):
    assert [NAMES[c["system"]] for c in context.model.calls] == names.split(", ")


@then("call {n:d} has the reply of call {m:d} as its user message")
def step(context, n, m):
    assert context.model.calls[n - 1]["user"] == context.model.calls[m - 1]["reply"]


@then("call {n:d} has the reply of call {m:d} in its user message")
def step(context, n, m):
    assert context.model.calls[m - 1]["reply"] in context.model.calls[n - 1]["user"]


@then('the second generator call has "{text}" in its user message')
def step(context, text):
    generator_calls = [c for c in context.model.calls if c["system"] == evaluator.GENERATOR]
    assert text in generator_calls[1]["user"], generator_calls[1]["user"]


@then('the result has {key} "{value}"')
def step(context, key, value):
    assert str(context.out[key]) == value, context.out


@then("{n:d} model calls were made and all of them overlap")
def step(context, n):
    calls = context.model.calls
    assert len(calls) == n
    assert max(c["start"] for c in calls) < min(c["end"] for c in calls)


@then("every model call has the same system prompt and user message")
def step(context):
    assert len({(c["system"], c["user"]) for c in context.model.calls}) == 1


@then("{n:d} model calls were made")
def step(context, n):
    assert len(context.model.calls) == n


@then('the run stopped with "{message}"')
def step(context, message):
    assert context.error == message, context.error
