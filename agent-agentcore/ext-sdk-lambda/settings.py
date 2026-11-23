"""The settings of ext-sdk-lambda: one frozen object read from the environment (as agent-agentcore's main/config.py).

`ext-sdk-lambda/.env.example` lists each setting with its default and where the default comes from. No field has a
default here, so a missing value stops the run and names the variable. In Lambda the deploy step sets them as the
function's environment variables, except the token: the function gets the NAME of its Parameter Store parameter
(`KB_MCP_TOKEN_PARAM`) and reads the value once per execution environment (handler.py).
"""
from __future__ import annotations

from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_NAMES = {
    "aws_region": "AWS_REGION", "prefix": "PREFIX", "course_gateway_url": "COURSE_GATEWAY_URL",
    "agent_sdk": "AGENT_SDK", "agent_model": "AGENT_MODEL", "max_model_calls": "MAX_MODEL_CALLS",
    "max_tool_calls": "MAX_TOOL_CALLS", "deadline_s": "DEADLINE_S", "call_timeout_s": "CALL_TIMEOUT_S",
    "reply_max_tokens": "REPLY_MAX_TOKENS", "trips_path": "TRIPS_PATH", "kb_mcp_token_param": "KB_MCP_TOKEN_PARAM",
    "kb_mcp_token": "KB_MCP_TOKEN",
}
SECRETS = ("kb_mcp_token",)                 # never printed, never in the function's configuration
SDKS = ("strands", "pydantic-ai")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    aws_region: str = Field(min_length=1, validation_alias=AliasChoices("AWS_REGION", "AWS_DEFAULT_REGION"))
    prefix: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]{0,23}$")
    course_gateway_url: str = Field(validation_alias=AliasChoices("COURSE_GATEWAY_URL", "GATEWAY_URL"))
    agent_sdk: str = Field(pattern=r"^(strands|pydantic-ai)$")
    agent_model: str = Field(min_length=1)
    max_model_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=0)
    deadline_s: float = Field(gt=0)
    call_timeout_s: float = Field(gt=0)
    reply_max_tokens: int = Field(ge=1)
    trips_path: str = Field(min_length=1)
    kb_mcp_token_param: str
    kb_mcp_token: str = Field(repr=False, validation_alias=AliasChoices("KB_MCP_TOKEN", "COURSE_MCP_TOKEN"))

    def need(self, field: str) -> str:
        value = getattr(self, field)
        if not value:
            raise SystemExit(f"Set {ENV_NAMES[field]} (ext-sdk-lambda/README.md, Run it live)")
        return value

    @property
    def function_name(self) -> str:
        return f"{self.prefix}-agent-sdk"

    @property
    def role_name(self) -> str:
        return f"{self.prefix}-agent-sdk-role"

    @property
    def token_param(self) -> str:
        return f"{self.prefix}-agent-sdk-token"

    @property
    def log_group(self) -> str:
        return f"/aws/lambda/{self.function_name}"

    def deployed_env(self) -> dict[str, str]:
        """The function's environment variables: every setting but the token, and the token parameter's name."""
        env = {name: str(getattr(self, field)) for field, name in ENV_NAMES.items()
               if field not in SECRETS and name != "AWS_REGION"}      # Lambda sets AWS_REGION itself (reserved)
        return {**env, "TRIPS_PATH": "trips.parquet", "KB_MCP_TOKEN_PARAM": self.token_param,
                "PYDANTIC_AI_NO_BANNER": "1"}


def load_settings(**overrides) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as err:
        lines = []
        for e in err.errors():
            field = str(e["loc"][0]) if e["loc"] else "?"
            name = ENV_NAMES.get(field.lower(), field.upper())
            lines.append(f"Set {name} (ext-sdk-lambda/.env.example)" if e["type"] == "missing"
                         else f"{name} is not valid: {e['msg']}")
        raise SystemExit("\n".join(lines)) from None
