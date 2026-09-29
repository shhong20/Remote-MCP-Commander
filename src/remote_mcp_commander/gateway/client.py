from __future__ import annotations

from typing import Any

import httpx

from remote_mcp_commander.config import Settings
from remote_mcp_commander.protocol import (
    AgentInfo,
    AgentList,
    CommandResult,
    CommandSessionDiscardResult,
    CommandSessionOutput,
    CommandSessionSnapshot,
    DirectoryListResult,
    FileInfoResult,
    FileReadResult,
    FileRootListResult,
    FileWriteResult,
    GitStatusResult,
    PingResponse,
    PortLookupResult,
    ProcessListResult,
    ProcessTerminateResult,
    ServiceActionResult,
    ServiceLogsResult,
    ServiceStatusResult,
    SystemHealthResult,
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
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.control_token}"}
        async with httpx.AsyncClient(
            base_url=self.settings.gateway_http,
            headers=headers,
            timeout=self.settings.mcp_gateway_timeout_s,
            transport=self.transport,
        ) as client:
            response = await client.request(method, path, json=json_body)

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

    async def execute(self, agent_id: str, argv: list[str]) -> CommandResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/execute",
            json_body={"argv": argv},
        )
        return CommandResult.model_validate(payload)

    async def start_command_session(
        self,
        agent_id: str,
        argv: list[str],
    ) -> CommandSessionSnapshot:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/commands/sessions",
            json_body={"argv": argv},
        )
        return CommandSessionSnapshot.model_validate(payload)

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

    async def file_info(self, agent_id: str, path: str) -> FileInfoResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/files/info",
            json_body={"path": path},
        )
        return FileInfoResult.model_validate(payload)

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

    async def list_processes(self, agent_id: str, *, limit: int = 100) -> ProcessListResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/processes",
            json_body={"limit": limit},
        )
        return ProcessListResult.model_validate(payload)

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
