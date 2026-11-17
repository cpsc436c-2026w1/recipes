"""Where the example data lives: memory on the laptop, DynamoDB on AWS.

Both stores have the same methods, so the tools in main/tools.py do not know which one they have:
    candidates(words, available_only, max_pages)    books whose text holds any of the words
    book(book_id)                                   one book, or None
    holds(member)                                   the rows of one member's holds
    put_hold(member, request_id, book_id)           one conditional write; returns (row, created)

The write happens once per request key: the key of a hold is (member, request_id), and the store itself refuses
a second row with the same key (a condition expression in DynamoDB), returning the stored row.

This file and main/tools.py go into the tools Lambda's zip: they use the standard library and boto3 only.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

SEARCH_FIELDS = ("title", "author", "genre")
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "books.jsonl"


def read_books(path: Path = DATA_FILE) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    for r in rows:
        r["search_text"] = " ".join(str(r.get(f) or "") for f in SEARCH_FIELDS).lower()
    return rows


class MemoryStore:
    """The books file and the holds in this process: the offline stand-in for the two DynamoDB tables."""

    def __init__(self, path: Path = DATA_FILE):
        self.books = {r["book_id"]: r for r in read_books(path)}
        self.rows: dict[tuple[str, str], dict] = {}

    def candidates(self, words: list[str], available_only: bool = False, max_pages: int | None = None):
        return [dict(r) for r in self.books.values()
                if any(w in r["search_text"] for w in words)
                and (not available_only or r["copies_available"] > 0)
                and (max_pages is None or r["pages"] <= max_pages)]

    def book(self, book_id: str) -> dict | None:
        row = self.books.get(book_id)
        return dict(row) if row else None

    def holds(self, member: str) -> list[dict]:
        return sorted((r for (m, _), r in self.rows.items() if m == member), key=lambda r: r["placed_at"])

    def put_hold(self, member: str, request_id: str, book_id: str) -> tuple[dict, bool]:
        key = (member, request_id)
        if key in self.rows:                  # this request key was written before: return what was stored
            return self.rows[key], False
        self.rows[key] = {"member": member, "request_id": request_id, "book_id": book_id, "placed_at": time.time()}
        return self.rows[key], True


class DynamoStore:
    """Two DynamoDB tables: books (key: book_id) and holds (key: member, request_id).

    The search is a Scan with a filter: fine for a few hundred items in one 1 MB page, and not a search index.
    """

    def __init__(self, books_table: str, holds_table: str, region: str, session=None):
        import boto3

        ddb = (session or boto3.Session(region_name=region)).resource("dynamodb", region_name=region)
        self.books_table, self.holds_table = ddb.Table(books_table), ddb.Table(holds_table)

    def candidates(self, words: list[str], available_only: bool = False, max_pages: int | None = None):
        from boto3.dynamodb.conditions import Attr

        if not words:
            return []
        cond = Attr("search_text").contains(words[0])
        for w in words[1:]:
            cond = cond | Attr("search_text").contains(w)
        if available_only:
            cond = cond & Attr("copies_available").gt(0)
        if max_pages is not None:
            cond = cond & Attr("pages").lte(int(max_pages))
        rows, kwargs = [], {"FilterExpression": cond}
        while True:
            page = self.books_table.scan(**kwargs)
            rows += page["Items"]
            if "LastEvaluatedKey" not in page:
                return [_plain(r) for r in rows]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def book(self, book_id: str) -> dict | None:
        item = self.books_table.get_item(Key={"book_id": book_id}).get("Item")
        return _plain(item) if item else None

    def holds(self, member: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        # a strongly consistent read: the member sees the row that was just written
        items = self.holds_table.query(KeyConditionExpression=Key("member").eq(member), ConsistentRead=True)["Items"]
        return sorted((_plain(i) for i in items), key=lambda r: r["placed_at"])

    def put_hold(self, member: str, request_id: str, book_id: str) -> tuple[dict, bool]:
        from botocore.exceptions import ClientError

        row = {"member": member, "request_id": request_id, "book_id": book_id,
               "placed_at": Decimal(str(time.time()))}
        try:
            # the condition makes the write happen once: it fails when an item with this key already exists
            self.holds_table.put_item(Item=row, ConditionExpression="attribute_not_exists(request_id)")
            return _plain(row), True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            stored = self.holds_table.get_item(Key={"member": member, "request_id": request_id},
                                               ConsistentRead=True)["Item"]
            return _plain(stored), False


def _plain(item: dict) -> dict:
    """DynamoDB numbers come back as Decimal; the tools return ints and floats."""
    return {k: ((int(v) if v == v.to_integral_value() else float(v)) if isinstance(v, Decimal) else v)
            for k, v in item.items()}
