from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from remote_mcp_commander.agent.pty_ops import PtySessionManager
from remote_mcp_commander.agent.session_ops import CommandSessionManager
from remote_mcp_commander.protocol import (
    CommandSessionLineOutput,
    CommandSessionLineOutputRequest,
    PtySessionLineOutput,
    PtySessionLineOutputRequest,
)

SendText = Callable[[str], Awaitable[None]]


class LineOutputWaitDispatcher:
    def __init__(
        self,
        command_sessions: CommandSessionManager,
        pty_sessions: PtySessionManager,
        *,
        max_active: int = 16,
    ) -> None:
        self.command_sessions = command_sessions
        self.pty_sessions = pty_sessions
        self.max_active = max_active
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
            pass

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def submit_command(
        self, request: CommandSessionLineOutputRequest, send_text: SendText
    ) -> bool:
        if len(self._tasks) >= self.max_active:
            result = CommandSessionLineOutput(
                request_id=request.request_id,
                session_id=request.session_id,
                state="failed",
                stream=request.stream,
                rejected=True,
                error="too many active output waits",
            )
            await send_text(result.model_dump_json())
            return False
        task = asyncio.create_task(self._run_command(request, send_text))
        self._track(task)
        return True

    async def _run_command(
        self, request: CommandSessionLineOutputRequest, send_text: SendText
    ) -> None:
        result = await self.command_sessions.output_lines(
            request.request_id,
            request.session_id,
            stream=request.stream,
            offset=request.offset,
            max_lines=request.max_lines,
            wait_ms=request.wait_ms,
        )
        await send_text(result.model_dump_json())

    async def submit_pty(
        self, request: PtySessionLineOutputRequest, send_text: SendText
    ) -> bool:
        if len(self._tasks) >= self.max_active:
            result = PtySessionLineOutput(
                request_id=request.request_id,
                session_id=request.session_id,
                state="failed",
                rejected=True,
                error="too many active output waits",
            )
            await send_text(result.model_dump_json())
            return False
        task = asyncio.create_task(self._run_pty(request, send_text))
        self._track(task)
        return True

    async def _run_pty(
        self, request: PtySessionLineOutputRequest, send_text: SendText
    ) -> None:
        result = await self.pty_sessions.output_lines(
            request.request_id,
            request.session_id,
            offset=request.offset,
            max_lines=request.max_lines,
            wait_ms=request.wait_ms,
        )
        await send_text(result.model_dump_json())

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
