import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import FileLineReadBody, FileLineReadResult


class FakeLineReadWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            FileLineReadResult(
                request_id=payload["request_id"],
                path=payload["path"],
                content="line\n",
                total_lines=10,
                start_line=payload["offset"],
                next_line=payload["offset"] + 1,
                eof=False,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_reads_lines_with_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = FakeLineReadWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["file.read_lines"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.read_file_lines(
            "server-01", FileLineReadBody(path="/srv/app.py", offset=4, max_lines=20),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)
    assert result.start_line == 4
    assert websocket.payloads[0]["type"] == "file_line_read_request"


@pytest.mark.asyncio
async def test_gateway_rejects_line_read_without_capability() -> None:
    websocket = FakeLineReadWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.read_file_lines(
                "server-01", FileLineReadBody(path="/srv/app.py"), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
