"""Offline harness for the bash scenarios.

Two ways to run a script without an AWS account:

- dry: DRY=1, with an `aws` on the PATH that fails if it is ever called, so the test sees the printed commands only.
- local: the real AWS CLI against a moto server on localhost (AWS_ENDPOINT_URL), with made-up credentials and no
  profile, so nothing can reach AWS. Skipped when the AWS CLI is not installed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import FAILING_AWS, RECIPE, base_env as _base_env, commands, put_on_path  # noqa: F401


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    """No test depends on the caller's profile: a profile set in the shell could point a test at a real account."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


@pytest.fixture
def dry(tmp_path):
    """dry(script, **env) -> CompletedProcess. Runs `bash <script>` with DRY=1; any call to aws fails the test."""
    fake = put_on_path(tmp_path, FAILING_AWS)

    def _run(script, **extra):
        env = _base_env(tmp_path) | {"DRY": "1", "PATH": f"{fake}:{os.environ['PATH']}"} | extra
        p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=60)
        assert "aws was called" not in p.stderr, p.stderr
        assert p.returncode == 0, p.stdout + p.stderr
        return p

    return _run


@pytest.fixture
def moto_url():
    """A fresh moto server per test, so no test sees another test's tables."""
    if not shutil.which("aws"):
        pytest.skip("AWS CLI not installed; the dry-run tests still check every command")
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    # moto keeps its state per process, not per server: reset it so that this test starts with no tables.
    import urllib.request

    urllib.request.urlopen(urllib.request.Request(url + "/moto-api/reset", method="POST"), timeout=10)
    yield url
    server.stop()


@pytest.fixture
def local(tmp_path, moto_url):
    """local(script, **env) -> CompletedProcess. Runs `bash <script>` with the AWS CLI pointed at the moto server."""

    def _run(script, check=True, **extra):
        env = _base_env(tmp_path) | {"AWS_ENDPOINT_URL": moto_url} | extra
        p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=180)
        if check:
            assert p.returncode == 0, p.stdout + p.stderr
        return p

    return _run

