"""The once-only write: a key computed from the request, and a store that refuses the key a second time."""
import pytest

from main.infra import ensure_tables, load_books
from main.request_key import KEY_CHARS, request_key
from main.store import DynamoStore, MemoryStore
from main.tools import LibraryTools
from tests.conftest import no_wait


def test_the_same_request_member_and_book_give_the_same_key():
    assert request_key("req-1", "sam", "b02") == request_key("req-1", "sam", "b02")
    assert len(request_key("req-1", "sam", "b02")) == KEY_CHARS


@pytest.mark.parametrize("other", [("req-2", "sam", "b02"), ("req-1", "alex", "b02"), ("req-1", "sam", "b01")])
def test_another_request_member_or_book_gives_another_key(other):
    assert request_key(*other) != request_key("req-1", "sam", "b02")


@pytest.mark.parametrize("parts", [("", "sam", "b02"), ("req-1", "", "b02"), ("req-1", "sam", "")])
def test_a_key_needs_all_three_parts(parts):
    with pytest.raises(ValueError):
        request_key(*parts)


@pytest.fixture(params=["memory", "dynamodb"])
def any_library(request, s):
    """The same tests on both stores."""
    if request.param == "memory":
        yield LibraryTools(MemoryStore())
        return
    aws = request.getfixturevalue("aws")
    ensure_tables(s, aws, no_wait)
    load_books(s, aws)
    yield LibraryTools(DynamoStore(s.books_table, s.holds_table, s.aws_region))


def test_a_second_write_with_the_same_key_returns_the_stored_row_and_adds_nothing(any_library):
    key = request_key("req-1", "sam", "b02")
    first = any_library.place_hold("sam", "b02", key)
    again = any_library.place_hold("sam", "b02", key)
    assert (first["placed"], first["duplicate_request"]) == (True, False)
    assert (again["placed"], again["duplicate_request"], again["request_id"]) == (False, True, key)
    assert any_library.get_holds("sam")["holds"] == [{"book_id": "b02", "title": "Emma"}]


def test_a_write_without_a_key_is_refused(any_library):
    assert "needs a request_id" in any_library.place_hold("sam", "b02", "")["error"]
    assert any_library.get_holds("sam")["holds"] == []


def test_the_read_tools_give_the_same_results_on_both_stores(any_library):
    found = any_library.search_books("Austen novels", {"available_only": True})
    assert [b["book_id"] for b in found["books"]][0] == "b02" and found["more"] >= 0
    assert all(b["copies_available"] > 0 for b in found["books"])
    assert [b["book_id"] for b in any_library.search_books("wells", {"max_pages": 150})["books"]] == ["b07"]
    assert any_library.get_book("b01")["pages"] == 612 and isinstance(any_library.get_book("b01")["pages"], int)
    assert "error" in any_library.get_book("zzz")
    assert "wrong arguments" in any_library.call("get_book", {"id": "b01"})["error"]
