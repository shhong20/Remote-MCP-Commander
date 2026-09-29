from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.audit import audit
from remote_mcp_commander.gateway.registry import DeviceRegistry
from remote_mcp_commander.protocol import (
    AgentHello,
    AgentInfo,
    AgentList,
    CommandRequest,
    CommandResult,
    EnrollmentClaimBody,
    EnrollmentClaimResult,
    EnrollmentCreateBody,
    EnrollmentTicket,
    ExecuteBody,
    FileReadBody,
    FileReadRequest,
    FileReadResult,
    FileWriteBody,
    FileWriteRequest,
    FileWriteResult,
    PingRequest,
    PingResponse,
    PingResult,
    ProcessListBody,
    ProcessListRequest,
    ProcessListResult,
    RegisteredDevice,
    RegisteredDeviceList,
    RevokeResult,
    ServiceStatusBody,
    ServiceStatusRequest,
    ServiceStatusResult,
)

AgentReply = (
    CommandResult
    | PingResult
    | FileReadResult
    | FileWriteResult
    | ProcessListResult
    | ServiceStatusResult
)


@dataclass
class AgentConnection:
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    hostname: str | None = None
    platform: str | None = None
    version: str | None = None
    pending: dict[str, asyncio.Future[AgentReply]] = field(default_factory=dict)


app = FastAPI(title="Remote MCP Commander Gateway", version="0.3.0")
connections: dict[str, AgentConnection] = {}
SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthorizationHeader = Annotated[str | None, Header()]


@lru_cache
def registry_for(path: str) -> DeviceRegistry:
    return DeviceRegistry(Path(path).expanduser())


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


def bearer_value(authorization: str | None) -> str | None:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        return None
    value = authorization[len(prefix) :]
    return value or None


async def agent_auth_source(
    agent_id: str,
    authorization: str | None,
    settings: Settings,
) -> str | None:
    token = bearer_value(authorization)
    if token is None:
        return None

    registry = registry_for(str(settings.registry_file))
    registered = await registry.get(agent_id)
    if registered is not None:
        if await registry.verify(agent_id, token):
            return "registry"
        return None

    static_token = settings.token_for_agent(agent_id)
    if static_token is not None and secrets.compare_digest(token, static_token):
        return "static"
    return None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/enrollments",
    dependencies=[Depends(require_control_token)],
    status_code=status.HTTP_201_CREATED,
)
async def create_enrollment(body: EnrollmentCreateBody, settings: SettingsDep) -> EnrollmentTicket:
    registry = registry_for(str(settings.registry_file))
    code, expires_at = await registry.create_enrollment(body.agent_id, settings.enrollment_ttl_s)
    audit("enrollment_created", agent_id=body.agent_id, expires_at=expires_at.isoformat())
    return EnrollmentTicket(agent_id=body.agent_id, code=code, expires_at=expires_at)


@app.post("/api/v1/enrollments/claim")
async def claim_enrollment(
    body: EnrollmentClaimBody,
    settings: SettingsDep,
) -> EnrollmentClaimResult:
    registry = registry_for(str(settings.registry_file))
    try:
        token = await registry.claim_enrollment(body.agent_id, body.code)
    except ValueError as exc:
        audit("enrollment_claim_failed", agent_id=body.agent_id)
        raise HTTPException(status_code=401, detail="invalid or expired enrollment code") from exc

    old = connections.get(body.agent_id)
    if old is not None:
        await old.websocket.close(code=4002, reason="device credential rotated")
    audit("enrollment_claimed", agent_id=body.agent_id)
    return EnrollmentClaimResult(agent_id=body.agent_id, agent_token=token)


@app.get(
    "/api/v1/registrations",
    dependencies=[Depends(require_control_token)],
)
async def list_registrations(settings: SettingsDep) -> RegisteredDeviceList:
    registry = registry_for(str(settings.registry_file))
    records = await registry.list_devices()
    devices = [
        RegisteredDevice(
            agent_id=record.agent_id,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
        )
        for record in records
    ]
    return RegisteredDeviceList(devices=devices)


@app.post(
    "/api/v1/agents/{agent_id}/revoke",
    dependencies=[Depends(require_control_token)],
)
async def revoke_agent(agent_id: str, settings: SettingsDep) -> RevokeResult:
    registry = registry_for(str(settings.registry_file))
    if not await registry.revoke(agent_id):
        raise HTTPException(status_code=404, detail="registered device not found")
    connection = connections.get(agent_id)
    if connection is not None:
        await connection.websocket.close(code=4001, reason="device revoked")
    audit("agent_revoked", agent_id=agent_id)
    return RevokeResult(agent_id=agent_id, revoked=True)


@app.get("/api/v1/agents", dependencies=[Depends(require_control_token)])
async def list_agents() -> AgentList:
    agents = [
        agent_info(agent_id, connection) for agent_id, connection in sorted(connections.items())
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


@app.post(
    "/api/v1/agents/{agent_id}/files/read",
    dependencies=[Depends(require_control_token)],
)
async def read_file(agent_id: str, body: FileReadBody, settings: SettingsDep) -> FileReadResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = FileReadRequest(request_id=request_id, **body.model_dump())
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, FileReadResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit("file_read", agent_id=agent_id, path=body.path, rejected=reply.rejected)
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent file read timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/files/write",
    dependencies=[Depends(require_control_token)],
)
async def write_file(agent_id: str, body: FileWriteBody, settings: SettingsDep) -> FileWriteResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = FileWriteRequest(request_id=request_id, **body.model_dump())
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, FileWriteResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "file_write",
            agent_id=agent_id,
            path=body.path,
            bytes_written=reply.bytes_written,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent file write timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/processes",
    dependencies=[Depends(require_control_token)],
)
async def process_list(
    agent_id: str, body: ProcessListBody, settings: SettingsDep
) -> ProcessListResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = ProcessListRequest(request_id=request_id, limit=body.limit)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, ProcessListResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "process_list",
            agent_id=agent_id,
            count=len(reply.processes),
            truncated=reply.truncated,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent process list timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/services/status",
    dependencies=[Depends(require_control_token)],
)
async def get_service_status(
    agent_id: str, body: ServiceStatusBody, settings: SettingsDep
) -> ServiceStatusResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = ServiceStatusRequest(request_id=request_id, unit=body.unit)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, ServiceStatusResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "service_status",
            agent_id=agent_id,
            unit=body.unit,
            active_state=reply.active_state,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent service status timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.websocket("/ws/agent/{agent_id}")
async def agent_socket(websocket: WebSocket, agent_id: str) -> None:
    settings = get_settings()
    auth_source = await agent_auth_source(
        agent_id,
        websocket.headers.get("authorization"),
        settings,
    )
    if auth_source is None:
        audit("agent_auth_failed", agent_id=agent_id)
        await websocket.close(code=4401)
        return

    await websocket.accept()
    old = connections.get(agent_id)
    if old is not None:
        await old.websocket.close(code=4000, reason="replaced by newer agent connection")

    connection = AgentConnection(websocket=websocket)
    connections[agent_id] = connection
    audit("agent_connected", agent_id=agent_id, auth_source=auth_source)

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
            elif message_type == "file_read_result":
                reply = FileReadResult.model_validate(payload)
            elif message_type == "file_write_result":
                reply = FileWriteResult.model_validate(payload)
            elif message_type == "process_list_result":
                reply = ProcessListResult.model_validate(payload)
            elif message_type == "service_status_result":
                reply = ServiceStatusResult.model_validate(payload)
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
