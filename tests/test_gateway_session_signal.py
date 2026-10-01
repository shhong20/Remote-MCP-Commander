import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import SessionSignalBody, SessionSignalResult


class FakeSessionSignalWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            SessionSignalResult(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                kind=payload["kind"],
                signal=payload["signal"],
                pid=123,
                create_time_ms=456789,
                signal_sent=True,
            )
        )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
        "operation_mode": "personal",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_gateway_signals_managed_session_in_personal_mode(monkeypatch) -> None:
    websocket = FakeSessionSignalWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.session_signal"]
    gateway.connections["server-01"] = connection
    events: list[str] = []
    monkeypatch.setattr(gateway, "audit_required", lambda event, **kwargs: events.append(event))
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    session_id = "ab" * 16
    try:
        result = await gateway.signal_runtime_session(
            "server-01", session_id,
            SessionSignalBody(kind="command", signal="int"), make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)
    assert result.signal_sent is True
    assert websocket.payloads[0]["type"] == "session_signal_request"
    assert websocket.payloads[0]["session_id"] == session_id
    assert websocket.payloads[0]["kind"] == "command"
    assert websocket.payloads[0]["signal"] == "int"
    assert "session_signal_requested" in events


@pytest.mark.asyncio
async def test_gateway_rejects_session_signal_without_capability() -> None:
    websocket = FakeSessionSignalWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.signal_runtime_session(
                "server-01", "cd" * 16,
                SessionSignalBody(kind="pty", signal="term"), make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_rejects_session_signal_when_approval_is_required() -> None:
    websocket = FakeSessionSignalWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.session_signal"]
    gateway.connections["server-01"] = connection
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.signal_runtime_session(
                "server-01", "ef" * 16,
                SessionSignalBody(kind="command", signal="hup"),
                make_settings(personal_process_approval_required=True),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 403
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_session_signal_audit_failure_does_not_leave_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeSessionSignalWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["command.session_signal"]
    gateway.connections["server-01"] = connection

    def fail_audit(*args, **kwargs) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(gateway, "audit_required", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await gateway.signal_runtime_session(
                "server-01", "aa" * 16,
                SessionSignalBody(kind="command", signal="term"), make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert connection.pending == {}
    assert websocket.payloads == []
