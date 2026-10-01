from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    AgentHello,
    CommandSessionSnapshot,
    PtySessionSnapshot,
    RuntimeSessionInfo,
)


class FakeWebSocket:
    def __init__(self, messages: list[object] | None = None) -> None:
        self.headers = {"authorization": "Bearer test-value"}
        self.messages = list(messages or [])
        self.accepted = False
        self.close_calls: list[tuple[int, str | None]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int, reason: str | None = None) -> None:
        self.close_calls.append((code, reason))

    async def receive_json(self):
        if not self.messages:
            raise WebSocketDisconnect()
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def hello_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "hello",
        "agent_id": "server-01",
        "hostname": "worker-01",
        "platform": "Linux",
        "version": "0.14.0",
    }
    payload.update(overrides)
    return payload


def test_legacy_hello_defaults_to_protocol_v1() -> None:
    hello = AgentHello.model_validate(hello_payload())

    assert hello.protocol_min == 1
    assert hello.protocol_max == 1


def test_hello_rejects_reversed_protocol_range() -> None:
    with pytest.raises(ValidationError, match="protocol_min"):
        AgentHello.model_validate(hello_payload(protocol_min=3, protocol_max=2))


def test_gateway_negotiates_highest_common_protocol() -> None:
    assert gateway.negotiate_protocol(1, 1) == 1
    assert gateway.negotiate_protocol(1, 2) == 1
    assert gateway.negotiate_protocol(2, 2) is None


def test_agent_info_exposes_negotiated_protocol() -> None:
    connection = gateway.AgentConnection(websocket=None)  # type: ignore[arg-type]
    connection.protocol_version = 1
    connection.connected_at = datetime.now(UTC)

    info = gateway.agent_info("server-01", connection)
    assert info.protocol_version == 1


@pytest.mark.asyncio
async def test_incompatible_hello_does_not_replace_existing_agent(monkeypatch) -> None:
    async def allow_auth(*args, **kwargs):
        return "test"

    monkeypatch.setattr(gateway, "agent_auth_source", allow_auth)
    gateway.connections.clear()
    old_socket = FakeWebSocket()
    old_connection = gateway.AgentConnection(websocket=old_socket)  # type: ignore[arg-type]
    gateway.connections["server-01"] = old_connection
    new_socket = FakeWebSocket([hello_payload(protocol_min=2, protocol_max=2)])

    await gateway.agent_socket(new_socket, "server-01")  # type: ignore[arg-type]

    assert new_socket.accepted is True
    assert new_socket.close_calls == [(4406, "incompatible protocol")]
    assert old_socket.close_calls == []
    assert gateway.connections["server-01"] is old_connection
    gateway.connections.clear()


@pytest.mark.asyncio
async def test_non_hello_first_message_does_not_replace_existing_agent(monkeypatch) -> None:
    async def allow_auth(*args, **kwargs):
        return "test"

    monkeypatch.setattr(gateway, "agent_auth_source", allow_auth)
    gateway.connections.clear()
    old_socket = FakeWebSocket()
    old_connection = gateway.AgentConnection(websocket=old_socket)  # type: ignore[arg-type]
    gateway.connections["server-01"] = old_connection
    new_socket = FakeWebSocket([{"type": "heartbeat", "agent_id": "server-01"}])

    await gateway.agent_socket(new_socket, "server-01")  # type: ignore[arg-type]

    assert new_socket.close_calls == [(4400, "hello required")]
    assert old_socket.close_calls == []
    assert gateway.connections["server-01"] is old_connection
    gateway.connections.clear()


@pytest.mark.asyncio
async def test_valid_hello_replaces_existing_agent_only_after_validation(monkeypatch) -> None:
    async def allow_auth(*args, **kwargs):
        return "test"

    monkeypatch.setattr(gateway, "agent_auth_source", allow_auth)
    gateway.connections.clear()
    old_socket = FakeWebSocket()
    old_connection = gateway.AgentConnection(websocket=old_socket)  # type: ignore[arg-type]
    gateway.connections["server-01"] = old_connection
    new_socket = FakeWebSocket([hello_payload(), WebSocketDisconnect()])

    await gateway.agent_socket(new_socket, "server-01")  # type: ignore[arg-type]

    assert old_socket.close_calls == [(4000, "replaced by newer agent connection")]
    assert new_socket.accepted is True
    assert "server-01" not in gateway.connections


def test_session_process_identity_fields_are_backward_compatible() -> None:
    runtime = RuntimeSessionInfo.model_validate(
        {
            "session_id": "a" * 32,
            "kind": "command",
            "executable": "pytest",
            "state": "running",
        }
    )
    command = CommandSessionSnapshot.model_validate(
        {
            "request_id": "legacy",
            "session_id": "b" * 32,
            "state": "completed",
        }
    )
    pty = PtySessionSnapshot.model_validate(
        {
            "request_id": "legacy",
            "session_id": "c" * 32,
            "state": "completed",
        }
    )

    assert runtime.pid is None and runtime.create_time_ms is None
    assert command.pid is None and command.create_time_ms is None
    assert pty.pid is None and pty.create_time_ms is None
