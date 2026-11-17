"""Every environment variable the gateway reads, in one place.

Values are read when `load()` is called, so tests can set the environment first.
"""

import os
from dataclasses import dataclass

# Defaults for a new key, used by the admin CLI and as fallbacks in the app.
DEFAULT_BUDGET_MICRO = {"person": 3_000_000, "team": 20_000_000, "admin": None}
CURRENCY = "USD"  # label printed next to amounts; the prices in pricing.py are in this currency
DEFAULT_EXPIRES_DAYS = 180  # a new key expires this many days after it is issued, unless --expires is given
KEY_PREFIX = "gw"  # a key id is `<prefix>-<8 characters>`, for example gw-ABCDEFGH
IMPORTED_KEY_PREFIX = "im"  # id prefix of a key added with `gw-admin import`, for example im-ABCDEFGH
DEFAULT_MAX_RPM = 30
DEFAULT_MAX_TOKENS_CAP = 16_384  # thinking models spend output tokens reasoning; 8192 cut replies
DEFAULT_MAX_BODY_BYTES = 262_144
DEFAULT_MAX_TOKENS = 4_096  # injected when a chat request carries no max_tokens
TOKENS_PER_MESSAGE = 16  # chat template and special tokens the server adds per message
THRESHOLDS = (50, 80, 100)
MAX_TEXT_ITEM_BYTES = 350_000  # DynamoDB refuses an item over 400 KB, attribute names included


@dataclass(frozen=True)
class Settings:
    table_keys: str
    table_usage: str
    table_texts: str
    text_ttl_days: int
    topic_arn: str
    secret_arn: str
    upstream_base_url: str
    region: str
    function_name: str
    secret_ttl_s: float
    disconnect_grace_s: float
    connect_timeout_s: float
    read_timeout_s: float
    total_timeout_s: float


def load() -> Settings:
    env = os.environ.get
    return Settings(
        table_keys=env("TABLE_KEYS", "llm-gateway-keys"),
        table_usage=env("TABLE_USAGE", "llm-gateway-usage"),
        table_texts=env("TABLE_TEXTS", ""),  # empty: the text log is off
        text_ttl_days=int(env("TEXT_TTL_DAYS", "180")),  # 0 keeps the text until someone deletes it
        topic_arn=env("TOPIC_ARN", ""),
        secret_arn=env("SECRET_ARN", "llm-gateway/upstream-key"),
        upstream_base_url=env("UPSTREAM_BASE_URL", "").rstrip("/"),  # required, no default
        region=env("AWS_REGION", env("AWS_DEFAULT_REGION", "ca-central-1")),
        function_name=env("FUNCTION_NAME", "llm-gateway"),
        secret_ttl_s=float(env("SECRET_TTL_S", "900")),
        disconnect_grace_s=float(env("DISCONNECT_GRACE_S", "5")),
        connect_timeout_s=float(env("UPSTREAM_CONNECT_TIMEOUT_S", "10")),
        read_timeout_s=float(env("UPSTREAM_READ_TIMEOUT_S", "240")),  # a thinking model can take minutes to answer; total stays 285
        total_timeout_s=float(env("UPSTREAM_TOTAL_TIMEOUT_S", "285")),
    )
