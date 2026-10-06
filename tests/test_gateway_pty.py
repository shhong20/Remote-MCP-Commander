import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    PtySessionInputBody,
    PtySessionInputResult,
    PtySessionSnapshot,
    PtySessionStartBody,
)


class FakePtyWebSocket:
    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            PtySessionSnapshot(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                executable=payload["argv"][0],
                state="running",
                columns=payload["columns"],
                rows=payload["rows"],
            )
        )


class FakePtyInputWebSocket:
    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            PtySessionInputResult(
                request_id=payload["request_id"],
                session_id=payload["session_id"],
                accepted_bytes=len(payload["data"].encode()),
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
        pty_agent_policies_json='{"server-01":["bash"]}',
    )


@pytest.mark.asyncio
async def test_pty_start_consumes_argv_bound_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway.approval_store.cache_clear()
    store = gateway.approval_store()
    target = gateway.pty_approval_target(["bash", "-l"])
    grant, secret = await store.issue(
        agent_id="server-01",
        operation="pty.start",
        target=target,
        ttl_s=60,
    )
    connection = gateway.AgentConnection(websocket=FakePtyWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["command.pty"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(gateway, "audit_required", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        result = await gateway.start_pty_session(
            "server-01",
            PtySessionStartBody(
                argv=["bash", "-l"],
                approval_id=grant.approval_id,
                approval_secret=secret,
                columns=100,
                rows=30,
            ),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)
        gateway.approval_store.cache_clear()

    assert result.state == "running"
    assert result.executable == "bash"
    assert (result.columns, result.rows) == (100, 30)
    with pytest.raises(gateway.ApprovalError, match="already consumed"):
        await store.consume(
            approval_id=grant.approval_id,
            secret=secret,
            agent_id="server-01",
            operation="pty.start",
            target=target,
        )


@pytest.mark.asyncio
async def test_pty_start_rejects_missing_capability_without_burning_approval() -> None:
    gateway.approval_store.cache_clear()
    store = gateway.approval_store()
    target = gateway.pty_approval_target(["bash"])
    grant, secret = await store.issue(
        agent_id="server-01",
        operation="pty.start",
        target=target,
        ttl_s=60,
    )
    gateway.connections["server-01"] = gateway.AgentConnection(
        websocket=FakePtyWebSocket()  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.start_pty_session(
                "server-01",
                PtySessionStartBody(
                    argv=["bash"],
                    approval_id=grant.approval_id,
                    approval_secret=secret,
                ),
                make_settings(),
            )
        assert exc_info.value.status_code == 409
        consumed = await store.consume(
            approval_id=grant.approval_id,
            secret=secret,
            agent_id="server-01",
            operation="pty.start",
            target=target,
        )
        assert consumed.approval_id == grant.approval_id
    finally:
        gateway.connections.pop("server-01", None)
        gateway.approval_store.cache_clear()


@pytest.mark.asyncio
async def test_pty_input_audit_records_size_not_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    connection = gateway.AgentConnection(websocket=FakePtyInputWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["command.pty"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: events.append((event, fields)),
    )
    try:
        result = await gateway.write_pty_input(
            "server-01",
            "a" * 32,
            PtySessionInputBody(data="secret-input\n"),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.accepted_bytes == len(b"secret-input\n")
    event, fields = events[0]
    assert event == "pty_session_input_requested"
    assert fields["input_bytes"] == len(b"secret-input\n")
    assert "secret-input" not in str(fields)


@pytest.mark.asyncio
async def test_personal_mode_can_start_pty_without_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = gateway.AgentConnection(websocket=FakePtyWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["command.pty"]
    gateway.connections["server-01"] = connection
    events: list[str] = []
    monkeypatch.setattr(gateway, "audit_required", lambda event, **kwargs: events.append(event))
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    settings = Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
        operation_mode="personal",
        pty_agent_policies_json='{"server-01":["bash"]}',
    )
    try:
        result = await gateway.start_pty_session(
            "server-01", PtySessionStartBody(argv=["bash"]), settings
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.state == "running"
    assert "pty_personal_approval_bypassed" in events
    assert "approval_consumed" not in events


@pytest.mark.asyncio
async def test_personal_mode_can_require_pty_approval() -> None:
    connection = gateway.AgentConnection(websocket=FakePtyWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["command.pty"]
    gateway.connections["server-01"] = connection
    settings = Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
        operation_mode="personal",
        personal_pty_approval_required=True,
        pty_agent_policies_json='{"server-01":["bash"]}',
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.start_pty_session(
                "server-01", PtySessionStartBody(argv=["bash"]), settings
            )
        assert exc_info.value.status_code == 403
        assert "approval is required" in str(exc_info.value.detail)
    finally:
        gateway.connections.pop("server-01", None)


@pytest.mark.asyncio
async def test_pty_rejects_partial_approval_fields() -> None:
    connection = gateway.AgentConnection(websocket=FakePtyWebSocket())  # type: ignore[arg-type]
    connection.capabilities = ["command.pty"]
    gateway.connections["server-01"] = connection
    settings = Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
        operation_mode="personal",
        pty_agent_policies_json='{"server-01":["bash"]}',
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.start_pty_session(
                "server-01",
                PtySessionStartBody(argv=["bash"], approval_id="approval-123"),
                settings,
            )
        assert exc_info.value.status_code == 422
    finally:
        gateway.connections.pop("server-01", None)
