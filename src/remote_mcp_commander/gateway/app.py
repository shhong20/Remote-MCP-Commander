from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status

from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.protocol import CommandRequest, CommandResult, ExecuteBody


@dataclass
class AgentConnection:
    websocket: WebSocket
    pending: dict[str, asyncio.Future[CommandResult]] = field(default_factory=dict)


app = FastAPI(title="Remote MCP Commander Gateway", version="0.1.0")
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
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"gateway policy denied executable: {executable}",
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/agents", dependencies=[Depends(require_control_token)])
async def list_agents() -> dict[str, list[str]]:
    return {"agents": sorted(connections)}


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
    loop = asyncio.get_running_loop()
    future: asyncio.Future[CommandResult] = loop.create_future()
    connection.pending[request_id] = future

    try:
        request = CommandRequest(request_id=request_id, argv=body.argv)
        await connection.websocket.send_text(request.model_dump_json())
        return await asyncio.wait_for(future, timeout=settings.request_timeout_s)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="agent request timed out") from exc
    finally:
        connection.pending.pop(request_id, None)


@app.websocket("/ws/agent/{agent_id}")
async def agent_socket(websocket: WebSocket, agent_id: str) -> None:
    settings = get_settings()
    token = websocket.headers.get("authorization")
    expected = f"Bearer {settings.token_for_agent(agent_id)}"
    if token is None or not secrets.compare_digest(token, expected):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    old = connections.get(agent_id)
    if old is not None:
        await old.websocket.close(code=4000, reason="replaced by newer agent connection")

    connection = AgentConnection(websocket=websocket)
    connections[agent_id] = connection

    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") != "command_result":
                continue
            result = CommandResult.model_validate(payload)
            future = connection.pending.get(result.request_id)
            if future is not None and not future.done():
                future.set_result(result)
    except WebSocketDisconnect:
        pass
    finally:
        if connections.get(agent_id) is connection:
            connections.pop(agent_id, None)
        for future in connection.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent disconnected"))


def run() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)
