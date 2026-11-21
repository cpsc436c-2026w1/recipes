#!/usr/bin/env python3
"""Helpers of ext-vector-search/run.sh. Python's standard library only, so it runs in CloudShell as it is.

    python3 vectors.py embed OUT                 make the vectors through the course LLM gateway (needs a token)
    python3 vectors.py dim FILE                  print the number of dimensions of the vectors in FILE
    python3 vectors.py item FILE TRIP OUT        write the values of one trip's update-item (message and vector)
    python3 vectors.py query FILE NAME OUT       write the search vector of one question
    python3 vectors.py record KIND LOG           add the JSON reply on stdin (a write or a search) to LOG
    python3 vectors.py cost LOG                  print the bytes of the run, their price, and the estimate rule

The gateway's address comes from COURSE_GATEWAY_URL and the token from KB_MCP_TOKEN (the older GATEWAY_URL and
COURSE_MCP_TOKEN still work when the new names are not set). Neither is ever printed.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = "qwen3-embedding-0.6b"
# ca-central-1, AWS bulk price list for AmazonDynamoDB (publication date 2026-11-11, read 2026-11-21). README,
# Decisions: cost.
USD_PER_GB_SEARCHED = 0.0022       # DDB-VectorSearch
USD_PER_GB_WRITTEN = 0.572         # DDB-VectorWriteRequest
USD_PER_MILLION_WRITE_UNITS = 0.6875  # table write units, on demand, Standard table class
USD_PER_GB_MONTH = 0.275           # table storage; assumed for the vector index too (README, Cost)
GB = 2**30                         # DynamoDB sizes are binary: 1 GB = 2^30 bytes (developer guide, Quotas)
MIN_BYTES = 1024                   # DynamoDB meters each vector request at 1 KB at least


def embed(out: str) -> None:
    url = (os.environ.get("COURSE_GATEWAY_URL") or os.environ.get("GATEWAY_URL") or "").rstrip("/")
    token = os.environ.get("KB_MCP_TOKEN") or os.environ.get("COURSE_MCP_TOKEN") or ""
    if not url or not token:
        sys.exit("FAILED: set COURSE_GATEWAY_URL and KB_MCP_TOKEN first (README, Run it)")
    data = json.loads((HERE / "messages.json").read_text())
    names = list(data["messages"]) + list(data["questions"])
    texts = list(data["messages"].values()) + list(data["questions"].values())
    body = json.dumps({"model": MODEL, "input": texts}).encode()
    req = urllib.request.Request(url + "/embeddings", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        reply = json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:      # the status and the gateway's reason, never the address or the token
        sys.exit(f"FAILED: the course LLM gateway answered {e.code}: {e.read().decode(errors='replace')[:300]}")
    rows = sorted(reply["data"], key=lambda r: r["index"])
    if len(rows) != len(texts):
        sys.exit(f"FAILED: the gateway returned {len(rows)} vectors for {len(texts)} texts")
    vectors = {n: [round(x, 7) for x in r["embedding"]] for n, r in zip(names, rows)}
    dims = {len(v) for v in vectors.values()}
    if len(dims) != 1:
        sys.exit(f"FAILED: the vectors do not all have the same number of dimensions: {sorted(dims)}")
    trips = {n: vectors[n] for n in data["messages"]}
    questions = {n: vectors[n] for n in data["questions"]}
    Path(out).write_text(json.dumps({"model": MODEL, "dimensions": dims.pop(), "trips": trips,
                                     "questions": questions}, separators=(",", ":")) + "\n")
    usage = reply.get("usage") or {}
    print(json.dumps({"model": reply.get("model", MODEL), "texts": len(texts),
                      "dimensions": len(next(iter(trips.values()))), "prompt_tokens": usage.get("prompt_tokens")},
                     indent=4))


def number(x: float) -> dict:
    """A DynamoDB Number with no exponent: DynamoDB keeps the vector as 32-bit floats, about 7 digits."""
    return {"N": format(x, ".7f").rstrip("0").rstrip(".") or "0"}


def item(file: str, trip: str, out: str) -> None:
    v = json.loads(Path(file).read_text())
    message = json.loads((HERE / "messages.json").read_text())["messages"][trip]
    Path(out).write_text(json.dumps({":m": {"S": message}, ":v": {"L": [number(x) for x in v["trips"][trip]]}}))


def query(file: str, name: str, out: str) -> None:
    v = json.loads(Path(file).read_text())
    Path(out).write_text(json.dumps([number(x) for x in v["questions"][name]]))


def record(kind: str, log: str) -> None:
    text = sys.stdin.read().strip()
    reply = json.loads(text) if text else {}
    with open(log, "a") as f:
        f.write(json.dumps({"kind": kind, "bytes": reply.get("vector_write_bytes") if kind == "write"
                            else reply.get("search_bytes"),
                            "units": reply.get("write_units") if kind == "write" else None}) + "\n")


def cost(log: str) -> None:
    rows = [json.loads(line) for line in Path(log).read_text().splitlines()] if Path(log).exists() else []
    out = {}
    for kind, price in (("write", USD_PER_GB_WRITTEN), ("search", USD_PER_GB_SEARCHED)):
        measured = [r["bytes"] for r in rows if r["kind"] == kind and r["bytes"] is not None]
        billed = sum(max(MIN_BYTES, b) for b in measured)
        out[kind] = {"requests": len(measured), "bytes_reported": sum(measured), "bytes_billed": billed,
                     "bytes_per_request": round(sum(measured) / len(measured)) if measured else None,
                     "usd": round(billed / GB * price, 10)}
    units = [r.get("units") for r in rows if r["kind"] == "write" and r.get("units") is not None]
    out["table_write_units"] = ({"units": sum(units), "units_per_write": round(sum(units) / len(units), 1),
                                 "usd": round(sum(units) / 1e6 * USD_PER_MILLION_WRITE_UNITS, 10)} if units else
                                "unavailable: this log has no write units; run the writes again to measure them")
    print(json.dumps(out, indent=4))
    w, s = out["write"]["bytes_per_request"], out["search"]["bytes_per_request"]
    u = out["table_write_units"]["units_per_write"] if units else None
    if w and s:
        print(f"\nEstimate for your data, from these bytes (measure your own: the bytes depend on your dimensions,"
              f" projection and TopK; 1 GB = 2^30 bytes):\n"
              + (f"  table writes: items x {u} write units x {USD_PER_MILLION_WRITE_UNITS} USD per million, once,"
                 f" and again for every changed item\n" if u else
                 "  table writes: not measured in this log; run the writes again to measure them\n") +
              f"  load:     items x {max(MIN_BYTES, w)} bytes x {USD_PER_GB_WRITTEN} USD per GB, once,"
              f" and again for every changed vector\n"
              f"  searches: searches x {max(MIN_BYTES, s)} bytes x {USD_PER_GB_SEARCHED} USD per GB; measure the bytes"
              f" again with an index of your own size, your TopK, filters and projection\n"
              f"  storage:  index size x {USD_PER_GB_MONTH} USD per GB-month (assumed: the table storage price;"
              f" check the DynamoDB pricing page)")

if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"embed": embed, "dim": lambda f: print(json.loads(Path(f).read_text())["dimensions"]),
     "item": item, "query": query, "record": record, "cost": cost}[cmd](*args)
