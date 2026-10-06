import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    CommandSessionInputBody,
    CommandSessionInputResult,
    CommandSessionStdinCloseResult,
)


class FakeCommandStdinWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        if payload["type"] == "command_session_input_request":
            reply = CommandSessionInputResult(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                accepted_bytes=len(str(payload["data"]).encode()),
            )
        else:
            reply = CommandSessionStdinCloseResult(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                closed=True,
            )
        connection.pending[payload["request_id"]].set_result(reply)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_gateway_writes_and_closes_command_stdin_without_logging_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeCommandStdinWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.stdin"]
    gateway.connections["server-01"] = connection
    audits: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway, "audit_required", lambda event, **fields: audits.append((event, fields))
    )
    session_id = "a" * 32
    try:
        written = await gateway.write_command_session_input(
            "server-01",
            session_id,
            CommandSessionInputBody(data="private-input\n"),
            make_settings(),
        )
        closed = await gateway.close_command_session_stdin(
            "server-01", session_id, make_settings()
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert written.accepted_bytes == len(b"private-input\n")
    assert closed.closed is True
    assert audits[0][0] == "command_session_input_requested"
    assert audits[0][1]["input_bytes"] == len(b"private-input\n")
    assert "private-input" not in str(audits)
    assert audits[1][0] == "command_session_stdin_close_requested"


@pytest.mark.asyncio
async def test_gateway_command_stdin_requires_capability() -> None:
    websocket = FakeCommandStdinWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.write_command_session_input(
                "server-01",
                "b" * 32,
                CommandSessionInputBody(data="hello"),
                make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_command_stdin_enforces_byte_limit() -> None:
    websocket = FakeCommandStdinWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.stdin"]
    gateway.connections["server-01"] = connection
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.write_command_session_input(
                "server-01", "c" * 32,
                CommandSessionInputBody(data="ééé"),
                make_settings(session_input_max_bytes=4),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 413
    assert websocket.payloads == []
