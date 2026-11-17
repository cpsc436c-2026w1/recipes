"""The settings object: every value a person might change, read once from the environment (README, Decisions).

`.env.example` lists each setting with its default and where the default comes from. No field has a default
here, so a value that is missing stops the run and names the variable. Build the object once, in the script's
`main()`, and pass it to the functions that need it:

    s = load_settings()
    run(s, ...)

Empty is allowed for the secrets, the gateway address and the three role ARNs: an offline run needs none of them,
and the code that needs one checks it where it is used (`s.need("kb_mcp_token")`).
"""
from __future__ import annotations

from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# Field -> the name to set in .env; used by need() and by the error for a missing setting.
ENV_NAMES = {
    "aws_region": "AWS_REGION", "prefix": "PREFIX", "course_gateway_url": "COURSE_GATEWAY_URL",
    "agent_model": "AGENT_MODEL", "max_model_calls": "MAX_MODEL_CALLS", "max_tool_calls": "MAX_TOOL_CALLS",
    "deadline_s": "DEADLINE_S", "reply_max_tokens": "REPLY_MAX_TOKENS",
    "runtime_idle_timeout_s": "RUNTIME_IDLE_TIMEOUT_S", "runtime_max_lifetime_s": "RUNTIME_MAX_LIFETIME_S",
    "lambda_role_arn": "LAMBDA_ROLE_ARN", "gateway_role_arn": "GATEWAY_ROLE_ARN",
    "runtime_role_arn": "RUNTIME_ROLE_ARN", "logfire_enabled": "LOGFIRE_ENABLED",
    "kb_mcp_token": "KB_MCP_TOKEN", "logfire_token": "LOGFIRE_TOKEN",
}
SECRETS = ("kb_mcp_token", "logfire_token")          # never printed, never in the Runtime's configuration
TARGET_NAME = "library-tools"                        # the Gateway names each tool library-tools___<tool>


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    # 1. course-wide
    aws_region: str = Field(min_length=1, validation_alias=AliasChoices("AWS_REGION", "AWS_DEFAULT_REGION"))
    prefix: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]{0,23}$")
    course_gateway_url: str = Field(validation_alias=AliasChoices("COURSE_GATEWAY_URL", "GATEWAY_URL"))
    # 2. this recipe
    agent_model: str = Field(min_length=1)
    max_model_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=0)
    deadline_s: float = Field(gt=0)
    reply_max_tokens: int = Field(ge=1)
    runtime_idle_timeout_s: int = Field(ge=60)
    runtime_max_lifetime_s: int = Field(ge=60)
    lambda_role_arn: str
    gateway_role_arn: str
    runtime_role_arn: str
    logfire_enabled: bool
    # 3. secrets
    kb_mcp_token: str = Field(repr=False, validation_alias=AliasChoices("KB_MCP_TOKEN", "COURSE_MCP_TOKEN"))
    logfire_token: str = Field(repr=False)

    def need(self, field: str) -> str:
        """The value of a setting that may be empty, or a stop that names the variable to set."""
        value = getattr(self, field)
        if not value:
            raise SystemExit(f"Set {ENV_NAMES[field]} in .env (README, Run it)")
        return value

    # Resource names: all built here, all from PREFIX.
    @property
    def books_table(self) -> str:
        return f"{self.prefix}-agent-books"

    @property
    def holds_table(self) -> str:
        return f"{self.prefix}-agent-holds"

    @property
    def tools_function(self) -> str:
        return f"{self.prefix}-agent-tools"

    @property
    def gateway_name(self) -> str:
        return f"{self.prefix}-agent-gateway"

    @property
    def runtime_name(self) -> str:
        """A Runtime's name takes letters, digits and underscores only, so the hyphens of PREFIX become underscores."""
        return f"{self.prefix.replace('-', '_')}_agent"

    @property
    def token_param(self) -> str:
        return f"{self.prefix}-agent-kb-mcp-token"

    @property
    def logfire_param(self) -> str:
        return f"{self.prefix}-agent-logfire-token"

    def code_bucket(self, account: str) -> str:
        return f"{self.prefix}-agent-code-{account}"

    def role_name(self, part: str) -> str:
        """part: lambda, gateway or runtime."""
        return f"{self.prefix}-agent-{part}-role"

    def deployed_env(self, tools_gateway_url: str) -> dict[str, str]:
        """What the Runtime gets as environment variables: every setting that is not a secret, the Gateway's
        address, and the NAMES of the Parameter Store parameters that hold the secrets."""
        env = {name: _text(getattr(self, field)) for field, name in ENV_NAMES.items() if field not in SECRETS}
        return {**env, "TOOLS_GATEWAY_URL": tools_gateway_url, "KB_MCP_TOKEN_PARAM": self.token_param,
                "LOGFIRE_TOKEN_PARAM": self.logfire_param if self.logfire_token else ""}


def _text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def load_settings(**overrides) -> Settings:
    """The settings from the environment. A missing or wrong value ends the run with the variable's name."""
    try:
        return Settings(**overrides)
    except ValidationError as err:
        lines = []
        for e in err.errors():
            field = str(e["loc"][0]) if e["loc"] else "?"
            name = ENV_NAMES.get(field.lower(), field.upper())
            lines.append(f"Set {name} in .env (README, Run it)" if e["type"] == "missing"
                         else f"{name} in .env is not valid: {e['msg']}")
        raise SystemExit("\n".join(lines)) from None
