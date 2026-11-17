"""Nothing under any recipe's output/ gives away an account, an endpoint or a key.

    python3 -m pytest -q tools/test_outputs.py        # or: uv run --with pytest pytest -q tools/

output/ holds captured runs that students and coding assistants read. A capture is masked by hand or by
tools/capture.sh, so this test is the check that the masking was done (AGENTS.md: no keys, tokens or account numbers
in code, commits or outputs). It reads files only; it needs no account and no network.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MASKED_ACCOUNT = "111122223333"          # the account number AWS documentation uses in examples
# The one other stand-in in the captures: the organization's management account in the ARN of a service control
# policy, masked with the second example number of the AWS documentation.
MASKED_ORG_POLICY = "arn:aws:organizations::444455556666:policy/o-exampleorgid/"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")   # a request id can end in 12 digits

# Every standalone 12-digit number reads as an account: not part of a longer digit run (epoch milliseconds are 13
# digits) and not either side of a decimal point. An account number appears in many shapes ("Account": "...",
# Account=..., account ..., <account>.dkr.ecr...), so no neighbouring character is required.
ACCOUNT = re.compile(r"(?<![0-9])(?<![0-9]\.)[0-9]{12}(?![0-9])(?!\.[0-9])")
# 12 digits that are an account for certain: inside an ARN, or as the suffix of a bucket or role name.
ACCOUNT_IN_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:([0-9]{12}):")
# Hosts that are an address of one deployment: an API Gateway stage, a Lambda function URL, an OpenSearch Serverless
# collection or an OpenSearch Service domain, a CloudFront distribution. A service principal (es.amazonaws.com) and a
# placeholder (<id>.cloudfront.net) are not hosts of a deployment.
HOSTS = {
    "execute-api or lambda-url host":
        re.compile(r"[a-z0-9]+\.(?:execute-api|lambda-url)\.[a-z0-9-]+\.(?:amazonaws\.com|on\.aws)"),
    "OpenSearch host": re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z]{2}(?:-[a-z]+)+-[0-9]\.(?:aoss|es)\.amazonaws\.com"),
    # the address a NextGen Serverless collection gave on 2026-11-14: <collection id>.aoss.<region>.on.aws
    "OpenSearch Serverless host (on.aws)": re.compile(r"[a-z0-9]+\.aoss\.[a-z0-9-]+\.on\.aws"),
    "CloudFront host": re.compile(r"[a-z0-9]+\.cloudfront\.net"),
}
# Who ran the capture: the role and session name in an assumed-role ARN (an error message quotes it), a course
# gateway key id, an e-mail address. <role>, gw-XXXXXXXX and addresses at example.com are the stand-ins.
WHO = {
    "role and session name": re.compile(r"assumed-role/(?!<role>)[^\s/]+(?:/[^\s]+)?"),
    "gateway key id": re.compile(r"\bgw-(?!X{8}\b)[A-Za-z0-9]{8,}\b"),
    "e-mail address": re.compile(
        r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
}
# Token shapes: AWS access key ids, bearer values, provider keys, GitHub tokens, JSON web tokens, and the value of
# a secret-looking setting.
TOKENS = {
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b"),
    "bearer value": re.compile(r"(?i)\bbearer\s+(?!<|\$|\{|token\b|key\b|your\b|\.\.\.)[A-Za-z0-9._~+/-]{16,}"),
    "provider key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    "JSON web token": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "secret setting": re.compile(
        r"(?i)\b(?:COURSE_MCP_TOKEN|KB_MCP_TOKEN|[A-Z_]*API_KEY|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)\s*[=:]\s*"
        r"[\"']?(?!<|\$|\{|\.\.\.|\*|testing\b|your\b|set\b|unset\b|not\b|\(|masked\b)[A-Za-z0-9._~+/-]{12,}"),
}


def output_files() -> list[pathlib.Path]:
    return sorted(p for p in ROOT.glob("**/output/**/*")
                  if p.is_file() and ".venv" not in p.parts and "node_modules" not in p.parts)


def findings(text: str) -> list[str]:
    """What a text gives away, one line per hit. Used on every output file, and by the tests of the patterns."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        line = UUID.sub("<uuid>", line.replace(MASKED_ORG_POLICY, "<organization policy>"))
        for m in ACCOUNT_IN_ARN.finditer(line):
            if m.group(1) != MASKED_ACCOUNT:
                found.append(f"line {n}: account number in an ARN")
        for m in ACCOUNT.finditer(line):
            if m.group(0) != MASKED_ACCOUNT:
                found.append(f"line {n}: 12-digit number that reads as an account")
        for what, pattern in HOSTS.items():
            if pattern.search(line):
                found.append(f"line {n}: {what}")
        for what, pattern in {**TOKENS, **WHO}.items():
            if pattern.search(line):
                found.append(f"line {n}: {what}")
    return sorted(set(found))


def test_there_are_outputs_to_check():
    assert len(output_files()) > 20      # the glob still finds them; 16 recipes, several files each


@pytest.mark.parametrize("path", output_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_output_gives_nothing_away(path):
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        pytest.skip("binary file")
    assert not findings(text), f"{path.relative_to(ROOT)}: mask these before committing"


@pytest.mark.parametrize("line, hit", [
    ("arn:aws:iam::123456789012:role/recipe-api-role", True),
    ("arn:aws:iam::111122223333:role/recipe-api-role", False),
    ("bucket recipe-lake-123456789012 created", True),
    ("bucket recipe-lake-111122223333 created", False),
    ("https://abc123def4.execute-api.ca-central-1.amazonaws.com/chat", True),
    ("https://abcdefghijklmnop.lambda-url.ca-central-1.on.aws/", True),
    ("https://<id>.lambda-url.ca-central-1.on.aws/", False),
    ("Authorization: Bearer 4f9c2e7a1b3d5f60718293a4b5c6d7e8", True),
    ("Authorization: Bearer $COURSE_MCP_TOKEN", False),
    ("key AKIAIOSFODNN7EXAMPLE", True),
    ("COURSE_MCP_TOKEN=9a8b7c6d5e4f3a2b1c0d", True),
    ("KB_MCP_TOKEN=9a8b7c6d5e4f3a2b1c0d", True),
    ("set GATEWAY_URL and COURSE_MCP_TOKEN first", False),
    ("at 1794268800000 ms, 12345678901 bytes scanned", False),
    ("at 1794268800000 ms, 123456789012 bytes scanned", True),   # twelve digits are an account wherever they are
    ('"Account": "123456789012",', True),
    ("caller is account 123456789012", True),
    ("Account=123456789012", True),
    ("123456789012.dkr.ecr.ca-central-1.amazonaws.com/x", True),
    ("111122223333.dkr.ecr.ca-central-1.amazonaws.com/x", False),
    ("request 1234567890123 and 12345678901234567890", False),
    ("ratio 0.123456789012 and 123456789012.5 s", False),
    ("2026-11-12T10:15:30.123456789012Z", False),
    ('"RequestId": "4e49cdbd-de0a-4f12-a570-123456789012"', False),
    ("https://abc123xyz.ca-central-1.aoss.amazonaws.com/_dashboards", True),
    ("https://search-recipe-abcdef.ca-central-1.es.amazonaws.com", True),
    ('"Service": "es.amazonaws.com"', False),
    ("https://<collection-id>.ca-central-1.aoss.amazonaws.com", False),
    ("endpoint https://abc123xyz0abc123xyz0.aoss.ca-central-1.on.aws", True),
    ("endpoint https://<collection-id>.aoss.ca-central-1.on.aws", False),
    ("User: arn:aws:sts::111122223333:assumed-role/some-ops-role/botocore-session-1794635510 is not", True),
    ("User: arn:aws:sts::111122223333:assumed-role/<role> is not authorized", False),
    ("total gw-HURQ2HEX  0.001371 CAD", True),
    ("total gw-XXXXXXXX  0.001371 CAD", False),
    ("run gw-admin issue first", False),
    ("alarms go to ana.lee@students.example.ca", True),
    ("export ALARM_EMAIL=ana@example.com", False),
    ("frontend@0.25.2/dist/index.js", False),
    ("https://d1a2b3c4d5e6f7.cloudfront.net/index.html", True),
    ("https://<id>.cloudfront.net/", False),
    ('"id": "4e49cdbd-de0a-4f12-a570-135257603601"', False),
    ("arn:aws:organizations::444455556666:policy/o-exampleorgid/service_control_policy/p-examplepolicy", False),
    ("arn:aws:organizations::444455556666:account/o-real/444455556666", True),
])
def test_the_patterns(line, hit):
    assert bool(findings(line)) is hit
