from __future__ import annotations

import asyncio
import json
import platform
import socket

import websockets

from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.agent.file_ops import allowed_roots, read_text_file, write_text_file
from remote_mcp_commander.agent.system_ops import list_processes, service_status
from remote_mcp_commander.config import get_settings
from remote_mcp_commander.protocol import (
    AgentHello,
    CommandRequest,
    FileReadRequest,
    FileWriteRequest,
    Heartbeat,
    PingRequest,
    PingResult,
    ProcessListRequest,
    ServiceStatusRequest,
)


async def heartbeat_loop(websocket: websockets.ClientConnection, agent_id: str) -> None:
    while True:
        await asyncio.sleep(15)
        await websocket.send(Heartbeat(agent_id=agent_id).model_dump_json())


async def agent_loop() -> None:
    settings = get_settings()
    token = settings.load_agent_token()
    settings.validate_agent_security(token)
    headers = {"Authorization": f"Bearer {token}"}
    roots = allowed_roots(settings.allowed_roots)

    while True:
        try:
            async with websockets.connect(
                settings.gateway_ws,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_size=2_097_152,
            ) as websocket:
                hello = AgentHello(
                    agent_id=settings.agent_id,
                    hostname=socket.gethostname(),
                    platform=platform.platform(),
                )
                await websocket.send(hello.model_dump_json())
                heartbeat_task = asyncio.create_task(heartbeat_loop(websocket, settings.agent_id))
                try:
                    async for raw in websocket:
                        payload = json.loads(raw)
                        message_type = payload.get("type")
                        if message_type == "ping_request":
                            ping = PingRequest.model_validate(payload)
                            await websocket.send(
                                PingResult(request_id=ping.request_id).model_dump_json()
                            )
                            continue

                        if message_type == "file_read_request":
                            request = FileReadRequest.model_validate(payload)
                            result = await read_text_file(
                                request.request_id,
                                request.path,
                                roots=roots,
                                offset=request.offset,
                                max_bytes=request.max_bytes,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_write_request":
                            request = FileWriteRequest.model_validate(payload)
                            result = await write_text_file(
                                request.request_id,
                                request.path,
                                request.content,
                                roots=roots,
                                overwrite=request.overwrite,
                                expected_sha256=request.expected_sha256,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "process_list_request":
                            request = ProcessListRequest.model_validate(payload)
                            result = await list_processes(request.request_id, request.limit)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "service_status_request":
                            request = ServiceStatusRequest.model_validate(payload)
                            result = await service_status(
                                request.request_id, request.unit, settings.exec_timeout_s
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type != "command_request":
                            continue
                        request = CommandRequest.model_validate(payload)
                        result = await execute_argv(
                            request.request_id,
                            request.argv,
                            allowlist=settings.executable_allowlist,
                            timeout_s=settings.exec_timeout_s,
                            max_output_bytes=settings.max_output_bytes,
                        )
                        await websocket.send(result.model_dump_json())
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
        except (OSError, websockets.ConnectionClosed):
            await asyncio.sleep(2)


def run() -> None:
    asyncio.run(agent_loop())


if __name__ == "__main__":
    run()
