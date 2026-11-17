"""Offline runs: moto plays AWS inside this process. Fake keys only; nothing leaves the machine."""
import contextlib
import os


@contextlib.contextmanager
def fake_aws():
    from moto import mock_aws
    saved = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                                            "AWS_PROFILE", "AWS_DEFAULT_REGION")}
    os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="ca-central-1")
    os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ.pop("AWS_PROFILE", None)
    try:
        with mock_aws():
            yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
