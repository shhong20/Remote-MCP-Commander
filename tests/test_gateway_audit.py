import json

import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import CommandResult, ExecuteBody


class FakeWebSocket:
    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        connection = gateway.connections["server-01"]
        future = connection.pending[payload["request_id"]]
        future.set_result(
            CommandResult(request_id=payload["request_id"], returncode=0, stdout="ok")
        )


@pytest.mark.asyncio
async def test_command_audit_does_not_log_argument_values(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(gateway, "audit", lambda event, **fields: events.append((event, fields)))
    gateway.connections["server-01"] = gateway.AgentConnection(websocket=FakeWebSocket())
    settings = Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )
    try:
        await gateway.execute(
            "server-01",
            ExecuteBody(argv=["echo", "sensitive-argument"]),
            settings,
        )
    finally:
        gateway.connections.pop("server-01", None)

    requested = next(fields for event, fields in events if event == "command_requested")
    assert requested["executable"] == "echo"
    assert requested["argc"] == 2
    assert "argv" not in requested
    assert "sensitive-argument" not in str(requested)
