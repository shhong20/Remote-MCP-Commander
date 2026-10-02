from __future__ import annotations

from typing import Any

import httpx

from remote_mcp_commander.config import Settings
from remote_mcp_commander.protocol import (
    AgentInfo,
    AgentList,
    AuditQueryResult,
    CommandDiscoveryResult,
    CommandResult,
    CommandSessionDiscardResult,
    CommandSessionInputResult,
    CommandSessionLineOutput,
    CommandSessionOutput,
    CommandSessionSnapshot,
    CommandSessionStdinCloseResult,
    DirectoryListResult,
    DirectoryTreeResult,
    DocumentPreviewResult,
    FileAppendResult,
    FileEditResult,
    FileInfoResult,
    FileLineReadResult,
    FileReadManyResult,
    FileReadResult,
    FileRootListResult,
    FileSearchResult,
    FileSearchSessionListResult,
    FileSearchSessionPage,
    FileSearchSessionStopResult,
    FileTailResult,
    FileWriteResult,
    GitStatusResult,
    PathMutationResult,
    PingResponse,
    PortLookupResult,
    ProcessInfoResult,
    ProcessListResult,
    ProcessSignalResult,
    ProcessTerminateResult,
    PtySessionDiscardResult,
    PtySessionInputResult,
    PtySessionLineOutput,
    PtySessionOutput,
    PtySessionResizeResult,
    PtySessionSnapshot,
    RuntimeSessionFilter,
    ServiceActionResult,
    ServiceLogsResult,
    ServiceStatusResult,
    SessionListResult,
    SessionSignalResult,
    SystemHealthResult,
    TreeInspectResult,
    TreeMutationResult,
)


class GatewayAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"gateway request failed ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class GatewayClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.control_token}"}
        async with httpx.AsyncClient(
            base_url=self.settings.gateway_http,
            headers=headers,
            timeout=timeout_s or self.settings.mcp_gateway_timeout_s,
            transport=self.transport,
        ) as client:
            response = await client.request(method, path, json=json_body, params=params)

        if response.is_error:
            try:
                payload = response.json()
                detail = str(payload.get("detail", "gateway error"))
            except ValueError:
                detail = "gateway error"
            raise GatewayAPIError(response.status_code, detail)
        return response.json()

    async def list_devices(self) -> AgentList:
        payload = await self._request("GET", "/api/v1/agents")
        return AgentList.model_validate(payload)

    async def list_audit_records(
        self,
        *,
        limit: int = 50,
        event: str | None = None,
        agent_id: str | None = None,
    ) -> AuditQueryResult:
        params: dict[str, Any] = {"limit": limit}
        if event is not None:
            params["event"] = event
        if agent_id is not None:
            params["agent_id"] = agent_id
        payload = await self._request("GET", "/api/v1/audit", params=params)
        return AuditQueryResult.model_validate(payload)

    async def device_info(self, agent_id: str) -> AgentInfo:
        payload = await self._request("GET", f"/api/v1/agents/{agent_id}")
        return AgentInfo.model_validate(payload)

    async def ping_device(self, agent_id: str) -> PingResponse:
        payload = await self._request("POST", f"/api/v1/agents/{agent_id}/ping")
        return PingResponse.model_validate(payload)

    async def system_health(self, agent_id: str) -> SystemHealthResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/diagnostics/health",
        )
        return SystemHealthResult.model_validate(payload)

    async def lookup_port(self, agent_id: str, port: int) -> PortLookupResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/diagnostics/port",
            json_body={"port": port},
        )
        return PortLookupResult.model_validate(payload)

    async def service_logs(
        self, agent_id: str, unit: str, *, lines: int = 100
    ) -> ServiceLogsResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/diagnostics/service-logs",
            json_body={"unit": unit, "lines": lines},
        )
        return ServiceLogsResult.model_validate(payload)

    async def git_status(self, agent_id: str, path: str) -> GitStatusResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/diagnostics/git-status",
            json_body={"path": path},
        )
        return GitStatusResult.model_validate(payload)

    async def list_commands(self, agent_id: str) -> CommandDiscoveryResult:
        payload = await self._request(
            "GET", f"/api/v1/agents/{agent_id}/commands/discovery"
        )
        return CommandDiscoveryResult.model_validate(payload)

    async def execute(
        self,
        agent_id: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/execute",
            json_body={
                "argv": argv, "cwd": cwd, "env": env or {}, "timeout_s": timeout_s,
            },
            timeout_s=max(
                self.settings.mcp_gateway_timeout_s,
                (timeout_s or self.settings.exec_timeout_s) + 5.0,
            ),
        )
        return CommandResult.model_validate(payload)

    async def signal_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        kind: str,
        requested_signal: str,
    ) -> SessionSignalResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/sessions/{session_id}/signal",
            json_body={"kind": kind, "signal": requested_signal},
        )
        return SessionSignalResult.model_validate(payload)

    async def start_command_session(
        self,
        agent_id: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandSessionSnapshot:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions",
            json_body={
                "argv": argv, "cwd": cwd, "env": env or {}, "timeout_s": timeout_s,
            },
        )
        return CommandSessionSnapshot.model_validate(payload)

    async def list_sessions(
        self,
        agent_id: str,
        *,
        kind: RuntimeSessionFilter = "all",
        include_completed: bool = False,
        limit: int = 100,
    ) -> SessionListResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/sessions",
            json_body={
                "kind": kind,
                "include_completed": include_completed,
                "limit": limit,
            },
        )
        return SessionListResult.model_validate(payload)

    async def command_session_status(
        self,
        agent_id: str,
        session_id: str,
    ) -> CommandSessionSnapshot:
        payload = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}",
        )
        return CommandSessionSnapshot.model_validate(payload)

    async def cancel_command_session(
        self,
        agent_id: str,
        session_id: str,
    ) -> CommandSessionSnapshot:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/cancel",
        )
        return CommandSessionSnapshot.model_validate(payload)

    async def write_command_input(
        self, agent_id: str, session_id: str, data: str
    ) -> CommandSessionInputResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/input",
            json_body={"data": data},
        )
        return CommandSessionInputResult.model_validate(payload)

    async def close_command_stdin(
        self, agent_id: str, session_id: str
    ) -> CommandSessionStdinCloseResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/stdin/close",
        )
        return CommandSessionStdinCloseResult.model_validate(payload)

    async def command_session_output(
        self,
        agent_id: str,
        session_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_chars: int = 8192,
    ) -> CommandSessionOutput:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/output",
            json_body={
                "stdout_offset": stdout_offset,
                "stderr_offset": stderr_offset,
                "max_chars": max_chars,
            },
        )
        return CommandSessionOutput.model_validate(payload)

    async def command_session_output_lines(
        self,
        agent_id: str,
        session_id: str,
        *,
        stream: str = "stdout",
        offset: int = 0,
        max_lines: int = 200,
        wait_ms: int = 0,
    ) -> CommandSessionLineOutput:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/output/lines",
            json_body={
                "stream": stream, "offset": offset, "max_lines": max_lines, "wait_ms": wait_ms
            },
            timeout_s=max(self.settings.mcp_gateway_timeout_s, wait_ms / 1000.0 + 3.0),
        )
        return CommandSessionLineOutput.model_validate(payload)

    async def discard_command_session(
        self,
        agent_id: str,
        session_id: str,
    ) -> CommandSessionDiscardResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions/{session_id}/discard",
        )
        return CommandSessionDiscardResult.model_validate(payload)

    async def start_pty_session(
        self,
        agent_id: str,
        argv: list[str],
        approval_id: str | None = None,
        approval_secret: str | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        columns: int = 80,
        rows: int = 24,
    ) -> PtySessionSnapshot:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/pty/sessions",
            json_body={
                "argv": argv,
                "cwd": cwd,
                "env": env or {},
                "columns": columns,
                "rows": rows,
                "approval_id": approval_id,
                "approval_secret": approval_secret,
            },
        )
        return PtySessionSnapshot.model_validate(payload)

    async def pty_session_status(self, agent_id: str, session_id: str) -> PtySessionSnapshot:
        payload = await self._request("GET", f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}")
        return PtySessionSnapshot.model_validate(payload)

    async def write_pty_input(
        self, agent_id: str, session_id: str, data: str
    ) -> PtySessionInputResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/input",
            json_body={"data": data},
        )
        return PtySessionInputResult.model_validate(payload)

    async def resize_pty_session(
        self, agent_id: str, session_id: str, *, columns: int, rows: int
    ) -> PtySessionResizeResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/resize",
            json_body={"columns": columns, "rows": rows},
        )
        return PtySessionResizeResult.model_validate(payload)

    async def pty_session_output(
        self,
        agent_id: str,
        session_id: str,
        *,
        offset: int = 0,
        max_chars: int = 8192,
    ) -> PtySessionOutput:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/output",
            json_body={"offset": offset, "max_chars": max_chars},
        )
        return PtySessionOutput.model_validate(payload)

    async def pty_session_output_lines(
        self,
        agent_id: str,
        session_id: str,
        *,
        offset: int = 0,
        max_lines: int = 200,
        wait_ms: int = 0,
    ) -> PtySessionLineOutput:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/output/lines",
            json_body={"offset": offset, "max_lines": max_lines, "wait_ms": wait_ms},
            timeout_s=max(self.settings.mcp_gateway_timeout_s, wait_ms / 1000.0 + 3.0),
        )
        return PtySessionLineOutput.model_validate(payload)

    async def cancel_pty_session(self, agent_id: str, session_id: str) -> PtySessionSnapshot:
        payload = await self._request(
            "POST", f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/cancel"
        )
        return PtySessionSnapshot.model_validate(payload)

    async def discard_pty_session(self, agent_id: str, session_id: str) -> PtySessionDiscardResult:
        payload = await self._request(
            "POST", f"/api/v1/agents/{agent_id}/pty/sessions/{session_id}/discard"
        )
        return PtySessionDiscardResult.model_validate(payload)

    async def list_file_roots(self, agent_id: str) -> FileRootListResult:
        payload = await self._request(
            "GET",
            f"/api/v1/agents/{agent_id}/files/roots",
        )
        return FileRootListResult.model_validate(payload)

    async def list_directory(
        self,
        agent_id: str,
        path: str,
        *,
        limit: int = 200,
    ) -> DirectoryListResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/list",
            json_body={"path": path, "limit": limit},
        )
        return DirectoryListResult.model_validate(payload)

    async def list_directory_tree(
        self,
        agent_id: str,
        path: str,
        *,
        depth: int = 2,
        include_hidden: bool = False,
        per_directory_limit: int = 100,
        max_entries: int = 1000,
    ) -> DirectoryTreeResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/tree-list",
            json_body={
                "path": path, "depth": depth, "include_hidden": include_hidden,
                "per_directory_limit": per_directory_limit, "max_entries": max_entries,
            },
        )
        return DirectoryTreeResult.model_validate(payload)

    async def file_info(self, agent_id: str, path: str) -> FileInfoResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/info",
            json_body={"path": path},
        )
        return FileInfoResult.model_validate(payload)

    async def preview_document(
        self,
        agent_id: str,
        path: str,
        *,
        page: int = 1,
        max_pages: int = 5,
        sheet: str | None = None,
        cell_range: str | None = None,
        max_rows: int = 200,
        max_chars: int = 65_536,
    ) -> DocumentPreviewResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/documents/preview",
            json_body={
                "path": path,
                "page": page,
                "max_pages": max_pages,
                "sheet": sheet,
                "cell_range": cell_range,
                "max_rows": max_rows,
                "max_chars": max_chars,
            },
            timeout_s=max(self.settings.mcp_gateway_timeout_s, 25.0),
        )
        return DocumentPreviewResult.model_validate(payload)

    async def read_file(
        self,
        agent_id: str,
        path: str,
        *,
        offset: int = 0,
        max_bytes: int = 65_536,
    ) -> FileReadResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/read",
            json_body={"path": path, "offset": offset, "max_bytes": max_bytes},
        )
        return FileReadResult.model_validate(payload)

    async def read_file_lines(
        self, agent_id: str, path: str, *, offset: int = 0, max_lines: int = 200
    ) -> FileLineReadResult:
        payload = await self._request(
            "POST", f"/api/v1/agents/{agent_id}/files/read-lines",
            json_body={"path": path, "offset": offset, "max_lines": max_lines},
        )
        return FileLineReadResult.model_validate(payload)

    async def tail_file(
        self, agent_id: str, path: str, *, lines: int = 100, max_bytes: int = 262_144
    ) -> FileTailResult:
        payload = await self._request(
            "POST", f"/api/v1/agents/{agent_id}/files/tail",
            json_body={"path": path, "lines": lines, "max_bytes": max_bytes},
        )
        return FileTailResult.model_validate(payload)

    async def read_many_files(
        self,
        agent_id: str,
        paths: list[str],
        *,
        max_bytes_per_file: int = 32_768,
        max_total_bytes: int = 262_144,
    ) -> FileReadManyResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/read-many",
            json_body={
                "paths": paths,
                "max_bytes_per_file": max_bytes_per_file,
                "max_total_bytes": max_total_bytes,
            },
        )
        return FileReadManyResult.model_validate(payload)

    async def write_file(
        self,
        agent_id: str,
        path: str,
        content: str,
        *,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> FileWriteResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/write",
            json_body={
                "path": path,
                "content": content,
                "overwrite": overwrite,
                "expected_sha256": expected_sha256,
            },
        )
        return FileWriteResult.model_validate(payload)

    async def append_file(
        self,
        agent_id: str,
        path: str,
        content: str,
        *,
        expected_sha256: str | None = None,
    ) -> FileAppendResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/append",
            json_body={
                "path": path, "content": content,
                "expected_sha256": expected_sha256,
            },
        )
        return FileAppendResult.model_validate(payload)

    async def edit_file(
        self,
        agent_id: str,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> FileEditResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/edit",
            json_body={
                "path": path,
                "old_text": old_text,
                "new_text": new_text,
                "replace_all": replace_all,
            },
        )
        return FileEditResult.model_validate(payload)

    async def search_files(
        self,
        agent_id: str,
        root: str,
        query: str,
        *,
        mode: str = "files",
        file_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> FileSearchResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/search",
            json_body={
                "root": root,
                "query": query,
                "mode": mode,
                "file_glob": file_glob,
                "case_sensitive": case_sensitive,
                "max_results": max_results,
            },
        )
        return FileSearchResult.model_validate(payload)

    async def start_search_session(
        self,
        agent_id: str,
        root: str,
        query: str,
        *,
        mode: str = "files",
        file_glob: str | None = None,
        case_sensitive: bool = False,
        include_hidden: bool = False,
        page_size: int = 50,
        max_results: int = 1000,
    ) -> FileSearchSessionPage:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/search/sessions",
            json_body={
                "root": root,
                "query": query,
                "mode": mode,
                "file_glob": file_glob,
                "case_sensitive": case_sensitive,
                "include_hidden": include_hidden,
                "page_size": page_size,
                "max_results": max_results,
            },
        )
        return FileSearchSessionPage.model_validate(payload)

    async def list_search_sessions(self, agent_id: str) -> FileSearchSessionListResult:
        payload = await self._request("GET", f"/api/v1/agents/{agent_id}/files/search/sessions")
        return FileSearchSessionListResult.model_validate(payload)

    async def more_search_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 50,
    ) -> FileSearchSessionPage:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/search/sessions/{session_id}/more",
            json_body={"offset": offset, "limit": limit},
        )
        return FileSearchSessionPage.model_validate(payload)

    async def stop_search_session(
        self, agent_id: str, session_id: str
    ) -> FileSearchSessionStopResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/search/sessions/{session_id}/stop",
        )
        return FileSearchSessionStopResult.model_validate(payload)

    async def inspect_tree(
        self,
        agent_id: str,
        path: str,
        *,
        max_entries: int = 5000,
        max_total_bytes: int = 268_435_456,
    ) -> TreeInspectResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/tree/inspect",
            json_body={
                "path": path,
                "max_entries": max_entries,
                "max_total_bytes": max_total_bytes,
            },
        )
        return TreeInspectResult.model_validate(payload)

    async def mutate_tree(
        self,
        agent_id: str,
        operation: str,
        path: str,
        expected_tree_sha256: str,
        *,
        destination: str | None = None,
        max_entries: int = 5000,
        max_total_bytes: int = 268_435_456,
    ) -> TreeMutationResult:
        action = "copy" if operation == "copy_tree" else "delete"
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/tree/{action}",
            json_body={
                "path": path,
                "destination": destination,
                "expected_tree_sha256": expected_tree_sha256,
                "max_entries": max_entries,
                "max_total_bytes": max_total_bytes,
            },
        )
        return TreeMutationResult.model_validate(payload)

    async def mutate_path(
        self,
        agent_id: str,
        operation: str,
        path: str,
        *,
        destination: str | None = None,
        parents: bool = False,
        overwrite: bool = False,
    ) -> PathMutationResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/{operation}",
            json_body={
                "path": path,
                "destination": destination,
                "parents": parents,
                "overwrite": overwrite,
            },
        )
        return PathMutationResult.model_validate(payload)

    async def list_processes(self, agent_id: str, *, limit: int = 100) -> ProcessListResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/processes",
            json_body={"limit": limit},
        )
        return ProcessListResult.model_validate(payload)

    async def process_info(self, agent_id: str, pid: int) -> ProcessInfoResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/processes/info",
            json_body={"pid": pid},
        )
        return ProcessInfoResult.model_validate(payload)

    async def service_status(self, agent_id: str, unit: str) -> ServiceStatusResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/services/status",
            json_body={"unit": unit},
        )
        return ServiceStatusResult.model_validate(payload)

    async def terminate_process(
        self,
        agent_id: str,
        pid: int,
        expected_create_time_ms: int,
        approval_id: str,
        approval_secret: str,
    ) -> ProcessTerminateResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/processes/terminate",
            json_body={
                "pid": pid,
                "expected_create_time_ms": expected_create_time_ms,
                "approval_id": approval_id,
                "approval_secret": approval_secret,
            },
        )
        return ProcessTerminateResult.model_validate(payload)

    async def signal_process(
        self,
        agent_id: str,
        pid: int,
        expected_create_time_ms: int,
        requested_signal: str,
        approval_id: str | None = None,
        approval_secret: str | None = None,
    ) -> ProcessSignalResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/processes/signal",
            json_body={
                "pid": pid,
                "expected_create_time_ms": expected_create_time_ms,
                "signal": requested_signal,
                "approval_id": approval_id,
                "approval_secret": approval_secret,
            },
        )
        return ProcessSignalResult.model_validate(payload)

    async def service_action(
        self,
        agent_id: str,
        unit: str,
        action: str,
        approval_id: str,
        approval_secret: str,
    ) -> ServiceActionResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/services/action",
            json_body={
                "unit": unit,
                "action": action,
                "approval_id": approval_id,
                "approval_secret": approval_secret,
            },
        )
        return ServiceActionResult.model_validate(payload)
