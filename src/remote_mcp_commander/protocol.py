from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"


class CommandRequest(BaseModel):
    type: Literal["command_request"] = "command_request"
    request_id: str
    argv: list[str] = Field(min_length=1, max_length=64)


class CommandResult(BaseModel):
    type: Literal["command_result"] = "command_result"
    request_id: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    rejected: bool = False
    error: str | None = None


class PingRequest(BaseModel):
    type: Literal["ping_request"] = "ping_request"
    request_id: str


class PingResult(BaseModel):
    type: Literal["ping_result"] = "ping_result"
    request_id: str


class AgentHello(BaseModel):
    type: Literal["hello"] = "hello"
    agent_id: str
    hostname: str
    platform: str
    version: str = "0.1.0"


class Heartbeat(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    agent_id: str


class ExecuteBody(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=64)


class AgentInfo(BaseModel):
    agent_id: str
    hostname: str | None = None
    platform: str | None = None
    version: str | None = None
    connected_at: datetime
    last_seen: datetime


class AgentList(BaseModel):
    agents: list[AgentInfo]


class PingResponse(BaseModel):
    agent_id: str
    round_trip_ms: float
    last_seen: datetime


class EnrollmentCreateBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)


class EnrollmentTicket(BaseModel):
    agent_id: str
    code: str
    expires_at: datetime


class EnrollmentClaimBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    code: str = Field(min_length=8, max_length=256)


class EnrollmentClaimResult(BaseModel):
    agent_id: str
    agent_token: str


class RegisteredDevice(BaseModel):
    agent_id: str
    created_at: datetime
    revoked_at: datetime | None = None


class RegisteredDeviceList(BaseModel):
    devices: list[RegisteredDevice]


class RevokeResult(BaseModel):
    agent_id: str
    revoked: bool


class FileReadRequest(BaseModel):
    type: Literal["file_read_request"] = "file_read_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=4, le=262_144)


class FileReadResult(BaseModel):
    type: Literal["file_read_result"] = "file_read_result"
    request_id: str
    path: str = ""
    content: str = ""
    size: int = 0
    offset: int = 0
    next_offset: int = 0
    eof: bool = True
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileWriteRequest(BaseModel):
    type: Literal["file_write_request"] = "file_write_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FileWriteResult(BaseModel):
    type: Literal["file_write_result"] = "file_write_result"
    request_id: str
    path: str = ""
    bytes_written: int = 0
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileReadBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=4, le=262_144)


class FileWriteBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class ProcessInfo(BaseModel):
    pid: int
    create_time_ms: int
    name: str
    username: str | None = None
    status: str | None = None
    memory_rss: int | None = None


class ProcessListRequest(BaseModel):
    type: Literal["process_list_request"] = "process_list_request"
    request_id: str
    limit: int = Field(default=100, ge=1, le=500)


class ProcessListResult(BaseModel):
    type: Literal["process_list_result"] = "process_list_result"
    request_id: str
    processes: list[ProcessInfo] = Field(default_factory=list)
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class ProcessListBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class ServiceStatusRequest(BaseModel):
    type: Literal["service_status_request"] = "service_status_request"
    request_id: str
    unit: str = Field(min_length=1, max_length=256)


class ServiceStatusResult(BaseModel):
    type: Literal["service_status_result"] = "service_status_result"
    request_id: str
    unit: str
    id: str | None = None
    description: str | None = None
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    returncode: int | None = None
    rejected: bool = False
    error: str | None = None


class ServiceStatusBody(BaseModel):
    unit: str = Field(min_length=1, max_length=256)


MutationOperation = Literal[
    "process.terminate",
    "service.start",
    "service.stop",
    "service.restart",
]
ServiceAction = Literal["start", "stop", "restart"]


class ApprovalCreateBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    operation: MutationOperation
    target: str = Field(min_length=1, max_length=512)


class ApprovalTicket(BaseModel):
    approval_id: str
    approval_secret: str
    agent_id: str
    operation: MutationOperation
    target: str
    expires_at: datetime


class ApprovalUse(BaseModel):
    approval_id: str = Field(min_length=8, max_length=256)
    approval_secret: str = Field(min_length=16, max_length=512)


class ProcessTerminateRequest(BaseModel):
    type: Literal["process_terminate_request"] = "process_terminate_request"
    request_id: str
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)


class ProcessTerminateResult(BaseModel):
    type: Literal["process_terminate_result"] = "process_terminate_result"
    request_id: str
    pid: int
    signal_sent: bool = False
    exited: bool = False
    rejected: bool = False
    error: str | None = None


class ProcessTerminateBody(ApprovalUse):
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)


class ServiceActionRequest(BaseModel):
    type: Literal["service_action_request"] = "service_action_request"
    request_id: str
    unit: str = Field(min_length=1, max_length=256)
    action: ServiceAction


class ServiceActionResult(BaseModel):
    type: Literal["service_action_result"] = "service_action_result"
    request_id: str
    unit: str
    action: ServiceAction
    returncode: int | None = None
    rejected: bool = False
    error: str | None = None


class ServiceActionBody(ApprovalUse):
    unit: str = Field(min_length=1, max_length=256)
    action: ServiceAction


class AuditQueryResult(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    scan_truncated: bool = False
