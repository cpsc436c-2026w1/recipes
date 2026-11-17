"""Names of everything a run creates, in one place.

Every name starts with $PREFIX (default "recipe") so that teammates sharing one account do not collide and
clean_up.py can find all of it (README, Decisions: "names").
"""
import os

REGION = "ca-central-1"          # course constraint: every recipe runs in ca-central-1
TAG = {"Key": "recipe", "Value": "project-skeleton"}   # clean_up.py deletes only IAM roles that carry this tag


def prefix() -> str:
    p = os.environ.get("PREFIX", "recipe").lower()
    if not p.replace("-", "").isalnum() or len(p) > 30:
        raise SystemExit(f"PREFIX={p!r}: use letters, digits and hyphens, at most 30 characters")
    return p


def names(p: str | None = None, account: str = "111122223333") -> dict:
    """All resource names for one prefix. The bucket name carries the account id because S3 names are global."""
    p = p or prefix()
    db = p.replace("-", "_") + "_lake"          # Glue and Athena names: underscores, not hyphens
    return {
        "stream": f"{p}-updates",               # Kinesis data stream (lakehouse first)
        "firehose": f"{p}-to-lake",             # Firehose stream: Kinesis -> S3 as Parquet
        "firehose_role": f"{p}-firehose-role",
        "bucket": f"{p}-lake-{account}",        # the lake, the DynamoDB exports and Athena's results
        "database": db,                         # Glue Data Catalog database
        "workgroup": f"{p}-wg",                 # Athena workgroup with a per-query scan limit
        "table": f"{p}-updates",                # DynamoDB table (operational first, generative AI first)
        "function": f"{p}-api",                 # Lambda function behind the function URL
        "function_role": f"{p}-api-role",
        "log_group": f"/aws/lambda/{p}-api",
    }


def session():
    """A boto3 session in the course region. Credentials come from your profile or CloudShell, never from code."""
    import boto3
    return boto3.Session(region_name=REGION)


def account_id(sess) -> str:
    return sess.client("sts").get_caller_identity()["Account"]
