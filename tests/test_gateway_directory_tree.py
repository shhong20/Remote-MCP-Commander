import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import DirectoryTreeBody, DirectoryTreeResult


class FakeTreeWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            DirectoryTreeResult(
                request_id=payload["request_id"],
                path=payload["path"],
                scanned_directories=1,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_lists_directory_tree_with_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeTreeWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["filesystem.tree_list"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.list_agent_directory_tree(
            "server-01",
            DirectoryTreeBody(path="/srv/project", depth=3, max_entries=250),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.scanned_directories == 1
    assert websocket.payloads[0]["type"] == "directory_tree_request"
    assert websocket.payloads[0]["depth"] == 3
    assert websocket.payloads[0]["max_entries"] == 250


@pytest.mark.asyncio
async def test_gateway_rejects_directory_tree_without_capability() -> None:
    websocket = FakeTreeWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.list_agent_directory_tree(
                "server-01", DirectoryTreeBody(path="/srv/project"), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)

    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
