import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    CommandResult,
    CommandSessionSnapshot,
    CommandSessionStartBody,
    ExecuteBody,
)


class FakeTimeoutWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        if payload["type"] == "command_request":
            reply = CommandResult(request_id=payload["request_id"], returncode=0)
        else:
            reply = CommandSessionSnapshot(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                executable="echo",
                state="running",
                timeout_s=payload["timeout_s"],
            )
        connection.pending[payload["request_id"]].set_result(reply)


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_dispatches_timeout_override_with_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeTimeoutWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.timeout"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        one_shot = await gateway.execute(
            "server-01", ExecuteBody(argv=["echo", "ok"], timeout_s=30.0), make_settings()
        )
        session = await gateway.start_command_session(
            "server-01",
            CommandSessionStartBody(argv=["echo", "ok"], timeout_s=900.0),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert one_shot.returncode == 0
    assert session.timeout_s == 900.0
    assert websocket.payloads[0]["timeout_s"] == 30.0
    assert websocket.payloads[1]["timeout_s"] == 900.0


@pytest.mark.asyncio
async def test_gateway_rejects_timeout_override_without_capability() -> None:
    websocket = FakeTimeoutWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.execute(
                "server-01", ExecuteBody(argv=["echo", "ok"], timeout_s=30.0), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
