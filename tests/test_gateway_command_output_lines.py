import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    CommandSessionLineOutput,
    CommandSessionLineOutputBody,
)


class FakeLineOutputWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        reply = CommandSessionLineOutput(
            request_id=str(payload["request_id"]),
            session_id=str(payload["session_id"]),
            state="running",
            stream=str(payload["stream"]),
            content="line\n",
            total_lines=1,
            next_line=1,
        )
        connection.pending[str(payload["request_id"])].set_result(reply)


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_dispatches_command_line_output_with_capability() -> None:
    websocket = FakeLineOutputWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.output_lines"]
    gateway.connections["server-01"] = connection
    session_id = "ab" * 16
    try:
        result = await gateway.get_command_session_output_lines(
            "server-01",
            session_id,
            CommandSessionLineOutputBody(stream="stderr", offset=-10, max_lines=20),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.content == "line\n"
    assert websocket.payloads[0]["type"] == "command_session_line_output_request"
    assert websocket.payloads[0]["stream"] == "stderr"
    assert websocket.payloads[0]["offset"] == -10
    assert websocket.payloads[0]["max_lines"] == 20


@pytest.mark.asyncio
async def test_gateway_rejects_command_line_output_without_capability() -> None:
    websocket = FakeLineOutputWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    session_id = "cd" * 16
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.get_command_session_output_lines(
                "server-01",
                session_id,
                CommandSessionLineOutputBody(),
                make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)

    assert exc_info.value.status_code == 409
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_requires_wait_capability_only_for_long_poll() -> None:
    websocket = FakeLineOutputWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.output_lines"]
    gateway.connections["server-01"] = connection
    session_id = "de" * 16
    try:
        immediate = await gateway.get_command_session_output_lines(
            "server-01",
            session_id,
            CommandSessionLineOutputBody(wait_ms=0),
            make_settings(),
        )
        with pytest.raises(HTTPException) as exc_info:
            await gateway.get_command_session_output_lines(
                "server-01",
                session_id,
                CommandSessionLineOutputBody(wait_ms=250),
                make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)

    assert immediate.content == "line\n"
    assert exc_info.value.status_code == 409
    assert len(websocket.payloads) == 1

@pytest.mark.asyncio
async def test_gateway_dispatches_command_line_wait_when_supported() -> None:
    websocket = FakeLineOutputWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.output_lines", "command.output_wait"]
    gateway.connections["server-01"] = connection
    session_id = "df" * 16
    try:
        result = await gateway.get_command_session_output_lines(
            "server-01",
            session_id,
            CommandSessionLineOutputBody(wait_ms=250),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.content == "line\n"
    assert websocket.payloads[0]["wait_ms"] == 250