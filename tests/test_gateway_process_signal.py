import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import ProcessSignalBody, ProcessSignalResult


class FakeSignalWebSocket:
    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            ProcessSignalResult(
                request_id=payload["request_id"],
                pid=payload["pid"],
                signal=payload["signal"],
                signal_sent=True,
                exited=True,
            )
        )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_personal_mode_can_signal_without_external_approval(monkeypatch) -> None:
    connection = gateway.AgentConnection(websocket=FakeSignalWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["process.signal"]
    gateway.connections["server-01"] = connection
    events: list[str] = []
    monkeypatch.setattr(gateway, "audit_required", lambda event, **kwargs: events.append(event))
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.signal_agent_process(
            "server-01",
            ProcessSignalBody(pid=123, expected_create_time_ms=456789, signal="kill"),
            make_settings(operation_mode="personal"),
        )
    finally:
        gateway.connections.pop("server-01", None)
    assert result.signal_sent is True
    assert result.signal == "kill"
    assert "process_signal_personal_approval_bypassed" in events
    assert "process_signal_requested" in events


@pytest.mark.asyncio
async def test_hardened_mode_requires_process_signal_approval() -> None:
    connection = gateway.AgentConnection(websocket=FakeSignalWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["process.signal"]
    gateway.connections["server-01"] = connection
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.signal_agent_process(
                "server-01",
                ProcessSignalBody(pid=123, expected_create_time_ms=456789, signal="term"),
                make_settings(operation_mode="hardened"),
            )
        assert exc_info.value.status_code == 403
    finally:
        gateway.connections.pop("server-01", None)


@pytest.mark.asyncio
async def test_personal_mode_can_restore_process_signal_approval_requirement() -> None:
    connection = gateway.AgentConnection(websocket=FakeSignalWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["process.signal"]
    gateway.connections["server-01"] = connection
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.signal_agent_process(
                "server-01",
                ProcessSignalBody(pid=123, expected_create_time_ms=456789, signal="hup"),
                make_settings(
                    operation_mode="personal",
                    personal_process_approval_required=True,
                ),
            )
        assert exc_info.value.status_code == 403
    finally:
        gateway.connections.pop("server-01", None)
