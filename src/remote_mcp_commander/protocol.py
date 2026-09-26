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
