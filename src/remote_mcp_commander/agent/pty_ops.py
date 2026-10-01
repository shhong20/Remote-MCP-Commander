from __future__ import annotations

import asyncio
import codecs
import errno
import os
import shutil
import signal
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from remote_mcp_commander.agent.env_policy import merge_command_env
from remote_mcp_commander.agent.file_ops import resolve_allowed_directory
from remote_mcp_commander.agent.output_lines import wait_for_line_page
from remote_mcp_commander.agent.system_ops import process_create_time_ms, process_signal_value
from remote_mcp_commander.policy import TRUSTED_GENERIC_EXEC_PATH, validate_pty_argv
from remote_mcp_commander.protocol import (
    CommandSessionState,
    ProcessSignal,
    PtySessionDiscardResult,
    PtySessionInputResult,
    PtySessionLineOutput,
    PtySessionOutput,
    PtySessionResizeResult,
    PtySessionSnapshot,
    RuntimeSessionInfo,
    SessionSignalResult,
)


@dataclass
class _PtySession:
    session_id: str
    executable: str
    process: asyncio.subprocess.Process
    master_fd: int
    columns: int
    rows: int
    started_at: datetime
    create_time_ms: int | None = None
    cwd: str | None = None
    state: CommandSessionState = "running"
    finished_at: datetime | None = None
    returncode: int | None = None
    output: str = ""
    output_truncated: bool = False
    error: str | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None
    output_event: asyncio.Event = field(default_factory=asyncio.Event)


def resolve_pty_executable(name: str, *, search_path: str) -> str | None:
    candidate = shutil.which(name, path=search_path)
    if candidate is None:
        return None
    resolved = Path(candidate).resolve()
    trusted_directories = {Path(item).resolve() for item in search_path.split(":") if item}
    if resolved.parent not in trusted_directories or not resolved.is_file():
        return None
    return str(resolved)


def _set_window_size(fd: int, columns: int, rows: int) -> None:
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _prepare_pty_child(slave_fd: int) -> None:
    import fcntl
    import termios

    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


class PtySessionManager:
    def __init__(
        self,
        *,
        allowlist: set[str],
        timeout_s: float,
        max_output_bytes: int,
        max_input_bytes: int,
        max_active: int = 1,
        history_limit: int = 100,
        exec_search_path: str = TRUSTED_GENERIC_EXEC_PATH,
        child_env: dict[str, str] | None = None,
        personal_mode: bool = False,
        roots: list[Path] | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.max_input_bytes = max_input_bytes
        self.max_active = max_active
        self.history_limit = history_limit
        self.exec_search_path = exec_search_path
        self.child_env = child_env or {"PATH": exec_search_path}
        self.personal_mode = personal_mode
        self.roots = roots or []
        self._sessions: dict[str, _PtySession] = {}
        self._lock = asyncio.Lock()

    def _snapshot(self, request_id: str, session: _PtySession) -> PtySessionSnapshot:
        return PtySessionSnapshot(
            request_id=request_id,
            session_id=session.session_id,
            executable=session.executable,
            pid=session.process.pid,
            create_time_ms=session.create_time_ms,
            state=session.state,
            cwd=session.cwd,
            columns=session.columns,
            rows=session.rows,
            started_at=session.started_at,
            finished_at=session.finished_at,
            returncode=session.returncode,
            output=session.output,
            output_truncated=session.output_truncated,
            error=session.error,
        )

    def _error_snapshot(self, request_id: str, session_id: str, error: str) -> PtySessionSnapshot:
        return PtySessionSnapshot(
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
        columns: int,
        rows: int,
    ) -> PtySessionSnapshot:
        if os.name != "posix":
            return self._error_snapshot(
                request_id, session_id, "PTY sessions are supported only on POSIX Agents"
            )
        policy_error = validate_pty_argv(argv)
        if policy_error is not None:
            return self._error_snapshot(request_id, session_id, policy_error)
        executable = argv[0]
        if executable not in self.allowlist:
            return self._error_snapshot(
                request_id, session_id, f"PTY executable not allowed: {executable}"
            )
        resolved = resolve_pty_executable(executable, search_path=self.exec_search_path)
        if resolved is None:
            return self._error_snapshot(
                request_id,
                session_id,
                f"PTY executable not found in trusted path: {executable}",
            )
        try:
            resolved_cwd = resolve_allowed_directory(cwd, self.roots) if cwd is not None else None
            process_env = merge_command_env(
                self.child_env,
                env_overrides or {},
                personal_mode=self.personal_mode,
            )
        except (OSError, PermissionError, ValueError) as exc:
            return self._error_snapshot(request_id, session_id, str(exc))

        async with self._lock:
            self._prune_completed()
            if len(self._sessions) >= self.history_limit:
                return self._error_snapshot(request_id, session_id, "PTY session history is full")
            active = sum(1 for session in self._sessions.values() if session.state == "running")
            if active >= self.max_active:
                return self._error_snapshot(request_id, session_id, "too many active PTY sessions")
            if session_id in self._sessions:
                return self._error_snapshot(request_id, session_id, "PTY session already exists")

            master_fd, slave_fd = os.openpty()
            try:
                os.set_blocking(master_fd, False)
                _set_window_size(slave_fd, columns, rows)
                process = await asyncio.create_subprocess_exec(
                    resolved,
                    *argv[1:],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=resolved_cwd,
                    env={
                        **process_env,
                        "TERM": process_env.get("TERM", "xterm-256color"),
                        "LANG": process_env.get("LANG", "C.UTF-8"),
                        "LC_ALL": process_env.get("LC_ALL", "C.UTF-8"),
                    },
                    preexec_fn=lambda: _prepare_pty_child(slave_fd),
                )
            except Exception as exc:
                os.close(master_fd)
                os.close(slave_fd)
                return self._error_snapshot(
                    request_id,
                    session_id,
                    f"PTY session start failed: {type(exc).__name__}",
                )
            os.close(slave_fd)

            session = _PtySession(
                session_id=session_id,
                executable=executable,
                process=process,
                master_fd=master_fd,
                columns=columns,
                rows=rows,
                started_at=datetime.now(UTC),
                create_time_ms=process_create_time_ms(process.pid),
                cwd=str(resolved_cwd) if resolved_cwd is not None else None,
            )
            self._sessions[session_id] = session
            session.task = asyncio.create_task(self._run(session))
            return self._snapshot(request_id, session)

    async def _wait_readable(self, fd: int) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def mark_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(fd, mark_ready)
        try:
            await ready
        finally:
            loop.remove_reader(fd)

    async def _capture_output(self, session: _PtySession) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        retained_bytes = 0
        while True:
            try:
                await self._wait_readable(session.master_fd)
                chunk = os.read(session.master_fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            remaining = max(0, self.max_output_bytes - retained_bytes)
            accepted = chunk[:remaining]
            if accepted:
                session.output += decoder.decode(accepted, final=False)
                retained_bytes += len(accepted)
                session.output_event.set()
            if len(chunk) > remaining:
                session.output_truncated = True
                session.output_event.set()
        tail = decoder.decode(b"", final=True)
        if tail:
            session.output += tail
            session.output_event.set()

    async def _terminate(self, session: _PtySession) -> None:
        if session.process.returncode is not None:
            return
        try:
            os.killpg(session.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.process.wait(), timeout=2)
        except TimeoutError:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await session.process.wait()

    async def _run(self, session: _PtySession) -> None:
        reader = asyncio.create_task(self._capture_output(session))
        terminal_state: CommandSessionState = "failed"
        terminal_error: str | None = None
        try:
            await asyncio.wait_for(session.process.wait(), timeout=self.timeout_s)
            session.returncode = session.process.returncode
            if session.cancel_requested:
                terminal_state = "cancelled"
                terminal_error = "PTY session cancelled"
            else:
                terminal_state = "completed"
        except TimeoutError:
            await self._terminate(session)
            session.returncode = session.process.returncode
            terminal_state = "timed_out"
            terminal_error = "PTY session timed out"
        except asyncio.CancelledError:
            await self._terminate(session)
            session.returncode = session.process.returncode
            terminal_state = "cancelled"
            terminal_error = "PTY session cancelled"
        except Exception as exc:
            await self._terminate(session)
            session.returncode = session.process.returncode
            terminal_state = "failed"
            terminal_error = f"PTY session failed: {type(exc).__name__}"
        finally:
            try:
                await asyncio.wait_for(reader, timeout=2)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            except Exception as exc:
                if terminal_state == "completed":
                    terminal_state = "failed"
                    terminal_error = f"PTY output capture failed: {type(exc).__name__}"
            try:
                os.close(session.master_fd)
            except OSError:
                pass
            session.state = terminal_state
            session.error = terminal_error
            session.finished_at = datetime.now(UTC)
            session.output_event.set()

    async def list_infos(self, *, include_completed: bool) -> list[RuntimeSessionInfo]:
        sessions = list(self._sessions.values())
        if not include_completed:
            sessions = [session for session in sessions if session.state == "running"]
        return [
            RuntimeSessionInfo(
                session_id=session.session_id,
                kind="pty",
                executable=session.executable,
                pid=session.process.pid,
                create_time_ms=session.create_time_ms,
                state=session.state,
                cwd=session.cwd,
                started_at=session.started_at,
                finished_at=session.finished_at,
                returncode=session.returncode,
                output_chars=len(session.output),
                output_truncated=session.output_truncated,
            )
            for session in sessions
        ]

    async def status(self, request_id: str, session_id: str) -> PtySessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            return self._error_snapshot(request_id, session_id, "PTY session not found")
        return self._snapshot(request_id, session)

    async def output(
        self, request_id: str, session_id: str, *, offset: int, max_chars: int
    ) -> PtySessionOutput:
        session = self._sessions.get(session_id)
        if session is None:
            return PtySessionOutput(
                request_id=request_id,
                session_id=session_id,
                state="failed",
                rejected=True,
                error="PTY session not found",
            )
        if offset > len(session.output):
            return PtySessionOutput(
                request_id=request_id,
                session_id=session_id,
                state=session.state,
                next_offset=len(session.output),
                output_truncated=session.output_truncated,
                rejected=True,
                error="PTY output offset exceeds retained output",
            )
        output = session.output[offset : offset + max_chars]
        return PtySessionOutput(
            request_id=request_id,
            session_id=session_id,
            state=session.state,
            output=output,
            next_offset=offset + len(output),
            output_truncated=session.output_truncated,
            error=session.error,
        )

    async def output_lines(
        self,
        request_id: str,
        session_id: str,
        *,
        offset: int,
        max_lines: int,
        wait_ms: int = 0,
    ) -> PtySessionLineOutput:
        session = self._sessions.get(session_id)
        if session is None:
            return PtySessionLineOutput(
                request_id=request_id,
                session_id=session_id,
                state="failed",
                rejected=True,
                error="PTY session not found",
            )
        page, waited_ms, wait_timed_out = await wait_for_line_page(
            lambda: session.output,
            lambda: session.state == "running",
            lambda: not session.output_truncated,
            session.output_event,
            offset=offset,
            max_lines=max_lines,
            wait_ms=wait_ms,
        )
        return PtySessionLineOutput(
            request_id=request_id,
            session_id=session_id,
            state=session.state,
            content=page.content,
            total_lines=page.total_lines,
            start_line=page.start_line,
            next_line=page.next_line,
            eof=page.eof,
            pending_partial=page.pending_partial,
            output_truncated=session.output_truncated,
            waited_ms=waited_ms,
            wait_timed_out=wait_timed_out,
            error=session.error,
        )

    async def signal_session(
        self, request_id: str, session_id: str, requested_signal: ProcessSignal
    ) -> SessionSignalResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, rejected=True, error="session not found",
            )
        if session.state != "running" or session.process.returncode is not None:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, pid=session.process.pid,
                create_time_ms=session.create_time_ms, rejected=True,
                error="session is not running",
            )
        actual_create_time_ms = process_create_time_ms(session.process.pid)
        if session.create_time_ms is None or actual_create_time_ms != session.create_time_ms:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, pid=session.process.pid,
                create_time_ms=session.create_time_ms, rejected=True,
                error="session process identity changed or is unreadable",
            )
        signal_value = process_signal_value(requested_signal)
        if signal_value is None:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, pid=session.process.pid,
                create_time_ms=session.create_time_ms, rejected=True,
                error=f"signal is unavailable on this platform: {requested_signal}",
            )
        try:
            if os.name == "posix":
                os.killpg(session.process.pid, signal_value)
            else:
                session.process.send_signal(signal_value)
        except ProcessLookupError:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, pid=session.process.pid,
                create_time_ms=session.create_time_ms, rejected=True,
                error="session process no longer exists",
            )
        except PermissionError:
            return SessionSignalResult(
                request_id=request_id, session_id=session_id, kind="pty",
                signal=requested_signal, pid=session.process.pid,
                create_time_ms=session.create_time_ms, rejected=True,
                error="permission denied",
            )
        return SessionSignalResult(
            request_id=request_id, session_id=session_id, kind="pty",
            signal=requested_signal, pid=session.process.pid,
            create_time_ms=session.create_time_ms, signal_sent=True,
        )

    async def input(self, request_id: str, session_id: str, data: str) -> PtySessionInputResult:
        session = self._sessions.get(session_id)
        if session is None:
            return PtySessionInputResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY session not found",
            )
        if session.state != "running":
            return PtySessionInputResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY session is not running",
            )
        encoded = data.encode("utf-8")
        if len(encoded) > self.max_input_bytes:
            return PtySessionInputResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY input exceeds byte limit",
            )
        try:
            written = os.write(session.master_fd, encoded)
        except BlockingIOError:
            return PtySessionInputResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY input would block",
            )
        except OSError:
            return PtySessionInputResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY input failed",
            )
        return PtySessionInputResult(
            request_id=request_id,
            session_id=session_id,
            accepted_bytes=written,
        )

    async def resize(
        self, request_id: str, session_id: str, *, columns: int, rows: int
    ) -> PtySessionResizeResult:
        session = self._sessions.get(session_id)
        if session is None:
            return PtySessionResizeResult(
                request_id=request_id,
                session_id=session_id,
                rejected=True,
                error="PTY session not found",
            )
        if session.state != "running":
            return PtySessionResizeResult(
                request_id=request_id,
                session_id=session_id,
                columns=session.columns,
                rows=session.rows,
                rejected=True,
                error="PTY session is not running",
            )
        try:
            _set_window_size(session.master_fd, columns, rows)
        except OSError:
            return PtySessionResizeResult(
                request_id=request_id,
                session_id=session_id,
                columns=session.columns,
                rows=session.rows,
                rejected=True,
                error="PTY resize failed",
            )
        session.columns = columns
        session.rows = rows
        return PtySessionResizeResult(
            request_id=request_id,
            session_id=session_id,
            columns=columns,
            rows=rows,
        )

    async def cancel(self, request_id: str, session_id: str) -> PtySessionSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            return self._error_snapshot(request_id, session_id, "PTY session not found")
        if session.state != "running" or session.task is None:
            return self._snapshot(request_id, session)
        session.cancel_requested = True
        await self._terminate(session)
        await asyncio.shield(session.task)
        return self._snapshot(request_id, session)

    async def discard(self, request_id: str, session_id: str) -> PtySessionDiscardResult:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return PtySessionDiscardResult(
                    request_id=request_id,
                    session_id=session_id,
                    rejected=True,
                    error="PTY session not found",
                )
            if session.state == "running":
                return PtySessionDiscardResult(
                    request_id=request_id,
                    session_id=session_id,
                    rejected=True,
                    error="running PTY session cannot be discarded",
                )
            self._sessions.pop(session_id, None)
        return PtySessionDiscardResult(
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
