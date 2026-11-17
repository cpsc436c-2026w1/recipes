"""GEPA over the program's instructions: reflect on train, choose on validation. The test split is not loaded here.

Decision (README, Decisions: "Three splits"): this module receives train and validation only. It does not import
`load_test`; tests/test_splits.py checks that, and checks that no test question reaches the task model while
this module runs.
"""

from __future__ import annotations

import dspy
from dspy.teleprompt.gepa import InstructionProposer

from main.metric import gepa_metric
from main.models import Budget
from main.program import SQLAnswerer, as_examples, instructions_of


def optimize(train: list[dict], val: list[dict], task_lm: dspy.BaseLM, reflection_lm: dspy.BaseLM,
             budget: Budget, max_metric_calls: int, seed: int = 0) -> tuple[SQLAnswerer, list[dict]]:
    """Return the program with the best validation score, and every candidate GEPA kept."""
    dspy.configure(lm=task_lm, adapter=dspy.ChatAdapter())
    gepa = dspy.GEPA(
        metric=gepa_metric,
        reflection_lm=reflection_lm,
        # The reflection call reads and writes plain text fields (ChatAdapter). DSPy's default here is the
        # JSONAdapter, which may ask the endpoint for structured output; plain text works on any chat model.
        instruction_proposer=InstructionProposer(adapter=dspy.ChatAdapter()),
        # Spend cap, part 1: GEPA's budget of task-model calls on train and validation questions.
        max_metric_calls=max_metric_calls,
        # Three train questions per reflection, as dspy.GEPA(auto="light") does: the reflection model reads
        # a few failures closely instead of many at once.
        reflection_minibatch_size=3,
        # Keep every candidate that is best on at least one validation question, and start from one of them.
        candidate_selection_strategy="pareto",
        use_merge=False,      # merging two candidates is a second search move; one move keeps the run readable
        num_threads=4,
        track_stats=True,     # keeps every candidate and its validation score for the report
        seed=seed,
        # Spend cap, part 2: stop before the next reflection once the reflection or money cap is reached.
        gepa_kwargs={"stop_callbacks": [budget.stop]},
    )
    best = gepa.compile(SQLAnswerer(), trainset=as_examples(train), valset=as_examples(val))
    res = best.detailed_results
    candidates = [{"idx": i, "val_score": round(s, 4), "parents": res.parents[i], "instructions": instructions_of(c)}
                  for i, (c, s) in enumerate(zip(res.candidates, res.val_aggregate_scores))]
    return best, candidates
