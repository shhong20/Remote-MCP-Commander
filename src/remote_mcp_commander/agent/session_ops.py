from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from remote_mcp_commander.policy import (
    TRUSTED_GENERIC_EXEC_PATH,
    resolve_generic_executable,
    validate_generic_argv,
)
from remote_mcp_commander.protocol import CommandSessionSnapshot, CommandSessionState


@dataclass
class _CommandSession:
    session_id: str
    executable: str
    process: asyncio.subprocess.Process
    started_at: datetime
    state: CommandSessionState = "running"
    finished_at: datetime | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None


class CommandSessionManager:
    def __init__(
        self,
        *,
        allowlist: set[str],
        timeout_s: float,
        max_output_bytes: int,
        max_active: int = 4,
        history_limit: int = 100,
        exec_search_path: str = TRUSTED_GENERIC_EXEC_PATH,
    ) -> None:
        self.allowlist = allowlist
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.max_active = max_active
        self.history_limit = history_limit
        self.exec_search_path = exec_search_path
        self._sessions: dict[str, _CommandSession] = {}
        self._lock = asyncio.Lock()

    def _snapshot(self, request_id: str, session: _CommandSession) -> CommandSessionSnapshot:
        return CommandSessionSnapshot(
            request_id=request_id,
            session_id=session.session_id,
            executable=session.executable,
            state=session.state,
            started_at=session.started_at,
            finished_at=session.finished_at,
            returncode=session.returncode,
            stdout=session.stdout,
            stderr=session.stderr,
            error=session.error,
        )

    def _error_snapshot(
        self, request_id: str, session_id: str, error: str
    ) -> CommandSessionSnapshot:
        return CommandSessionSnapshot(
            request_id=request_id,
            session_id=session_id,
            state="failed",
            rejected=True,
            error=error,
        )

    def _prune_completed(self) -> None:
        if len(self._sessions) < self.history_limit:
            return
        removable = [
            session_id
            for session_id, session in self._sessions.items()
            if session.state != "running"
        ]
        while len(self._sessions) >= self.history_limit and removable:
            self._sessions.pop(removable.pop(0), None)

    async def start(
        self,
        request_id: str,
        session_id: str,
        argv: list[str],
    ) -> CommandSessionSnapshot:
        policy_error = validate_generic_argv(argv)
        if policy_error is not None:
            return self._error_snapshot(request_id, session_id, policy_error)
        executable = Path(argv[0]).name
        if executable not in self.allowlist:
            return self._error_snapshot(
                request_id,
                session_id,
                f"executable not allowed: {executable}",
            )
        resolved = resolve_generic_executable(executable, search_path=self.exec_search_path)
        if resolved is None:
            return self._error_snapshot(
                request_id,
                session_id,
                f"executable not found in trusted path: {executable}",
            )

        async with self._lock:
            self._prune_completed()
            if len(self._sessions) >= self.history_limit:
                return self._error_snapshot(request_id, session_id, "session history is full")
            active = sum(1 for session in self._sessions.values() if session.state == "running")
            if active >= self.max_active:
                return self._error_snapshot(
                    request_id, session_id, "too many active command sessions"
                )
            if session_id in self._sessions:
                return self._error_snapshot(request_id, session_id, "session already exists")
            try:
                process = await asyncio.create_subprocess_exec(
                    resolved,
                    *argv[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={"PATH": self.exec_search_path},
                )
            except FileNotFoundError:
                return self._error_snapshot(request_id, session_id, "executable not found")
            except Exception as exc:  # defensive OS boundary
                return self._error_snapshot(
                    request_id,
                    session_id,
                    f"session start failed: {type(exc).__name__}",
                )

            session = _CommandSession(
                session_id=session_id,
                executable=executable,
                process=process,
                started_at=datetime.now(UTC),
            )
            self._sessions[session_id] = session
            session.task = asyncio.create_task(self._run(session))
            return self._snapshot(request_id, session)

    async def _run(self, session: _CommandSession) -> None:
        communicate_task = asyncio.create_task(session.process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=self.timeout_s,
            )
            session.returncode = session.process.returncode
            session.stdout = stdout[: self.max_output_bytes].decode("utf-8", errors="replace")
            session.stderr = stderr[: self.max_output_bytes].decode("utf-8", errors="replace")
            if session.cancel_requested:
                session.state = "cancelled"
                session.error = "session cancelled"
            else:
                session.state = "completed"
        except TimeoutError:
            session.process.kill()
            stdout, stderr = await communicate_task
            session.returncode = session.process.returncode
            session.stdout = stdout[: self.max_output_bytes].decode("utf-8", errors="replace")
            session.stderr = stderr[: self.max_output_bytes].decode("utf-8", errors="replace")
            session.state = "timed_out"
            session.error = "session timed out"
        except asyncio.CancelledError:
            if session.process.returncode is None:
                session.process.terminate()
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=2,
                    )
                except TimeoutError:
                    session.process.kill()
                    stdout, stderr = await communicate_task
                session.stdout = stdout[: self.max_output_bytes].decode("utf-8", errors="replace")
                session.stderr = stderr[: self.max_output_bytes].decode("utf-8", errors="replace")
            elif communicate_task.done():
                stdout, stderr = communicate_task.result()
                session.stdout = stdout[: self.max_output_bytes].decode("utf-8", errors="replace")
                session.stderr = stderr[: self.max_output_bytes].decode("utf-8", errors="replace")
            session.returncode = session.process.returncode
            session.state = "cancelled"
            session.error = "session cancelled"
        except Exception as exc:  # defensive OS boundary
            if not communicate_task.done():
                communicate_task.cancel()
            session.state = "failed"
            session.error = f"session failed: {type(exc).__name__}"
        finally:
            session.finished_at = datetime.now(UTC)

    async def status(self, request_id: str, session_id: str) -> CommandSessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            return self._error_snapshot(request_id, session_id, "session not found")
        return self._snapshot(request_id, session)

    async def cancel(self, request_id: str, session_id: str) -> CommandSessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            return self._error_snapshot(request_id, session_id, "session not found")
        if session.state != "running" or session.task is None:
            return self._snapshot(request_id, session)
        if session.process.returncode is not None:
            await asyncio.shield(session.task)
            return self._snapshot(request_id, session)

        session.cancel_requested = True
        session.process.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(session.task), timeout=2)
        except TimeoutError:
            if session.process.returncode is None:
                session.process.kill()
            await session.task
        return self._snapshot(request_id, session)

    async def cancel_all(self) -> None:
        running = [
            session.session_id for session in self._sessions.values() if session.state == "running"
        ]
        if running:
            await asyncio.gather(
                *(self.cancel("disconnect", session_id) for session_id in running),
                return_exceptions=True,
            )
