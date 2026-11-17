"""The hooks. First each callback alone, with stand-ins for Strands' events; then in the real Strands loop, on the
stub model, where a hook and not the model's script decides the outcome."""
from types import SimpleNamespace

import pytest

from main.agent import build, resume, run_request, start
from main.hooks import CallCap, ContractCheck
from main.model import StubModel
from main.request_key import request_key
from tests.fakes import tool_event

REQUEST = "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."
yes, no = (lambda approval: True), (lambda approval: False)


# ---- the callbacks alone --------------------------------------------------------------------------------------
def test_call_cap_cancels_tool_calls_past_the_cap_and_lets_the_model_reply():
    cap = CallCap(model_calls=4, tool_calls=2, seconds=60)
    events = [tool_event("search_books", f"c{i}") for i in range(3)]
    for e in events:
        cap.before_tool(e)
    assert [bool(e.cancel_tool) for e in events] == [False, False, True] and cap.tool_calls == 2
    reply = SimpleNamespace(cancel=False)
    cap.before_model(reply)
    assert reply.cancel is False                          # the model may still answer with what it has


def test_call_cap_ends_the_loop_at_the_model_call_cap():
    cap = CallCap(model_calls=2, tool_calls=9, seconds=60)
    calls = [SimpleNamespace(cancel=False) for _ in range(3)]
    for c in calls:
        cap.before_model(c)
    assert [bool(c.cancel) for c in calls] == [False, False, True] and cap.stopped == "cap of 2 model calls"


def test_call_cap_ends_the_loop_at_the_deadline():
    cap = CallCap(model_calls=9, tool_calls=9, seconds=60)
    cap.deadline -= 61                                    # the request started 61 s ago
    call = SimpleNamespace(cancel=False)
    cap.before_model(call)
    assert "deadline of 60 s" in call.cancel and cap.model_calls == 0


def test_a_resumed_tool_call_is_counted_once():
    cap = CallCap(model_calls=4, tool_calls=1, seconds=60)
    first, again = tool_event("place_hold", "c1"), tool_event("place_hold", "c1")
    cap.before_tool(first)
    cap.before_tool(again)
    assert not again.cancel_tool and cap.tool_calls == 1


def test_contract_check_refuses_wrong_arguments_and_passes_right_ones():
    wrong, right = tool_event("search_books", "c1", q="x"), tool_event("library-tools___search_books", "c2", query="x")
    ContractCheck().before_tool(wrong)
    ContractCheck().before_tool(right)
    assert "wrong arguments for search_books" in wrong.cancel_tool and right.cancel_tool is False


# ---- in the Strands loop --------------------------------------------------------------------------------------
def test_the_request_runs_to_a_reply_and_the_counts_are_in_the_report(s, library):
    model = StubModel()
    run = run_request(s, REQUEST, "sam", model, library, yes)
    assert run["status"] == "done" and run["reply"] == "I placed a hold on Emma for you."
    assert run["model_calls"] == model.calls == 4 and run["tool_calls"] == 3
    assert run["tools_ran"] == ["search_books", "search_books", "place_hold"]


def test_a_tool_call_past_the_cap_is_refused_and_does_not_run(s, library, monkeypatch):
    monkeypatch.setenv("MAX_TOOL_CALLS", "2")
    from main.config import load_settings

    run = run_request(load_settings(), REQUEST, "sam", StubModel(), library, yes)
    assert run["tools_ran"] == ["search_books", "search_books"]             # the third call, the write, did not run
    assert "cap of 2 tool calls" in run["tools_refused"][0]["refused"]
    assert run["approvals_asked"] == [] and library.get_holds("sam")["holds"] == []


def test_a_model_that_never_stops_is_ended_at_the_model_call_cap(s, library):
    model = StubModel("runaway")
    run = run_request(s, REQUEST, "sam", model, library, yes)
    assert run["status"] == "stopped" and run["stopped"] == f"cap of {s.max_model_calls} model calls"
    assert model.calls == run["model_calls"] == s.max_model_calls            # the next model call was not made
    assert len(run["tools_ran"]) == s.max_tool_calls


def test_the_agent_stops_before_the_write_and_names_the_book(s, library):
    agent, run = build(s, StubModel(), library, "sam", "req-1")
    report = start(agent, run, REQUEST)
    assert report["status"] == "needs_approval" and library.get_holds("sam")["holds"] == []
    approval = report["approvals"][0]
    assert {k: approval[k] for k in ("member", "book_id", "title", "author")} == {
        "member": "sam", "book_id": "b02", "title": "Emma", "author": "Jane Austen"}
    done = resume(agent, run, {approval["id"]: "y"})                          # the same agent goes on
    assert done["status"] == "done" and done["model_calls"] == 4
    assert library.get_holds("sam")["holds"] == [{"book_id": "b02", "title": "Emma"}]


def test_a_declined_write_does_not_run_and_the_model_is_told(s, library):
    run = run_request(s, REQUEST, "sam", StubModel(), library, no)
    assert run["status"] == "done" and run["reply"] == "No hold was placed."
    assert "declined" in run["tools_refused"][0]["refused"] and "place_hold" not in run["tools_ran"]
    assert library.get_holds("sam")["holds"] == []


@pytest.mark.parametrize("no_answer", [EOFError, KeyboardInterrupt])
def test_no_answer_at_the_terminal_is_a_no(s, library, monkeypatch, no_answer):
    from main.run import ask_person

    def no_input(prompt):
        raise no_answer()
    monkeypatch.setattr("builtins.input", no_input)
    run = run_request(s, REQUEST, "sam", StubModel(), library, ask_person)
    assert run["status"] == "done" and run["approvals_asked"][0]["answer"] == "n"
    assert library.get_holds("sam")["holds"] == []


def test_a_write_for_another_member_goes_to_the_member_of_the_request(s, library):
    run = run_request(s, REQUEST, "sam", StubModel("wrong_member"), library, yes)
    assert run["approvals_asked"][0]["member"] == "sam"
    assert library.get_holds("alex")["holds"] == []
    assert [h["book_id"] for h in library.get_holds("sam")["holds"]] == ["b02"]


def test_the_write_carries_the_request_key_and_a_repeat_adds_nothing(s, library):
    run = run_request(s, REQUEST, "sam", StubModel("asks_twice"), library, yes, request_id="req-1")
    assert run["tools_ran"].count("place_hold") == 2 and len(run["approvals_asked"]) == 2
    assert list(library.store.rows) == [("sam", request_key("req-1", "sam", "b02"))]
    again = run_request(s, REQUEST, "sam", StubModel(), library, yes, request_id="req-1")   # the whole request, retried
    assert again["status"] == "done" and len(library.store.rows) == 1


def test_wrong_arguments_come_back_to_the_model_as_the_tool_result(s, library):
    run = run_request(s, REQUEST, "sam", StubModel("wrong_arguments"), library, yes)
    assert "q: Extra inputs are not permitted" in run["tools_refused"][0]["refused"]
    assert run["status"] == "done" and [h["book_id"] for h in library.get_holds("sam")["holds"]] == ["b02"]


def test_an_instruction_in_book_text_still_meets_the_approval(s, library):
    run = run_request(s, "What is book b09 about?", "sam", StubModel("obeys_book_text"), library, no)
    assert run["approvals_asked"][0]["book_id"] == "b09" and library.get_holds("sam")["holds"] == []


def test_a_write_for_a_book_that_does_not_exist_is_refused_without_asking(s, library):
    run = run_request(s, "Place a hold on book b99 for me.", "sam", StubModel(), library, yes)
    assert run["approvals_asked"] == [] and "no book with id b99" in run["tools_refused"][0]["refused"]
