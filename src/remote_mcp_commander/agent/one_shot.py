from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.protocol import CommandRequest, CommandResult

SendText = Callable[[str], Awaitable[None]]


class OneShotCommandDispatcher:
    def __init__(
        self,
        *,
        allowlist: set[str],
        timeout_s: float,
        max_output_bytes: int,
        max_active: int,
        exec_search_path: str,
        policy_mode: str,
        child_env: dict[str, str],
        roots: list[Path] | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.max_active = max_active
        self.exec_search_path = exec_search_path
        self.policy_mode = policy_mode
        self.child_env = child_env
        self.roots = roots or []
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            # The connection loop owns reconnect/error handling. A failed response
            # send must not become an unobserved background-task exception.
            pass

    async def submit(self, request: CommandRequest, send_text: SendText) -> bool:
        if len(self._tasks) >= self.max_active:
            rejected = CommandResult(
                request_id=request.request_id,
                rejected=True,
                error="too many active one-shot commands",
            )
            await send_text(rejected.model_dump_json())
            return False
        task = asyncio.create_task(self._run(request, send_text))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return True

    async def _run(self, request: CommandRequest, send_text: SendText) -> None:
        result = await execute_argv(
            request.request_id,
            request.argv,
            allowlist=self.allowlist,
            timeout_s=self.timeout_s,
            max_output_bytes=self.max_output_bytes,
            exec_search_path=self.exec_search_path,
            policy_mode=self.policy_mode,
            child_env=self.child_env,
            env_overrides=request.env,
            cwd=request.cwd,
            roots=self.roots,
        )
        await send_text(result.model_dump_json())

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
