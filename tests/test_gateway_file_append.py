import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import FileAppendBody, FileAppendResult


class FakeAppendWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            FileAppendResult(
                request_id=payload["request_id"],
                path=payload["path"],
                bytes_appended=len(str(payload["content"]).encode()),
                size=12,
                sha256="1" * 64,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_appends_with_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = FakeAppendWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["file.append"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit_required", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.append_file(
            "server-01", FileAppendBody(path="/srv/note", content="hello"), make_settings()
        )
    finally:
        gateway.connections.pop("server-01", None)
    assert result.bytes_appended == 5
    assert websocket.payloads[0]["type"] == "file_append_request"


@pytest.mark.asyncio
async def test_gateway_rejects_append_without_capability() -> None:
    websocket = FakeAppendWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.append_file(
                "server-01", FileAppendBody(path="/srv/note", content="hello"), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
