from __future__ import annotations

import asyncio
import codecs
import os
import signal
from pathlib import Path

from remote_mcp_commander.agent.env_policy import merge_command_env
from remote_mcp_commander.agent.file_ops import resolve_allowed_directory
from remote_mcp_commander.policy import (
    TRUSTED_GENERIC_EXEC_PATH,
    resolve_generic_executable,
    validate_generic_argv,
)
from remote_mcp_commander.protocol import CommandResult


async def _read_bounded(stream: asyncio.StreamReader, max_output_bytes: int) -> tuple[str, bool]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    retained = 0
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        remaining = max(0, max_output_bytes - retained)
        accepted = chunk[:remaining]
        if accepted:
            parts.append(decoder.decode(accepted, final=False))
            retained += len(accepted)
        if len(chunk) > remaining:
            truncated = True
    parts.append(decoder.decode(b"", final=True))
    return "".join(parts), truncated


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


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
    env_overrides: dict[str, str] | None = None,
    cwd: str | None = None,
    roots: list[Path] | None = None,
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
        resolved_cwd = resolve_allowed_directory(cwd, roots or []) if cwd is not None else None
        process_env = merge_command_env(
            child_env or {"PATH": exec_search_path},
            env_overrides or {},
            personal_mode=policy_mode == "personal",
        )
    except (OSError, PermissionError, ValueError) as exc:
        return CommandResult(request_id=request_id, rejected=True, error=str(exc))

    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            cwd=resolved_cwd,
            start_new_session=(os.name == "posix"),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_output_bytes))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, max_output_bytes))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await _terminate_process(process)
        except asyncio.CancelledError:
            await _terminate_process(process)
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        return CommandResult(
            request_id=request_id,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            error="execution timed out" if timed_out else None,
        )
    except FileNotFoundError:
        return CommandResult(request_id=request_id, rejected=True, error="executable not found")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # defensive boundary around OS execution
        return CommandResult(request_id=request_id, error=f"execution failed: {type(exc).__name__}")
