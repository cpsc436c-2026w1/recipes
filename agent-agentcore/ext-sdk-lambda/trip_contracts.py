"""What the model sees and what it must return, the same for both SDKs (README, Decisions: one prompt, one tool).

  RunSqlArgs   the arguments of the one tool, run_sql (checked before the tool runs, caps.SqlContract)
  TripAnswer   the model's final output: the reply for the analyst and the value it states, or null
  SYSTEM_PROMPT, RUN_SQL_DESCRIPTION   the words both agents get
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

TOOL_NAME = "run_sql"
OUTPUT_NAME = "TripAnswer"     # Strands names its structured-output tool after the class (model.__name__)


class RunSqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=2000, description="one DuckDB SELECT over the table trips")


class TripAnswer(BaseModel):
    """The final output. `value` is the one number the question asks for, or null when the trips cannot answer it."""
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=1000, description="one or two sentences for the analyst")
    value: float | None = Field(description="the number the question asks for, from a run_sql result; null when "
                                            "the trips table cannot answer the question")


RUN_SQL_DESCRIPTION = (
    "Run one read-only DuckDB SELECT over the table trips and get its columns and rows (at most 50 rows). "
    "Any other statement is refused.")

SYSTEM_PROMPT = """You answer an operations analyst's question about the trips of one day, 2024-03-14, in three
pickup zones (48, 161 and 230). The only data is the DuckDB table trips:
  trip_id VARCHAR, trip_state VARCHAR ('completed'), pickup_zone_id INTEGER, requested_time TIMESTAMP,
  driver_id VARCHAR (always null), fare DOUBLE (the base passenger fare only: tips, tolls and taxes are not in
  it, and the table has no column for them), trip_miles DOUBLE.
Use the tool run_sql to get every number you state; never guess a number. Send one SELECT over trips per call.
A time window runs from its start up to its end, the end not included: "between 08:00 and 09:00" means
hour(requested_time) = 8, "from 15:00 to 18:00" means hour(requested_time) >= 15 AND hour(requested_time) < 18,
and a window that ends at midnight takes every hour from its start to the end of the day ("from 20:00 to
midnight" means hour(requested_time) >= 20). Put parentheses around a condition with OR.
When the answer is computed from several numbers (a difference, a sum, a largest), compute it in one SELECT over
trips, with subqueries if needed; do not do the arithmetic yourself: the value must be a cell of a query result.
Then give your final answer: reply (one or two sentences that state the value with all its digits) and value
(the number the question asks for, exactly as a query returned it). In the reply, write no other number of 10 or
more unless the question or a query result holds it; do not repeat the date.
If the table cannot answer the question, set value to null and say why in the reply, without a query: a question
about another day than 2024-03-14 (the table holds no other day, so a count over it is 0, not an answer), or about
something no column holds (a fare is not a tip)."""
