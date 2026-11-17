"""AgentCore Runtime entry point: the bounded agent of main/agent.py, with its tools behind the Gateway.

The Runtime's environment variables hold no secret. They hold the NAMES of the Parameter Store parameters; the
values are read here, once per microVM, with the Runtime's execution role. Payloads: main/session.py.
"""
import logging
import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
_sessions = None


def read_secrets() -> None:
    """Parameter Store SecureString values into this process's environment, where the settings object reads them."""
    ssm = boto3.client("ssm", region_name=os.environ["AWS_REGION"])
    for name, param in (("KB_MCP_TOKEN", os.environ["KB_MCP_TOKEN_PARAM"]),
                        ("LOGFIRE_TOKEN", os.environ["LOGFIRE_TOKEN_PARAM"])):
        os.environ[name] = (ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"].strip()
                            if param else "")


def sessions():
    """Once per microVM: the secrets, the settings, the Gateway connection, the model."""
    global _sessions
    if _sessions is None:
        from main import tracing
        from main.config import load_settings
        from main.gateway_tools import GatewayLibrary, open_gateway
        from main.model import gateway_model
        from main.session import Sessions

        read_secrets()
        s = load_settings()
        logging.getLogger("strands").setLevel(logging.ERROR)
        traced = tracing.configure(s)
        library = GatewayLibrary(open_gateway(os.environ["TOOLS_GATEWAY_URL"], s.aws_region))
        _sessions = Sessions(s, gateway_model(s), library, tools=library.tools, traced=traced)
    return _sessions


@app.entrypoint
def invoke(payload, context):
    return sessions().handle(dict(payload or {}), getattr(context, "session_id", None) or "no-session")


if __name__ == "__main__":
    app.run()
