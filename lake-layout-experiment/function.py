"""Package and deploy <PREFIX>-duckdb-query (arm64, Python 3.12, 2,048 MB, 300 s, /tmp 512 MB, async retries 0),
its role <PREFIX>-duckdb-role (GetObject on layout/*, ListBucket with prefix layout/, its own logs) and its log group
(7 days). DuckDB 2.0.1 for Python 3.12 on arm64 and its httpfs extension for linux_arm64, without the aws extension:
about 29 MiB zipped and 85 MiB unzipped (measured 2026-11-21), under Lambda's 50 MB direct upload and 250 MB limits.
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUCKDB_VERSION = "2.0.1"
MEMORY_MB = 2048  # at 1,769 MB a function has the equivalent of one vCPU (Lambda quotas)
TIMEOUT_S = 300
HTTPFS = f"https://extensions.duckdb.org/v{DUCKDB_VERSION}/linux_arm64/httpfs.duckdb_extension.gz"


# extensions.duckdb.org refuses urllib's own User-Agent with 403 (the first live build, 2026-11-23); any named client is
# served
USER_AGENT = "lake-layout-experiment/1.0"


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def role_policy(names, account: str, region: str = "ca-central-1") -> dict:
    b = f"arn:aws:s3:::{names.bucket}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ReadTables", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"{b}/layout/*"},
        {"Sid": "ListTables", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": b,
         "Condition": {"StringLike": {"s3:prefix": ["layout/*"]}}},
        {"Sid": "OwnLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:{names.log_group}:*"}]}


def package(out: Path) -> Path:
    """The zip: handler.py, the duckdb wheel's files, httpfs.duckdb_extension."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # uv's pip (a uv project's venv has no pip of its own); the wheel for Python 3.12 on arm64 Linux
        import shutil

        if shutil.which("uv"):
            cmd = ["uv", "pip", "install", "--target", str(tmp), "--python-platform", "aarch64-manylinux_2_28",
                   "--python-version", "3.12", "--only-binary", ":all:", "--no-deps", f"duckdb=={DUCKDB_VERSION}"]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--target", str(tmp), "--platform", "manylinux_2_28_aarch64",
                   "--python-version", "3.12", "--only-binary=:all:", "--no-deps", f"duckdb=={DUCKDB_VERSION}"]
        subprocess.run(cmd, check=True)
        (tmp / "httpfs.duckdb_extension").write_bytes(gzip.decompress(download(HTTPFS)))
        (tmp / "handler.py").write_bytes((HERE / "duckdb_fn" / "handler.py").read_bytes())
        out.parent.mkdir(parents=True, exist_ok=True)  # build/ exists only after an offline run (the live build)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(tmp.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(tmp))
    return out


def deploy(names, sess, zip_path: Path, log=print) -> str:
    import lake
    from tags import record, tags

    account = lake.account_id(sess)
    iam, logs, lam = sess.client("iam"), sess.client("logs"), sess.client("lambda")
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
        "Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=names.role)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=names.role, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=tags("iam"))["Role"]["Arn"]
        record("iam-role", names.role, "created", arn=arn, script="experiment.py build", clean="experiment.py clean")
    iam.put_role_policy(RoleName=names.role, PolicyName="read-layout",
                        PolicyDocument=json.dumps(role_policy(names, account)))
    try:
        logs.create_log_group(logGroupName=names.log_group, tags=tags("logs"))
        record("log-group", names.log_group, "created", script="experiment.py build", clean="experiment.py clean")
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=names.log_group, retentionInDays=7)
    code = zip_path.read_bytes()
    try:
        lam.get_function(FunctionName=names.function)
        lam.update_function_code(FunctionName=names.function, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=names.function)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a new role takes a few seconds before Lambda may assume it
            try:
                fn = lam.create_function(FunctionName=names.function, Runtime="python3.12", Architectures=["arm64"],
                                         Role=arn, Handler="handler.handler", Code={"ZipFile": code},
                                         MemorySize=MEMORY_MB, Timeout=TIMEOUT_S, EphemeralStorage={"Size": 512},
                                         Tags=tags("lambda"))
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 9:
                    raise
                time.sleep(3)
        record("lambda-function", names.function, "created", arn=fn["FunctionArn"], script="experiment.py build",
               clean="experiment.py clean")
        lam.get_waiter("function_active_v2").wait(FunctionName=names.function)
    lam.put_function_event_invoke_config(FunctionName=names.function, MaximumRetryAttempts=0)
    log(f"function {names.function}: {len(code) / 2**20:.1f} MiB zipped")
    return names.function


assert io
