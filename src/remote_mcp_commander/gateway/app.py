from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Annotated

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from remote_mcp_commander import __version__
from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.approvals import ApprovalError, ApprovalStore
from remote_mcp_commander.gateway.audit import (
    audit,
    audit_required,
    configure_audit,
    current_journal,
)
from remote_mcp_commander.gateway.registry import DeviceRegistry
from remote_mcp_commander.policy import (
    pty_approval_target,
    validate_generic_argv,
    validate_pty_argv,
)
from remote_mcp_commander.protocol import (
    PROTOCOL_MAX_SUPPORTED,
    PROTOCOL_MIN_SUPPORTED,
    SESSION_ID_PATTERN,
    AgentHello,
    AgentInfo,
    AgentList,
    ApprovalCreateBody,
    ApprovalTicket,
    AuditQueryResult,
    CommandRequest,
    CommandResult,
    CommandSessionCancelRequest,
    CommandSessionDiscardRequest,
    CommandSessionDiscardResult,
    CommandSessionOutput,
    CommandSessionOutputBody,
    CommandSessionOutputRequest,
    CommandSessionSnapshot,
    CommandSessionStartBody,
    CommandSessionStartRequest,
    CommandSessionStatusRequest,
    DirectoryListBody,
    DirectoryListRequest,
    DirectoryListResult,
    EnrollmentClaimBody,
    EnrollmentClaimResult,
    EnrollmentCreateBody,
    EnrollmentTicket,
    ExecuteBody,
    FileInfoBody,
    FileInfoRequest,
    FileInfoResult,
    FileReadBody,
    FileReadRequest,
    FileReadResult,
    FileRootListRequest,
    FileRootListResult,
    FileWriteBody,
    FileWriteRequest,
    FileWriteResult,
    GitStatusBody,
    GitStatusRequest,
    GitStatusResult,
    Heartbeat,
    PingRequest,
    PingResponse,
    PingResult,
    PortLookupBody,
    PortLookupRequest,
    PortLookupResult,
    ProcessListBody,
    ProcessListRequest,
    ProcessListResult,
    ProcessTerminateBody,
    ProcessTerminateRequest,
    ProcessTerminateResult,
    PtySessionCancelRequest,
    PtySessionDiscardRequest,
    PtySessionDiscardResult,
    PtySessionInputBody,
    PtySessionInputRequest,
    PtySessionInputResult,
    PtySessionOutput,
    PtySessionOutputBody,
    PtySessionOutputRequest,
    PtySessionResizeBody,
    PtySessionResizeRequest,
    PtySessionResizeResult,
    PtySessionSnapshot,
    PtySessionStartBody,
    PtySessionStartRequest,
    PtySessionStatusRequest,
    RegisteredDevice,
    RegisteredDeviceList,
    RevokeResult,
    ServiceActionBody,
    ServiceActionRequest,
    ServiceActionResult,
    ServiceLogsBody,
    ServiceLogsRequest,
    ServiceLogsResult,
    ServiceStatusBody,
    ServiceStatusRequest,
    ServiceStatusResult,
    SystemHealthRequest,
    SystemHealthResult,
)

AgentReply = (
    CommandResult
    | PingResult
    | FileReadResult
    | FileRootListResult
    | DirectoryListResult
    | FileInfoResult
    | FileWriteResult
    | ProcessListResult
    | ServiceStatusResult
    | ProcessTerminateResult
    | ServiceActionResult
    | CommandSessionSnapshot
    | CommandSessionOutput
    | CommandSessionDiscardResult
    | PtySessionSnapshot
    | PtySessionOutput
    | PtySessionInputResult
    | PtySessionResizeResult
    | PtySessionDiscardResult
    | SystemHealthResult
    | PortLookupResult
    | ServiceLogsResult
    | GitStatusResult
)


@dataclass
class AgentConnection:
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    hostname: str | None = None
    platform: str | None = None
    version: str | None = None
    protocol_version: int | None = None
    capabilities: list[str] = field(default_factory=list)
    pending: dict[str, asyncio.Future[AgentReply]] = field(default_factory=dict)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_audit(settings.audit_file, fsync=settings.audit_fsync)
    yield


app = FastAPI(
    title="Remote MCP Commander Gateway",
    version=__version__,
    lifespan=lifespan,
)
connections: dict[str, AgentConnection] = {}
SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthorizationHeader = Annotated[str | None, Header()]


@lru_cache
def registry_for(path: str) -> DeviceRegistry:
    return DeviceRegistry(Path(path).expanduser())


@lru_cache
def approval_store() -> ApprovalStore:
    return ApprovalStore()


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


def require_approval_admin_token(
    settings: SettingsDep,
    authorization: AuthorizationHeader = None,
) -> None:
    if not settings.approval_admin_token:
        raise HTTPException(status_code=503, detail="mutation approvals are not configured")
    expected = f"Bearer {settings.approval_admin_token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid approval token"
        )


def process_approval_target(pid: int, create_time_ms: int) -> str:
    return f"pid:{pid}@{create_time_ms}"


def service_approval_operation(action: str) -> str:
    return f"service.{action}"


PROCESS_APPROVAL_TARGET_RE = re.compile(r"^pid:(?:[2-9]|[1-9][0-9]+)@[1-9][0-9]*$")
SERVICE_APPROVAL_TARGET_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,256}$")
PTY_APPROVAL_TARGET_RE = re.compile(r"^argv-sha256:[a-f0-9]{64}$")
COMMAND_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


def validate_command_session_id(session_id: str) -> None:
    if COMMAND_SESSION_ID_RE.fullmatch(session_id) is None:
        raise HTTPException(status_code=422, detail="invalid command session id")


def validate_approval_target(operation: str, target: str) -> None:
    if operation == "process.terminate":
        valid = PROCESS_APPROVAL_TARGET_RE.fullmatch(target) is not None
    elif operation == "pty.start":
        valid = PTY_APPROVAL_TARGET_RE.fullmatch(target) is not None
    else:
        valid = SERVICE_APPROVAL_TARGET_RE.fullmatch(target) is not None
    if not valid:
        raise HTTPException(status_code=422, detail="invalid approval target for operation")


def enforce_agent_policy(agent_id: str, argv: list[str], settings: Settings) -> None:
    generic_error = validate_generic_argv(argv)
    if generic_error is not None:
        audit("command_denied", agent_id=agent_id, executable=Path(argv[0]).name)
        raise HTTPException(status_code=403, detail=generic_error)
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


def enforce_pty_policy(agent_id: str, argv: list[str], settings: Settings) -> None:
    policy_error = validate_pty_argv(argv)
    executable = Path(argv[0]).name if argv else ""
    if policy_error is not None:
        audit("pty_session_denied", agent_id=agent_id, executable=executable)
        raise HTTPException(status_code=403, detail=policy_error)
    policy = settings.pty_agent_policies.get(agent_id)
    if policy is None or executable not in policy:
        audit("pty_session_denied", agent_id=agent_id, executable=executable)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"gateway PTY policy denied executable: {executable}",
        )


def require_agent_capability(connection: AgentConnection, capability: str) -> None:
    if capability not in connection.capabilities:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"agent does not advertise capability: {capability}",
        )


def agent_info(agent_id: str, connection: AgentConnection) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        hostname=connection.hostname,
        platform=connection.platform,
        version=connection.version,
        protocol_version=connection.protocol_version,
        capabilities=connection.capabilities,
        connected_at=connection.connected_at,
        last_seen=connection.last_seen,
    )


def negotiate_protocol(protocol_min: int, protocol_max: int) -> int | None:
    lower = max(PROTOCOL_MIN_SUPPORTED, protocol_min)
    upper = min(PROTOCOL_MAX_SUPPORTED, protocol_max)
    return upper if lower <= upper else None


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


@app.post(
    "/api/v1/approvals",
    dependencies=[Depends(require_approval_admin_token)],
    status_code=status.HTTP_201_CREATED,
)
async def create_approval(body: ApprovalCreateBody, settings: SettingsDep) -> ApprovalTicket:
    validate_approval_target(body.operation, body.target)
    grant, approval_secret = await approval_store().issue(
        agent_id=body.agent_id,
        operation=body.operation,
        target=body.target,
        ttl_s=settings.approval_ttl_s,
    )
    audit_required(
        "approval_issued",
        approval_id=grant.approval_id,
        agent_id=grant.agent_id,
        operation=grant.operation,
        target=grant.target,
        expires_at=grant.expires_at.isoformat(),
    )
    return ApprovalTicket(
        approval_id=grant.approval_id,
        approval_secret=approval_secret,
        agent_id=grant.agent_id,
        operation=body.operation,
        target=grant.target,
        expires_at=grant.expires_at,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/api/v1/audit",
    dependencies=[Depends(require_control_token)],
)
async def list_audit_records(
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    event: Annotated[str | None, Query(max_length=128)] = None,
    agent_id: Annotated[str | None, Query(max_length=128)] = None,
) -> AuditQueryResult:
    journal = current_journal()
    if journal is None:
        journal = configure_audit(settings.audit_file, fsync=settings.audit_fsync)
    records, truncated = await asyncio.to_thread(
        journal.read_recent,
        limit=limit,
        event=event,
        agent_id=agent_id,
        max_scan_bytes=settings.audit_query_max_scan_bytes,
    )
    return AuditQueryResult(records=records, scan_truncated=truncated)


@app.post(
    "/api/v1/enrollments",
    dependencies=[Depends(require_control_token)],
    status_code=status.HTTP_201_CREATED,
)
async def create_enrollment(body: EnrollmentCreateBody, settings: SettingsDep) -> EnrollmentTicket:
    audit_required("enrollment_create_requested", agent_id=body.agent_id)
    registry = registry_for(str(settings.registry_file))
    code, expires_at = await registry.create_enrollment(body.agent_id, settings.enrollment_ttl_s)
    audit("enrollment_created", agent_id=body.agent_id, expires_at=expires_at.isoformat())
    return EnrollmentTicket(agent_id=body.agent_id, code=code, expires_at=expires_at)


@app.post("/api/v1/enrollments/claim")
async def claim_enrollment(
    body: EnrollmentClaimBody,
    settings: SettingsDep,
) -> EnrollmentClaimResult:
    audit_required("enrollment_claim_requested", agent_id=body.agent_id)
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
    audit_required("agent_revoke_requested", agent_id=agent_id)
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
    "/api/v1/agents/{agent_id}/commands/sessions",
    dependencies=[Depends(require_control_token)],
)
async def start_command_session(
    agent_id: str, body: CommandSessionStartBody, settings: SettingsDep
) -> CommandSessionSnapshot:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    enforce_agent_policy(agent_id, body.argv, settings)
    request_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    audit(
        "command_session_start_requested",
        agent_id=agent_id,
        session_id=session_id,
        executable=Path(body.argv[0]).name,
        argc=len(body.argv),
    )
    try:
        request = CommandSessionStartRequest(
            request_id=request_id,
            session_id=session_id,
            argv=body.argv,
        )
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandSessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "command_session_started",
            agent_id=agent_id,
            session_id=session_id,
            state=reply.state,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent session start timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.get(
    "/api/v1/agents/{agent_id}/commands/sessions/{session_id}",
    dependencies=[Depends(require_control_token)],
)
async def get_command_session(
    agent_id: str, session_id: str, settings: SettingsDep
) -> CommandSessionSnapshot:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = CommandSessionStatusRequest(request_id=request_id, session_id=session_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandSessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent session status timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/commands/sessions/{session_id}/cancel",
    dependencies=[Depends(require_control_token)],
)
async def cancel_command_session(
    agent_id: str, session_id: str, settings: SettingsDep
) -> CommandSessionSnapshot:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    audit("command_session_cancel_requested", agent_id=agent_id, session_id=session_id)
    try:
        request = CommandSessionCancelRequest(request_id=request_id, session_id=session_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandSessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "command_session_cancelled",
            agent_id=agent_id,
            session_id=session_id,
            state=reply.state,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent session cancel timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/commands/sessions/{session_id}/output",
    dependencies=[Depends(require_control_token)],
)
async def get_command_session_output(
    agent_id: str,
    session_id: str,
    body: CommandSessionOutputBody,
    settings: SettingsDep,
) -> CommandSessionOutput:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = CommandSessionOutputRequest(
            request_id=request_id,
            session_id=session_id,
            **body.model_dump(),
        )
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandSessionOutput):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent session output timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/commands/sessions/{session_id}/discard",
    dependencies=[Depends(require_control_token)],
)
async def discard_command_session(
    agent_id: str,
    session_id: str,
    settings: SettingsDep,
) -> CommandSessionDiscardResult:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = CommandSessionDiscardRequest(request_id=request_id, session_id=session_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, CommandSessionDiscardResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "command_session_discarded",
            agent_id=agent_id,
            session_id=session_id,
            discarded=reply.discarded,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent session discard timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions",
    dependencies=[Depends(require_control_token)],
)
async def start_pty_session(
    agent_id: str, body: PtySessionStartBody, settings: SettingsDep
) -> PtySessionSnapshot:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    enforce_pty_policy(agent_id, body.argv, settings)
    target = pty_approval_target(body.argv)
    try:
        grant = await approval_store().consume(
            approval_id=body.approval_id,
            secret=body.approval_secret,
            agent_id=agent_id,
            operation="pty.start",
            target=target,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    request_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    audit_required(
        "approval_consumed",
        approval_id=grant.approval_id,
        agent_id=agent_id,
        operation=grant.operation,
        target=grant.target,
    )
    audit_required(
        "pty_session_start_requested",
        agent_id=agent_id,
        session_id=session_id,
        executable=Path(body.argv[0]).name,
        argc=len(body.argv),
        columns=body.columns,
        rows=body.rows,
    )
    future = pending_request(connection, request_id)
    try:
        request = PtySessionStartRequest(
            request_id=request_id,
            session_id=session_id,
            argv=body.argv,
            columns=body.columns,
            rows=body.rows,
        )
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "pty_session_started",
            agent_id=agent_id,
            session_id=session_id,
            state=reply.state,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY start timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.get(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}",
    dependencies=[Depends(require_control_token)],
)
async def get_pty_session(
    agent_id: str, session_id: str, settings: SettingsDep
) -> PtySessionSnapshot:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionStatusRequest(
                request_id=request_id, session_id=session_id
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY status timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}/input",
    dependencies=[Depends(require_control_token)],
)
async def write_pty_input(
    agent_id: str,
    session_id: str,
    body: PtySessionInputBody,
    settings: SettingsDep,
) -> PtySessionInputResult:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    input_bytes = len(body.data.encode())
    if input_bytes > settings.pty_input_max_bytes:
        raise HTTPException(status_code=413, detail="PTY input exceeds byte limit")
    request_id = uuid.uuid4().hex
    audit_required(
        "pty_session_input_requested",
        agent_id=agent_id,
        session_id=session_id,
        input_bytes=input_bytes,
    )
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionInputRequest(
                request_id=request_id, session_id=session_id, data=body.data
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionInputResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY input timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}/resize",
    dependencies=[Depends(require_control_token)],
)
async def resize_pty_session(
    agent_id: str,
    session_id: str,
    body: PtySessionResizeBody,
    settings: SettingsDep,
) -> PtySessionResizeResult:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    request_id = uuid.uuid4().hex
    audit_required(
        "pty_session_resize_requested",
        agent_id=agent_id,
        session_id=session_id,
        columns=body.columns,
        rows=body.rows,
    )
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionResizeRequest(
                request_id=request_id,
                session_id=session_id,
                columns=body.columns,
                rows=body.rows,
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionResizeResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY resize timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}/output",
    dependencies=[Depends(require_control_token)],
)
async def get_pty_output(
    agent_id: str,
    session_id: str,
    body: PtySessionOutputBody,
    settings: SettingsDep,
) -> PtySessionOutput:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionOutputRequest(
                request_id=request_id,
                session_id=session_id,
                offset=body.offset,
                max_chars=body.max_chars,
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionOutput):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY output timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}/cancel",
    dependencies=[Depends(require_control_token)],
)
async def cancel_pty_session(
    agent_id: str, session_id: str, settings: SettingsDep
) -> PtySessionSnapshot:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    request_id = uuid.uuid4().hex
    audit_required("pty_session_cancel_requested", agent_id=agent_id, session_id=session_id)
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionCancelRequest(
                request_id=request_id, session_id=session_id
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionSnapshot):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY cancel timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/pty/sessions/{session_id}/discard",
    dependencies=[Depends(require_control_token)],
)
async def discard_pty_session(
    agent_id: str, session_id: str, settings: SettingsDep
) -> PtySessionDiscardResult:
    validate_command_session_id(session_id)
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    require_agent_capability(connection, "command.pty")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        await connection.websocket.send_text(
            PtySessionDiscardRequest(
                request_id=request_id, session_id=session_id
            ).model_dump_json()
        )
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PtySessionDiscardResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "pty_session_discarded",
            agent_id=agent_id,
            session_id=session_id,
            discarded=reply.discarded,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent PTY discard timed out") from exc
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
    "/api/v1/agents/{agent_id}/diagnostics/health",
    dependencies=[Depends(require_control_token)],
)
async def get_system_health(agent_id: str, settings: SettingsDep) -> SystemHealthResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = SystemHealthRequest(request_id=request_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, SystemHealthResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent health query timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/diagnostics/port",
    dependencies=[Depends(require_control_token)],
)
async def lookup_agent_port(
    agent_id: str, body: PortLookupBody, settings: SettingsDep
) -> PortLookupResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = PortLookupRequest(request_id=request_id, port=body.port)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, PortLookupResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit("port_lookup", agent_id=agent_id, port=body.port, count=len(reply.listeners))
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent port lookup timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/diagnostics/service-logs",
    dependencies=[Depends(require_control_token)],
)
async def get_agent_service_logs(
    agent_id: str, body: ServiceLogsBody, settings: SettingsDep
) -> ServiceLogsResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = ServiceLogsRequest(request_id=request_id, **body.model_dump())
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, ServiceLogsResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "service_logs",
            agent_id=agent_id,
            unit=body.unit,
            lines=body.lines,
            truncated=reply.truncated,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent service logs timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/diagnostics/git-status",
    dependencies=[Depends(require_control_token)],
)
async def get_agent_git_status(
    agent_id: str, body: GitStatusBody, settings: SettingsDep
) -> GitStatusResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = GitStatusRequest(request_id=request_id, path=body.path)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, GitStatusResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "git_status",
            agent_id=agent_id,
            path=body.path,
            clean=reply.clean,
            truncated=reply.truncated,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent git status timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.get(
    "/api/v1/agents/{agent_id}/files/roots",
    dependencies=[Depends(require_control_token)],
)
async def list_agent_file_roots(agent_id: str, settings: SettingsDep) -> FileRootListResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = FileRootListRequest(request_id=request_id)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, FileRootListResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent file roots timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/files/list",
    dependencies=[Depends(require_control_token)],
)
async def list_agent_directory(
    agent_id: str, body: DirectoryListBody, settings: SettingsDep
) -> DirectoryListResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = DirectoryListRequest(request_id=request_id, **body.model_dump())
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, DirectoryListResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "directory_list",
            agent_id=agent_id,
            path=body.path,
            count=len(reply.entries),
            truncated=reply.truncated,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent directory list timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/files/info",
    dependencies=[Depends(require_control_token)],
)
async def get_agent_file_info(
    agent_id: str, body: FileInfoBody, settings: SettingsDep
) -> FileInfoResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    try:
        request = FileInfoRequest(request_id=request_id, path=body.path)
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, FileInfoResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit("file_info", agent_id=agent_id, path=body.path, kind=reply.kind)
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent file info timed out") from exc
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
    audit_required(
        "file_write_requested",
        agent_id=agent_id,
        path=body.path,
        overwrite=body.overwrite,
    )
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


@app.post(
    "/api/v1/agents/{agent_id}/processes/terminate",
    dependencies=[Depends(require_control_token)],
)
async def terminate_agent_process(
    agent_id: str, body: ProcessTerminateBody, settings: SettingsDep
) -> ProcessTerminateResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    target = process_approval_target(body.pid, body.expected_create_time_ms)
    try:
        grant = await approval_store().consume(
            approval_id=body.approval_id,
            secret=body.approval_secret,
            agent_id=agent_id,
            operation="process.terminate",
            target=target,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    audit_required(
        "approval_consumed",
        approval_id=grant.approval_id,
        agent_id=agent_id,
        operation=grant.operation,
        target=grant.target,
    )
    try:
        request = ProcessTerminateRequest(
            request_id=request_id,
            pid=body.pid,
            expected_create_time_ms=body.expected_create_time_ms,
        )
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, ProcessTerminateResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit("process_terminate", agent_id=agent_id, pid=body.pid, rejected=reply.rejected)
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent process terminate timed out") from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="agent disconnected") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.post(
    "/api/v1/agents/{agent_id}/services/action",
    dependencies=[Depends(require_control_token)],
)
async def mutate_agent_service(
    agent_id: str, body: ServiceActionBody, settings: SettingsDep
) -> ServiceActionResult:
    connection = connections.get(agent_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="agent not connected")
    operation = service_approval_operation(body.action)
    try:
        grant = await approval_store().consume(
            approval_id=body.approval_id,
            secret=body.approval_secret,
            agent_id=agent_id,
            operation=operation,
            target=body.unit,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    request_id = uuid.uuid4().hex
    future = pending_request(connection, request_id)
    audit_required(
        "approval_consumed",
        approval_id=grant.approval_id,
        agent_id=agent_id,
        operation=grant.operation,
        target=grant.target,
    )
    try:
        request = ServiceActionRequest(
            request_id=request_id,
            unit=body.unit,
            action=body.action,
        )
        await connection.websocket.send_text(request.model_dump_json())
        reply = await asyncio.wait_for(future, timeout=settings.request_timeout_s)
        if not isinstance(reply, ServiceActionResult):
            raise HTTPException(status_code=502, detail="unexpected agent response")
        audit(
            "service_action",
            agent_id=agent_id,
            unit=body.unit,
            action=body.action,
            returncode=reply.returncode,
            rejected=reply.rejected,
        )
        return reply
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent service action timed out") from exc
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
    connection = AgentConnection(websocket=websocket)
    registered = False

    try:
        try:
            payload = await asyncio.wait_for(
                websocket.receive_json(), timeout=settings.request_timeout_s
            )
        except TimeoutError:
            audit("agent_hello_timeout", agent_id=agent_id)
            await websocket.close(code=4408, reason="hello timeout")
            return
        except ValueError:
            audit("agent_hello_invalid", agent_id=agent_id)
            await websocket.close(code=4400, reason="invalid hello")
            return
        connection.last_seen = datetime.now(UTC)
        if not isinstance(payload, dict) or payload.get("type") != "hello":
            audit("agent_hello_required", agent_id=agent_id)
            await websocket.close(code=4400, reason="hello required")
            return
        try:
            hello = AgentHello.model_validate(payload)
        except ValueError:
            audit("agent_hello_invalid", agent_id=agent_id)
            await websocket.close(code=4400, reason="invalid hello")
            return
        if hello.agent_id != agent_id:
            await websocket.close(code=4403, reason="agent identity mismatch")
            return

        protocol_version = negotiate_protocol(hello.protocol_min, hello.protocol_max)
        if protocol_version is None:
            audit(
                "agent_protocol_rejected",
                agent_id=agent_id,
                agent_protocol_min=hello.protocol_min,
                agent_protocol_max=hello.protocol_max,
                gateway_protocol_min=PROTOCOL_MIN_SUPPORTED,
                gateway_protocol_max=PROTOCOL_MAX_SUPPORTED,
            )
            await websocket.close(code=4406, reason="incompatible protocol")
            return

        connection.hostname = hello.hostname
        connection.platform = hello.platform
        connection.version = hello.version
        connection.protocol_version = protocol_version
        connection.capabilities = sorted(set(hello.capabilities))

        old = connections.get(agent_id)
        connections[agent_id] = connection
        registered = True
        if old is not None and old is not connection:
            await old.websocket.close(code=4000, reason="replaced by newer agent connection")

        audit(
            "agent_connected",
            agent_id=agent_id,
            auth_source=auth_source,
            protocol_version=protocol_version,
        )
        audit(
            "agent_hello",
            agent_id=agent_id,
            hostname=hello.hostname,
            protocol_version=protocol_version,
            capability_count=len(connection.capabilities),
        )

        while True:
            try:
                payload = await websocket.receive_json()
            except ValueError:
                await websocket.close(code=4400, reason="invalid protocol message")
                return
            connection.last_seen = datetime.now(UTC)
            if not isinstance(payload, dict):
                await websocket.close(code=4400, reason="invalid protocol message")
                return
            message_type = payload.get("type")

            if message_type == "hello":
                await websocket.close(code=4400, reason="duplicate hello")
                return

            if message_type == "heartbeat":
                try:
                    heartbeat = Heartbeat.model_validate(payload)
                except ValueError:
                    await websocket.close(code=4400, reason="invalid heartbeat")
                    return
                if heartbeat.agent_id != agent_id:
                    await websocket.close(code=4403, reason="agent identity mismatch")
                    return
                continue

            if message_type == "command_result":
                reply: AgentReply = CommandResult.model_validate(payload)
            elif message_type == "command_session_snapshot":
                reply = CommandSessionSnapshot.model_validate(payload)
            elif message_type == "command_session_output":
                reply = CommandSessionOutput.model_validate(payload)
            elif message_type == "command_session_discard_result":
                reply = CommandSessionDiscardResult.model_validate(payload)
            elif message_type == "pty_session_snapshot":
                reply = PtySessionSnapshot.model_validate(payload)
            elif message_type == "pty_session_output":
                reply = PtySessionOutput.model_validate(payload)
            elif message_type == "pty_session_input_result":
                reply = PtySessionInputResult.model_validate(payload)
            elif message_type == "pty_session_resize_result":
                reply = PtySessionResizeResult.model_validate(payload)
            elif message_type == "pty_session_discard_result":
                reply = PtySessionDiscardResult.model_validate(payload)
            elif message_type == "ping_result":
                reply = PingResult.model_validate(payload)
            elif message_type == "file_root_list_result":
                reply = FileRootListResult.model_validate(payload)
            elif message_type == "directory_list_result":
                reply = DirectoryListResult.model_validate(payload)
            elif message_type == "file_info_result":
                reply = FileInfoResult.model_validate(payload)
            elif message_type == "file_read_result":
                reply = FileReadResult.model_validate(payload)
            elif message_type == "file_write_result":
                reply = FileWriteResult.model_validate(payload)
            elif message_type == "process_list_result":
                reply = ProcessListResult.model_validate(payload)
            elif message_type == "service_status_result":
                reply = ServiceStatusResult.model_validate(payload)
            elif message_type == "process_terminate_result":
                reply = ProcessTerminateResult.model_validate(payload)
            elif message_type == "service_action_result":
                reply = ServiceActionResult.model_validate(payload)
            elif message_type == "system_health_result":
                reply = SystemHealthResult.model_validate(payload)
            elif message_type == "port_lookup_result":
                reply = PortLookupResult.model_validate(payload)
            elif message_type == "service_logs_result":
                reply = ServiceLogsResult.model_validate(payload)
            elif message_type == "git_status_result":
                reply = GitStatusResult.model_validate(payload)
            else:
                continue

            future = connection.pending.get(reply.request_id)
            if future is not None and not future.done():
                future.set_result(reply)
    except WebSocketDisconnect:
        pass
    finally:
        if registered and connections.get(agent_id) is connection:
            connections.pop(agent_id, None)
        for future in connection.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent disconnected"))
        if registered:
            audit("agent_disconnected", agent_id=agent_id)


def run() -> None:
    settings = get_settings()
    settings.validate_gateway_security()
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
