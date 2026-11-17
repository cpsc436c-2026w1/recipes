"""What pytest (tests/) and behave (features/) share to run the bash scripts without an AWS account.

- base_env: the environment of a script run: made-up credentials, no profile, no config files.
- FAILING_AWS: an `aws` that fails when it is called, for DRY=1 runs.
- FAKE_AWS: an `aws` that writes each call to a log and answers what the test tells it to. moto does not refuse a
  call and has no DELETING state, so refusals and that state are played by this stand-in. Its "not found" for
  DescribeTable is the error code of the API reference (ResourceNotFoundException); an Athena query ends in the
  state FAKE_AWS_ATHENA_STATE (default SUCCEEDED), an export in FAKE_AWS_EXPORT_STATUS (default COMPLETED).
- commands: the commands a script printed.
Nothing here reaches AWS.
"""

import os
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]

FAILING_AWS = "#!/bin/sh\necho \"aws was called under DRY=1: $*\" >&2\nexit 99\n"

FAKE_AWS = r"""#!/bin/bash
all="$*"; echo "${all//$'\n'/ }" >> "$FAKE_AWS_LOG"     # one log line per call, also for a query of several lines
echo "${AWS_REGION:-}" >> "$FAKE_AWS_LOG.region"
# FAKE_AWS_REFUSE_FROM=3: the first two calls that hold FAKE_AWS_REFUSE go through, the third and later are refused
if [ -n "${FAKE_AWS_REFUSE:-}" ] && [[ "$*" == *"$FAKE_AWS_REFUSE"* ]] \
   && [ "$(grep -c -F -- "$FAKE_AWS_REFUSE" "$FAKE_AWS_LOG")" -ge "${FAKE_AWS_REFUSE_FROM:-1}" ]; then
  echo "An error occurred (${FAKE_AWS_CODE:-AccessDeniedException}) when calling the operation: put in by the test" >&2
  exit 254
fi
case "$*" in
  *describe-table*)
    if [ -n "${FAKE_AWS_TABLE_STATUS:-}" ]; then echo "$FAKE_AWS_TABLE_STATUS"; exit 0; fi
    echo "An error occurred (ResourceNotFoundException) when calling the DescribeTable operation: not found" >&2
    exit 254;;
  *list-tables*|*get-databases*) [[ "$*" == *"${FAKE_AWS_LEFT_IN:-none}"* ]] && echo "${FAKE_AWS_LEFT:-}";;
  *describe-export*ExportStatus*) echo "${FAKE_AWS_EXPORT_STATUS:-COMPLETED}";;
  *batch-write-item*)
    # FAKE_AWS_UNPROCESSED=n: the first n load calls give one item back as unprocessed; "always": every call does
    echo x >> "$FAKE_AWS_LOG.batch"
    if [ "${FAKE_AWS_UNPROCESSED:-0}" = always ] || [ "$(wc -l < "$FAKE_AWS_LOG.batch")" -le "${FAKE_AWS_UNPROCESSED:-0}" ]; then
      echo '{"UnprocessedItems": {"recipe-trips": [{"PutRequest": {"Item": {"trip_id": {"S": "t-0007"}}}}]}, "ConsumedCapacity": [{"TableName": "recipe-trips", "CapacityUnits": 48.0}]}'
    else
      echo '{"UnprocessedItems": {}, "ConsumedCapacity": [{"TableName": "recipe-trips", "CapacityUnits": 50.0}]}'
    fi;;
  *"athena get-query-execution"*Status.State*) echo "${FAKE_AWS_ATHENA_STATE:-SUCCEEDED}";;
  *"s3 rm"*) [ -z "${FAKE_AWS_SILENT_FAIL:-}" ] || exit 1;;
esac
exit 0
"""


def base_env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(
        AWS_REGION="ca-central-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
        AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull, TMPDIR=str(tmp_path),
    )
    for name in ("KEEP", "DRY", "BUCKET", "PREFIX", "MAX_UNITS", "BATCH_CALLS"):
        env.pop(name, None)
    return env


def put_on_path(tmp_path: Path, script: str) -> Path:
    """Write `script` as an executable `aws` in a new folder under tmp_path; returns the folder."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "aws").write_text(script)
    (bin_dir / "aws").chmod(0o755)
    return bin_dir


SLEEP_LOG = "#!/bin/sh\necho \"$1\" >> \"$SLEEP_LOG\"\n"      # a `sleep` that writes down the wait and does not wait


def put_tool(bin_dir: Path, name: str, script: str) -> None:
    (bin_dir / name).write_text(script)
    (bin_dir / name).chmod(0o755)


def commands(stdout: str) -> list[str]:
    """The commands a script printed, one per `$ ` line."""
    return [line[2:] for line in stdout.splitlines() if line.startswith("$ ")]
