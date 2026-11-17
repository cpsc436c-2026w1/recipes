"""What pytest (tests/) and behave (features/) share to run the bash scripts without an AWS account.

- FAKE_AWS: an `aws` that writes each call to a log and answers what the test tells it to: a refusal with a
  given error code, a function that is there already, a change that is still in progress, a name that is still
  there after the clean-up. Its error codes are those of the API reference pages the README links.
- FAILING: a tool that fails when it is called, for DRY=1 runs.
- LOGGING: a tool that writes down its arguments and does nothing (`curl`, `sleep`).
- PYTHON3: a `python3` that writes down a run of the handler and does not run it (a run of the handler would call
  AWS), and passes every other use to the real python3 (the zip of step 4).
Nothing here reaches AWS and nothing waits.
"""

import os
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]

FAKE_AWS = r"""#!/bin/bash
all="$*"; echo "${all//$'\n'/ }" >> "$FAKE_AWS_LOG"     # one log line per call, also for a policy of several lines
echo "${AWS_REGION:-}" >> "$FAKE_AWS_LOG.region"
refuse() { echo "An error occurred ($1) when calling the operation: ${2:-put in by the test}" >&2; exit 254; }
if [ -n "${FAKE_AWS_REFUSE:-}" ] && [[ "$*" == *"$FAKE_AWS_REFUSE"* ]]; then
  refuse "${FAKE_AWS_CODE:-AccessDeniedException}" "${FAKE_AWS_MESSAGE:-}"
fi
# FAKE_AWS_BUSY=n: the first n calls of add-permission are refused as "in progress"
if [ -n "${FAKE_AWS_BUSY:-}" ] && [[ "$*" == *add-permission* ]]; then
  echo x >> "$FAKE_AWS_LOG.busy"
  [ "$(wc -l < "$FAKE_AWS_LOG.busy")" -gt "$FAKE_AWS_BUSY" ] \
    || refuse ResourceConflictException "The operation cannot be performed at this time. An update is in progress for resource: arn:aws:lambda:ca-central-1:111122223333:function:recipe-api"
fi
case "$*" in
  *get-caller-identity*) echo 111122223333;;
  *describe-table*) [ -z "${FAKE_AWS_NO_TABLES:-}" ] || refuse ResourceNotFoundException "Requested resource not found: Table not found";;
  *get-function-url-config*) echo "https://abc123.lambda-url.ca-central-1.on.aws/";;
  *"lambda get-function "*) [ -n "${FAKE_AWS_EXISTS:-}" ] || refuse ResourceNotFoundException;;
  *"logs create-log-group"*) [ -z "${FAKE_AWS_EXISTS:-}" ] || refuse ResourceAlreadyExistsException "The specified log group already exists";;
  *"iam create-role"*) [ -z "${FAKE_AWS_EXISTS:-}" ] || refuse EntityAlreadyExists "Role with name recipe-api-role already exists.";;
  *create-function-url-config*) [ -z "${FAKE_AWS_EXISTS:-}" ] || refuse ResourceConflictException \
    "Failed to create function url config for [functionArn = arn:aws:lambda:ca-central-1:111122223333:function:recipe-api]. Error message:  FunctionUrlConfig exists for this Lambda function";;
  *add-permission*) [ -z "${FAKE_AWS_EXISTS:-}" ] || refuse ResourceConflictException \
    "The statement id (url-public) provided already exists. Please specify a new statement id, or remove the existing statement.";;
  *"lambda invoke"*) for a in "$@"; do case "$a" in *-out.json) echo '{"ok": true}' > "$a";; esac; done;;
  *list-functions*|*describe-log-groups*|*list-roles*) [[ "$*" == *"${FAKE_AWS_LEFT_IN:-none}"* ]] && echo "${FAKE_AWS_LEFT:-}";;
esac
exit 0
"""


FAILING = "#!/bin/sh\necho \"{tool} was called under DRY=1: $*\" >&2\nexit 99\n"
LOGGING = "#!/bin/bash\nall=\"$*\"; echo \"${all//$'\\n'/ }\" >> \"$TOOL_LOG.{tool}\"\n"
PYTHON3 = f"""#!/bin/sh
case "$1" in *handler.py) echo "$*" >> "$TOOL_LOG.python3"; exit 0;; esac
exec {sys.executable} "$@"
"""


def put_tools(bin_dir: Path, **tools: str) -> Path:
    """Write each tool (name -> script) as an executable in bin_dir; returns bin_dir."""
    bin_dir.mkdir(exist_ok=True)
    for name, script in tools.items():
        (bin_dir / name).write_text(script.replace("{tool}", name))
        (bin_dir / name).chmod(0o755)
    return bin_dir


def script_env(tmp_path: Path) -> dict:
    """The environment of a script run: no AWS variables of the caller, no KEEP, DRY or ROLE_ARN."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AWS_") and k not in ("KEEP", "DRY", "ROLE_ARN", "PREFIX", "CREATE_TRIES", "CREATE_WAIT", "LOG_DAYS",
                                                     "ROLE_WAIT")}
    return env | {"TMPDIR": str(tmp_path)}


def commands(stdout: str) -> list[str]:
    """The commands a script printed, one per `$ ` line."""
    return [line[2:] for line in stdout.splitlines() if line.startswith("$ ")]
