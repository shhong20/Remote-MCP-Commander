from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from remote_mcp_commander.policy import (
    TRUSTED_GENERIC_EXEC_PATH,
    resolve_generic_executable,
    validate_generic_argv,
)
from remote_mcp_commander.protocol import (
    CommandSessionDiscardResult,
    CommandSessionOutput,
    CommandSessionSnapshot,
    CommandSessionState,
)


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
    stdout_truncated: bool = False
    stderr_truncated: bool = False
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
            stdout_truncated=session.stdout_truncated,
            stderr_truncated=session.stderr_truncated,
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

    async def _read_stream(
        self,
        stream: asyncio.StreamReader,
        session: _CommandSession,
        *,
        output_attr: str,
        truncated_attr: str,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        retained_bytes = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = max(0, self.max_output_bytes - retained_bytes)
            accepted = chunk[:remaining]
            if accepted:
                text = decoder.decode(accepted, final=False)
                setattr(session, output_attr, getattr(session, output_attr) + text)
                retained_bytes += len(accepted)
            if len(chunk) > remaining:
                setattr(session, truncated_attr, True)
        tail = decoder.decode(b"", final=True)
        if tail:
            setattr(session, output_attr, getattr(session, output_attr) + tail)

    async def _wait_readers(self, *tasks: asyncio.Task[None]) -> str | None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                return f"output capture failed: {type(result).__name__}"
        return None

    async def _terminate_process(self, session: _CommandSession) -> None:
        if session.process.returncode is not None:
            return
        session.process.terminate()
        try:
            await asyncio.wait_for(session.process.wait(), timeout=2)
        except TimeoutError:
            if session.process.returncode is None:
                session.process.kill()
            await session.process.wait()

    async def _run(self, session: _CommandSession) -> None:
        assert session.process.stdout is not None
        assert session.process.stderr is not None
        stdout_task = asyncio.create_task(
            self._read_stream(
                session.process.stdout,
                session,
                output_attr="stdout",
                truncated_attr="stdout_truncated",
            )
        )
        stderr_task = asyncio.create_task(
            self._read_stream(
                session.process.stderr,
                session,
                output_attr="stderr",
                truncated_attr="stderr_truncated",
            )
        )
        terminal_state: CommandSessionState = "failed"
        terminal_error: str | None = None
        try:
            await asyncio.wait_for(session.process.wait(), timeout=self.timeout_s)
            session.returncode = session.process.returncode
            if session.cancel_requested:
                terminal_state = "cancelled"
                terminal_error = "session cancelled"
            else:
                terminal_state = "completed"
        except TimeoutError:
            if session.process.returncode is None:
                session.process.kill()
            await session.process.wait()
            session.returncode = session.process.returncode
            terminal_state = "timed_out"
            terminal_error = "session timed out"
        except asyncio.CancelledError:
            await self._terminate_process(session)
            session.returncode = session.process.returncode
            terminal_state = "cancelled"
            terminal_error = "session cancelled"
        except Exception as exc:  # defensive OS boundary
            await self._terminate_process(session)
            session.returncode = session.process.returncode
            terminal_state = "failed"
            terminal_error = f"session failed: {type(exc).__name__}"
        finally:
            capture_error = await self._wait_readers(stdout_task, stderr_task)
            if capture_error is not None and terminal_state == "completed":
                terminal_state = "failed"
                terminal_error = capture_error
            session.state = terminal_state
            session.error = terminal_error
            session.finished_at = datetime.now(UTC)

    async def status(self, request_id: str, session_id: str) -> CommandSessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            return self._error_snapshot(request_id, session_id, "session not found")
        return self._snapshot(request_id, session)

    async def output(
        self,
        request_id: str,
        session_id: str,
        *,
        stdout_offset: int,
        stderr_offset: int,
        max_chars: int,
    ) -> CommandSessionOutput:
        session = self._sessions.get(session_id)
        if session is None:
            return CommandSessionOutput(
                request_id=request_id,
                session_id=session_id,
                state="failed",
                rejected=True,
                error="session not found",
            )
        if stdout_offset > len(session.stdout) or stderr_offset > len(session.stderr):
            return CommandSessionOutput(
                request_id=request_id,
                session_id=session_id,
                state=session.state,
                next_stdout_offset=len(session.stdout),
                next_stderr_offset=len(session.stderr),
                stdout_truncated=session.stdout_truncated,
                stderr_truncated=session.stderr_truncated,
                rejected=True,
                error="output offset exceeds retained output",
            )
        stdout = session.stdout[stdout_offset : stdout_offset + max_chars]
        stderr = session.stderr[stderr_offset : stderr_offset + max_chars]
        return CommandSessionOutput(
            request_id=request_id,
            session_id=session_id,
            state=session.state,
            stdout=stdout,
            stderr=stderr,
            next_stdout_offset=stdout_offset + len(stdout),
            next_stderr_offset=stderr_offset + len(stderr),
            stdout_truncated=session.stdout_truncated,
            stderr_truncated=session.stderr_truncated,
            error=session.error,
        )

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

    async def discard(self, request_id: str, session_id: str) -> CommandSessionDiscardResult:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return CommandSessionDiscardResult(
                    request_id=request_id,
                    session_id=session_id,
                    rejected=True,
                    error="session not found",
                )
            if session.state == "running":
                return CommandSessionDiscardResult(
                    request_id=request_id,
                    session_id=session_id,
                    rejected=True,
                    error="running session cannot be discarded",
                )
            self._sessions.pop(session_id, None)
        return CommandSessionDiscardResult(
            request_id=request_id,
            session_id=session_id,
            discarded=True,
        )

    async def cancel_all(self) -> None:
        running = [
            session.session_id for session in self._sessions.values() if session.state == "running"
        ]
        if running:
            await asyncio.gather(
                *(self.cancel("disconnect", session_id) for session_id in running),
                return_exceptions=True,
            )
