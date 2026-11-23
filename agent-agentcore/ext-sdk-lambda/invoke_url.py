"""Send a question to the function URL, signed with SigV4 by your AWS credentials, and print the answer.

    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/invoke_url.py "How many trips were requested on 2024-03-14?"
    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/invoke_url.py --unsigned "How many trips?"   # expect 403
    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/invoke_url.py --measure

The URL's auth type is AWS_IAM: Lambda checks the signature and the caller's lambda:InvokeFunctionUrl permission
before the function runs, so an unsigned request gets 403 and never starts it. Signing proves which AWS identity
sent the request; it is not a sign-in of the analyst.

--measure sends one question to warm the function, then the 12 questions of the eval trip-questions 3 times each,
one at a time, and prints the 36 response times (from sending the request to reading the whole reply) and their
p95 by nearest rank: the 35th of the 36 in order. Every reply counts, stopped and did_not_answer included.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
REGION = "ca-central-1"
CASES = HERE / "evals" / "trip_questions" / "cases.jsonl"
SLO_S = 30.0


def signed_request(url: str, body: bytes, credentials, region: str = REGION) -> urllib.request.Request:
    """A POST to the function URL with SigV4 headers for the service `lambda` (function URL access control)."""
    aws = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(credentials, "lambda", region).add_auth(aws)
    return urllib.request.Request(url, data=body, method="POST", headers=dict(aws.headers.items()))


def send(url: str, question: str, credentials=None, timeout: float = 70) -> tuple[int, dict, float]:
    """(HTTP status, JSON body, seconds from sending to the whole reply read)."""
    body = json.dumps({"question": question}).encode()
    req = (signed_request(url, body, credentials) if credentials is not None else
           urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"}))
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read().decode()
    except urllib.error.HTTPError as err:
        status, text = err.code, err.read().decode(errors="replace")
    took = time.monotonic() - start
    try:
        return status, json.loads(text), took
    except ValueError:
        return status, {"text": text[:300]}, took


def p95_nearest_rank(times: list[float]) -> float:
    """The value at rank ceil(0.95 n) in order: the 35th of 36."""
    ordered = sorted(times)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def questions() -> list[str]:
    return [json.loads(line)["question"] for line in CASES.read_text().splitlines() if line.strip()]


def function_url() -> str:
    if os.environ.get("FUNCTION_URL"):
        return os.environ["FUNCTION_URL"]
    from settings import load_settings

    name = load_settings().function_name
    return boto3.client("lambda", region_name=REGION).get_function_url_config(FunctionName=name)["FunctionUrl"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="?")
    p.add_argument("--unsigned", action="store_true", help="send without a signature (expect 403)")
    p.add_argument("--measure", action="store_true")
    p.add_argument("--runs", type=int, default=3)
    a = p.parse_args(argv)
    url = function_url()
    creds = None if a.unsigned else boto3.session.Session(region_name=REGION).get_credentials().get_frozen_credentials()
    if not a.measure:
        status, body, took = send(url, a.question or "How many trips were requested on 2024-03-14?", creds)
        print(f"HTTP {status} in {took:.2f} s")
        print(json.dumps(body, indent=2))
        return 0 if status == 200 else 1
    send(url, questions()[0], creds)                                   # warm the execution environment
    times = []
    for run in range(1, a.runs + 1):
        for i, q in enumerate(questions(), 1):
            status, body, took = send(url, q, creds)
            times.append(took)
            print(f"run {run} case {i:>2}  HTTP {status}  {took:6.2f} s  {body.get('status')}  {body.get('value')}")
    p95 = p95_nearest_rank(times)
    print(f"{len(times)} replies; p95 (nearest rank, {math.ceil(0.95 * len(times))}th in order) {p95:.2f} s; "
          f"target under {SLO_S:g} s: {'met' if p95 < SLO_S else 'NOT met'}")
    return 0 if p95 < SLO_S else 1


if __name__ == "__main__":
    sys.exit(main())
