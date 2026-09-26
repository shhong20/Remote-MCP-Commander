from __future__ import annotations

import asyncio
import os
from pathlib import Path

from remote_mcp_commander.protocol import CommandResult


async def execute_argv(
    request_id: str,
    argv: list[str],
    *,
    allowlist: set[str],
    timeout_s: float,
    max_output_bytes: int,
) -> CommandResult:
    executable = Path(argv[0]).name
    if executable not in allowlist:
        return CommandResult(
            request_id=request_id,
            rejected=True,
            error=f"executable not allowed: {executable}",
        )

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(request_id=request_id, timed_out=True, error="execution timed out")

        return CommandResult(
            request_id=request_id,
            returncode=process.returncode,
            stdout=stdout[:max_output_bytes].decode("utf-8", errors="replace"),
            stderr=stderr[:max_output_bytes].decode("utf-8", errors="replace"),
        )
    except FileNotFoundError:
        return CommandResult(request_id=request_id, rejected=True, error="executable not found")
    except Exception as exc:  # defensive boundary around OS execution
        return CommandResult(request_id=request_id, error=f"execution failed: {type(exc).__name__}")
