"""The LLM step: one model call, declared as a DSPy signature inside a module.

Decision (README, Decisions: "One call as a signature"): the signature names the inputs (the user's question,
the tables) and the output (the SQL). DSPy writes the system prompt and the user message from it, and reads
the reply back into the `sql` field. GEPA changes only the signature's instructions, the docstring below;
the field names, their descriptions and the table description stay as written (README, Decisions: "What GEPA may change").
"""

from __future__ import annotations

import dspy

from main.data import SCHEMA


class QuestionToSQL(dspy.Signature):
    """Answer the question about the cafe's orders with one DuckDB SQL query over the tables given.
    The app runs the query and shows its single value to the user."""

    question: str = dspy.InputField(desc="a question from a user, in plain words")
    tables: str = dspy.InputField(desc="the tables and columns the query may use")
    sql: str = dspy.OutputField(desc="one DuckDB SELECT query that returns one row with one number")


SEED_INSTRUCTIONS = QuestionToSQL.instructions


class SQLAnswerer(dspy.Module):
    """The program GEPA optimizes. `instructions` replaces the docstring; None keeps the seed."""

    def __init__(self, instructions: str | None = None):
        super().__init__()
        sig = QuestionToSQL if instructions is None else QuestionToSQL.with_instructions(instructions)
        self.write_sql = dspy.Predict(sig)

    def forward(self, question: str) -> dspy.Prediction:
        # The model writes the SQL; it does not see the data. The metric runs the SQL (main/metric.py).
        return self.write_sql(question=question, tables=SCHEMA)


def instructions_of(program: SQLAnswerer) -> str:
    return program.write_sql.signature.instructions


def as_examples(questions: list[dict]) -> list[dspy.Example]:
    """DSPy examples: `question` is the input; id, sql and reference ride along for the metric."""
    return [dspy.Example(**q).with_inputs("question") for q in questions]
