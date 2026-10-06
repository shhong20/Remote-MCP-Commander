import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    RuntimeSessionInfo,
    SessionListBody,
    SessionListResult,
)


class FakeSessionListWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            SessionListResult(
                request_id=payload["request_id"],
                sessions=[
                    RuntimeSessionInfo(
                        session_id="a" * 32,
                        kind="command",
                        executable="pytest",
                        pid=4321,
                        create_time_ms=123456789,
                        state="running",
                        output_chars=12,
                    )
                ],
                total_count=1,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_lists_runtime_sessions_with_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeSessionListWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.session_list"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.list_runtime_sessions(
            "server-01",
            SessionListBody(kind="command", include_completed=True, limit=25),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.total_count == 1
    assert result.sessions[0].executable == "pytest"
    assert result.sessions[0].pid == 4321
    assert result.sessions[0].create_time_ms == 123456789
    assert websocket.payloads[0]["type"] == "session_list_request"
    assert websocket.payloads[0]["kind"] == "command"
    assert websocket.payloads[0]["include_completed"] is True
    assert websocket.payloads[0]["limit"] == 25


@pytest.mark.asyncio
async def test_gateway_rejects_session_list_without_capability() -> None:
    websocket = FakeSessionListWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.list_runtime_sessions(
                "server-01", SessionListBody(), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)

    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
