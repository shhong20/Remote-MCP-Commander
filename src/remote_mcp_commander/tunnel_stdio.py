"""Private MCP child process for the OpenAI tunnel client, with no HTTP listener."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from remote_mcp_commander.config import Settings
from remote_mcp_commander.mcp_server import build_mcp


def run() -> None:
    # These belong to the parent tunnel client/operator, not to the MCP child.
    for name in (
        "CONTROL_PLANE_API_KEY",
        "OPENAI_API_KEY",
        "COMMANDER_APPROVAL_ADMIN_TOKEN",
        "COMMANDER_AGENT_TOKEN",
        "COMMANDER_AGENT_TOKENS_JSON",
        "COMMANDER_MCP_TOKEN",
        "COMMANDER_AUDIT_REMOTE_TOKEN",
    ):
        os.environ.pop(name, None)
    # Do not accidentally reload a combined .env from the working directory.
    settings = Settings(_env_file=None, mcp_transport="stdio")
    gateway = urlparse(settings.gateway_http)
    if (
        gateway.scheme != "http"
        or gateway.hostname not in {"127.0.0.1", "localhost", "::1"}
        or gateway.username is not None
        or gateway.password is not None
        or gateway.query
        or gateway.fragment
    ):
        raise ValueError("private tunnel MCP requires a clean loopback HTTP Gateway URL")
    build_mcp(settings).run(transport="stdio")


if __name__ == "__main__":
    run()
