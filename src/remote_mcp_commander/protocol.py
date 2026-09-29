from __future__ import annotations

from datetime import datetime
from typing import Literal

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
