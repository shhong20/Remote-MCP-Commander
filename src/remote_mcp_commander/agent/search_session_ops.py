from __future__ import annotations

import asyncio
from dataclasses import dataclass

from remote_mcp_commander.agent.search_ops import search_files
from remote_mcp_commander.protocol import (
    FileSearchMatch,
    FileSearchSessionPage,
    FileSearchSessionStopResult,
)


@dataclass
class _SearchSession:
    session_id: str
    matches: list[FileSearchMatch]
    scanned_files: int
    truncated: bool
    cursor: int = 0


class FileSearchSessionManager:
    def __init__(self, *, roots, max_sessions: int = 20) -> None:
        self.roots = roots
        self.max_sessions = max_sessions
        self._sessions: dict[str, _SearchSession] = {}
        self._lock = asyncio.Lock()

    def _error_page(self, request_id: str, session_id: str, error: str) -> FileSearchSessionPage:
        return FileSearchSessionPage(
            request_id=request_id,
            session_id=session_id,
            rejected=True,
            exhausted=True,
            error=error,
        )

    def _page(
        self,
        request_id: str,
        session: _SearchSession,
        limit: int,
    ) -> FileSearchSessionPage:
        start = session.cursor
        end = min(len(session.matches), start + limit)
        matches = session.matches[start:end]
        session.cursor = end
        remaining = len(session.matches) - end
        return FileSearchSessionPage(
            request_id=request_id,
            session_id=session.session_id,
            matches=matches,
            scanned_files=session.scanned_files,
            returned_count=len(matches),
            remaining=remaining,
            exhausted=remaining == 0,
            truncated=session.truncated,
        )

    async def start(
        self,
        request_id: str,
        session_id: str,
        root: str,
        query: str,
        *,
        mode: str,
        file_glob: str | None,
        case_sensitive: bool,
        page_size: int,
        max_results: int,
        include_hidden: bool = False,
    ) -> FileSearchSessionPage:
        result = await search_files(
            request_id,
            root,
            query,
            roots=self.roots,
            mode=mode,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            max_results=max_results,
            include_hidden=include_hidden,
        )
        if result.rejected:
            return self._error_page(request_id, session_id, result.error or "search failed")

        async with self._lock:
            if session_id in self._sessions:
                return self._error_page(request_id, session_id, "search session already exists")
            while len(self._sessions) >= self.max_sessions:
                oldest = next(iter(self._sessions))
                self._sessions.pop(oldest, None)
            session = _SearchSession(
                session_id=session_id,
                matches=result.matches,
                scanned_files=result.scanned_files,
                truncated=result.truncated,
            )
            self._sessions[session_id] = session
            return self._page(request_id, session, page_size)

    async def more(self, request_id: str, session_id: str, limit: int) -> FileSearchSessionPage:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return self._error_page(request_id, session_id, "search session not found")
            return self._page(request_id, session, limit)

    async def stop(self, request_id: str, session_id: str) -> FileSearchSessionStopResult:
        async with self._lock:
            stopped = self._sessions.pop(session_id, None) is not None
        return FileSearchSessionStopResult(
            request_id=request_id,
            session_id=session_id,
            stopped=stopped,
            rejected=not stopped,
            error=None if stopped else "search session not found",
        )


class FileSearchStartDispatcher:
    def __init__(self, manager: FileSearchSessionManager, *, max_active: int = 2) -> None:
        self.manager = manager
        self.max_active = max_active
        self._tasks: set[asyncio.Task[None]] = set()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            pass

    async def submit(self, request, send_text) -> bool:
        if len(self._tasks) >= self.max_active:
            page = self.manager._error_page(
                request.request_id,
                request.session_id,
                "too many active search sessions",
            )
            await send_text(page.model_dump_json())
            return False
        task = asyncio.create_task(self._run(request, send_text))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return True

    async def _run(self, request, send_text) -> None:
        result = await self.manager.start(
            request.request_id,
            request.session_id,
            request.root,
            request.query,
            mode=request.mode,
            file_glob=request.file_glob,
            case_sensitive=request.case_sensitive,
            page_size=request.page_size,
            max_results=request.max_results,
            include_hidden=request.include_hidden,
        )
        await send_text(result.model_dump_json())

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
