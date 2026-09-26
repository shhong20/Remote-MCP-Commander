from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.audit import audit
from remote_mcp_commander.protocol import (
    AgentHello,
    AgentInfo,
    AgentList,
    CommandRequest,
    CommandResult,
    ExecuteBody,
    PingRequest,
    PingResponse,
    PingResult,
)

AgentReply = CommandResult | PingResult


@dataclass
class AgentConnection:
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    hostname: str | None = None
    platform: str | None = None
    version: str | None = None
    pending: dict[str, asyncio.Future[AgentReply]] = field(default_factory=dict)


app = FastAPI(title="Remote MCP Commander Gateway", version="0.2.0")
connections: dict[str, AgentConnection] = {}
SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthorizationHeader = Annotated[str | None, Header()]


def require_control_token(
    settings: SettingsDep,
    authorization: AuthorizationHeader = None,
) -> None:
    expected = f"Bearer {settings.control_token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid control token",
        )


def enforce_agent_policy(agent_id: str, argv: list[str], settings: Settings) -> None:
    policy = settings.agent_policies.get(agent_id)
    if policy is None:
        return
    executable = Path(argv[0]).name
    if executable not in policy:
        audit("command_denied", agent_id=agent_id, executable=executable)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"gateway policy denied executable: {executable}",
        )


def agent_info(agent_id: str, connection: AgentConnection) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        hostname=connection.hostname,
        platform=connection.platform,
        version=connection.version,
        connected_at=connection.connected_at,
        last_seen=connection.last_seen,
    )


def pending_request(connection: AgentConnection, request_id: str) -> asyncio.Future[AgentReply]:
    future: asyncio.Future[AgentReply] = asyncio.get_running_loop().create_future()
    connection.pending[request_id] = future
    return future


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/agents", dependencies=[Depends(require_control_token)])
async def list_agents() -> AgentList:
    agents = [
        agent_info(agent_id, connection)
        for agent_id, connection in sorted(connections.items())
    ]
    return AgentList(agents=agents)


@app.get(
    "/api/v1/agents/{agent_id}",
    dependencies=[Depends(require_control_token)],
)
async def get_agent(agent_id: str) -> AgentInfo:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    return agent_info(agent_id, connection)


@app.post(
    "/api/v1/agents/{agent_id}/execute",
    dependencies=[Depends(require_control_token)],
)
async def execute(agent_id: str, body: ExecuteBody, settings: SettingsDep) -> CommandResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")

    enforce_agent_policy(agent_id, body.argv, settings)
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    audit(
        "command_requested",
        agent_id=agent_id,
        request_id=request_id,
        executable=Path(body.argv[0]).name,
        argc=len(body.argv),
    )

    try:
        request = CommandRequest(request_id=request_id, argv=body.argv)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "command_completed",
            agent_id=agent_id,
            request_id=request_id,
            returncode=reply.returncode,
            rejected=reply.rejected,
            timed_out=reply.timed_out,
        )
        return reply
    except TimeoutError as exc:
        audit("command_timeout", agent_id=agent_id, request_id=request_id)
        raise HTTPException(status_code=504, detail="agent request timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/ping",
    dependencies=[Depends(require_control_token)],
)
async def ping_agent(agent_id: str, settings: SettingsDep) -> PingResponse:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")

    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    started = perf_counter()
    try:
        request = PingRequest(request_id=request_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PingResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        round_trip_ms = round((perf_counter() - started) * 1000, 3)
        audit("agent_ping", agent_id=agent_id, round_trip_ms=round_trip_ms)
        return PingResponse(
            agent_id=agent_id,
            round_trip_ms=round_trip_ms,
            last_seen=connection.last_seen,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent ping timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.websocket("/ws/agent/{agent_id}")
async def agent_socket(websocket: WebSocket, agent_id: str) -> None:
    settings = get_settings()
    token = websocket.headers.get("authorization")
    expected = f"Bearer {settings.token_for_agent(agent_id)}"
    if token is None or not secrets.compare_digest(token, expected):
        audit("agent_auth_failed", agent_id=agent_id)
        await websocket.close(code=4401)
        return

    await websocket.accept()
    old = connections.get(agent_id)
    if old is not None:
        await old.websocket.close(code=4000, reason="replaced by newer agent connection")

    connection = AgentConnection(websocket=websocket)
    connections[agent_id] = connection
    audit("agent_connected", agent_id=agent_id)

    try:
        while True:
            payload = await websocket.receive_json()
            connection.last_seen = datetime.now(UTC)
            message_type = payload.get("type")

            if message_type == "hello":
                hello = AgentHello.model_validate(payload)
                if hello.agent_id != agent_id:
                    await websocket.close(code=4403, reason="agent identity mismatch")
                    return
                connection.hostname = hello.hostname
                connection.platform = hello.platform
                connection.version = hello.version
                audit("agent_hello", agent_id=agent_id, hostname=hello.hostname)
                continue

            if message_type == "heartbeat":
                continue

            if message_type == "command_result":
                reply: AgentReply = CommandResult.model_validate(payload)
            elif message_type == "ping_result":
                reply = PingResult.model_validate(payload)
            else:
                continue

            future = connection.pending.get(reply.request_id)
            if future is not None and not future.done():
                future.set_result(reply)
    except WebSocketDisconnect:
        pass
    finally:
        if connections.get(agent_id) is connection:
            connections.pop(agent_id, None)
        for future in connection.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent disconnected"))
        audit("agent_disconnected", agent_id=agent_id)


def run() -> None:
    settings = get_settings()
    settings.validate_gateway_security()
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
