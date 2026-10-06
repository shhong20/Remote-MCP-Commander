import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import ProcessInfo, ProcessInfoBody, ProcessInfoResult


class FakeProcessInfoWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            ProcessInfoResult(
                request_id=payload["request_id"],
                process=ProcessInfo(
                    pid=payload["pid"],
                    create_time_ms=123456789,
                    name="bash",
                    username="ubuntu",
                    status="sleeping",
                    memory_rss=4096,
                ),
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_reads_one_process_with_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeProcessInfoWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["process.info"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.get_process_info(
            "server-01", ProcessInfoBody(pid=4242), make_settings()
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.process is not None
    assert result.process.pid == 4242
    assert result.process.create_time_ms == 123456789
    assert websocket.payloads[0]["type"] == "process_info_request"
    assert websocket.payloads[0]["pid"] == 4242


@pytest.mark.asyncio
async def test_gateway_rejects_process_info_without_capability() -> None:
    websocket = FakeProcessInfoWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.get_process_info(
                "server-01", ProcessInfoBody(pid=4242), make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)

    assert exc_info.value.status_code == 409
    assert websocket.payloads == []
