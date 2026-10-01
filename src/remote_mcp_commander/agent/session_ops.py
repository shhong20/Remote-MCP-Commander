from __future__ import annotations

import asyncio
import codecs
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from remote_mcp_commander.agent.env_policy import merge_command_env
from remote_mcp_commander.agent.file_ops import resolve_allowed_directory
from remote_mcp_commander.agent.output_lines import paginate_lines
from remote_mcp_commander.policy import (
    TRUSTED_GENERIC_EXEC_PATH,
    resolve_generic_executable,
    validate_generic_argv,
)
from remote_mcp_commander.protocol import (
    CommandSessionDiscardResult,
    CommandSessionInputResult,
    CommandSessionLineOutput,
    CommandSessionOutput,
    CommandSessionSnapshot,
    CommandSessionState,
    CommandSessionStdinCloseResult,
    RuntimeSessionInfo,
)


@dataclass
class _CommandSession:
    session_id: str
    executable: str
    process: asyncio.subprocess.Process
    started_at: datetime
    cwd: str | None = None
    timeout_s: float = 300.0
    state: CommandSessionState = "running"
    finished_at: datetime | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None
    cancel_requested: bool = False
    stdin_closed: bool = False
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
        max_input_bytes: int = 16_384,
        exec_search_path: str = TRUSTED_GENERIC_EXEC_PATH,
        policy_mode: str = "hardened",
        child_env: dict[str, str] | None = None,
        roots: list[Path] | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.max_active = max_active
        self.history_limit = history_limit
        self.max_input_bytes = max_input_bytes
        self.exec_search_path = exec_search_path
        self.policy_mode = policy_mode
        self.child_env = child_env or {"PATH": exec_search_path}
        self.roots = roots or []
        self._sessions: dict[str, _CommandSession] = {}
        self._lock = asyncio.Lock()

    def _snapshot(self, request_id: str, session: _CommandSession) -> CommandSessionSnapshot:
        return CommandSessionSnapshot(
            request_id=request_id,
            session_id=session.session_id,
            executable=session.executable,
            state=session.state,
            cwd=session.cwd,
            timeout_s=session.timeout_s,
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
        *,
        cwd: str | None = None,
        env_overrides: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandSessionSnapshot:
        policy_error = validate_generic_argv(argv, mode=self.policy_mode)
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
        try:
            if timeout_s is not None and not 1.0 <= timeout_s <= 3600.0:
                raise ValueError("session timeout must be between 1 and 3600 seconds")
            effective_timeout = timeout_s or self.timeout_s
            resolved_cwd = resolve_allowed_directory(cwd, self.roots) if cwd is not None else None
            process_env = merge_command_env(
                self.child_env,
                env_overrides or {},
                personal_mode=self.policy_mode == "personal",
            )
        except (OSError, PermissionError, ValueError) as exc:
            return self._error_snapshot(request_id, session_id, str(exc))

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
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=process_env,
                    cwd=resolved_cwd,
                    start_new_session=(os.name == "posix"),
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
                cwd=str(resolved_cwd) if resolved_cwd is not None else None,
                timeout_s=effective_timeout,
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
        try:
            if os.name == "posix":
                os.killpg(session.process.pid, signal.SIGTERM)
            else:
                session.process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(session.process.wait(), timeout=2)
        except TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(session.process.pid, signal.SIGKILL)
                elif session.process.returncode is None:
                    session.process.kill()
            except ProcessLookupError:
                pass
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
            await asyncio.wait_for(session.process.wait(), timeout=session.timeout_s)
            session.returncode = session.process.returncode
            if session.cancel_requested:
                terminal_state = "cancelled"
                terminal_error = "session cancelled"
            else:
                terminal_state = "completed"
        except TimeoutError:
            await self._terminate_process(session)
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
            session.stdin_closed = True
            session.state = terminal_state
            session.error = terminal_error
            session.finished_at = datetime.now(UTC)

    async def write_input(
        self, request_id: str, session_id: str, data: str
    ) -> CommandSessionInputResult:
        session = self._sessions.get(session_id)
        if session is None:
            return CommandSessionInputResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="session not found",
            )
        encoded = data.encode("utf-8")
        if len(encoded) > self.max_input_bytes:
            return CommandSessionInputResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="input exceeds size limit",
            )
        writer = session.process.stdin
        if session.state != "running" or writer is None or session.stdin_closed:
            return CommandSessionInputResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="session stdin is not writable",
            )
        try:
            writer.write(encoded)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            session.stdin_closed = True
            return CommandSessionInputResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="session stdin is closed",
            )
        return CommandSessionInputResult(
            request_id=request_id, session_id=session_id, accepted_bytes=len(encoded)
        )

    async def close_stdin(
        self, request_id: str, session_id: str
    ) -> CommandSessionStdinCloseResult:
        session = self._sessions.get(session_id)
        if session is None:
            return CommandSessionStdinCloseResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="session not found",
            )
        if session.stdin_closed:
            return CommandSessionStdinCloseResult(
                request_id=request_id, session_id=session_id, closed=True
            )
        writer = session.process.stdin
        if writer is None:
            return CommandSessionStdinCloseResult(
                request_id=request_id, session_id=session_id, rejected=True,
                error="session stdin is unavailable",
            )
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        session.stdin_closed = True
        return CommandSessionStdinCloseResult(
            request_id=request_id, session_id=session_id, closed=True
        )

    async def list_infos(self, *, include_completed: bool) -> list[RuntimeSessionInfo]:
        sessions = list(self._sessions.values())
        if not include_completed:
            sessions = [session for session in sessions if session.state == "running"]
        return [
            RuntimeSessionInfo(
                session_id=session.session_id,
                kind="command",
                executable=session.executable,
                state=session.state,
                cwd=session.cwd,
                timeout_s=session.timeout_s,
                started_at=session.started_at,
                finished_at=session.finished_at,
                returncode=session.returncode,
                output_chars=len(session.stdout) + len(session.stderr),
                output_truncated=session.stdout_truncated or session.stderr_truncated,
            )
            for session in sessions
        ]

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

    async def output_lines(
        self,
        request_id: str,
        session_id: str,
        *,
        stream: str,
        offset: int,
        max_lines: int,
    ) -> CommandSessionLineOutput:
        session = self._sessions.get(session_id)
        if session is None:
            return CommandSessionLineOutput(
                request_id=request_id, session_id=session_id, state="failed",
                stream=stream, rejected=True, error="session not found",
            )
        text = session.stdout if stream == "stdout" else session.stderr
        truncated = session.stdout_truncated if stream == "stdout" else session.stderr_truncated
        page = paginate_lines(
            text, running=session.state == "running", offset=offset, max_lines=max_lines
        )
        return CommandSessionLineOutput(
            request_id=request_id,
            session_id=session_id,
            state=session.state,
            stream=stream,
            content=page.content,
            total_lines=page.total_lines,
            start_line=page.start_line,
            next_line=page.next_line,
            eof=page.eof,
            pending_partial=page.pending_partial,
            output_truncated=truncated,
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
        await self._terminate_process(session)
        await asyncio.shield(session.task)
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
