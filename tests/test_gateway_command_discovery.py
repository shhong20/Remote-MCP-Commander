import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import CommandDiscoveryResult


class FakeDiscoveryWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            CommandDiscoveryResult(
                request_id=payload["request_id"],
                operation_mode="personal",
                generic_available=["bash", "git"],
                generic_unavailable=["rg"],
                pty_available=["bash"],
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_command_discovery_requires_capability() -> None:
    websocket = FakeDiscoveryWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.discover_agent_commands("server-01", make_settings())
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_command_discovery_returns_agent_profiles() -> None:
    websocket = FakeDiscoveryWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.discovery"]
    gateway.connections["server-01"] = connection
    try:
        result = await gateway.discover_agent_commands("server-01", make_settings())
    finally:
        gateway.connections.pop("server-01", None)
    assert result.generic_available == ["bash", "git"]
    assert result.pty_available == ["bash"]
    assert websocket.payloads[0]["type"] == "command_discovery_request"
