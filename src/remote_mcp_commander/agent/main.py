from __future__ import annotations

import asyncio
import json
import platform
import socket

import websockets

from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.config import get_settings
from remote_mcp_commander.protocol import (
    AgentHello,
    CommandRequest,
    Heartbeat,
    PingRequest,
    PingResult,
)


async def heartbeat_loop(websocket: websockets.ClientConnection, agent_id: str) -> None:
    while True:
        await asyncio.sleep(15)
        await websocket.send(Heartbeat(agent_id=agent_id).model_dump_json())


async def agent_loop() -> None:
    settings = get_settings()
    settings.validate_agent_security()
    headers = {"Authorization": f"Bearer {settings.agent_token}"}

    while True:
        try:
            async with websockets.connect(
                settings.gateway_ws,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_size=1_048_576,
            ) as websocket:
                hello = AgentHello(
                    agent_id=settings.agent_id,
                    hostname=socket.gethostname(),
                    platform=platform.platform(),
                )
                await websocket.send(hello.model_dump_json())
                heartbeat_task = asyncio.create_task(
                    heartbeat_loop(websocket, settings.agent_id)
                )
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
