from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class AgentHello(BaseModel):
    type: Literal["hello"] = "hello"
    agent_id: str
    hostname: str


class Heartbeat(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    agent_id: str


class ExecuteBody(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=64)
