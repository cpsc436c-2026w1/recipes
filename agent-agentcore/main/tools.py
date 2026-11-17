"""The example tools, as plain functions over a store (main/store.py): a small library.

    search_books(query, filters)                read
    get_book(book_id)                           read
    get_holds(member)                           read
    place_hold(member, book_id, request_id)     write: changes the member's holds, once per request key

Every result is a small dict that reaches the LLM as the tool result, so it is capped: at most MAX_RESULTS books
per search, without their summaries, and at most MAX_TEXT characters of summary in get_book. A result that was
cut says so ("more": the number of matches left out). The shapes are the models of main/contracts.py.
"""
from __future__ import annotations

import re

MAX_RESULTS = 5      # books in one search result. src: assumed (five short rows are about 250 input tokens)
MAX_TEXT = 300       # characters of summary in get_book. src: assumed
STOP_WORDS = {"a", "an", "the", "and", "or", "for", "with", "without", "of", "by", "some", "book", "books"}


def query_words(query: str) -> list[str]:
    """'The novels of Austen' -> ['novel', 'austen']: lower case, no stop words, no plural s."""
    words = [w for w in re.findall(r"[a-zà-ÿ]+", str(query).lower()) if w not in STOP_WORDS]
    return [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words]


class LibraryTools:
    def __init__(self, store):
        self.store = store

    def search_books(self, query: str, filters: dict | None = None) -> dict:
        filters = filters if isinstance(filters, dict) else {}
        words = query_words(query)
        rows = self.store.candidates(words, available_only=bool(filters.get("available_only")),
                                     max_pages=filters.get("max_pages"))
        # books that hold more of the words come first, then by title
        rows.sort(key=lambda r: (-sum(w in r["search_text"] for w in words), r["title"], r["book_id"]))
        top = [_short(r) for r in rows[:MAX_RESULTS]]
        return {"books": top, "more": max(0, len(rows) - len(top))}

    def get_book(self, book_id: str) -> dict:
        row = self.store.book(str(book_id))
        if row is None:
            return {"error": f"no book with id {book_id}"}
        return {**_short(row), "genre": row["genre"], "summary": row["summary"][:MAX_TEXT]}

    def get_holds(self, member: str) -> dict:
        held = []
        for row in self.store.holds(member):
            book = self.store.book(row["book_id"]) or {"title": "?"}
            held.append({"book_id": row["book_id"], "title": book["title"]})
        return {"member": member, "holds": held}

    def place_hold(self, member: str, book_id: str, request_id: str = "") -> dict:
        if not request_id:
            return {"error": "place_hold needs a request_id: app code sets it, one per approved write"}
        book = self.store.book(str(book_id))
        if book is None:
            return {"error": f"no book with id {book_id}"}
        row, created = self.store.put_hold(member, request_id, book["book_id"])
        # created is False when this request key was written before: no second hold was placed
        return {"placed": created, "duplicate_request": not created, "request_id": row["request_id"],
                "book_id": row["book_id"], "title": book["title"]}

    def call(self, name: str, args: dict) -> dict:
        """Run one tool call. A wrong name or wrong arguments become a tool result, not an exception."""
        tool = {"search_books": self.search_books, "get_book": self.get_book,
                "get_holds": self.get_holds, "place_hold": self.place_hold}.get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        try:
            return tool(**args)
        except TypeError as e:
            return {"error": f"wrong arguments for {name}: {e}"}


def _short(row: dict) -> dict:
    return {"book_id": row["book_id"], "title": row["title"], "author": row["author"], "year": row["year"],
            "pages": row["pages"], "copies_available": row["copies_available"]}
