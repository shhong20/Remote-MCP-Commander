from __future__ import annotations

import asyncio
import json

import websockets

from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.config import get_settings
from remote_mcp_commander.protocol import CommandRequest


async def agent_loop() -> None:
    settings = get_settings()
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
                async for raw in websocket:
                    payload = json.loads(raw)
                    if payload.get("type") != "command_request":
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
        except (OSError, websockets.ConnectionClosed):
            await asyncio.sleep(2)


def run() -> None:
    asyncio.run(agent_loop())


if __name__ == "__main__":
    run()
