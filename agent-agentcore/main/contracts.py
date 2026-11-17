"""The tool contracts: one pydantic model for the arguments of each tool and one for each result.

The example tools are a small library: search the books, read one book, read a member's holds, place a hold.
Replace them with your own (README, Adapt it); the three uses below stay the same.

The same models are used in three places, so they cannot drift apart:
  - `tool_schema()` builds the tool definitions the AgentCore Gateway target gets (and the LLM sees);
  - `check_arguments()` validates a tool call the LLM asked for, before the tool runs (main/hooks.py);
  - `parse_result()` validates what a tool returned.

The tools Lambda itself does not import this file: its zip holds only the standard library and boto3, and the
arguments are validated on the agent's side before the call leaves (README, Decisions).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")      # an argument the tool does not have is refused, not dropped


class SearchFilters(Arguments):
    available_only: bool = Field(default=False, description="true: only books with a copy on the shelf now")
    max_pages: int | None = Field(default=None, gt=0, description="only books with at most this many pages")


class SearchBooksArgs(Arguments):
    query: str = Field(min_length=1, max_length=80, description="one or two words: a title, an author or a genre")
    filters: SearchFilters | None = Field(default=None, description="optional filters")


class GetBookArgs(Arguments):
    book_id: str = Field(min_length=1, max_length=32, description="the book's id")


class GetHoldsArgs(Arguments):
    member: str = Field(min_length=1, max_length=64, description="the library member")


class PlaceHoldArgs(Arguments):
    member: str = Field(min_length=1, max_length=64, description="the library member")
    book_id: str = Field(min_length=1, max_length=32, description="the book's id")
    request_id: str = Field(default="", max_length=64, description="set by the app, one per approved write; leave it empty")


class Book(BaseModel):
    book_id: str
    title: str
    author: str
    year: int
    pages: int
    copies_available: int = Field(ge=0)


class BookDetail(Book):
    genre: str
    summary: str


class SearchResult(BaseModel):
    books: list[Book]
    more: int = Field(ge=0)


class Hold(BaseModel):
    book_id: str
    title: str


class Holds(BaseModel):
    member: str
    holds: list[Hold]


class HoldResult(BaseModel):
    placed: bool
    duplicate_request: bool
    request_id: str
    book_id: str
    title: str


class ToolError(BaseModel):
    error: str


# name -> (what the LLM reads about the tool, arguments, result)
TOOLS: dict[str, tuple[str, type[Arguments], type[BaseModel]]] = {
    "search_books": ("Search the library catalogue by one or two plain words (a title, an author or a genre). "
                     "Returns at most 5 books, best match first, with book_id, title, author, pages and "
                     "copies_available.", SearchBooksArgs, SearchResult),
    "get_book": ("One book by its id: title, author, year, pages, copies_available, genre, summary.",
                 GetBookArgs, BookDetail),
    "get_holds": ("The books a member has on hold.", GetHoldsArgs, Holds),
    "place_hold": ("Place a hold on one book for a member. The member is asked to approve first.",
                   PlaceHoldArgs, HoldResult),
}
WRITE_TOOLS = frozenset({"place_hold"})     # every tool here waits for a person's approval (main/hooks.py)
# The keys a Gateway tool definition takes in inputSchema (bedrock-agentcore-control, SchemaDefinition).
# src: receipt: botocore 1.43.117 service model of CreateGatewayTarget, read 2026-11-16
SCHEMA_KEYS = ("type", "properties", "required", "items", "description")


def _plain(schema: dict, defs: dict) -> dict:
    """A pydantic JSON schema cut down to the keys the Gateway takes: no $ref, anyOf, title or default."""
    if "$ref" in schema:
        schema = {**defs[schema["$ref"].split("/")[-1]], **{k: v for k, v in schema.items() if k != "$ref"}}
    if "anyOf" in schema:                               # "X or null" from an optional argument: keep X
        chosen = next(s for s in schema["anyOf"] if s.get("type") != "null")
        schema = {**_plain(chosen, defs), **{k: v for k, v in schema.items() if k != "anyOf"}}
    out = {k: schema[k] for k in SCHEMA_KEYS if k in schema}
    if "properties" in out:
        out["properties"] = {name: _plain(sub, defs) for name, sub in out["properties"].items()}
    if "items" in out:
        out["items"] = _plain(out["items"], defs)
    return out


def tool_schema() -> list[dict]:
    """The tool definitions, in the form of a Gateway Lambda target's inline tool schema."""
    out = []
    for name, (description, arguments, _) in TOOLS.items():
        schema = arguments.model_json_schema()
        out.append({"name": name, "description": description,
                    "inputSchema": _plain(schema, schema.get("$defs", {}))})
    return out


def short_name(tool_name: str) -> str:
    """'place_hold' for the local tool and for the Gateway's 'library-tools___place_hold'."""
    return tool_name.split("___")[-1]


def check_arguments(tool_name: str, arguments: dict) -> tuple[dict, str]:
    """(the validated arguments, "") or ({}, the reason the call is refused), for one tool call of the LLM."""
    name = short_name(tool_name)
    if name not in TOOLS:
        return {}, f"unknown tool {name}"
    try:
        checked = TOOLS[name][1].model_validate(arguments)
    except ValidationError as err:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in err.errors())
        return {}, f"wrong arguments for {name}: {problems}"
    return checked.model_dump(exclude_none=True), ""


def parse_result(tool_name: str, result: dict) -> BaseModel:
    """The tool's result as its model, or ToolError when the tool refused. Raises when it is neither."""
    if "error" in result:
        return ToolError.model_validate(result)
    return TOOLS[short_name(tool_name)][2].model_validate(result)
