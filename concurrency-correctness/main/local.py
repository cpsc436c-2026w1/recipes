"""Local stand-in for DynamoDB, for `--target local` and the tests: moto, with each call made one at a time.

DynamoDB checks a condition and writes the item in one step: two conditional writes to one item cannot both pass.
moto keeps its tables in Python dictionaries without a lock, so two threads could interleave inside one call. The
wrapper below holds one lock for the length of each call, which gives the same promise DynamoDB gives. It does not
hide the race this recipe shows: the read and the write are separate calls, and another client can write between them.
"""

import threading

import boto3

_LOCK = threading.Lock()


class OneCallAtATime:
    def __init__(self, client):
        self._client = client
        self.exceptions = client.exceptions

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with _LOCK:
                return attr(*args, **kwargs)
        return call


def client_factory(region: str):
    return lambda: OneCallAtATime(boto3.client("dynamodb", region_name=region))
