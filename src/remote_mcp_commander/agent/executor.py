from __future__ import annotations

import asyncio
from pathlib import Path

from remote_mcp_commander.policy import (
    TRUSTED_GENERIC_EXEC_PATH,
    resolve_generic_executable,
    validate_generic_argv,
)
from remote_mcp_commander.protocol import CommandResult


async def execute_argv(
    request_id: str,
    argv: list[str],
    *,
    allowlist: set[str],
    timeout_s: float,
    max_output_bytes: int,
    exec_search_path: str = TRUSTED_GENERIC_EXEC_PATH,
    policy_mode: str = "hardened",
    child_env: dict[str, str] | None = None,
) -> CommandResult:
    executable = Path(argv[0]).name
    policy_error = validate_generic_argv(argv, mode=policy_mode)
    if policy_error is not None:
        return CommandResult(request_id=request_id, rejected=True, error=policy_error)
    if executable not in allowlist:
        return CommandResult(
            request_id=request_id,
            rejected=True,
            error=f"executable not allowed: {executable}",
        )
    resolved = resolve_generic_executable(executable, search_path=exec_search_path)
    if resolved is None:
        return CommandResult(
            request_id=request_id,
            rejected=True,
            error=f"executable not found in trusted path: {executable}",
        )

    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env or {"PATH": exec_search_path},
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
